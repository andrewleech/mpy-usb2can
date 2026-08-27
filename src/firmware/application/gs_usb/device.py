"""
gs_usb data plane: joins CanCore's per-frame callbacks to the two bulk
endpoints GsUsbUsbDevice exposes, and is the runnable object that owns
that wiring's asyncio lifecycle.

Endpoint discipline
--------------------
machine.USBDevice.submit_xfer() accepts only one outstanding transfer per
endpoint at a time. Bulk IN carries two kinds of traffic, received
frames and TX echoes, so `_OutQueue` below is a single preallocated
queue shared by both: a CAN interrupt that fires while a previous frame
is still being clocked out over USB queues behind it instead of being
lost. Bulk OUT only ever has one host frame being unpacked at a time,
into a single reused buffer, because the endpoint is re-armed only once
that frame is fully resolved: submitted to CanCore, rejected outright
with its echo already queued, or retried until CanCore has room for it
(F10, see `_try_submit_pending`).

submit_xfer() itself is not assumed to only ever return False when it
declines a transfer: on target it can also raise OSError (F19), so every
call site catches that rather than letting it reach the callback
trampoline. A USB bus reset is handled the same defensive way: TinyUSB
drops any transfer that was still outstanding on either endpoint without
ever calling xfer_cb for it (F1), so `reset()` below clears the latched
in-flight/armed state that would otherwise wedge both endpoints for the
rest of the session, rather than relying on a completion that will never
arrive.

Echo discipline (R12)
----------------------
Exactly one echo per accepted TX frame, never lost or duplicated, is
this module's central contract. Two policies can produce that echo, and
`_try_submit_pending()`/`_on_tx_complete()` are exactly where they
diverge; everything else (queueing, USB dispatch, correlation storage)
is shared between them:

- echo-on-write (candleLight-like, ACTIVE DEFAULT, `echo_on_write=True`):
  the echo is built and queued the instant a frame is handed to
  `CanCore.submit()`, accepted or not. This is what real
  candleLight-family firmware does, and the host sees its frame
  accounted for without waiting on bus arbitration.
- echo-on-completion (F21, not the active default): the echo is
  deferred until `CanCore`'s `on_tx_complete` fires for the slot
  `submit()` returned, correlated through the table below. Constructing
  with `echo_on_write=False` selects this policy; `_send_echo()` and
  the queue it feeds are already policy-agnostic.

A transmit `CanCore` declines (`submit()` returns None) is held and
retried rather than echoed and dropped (F10): the host is still entitled
to have that frame transmitted once the controller can take it, so
dropping it would lose a frame the host believes it sent.

Both causes of a decline are treated alike because nothing distinguishes
them here. `machine.CAN` returns None when the 3-deep TX queue is full
and also when a frame carrying the same CAN id is already pending (F31),
and neither the port nor the mock reports which. Treating them alike is
also the safe choice: the same-id case clears as soon as the pending
frame completes, exactly like the queue-full case. See
`_try_submit_pending`'s docstring.

Correlation table
-------------------
Indexed by channel then directly by the slot number `submit()` and
`on_tx_complete()` already agree on, sized to `tx_queue_len` (the number
of slots CanCore can ever hand out per channel). Only read and written
under echo-on-completion; echo-on-write never populates it. A slot
reused while its previous occupant's completion was never observed (a
channel restarted without the completions that would normally have
drained it) is a leak in the making from outside this class's control;
`_reserve_correlation()` flushes that stale echo immediately, out of
order, rather than overwriting it silently.

`stop()` and the public `flush_channel()` cover the same failure mode at
shutdown: once a channel is detached, its `on_tx_complete` will never
fire again, so any correlation entry still outstanding, and any TX frame
still held under backpressure for that channel (F10), is flushed before
detaching. This does not cover a channel being reset independently of
this class: `gs_usb.control`'s MODE RESET stops a channel directly
through `CanCore.stop()`, with no hook back into this class today.
Under the active echo-on-write default the correlation-table half of
that gap is unreachable, since no correlation entry is ever outstanding
to leak; the backpressure half is reachable regardless of echo policy,
though only in the narrow window where CanCore's queue was actually full
for that channel at the moment it was independently stopped. Wiring
`flush_channel()` into that stop path is required before either gap is
fully closed in production.

Overflow signalling
---------------------
`gs_host_frame.flags` carries `CAN_FLAG_OVERFLOW` to tell the host
frames were dropped before this one. Two independent sources set it on
an RX frame: CanCore's own `errors` argument to `on_rx`
(`RECV_ERR_FULL`/`RECV_ERR_OVERRUN`, already attached by the hardware
and CanCore to the first frame that made it through after a drop), and
this module's own `_rx_overflow_pending`, set when the output queue has
no free slot for an RX frame, or when an echo evicts one already queued
(see `_OutQueue`). Both feed the same wire bit; the host does not
distinguish where a drop happened, only that one did.

Zero allocation (R10)
------------------------
Every buffer this class touches on the RX/TX hot path, the shared output
queue's frame buffers, the OUT-transfer scratch buffer, the unpack
destination and its precomputed per-length memoryview slices, is
allocated once, in `__init__`; the invariant is asserted by the
`gc.mem_alloc()` measurement in `test_gs_usb_device.py`'s
`TestZeroAllocationSteadyState`. This extends to the wire `can_id`
field: `protocol.pack_classic_frame_into`/`unpack_classic_frame_into`
work in decomposed ident/eff/rtr/err terms rather than a combined 32-bit
`can_id`, so this module never calls `combine_can_id()`/`split_can_id()`
on the hot path, the one place a fresh extended-id frame would otherwise
force a heap allocation on a 32-bit target (F15; see protocol.py's
module docstring). The output queue's sequence counter wraps at 16 bits
for the same reason (F27): an ever-growing plain counter would
eventually outgrow the small-int range too.
"""

