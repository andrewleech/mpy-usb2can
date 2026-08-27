"""
Tests for gs_usb.control against can_core backed by mock_can, standing in
for machine.CAN on the unix port.
"""

import struct
import unittest

import can_core
import mock_can

from gs_usb import control, protocol

_STAGE_SETUP = 1
_STAGE_DATA = 2
_STAGE_ACK = 3

_BM_OUT = 0x41
_BM_IN = 0xC1

_SETUP_FMT = "<BBHHH"


def make(num_channels=1, sw_version=2, hw_version=1, interface_number=0):
    core = can_core.CanCore(num_channels=num_channels, can_class=mock_can.MockCAN)
    ctrl = control.GsUsbControl(
        core,
        num_channels=num_channels,
        sw_version=sw_version,
        hw_version=hw_version,
        interface_number=interface_number,
    )
    return core, ctrl


def setup_packet(bm_request_type, b_request, w_value, w_index, w_length):
    return struct.pack(_SETUP_FMT, bm_request_type, b_request, w_value, w_index, w_length)


def do_in(ctrl, b_request, w_value, w_index, w_length):
    """Run a full IN control transfer (SETUP, DATA, ACK) and return what
    SETUP produced: False on stall, otherwise the response bytes."""
    req = setup_packet(_BM_IN, b_request, w_value, w_index, w_length)
    result = ctrl.control_xfer_cb(_STAGE_SETUP, req)
    if result is False:
        return False
    ctrl.control_xfer_cb(_STAGE_DATA, req)
    ack = ctrl.control_xfer_cb(_STAGE_ACK, req)
    assert ack is True
    return bytes(result)


def do_out(ctrl, b_request, w_value, w_index, payload):
    """Run a full OUT control transfer and return the ACK-stage result
    (True, or False on stall)."""
    req = setup_packet(_BM_OUT, b_request, w_value, w_index, len(payload))
    buf = ctrl.control_xfer_cb(_STAGE_SETUP, req)
    if buf is False:
        return False
    buf[:] = payload
    ctrl.control_xfer_cb(_STAGE_DATA, req)
    return ctrl.control_xfer_cb(_STAGE_ACK, req)


def setup_only(ctrl, bm_request_type, b_request, w_value, w_index, w_length):
    """Run just the SETUP stage and return its result, for tests that only
    care whether a request is accepted or stalled at that point."""
    req = setup_packet(bm_request_type, b_request, w_value, w_index, w_length)
    return ctrl.control_xfer_cb(_STAGE_SETUP, req)


# 16 time quanta (1 sync + 13 tseg1 + 2 tseg2) at brp=1 against this board's
# 8 MHz HSE is exactly 500 kbit; prop_seg/phase_seg1 (6/7) split tseg1's 13
# quanta arbitrarily, the way a real host's request would, but the total
# and the resulting bitrate are chosen, not copied from any table (R7).
_BT_500K = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=1, brp=1)
_BT_500K_BITRATE = 500_000
_BT_500K_TSEG1 = 13
_BT_500K_TSEG2 = 2
_BT_500K_SJW = 1

# Same tseg1/tseg2 split, brp=4: 8_000_000 // (4 * 16) = 125_000.
_BT_125K = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=1, brp=4)


def bring_up(ctrl, channel=0, bittiming=_BT_500K, flags=0):
    """BITTIMING then MODE START on one channel; asserts each step
    succeeds, for tests whose focus is what happens next."""
    assert do_out(ctrl, protocol.BREQ_BITTIMING, channel, 0, bittiming) is True
    payload = protocol.pack_device_mode(protocol.CAN_MODE_START, flags)
    assert do_out(ctrl, protocol.BREQ_MODE, channel, 0, payload) is True


