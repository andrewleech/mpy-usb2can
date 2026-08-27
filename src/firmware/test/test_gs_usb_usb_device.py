"""
Structural tests for gs_usb.usb_device.

These parse the module's own descriptor output back the way a USB host
would: walk the TLV stream, check every bLength, cross-check wTotalLength
and bNumInterfaces against what was actually built, and check endpoint
addresses, directions and the vendor interface's class triple. Identity
fields are cross-referenced field by field against the reference capture at
planning/reference/usbmon/u2c_descriptors.txt (BigTreeTech U2C, budgetcan
firmware), with bcdUSB and bcdDevice called out where this device
deliberately departs from it (R5, F13).
"""
import struct
import unittest

from gs_usb import usb_device


def _iter_descriptors(buf):
    """
    Walk a TLV descriptor buffer the way a host parses one, yielding
    (offset, bLength, bDescriptorType, bytes) for each descriptor in turn.
    """
    offset = 0
    n = len(buf)
    while offset < n:
        length = buf[offset]
        if length == 0:
            raise ValueError("zero-length descriptor at offset {}".format(offset))
        if offset + length > n:
            raise ValueError("descriptor at offset {} overruns the buffer".format(offset))
        dtype = buf[offset + 1]
        yield offset, length, dtype, bytes(buf[offset : offset + length])
        offset += length


class TestDeviceDescriptor(unittest.TestCase):
    def test_length_and_type(self):
        desc = usb_device.build_device_descriptor()
        self.assertEqual(len(desc), usb_device.DEVICE_DESC_SIZE)
        self.assertEqual(desc[0], usb_device.DEVICE_DESC_SIZE)
        self.assertEqual(desc[1], usb_device._DESC_TYPE_DEVICE)

    def test_identity_fields(self):
        desc = usb_device.build_device_descriptor()
        (
            _b_length,
            _b_desc_type,
            bcd_usb,
            dev_class,
            dev_subclass,
            dev_proto,
            max_packet0,
            id_vendor,
            id_product,
            bcd_device,
            i_manuf,
            i_prod,
            i_serial,
            num_configs,
        ) = struct.unpack("<BBHBBBBHHHBBBB", desc)
        self.assertEqual(id_vendor, usb_device.VENDOR_ID)
        self.assertEqual(id_product, usb_device.PRODUCT_ID)
        self.assertEqual(bcd_usb, usb_device.USB_SPEC_VERSION)
        self.assertEqual(bcd_device, usb_device.DEFAULT_DEVICE_VERSION)
        self.assertEqual(dev_class, 0)
        self.assertEqual(dev_subclass, 0)
        self.assertEqual(dev_proto, 0)
        self.assertEqual(max_packet0, 64)
        self.assertEqual(num_configs, 1)
        self.assertEqual(i_manuf, usb_device.STR_IDX_MANUFACTURER)
        self.assertEqual(i_prod, usb_device.STR_IDX_PRODUCT)
        self.assertEqual(i_serial, usb_device.STR_IDX_SERIAL)

    def test_bcd_device_default_is_neither_forbidden_value(self):
        # R5/F13: candleLight_fw and the captured U2C (budgetcan) both use
        # 0x0000, and 0x0200 is the other value the driver study flagged.
        # Windows negative-caches a failed first attach by VID+PID+bcdDevice,
        # so reusing either value would reproduce the trap rather than avoid it.
        self.assertNotEqual(usb_device.DEFAULT_DEVICE_VERSION, 0x0000)
        self.assertNotEqual(usb_device.DEFAULT_DEVICE_VERSION, 0x0200)

    def test_rejects_forbidden_bcd_device_values(self):
        with self.assertRaises(ValueError):
            usb_device.build_device_descriptor(bcd_device=0x0000)
        with self.assertRaises(ValueError):
            usb_device.build_device_descriptor(bcd_device=0x0200)

    def test_custom_identity_fields_round_trip(self):
        desc = usb_device.build_device_descriptor(
            id_vendor=0x1234,
            id_product=0x5678,
            bcd_device=0x0103,
            device_class=0xEF,
            device_subclass=0x02,
            device_protocol=0x01,
            num_configurations=2,
        )
        (
            _b_length,
            _b_desc_type,
            _bcd_usb,
            dev_class,
            dev_subclass,
            dev_proto,
            _max_packet0,
            id_vendor,
            id_product,
            bcd_device,
            *_rest,
            num_configs,
        ) = struct.unpack("<BBHBBBBHHHBBBB", desc)
        self.assertEqual(id_vendor, 0x1234)
        self.assertEqual(id_product, 0x5678)
        self.assertEqual(bcd_device, 0x0103)
        self.assertEqual(dev_class, 0xEF)
        self.assertEqual(dev_subclass, 0x02)
        self.assertEqual(dev_proto, 0x01)
        self.assertEqual(num_configs, 2)

    def test_cross_check_against_reference_capture(self):
        """
        planning/reference/usbmon/u2c_descriptors.txt records the U2C's
        device descriptor as: bcdUSB 2.00, idVendor 0x1d50, idProduct
        0x606f, bcdDevice 0.00, bMaxPacketSize0 64, device class/subclass/
        protocol 0/0/0, bNumConfigurations 1. idVendor, idProduct,
        bMaxPacketSize0, the class triple and bNumConfigurations match;
        bcdUSB and bcdDevice are the deliberate departures R5 requires.
        """
        desc = usb_device.build_device_descriptor()
        (
            _b_length,
            _b_desc_type,
            bcd_usb,
            dev_class,
            dev_subclass,
            dev_proto,
            max_packet0,
            id_vendor,
            id_product,
            bcd_device,
            *_rest,
        ) = struct.unpack("<BBHBBBBHHHBBBB", desc)
        self.assertEqual(id_vendor, 0x1D50)
        self.assertEqual(id_product, 0x606F)
        self.assertEqual(max_packet0, 64)
        self.assertEqual(dev_class, 0)
        self.assertEqual(dev_subclass, 0)
        self.assertEqual(dev_proto, 0)
        self.assertEqual(bcd_usb, 0x0210)
        self.assertNotEqual(bcd_usb, 0x0200)
        self.assertNotEqual(bcd_device, 0x0000)


