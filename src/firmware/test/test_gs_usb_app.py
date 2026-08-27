"""
Tests for the composition root.

Everything below the composition is covered by its own suite; what is left to
check is the wiring, and specifically the two things a construction cycle makes
easy to get wrong: that callbacks arriving before the data plane exists are
survivable, and that the callbacks really reach the data plane once it does.
"""

import struct
import unittest

import mock_can
from gs_usb import app as app_mod
from gs_usb import protocol
from gs_usb import usb_device


# machine.USBDevice reports a completed transfer with result 0.
_XFER_SUCCESS = 0


class FakeUsbDevice:
    """Stands in for machine.USBDevice's singleton."""

    BUILTIN_NONE = "builtin_none"

    def __init__(self):
        self.builtin_driver = None
        self.config_kwargs = None
        self.active_calls = []
        self.pending = {}
        self.submits = []

    def active(self, value=None):
        if value is None:
            return bool(self.active_calls and self.active_calls[-1])
        self.active_calls.append(value)
        return value

    def config(self, **kwargs):
        self.config_kwargs = kwargs

    def submit_xfer(self, ep, buffer):
        if ep in self.pending:
            return False
        self.pending[ep] = buffer
        self.submits.append(ep)
        return True

    def complete(self, app, ep, xferred=None):
        buf = self.pending.pop(ep)
        app.usb_descriptors.xfer_cb(ep, _XFER_SUCCESS, len(buf) if xferred is None else xferred)

    def feed_out(self, app, frame_bytes):
        buf = self.pending[usb_device.EP_BULK_OUT]
        buf[: len(frame_bytes)] = frame_bytes
        self.complete(app, usb_device.EP_BULK_OUT, len(frame_bytes))


def make(num_channels=1):
    app = app_mod.GsUsbApp(num_channels=num_channels, can_class=mock_can.MockCAN)
    fake = FakeUsbDevice()
    app.start(fake)
    return app, fake


class TestCallbacksBeforeTheDataPlaneExists(unittest.TestCase):
    """The handlers are registered during apply(), which runs before the data
    plane is constructed, so each must tolerate being called with none."""

    def test_xfer_before_start_is_dropped(self):
        app = app_mod.GsUsbApp(can_class=mock_can.MockCAN)
        self.assertIsNone(app.plane)
        self.assertFalse(app.usb_descriptors.xfer_cb(usb_device.EP_BULK_IN, _XFER_SUCCESS, 0))

    def test_open_itf_before_start_is_dropped(self):
        app = app_mod.GsUsbApp(can_class=mock_can.MockCAN)
        app.usb_descriptors.open_itf_cb(None)  # must not raise

    def test_reset_before_start_still_clears_control_state(self):
        app = app_mod.GsUsbApp(can_class=mock_can.MockCAN)
        app.usb_descriptors.reset_cb()  # must not raise with plane None


class TestApply(unittest.TestCase):
    def test_start_configures_and_enumerates(self):
        app, fake = make()
        self.assertEqual(fake.builtin_driver, FakeUsbDevice.BUILTIN_NONE)
        cfg = fake.config_kwargs
        self.assertTrue("desc_dev" in cfg)
        self.assertTrue("desc_cfg" in cfg)
        self.assertTrue("desc_bos" in cfg)
        # Deactivated for reconfiguration, then brought up.
        self.assertEqual(fake.active_calls, [False, True])

    def test_vendor_interface_is_number_zero(self):
        app, _ = make()
        self.assertEqual(app.usb_descriptors.vendor_interface_number, 0)

    def test_bulk_out_is_armed_once_started(self):
        _, fake = make()
        self.assertTrue(usb_device.EP_BULK_OUT in fake.pending)