class TestHostFormat(unittest.TestCase):
    def test_accepted(self):
        _core, ctrl = make()
        payload = protocol.pack_host_config(protocol.HOST_FORMAT_MAGIC)
        self.assertIs(
            do_out(ctrl, protocol.BREQ_HOST_FORMAT, protocol.DEVICE_SCOPE_WVALUE, 0, payload),
            True,
        )

    def test_wrong_wvalue_stalls(self):
        _core, ctrl = make()
        payload = protocol.pack_host_config()
        self.assertIs(do_out(ctrl, protocol.BREQ_HOST_FORMAT, 0, 0, payload), False)

    def test_wrong_windex_stalls(self):
        _core, ctrl = make(interface_number=0)
        payload = protocol.pack_host_config()
        self.assertIs(
            do_out(ctrl, protocol.BREQ_HOST_FORMAT, protocol.DEVICE_SCOPE_WVALUE, 1, payload),
            False,
        )

    def test_wrong_wlength_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(
                ctrl, _BM_OUT, protocol.BREQ_HOST_FORMAT, protocol.DEVICE_SCOPE_WVALUE, 0, 3
            ),
            False,
        )

    def test_wrong_direction_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(
                ctrl,
                _BM_IN,
                protocol.BREQ_HOST_FORMAT,
                protocol.DEVICE_SCOPE_WVALUE,
                0,
                protocol.HOST_CONFIG_SIZE,
            ),
            False,
        )


class TestDeviceConfig(unittest.TestCase):
    def test_response_matches_reference_capture_bytes(self):
        # REFERENCE.md's DEVICE_CONFIG response for icount=0, sw_version=2,
        # hw_version=1: reserved1..3 zero, icount 0, sw 2, hw 1, all __le32
        # after the three reserved bytes.
        _core, ctrl = make(num_channels=1, sw_version=2, hw_version=1)
        response = do_in(
            ctrl,
            protocol.BREQ_DEVICE_CONFIG,
            protocol.DEVICE_SCOPE_WVALUE,
            0,
            protocol.DEVICE_CONFIG_SIZE,
        )
        self.assertEqual(response, bytes.fromhex("000000000200000001000000"))

    def test_icount_is_channel_count_minus_one(self):
        _core, ctrl = make(num_channels=2)
        response = do_in(
            ctrl,
            protocol.BREQ_DEVICE_CONFIG,
            protocol.DEVICE_SCOPE_WVALUE,
            0,
            protocol.DEVICE_CONFIG_SIZE,
        )
        _r1, _r2, _r3, icount, _sw, _hw = protocol.unpack_device_config(response)
        self.assertEqual(icount, 1)

    def test_short_wlength_truncates(self):
        _core, ctrl = make()
        response = do_in(ctrl, protocol.BREQ_DEVICE_CONFIG, protocol.DEVICE_SCOPE_WVALUE, 0, 5)
        full = protocol.pack_device_config(0, 2, 1)
        self.assertEqual(response, full[:5])

    def test_long_wlength_zero_pads(self):
        _core, ctrl = make()
        response = do_in(ctrl, protocol.BREQ_DEVICE_CONFIG, protocol.DEVICE_SCOPE_WVALUE, 0, 20)
        full = protocol.pack_device_config(0, 2, 1)
        self.assertEqual(len(response), 20)
        self.assertEqual(response[: len(full)], full)
        self.assertEqual(response[len(full) :], bytes(20 - len(full)))

    def test_excessive_wlength_stalls_rather_than_padding_unbounded(self):
        # wLength is a host-controlled u16; honouring one far beyond any
        # struct this module packs would let a single control read force an
        # allocation sized by the host (up to 64 KB) rather than by the
        # response actually being read.
        _core, ctrl = make()
        self.assertIs(
            setup_only(
                ctrl,
                _BM_IN,
                protocol.BREQ_DEVICE_CONFIG,
                protocol.DEVICE_SCOPE_WVALUE,
                0,
                0xFFFF,
            ),
            False,
        )

    def test_wvalue_is_not_a_channel_index(self):
        # DEVICE_CONFIG is device-scoped: wValue is always the literal 1,
        # even on a multi-channel device where 1 would also be a valid
        # channel index for a per-channel request.
        _core, ctrl = make(num_channels=2)
        self.assertIsNot(
            do_in(ctrl, protocol.BREQ_DEVICE_CONFIG, 1, 0, protocol.DEVICE_CONFIG_SIZE), False
        )
        self.assertIs(
            do_in(ctrl, protocol.BREQ_DEVICE_CONFIG, 0, 0, protocol.DEVICE_CONFIG_SIZE), False
        )