class TestBosDescriptor(unittest.TestCase):
    def test_length_and_type(self):
        bos = usb_device.build_default_bos()
        self.assertEqual(len(bos), usb_device.BOS_DESC_SIZE + usb_device.USB_2_0_EXTENSION_SIZE)
        b_length, b_desc_type, w_total_length, b_num_device_caps = struct.unpack(
            "<BBHB", bos[: usb_device.BOS_DESC_SIZE]
        )
        self.assertEqual(b_length, usb_device.BOS_DESC_SIZE)
        self.assertEqual(b_desc_type, usb_device._DESC_TYPE_BOS)
        self.assertEqual(w_total_length, len(bos))
        self.assertEqual(b_num_device_caps, 1)

    def test_usb_2_0_extension_capability(self):
        bos = usb_device.build_default_bos()
        cap = bos[usb_device.BOS_DESC_SIZE :]
        b_length, b_desc_type, b_dev_cap_type, bm_attributes = struct.unpack("<BBBI", cap)
        self.assertEqual(b_length, usb_device.USB_2_0_EXTENSION_SIZE)
        self.assertEqual(b_desc_type, usb_device._DESC_TYPE_DEVICE_CAPABILITY)
        self.assertEqual(b_dev_cap_type, usb_device._DEV_CAP_TYPE_USB_2_0_EXTENSION)
        self.assertEqual(bm_attributes, 0)

    def test_device_ships_with_a_bos_by_default(self):
        # bcdUSB 0x0210 obliges the device to answer GET_DESCRIPTOR(BOS)
        # (R5); a None default here would mean stalling that request on a
        # device that advertises support for it (F13's negative-cache trap).
        dev = usb_device.GsUsbUsbDevice()
        self.assertIsNotNone(dev.desc_bos)
        self.assertEqual(dev.desc_bos, usb_device.build_default_bos())


