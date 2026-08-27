"""
Tests for gs_usb.device against can_core backed by mock_can, standing in
for machine.CAN on the unix port. FakeUsb below stands in for the two
bulk endpoints of machine.USBDevice, exercised through GsUsbDataPlane's
own xfer_cb the same way GsUsbUsbDevice would call it.
"""

import gc
import unittest

import can_core
import mock_can

from gs_usb import device, protocol, usb_device

_XFER_SUCCESS = 0


class FakeUsb:
    """
    Records what GsUsbDataPlane submits, standing in for
    machine.USBDevice's submit_xfer()/xfer_cb contract: at most one
    outstanding transfer per endpoint, and the buffer handed to
    submit_xfer() is the one the caller later reads from (an IN
    transfer's content) or writes into (an OUT transfer's content)
    before the matching xfer_cb fires.

    `record` gates whether `submits` (a list, growing per call) tracks
    every accepted submission; the zero-allocation test turns it off for
    the same reason mock_can's own tx_log has an enable flag: the list
    itself is a test affordance, not part of what is being measured.
    """

    def __init__(self):
        self.pending = {}
        self.submits = []
        self.submit_count = 0
        self.accept = True
        self.record = True

    def submit_xfer(self, ep, buffer):
        if not self.accept or ep in self.pending:
            return False
        self.pending[ep] = buffer
        self.submit_count += 1
        if self.record:
            self.submits.append((ep, bytes(buffer)))
        return True

    def complete(self, dev, ep, result=_XFER_SUCCESS, xferred_bytes=None):
        buf = self.pending.pop(ep)
        if xferred_bytes is None:
            xferred_bytes = len(buf)
        dev.xfer_cb(ep, result, xferred_bytes)

    def complete_in(self, dev, result=_XFER_SUCCESS):
        self.complete(dev, usb_device.EP_BULK_IN, result=result)

    def feed_out(self, dev, frame_bytes, result=_XFER_SUCCESS):
        """Write frame_bytes into the buffer currently armed on bulk OUT
        (as the host writing its next frame into the transfer machine.CAN
        already handed it) and complete that transfer."""
        buf = self.pending[usb_device.EP_BULK_OUT]
        buf[: len(frame_bytes)] = frame_bytes
        self.complete(dev, usb_device.EP_BULK_OUT, result=result, xferred_bytes=len(frame_bytes))


def make_core(num_channels=1):
    return can_core.CanCore(num_channels=num_channels, can_class=mock_can.MockCAN)


def bring_up_channel(core, channel=0, bitrate=500_000):
    """Configure and start one channel, wiring its interrupt, and return
    the underlying MockCAN instance."""
    core.configure(channel, bitrate)
    core.start(channel)
    return core._channels[channel].can


def make(num_channels=1, **kwargs):
    """CanCore + FakeUsb + GsUsbDataPlane, wired but not started."""
    core = make_core(num_channels=num_channels)
    fake = FakeUsb()
    dev = device.GsUsbDataPlane(core, fake, num_channels=num_channels, **kwargs)
    return core, fake, dev


def in_submits(fake):
    return [buf for ep, buf in fake.submits if ep == usb_device.EP_BULK_IN]


def decode(buf):
    """(echo_id, can_id, can_dlc, channel, flags, data) for one classic frame."""
    echo_id, can_id, can_dlc, channel, flags, data, _ts = protocol.unpack_classic_frame(
        buf, with_timestamp=False
    )
    return echo_id, can_id, can_dlc, channel, flags, data


class TestOutQueue(unittest.TestCase):
    """Unit tests of the shared RX/echo output queue in isolation: the
    eviction and exhaustion decisions this module makes for the "echo
    table exhaustion" scenario live here, independent of USB or CAN."""

    def test_rx_reservation_fails_when_full_without_evicting_anything(self):
        q = device._OutQueue(1, 4)
        q.reserve(is_echo=False)
        slot = q.reserve(is_echo=False)
        self.assertIsNone(slot)
        self.assertFalse(q.evicted)

    def test_echo_evicts_the_oldest_rx_entry_when_full(self):
        q = device._OutQueue(2, 4)
        rx_slot = q.reserve(is_echo=False)
        q.reserve(is_echo=True)  # fills the second (and last) free slot

        evicting_slot = q.reserve(is_echo=True)
        self.assertTrue(q.evicted)
        self.assertEqual(evicting_slot, rx_slot)

    def test_echo_reservation_fails_once_every_slot_already_holds_an_echo(self):
        q = device._OutQueue(2, 4)
        q.reserve(is_echo=True)
        q.reserve(is_echo=True)

        slot = q.reserve(is_echo=True)
        self.assertIsNone(slot)
        self.assertFalse(q.evicted)

    def test_oldest_serves_in_reservation_order(self):
        q = device._OutQueue(3, 4)
        first = q.reserve(is_echo=False)
        q.reserve(is_echo=False)
        self.assertEqual(q.oldest(), first)
        q.release(first)
        second_oldest = q.oldest()
        self.assertNotEqual(second_oldest, first)

    def test_reserve_never_evicts_the_excluded_slot(self):
        # F9: the slot currently in flight on bulk IN must survive an
        # eviction even when it is the oldest queued entry and the queue
        # has nothing else free.
        q = device._OutQueue(2, 4)
        oldest = q.reserve(is_echo=False)
        q.reserve(is_echo=False)  # fills the last free slot

        evicting_slot = q.reserve(is_echo=True, exclude=oldest)
        self.assertTrue(q.evicted)
        self.assertNotEqual(evicting_slot, oldest)


