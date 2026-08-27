"""
Byte-exact tests for gs_usb.protocol against the reference capture at
planning/reference/usbmon/u2c_lifecycle.pcap (BigTreeTech U2C, budgetcan
firmware, captured on kernel 6.17; see the REFERENCE.md alongside it).
Fixture comments give the capture's frame number so a mismatch can be
checked against the pcap directly.
"""
import gc
import unittest

from gs_usb import protocol


class TestClassicFrameCodec(unittest.TestCase):
    # Frame 199 (OUT, EP 0x02): host -> device, standard id, 8-byte payload.
    HOST_TO_DEVICE_STD = bytes.fromhex("000000002301000008000000d994553cc05d3768")
    # Frame 201 (IN, EP 0x81): echo of the frame above, with timestamp.
    ECHO_STD = bytes.fromhex("000000002301000008000000d994553cc05d37682198e80b")
    # Frame 203 (IN, EP 0x81): the loopback receive of the same frame.
    RX_STD = bytes.fromhex("ffffffff2301000008000000d994553cc05d37680599e80b")

    def test_pack_host_to_device_standard_id(self):
        packed = protocol.pack_classic_frame(
            echo_id=0,
            can_id=0x123,
            can_dlc=8,
            channel=0,
            flags=0,
            data=bytes.fromhex("d994553cc05d3768"),
        )
        self.assertEqual(packed, self.HOST_TO_DEVICE_STD)
        self.assertEqual(len(packed), protocol.FRAME_SIZE_CLASSIC)

    def test_unpack_host_to_device_standard_id(self):
        (
            echo_id,
            can_id,
            can_dlc,
            channel,
            flags,
            data,
            timestamp_us,
        ) = protocol.unpack_classic_frame(self.HOST_TO_DEVICE_STD, with_timestamp=False)
        self.assertEqual(echo_id, 0)
        self.assertEqual(can_id, 0x123)
        self.assertEqual(can_dlc, 8)
        self.assertEqual(channel, 0)
        self.assertEqual(flags, 0)
        self.assertEqual(data, bytes.fromhex("d994553cc05d3768"))
        self.assertIsNone(timestamp_us)

    def test_pack_echo_with_timestamp(self):
        packed = protocol.pack_classic_frame(
            echo_id=0,
            can_id=0x123,
            can_dlc=8,
            channel=0,
            flags=0,
            data=bytes.fromhex("d994553cc05d3768"),
            timestamp_us=199792673,
        )
        self.assertEqual(packed, self.ECHO_STD)
        self.assertEqual(len(packed), protocol.FRAME_SIZE_CLASSIC_TS)

    def test_unpack_echo_with_timestamp(self):
        (
            echo_id,
            can_id,
            can_dlc,
            channel,
            flags,
            data,
            timestamp_us,
        ) = protocol.unpack_classic_frame(self.ECHO_STD, with_timestamp=True)
        self.assertEqual(echo_id, 0)
        self.assertEqual(can_id, 0x123)
        self.assertEqual(timestamp_us, 199792673)
        self.assertEqual(data, bytes.fromhex("d994553cc05d3768"))

    def test_pack_rx_frame_uses_echo_id_rx_marker(self):
        packed = protocol.pack_classic_frame(
            echo_id=protocol.ECHO_ID_RX,
            can_id=0x123,
            can_dlc=8,
            channel=0,
            flags=0,
            data=bytes.fromhex("d994553cc05d3768"),
            timestamp_us=199792901,
        )
        self.assertEqual(packed, self.RX_STD)

    def test_unpack_rx_frame_echo_id(self):
        echo_id, *_rest = protocol.unpack_classic_frame(self.RX_STD, with_timestamp=True)
        self.assertEqual(echo_id, protocol.ECHO_ID_RX)
        self.assertEqual(echo_id, 0xFFFFFFFF)

    # Frame 259 (OUT): extended id 0x1FFFFFFF (EFF flag set), 8-byte payload.
    HOST_TO_DEVICE_EXT = bytes.fromhex("00000000ffffff9f08000000d29d0919b7e3c031")
    # Frame 261 (IN): its echo.
    ECHO_EXT = bytes.fromhex("00000000ffffff9f08000000d29d0919b7e3c031e159e90b")

    def test_pack_extended_id_sets_eff_flag(self):
        can_id = protocol.combine_can_id(0x1FFFFFFF, eff=True)
        self.assertEqual(can_id, 0x9FFFFFFF)
        packed = protocol.pack_classic_frame(
            echo_id=0,
            can_id=can_id,
            can_dlc=8,
            channel=0,
            flags=0,
            data=bytes.fromhex("d29d0919b7e3c031"),
        )
        self.assertEqual(packed, self.HOST_TO_DEVICE_EXT)

    def test_unpack_extended_id_round_trips_through_split_can_id(self):
        _echo_id, can_id, *_rest = protocol.unpack_classic_frame(
            self.HOST_TO_DEVICE_EXT, with_timestamp=False
        )
        ident, eff, rtr, err = protocol.split_can_id(can_id)
        self.assertEqual(ident, 0x1FFFFFFF)
        self.assertTrue(eff)
        self.assertFalse(rtr)
        self.assertFalse(err)

    def test_unpack_extended_echo_with_timestamp(self):
        (
            echo_id,
            can_id,
            can_dlc,
            channel,
            flags,
            data,
            timestamp_us,
        ) = protocol.unpack_classic_frame(self.ECHO_EXT, with_timestamp=True)
        self.assertEqual(echo_id, 0)
        self.assertEqual(can_id, 0x9FFFFFFF)
        self.assertEqual(can_dlc, 8)
        self.assertEqual(timestamp_us, 0x0BE959E1)
        self.assertEqual(data, bytes.fromhex("d29d0919b7e3c031"))

    # Frame 279 (OUT): standard id 0x456, zero-length payload (dlc 0).
    HOST_TO_DEVICE_ZERO_LEN = bytes.fromhex("0000000056040000000000000000000000000000")
    # Frame 281 (IN): its echo, with timestamp.
    ECHO_ZERO_LEN = bytes.fromhex("0000000056040000000000000000000000000000aab7e90b")

    def test_pack_zero_length_payload(self):
        packed = protocol.pack_classic_frame(
            echo_id=0,
            can_id=0x456,
            can_dlc=0,
            channel=0,
            flags=0,
            data=b"",
        )
        self.assertEqual(packed, self.HOST_TO_DEVICE_ZERO_LEN)
        self.assertEqual(len(packed), protocol.FRAME_SIZE_CLASSIC)

    def test_unpack_zero_length_payload(self):
        (
            echo_id,
            can_id,
            can_dlc,
            channel,
            flags,
            data,
            timestamp_us,
        ) = protocol.unpack_classic_frame(self.HOST_TO_DEVICE_ZERO_LEN, with_timestamp=False)
        self.assertEqual(can_id, 0x456)
        self.assertEqual(can_dlc, 0)
        self.assertEqual(protocol.dlc_to_len(can_dlc), 0)
        # The wire field is still the full 8 bytes, zero-padded.
        self.assertEqual(data, bytes(8))

    def test_pack_zero_length_echo_with_timestamp(self):
        packed = protocol.pack_classic_frame(
            echo_id=0,
            can_id=0x456,
            can_dlc=0,
            channel=0,
            flags=0,
            data=b"",
            timestamp_us=0x0BE9B7AA,
        )
        self.assertEqual(packed, self.ECHO_ZERO_LEN)

    def test_rtr_frame_flag_round_trips(self):
        # Not present in the capture (cangen did not emit one); constructed
        # directly against the documented CAN_RTR_FLAG bit instead of the
        # pcap. An RTR frame carries no payload bytes regardless of dlc.
        can_id = protocol.combine_can_id(0x123, rtr=True)
        packed = protocol.pack_classic_frame(
            echo_id=protocol.ECHO_ID_RX,
            can_id=can_id,
            can_dlc=8,
            channel=0,
            flags=0,
            data=b"",
        )
        _echo_id, unpacked_id, can_dlc, _ch, _flags, data, _ts = protocol.unpack_classic_frame(
            packed, with_timestamp=False
        )
        ident, eff, rtr, err = protocol.split_can_id(unpacked_id)
        self.assertEqual(ident, 0x123)
        self.assertFalse(eff)
        self.assertTrue(rtr)
        self.assertFalse(err)
        self.assertEqual(can_dlc, 8)  # RTR still carries the requested dlc
        self.assertEqual(data, bytes(8))  # but no payload bytes on the wire

    def test_rejects_payload_longer_than_eight_bytes(self):
        with self.assertRaises(ValueError):
            protocol.pack_classic_frame(0, 0x123, 8, 0, 0, bytes(9))

    def test_frame_sizes(self):
        self.assertEqual(protocol.FRAME_HEADER_SIZE, 12)
        self.assertEqual(protocol.FRAME_SIZE_CLASSIC, 20)
        self.assertEqual(protocol.FRAME_SIZE_CLASSIC_TS, 24)
        self.assertEqual(protocol.classic_frame_size(with_timestamp=False), 20)
        self.assertEqual(protocol.classic_frame_size(with_timestamp=True), 24)