class TestBtConst(unittest.TestCase):
    def test_response_uses_this_boards_own_limits(self):
        _core, ctrl = make()
        response = do_in(ctrl, protocol.BREQ_BT_CONST, 0, 0, protocol.BT_CONST_SIZE)
        (
            feature,
            fclk_can,
            tseg1_min,
            tseg1_max,
            tseg2_min,
            tseg2_max,
            sjw_max,
            brp_min,
            brp_max,
            brp_inc,
        ) = protocol.unpack_bt_const(response)
        self.assertEqual(feature, control.FEATURE)
        self.assertEqual(fclk_can, 8_000_000)
        self.assertNotEqual(fclk_can, 64_000_000)  # the reference device's clock; not ours (R7)
        self.assertEqual((tseg1_min, tseg1_max), (1, 16))
        self.assertEqual((tseg2_min, tseg2_max), (1, 8))
        self.assertEqual(sjw_max, 4)
        self.assertEqual((brp_min, brp_max, brp_inc), (1, 512, 1))

    def test_feature_word_advertises_only_the_decided_bits(self):
        # LISTEN_ONLY and LOOP_BACK map onto real machine.CAN modes and are
        # advertised; everything R6 forbids, and HW_TIMESTAMP/TERMINATION
        # (see control.py's module docstring), must not be set.
        self.assertEqual(
            control.FEATURE, protocol.FEATURE_LISTEN_ONLY | protocol.FEATURE_LOOP_BACK
        )
        excluded = (
            protocol.FEATURE_HW_TIMESTAMP
            | protocol.FEATURE_IDENTIFY
            | protocol.FEATURE_USER_ID
            | protocol.FEATURE_PAD_PKTS_TO_MAX_PKT_SIZE
            | protocol.FEATURE_FD
            | protocol.FEATURE_TERMINATION
            | protocol.FEATURE_BERR_REPORTING
            | protocol.FEATURE_GET_STATE
        )
        self.assertEqual(control.FEATURE & excluded, 0)

    def test_channel_out_of_range_stalls_and_never_reaches_can_core(self):
        core, ctrl = make(num_channels=1)
        self.assertIs(do_in(ctrl, protocol.BREQ_BT_CONST, 1, 0, protocol.BT_CONST_SIZE), False)
        self.assertIsNone(core._channels[0].can)

    def test_wrong_direction_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(ctrl, _BM_OUT, protocol.BREQ_BT_CONST, 0, 0, protocol.BT_CONST_SIZE),
            False,
        )

    def test_excessive_wlength_stalls_rather_than_padding_unbounded(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(ctrl, _BM_IN, protocol.BREQ_BT_CONST, 0, 0, 0xFFFF),
            False,
        )


class TestBitTiming(unittest.TestCase):
    def test_accepted_does_not_touch_can_core_yet(self):
        # F12: BITTIMING is a pure state update while stopped; CanCore is
        # only driven once MODE START also supplies the mode flags.
        core, ctrl = make()
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_500K), True)
        self.assertIsNone(core._channels[0].can)
        self.assertEqual(
            ctrl._bittiming[0],
            (_BT_500K_TSEG1, _BT_500K_TSEG2, _BT_500K_SJW, _BT_500K_BITRATE),
        )

    def test_wrong_wlength_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(ctrl, _BM_OUT, protocol.BREQ_BITTIMING, 0, 0, 8),
            False,
        )

    def test_channel_out_of_range_stalls_and_never_reaches_can_core(self):
        core, ctrl = make(num_channels=1)
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 1, 0, _BT_500K), False)
        self.assertIsNone(core._channels[0].can)

    def test_out_of_range_field_stalls_without_reaching_can_core(self):
        core, ctrl = make()
        bad = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=99, brp=1)
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, bad), False)
        self.assertIsNone(core._channels[0].can)
        self.assertIsNone(ctrl._bittiming[0])

    def test_wrong_direction_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(ctrl, _BM_IN, protocol.BREQ_BITTIMING, 0, 0, protocol.BITTIMING_SIZE),
            False,
        )

    def test_replacing_bittiming_while_stopped_is_accepted(self):
        # Requests arriving out of order: a second BITTIMING before any
        # MODE START simply replaces the first; both are while-stopped.
        _core, ctrl = make()
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_500K), True)
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_125K), True)
        self.assertEqual(ctrl._bittiming[0][3], 125_000)

    def test_bittiming_while_running_is_rejected_and_leaves_channel_running(self):
        core, ctrl = make()
        bring_up(ctrl)
        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_125K), False)
        can = core._channels[0].can
        self.assertTrue(can._started)
        self.assertEqual(can._bitrate, _BT_500K_BITRATE)