class TestReceivePath(unittest.TestCase):
    def test_single_receive_becomes_one_bulk_in_with_correct_bytes(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        can.inject(0x123, b"\x01\x02\x03\x04")

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        expected = protocol.pack_classic_frame(
            protocol.ECHO_ID_RX, 0x123, 4, 0, 0, b"\x01\x02\x03\x04"
        )
        self.assertEqual(submits[0], expected)

    def test_receive_burst_larger_than_the_rx_fifo_sets_overflow_on_the_wire(self):
        core, fake, dev = make()
        core.configure(0, 500_000)  # channel participates on the bus; irq not wired yet
        dev.start()  # attach() before the flush below fires, so it is observed

        can = core._channels[0].can
        for i in range(4):  # one more than the 3-deep RX FIFO
            can.inject(0x100 + i, bytes([i]))

        core.start(0)  # wires the interrupt and flushes the 3-deep backlog

        delivered = []
        for _ in range(3):
            delivered.append(in_submits(fake)[-1])
            fake.complete_in(dev)

        decoded = [decode(buf) for buf in delivered]
        self.assertEqual([d[1] for d in decoded], [0x100, 0x101, 0x102])  # 0x103 was dropped
        self.assertTrue(any(d[4] & protocol.CAN_FLAG_OVERFLOW for d in decoded))


class TestTransmitPath(unittest.TestCase):
    def test_single_out_frame_becomes_one_can_transmit(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        frame = protocol.pack_classic_frame(3, 0x321, 2, 0, 0, b"\xaa\xbb")
        fake.feed_out(dev, frame)

        self.assertEqual(len(can.tx_log), 1)
        can_id, payload, _flags, _slot = can.tx_log[0]
        self.assertEqual(can_id, 0x321)
        self.assertEqual(payload, b"\xaa\xbb")

    def test_accepted_transmit_produces_exactly_one_echo(self):
        # F3: the echo must reproduce the transmitted frame's content, not
        # just its echo_id; a wrong can_id/dlc/flags/payload here would
        # have the host correlate this echo against the wrong pending TX.
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()

        frame = protocol.pack_classic_frame(5, 0x100, 1, 0, 0, b"\x01")
        fake.feed_out(dev, frame)

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        # The echo is a fresh wire encoding of the same fields, not the
        # original bytes replayed, but for an accepted frame with no
        # overflow pending the two must come out identical byte for byte.
        self.assertEqual(submits[0], frame)

        # draining it and letting the bus settle raises no further echo.
        fake.complete_in(dev)
        self.assertEqual(len(in_submits(fake)), 1)

    def test_transmit_backpressure_holds_the_frame_until_a_tx_slot_frees(self):
        # F10: a frame CanCore has no room for yet is retried once a slot
        # frees, not echoed and dropped outright: the host is still
        # entitled to have it transmitted.
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        for i in range(mock_can.TX_QUEUE_LEN):  # fill the 3-deep hardware queue
            fake.feed_out(dev, protocol.pack_classic_frame(i, 0x100 + i, 0, 0, 0, b""))
            fake.complete_in(dev)  # drain each echo so the output queue stays empty
        self.assertEqual(len(can.tx_log), mock_can.TX_QUEUE_LEN)
        submitted_before = len(in_submits(fake))

        fake.feed_out(dev, protocol.pack_classic_frame(9, 0x200, 0, 0, 0, b""))

        # Held, not rejected: no new echo yet, and bulk OUT stays unarmed
        # so the host cannot overwrite this frame before it is resolved.
        self.assertEqual(len(can.tx_log), mock_can.TX_QUEUE_LEN)
        self.assertEqual(len(in_submits(fake)), submitted_before)
        self.assertFalse(usb_device.EP_BULK_OUT in fake.pending)

        can.complete_tx()  # frees a hardware TX slot

        self.assertEqual(len(can.tx_log), mock_can.TX_QUEUE_LEN + 1)  # retried and accepted
        self.assertEqual(len(in_submits(fake)), submitted_before + 1)  # exactly one new echo
        self.assertEqual(decode(in_submits(fake)[-1])[0], 9)
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)  # re-armed once resolved

    def test_out_of_range_channel_gets_an_echo_on_channel_zero(self):
        # F20: the host is still owed exactly one echo per frame handed
        # over (R12), invalid channel or not; it goes out on channel 0,
        # which always exists, rather than the channel the host asked for.
        core, fake, dev = make()  # num_channels defaults to 1
        bring_up_channel(core)
        dev.start()

        frame = protocol.pack_classic_frame(3, 0x123, 0, 5, 0, b"")  # channel 5 does not exist
        fake.feed_out(dev, frame)

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        echo_id, _can_id, _dlc, channel, _flags, _data = decode(submits[0])
        self.assertEqual(echo_id, 3)
        self.assertEqual(channel, 0)
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)  # re-armed for the next frame

    def test_transmit_for_a_channel_stopped_independently_still_gets_its_echo(self):
        # F12: gs_usb.control's MODE RESET stops a channel through
        # CanCore.stop() directly, with no call back into this class; a
        # frame that was already in flight over USB when that happened
        # must not reach a torn-down controller.
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()
        core.stop(0)

        frame = protocol.pack_classic_frame(2, 0x123, 0, 0, 0, b"")
        fake.feed_out(dev, frame)

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        self.assertEqual(decode(submits[0])[0], 2)

    def test_malformed_out_transfer_is_dropped_and_out_is_rearmed(self):
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()

        fake.complete(dev, usb_device.EP_BULK_OUT, result=1, xferred_bytes=0)

        self.assertEqual(in_submits(fake), [])
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)