class TestZeroAllocationPackPath(unittest.TestCase):
    def test_pack_classic_frame_into_allocates_nothing(self):
        buf = bytearray(protocol.FRAME_SIZE_CLASSIC_TS)
        data = bytes.fromhex("d994553cc05d3768")

        gc.collect()
        before = gc.mem_alloc()
        protocol.pack_classic_frame_into(buf, 0, 0, 0x123, 8, 0, 0, data, timestamp_us=199792673)
        after = gc.mem_alloc()

        self.assertEqual(after, before)
        self.assertEqual(bytes(buf), TestClassicFrameCodec.ECHO_STD)

    def test_pack_classic_frame_into_at_nonzero_offset_allocates_nothing(self):
        # The data plane packs directly into a slot of a shared bulk-IN
        # buffer, not at offset 0, so the offset arithmetic itself must not
        # allocate either.
        buf = bytearray(64)
        data = bytes.fromhex("d994553cc05d3768")

        gc.collect()
        before = gc.mem_alloc()
        size = protocol.pack_classic_frame_into(
            buf, 10, 0, 0x123, 8, 0, 0, data, timestamp_us=199792673
        )
        after = gc.mem_alloc()

        self.assertEqual(after, before)
        self.assertEqual(size, protocol.FRAME_SIZE_CLASSIC_TS)
        self.assertEqual(bytes(buf[10 : 10 + size]), TestClassicFrameCodec.ECHO_STD)

    def test_pack_classic_frame_into_extended_id_allocates_nothing(self):
        # F15: an extended id's EFF flag pushes a *combined* can_id past
        # 0x80000000, which would no longer fit a 32-bit target's small-int
        # range; this function must never construct that combined value.
        buf = bytearray(protocol.FRAME_SIZE_CLASSIC)
        data = bytes.fromhex("d29d0919b7e3c031")

        gc.collect()
        before = gc.mem_alloc()
        protocol.pack_classic_frame_into(buf, 0, 0, 0x1FFFFFFF, 8, 0, 0, data, eff=True)
        after = gc.mem_alloc()

        self.assertEqual(after, before)
        self.assertEqual(bytes(buf), TestClassicFrameCodec.HOST_TO_DEVICE_EXT)

    def test_unpack_classic_frame_into_allocates_nothing(self):
        buf = TestClassicFrameCodec.ECHO_STD
        out = [0, 0, 0, 0, 0, 0, 0, 0, 0]
        data_out = bytearray(protocol.CLASSIC_DATA_SIZE)

        gc.collect()
        before = gc.mem_alloc()
        protocol.unpack_classic_frame_into(buf, 0, out, data_out, with_timestamp=True)
        after = gc.mem_alloc()

        self.assertEqual(after, before)
        self.assertEqual(out, [0, 0x123, 8, 0, 0, False, False, False, 199792673])

    def test_unpack_classic_frame_into_extended_id_allocates_nothing(self):
        buf = TestClassicFrameCodec.HOST_TO_DEVICE_EXT
        out = [0, 0, 0, 0, 0, 0, 0, 0, 0]
        data_out = bytearray(protocol.CLASSIC_DATA_SIZE)

        gc.collect()
        before = gc.mem_alloc()
        protocol.unpack_classic_frame_into(buf, 0, out, data_out, with_timestamp=False)
        after = gc.mem_alloc()

        self.assertEqual(after, before)
        self.assertEqual(out[:5], [0, 0x1FFFFFFF, 8, 0, 0])
        self.assertEqual(out[5:8], [True, False, False])