class TestConfigurationDescriptor(unittest.TestCase):
    def test_single_vendor_interface(self):
        dev = usb_device.GsUsbUsbDevice()
        desc_cfg = dev.desc_cfg
        entries = list(_iter_descriptors(desc_cfg))
        self.assertEqual(len(entries), 4)  # config header, 1 interface, 2 endpoints

        _, length, dtype, raw = entries[0]
        self.assertEqual(dtype, usb_device._DESC_TYPE_CONFIGURATION)
        self.assertEqual(length, usb_device.CONFIG_DESC_SIZE)
        (
            _,
            _,
            w_total_length,
            b_num_interfaces,
            _cfg_val,
            _i_cfg,
            attributes,
            _max_power,
        ) = struct.unpack("<BBHBBBBB", raw)
        self.assertEqual(w_total_length, len(desc_cfg))
        self.assertEqual(b_num_interfaces, 1)
        self.assertTrue(attributes & 0x80)  # reserved bit always set

        _, length, dtype, raw = entries[1]
        self.assertEqual(dtype, usb_device._DESC_TYPE_INTERFACE)
        self.assertEqual(length, usb_device.INTERFACE_DESC_SIZE)
        _, _, itf_num, alt, num_ep, itf_class, itf_sub, itf_proto, _i_itf = struct.unpack(
            "<BBBBBBBBB", raw
        )
        self.assertEqual(itf_num, 0)
        self.assertEqual(itf_num, dev.vendor_interface_number)
        self.assertEqual(alt, 0)
        self.assertEqual(num_ep, 2)
        self.assertEqual(itf_class, usb_device.VENDOR_INTERFACE_CLASS)
        self.assertEqual(itf_sub, usb_device.VENDOR_INTERFACE_SUBCLASS)
        self.assertEqual(itf_proto, usb_device.VENDOR_INTERFACE_PROTOCOL)
        self.assertEqual((itf_class, itf_sub, itf_proto), (0xFF, 0xFF, 0xFF))

        ep_in, ep_out = entries[2], entries[3]
        for _, length, dtype, _raw in (ep_in, ep_out):
            self.assertEqual(dtype, usb_device._DESC_TYPE_ENDPOINT)
            self.assertEqual(length, usb_device.ENDPOINT_DESC_SIZE)

        _, _, addr_in, attr_in, size_in, _interval = struct.unpack("<BBBBHB", ep_in[3])
        _, _, addr_out, attr_out, size_out, _interval = struct.unpack("<BBBBHB", ep_out[3])
        self.assertEqual(addr_in, usb_device.EP_BULK_IN)
        self.assertEqual(addr_out, usb_device.EP_BULK_OUT)
        self.assertEqual(addr_in, 0x81)
        self.assertEqual(addr_out, 0x02)
        self.assertTrue(addr_in & 0x80)  # IN
        self.assertFalse(addr_out & 0x80)  # OUT
        self.assertEqual(attr_in, 0x02)  # bulk
        self.assertEqual(attr_out, 0x02)  # bulk
        self.assertEqual(size_in, 32)
        self.assertEqual(size_out, 32)
        self.assertEqual(usb_device.BULK_MAX_PACKET_SIZE, 32)

    def test_every_descriptor_is_consistently_framed(self):
        dev = usb_device.GsUsbUsbDevice()
        desc_cfg = dev.desc_cfg
        total = 0
        for _offset, length, _dtype, _raw in _iter_descriptors(desc_cfg):
            self.assertTrue(length > 0)
            total += length
        self.assertEqual(total, len(desc_cfg))

    def test_max_power_is_encoded_in_2ma_units(self):
        dev = usb_device.GsUsbUsbDevice(max_power_ma=150)
        header = dev.desc_cfg[: usb_device.CONFIG_DESC_SIZE]
        *_rest, max_power = struct.unpack("<BBHBBBBB", header)
        self.assertEqual(max_power, 75)

    def test_cross_check_against_reference_capture(self):
        """
        The reference capture's configuration descriptor uses bulk
        endpoints 0x81 (IN) / 0x02 (OUT) at 32-byte packets, on a class
        ff/ff/ff interface numbered 0. This device matches all of that;
        it does not build the U2C's second (DFU) interface, which is out
        of scope here.
        """
        dev = usb_device.GsUsbUsbDevice()
        entries = list(_iter_descriptors(dev.desc_cfg))
        _, _, itf_num, _alt, _num_ep, itf_class, itf_sub, itf_proto, _i = struct.unpack(
            "<BBBBBBBBB", entries[1][3]
        )
        self.assertEqual(itf_num, 0)
        self.assertEqual((itf_class, itf_sub, itf_proto), (0xFF, 0xFF, 0xFF))
        _, _, addr_in, _attr, size_in, _interval = struct.unpack("<BBBBHB", entries[2][3])
        _, _, addr_out, _attr, size_out, _interval = struct.unpack("<BBBBHB", entries[3][3])
        self.assertEqual((addr_in, addr_out), (0x81, 0x02))
        self.assertEqual((size_in, size_out), (32, 32))