class TestMode(unittest.TestCase):
    def test_start_before_bittiming_is_rejected(self):
        core, ctrl = make()
        payload = protocol.pack_device_mode(protocol.CAN_MODE_START, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, payload), False)
        self.assertIsNone(core._channels[0].can)

    def test_start_then_start_again_is_idempotent(self):
        core, ctrl = make()
        bring_up(ctrl)
        can_before = core._channels[0].can
        payload = protocol.pack_device_mode(protocol.CAN_MODE_START, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, payload), True)
        self.assertIs(core._channels[0].can, can_before)
        self.assertTrue(can_before._started)

    def test_stop_when_never_started_does_not_raise(self):
        core, ctrl = make()
        payload = protocol.pack_device_mode(protocol.CAN_MODE_RESET, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, payload), True)
        self.assertIsNone(core._channels[0].can)

    def test_start_then_stop_then_start_again(self):
        # Requests arriving out of order: a full stop/reconfigure/restart
        # cycle after the channel has already been up once.
        core, ctrl = make()
        bring_up(ctrl)
        stop_payload = protocol.pack_device_mode(protocol.CAN_MODE_RESET, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, stop_payload), True)
        self.assertFalse(core._channels[0].can._started)

        self.assertIs(do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_125K), True)
        start_payload = protocol.pack_device_mode(protocol.CAN_MODE_START, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, start_payload), True)
        can = core._channels[0].can
        self.assertTrue(can._started)
        self.assertEqual(can._bitrate, 125_000)

    def test_channel_out_of_range_stalls(self):
        core, ctrl = make(num_channels=1)
        payload = protocol.pack_device_mode(protocol.CAN_MODE_START, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 1, 0, payload), False)
        self.assertIsNone(core._channels[0].can)

    def test_wrong_wlength_stalls(self):
        _core, ctrl = make()
        self.assertIs(setup_only(ctrl, _BM_OUT, protocol.BREQ_MODE, 0, 0, 4), False)

    def test_unknown_mode_value_stalls(self):
        _core, ctrl = make()
        bring_up(ctrl)
        payload = protocol.pack_device_mode(2, 0)  # neither CAN_MODE_RESET nor CAN_MODE_START
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, payload), False)

    def test_normal_mode_maps_to_can_mode_normal(self):
        core, ctrl = make()
        bring_up(ctrl, flags=0)
        self.assertEqual(core._channels[0].can._mode, mock_can.MODE_NORMAL)

    def test_loop_back_flag_maps_to_can_mode_loopback(self):
        core, ctrl = make()
        bring_up(ctrl, flags=protocol.MODE_LOOP_BACK)
        self.assertEqual(core._channels[0].can._mode, mock_can.MODE_LOOPBACK)

    def test_listen_only_flag_maps_to_can_mode_silent(self):
        core, ctrl = make()
        bring_up(ctrl, flags=protocol.MODE_LISTEN_ONLY)
        self.assertEqual(core._channels[0].can._mode, mock_can.MODE_SILENT)

    def test_loop_back_and_listen_only_maps_to_can_mode_silent_loopback(self):
        core, ctrl = make()
        bring_up(ctrl, flags=protocol.MODE_LOOP_BACK | protocol.MODE_LISTEN_ONLY)
        self.assertEqual(core._channels[0].can._mode, mock_can.MODE_SILENT_LOOPBACK)

    def test_unadvertised_flag_bits_are_ignored(self):
        # This device never advertises FEATURE_HW_TIMESTAMP, so a
        # compliant host never sets MODE_HW_TIMESTAMP talking to it; a
        # flag word carrying it anyway (e.g. left over from a different
        # device's negotiation) must not change the resulting CAN mode.
        core, ctrl = make()
        bring_up(ctrl, flags=protocol.MODE_LOOP_BACK | protocol.MODE_HW_TIMESTAMP)
        self.assertEqual(core._channels[0].can._mode, mock_can.MODE_LOOPBACK)

    def test_configured_timing_reaches_the_can_core(self):
        core, ctrl = make()
        bring_up(ctrl, bittiming=_BT_500K)
        can = core._channels[0].can
        self.assertEqual(can._bitrate, _BT_500K_BITRATE)
        self.assertEqual(can._tseg1, _BT_500K_TSEG1)
        self.assertEqual(can._tseg2, _BT_500K_TSEG2)
        self.assertEqual(can._sjw, _BT_500K_SJW)

    def test_wrong_direction_stalls(self):
        _core, ctrl = make()
        self.assertIs(
            setup_only(ctrl, _BM_IN, protocol.BREQ_MODE, 0, 0, protocol.DEVICE_MODE_SIZE),
            False,
        )