class TestEchoEvictionProtectsInFlightSlot(unittest.TestCase):
    def test_eviction_never_touches_the_slot_in_flight_on_bulk_in(self):
        # F9: an echo reservation must never evict the entry currently
        # armed on bulk IN, even when it is the oldest queued one and the
        # queue is otherwise full.
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        can.inject(0x100, b"\x01")  # becomes the in-flight bulk-IN transfer
        self.assertEqual(decode(fake.pending[usb_device.EP_BULK_IN])[1], 0x100)

        for i in range(device.OUT_QUEUE_LEN - 1):  # fill every remaining slot
            can.inject(0x200 + i, b"\x02")

        # The queue is now full; an echo must evict something, but not the
        # slot bulk IN is already reading from.
        frame = protocol.pack_classic_frame(0, 0x321, 1, 0, 0, b"\xaa")
        fake.feed_out(dev, frame)

        self.assertEqual(decode(fake.pending[usb_device.EP_BULK_IN])[1], 0x100)


class TestOutstandingEchoCapacity(unittest.TestCase):
    """The ten-slot wedge: this module's own output queue, not the
    hardware TX queue, is what GS_MAX_TX_URBS bounds it against."""

    def _submit(self, dev, fake, can, echo_id, can_id=None):
        if can_id is None:
            can_id = 0x100 + echo_id  # distinct ids: a repeated pending id is refused
        frame = protocol.pack_classic_frame(echo_id, can_id, 1, 0, 0, bytes([echo_id & 0xFF]))
        fake.feed_out(dev, frame)
        # Frees the 3-deep hardware TX queue immediately: it is the
        # software output queue's 10-deep capacity under test here, not
        # CanCore's, so nothing must be left resting on the latter.
        can.complete_tx()

    def test_ten_outstanding_echoes_all_survive_without_draining(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        for echo_id in range(device.OUT_QUEUE_LEN):
            self._submit(dev, fake, can, echo_id)

        # bulk-IN carries one transfer at a time; the other nine echoes
        # are queued, not lost.
        self.assertEqual(len(in_submits(fake)), 1)

        delivered = []
        for _ in range(device.OUT_QUEUE_LEN):
            delivered.append(in_submits(fake)[-1])
            fake.complete_in(dev)

        self.assertEqual([decode(buf)[0] for buf in delivered], list(range(device.OUT_QUEUE_LEN)))

    def test_eleventh_transmit_is_the_only_casualty(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        for echo_id in range(device.OUT_QUEUE_LEN):
            self._submit(dev, fake, can, echo_id)
        self._submit(dev, fake, can, 10, can_id=0x200)  # the queue is already full of echoes

        delivered = []
        for _ in range(device.OUT_QUEUE_LEN):
            delivered.append(in_submits(fake)[-1])
            fake.complete_in(dev)

        # the ten queued before the overflow are untouched and intact.
        self.assertEqual([decode(buf)[0] for buf in delivered], list(range(device.OUT_QUEUE_LEN)))


class TestEchoOnCompletionPolicy(unittest.TestCase):
    """F21: constructing with echo_on_write=False is the entire policy
    swap this module's design promises."""

    def test_echo_is_deferred_until_the_transmit_actually_completes(self):
        core, fake, dev = make(echo_on_write=False)
        can = bring_up_channel(core)
        dev.start()

        frame = protocol.pack_classic_frame(4, 0x123, 2, 0, 0, b"\xaa\xbb")
        fake.feed_out(dev, frame)
        self.assertEqual(in_submits(fake), [])  # accepted into the queue, not yet on the bus

        can.complete_tx()

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        self.assertEqual(decode(submits[0])[0], 4)

    def test_stop_flushes_a_correlation_entry_whose_completion_never_arrived(self):
        core, fake, dev = make(echo_on_write=False)
        bring_up_channel(core)
        dev.start()

        frame = protocol.pack_classic_frame(7, 0x123, 0, 0, 0, b"")
        fake.feed_out(dev, frame)  # accepted; no completion before stop()

        dev.stop()

        submits = in_submits(fake)
        self.assertEqual(len(submits), 1)
        self.assertEqual(decode(submits[0])[0], 7)


class TestChannelCount(unittest.TestCase):
    def test_num_channels_defaults_to_can_cores(self):
        # F4/F25: this class is not a second source of truth for how many
        # channels exist; left unset, it must take CanCore's own count.
        core = make_core(num_channels=2)
        fake = FakeUsb()
        dev = device.GsUsbDataPlane(core, fake)
        self.assertEqual(dev._num_channels, 2)

    def test_mismatched_explicit_num_channels_raises(self):
        core = make_core(num_channels=2)
        fake = FakeUsb()
        with self.assertRaises(ValueError):
            device.GsUsbDataPlane(core, fake, num_channels=1)


class TestReset(unittest.TestCase):
    def test_reset_clears_latched_state_and_discards_the_queue(self):
        # F1/F13: a USB bus reset drops any transfer TinyUSB had
        # outstanding on either bulk endpoint without ever calling
        # xfer_cb; reset() must clear the flags that would otherwise wedge
        # both endpoints and discard whatever the output queue held.
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        for i in range(mock_can.TX_QUEUE_LEN):  # fill the 3-deep hardware TX queue
            fake.feed_out(dev, protocol.pack_classic_frame(i, 0x100 + i, 0, 0, 0, b""))
            fake.complete_in(dev)  # drain each echo so bulk OUT re-arms for the next feed

        fake.feed_out(dev, protocol.pack_classic_frame(9, 0x200, 0, 0, 0, b""))
        # backpressure (F10): bulk OUT stays unarmed, tx_pending latched.
        self.assertTrue(dev._tx_pending)
        self.assertFalse(usb_device.EP_BULK_OUT in fake.pending)

        can.inject(0x300, b"\x01")  # becomes the in-flight bulk-IN transfer
        can.inject(0x400, b"\x02")  # queued behind it, no free slot yet

        self.assertTrue(dev._in_flight)
        self.assertIsNotNone(dev._in_flight_slot)
        self.assertIsNotNone(dev._queue.oldest())

        dev.reset()

        self.assertFalse(dev._in_flight)
        self.assertIsNone(dev._in_flight_slot)
        self.assertFalse(dev._out_armed)
        self.assertFalse(dev._tx_pending)
        self.assertIsNone(dev._queue.oldest())

        # Re-arming after the reset works from a clean slate, matching
        # what open_itf_cb does once re-enumeration completes.
        dev.open_itf_cb()
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)


class TestBackpressureLivenessAcrossStop(unittest.TestCase):
    def test_stopping_a_channel_holding_a_frame_does_not_wedge_bulk_out(self):
        # A frame held under backpressure is normally released by a
        # transmit completing on the same channel. Stopping the channel
        # destroys every queued transmission, so no completion ever
        # arrives and _tx_pending would latch forever, leaving bulk OUT
        # unarmed and the held frame's echo unsent (one of the host's ten
        # echo slots leaked). task()'s retry is the only thing that
        # recovers it, so drive that retry directly here.
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()

        for i in range(mock_can.TX_QUEUE_LEN):
            fake.feed_out(dev, protocol.pack_classic_frame(i, 0x100 + i, 0, 0, 0, b""))
            fake.complete_in(dev)

        fake.feed_out(dev, protocol.pack_classic_frame(9, 0x200, 0, 0, 0, b""))
        self.assertTrue(dev._tx_pending)
        self.assertFalse(usb_device.EP_BULK_OUT in fake.pending)

        core.stop(0)  # what MODE RESET does, i.e. ip link set can0 down

        # _arm_out() alone cannot recover: it early-returns on _tx_pending.
        dev._arm_out()
        self.assertFalse(usb_device.EP_BULK_OUT in fake.pending)

        # One pass of what task() does on each wake-up.
        dev._poll()

        self.assertFalse(dev._tx_pending)
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)
        # The held frame is owed exactly one echo even though the channel
        # went down under it (R12).
        echoed = [
            protocol.unpack_classic_frame(payload, False)[0]
            for ep, payload in fake.submits
            if ep == usb_device.EP_BULK_IN
        ]
        self.assertTrue(9 in echoed)


