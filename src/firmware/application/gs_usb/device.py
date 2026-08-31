"""
gs_usb data plane: joins CanCore's per-frame callbacks to the two bulk
endpoints GsUsbUsbDevice exposes, and is the runnable object that owns
that wiring's asyncio lifecycle.

Endpoint discipline
--------------------
machine.USBDevice.submit_xfer() takes a short queue per endpoint, and
`IN_XFER_QUEUE` bulk IN transfers are kept outstanding so the controller
has the next one to start the moment one finishes. Bulk IN carries two
kinds of traffic, received frames and TX echoes, so `_FrameRing` below
backs both with preallocated storage: a CAN interrupt that fires while a
previous frame is still being clocked out over USB queues behind it
instead of being lost. The two kinds get separate rings so that a burst
of receives can never crowd out an echo the host is waiting on.

Bulk OUT only ever has one host frame being unpacked at a time, into a
single reused buffer, because the endpoint is re-armed only once
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
an RX frame: `RECV_ERR_OVERRUN` in CanCore's `errors` argument to
`on_rx`, attached by the hardware to the first frame through after the
controller lost one, and this module's own `_rx_overflow_pending`, set
when the receive ring had no free slot. Both feed the same wire bit;
the host does not distinguish where a drop happened, only that one did.

`errors` also carries `RECV_ERR_FULL`, which is deliberately ignored
here. It means the controller's receive FIFO reached capacity, not that
anything was discarded, and a device driven near its delivery rate
reaches that state routinely. Reporting it would tell the host it had
lost frames it in fact received.

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

import micropython
from micropython import const

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

# machine.CAN's RECV_ERR_OVERRUN, mirrored for the same reason. Only the
# overrun bit means a frame was lost; RECV_ERR_FULL (1 << 0) reports a
# full FIFO, which is a normal state under load and not a drop.
_CAN_RECV_ERR_OVERRUN = 1 << 1

# machine.USBDevice.xfer_cb's XFER_SUCCESS (machine.USBDevice.rst).
# Reproduced rather than imported so this module never touches machine at
# load time; the unix port's machine simulation has no USBDevice at all.
_XFER_SUCCESS = const(0)

# Bound here rather than reached through usb_device on every transfer: the
# completion path runs once per frame and a module attribute lookup is not
# free at that rate.
_EP_BULK_IN = usb_device.EP_BULK_IN
_EP_BULK_OUT = usb_device.EP_BULK_OUT

# Bound for the same reason: the receive path reaches these once per frame.
_pack_classic_frame_into = protocol.pack_classic_frame_into
_pack_rx_record_into = protocol.pack_rx_record_into
_len_to_dlc = protocol.len_to_dlc
_CAN_FLAG_OVERFLOW = protocol.CAN_FLAG_OVERFLOW

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
# Periodic-task intervals: the busy one bounds how long a bulk IN refused
# with EBUSY waits before it is retried; the idle one is a liveness
# heartbeat with nothing queued.
_BUSY_POLL_MS = 1
_IDLE_POLL_MS = 1000

# Outbound capacity is partitioned rather than shared, so that an echo can
# never fail to find room and R12 reduces to arithmetic. The host holds ten
# transmits outstanding per channel (GS_MAX_TX_URBS), so that many echo frames
# is the most it can ever be owed at once.
ECHO_FRAMES_PER_CHANNEL = 10

# Receives get their own ring, sized to ride out a burst arriving faster than
# the bulk IN endpoint drains it. Sustained delivery is about 1680 frames/s at
# 1 Mbit; past that this ring is the only thing between an arriving frame and a
# drop, since the controller's own receive ring is emptied into it as fast as
# frames arrive and so never holds anything back.
#
# Depth buys burst tolerance and costs sustained capacity, because a frame is
# formatted before it is admitted and a deeper ring defers the discard until
# after that work is paid for. Measured at 1 Mbit: 64 frames absorbs an 80
# frame burst and delivers 1349 frame/s under overload, 128 absorbs 130 at
# 1300 frame/s, 256 absorbs 250 at 1210 frame/s. Revisit once formatting is
# cheap, which is what makes depth expensive.
RX_RING_FRAMES = 64

# Bulk IN transfers to keep outstanding. One leaves the endpoint idle from the
# moment a transfer completes until this code has run and submitted the next;
# a second means the controller always has one to start. Ports whose
# submit_xfer() takes only one at a time report EBUSY for the second, which
# costs nothing but the attempt.
IN_XFER_QUEUE = const(2)


class _FrameRing:
    """
    Fixed-size frames over `micropython.RingIO`, all or nothing.

    `RingIO` is a byte ring and its write truncates to whatever space is
    available, returning the short count rather than refusing
    (`py/objringio.c`). For a framed protocol a partial write is not a dropped
    frame, it is permanent desync: every subsequent read is misaligned. So the
    capacity is owned here (RingIO exposes none) and a frame is admitted only
    when a whole one fits.

    Frames leave in the order they arrived, and a reader gets a copy, so a
    frame handed to the USB controller cannot be overwritten by a later one.
    """

    def __init__(self, frames, frame_size):
        self._frame_size = frame_size
        self._capacity = frames * frame_size
        self._ring = micropython.RingIO(self._capacity)

    def has_room(self):
        """True when a whole frame would be admitted.

        Lets a producer discard a frame it cannot deliver before paying to
        format it, which is what keeps an overloaded receive path from
        spending its time on frames it is about to throw away.
        """
        return self._capacity - self._ring.any() >= self._frame_size

    def write(self, frame):
        """Append one whole frame, or nothing. True when it was taken."""
        if self._capacity - self._ring.any() < self._frame_size:
            return False
        self._ring.write(frame)
        return True

    def readinto(self, buf):
        """Fill buf with the oldest frame, or leave it alone. True when read."""
        if self._ring.any() < self._frame_size:
            return False
        self._ring.readinto(buf)
        return True

    def pending(self):
        return self._ring.any() // self._frame_size

    def reset(self):
        while self._ring.any():
            self._ring.read(self._ring.any())


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

        # Echo capacity is per channel because the host's outstanding-transmit
        # window is per channel; receives share one ring across channels.
        self._echo_ring = _FrameRing(
            num_channels * ECHO_FRAMES_PER_CHANNEL, protocol.FRAME_SIZE_CLASSIC
        )
        self._rx_ring = _FrameRing(RX_RING_FRAMES, protocol.FRAME_SIZE_CLASSIC)
        self._echo_scratch = bytearray(protocol.FRAME_SIZE_CLASSIC)
        self._rx_scratch = bytearray(protocol.FRAME_SIZE_CLASSIC)
        self._rx_overflow_pending = False

        # Buffers bulk IN transfers are submitted from, one per transfer that
        # may be outstanding at once. Each is held for the length of its
        # transfer, so a frame is never read over the top of one the controller
        # is still sending.
        #
        # Which buffer to fill next follows from how many transfers have
        # completed plus how many are still in flight, rather than being
        # tracked separately: `_kick_in()` reenters, because a submission can
        # complete before `submit_xfer()` returns, and separate bookkeeping
        # drifts out of step with the transfers when it does.
        self._in_bufs = [bytearray(protocol.FRAME_SIZE_CLASSIC) for _ in range(IN_XFER_QUEUE)]
        self._in_flight = 0
        self._in_done = 0
        # The buffer holding a frame read out of a ring that submit_xfer() did
        # not accept, or None. Held as the buffer itself rather than a flag,
        # because the frame belongs to one specific buffer: a completion
        # arriving before the retry moves what the rotation would pick next,
        # and submitting that instead would send a stale frame and lose this
        # one with nothing to count it.
        self._in_staged_buf = None

        # Counted rather than logged: these fire exactly when the device is
        # already behind, and a log line is milliseconds of blocking UART plus
        # an allocation, on the path R10 governs. `counters()` reports them.
        self._n_echo_lost = 0
        self._n_rx_dropped = 0
        self._n_in_submit_failed = 0
        self._n_in_xfer_failed = 0
        self._n_out_xfer_bad = 0
        self._n_out_arm_failed = 0
        self._n_out_processing_failed = 0
        self._n_tx_bad_channel = 0
        self._n_tx_not_started = 0
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
            self._can.attach(
                channel,
                on_rx=self._on_rx,
                on_tx_complete=self._on_tx_complete,
                can_accept=self._can_accept_rx,
                on_rx_ready=self._on_rx_ready,
            )
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
        self._in_flight = 0
        self._in_done = 0
        self._in_staged_buf = None
        self._out_armed = False
        self._tx_pending = False
        self._echo_ring.reset()
        self._rx_ring.reset()
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
            try:
                micropython.schedule(self._poll, None)
            except RuntimeError:
                # Schedule queue full, which means the device is already
                # saturated with callbacks doing this same work. Skipping
                # is correct: the next pass retries, and forcing it here
                # would run _poll unlocked, which is the hazard the
                # scheduling exists to avoid.
                pass
            # submit_xfer() can refuse a bulk IN with EBUSY while the
            # endpoint is still settling from the previous transfer. The
            # frame stays queued, but nothing else will retry it: no
            # completion is outstanding to call back. Poll quickly while
            # anything is queued so that retry costs a millisecond rather
            # than a second, and fall back to the idle interval once the
            # queue drains.
            if not (
                self._echo_ring.pending()
                or self._rx_ring.pending()
                or self._tx_pending
                or self._rx_sink_pending()
            ):
                await asyncio.sleep_ms(_IDLE_POLL_MS)
            else:
                await asyncio.sleep_ms(_BUSY_POLL_MS)

    def _rx_sink_pending(self):
        """Whether any channel's receive interrupt has frames stored that this
        object has not taken yet. Part of the busy test, so a full sink keeps
        the fast poll interval rather than dropping to the idle one."""
        for channel in range(self._can.num_channels):
            ringio, _rec = self._can.rx_sink(channel)
            if ringio is not None and ringio.any():
                return True
        return False

    def _poll(self, _arg=None):
        """One pass of task()'s liveness work.

        Runs through micropython.schedule rather than directly from the
        coroutine, so that it executes inside the scheduler's locked drain
        (`mp_sched_run_pending` holds MP_SCHED_LOCKED for the whole pass).
        That is the same context the CAN receive callback and the USB
        transfer callback already run in, which makes all three mutually
        atomic.

        Called from ordinary coroutine bytecode instead, every `if` here is
        a preemption point: a CAN callback pending from earlier traffic can
        land in the middle of _try_submit_pending, submit the echo this
        pass is still working on, and leave the retry to queue a second one.
        One accepted transmit, two echoes, and the host's ten-slot
        accounting leaks with nothing reported anywhere (R12).

        Takes the unused argument micropython.schedule passes, and is
        callable directly from a test with no event loop.
        """
        # Take whatever the receive interrupt has stored. The channel's own
        # notification fires on the sink going from empty to non-empty, which
        # stops happening once traffic keeps it permanently non-empty, so this
        # is what guarantees a full sink still drains.
        for channel in range(self._can.num_channels):
            self._on_rx_ready(channel)

        if self._tx_pending:
            self._retry_pending_tx()
        self._arm_out()
        self._kick_in()

    # -- USB dispatch ---------------------------------------------------------

    def xfer_cb(self, ep, result, xferred_bytes):
        """Bound to GsUsbUsbDevice's xfer_handler; dispatches on which
        bulk endpoint completed.

        The bulk IN case is written out here rather than delegated. It runs
        once per delivered frame, and at that rate the call itself is a
        measurable part of the frame's cost.
        """
        if ep == _EP_BULK_IN:
            if result != _XFER_SUCCESS:
                self._n_in_xfer_failed += 1
            self._in_done += 1
            self._in_flight -= 1
            self._kick_in()
        elif ep == _EP_BULK_OUT:
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
        except OSError:
            # F19: submit_xfer() can raise rather than return False when
            # the endpoint cannot accept a transfer right now (host not
            # yet configured, most likely at boot, or mid-reset). The
            # next call to _arm_out(), from whichever event fires next
            # (open_itf_cb, or task()'s own retry), tries again; there is
            # no separate retry timer beyond that.
            self._n_out_arm_failed += 1
            return
        if armed:
            self._out_armed = True

    def _kick_in(self):
        """Send the next outbound frame, echoes ahead of receives.

        A dropped receive is reportable to the host through the overflow flag
        on the next one; a dropped echo cannot be signalled at all, so echoes
        go first whenever both are waiting.

        Submits up to `IN_XFER_QUEUE` transfers, so the controller has the
        next one to start the moment the current one finishes.

        A frame read out of a ring but not accepted by submit_xfer() stays in
        `_in_staged_buf` rather than being lost, since it is no longer in the
        ring to be found again, and the next call submits that same buffer.
        """
        bufs = self._in_bufs
        echo_readinto = self._echo_ring.readinto
        rx_readinto = self._rx_ring.readinto
        submit = self._usb.submit_xfer
        while self._in_flight < IN_XFER_QUEUE:
            buf = self._in_staged_buf
            if buf is None:
                buf = bufs[(self._in_done + self._in_flight) % IN_XFER_QUEUE]
                if not echo_readinto(buf):
                    if not rx_readinto(buf):
                        return
            # Count it out before submitting, and only put that back if the
            # submission does not happen: once it does, the completion may run
            # before submit_xfer() returns and it undoes both of these.
            self._in_flight += 1
            self._in_staged_buf = None
            try:
                armed = submit(_EP_BULK_IN, buf)
            except OSError:  # F19; see _arm_out's matching comment
                self._in_flight -= 1
                self._in_staged_buf = buf
                self._n_in_submit_failed += 1
                return
            if not armed:
                self._in_flight -= 1
                self._in_staged_buf = buf
                return

    def _handle_out_done(self, result, xferred_bytes):
        self._out_armed = False
        try:
            resolved = self._process_out_transfer(result, xferred_bytes)
        except Exception:  # noqa: BLE001 - last-resort boundary, see below
            # A USB xfer_cb exception must never reach back into the C
            # callback trampoline (matching gs_usb.control's own
            # exception-boundary contract). Treated as resolved: holding
            # a frame this class does not understand well enough to
            # retry would wedge bulk OUT forever instead of just
            # dropping this one frame without an echo.
            self._n_out_processing_failed += 1
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
            self._n_out_xfer_bad += 1
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
            self._n_tx_bad_channel += 1
            self._send_echo(0, echo_id)
            return True

        if not self._can.is_started(channel):
            # A frame that was already in flight over USB when MODE
            # RESET stopped this channel: CanCore.submit() would raise
            # against a torn-down controller rather than return None, so
            # this is checked before ever calling it.
            self._n_tx_not_started += 1
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
            # Backpressure, not a rejection: nothing has been written anywhere,
            # so there is nothing to unwind. The frame is retried later.
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
        protocol.pack_classic_frame_into(
            self._echo_scratch, 0, echo_id, ident, can_dlc, channel, 0, data, eff=eff, rtr=rtr
        )
        if not self._echo_ring.write(self._echo_scratch):
            # Unreachable by construction: the ring holds the host's whole
            # outstanding-transmit window per channel, so it cannot be full of
            # echoes the host has not yet been sent. Counted rather than
            # trusted, because R12 is what this class exists to hold.
            self._n_echo_lost += 1
            return
        self._kick_in()

    # -- receive ----------------------------------------------------------------

    def _on_rx_ready(self, channel):
        """Take everything the receive interrupt has stored for this channel.

        The frames are already bytes in a ring, so this is the whole per-frame
        cost of receiving: a copy out, one compiled conversion into the wire
        frame, and a copy into the delivery ring. Nothing is called per frame
        to fetch a frame, and the endpoint is fed once for the whole batch
        rather than once per frame.
        """
        # Looked up per pass, not cached at attach: the sink does not exist
        # until the channel is configured, which happens later, when the host
        # sets bit timing.
        ringio, rec = self._can.rx_sink(channel)
        if ringio is None:
            return
        readinto = ringio.readinto
        size = len(rec)
        ring = self._rx_ring
        scratch = self._rx_scratch
        pack = _pack_rx_record_into
        while readinto(rec) == size:
            if not ring.has_room():
                # Same discipline as the recv() path: refuse before formatting,
                # and let the flag ride out on the next frame that gets through.
                self._rx_overflow_pending = True
                self._n_rx_dropped += 1
                continue
            pack(
                scratch,
                0,
                rec,
                channel,
                _CAN_FLAG_OVERFLOW if self._rx_overflow_pending else 0,
            )
            if ring.write(scratch):
                self._rx_overflow_pending = False
            else:
                self._rx_overflow_pending = True
                self._n_rx_dropped += 1
        self._kick_in()

    def _can_accept_rx(self):
        """Whether a received frame can be delivered, and the record of it if
        not.

        CanCore asks this before formatting anything, so a frame that arrives
        with no room costs a capacity test rather than a frame format. The
        refusal is latched here rather than left to the caller because the
        overflow flag the host is owed (GS_CAN_FLAG_OVERFLOW) rides on the
        next frame that does get through, and nothing else on this path would
        learn that one was lost.
        """
        if self._rx_ring.has_room():
            return True
        self._rx_overflow_pending = True
        self._n_rx_dropped += 1
        return False

    def _on_rx(self, channel, can_id, data, flags, errors):
        # Discarding costs one capacity test, delivering costs a frame format,
        # so a full ring is tested before any work is done. Formatting frames
        # that are then dropped takes the processor away from the delivery path
        # that would empty the ring, which makes an overloaded receive path
        # deliver less than a merely busy one.
        rx_ring = self._rx_ring
        if not rx_ring.has_room():
            self._rx_overflow_pending = True
            self._n_rx_dropped += 1
            return

        # The overflow flag rides on the next frame that gets through, so it is
        # decided before the write and only cleared once one has.
        overflow = bool(errors & _CAN_RECV_ERR_OVERRUN) or self._rx_overflow_pending
        eff = bool(flags & _CAN_MSG_FLAG_EXT_ID)
        rtr = bool(flags & _CAN_MSG_FLAG_RTR)
        wire_flags = _CAN_FLAG_OVERFLOW if overflow else 0
        can_dlc = _len_to_dlc(len(data))

        _pack_classic_frame_into(
            self._rx_scratch,
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
        if not rx_ring.write(self._rx_scratch):
            self._rx_overflow_pending = True
            self._n_rx_dropped += 1
            return
        self._rx_overflow_pending = False
        self._kick_in()

    def counters(self):
        """Everything the data path would otherwise have logged.

        Reported here rather than written out where it happens: these fire when
        the device is already behind, and a log line on this path is milliseconds
        of blocking UART plus an allocation, which makes the problem worse than
        the reporting is worth.
        """
        return {
            "echo_lost": self._n_echo_lost,
            "rx_dropped": self._n_rx_dropped,
            "in_submit_failed": self._n_in_submit_failed,
            "in_xfer_failed": self._n_in_xfer_failed,
            "out_xfer_bad": self._n_out_xfer_bad,
            "out_arm_failed": self._n_out_arm_failed,
            "out_processing_failed": self._n_out_processing_failed,
            "tx_bad_channel": self._n_tx_bad_channel,
            "tx_not_started": self._n_tx_not_started,
        }