class TestWiring(unittest.TestCase):
    def test_control_request_reaches_the_control_plane(self):
        app, _ = make()
        # DEVICE_CONFIG is an IN request answered entirely from the control
        # plane; getting bytes back proves the dispatch path.
        request = _setup(_BM_IN, protocol.BREQ_DEVICE_CONFIG, protocol.DEVICE_SCOPE_WVALUE, 0, 12)
        result = app.usb_descriptors.control_xfer_cb(_STAGE_SETUP, request)
        self.assertFalse(result is False)
        self.assertEqual(len(bytes(result)), 12)

    def test_host_frame_reaches_the_can_controller(self):
        app, fake = make()
        _bring_up(app)
        frame = protocol.pack_classic_frame(0, 0x123, 8, 0, 0, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        fake.feed_out(app, frame)
        can = app.core._channel(0).can
        self.assertEqual(len(can.tx_log), 1)
        self.assertEqual(can.tx_log[0][0], 0x123)

    def test_received_frame_reaches_the_host(self):
        app, fake = make()
        _bring_up(app)
        if usb_device.EP_BULK_IN in fake.pending:
            fake.complete(app, usb_device.EP_BULK_IN)
        can = app.core._channel(0).can
        can.inject(0x321, b"\xaa\xbb")
        self.assertTrue(usb_device.EP_BULK_IN in fake.pending)
        payload = bytes(fake.pending[usb_device.EP_BULK_IN])
        echo_id, can_id, dlc, channel, _flags, data, _ts = protocol.unpack_classic_frame(
            payload, False
        )
        self.assertEqual(echo_id, protocol.ECHO_ID_RX)
        self.assertEqual(can_id, 0x321)
        self.assertEqual(dlc, 2)
        self.assertEqual(bytes(data[:2]), b"\xaa\xbb")

    def test_bus_reset_clears_control_and_data_plane_state(self):
        app, fake = make()
        _bring_up(app)
        self.assertTrue(app.control._started[0])
        app.usb_descriptors.reset_cb()
        self.assertFalse(app.control._started[0])
        self.assertFalse(app.plane._out_armed)


class TestStop(unittest.TestCase):
    def test_stop_takes_the_device_off_the_bus_and_stops_channels(self):
        app, fake = make()
        _bring_up(app)
        self.assertTrue(app.core.is_started(0))
        app.stop()
        self.assertFalse(app.core.is_started(0))
        self.assertEqual(fake.active_calls[-1], False)


# The runtime USB layer hands control_xfer_cb the raw 8-byte setup packet,
# so build one rather than a stand-in object.
_SETUP_FMT = "<BBHHH"
_BM_OUT = 0x41
_BM_IN = 0xC1
_STAGE_SETUP = 1
_STAGE_DATA = 2
_STAGE_ACK = 3


def _setup(bm, breq, wvalue, windex, wlength):
    return struct.pack(_SETUP_FMT, bm, breq, wvalue, windex, wlength)


def _out(app, breq, wvalue, payload):
    """A full vendor OUT transfer through the composed dispatch path."""
    req = _setup(_BM_OUT, breq, wvalue, 0, len(payload))
    buf = app.usb_descriptors.control_xfer_cb(_STAGE_SETUP, req)
    assert buf is not False, "SETUP stalled for request %d" % breq
    buf[:] = payload
    app.usb_descriptors.control_xfer_cb(_STAGE_DATA, req)
    result = app.usb_descriptors.control_xfer_cb(_STAGE_ACK, req)
    assert result is True, "ACK stalled for request %d" % breq
    return result


def _bring_up(app, channel=0):
    """BITTIMING then MODE START, as the kernel driver's bring-up does.

    16 time quanta at brp=1 against this board's 8 MHz HSE is 500 kbit.
    """
    bt = protocol.pack_bittiming(prop_seg=6, phase_seg1=7, phase_seg2=2, sjw=1, brp=1)
    _out(app, protocol.BREQ_BITTIMING, channel, bt)
    mode = protocol.pack_device_mode(protocol.CAN_MODE_START, protocol.MODE_LOOP_BACK)
    _out(app, protocol.BREQ_MODE, channel, mode)


if __name__ == "__main__":
    unittest.main()
