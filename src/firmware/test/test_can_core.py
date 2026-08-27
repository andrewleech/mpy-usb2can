"""
Tests for can_core against mock_can, standing in for machine.CAN on the
unix port.
"""

import gc
import unittest

import can_core
import mock_can


def make_core(num_channels=1):
    return can_core.CanCore(num_channels=num_channels, can_class=mock_can.MockCAN)


class TestAttach(unittest.TestCase):
    def test_second_attach_raises(self):
        core = make_core()
        core.attach(0, on_rx=lambda *a: None)
        with self.assertRaises(RuntimeError):
            core.attach(0, on_rx=lambda *a: None)

    def test_detach_allows_a_new_consumer(self):
        core = make_core()
        core.attach(0, on_rx=lambda *a: None)
        core.detach(0)
        core.attach(0, on_rx=lambda *a: None)  # does not raise

    def test_channel_index_out_of_range_raises(self):
        core = make_core(num_channels=2)
        with self.assertRaises(IndexError):
            core.attach(2, on_rx=lambda *a: None)


class TestLifecycle(unittest.TestCase):
    def test_configure_while_started_raises(self):
        core = make_core()
        core.configure(0, 500_000)
        core.start(0)
        with self.assertRaises(RuntimeError):
            core.configure(0, 250_000)

    def test_start_without_configure_raises(self):
        core = make_core()
        with self.assertRaises(RuntimeError):
            core.start(0)

    def test_restart_without_start_raises(self):
        core = make_core()
        core.configure(0, 500_000)
        with self.assertRaises(RuntimeError):
            core.restart(0)

    def test_stop_then_reconfigure_reuses_the_same_can_object(self):
        core = make_core()
        core.configure(0, 500_000)
        can = core._channels[0].can
        core.start(0)
        core.stop(0)
        core.configure(0, 250_000)
        self.assertIs(core._channels[0].can, can)

    def test_stop_is_idempotent(self):
        core = make_core()
        core.configure(0, 500_000)
        core.start(0)
        core.stop(0)
        core.stop(0)  # does not raise

    def test_stale_scheduled_callback_after_stop_is_a_noop(self):
        # F18: a soft callback for this channel can already be queued
        # before stop() runs and only execute afterwards, once stop() has
        # cleared started/_irq_obj and torn the controller down. _service
        # must recognise that race and do nothing rather than calling
        # flags() on an interrupt object that no longer exists.
        core = make_core()
        core.configure(0, 500_000)
        core.start(0)
        ch = core._channels[0]
        stale_handler = ch._irq_obj.handler  # the callback as it was queued

        core.stop(0)

        stale_handler(ch.can)  # does not raise


class TestPromiscuousDefault(unittest.TestCase):
    def test_configured_channel_accepts_any_id_unfiltered(self):
        core = make_core()
        received = []
        core.attach(0, on_rx=lambda ch, can_id, data, flags, errors: received.append(can_id))
        core.configure(0, 500_000)
        core.start(0)

        can = core._channels[0].can
        can.inject(0x123, b"\x01\x02")
        can.inject(0x456, b"\x03\x04")

        self.assertEqual(received, [0x123, 0x456])