class TestAppendingInterfaces(unittest.TestCase):
    """
    Covers the CDC-REPL-at-interfaces-1-2 case (Q2's named default):
    appending more interfaces to a builder that already holds the vendor
    interface must keep wTotalLength and bNumInterfaces self-consistent
    without any byte offset being touched by hand.
    """

    def test_appending_a_second_interface_stays_consistent(self):
        dev = usb_device.GsUsbUsbDevice()
        self.assertEqual(dev.builder.next_interface_number, 1)

        appended_number = dev.builder.add_interface(
            0x02,
            0x02,
            0x01,  # CDC-ACM-shaped class triple, standing in for a real CDC descriptor
            endpoints=[(0x83, 8, 0x03, 0)],  # one interrupt notification endpoint
        )
        self.assertEqual(appended_number, 1)
        self.assertEqual(dev.builder.next_interface_number, 2)

        desc_cfg = dev.desc_cfg
        entries = list(_iter_descriptors(desc_cfg))

        _, _, w_total_length, b_num_interfaces, *_rest = struct.unpack("<BBHBBBBB", entries[0][3])
        self.assertEqual(w_total_length, len(desc_cfg))
        self.assertEqual(b_num_interfaces, 2)

        # The vendor interface (itf 0) and its 2 endpoints are unchanged by the append.
        _, _, itf0_num, _alt, itf0_num_ep, itf0_class, *_rest = struct.unpack(
            "<BBBBBBBBB", entries[1][3]
        )
        self.assertEqual(itf0_num, 0)
        self.assertEqual(itf0_num_ep, 2)
        self.assertEqual(itf0_class, 0xFF)

        # entries: [0]=cfg header, [1]=itf0, [2..3]=itf0's endpoints, [4]=appended itf1
        _, _, itf1_num, _alt, itf1_num_ep, itf1_class, *_rest = struct.unpack(
            "<BBBBBBBBB", entries[4][3]
        )
        self.assertEqual(itf1_num, 1)
        self.assertEqual(itf1_num_ep, 1)
        self.assertEqual(itf1_class, 0x02)
        self.assertEqual(len(entries), 6)  # cfg header + itf0 (+2 eps) + itf1 (+1 ep)

    def test_add_interface_group_uses_an_iad(self):
        dev = usb_device.GsUsbUsbDevice()
        first_interface = dev.builder.add_interface_group(
            0x02,
            0x02,
            0x01,
            interfaces=[
                (0x02, 0x02, 0x01, []),  # CDC control interface, no endpoints
                (0x0A, 0x00, 0x00, [(0x84, 16), (0x05, 16)]),  # CDC data interface
            ],
        )
        self.assertEqual(first_interface, 1)

        desc_cfg = dev.desc_cfg
        entries = list(_iter_descriptors(desc_cfg))

        _, _, _w_total, b_num_interfaces, *_rest = struct.unpack("<BBHBBBBB", entries[0][3])
        self.assertEqual(b_num_interfaces, 3)  # vendor + CDC control + CDC data

        # entries: [0]=cfg header, [1]=itf0, [2..3]=its endpoints, [4]=IAD,
        # [5]=control itf1 (0 endpoints), [6]=data itf2 (2 endpoints)
        _, length, dtype, raw = entries[4]
        self.assertEqual(dtype, usb_device._DESC_TYPE_INTERFACE_ASSOCIATION)
        self.assertEqual(length, usb_device.IAD_DESC_SIZE)
        _, _, iad_first_itf, iad_itf_count, iad_func_class, *_rest = struct.unpack(
            "<BBBBBBBB", raw
        )
        self.assertEqual(iad_first_itf, 1)
        self.assertEqual(iad_itf_count, 2)
        self.assertEqual(iad_func_class, 0x02)

        _, _, itf1_num, _alt, itf1_num_ep, *_rest = struct.unpack("<BBBBBBBBB", entries[5][3])
        self.assertEqual(itf1_num, 1)
        self.assertEqual(itf1_num_ep, 0)

        _, _, itf2_num, _alt, itf2_num_ep, *_rest = struct.unpack("<BBBBBBBBB", entries[6][3])
        self.assertEqual(itf2_num, 2)
        self.assertEqual(itf2_num_ep, 2)

    def test_add_block_advances_the_interface_counter(self):
        dev = usb_device.GsUsbUsbDevice()
        next_itf = dev.builder.next_interface_number

        # Stands in for descriptor bytes supplied by a driver library, such
        # as micropython-lib's usb-device-cdc, that encodes its own itf
        # number rather than taking one from add_interface().
        pre_encoded = usb_device.build_interface_descriptor(
            next_itf,
            0,
            interface_class=0x02,
            interface_subclass=0x02,
            interface_protocol=0x01,
        )
        first_interface = dev.builder.add_block(pre_encoded, num_interfaces=1)
        self.assertEqual(first_interface, next_itf)
        self.assertEqual(dev.builder.next_interface_number, next_itf + 1)

        desc_cfg = dev.desc_cfg
        _, _, w_total_length, b_num_interfaces, *_rest = struct.unpack(
            "<BBHBBBBB", desc_cfg[: usb_device.CONFIG_DESC_SIZE]
        )
        self.assertEqual(w_total_length, len(desc_cfg))
        self.assertEqual(b_num_interfaces, 2)