class TestDlcHandling(unittest.TestCase):
    def test_dlc_to_len_below_saturation(self):
        for dlc in range(0, 9):
            self.assertEqual(protocol.dlc_to_len(dlc), dlc)

    def test_dlc_to_len_saturates_above_eight(self):
        for dlc in range(9, 16):
            self.assertEqual(protocol.dlc_to_len(dlc), 8)

    def test_len_to_dlc_is_exact_within_classic_range(self):
        for length in range(0, 9):
            self.assertEqual(protocol.len_to_dlc(length), length)


class TestCanIdFlags(unittest.TestCase):
    def test_split_standard_id(self):
        ident, eff, rtr, err = protocol.split_can_id(0x123)
        self.assertEqual(ident, 0x123)
        self.assertFalse(eff)
        self.assertFalse(rtr)
        self.assertFalse(err)

    def test_split_extended_id(self):
        ident, eff, rtr, err = protocol.split_can_id(0x9FFFFFFF)
        self.assertEqual(ident, 0x1FFFFFFF)
        self.assertTrue(eff)
        self.assertFalse(rtr)
        self.assertFalse(err)

    def test_split_rtr_and_err_flags(self):
        _ident, eff, rtr, err = protocol.split_can_id(protocol.CAN_RTR_FLAG | 0x42)
        self.assertFalse(eff)
        self.assertTrue(rtr)
        self.assertFalse(err)

        _ident, eff, rtr, err = protocol.split_can_id(protocol.CAN_ERR_FLAG | 0x42)
        self.assertFalse(eff)
        self.assertFalse(rtr)
        self.assertTrue(err)

    def test_combine_standard_id_round_trips(self):
        can_id = protocol.combine_can_id(0x321)
        self.assertEqual(can_id, 0x321)
        self.assertEqual(protocol.split_can_id(can_id), (0x321, False, False, False))

    def test_combine_extended_id_round_trips(self):
        can_id = protocol.combine_can_id(0x1FFFFFFF, eff=True)
        self.assertEqual(can_id, 0x9FFFFFFF)
        self.assertEqual(protocol.split_can_id(can_id), (0x1FFFFFFF, True, False, False))

    def test_combine_masks_out_of_range_standard_id(self):
        # 0x123 has bits above the 11-bit SFF range; combine must not let
        # them bleed into the flag bits above the mask.
        can_id = protocol.combine_can_id(0x1FFF)
        self.assertEqual(can_id, 0x1FFF & protocol.CAN_SFF_MASK)
        self.assertEqual(can_id & protocol.CAN_EFF_FLAG, 0)

    def test_combine_all_flags_together(self):
        can_id = protocol.combine_can_id(0x1FFFFFFF, eff=True, rtr=True, err=True)
        ident, eff, rtr, err = protocol.split_can_id(can_id)
        self.assertEqual(ident, 0x1FFFFFFF)
        self.assertTrue(eff)
        self.assertTrue(rtr)
        self.assertTrue(err)