class TestTransmit(unittest.TestCase):
    def test_submit_returns_the_tx_slot_index(self):
        core = make_core()
        core.configure(0, 500_000)
        core.start(0)

        slot = core.submit(0, 0x100, b"\x01\x02\x03\x04")
        self.assertEqual(slot, 0)

        can = core._channels[0].can
        self.assertEqual(can.tx_log, [(0x100, b"\x01\x02\x03\x04", 0, 0)])

    def test_submit_returns_none_when_the_queue_is_full(self):
        core = make_core()
        core.configure(0, 500_000)
        core.start(0)

        for i in range(mock_can.TX_QUEUE_LEN):
            slot = core.submit(0, 0x100 + i, b"\x01")
            self.assertIsNotNone(slot)

        self.assertIsNone(core.submit(0, 0x200, b"\x01"))

    def test_tx_complete_delivers_the_slot_index_for_echo_correlation(self):
        core = make_core()
        completions = []
        core.attach(
            0, on_tx_complete=lambda ch, slot, success: completions.append((ch, slot, success))
        )
        core.configure(0, 500_000)
        core.start(0)

        slot = core.submit(0, 0x123, b"\x01\x02")
        can = core._channels[0].can
        can.complete_tx(slot)

        self.assertEqual(completions, [(0, slot, True)])

    def test_failed_tx_complete_reports_success_false(self):
        core = make_core()
        completions = []
        core.attach(0, on_tx_complete=lambda ch, slot, success: completions.append(success))
        core.configure(0, 500_000)
        core.start(0)

        slot = core.submit(0, 0x123, b"\x01\x02")
        core._channels[0].can.complete_tx(slot, success=False)

        self.assertEqual(completions, [False])

    def test_three_completions_drain_in_one_service_call(self):
        # F28: flags() reports and clears at most one completed transfer per
        # call, so a single interrupt covering three completions must loop.
        core = make_core()
        completions = []
        core.attach(0, on_tx_complete=lambda ch, slot, success: completions.append(slot))
        core.configure(0, 500_000)
        core.start(0)

        can = core._channels[0].can
        slots = [
            core.submit(0, 0x100 + i, b"\x01", flags=mock_can.FLAG_UNORDERED) for i in range(3)
        ]
        for slot in slots:
            can._tx_slots[slot] = can._tx_slots[slot][:3] + (
                True,
                True,
            )  # mark completed, don't fire yet

        core._service(core._channels[0])

        self.assertEqual(sorted(completions), sorted(slots))


class TestStateDuringDrain(unittest.TestCase):
    def test_state_transition_seen_only_mid_drain_is_still_handled(self):
        # F11: flags() clears IRQ_STATE the instant it is read, so a state
        # transition that only becomes visible on a read taken after the
        # TX-drain loop has already started must still be handled, not
        # silently dropped because only the very first read was ever
        # checked for it.
        core = make_core()
        states = []
        completions = []
        core.attach(
            0,
            on_tx_complete=lambda ch, slot, success: completions.append(slot),
            on_state_change=lambda ch, state: states.append(state),
        )
        core.configure(0, 500_000)
        core.start(0)

        can = core._channels[0].can
        slots = [
            core.submit(0, 0x100 + i, b"\x01", flags=mock_can.FLAG_UNORDERED) for i in range(2)
        ]
        for slot in slots:
            can._tx_slots[slot] = can._tx_slots[slot][:3] + (
                True,
                True,
            )  # mark completed, don't fire yet

        real_flags = can._irq_flags
        calls = [0]

        def _flags_then_latch_state():
            calls[0] += 1
            result = real_flags()
            if calls[0] == 1:
                # A bus-off transition latches only after the interrupt has
                # already begun draining transmit completions, exactly as a
                # real controller's status register can assert a new
                # condition mid-service.
                can._state = mock_can.STATE_BUS_OFF
                can._irq_state_pending = True
            return result

        can._irq_flags = _flags_then_latch_state

        core._service(core._channels[0])

        self.assertEqual(sorted(completions), sorted(slots))
        self.assertEqual(states, [mock_can.STATE_BUS_OFF])
        self.assertEqual(can.restart_count, 1)