import asyncio
import logging

from gs_usb import protocol, usb_device

log = logging.getLogger("gs_usb.device")

# machine.CAN's own CAN_MSG_FLAG_RTR/CAN_MSG_FLAG_EXT_ID encoding
# (extmod/machine_can_port.h, also reproduced in test/mock_can.py),
# duplicated here as plain ints so this module can exchange flags with
# CanCore's on_rx/submit() without importing machine at load time. Any
# can_class handed to CanCore must use this same bit encoding; a
# different one would make this module set the wrong flag bits on
# transmit and misclassify extended-id frames on receive.
_CAN_MSG_FLAG_RTR = 1 << 0
_CAN_MSG_FLAG_EXT_ID = 1 << 1

# machine.USBDevice.xfer_cb's XFER_SUCCESS (machine.USBDevice.rst).
# Reproduced rather than imported so this module never touches machine at
# load time; the unix port's machine simulation has no USBDevice at all.
_XFER_SUCCESS = 0

# GS_MAX_TX_URBS ([[20260827_linux_gs_usb_driver.md]] section 6): the
# Linux host's own bound on concurrently outstanding, un-drained
# transmits per channel. The shared RX/echo output queue below is sized
# to the same number, so a compliant host can never present more frames
# in flight, of either kind, than this queue can hold without loss. Not
# scaled by num_channels: RX traffic shares this queue too and carries no
# such per-channel accounting of its own, and a flat number keeps the
# preallocation one easily-reasoned-about size rather than one that grows
# with channel count for a bound that this single bulk-IN pipe enforces
# regardless of how many channels feed it.
OUT_QUEUE_LEN = 10

# _OutQueue's sequence numbers wrap at 16 bits (F27): see _seq_before.
_SEQ_MASK = 0xFFFF
_SEQ_HALF = 0x8000


def _seq_before(a, b):
    """
    True if sequence number `a` was assigned strictly before `b`, given
    16-bit wraparound sequence numbers.

    A plain, ever-incrementing counter would eventually need more than a
    small int can hold on a 32-bit target (R10); wrapping it at 16 bits
    avoids that, but a direct `a < b` comparison breaks the instant `a`
    wraps back to 0 while `b` is still near the top of the range. Signed
    subtraction modulo the wrap width recovers the right answer: the
    difference is negative (equivalently, its low 16 bits are at or past
    the halfway point) exactly when `a` is the older of the two, for any
    pair whose true separation is less than half the wrap width, which
    holds here since the queue's capacity is far smaller than 0x10000.
    """
    return ((a - b) & _SEQ_MASK) >= _SEQ_HALF