class TestControlPayloadCodecs(unittest.TestCase):
    # Fixtures below are all taken from the probe/bring-up section of the
    # reference capture (see REFERENCE.md "Control plane, as observed").

    def test_host_format_payload(self):
        fixture = bytes.fromhex("efbe0000")
        self.assertEqual(protocol.HOST_CONFIG_SIZE, 4)
        self.assertEqual(protocol.pack_host_config(), fixture)
        self.assertEqual(protocol.pack_host_config(protocol.HOST_FORMAT_MAGIC), fixture)
        self.assertEqual(protocol.unpack_host_config(fixture), protocol.HOST_FORMAT_MAGIC)

    def test_device_config_payload(self):
        fixture = bytes.fromhex("000000000200000001000000")
        self.assertEqual(protocol.DEVICE_CONFIG_SIZE, 12)
        packed = protocol.pack_device_config(icount=0, sw_version=2, hw_version=1)
        self.assertEqual(packed, fixture)
        self.assertEqual(protocol.unpack_device_config(fixture), (0, 0, 0, 0, 2, 1))

    def test_bt_const_payload(self):
        fixture = bytes.fromhex(
            "b3080000"
            "0090d003"
            "01000000"
            "10000000"
            "01000000"
            "08000000"
            "04000000"
            "01000000"
            "00040000"
            "01000000"
        )
        self.assertEqual(protocol.BT_CONST_SIZE, 40)
        feature = (
            protocol.FEATURE_LISTEN_ONLY
            | protocol.FEATURE_LOOP_BACK
            | protocol.FEATURE_HW_TIMESTAMP
            | protocol.FEATURE_IDENTIFY
            | protocol.FEATURE_PAD_PKTS_TO_MAX_PKT_SIZE
            | protocol.FEATURE_TERMINATION
        )
        self.assertEqual(feature, 0x8B3)
        packed = protocol.pack_bt_const(
            feature=feature,
            fclk_can=64000000,
            tseg1_min=1,
            tseg1_max=16,
            tseg2_min=1,
            tseg2_max=8,
            sjw_max=4,
            brp_min=1,
            brp_max=1024,
            brp_inc=1,
        )
        self.assertEqual(packed, fixture)
        self.assertEqual(
            protocol.unpack_bt_const(fixture),
            (feature, 64000000, 1, 16, 1, 8, 4, 1, 1024, 1),
        )

    def test_bittiming_payload(self):
        fixture = bytes.fromhex("06000000" "07000000" "02000000" "01000000" "08000000")
        self.assertEqual(protocol.BITTIMING_SIZE, 20)
        packed = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=1, brp=8)
        self.assertEqual(packed, fixture)
        self.assertEqual(protocol.unpack_bittiming(fixture), (6, 7, 2, 1, 8))

    def test_device_mode_start_payload(self):
        fixture = bytes.fromhex("01000000" "12000000")
        flags = protocol.MODE_LOOP_BACK | protocol.MODE_HW_TIMESTAMP
        self.assertEqual(flags, 0x12)
        packed = protocol.pack_device_mode(protocol.CAN_MODE_START, flags)
        self.assertEqual(packed, fixture)
        self.assertEqual(protocol.unpack_device_mode(fixture), (protocol.CAN_MODE_START, flags))

    def test_device_mode_reset_payload(self):
        fixture = bytes.fromhex("00000000" "00000000")
        packed = protocol.pack_device_mode(protocol.CAN_MODE_RESET, 0)
        self.assertEqual(packed, fixture)
        self.assertEqual(protocol.unpack_device_mode(fixture), (protocol.CAN_MODE_RESET, 0))

    def test_get_termination_payload(self):
        fixture = bytes.fromhex("00000000")
        self.assertEqual(protocol.TERMINATION_STATE_SIZE, 4)
        self.assertEqual(protocol.unpack_termination_state(fixture), protocol.TERMINATION_OFF)
        self.assertEqual(
            protocol.pack_termination_state(protocol.TERMINATION_ON), bytes.fromhex("01000000")
        )

    def test_timestamp_payload(self):
        fixture = bytes.fromhex("691bd90b")
        self.assertEqual(protocol.DEVICE_TIMESTAMP_SIZE, 4)
        self.assertEqual(protocol.unpack_device_timestamp(fixture), 0x0BD91B69)
        self.assertEqual(protocol.pack_device_timestamp(0x0BD91B69), fixture)


if __name__ == "__main__":
    unittest.main()