class TestSpuriousInCompletion(unittest.TestCase):
    def test_in_done_with_no_in_flight_slot_does_not_raise(self):
        # F7: xfer_cb(EP_BULK_IN, ...) firing with no slot recorded as
        # in-flight (e.g. a stale completion racing a reset) must be
        # logged and ignored, not raise out of the USB callback trampoline.
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()

        dev._handle_in_done(_XFER_SUCCESS)  # does not raise


class TestSubmitXferOSError(unittest.TestCase):
    """F19: submit_xfer() can raise OSError rather than return False when
    an endpoint cannot accept a transfer right now; every call site must
    catch it instead of letting it reach the callback trampoline."""

    def test_arm_out_survives_submit_xfer_raising(self):
        core, fake, dev = make()
        bring_up_channel(core)

        def _raise(ep, buffer):
            raise OSError("EINVAL")

        fake.submit_xfer = _raise
        dev.start()  # does not raise, despite _arm_out() failing internally
        self.assertFalse(usb_device.EP_BULK_OUT in fake.pending)
        self.assertFalse(dev._out_armed)

    def test_kick_in_survives_submit_xfer_raising(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        dev.start()

        def _raise(ep, buffer):
            raise OSError("EBUSY")

        fake.submit_xfer = _raise
        can.inject(0x100, b"\x01")  # does not raise, despite _kick_in() failing
        self.assertFalse(dev._in_flight)
        self.assertIsNone(dev._in_flight_slot)


class TestOutTransferExceptionBoundary(unittest.TestCase):
    def test_exception_in_process_out_transfer_still_rearms_bulk_out(self):
        # F12: an exception raised while resolving a bulk-OUT transfer
        # must not reach back into the C callback trampoline, and must not
        # leave bulk OUT wedged unarmed for the rest of the session.
        core, fake, dev = make()
        bring_up_channel(core)
        dev.start()

        def _raise(*a, **kw):
            raise ValueError("boom")

        dev._can.submit = _raise

        frame = protocol.pack_classic_frame(1, 0x100, 0, 0, 0, b"")
        fake.feed_out(dev, frame)  # does not raise

        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)
        self.assertFalse(dev._tx_pending)


class TestZeroAllocationSteadyState(unittest.TestCase):
    def test_200_frame_rx_and_tx_cycles_leave_no_permanent_growth(self):
        core, fake, dev = make()
        can = bring_up_channel(core)
        can.tx_log_enabled = False  # the log itself is a test affordance, not the hot path
        dev.start()

        out_frame = protocol.pack_classic_frame(0, 0x301, 4, 0, 0, b"\x05\x06\x07\x08")

        def _cycle():
            can.inject(0x300, b"\x01\x02\x03\x04")
            fake.complete_in(dev)
            fake.feed_out(dev, out_frame)
            can.complete_tx()
            fake.complete_in(dev)

        # One warm-up cycle so every fixed-size structure (the output
        # queue, FakeUsb's own pending dict, mock_can's rx fifo list)
        # reaches its steady-state capacity before the measurement
        # window starts.
        _cycle()
        fake.record = False  # fake.submits is a test affordance, not part of what is measured
        gc.collect()
        before = gc.mem_alloc()

        for _ in range(200):
            _cycle()

        gc.collect()
        after = gc.mem_alloc()

        self.assertEqual(can.counters["rx_overruns"], 0)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