class TestReceive(unittest.TestCase):
    def test_received_frame_is_delivered_with_matching_fields(self):
        core = make_core()
        events = []
        core.attach(
            0,
            on_rx=lambda ch, can_id, data, flags, errors: events.append(
                (ch, can_id, bytes(data), flags, errors)
            ),
        )
        core.configure(0, 500_000)
        core.start(0)

        core._channels[0].can.inject(0x321, b"\xaa\xbb\xcc", flags=mock_can.FLAG_EXT_ID)

        self.assertEqual(events, [(0, 0x321, b"\xaa\xbb\xcc", mock_can.FLAG_EXT_ID, 0)])

    def test_rx_fifo_overflow_is_counted_and_backlog_flushes_on_start(self):
        core = make_core()
        events = []
        core.attach(
            0, on_rx=lambda ch, can_id, data, flags, errors: events.append((can_id, errors))
        )
        core.configure(0, 500_000)  # participates on the bus; not started yet

        can = core._channels[0].can
        for i in range(4):  # one more than the 3-deep RX FIFO (F8)
            can.inject(0x100 + i, b"\x01")
        self.assertEqual(can.counters["rx_overruns"], 1)

        core.start(0)  # flushes the backlog that accumulated before this

        self.assertEqual(len(events), 3)
        self.assertEqual([can_id for can_id, _errors in events], [0x100, 0x101, 0x102])
        self.assertTrue(any(errors & mock_can.RECV_ERR_OVERRUN for _can_id, errors in events))


class TestBusOff(unittest.TestCase):
    def test_bus_off_notifies_consumer_and_self_recovers(self):
        core = make_core()
        states = []
        core.attach(0, on_state_change=lambda ch, state: states.append(state))
        core.configure(0, 500_000)
        core.start(0)

        can = core._channels[0].can
        can.set_state(mock_can.STATE_BUS_OFF)

        self.assertEqual(states, [mock_can.STATE_BUS_OFF])
        self.assertEqual(can.restart_count, 1)
        self.assertEqual(can.state(), mock_can.STATE_ACTIVE)


class TestFilterCapacity(unittest.TestCase):
    def test_too_many_standard_filters_raises(self):
        core = make_core()
        core.configure(0, 500_000)

        too_many = [(i, 0x7FF, 0) for i in range(mock_can._STD_FILTERS_MAX + 1)]
        with self.assertRaises(ValueError):
            core.set_filters(0, too_many)

    def test_filters_within_capacity_are_accepted(self):
        core = make_core()
        core.configure(0, 500_000)
        exactly_max = [(i, 0x7FF, 0) for i in range(mock_can._STD_FILTERS_MAX)]
        core.set_filters(0, exactly_max)  # does not raise


class TestZeroAllocationSteadyState(unittest.TestCase):
    def test_rx_and_tx_cycles_leave_no_permanent_growth(self):
        # mock_can's own send()/recv() must allocate transiently: a pure
        # Python memoryview cannot have its length mutated in place the way
        # the C driver's does, and send() copies its payload into a fresh
        # bytes object. This asserts that none of that survives a
        # gc.collect(): can_core's own hot path adds no permanent growth
        # across repeated cycles, whatever garbage the mock churns through
        # on the way.
        core = make_core()
        rx_count = [0]
        tx_count = [0]

        def _bump(counter):
            counter[0] += 1

        core.attach(0, on_rx=lambda *a: _bump(rx_count), on_tx_complete=lambda *a: _bump(tx_count))
        core.configure(0, 500_000)
        core.start(0)

        can = core._channels[0].can
        can.tx_log_enabled = (
            False  # the log itself is a test affordance, not part of the hot path under test
        )

        def _cycle():
            can.inject(0x100, b"\x01\x02\x03\x04")
            slot = core.submit(0, 0x200, b"\x05\x06\x07\x08")
            can.complete_tx(slot)

        # One warm-up cycle so the mock's own rx-fifo lists reach their
        # steady-state capacity before the measurement window starts;
        # list growth inside the mock is the mock's business, not
        # can_core's, and is not what this test is checking.
        _cycle()
        gc.collect()
        before = gc.mem_alloc()

        for _ in range(50):
            _cycle()

        gc.collect()
        after = gc.mem_alloc()

        self.assertEqual(rx_count[0], 51)
        self.assertEqual(tx_count[0], 51)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
