"""
Transport-agnostic CAN core (R14): the one component that owns the FDCAN
controllers, the preallocated frame buffers and the RX/TX lifecycle.
`gs_usb` is a consumer attached here, not the owner, so a later transport
(socketcand, a CANopen gateway) attaches the same way without a second
implementation competing for the same controller.

Consumer contract
------------------
A consumer calls `attach()` once per channel with the callbacks it wants
(`on_rx`, `on_tx_complete`, `on_state_change`) and is serviced by direct,
synchronous calls into those callbacks from the channel's `machine.CAN`
interrupt, mirroring how `gs_usb/usb_device.py` wires `machine.USBDevice`'s
runtime callbacks to caller-supplied handlers. Only one consumer may be
attached to a channel at a time (R14: arbitration is an explicit decision);
a second `attach()` on the same channel raises.

`on_rx` receives a `memoryview` onto this module's own per-channel receive
buffer, reused for every frame (R10: no per-frame allocation). The callback
must finish using it, or copy out of it, before returning: the next received
frame overwrites the same bytes.

`on_tx_complete` receives the slot index `submit()` returned, letting a
consumer correlate a transmit echo to the frame it sent (R12, F21). It fires
once per accepted transmission, when the controller reports on-bus
completion, not on acceptance into the queue.

`on_state_change` receives the new bus state on every transition. Recovery
from bus-off is this module's responsibility regardless of whether a
consumer is attached: the host driver never restarts a bus-off adapter on
its own (F12), so the core restarts the controller itself immediately after
notifying the consumer of the transition.
"""

import logging

log = logging.getLogger("can_core")

# MP_CAN_MAX_LEN with MICROPY_HW_ENABLE_FDCAN (extmod/machine_can_port.h):
# both FDCAN instances on this board are FD-capable even though classic CAN
# is the only mode this firmware drives today.
_MAX_FRAME_LEN = 64


class _Channel:
    """Per-channel state: the underlying CAN object, whether the core has
    wired its interrupt, and the one attached consumer's callbacks."""

    def __init__(self, index):
        self.index = index
        self.can = None
        self.started = False
        self.attached = False
        self.on_rx = None
        self.on_tx_complete = None
        self.on_state_change = None
        # [id, memoryview(data), flags, errors]; can.recv() overwrites every
        # element in place. Allocated once here rather than once per frame.
        self.rx_result = [0, memoryview(bytearray(_MAX_FRAME_LEN)), 0, 0]
        self._irq_obj = None