class _FakeUSBDevice:
    """Duck-typed stand-in for machine.USBDevice, absent on the unix port."""

    BUILTIN_NONE = "BUILTIN_NONE"

    def __init__(self):
        self.active_calls = []
        self.builtin_driver = None
        self.config_kwargs = None

    def active(self, value=None):
        if value is None:
            return True
        self.active_calls.append(value)

    def config(self, **kwargs):
        self.config_kwargs = kwargs


class TestCallbackDispatch(unittest.TestCase):
    """
    Every callback handed to USBDevice.config() is a thin dispatcher: it
    forwards to whatever handler was injected, unchanged, or falls back to
    a safe default (stall for control transfers, no-op otherwise) when
    none was.
    """

    def test_open_itf_cb_forwards_to_injected_handler(self):
        calls = []
        dev = usb_device.GsUsbUsbDevice(open_itf_handler=lambda view: calls.append(bytes(view)))
        dev.open_itf_cb(b"interface-descriptor-bytes")
        self.assertEqual(calls, [b"interface-descriptor-bytes"])

    def test_open_itf_cb_is_a_noop_without_a_handler(self):
        dev = usb_device.GsUsbUsbDevice()
        dev.open_itf_cb(b"interface-descriptor-bytes")  # must not raise

    def test_control_xfer_cb_forwards_the_handler_return_value(self):
        dev = usb_device.GsUsbUsbDevice(control_xfer_handler=lambda stage, request: stage == 1)
        self.assertTrue(dev.control_xfer_cb(1, b"12345678"))
        self.assertFalse(dev.control_xfer_cb(2, b"12345678"))

    def test_control_xfer_cb_stalls_without_a_handler(self):
        dev = usb_device.GsUsbUsbDevice()
        self.assertFalse(dev.control_xfer_cb(1, b"12345678"))

    def test_xfer_cb_forwards_to_injected_handler(self):
        calls = []
        dev = usb_device.GsUsbUsbDevice(
            xfer_handler=lambda ep, result, n: calls.append((ep, result, n))
        )
        dev.xfer_cb(usb_device.EP_BULK_OUT, 0, 20)
        self.assertEqual(calls, [(usb_device.EP_BULK_OUT, 0, 20)])

    def test_xfer_cb_stalls_without_a_handler(self):
        dev = usb_device.GsUsbUsbDevice()
        self.assertFalse(dev.xfer_cb(usb_device.EP_BULK_IN, 0, 24))

    def test_reset_cb_forwards_to_injected_handler(self):
        calls = []
        dev = usb_device.GsUsbUsbDevice(reset_handler=lambda: calls.append(True))
        dev.reset_cb()
        self.assertEqual(calls, [True])

    def test_reset_cb_is_a_noop_without_a_handler(self):
        dev = usb_device.GsUsbUsbDevice()
        dev.reset_cb()  # must not raise