class TestWValueConvention(unittest.TestCase):
    def test_per_channel_requests_reject_an_out_of_range_wvalue(self):
        core, ctrl = make(num_channels=2)
        for breq, w_length in (
            (protocol.BREQ_BT_CONST, protocol.BT_CONST_SIZE),
            (protocol.BREQ_BITTIMING, protocol.BITTIMING_SIZE),
            (protocol.BREQ_MODE, protocol.DEVICE_MODE_SIZE),
        ):
            bm = _BM_IN if breq == protocol.BREQ_BT_CONST else _BM_OUT
            self.assertIs(
                setup_only(ctrl, bm, breq, 2, 0, w_length),
                False,
                "breq %d accepted an out-of-range channel" % breq,
            )
        self.assertIsNone(core._channels[0].can)
        self.assertIsNone(core._channels[1].can)

    def test_device_scoped_requests_accept_wvalue_one_regardless_of_channel_count(self):
        _core, ctrl = make(num_channels=2)
        self.assertIsNot(
            do_in(ctrl, protocol.BREQ_DEVICE_CONFIG, 1, 0, protocol.DEVICE_CONFIG_SIZE), False
        )


class TestUnknownRequest(unittest.TestCase):
    def test_unimplemented_breq_stalls(self):
        _core, ctrl = make()
        for breq in (
            protocol.BREQ_BERR,
            protocol.BREQ_TIMESTAMP,
            protocol.BREQ_IDENTIFY,
            protocol.BREQ_GET_USER_ID,
            protocol.BREQ_SET_USER_ID,
            protocol.BREQ_DATA_BITTIMING,
            protocol.BREQ_BT_CONST_EXT,
            protocol.BREQ_SET_TERMINATION,
            protocol.BREQ_GET_TERMINATION,
            protocol.BREQ_GET_STATE,
        ):
            self.assertIs(
                setup_only(ctrl, _BM_IN, breq, 0, 0, 4),
                False,
                "breq %d should stall (feature not advertised)" % breq,
            )

    def test_request_number_outside_the_enum_stalls(self):
        _core, ctrl = make()
        self.assertIs(setup_only(ctrl, _BM_IN, 99, 0, 0, 4), False)