class CanCore:
    """Owns `num_channels` CAN controllers, addressed by index 0..N-1.

    `can_class` is the machine.CAN-shaped type to instantiate: pass
    `mock_can.MockCAN` under test, or leave it unset on target, where it
    resolves to `machine.CAN` on first use. The unix port's `machine`
    simulation module has no `CAN` attribute (`src/unix/simulation/machine.py`),
    so this module never imports `machine` at load time; only a genuine
    attempt to configure a channel without an explicit `can_class` does, and
    that only happens on target.
    """

    # Frames the controller can hold while this code is busy elsewhere. The
    # H5's hardware RX FIFO is 3 elements, which at 1 Mbit is under 400us of
    # tolerance, so a real bus delivering frames back to back overruns it
    # before an interrupt-driven drain can keep up. machine.CAN's software
    # ring absorbs that; 64 frames is roughly 7ms at the highest classic rate.
    RX_RING_FRAMES = 64

    def __init__(self, num_channels=2, can_class=None):
        self._can_class = can_class
        self._channels = [_Channel(i) for i in range(num_channels)]

    @property
    def num_channels(self):
        """The channel count this core was constructed with. A consumer
        that is not itself the source of truth for how many channels exist
        (gs_usb's control and data planes both take a channel count of
        their own only for backward compatibility) derives it from here
        instead of carrying a second number that could drift out of sync."""
        return len(self._channels)

    def _channel(self, channel_index):
        if not 0 <= channel_index < len(self._channels):
            raise IndexError("channel %d out of range" % channel_index)
        return self._channels[channel_index]

    def resolve_can_class(self):
        """The machine.CAN-shaped type this core is actually using, resolved
        to machine.CAN on first use if the caller never supplied one. Public
        so any other consumer that needs to interpret MODE_*/STATE_*/IRQ_*
        constants (gs_usb.control's mode-flag mapping) resolves the same
        class this core resolved, rather than independently resolving
        machine.CAN a second time and risking the two disagree about which
        class is in play under test, where a mock is passed explicitly."""
        if self._can_class is None:
            import machine

            self._can_class = machine.CAN
        return self._can_class

    # -- consumer registration -------------------------------------------

    def attach(self, channel_index, on_rx=None, on_tx_complete=None, on_state_change=None):
        """Register the one consumer for a channel. Raises RuntimeError if
        the channel already has a consumer attached."""
        ch = self._channel(channel_index)
        if ch.attached:
            raise RuntimeError("channel %d already has a consumer attached" % channel_index)
        ch.on_rx = on_rx
        ch.on_tx_complete = on_tx_complete
        ch.on_state_change = on_state_change
        ch.attached = True

    def detach(self, channel_index):
        """Release the channel's consumer, so a different one may attach."""
        ch = self._channel(channel_index)
        ch.on_rx = None
        ch.on_tx_complete = None
        ch.on_state_change = None
        ch.attached = False

    # -- lifecycle ---------------------------------------------------------

    def configure(
        self, channel_index, bitrate, mode=None, sample_point=75, sjw=1, tseg1=-1, tseg2=-1
    ):
        """Set or change a channel's bit timing. The channel must be
        stopped: the controller cannot be retimed while it is arbitrating on
        the bus. The controller starts participating on the bus as soon as
        this returns, ahead of `start()`, which only wires interrupt-driven
        draining; `start()` flushes whatever arrived in that window, but
        more than the 3-deep RX FIFO's worth of it overflows before there is
        anything to flush."""
        ch = self._channel(channel_index)
        if ch.started:
            raise RuntimeError(
                "channel %d is started; call stop() before reconfiguring" % channel_index
            )
        can_class = self.resolve_can_class()
        if mode is None:
            mode = can_class.MODE_NORMAL
        # rxbuf is only accepted by ports carrying the software receive ring;
        # elsewhere it is an unexpected keyword, so fall back rather than
        # refusing to run at all.
        kwargs = {
            "bitrate": bitrate,
            "mode": mode,
            "sample_point": sample_point,
            "sjw": sjw,
            "tseg1": tseg1,
            "tseg2": tseg2,
        }
        if ch.can is None:
            try:
                ch.can = can_class(channel_index + 1, rxbuf=self.RX_RING_FRAMES, **kwargs)
            except TypeError:
                ch.can = can_class(channel_index + 1, **kwargs)
        else:
            try:
                ch.can.init(rxbuf=self.RX_RING_FRAMES, **kwargs)
            except TypeError:
                ch.can.init(**kwargs)
        # Promiscuous by default (R14): the power-on state accepts nothing
        # until set_filters() is called at least once (F27), so this call is
        # what makes the channel a promiscuous frame source rather than a
        # deaf one.
        ch.can.set_filters(None)

    def set_filters(self, channel_index, filters):
        """Install channel-scoped hardware filters, replacing whatever is
        currently installed. `None` restores the promiscuous default. Filter
        capacity is enforced by the controller itself (28 standard plus 8
        extended per channel, F25); a filter list past that limit raises
        ValueError from the underlying CAN object, uninterpreted here."""
        ch = self._channel(channel_index)
        ch.can.set_filters(filters)

    def start(self, channel_index):
        """Wire the channel's interrupt so received frames, transmit
        completions and bus-state transitions are drained and delivered to
        the attached consumer, if any."""
        ch = self._channel(channel_index)
        if ch.can is None:
            raise RuntimeError("channel %d is not configured" % channel_index)
        if ch.started:
            return
        can_class = type(ch.can)
        trigger = can_class.IRQ_RX | can_class.IRQ_TX | can_class.IRQ_STATE
        ch._irq_obj = ch.can.irq(self._make_handler(ch), trigger=trigger, hard=False)
        ch.started = True
        # Flush whatever arrived in the configure()-to-start() window before
        # this point could raise the interrupt that would otherwise drain it.
        self._service(ch)

    def stop(self, channel_index):
        """Tear the channel down: disable its interrupt and deinitialise the
        controller. `configure()` reinitialises the same underlying CAN
        object rather than constructing a new one, matching how a real
        machine.CAN index is a singleton for the controller's lifetime.

        `started` and `_irq_obj` are cleared before `deinit()` runs, not
        after: a soft callback for this channel that was already scheduled
        before this call started can still run once `deinit()` returns
        control to the event loop, and `_service()` checks both of these
        to recognise that race and do nothing rather than servicing a
        channel whose interrupt object no longer exists."""
        ch = self._channel(channel_index)
        if ch.can is None or not ch.started:
            return
        ch.started = False
        ch._irq_obj = None
        ch.can.deinit()  # clears the controller's own irq handler and trigger

    def is_started(self, channel_index):
        """Whether the channel is currently started: has a configured
        controller with its interrupt wired. A consumer holding a frame
        that arrived before this call returns False checks this before
        calling submit(), rather than finding out from an exception raised
        by a controller that has since been torn down by MODE RESET."""
        return self._channel(channel_index).started

    def restart(self, channel_index):
        """Recover a channel from bus-off (or force a restart at any other
        time); the channel must be started. Also called internally the
        moment a bus-off transition is observed, since the host driver never
        issues this on the device's behalf (F12)."""
        ch = self._channel(channel_index)
        if not ch.started:
            raise RuntimeError("channel %d is not started" % channel_index)
        ch.can.restart()

    # -- transmit ------------------------------------------------------

    def submit(self, channel_index, can_id, data, flags=0):
        """Submit one frame for transmission. Returns the TX slot index on
        acceptance into the queue, or None if the 3-deep queue is full
        (F27); the slot index is what `on_tx_complete` reports back for
        echo correlation (R12)."""
        ch = self._channel(channel_index)
        return ch.can.send(can_id, data, flags)

    # -- interrupt service --------------------------------------------------

    def _make_handler(self, ch):
        def _handler(_can):
            self._service(ch)

        return _handler

    def _service(self, ch):
        """Runs once per channel interrupt (soft, off the hard-IRQ path).
        Drains every event a single interrupt can represent: transmit
        completions are drained in a loop, because flags() reports and
        clears at most one per call (F28); received frames are then
        drained until the FIFO is empty, since recv() returning None is the
        only way to know it is. Bus-state is handled last and only once,
        but every flags() read along the way, not just the first, is
        checked for it: flags() clears IRQ_STATE the moment it is read, so
        a state transition that only becomes visible on a later read inside
        the TX-drain loop would otherwise never be acted on at all.

        A soft callback for this channel can already be scheduled before
        `stop()` runs and only execute afterwards, once `stop()` has
        cleared `started`/`_irq_obj` and torn the controller down; this
        returns immediately in that case rather than calling `flags()` on
        an interrupt object that no longer exists."""
        if not ch.started or ch._irq_obj is None:
            return
        can = ch.can
        can_class = type(can)
        irq = ch._irq_obj
        flags = irq.flags()
        state_pending = bool(flags & can_class.IRQ_STATE)

        while flags & can_class.IRQ_TX:
            self._handle_tx_complete(ch, can_class, flags)
            flags = irq.flags()
            state_pending = state_pending or bool(flags & can_class.IRQ_STATE)

        self._drain_rx(ch)

        if state_pending:
            self._handle_state(ch, can_class)

    def _handle_state(self, ch, can_class):
        state = ch.can.state()
        if ch.on_state_change is not None:
            ch.on_state_change(ch.index, state)
        if state == can_class.STATE_BUS_OFF:
            log.warning("channel %d entered bus-off, restarting", ch.index)
            ch.can.restart()

    def _handle_tx_complete(self, ch, can_class, flags):
        slot = (flags >> can_class.IRQ_TX_IDX_SHIFT) & can_class.IRQ_TX_IDX_MASK
        success = not (flags & can_class.IRQ_TX_FAILED)
        if ch.on_tx_complete is not None:
            ch.on_tx_complete(ch.index, slot, success)

    def _drain_rx(self, ch):
        can = ch.can
        result = ch.rx_result
        while can.recv(result) is not None:
            if ch.on_rx is not None:
                ch.on_rx(ch.index, result[0], result[1], result[2], result[3])