class TestApply(unittest.TestCase):
    def test_apply_wires_descriptors_and_callbacks(self):
        # Bound methods compare unequal to themselves across separate
        # attribute accesses, so the callbacks USBDevice.config() received
        # are checked by behaviour (do they reach the injected handler?)
        # rather than by identity against dev.open_itf_cb and friends.
        calls: dict[str, object] = {}

        def record_open(view):
            calls["open"] = bytes(view)

        def record_control(stage, request):
            calls["control"] = (stage, bytes(request))
            return True

        def record_xfer(ep, result, n):
            calls["xfer"] = (ep, result, n)

        def record_reset():
            calls["reset"] = True

        dev = usb_device.GsUsbUsbDevice(
            open_itf_handler=record_open,
            control_xfer_handler=record_control,
            xfer_handler=record_xfer,
            reset_handler=record_reset,
        )
        fake = _FakeUSBDevice()

        returned = dev.apply(fake)

        self.assertIs(returned, fake)
        self.assertEqual(fake.active_calls, [False])
        self.assertEqual(fake.builtin_driver, _FakeUSBDevice.BUILTIN_NONE)
        kwargs = fake.config_kwargs
        assert kwargs is not None
        self.assertEqual(kwargs["desc_dev"], dev.desc_dev)
        self.assertEqual(kwargs["desc_cfg"], dev.desc_cfg)
        self.assertEqual(kwargs["desc_strs"], dev.desc_strs)
        self.assertEqual(kwargs["desc_bos"], usb_device.build_default_bos())

        kwargs["open_itf_cb"](b"itf-bytes")
        self.assertEqual(calls["open"], b"itf-bytes")
        self.assertTrue(kwargs["control_xfer_cb"](1, b"12345678"))
        self.assertEqual(calls["control"], (1, b"12345678"))
        kwargs["xfer_cb"](usb_device.EP_BULK_IN, 0, 24)
        self.assertEqual(calls["xfer"], (usb_device.EP_BULK_IN, 0, 24))
        kwargs["reset_cb"]()
        self.assertTrue(calls["reset"])

    def test_apply_passes_through_the_desc_bos_hook(self):
        dev = usb_device.GsUsbUsbDevice(desc_bos=b"fake-bos-blob")
        fake = _FakeUSBDevice()
        dev.apply(fake)
        kwargs = fake.config_kwargs
        assert kwargs is not None
        self.assertEqual(kwargs["desc_bos"], b"fake-bos-blob")


class TestStringTable(unittest.TestCase):
    def test_string_indices_match_the_device_descriptor(self):
        dev = usb_device.GsUsbUsbDevice(manufacturer="Acme", product="Widget", serial="42")
        self.assertEqual(dev.desc_strs[usb_device.STR_IDX_MANUFACTURER], "Acme")
        self.assertEqual(dev.desc_strs[usb_device.STR_IDX_PRODUCT], "Widget")
        self.assertEqual(dev.desc_strs[usb_device.STR_IDX_SERIAL], "42")
        # Index 0 (languages) is left absent so USBDevice falls back to its
        # built-in English default, per machine.USBDevice.rst.
        self.assertFalse(0 in dev.desc_strs)

    def test_default_strings_never_name_another_product(self):
        # R5: never "LinkLayer Labs" or "CANtact Pro", both real other products.
        dev = usb_device.GsUsbUsbDevice()
        for value in dev.desc_strs.values():
            self.assertFalse("LinkLayer Labs" in value)
            self.assertFalse("CANtact Pro" in value)