class TestExceptionBoundary(unittest.TestCase):
    def test_malformed_setup_packet_stalls_rather_than_raising(self):
        _core, ctrl = make()
        # Six bytes instead of the required eight: struct.unpack raises,
        # which control_xfer_cb must catch at its own boundary rather than
        # letting propagate into the (in this test) nonexistent C caller.
        result = ctrl.control_xfer_cb(_STAGE_SETUP, b"\x41\x00\x00\x00")
        self.assertIs(result, False)

    def test_ack_stage_exception_stalls_and_clears_pending_state(self):
        _core, ctrl = make()
        bad = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=99, brp=1)
        req = setup_packet(_BM_OUT, protocol.BREQ_BITTIMING, 0, 0, len(bad))
        buf = ctrl.control_xfer_cb(_STAGE_SETUP, req)
        self.assertIsNot(buf, False)
        buf[:] = bad
        ctrl.control_xfer_cb(_STAGE_DATA, req)
        self.assertIs(ctrl.control_xfer_cb(_STAGE_ACK, req), False)
        self.assertIsNone(ctrl._pending_ack)

    def test_stall_from_one_transfer_does_not_affect_the_next(self):
        _core, ctrl = make()
        ctrl.control_xfer_cb(_STAGE_SETUP, b"\x41\x00\x00\x00")
        payload = protocol.pack_host_config()
        self.assertIs(
            do_out(ctrl, protocol.BREQ_HOST_FORMAT, protocol.DEVICE_SCOPE_WVALUE, 0, payload),
            True,
        )

    def test_setup_only_transfer_abandoned_before_ack_does_not_stall_the_next(self):
        # A MODE SETUP is accepted (its buffer handed out, its ACK-stage
        # closure armed) and then abandoned: the host never reaches DATA
        # or ACK for it. Without invalidating the closure at the next
        # SETUP, it fires over the still-zeroed buffer on the ACK stage
        # of a wholly unrelated request and silently stops the channel.
        core, ctrl = make()
        bring_up(ctrl)
        self.assertTrue(ctrl._started[0])

        req = setup_packet(_BM_OUT, protocol.BREQ_MODE, 0, 0, protocol.DEVICE_MODE_SIZE)
        buf = ctrl.control_xfer_cb(_STAGE_SETUP, req)
        self.assertIsNot(buf, False)
        self.assertIsNotNone(ctrl._pending_ack)

        payload = protocol.pack_device_config(0, 2, 1)
        self.assertEqual(
            do_in(
                ctrl,
                protocol.BREQ_DEVICE_CONFIG,
                protocol.DEVICE_SCOPE_WVALUE,
                0,
                protocol.DEVICE_CONFIG_SIZE,
            ),
            payload,
        )
        self.assertTrue(ctrl._started[0])
        self.assertTrue(core._channels[0].started)


class TestChannelCountAndClassResolution(unittest.TestCase):
    def test_num_channels_defaults_to_can_cores_own_count(self):
        # F25: this class is not itself a source of truth for the channel
        # count; left unset, it takes CanCore's.
        core = can_core.CanCore(num_channels=3, can_class=mock_can.MockCAN)
        ctrl = control.GsUsbControl(core)
        self.assertEqual(ctrl._num_channels, 3)

    def test_mismatched_explicit_num_channels_raises(self):
        core = can_core.CanCore(num_channels=2, can_class=mock_can.MockCAN)
        with self.assertRaises(ValueError):
            control.GsUsbControl(core, num_channels=3)

    def test_mode_resolves_the_same_can_class_can_core_uses(self):
        # F26: a MODE START's flags-to-mode mapping must use whatever
        # class CanCore itself resolved, not a second, independently
        # resolved one that could disagree under test.
        core, ctrl = make()
        bring_up(ctrl, flags=protocol.MODE_LOOP_BACK)
        self.assertIs(type(core._channels[0].can), core.resolve_can_class())


class TestReset(unittest.TestCase):
    def test_reset_stops_every_running_channel(self):
        core, ctrl = make(num_channels=2)
        bring_up(ctrl, channel=0)
        bring_up(ctrl, channel=1)
        self.assertTrue(ctrl._started[0])
        self.assertTrue(ctrl._started[1])

        ctrl.reset()

        self.assertFalse(ctrl._started[0])
        self.assertFalse(ctrl._started[1])
        self.assertFalse(core._channels[0].started)
        self.assertFalse(core._channels[1].started)

    def test_reset_preserves_already_accepted_bittiming(self):
        # A host resending BITTIMING is not guaranteed after a bus reset
        # (see the module docstring's BITTIMING section); discarding it
        # here would make the next MODE START fail for a channel the host
        # reasonably believes is already configured.
        core, ctrl = make()
        assert do_out(ctrl, protocol.BREQ_BITTIMING, 0, 0, _BT_500K) is True

        ctrl.reset()

        payload = protocol.pack_device_mode(protocol.CAN_MODE_START, 0)
        self.assertIs(do_out(ctrl, protocol.BREQ_MODE, 0, 0, payload), True)

    def test_reset_clears_a_pending_ack_left_outstanding_mid_transfer(self):
        core, ctrl = make()
        req = setup_packet(_BM_OUT, protocol.BREQ_BITTIMING, 0, 0, len(_BT_500K))
        buf = ctrl.control_xfer_cb(_STAGE_SETUP, req)
        self.assertIsNot(buf, False)
        buf[:] = _BT_500K
        self.assertIsNotNone(ctrl._pending_ack)

        ctrl.reset()

        self.assertIsNone(ctrl._pending_ack)


if __name__ == "__main__":
    unittest.main()