class _OutQueue:
    """
    Fixed-capacity queue of pending outbound gs_host_frames, shared by
    the RX and echo paths since both leave over the same bulk-IN
    endpoint. Backed by `capacity` preallocated frame-sized buffers,
    reused by index; nothing here is appended, popped or resized after
    construction (R10).

    Entries are served oldest first, a monotonic (wrapping) sequence
    number breaking ties without needing an actual ring of positions. An
    echo that finds every slot occupied evicts the oldest RX-sourced
    entry rather than failing: a dropped RX frame has a recovery path
    (CAN_FLAG_OVERFLOW on the next one delivered); a dropped echo does
    not (R12), and is the outcome this queue exists to prevent. Only when
    every occupied slot already holds an echo, an echo backlog beyond
    what the capacity reserves for it, is a new echo actually lost; that
    last-resort case means an invariant elsewhere in this module has
    already been violated (see GsUsbDataPlane._send_echo), not that this
    branch is a supported outcome.
    """

    def __init__(self, capacity, frame_size):
        self._capacity = capacity
        self._bufs = [bytearray(frame_size) for _ in range(capacity)]
        self._occupied = [False] * capacity
        self._is_echo = [False] * capacity
        self._seq = [0] * capacity
        self._next_seq = 0
        # Set by reserve(); see its docstring for why a plain attribute,
        # not a second return value, is the zero-allocation way to
        # surface this.
        self.evicted = False

    def _free_slot(self):
        for i in range(self._capacity):
            if not self._occupied[i]:
                return i
        return None

    def _oldest_rx_slot(self, exclude):
        best = None
        for i in range(self._capacity):
            if i == exclude:
                continue
            if self._occupied[i] and not self._is_echo[i]:
                if best is None or _seq_before(self._seq[i], self._seq[best]):
                    best = i
        return best

    def _occupy(self, slot, is_echo):
        self._occupied[slot] = True
        self._is_echo[slot] = is_echo
        self._seq[slot] = self._next_seq
        self._next_seq = (self._next_seq + 1) & _SEQ_MASK

    def reserve(self, is_echo, exclude=None):
        """
        Reserve a slot for a new outbound frame. Returns the slot index,
        or None when the queue has no room for it (a plain RX
        reservation drops the frame there; an echo reservation only
        fails that way in the last-resort case described in the class
        docstring).

        Whether satisfying an echo reservation required evicting the
        oldest queued RX frame is left in `self.evicted` rather than a
        second return value: returning a tuple would allocate one on
        every single call, on a path this module promises never
        allocates (R10). This queue is only ever driven synchronously,
        from within one CAN or USB callback at a time, never re-entered,
        so `self.evicted` cannot go stale before the caller that just
        set it reads it back.

        `exclude`, when given, names a slot eviction must never choose:
        the one currently in flight on bulk IN, whose buffer a USB
        controller may already be reading from. Evicting it here would
        overwrite those bytes out from under that transfer instead of
        just discarding a queued frame (F9).
        """
        self.evicted = False
        slot = self._free_slot()
        if slot is not None:
            self._occupy(slot, is_echo)
            return slot
        if not is_echo:
            return None
        slot = self._oldest_rx_slot(exclude)
        if slot is None:
            return None
        self._occupy(slot, is_echo)
        self.evicted = True
        return slot

    def buffer(self, slot):
        return self._bufs[slot]

    def oldest(self):
        """Slot index of the oldest occupied entry, or None. Use
        buffer(slot) to read its contents."""
        best = None
        for i in range(self._capacity):
            if self._occupied[i] and (best is None or _seq_before(self._seq[i], self._seq[best])):
                best = i
        return best

    def release(self, slot):
        self._occupied[slot] = False

    def reset(self):
        """Clear every slot's occupancy, discarding whatever was queued
        (F1, F13): a USB bus reset means the host is restarting its own
        state from scratch and is not waiting for completion of anything
        queued for the connection that just ended. The sequence counter
        is left running; nothing about a reset requires resetting it
        too, and doing so would risk a stale duplicate against whatever
        it was last compared against."""
        for i in range(self._capacity):
            self._occupied[i] = False
            self._is_echo[i] = False


class GsUsbDataPlane:
    """
    Wires one `CanCore` to one `GsUsbUsbDevice`'s bulk endpoints and owns
    the asyncio lifecycle that starts and stops that wiring.

    `can` and `usb` are the CanCore and GsUsbUsbDevice-shaped objects to
    drive; `usb` need only implement `submit_xfer(ep, buffer)`, matching
    machine.USBDevice, and this class's own `xfer_cb`/`open_itf_cb`/
    `reset_cb` are what a caller binds as `usb`'s matching handlers.

    `num_channels`, left unset, is `can.num_channels`: this class is not
    itself a second source of truth for how many channels exist, matching
    how `GsUsbControl` derives the same count from the same `CanCore` (F25).
    An explicit value that disagrees with `can`'s own count raises
    immediately rather than silently emitting frames for a channel index
    the control plane never advertised in DEVICE_CONFIG.icount, which a
    6.17 host kernel NULL-derefs on (F4). `tx_queue_len` sizes the
    per-channel correlation table; it is this board's FDCAN TX queue
    depth, the same bound CanCore.submit() is built against, not
    independently discovered from a can_class here. `echo_on_write`
    selects the active policy (see the module docstring's echo discipline
    section); the default is the one this device ships with.
    """

    def __init__(
        self,
        can,
        usb,
        num_channels=None,
        tx_queue_len=3,
        out_queue_len=OUT_QUEUE_LEN,
        echo_on_write=True,
    ):
        self._can = can
        self._usb = usb
        if num_channels is None:
            num_channels = can.num_channels
        elif num_channels != can.num_channels:
            raise ValueError(
                "num_channels %d does not match can_core's %d" % (num_channels, can.num_channels)
            )
        self._num_channels = num_channels
        self._echo_on_write = echo_on_write

        self._queue = _OutQueue(out_queue_len, protocol.FRAME_SIZE_CLASSIC)
        self._rx_overflow_pending = False

        self._in_flight = False
        self._in_flight_slot = None
        self._out_armed = False

        # True while self._out_fields holds a well-formed frame that
        # CanCore had no room for yet (F10): bulk OUT stays unarmed and
        # this frame is retried from _on_tx_complete instead of being
        # overwritten by the next one.
        self._tx_pending = False

        self._out_buf = bytearray(protocol.FRAME_SIZE_CLASSIC)
        # [echo_id, ident, can_dlc, channel, flags, eff, rtr, err];
        # decomposed rather than a combined can_id (F15, see the module
        # docstring's zero-allocation section).
        self._out_fields = [0, 0, 0, 0, 0, False, False, False]
        self._out_data = bytearray(protocol.CLASSIC_DATA_SIZE)
        self._out_data_views = [
            memoryview(self._out_data)[0:n] for n in range(protocol.CLASSIC_DATA_SIZE + 1)
        ]

        # Correlation table: see the module docstring. Only ever indexed
        # by a slot CanCore itself handed out for `channel`, so it is
        # always in range; no bounds check is needed at the lookup sites.
        self._correlation = [[None] * tx_queue_len for _ in range(num_channels)]

        self.run = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Attach to every channel and arm the initial bulk-OUT read."""
        for channel in range(self._num_channels):
            self._can.attach(channel, on_rx=self._on_rx, on_tx_complete=self._on_tx_complete)
        self._arm_out()
        self.run.set()

    def stop(self):
        """Flush any correlation entry still outstanding on each channel,
        then detach: once detached, that channel's on_tx_complete will
        never fire again to do it, and leaving an entry in the table
        would leak the echo the host is still owed (R12)."""
        self.run.clear()
        for channel in range(self._num_channels):
            self.flush_channel(channel)
            self._can.detach(channel)

    def flush_channel(self, channel):
        """
        Resolve everything this class is still holding for `channel` that
        depends on its `on_tx_complete` firing again: every correlation
        entry still outstanding, each sent its owed echo, and a TX frame
        held under backpressure (F10) for this channel, if any, also
        echoed and bulk OUT re-armed for it. Public so a future
        channel-level stop (gs_usb.control's MODE RESET, which calls
        CanCore.stop() directly today with no hook back into this class)
        can call it without waiting for the whole data plane to shut
        down; see the module docstring's correlation table section for
        why that wiring does not exist yet.
        """
        table = self._correlation[channel]
        for slot in range(len(table)):
            echo_id = table[slot]
            if echo_id is not None:
                table[slot] = None
                self._send_echo(channel, echo_id)
        if self._tx_pending and self._out_fields[3] == channel:
            self._tx_pending = False
            echo_id, ident, can_dlc, _channel, _flags, eff, rtr, _err = self._out_fields
            length = protocol.dlc_to_len(can_dlc)
            self._send_echo(
                channel, echo_id, ident, eff, rtr, can_dlc, self._out_data_views[length]
            )
            self._arm_out()

    def reset(self):
        """
        Recover from a USB bus reset (F1). TinyUSB drops any transfer
        that was still outstanding on either bulk endpoint without ever
        calling xfer_cb for it, so `_in_flight`/`_out_armed`/
        `_tx_pending` would otherwise latch at whatever they were and
        wedge both endpoints for the rest of the session; this clears
        them and discards whatever the output queue was holding.
        Discarding is correct, not just convenient: the host's own
        gs_usb driver rebuilds its state from scratch after a reset
        (re-enumeration runs its probe again), so it is not waiting for
        completion of anything queued for the connection that just
        ended either.

        CanCore is untouched: a USB bus reset does not reset the CAN
        controllers, and a transmission already accepted onto the
        hardware queue still completes and still calls on_tx_complete
        normally, so the correlation table is left alone here.
        """
        self._in_flight = False
        self._in_flight_slot = None
        self._out_armed = False
        self._tx_pending = False
        self._queue.reset()
        self._rx_overflow_pending = False

    def open_itf_cb(self, itf_desc=None):
        """
        Dispatched by GsUsbUsbDevice on Set Configuration (F1). This is
        the reliable point to (re)arm both endpoints: at boot, start()
        runs before the host has issued Set Configuration, so its own
        _arm_out() call can find the endpoint not yet claimable; after a
        bus reset, reset() above has just unarmed everything, and Set
        Configuration is exactly the event that follows a reset once
        re-enumeration completes. `itf_desc` is accepted and ignored:
        this data plane owns exactly one interface, which is not in
        question here.
        """
        self._arm_out()
        self._kick_in()

    async def task(self):
        """
        Fits this object into the asyncio.Event-gated task-list
        convention used elsewhere in this codebase (application/device.py,
        blink.py). Nearly everything this class moves crosses it through
        CanCore's synchronous on_rx/on_tx_complete callbacks or usb's
        synchronous xfer_cb, never through this coroutine; the one thing
        it owns is periodic liveness: retrying _arm_out()/_kick_in()
        after a submit_xfer() that failed for a reason which resolves on
        its own (F1, F19), and retrying a frame held under backpressure.

        That second job is what keeps backpressure live. _tx_pending is
        otherwise cleared only by a transmit completing on the same
        channel, and a channel stopped while a frame is held will never
        report one, so without a retry from here bulk OUT stays unarmed
        for good and the held frame's echo is never sent.
        """
        while True:
            await self.run.wait()
            self._poll()
            await asyncio.sleep(1)

    def _poll(self):
        """One pass of task()'s liveness work, separated so it can be
        driven directly from a test without an event loop."""
        if self._tx_pending:
            self._retry_pending_tx()
        self._arm_out()
        self._kick_in()

    # -- USB dispatch ---------------------------------------------------------

    def xfer_cb(self, ep, result, xferred_bytes):
        """Bound to GsUsbUsbDevice's xfer_handler; dispatches on which
        bulk endpoint completed."""
        if ep == usb_device.EP_BULK_IN:
            self._handle_in_done(result)
        elif ep == usb_device.EP_BULK_OUT:
            self._handle_out_done(result, xferred_bytes)

    def _arm_out(self):
        if self._out_armed or self._tx_pending:
            # _tx_pending means self._out_fields/self._out_data still
            # hold a frame this class has not finished with (F10); the
            # host must not be allowed to overwrite them with the next
            # OUT transfer until that frame is resolved.
            return
        try:
            armed = self._usb.submit_xfer(usb_device.EP_BULK_OUT, self._out_buf)
        except OSError as exc:
            # F19: submit_xfer() can raise rather than return False when
            # the endpoint cannot accept a transfer right now (host not
            # yet configured, most likely at boot, or mid-reset). The
            # next call to _arm_out(), from whichever event fires next
            # (open_itf_cb, or task()'s own retry), tries again; there is
            # no separate retry timer beyond that.
            log.warning("bulk OUT arm failed: %r", exc)
            return
        if armed:
            self._out_armed = True

    def _kick_in(self):
        if self._in_flight:
            return
        slot = self._queue.oldest()
        if slot is None:
            return
        buf = self._queue.buffer(slot)
        try:
            armed = self._usb.submit_xfer(usb_device.EP_BULK_IN, buf)
        except OSError as exc:  # F19; see _arm_out's matching comment
            log.warning("bulk IN submit failed: %r", exc)
            return
        if armed:
            self._in_flight = True
            self._in_flight_slot = slot

    def _handle_in_done(self, result):
        if result != _XFER_SUCCESS:
            log.warning("bulk IN transfer failed (result=%d)", result)
        if self._in_flight_slot is not None:
            self._queue.release(self._in_flight_slot)
        else:
            log.warning("bulk IN completion with no in-flight slot recorded")
        self._in_flight = False
        self._in_flight_slot = None
        self._kick_in()

    def _handle_out_done(self, result, xferred_bytes):
        self._out_armed = False
        try:
            resolved = self._process_out_transfer(result, xferred_bytes)
        except Exception as exc:  # noqa: BLE001 - last-resort boundary, see below
            # A USB xfer_cb exception must never reach back into the C
            # callback trampoline (matching gs_usb.control's own
            # exception-boundary contract). Treated as resolved: holding
            # a frame this class does not understand well enough to
            # retry would wedge bulk OUT forever instead of just
            # dropping this one frame without an echo.
            log.error("bulk OUT processing failed: %r", exc)
            resolved = True
        self._tx_pending = not resolved
        if resolved:
            self._arm_out()

    def _process_out_transfer(self, result, xferred_bytes):
        """
        Returns True once the frame just received on bulk OUT is fully
        resolved, meaning it is safe to re-arm bulk OUT for the next host
        frame: a malformed transfer, an out-of-range channel, a channel
        that is not started, and a frame CanCore accepted are all
        resolved immediately. Returns False when the frame is well-formed
        but CanCore's transmit queue has no room for it right now (F10):
        self._out_fields/self._out_data are left holding it, and bulk OUT
        stays unarmed, so the host cannot overwrite it with another frame
        before a later on_tx_complete's retry (`_retry_pending_tx`)
        succeeds.
        """
        if result != _XFER_SUCCESS or xferred_bytes != protocol.FRAME_SIZE_CLASSIC:
            log.warning("malformed bulk OUT transfer (result=%d, %d bytes)", result, xferred_bytes)
            return True

        protocol.unpack_classic_frame_into(
            self._out_buf, 0, self._out_fields, self._out_data, with_timestamp=False
        )
        return self._try_submit_pending()

    # -- transmit -------------------------------------------------------------

    def _try_submit_pending(self):
        """
        Attempt to submit the frame currently decoded into
        self._out_fields/self._out_data to CanCore. Returns True once the
        frame is resolved one way or another (accepted, or rejected
        outright with its echo already queued); returns False when the
        frame is well-formed, its channel exists and is started, and
        CanCore's submit() declined it.

        A decline is backpressure to retry, not a rejection to echo and
        drop (F10): the host is still entitled to have the frame
        transmitted once the controller can take it. Both causes of a
        decline are treated alike because they are indistinguishable
        here. machine.CAN returns None both when the 3-deep TX queue is
        full and when a frame carrying the same CAN id is already pending
        (F31), and neither the port nor the mock says which.
        """
        echo_id, ident, can_dlc, channel, _flags, eff, rtr, _err = self._out_fields

        if not 0 <= channel < self._num_channels:
            # channel is host-supplied and untrusted: passing it straight
            # to CanCore would raise IndexError from _channel(). Every
            # frame this class itself emits (RX, echo) instead carries a
            # channel number that came from CanCore's own attach() range,
            # which is why R4 (never emit a frame with channel >= icount)
            # holds there by construction and needs no check of its own.
            # The echo still goes out, on channel 0 (always valid),
            # rather than the channel the host asked for: the host is
            # still owed exactly one echo per frame it handed over,
            # invalid channel or not (R12).
            log.warning("TX frame for out-of-range channel %d dropped", channel)
            self._send_echo(0, echo_id)
            return True

        if not self._can.is_started(channel):
            # A frame that was already in flight over USB when MODE
            # RESET stopped this channel: CanCore.submit() would raise
            # against a torn-down controller rather than return None, so
            # this is checked before ever calling it.
            log.warning("TX frame for channel %d dropped: channel not started", channel)
            length = protocol.dlc_to_len(can_dlc)
            self._send_echo(
                channel, echo_id, ident, eff, rtr, can_dlc, self._out_data_views[length]
            )
            return True

        length = protocol.dlc_to_len(can_dlc)
        mc_flags = 0
        if eff:
            mc_flags |= _CAN_MSG_FLAG_EXT_ID
        if rtr:
            mc_flags |= _CAN_MSG_FLAG_RTR

        slot = self._can.submit(channel, ident, self._out_data_views[length], flags=mc_flags)
        if slot is None:
            return False
        if self._echo_on_write:
            self._send_echo(
                channel, echo_id, ident, eff, rtr, can_dlc, self._out_data_views[length]
            )
            return True
        self._reserve_correlation(channel, slot, echo_id)
        return True

    def _reserve_correlation(self, channel, slot, echo_id):
        table = self._correlation[channel]
        stale = table[slot]
        if stale is not None:
            log.error(
                "channel %d slot %d reused with echo %d still outstanding; sending it now",
                channel,
                slot,
                stale,
            )
            self._send_echo(channel, stale)
        table[slot] = echo_id

    def _on_tx_complete(self, channel, slot, success):
        # success is not distinguished here: the host's echo path does
        # not read can_id/can_dlc/payload on an echo frame, and this
        # project's error-frame mechanism (CAN_ERR_FLAG, BERR reporting)
        # is out of scope (R6), so a failed transmission still gets
        # exactly the one echo every transmission is owed.
        if not self._echo_on_write:
            echo_id = self._correlation[channel][slot]
            if echo_id is None:
                log.warning(
                    "channel %d: tx-complete for slot %d with no pending echo", channel, slot
                )
            else:
                self._correlation[channel][slot] = None
                self._send_echo(channel, echo_id)
        if self._tx_pending and self._out_fields[3] == channel:
            self._retry_pending_tx()

    def _retry_pending_tx(self):
        """A transmit CanCore rejected for lack of queue room is retried
        here, the moment any transmission on the same channel completes
        and frees a slot (F10); see _try_submit_pending."""
        if self._try_submit_pending():
            self._tx_pending = False
            self._arm_out()

    def _send_echo(self, channel, echo_id, ident=0, eff=False, rtr=False, can_dlc=0, data=None):
        """
        Queue the one echo a TX frame is owed (R12); see the module
        docstring's echo discipline section for which policy calls this
        and when. ident/eff/rtr/can_dlc/data reproduce the original
        frame's content when the caller still has it to hand (accepted
        under echo-on-write and rejected outright, both synchronous with
        the OUT transfer that carried the frame, so self._out_data has
        not yet been overwritten by a later one); the deferred
        echo-on-completion path and the stale-correlation-entry recovery
        path only ever have echo_id by the time they call this, so they
        get a zeroed echo, unchanged from what this module has always
        sent for that not-yet-production policy (see the module
        docstring).
        """
        if data is None:
            data = self._out_data_views[0]
        slot = self._queue.reserve(is_echo=True, exclude=self._in_flight_slot)
        if slot is None:
            # Last resort (see _OutQueue's docstring): every slot already
            # holds an echo of its own. Reachable only if OUT_QUEUE_LEN is
            # undersized for how many channels are actually in flight at
            # once; logged loudly because losing an echo is exactly what
            # this module exists to prevent.
            log.error("channel %d: echo %d lost, output queue exhausted", channel, echo_id)
            return
        if self._queue.evicted:
            self._rx_overflow_pending = True
        buf = self._queue.buffer(slot)
        protocol.pack_classic_frame_into(
            buf, 0, echo_id, ident, can_dlc, channel, 0, data, eff=eff, rtr=rtr
        )
        self._kick_in()

    # -- receive ----------------------------------------------------------------

    def _on_rx(self, channel, can_id, data, flags, errors):
        slot = self._queue.reserve(is_echo=False)
        if slot is None:
            self._rx_overflow_pending = True
            log.warning("channel %d: RX frame dropped, output queue full", channel)
            return

        overflow = bool(errors) or self._rx_overflow_pending
        self._rx_overflow_pending = False

        eff = bool(flags & _CAN_MSG_FLAG_EXT_ID)
        rtr = bool(flags & _CAN_MSG_FLAG_RTR)
        wire_flags = protocol.CAN_FLAG_OVERFLOW if overflow else 0
        can_dlc = protocol.len_to_dlc(len(data))

        buf = self._queue.buffer(slot)
        protocol.pack_classic_frame_into(
            buf,
            0,
            protocol.ECHO_ID_RX,
            can_id,
            can_dlc,
            channel,
            wire_flags,
            data,
            eff=eff,
            rtr=rtr,
        )
        self._kick_in()
