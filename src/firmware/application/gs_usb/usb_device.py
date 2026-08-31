"""
gs_usb USB device layer: builds the descriptor set for the vendor bulk
interface and wires machine.USBDevice's three runtime callbacks to handlers
supplied by the caller.

This module owns USB wiring, not gs_usb request semantics: descriptor bytes,
endpoint identity, and callback dispatch. The control plane that answers the
five mandatory control requests and the data plane that moves frames are
separate modules that inject their handlers here; nothing in this file
parses a gs_usb control payload or a gs_host_frame. Endpoint and identity
constants are therefore defined locally rather than imported from
gs_usb.protocol, so this module has no dependency on the wire codec.

machine.USBDevice is only ever touched inside apply(); every descriptor
builder here works on plain bytes, which is what makes the module testable
on the unix port, where machine.USBDevice does not exist.
"""
import struct

# ---------------------------------------------------------------------------
# Identity (R5). VID/PID are the registered gs_usb values. bcdUSB 0x0210
# obliges this device to answer GET_DESCRIPTOR(BOS) (see build_default_bos
# and desc_bos below); bcd_device must be neither 0x0000 nor 0x0200, because
# both values are already in use by real gs_usb devices (candleLight_fw and,
# observed on the bench, a BigTreeTech U2C running budgetcan) and Windows
# negative-caches a failed first attach by VID+PID+bcdDevice (F13): reusing
# either value would reproduce that trap rather than avoid it. The value
# below is a project-chosen identifier distinct from both forbidden values;
# it must be bumped whenever the descriptor set this device presents changes
# in a way a host might have cached against the old one.
# ---------------------------------------------------------------------------
VENDOR_ID = 0x1D50
PRODUCT_ID = 0x606F
USB_SPEC_VERSION = 0x0210
DEFAULT_DEVICE_VERSION = 0x0100

# ---------------------------------------------------------------------------
# Vendor interface (R3, F2). bInterfaceNumber 0, class/subclass/protocol all
# vendor-specific, matching the observed reference device. The Linux driver
# matches on interface 0 alone, so whatever gets composited in after this
# interface (the CDC REPL, at interfaces 1-2) cannot affect the match.
# ---------------------------------------------------------------------------
VENDOR_INTERFACE_CLASS = 0xFF
VENDOR_INTERFACE_SUBCLASS = 0xFF
VENDOR_INTERFACE_PROTOCOL = 0xFF

# F2/R3: the kernel driver hardcodes these exact addresses; they are not
# negotiable.
EP_BULK_IN = 0x81
EP_BULK_OUT = 0x02

# The largest gs_usb frame this device emits is 24 bytes, classic plus a
# hardware timestamp (F1); 32 is one packet with headroom for that rather
# than the 64 several class-compliant hosts default to elsewhere, and
# matches what the captured reference device (a BigTreeTech U2C running
# budgetcan) advertises on both bulk endpoints. Either size makes every
# frame a short packet, so the choice affects only how much of a
# maximum-size bulk transaction goes unused, not the framing itself.
BULK_MAX_PACKET_SIZE = 32

CONTROL_MAX_PACKET_SIZE0 = 64

# desc_strs indices used by the device descriptor below. Index 0 (the
# languages descriptor) is left absent from desc_strs so USBDevice falls
# back to its built-in English default.
STR_IDX_MANUFACTURER = 1
STR_IDX_PRODUCT = 2
STR_IDX_SERIAL = 3

# Generic placeholders: real identity strings are supplied by the caller.
# Never "LinkLayer Labs" or "CANtact Pro" (R5): those belong to specific
# other products.
DEFAULT_MANUFACTURER = "mpy-usb2can"
DEFAULT_PRODUCT = "gs_usb CAN adapter"
DEFAULT_SERIAL = "0"

# ---------------------------------------------------------------------------
# Standard descriptor type codes (USB 2.0 s9.4/Interface Association ECN).
# ---------------------------------------------------------------------------
_DESC_TYPE_DEVICE = 0x01
_DESC_TYPE_CONFIGURATION = 0x02
_DESC_TYPE_INTERFACE = 0x04
_DESC_TYPE_ENDPOINT = 0x05
_DESC_TYPE_INTERFACE_ASSOCIATION = 0x0B

_EP_ATTR_BULK = 0x02


# ---------------------------------------------------------------------------
# Device descriptor.
# ---------------------------------------------------------------------------
_DEVICE_DESC_FMT = "<BBHBBBBHHHBBBB"
DEVICE_DESC_SIZE = struct.calcsize(_DEVICE_DESC_FMT)  # 18


def build_device_descriptor(
    *,
    id_vendor: int = VENDOR_ID,
    id_product: int = PRODUCT_ID,
    bcd_usb: int = USB_SPEC_VERSION,
    bcd_device: int = DEFAULT_DEVICE_VERSION,
    device_class: int = 0,
    device_subclass: int = 0,
    device_protocol: int = 0,
    max_packet_size0: int = CONTROL_MAX_PACKET_SIZE0,
    i_manufacturer: int = STR_IDX_MANUFACTURER,
    i_product: int = STR_IDX_PRODUCT,
    i_serial_number: int = STR_IDX_SERIAL,
    num_configurations: int = 1,
) -> bytes:
    """
    Build a standard 18-byte device descriptor.

    Rejects bcd_device of 0x0000 or 0x0200 (R5, F13): see the module
    docstring's identity section for why those two specific values are a
    trap rather than an arbitrary style preference.
    """
    if bcd_device in (0x0000, 0x0200):
        raise ValueError("bcd_device must not be 0x0000 or 0x0200 (R5, F13)")
    return struct.pack(
        _DEVICE_DESC_FMT,
        DEVICE_DESC_SIZE,
        _DESC_TYPE_DEVICE,
        bcd_usb,
        device_class,
        device_subclass,
        device_protocol,
        max_packet_size0,
        id_vendor,
        id_product,
        bcd_device,
        i_manufacturer,
        i_product,
        i_serial_number,
        num_configurations,
    )


# ---------------------------------------------------------------------------
# BOS (Binary device Object Store) descriptor. bcdUSB 0x0210 obliges a device
# to answer GET_DESCRIPTOR(BOS) (USB 2.0 LPM ECN); a device that declares
# 0x0210 and then stalls that request fails enumeration on any host that
# checks, and on Windows triggers the VID+PID+bcdDevice negative-cache trap
# R5/F13 already guards bcd_device against. The USB 2.0 Extension device
# capability is the minimum content that makes the stall-free answer honest:
# it exists to carry the LPM support bit, set here to "not supported" since
# this device does none of the LPM negotiation the bit would promise. The
# Microsoft OS 2.0 platform capability descriptor, when one is added, is a
# second device capability appended to this same BOS, not a replacement
# for it.
# ---------------------------------------------------------------------------
_BOS_DESC_FMT = "<BBHB"
BOS_DESC_SIZE = struct.calcsize(_BOS_DESC_FMT)  # 5

_DESC_TYPE_BOS = 0x0F
_DESC_TYPE_DEVICE_CAPABILITY = 0x10
_DEV_CAP_TYPE_USB_2_0_EXTENSION = 0x02

_USB_2_0_EXTENSION_FMT = "<BBBI"
USB_2_0_EXTENSION_SIZE = struct.calcsize(_USB_2_0_EXTENSION_FMT)  # 7


def build_default_bos() -> bytes:
    """
    Build the BOS descriptor this device answers GET_DESCRIPTOR(BOS) with:
    the header plus a single USB 2.0 Extension capability advertising no LPM
    support (bmAttributes 0), the minimum content a bcdUSB 0x0210 device
    needs to answer that request honestly rather than stalling it.
    """
    dev_cap = struct.pack(
        _USB_2_0_EXTENSION_FMT,
        USB_2_0_EXTENSION_SIZE,
        _DESC_TYPE_DEVICE_CAPABILITY,
        _DEV_CAP_TYPE_USB_2_0_EXTENSION,
        0,  # bmAttributes: no bits set, LPM not supported.
    )
    header = struct.pack(
        _BOS_DESC_FMT,
        BOS_DESC_SIZE,
        _DESC_TYPE_BOS,
        BOS_DESC_SIZE + len(dev_cap),
        1,  # bNumDeviceCaps
    )
    return header + dev_cap


# ---------------------------------------------------------------------------
# Configuration, interface, endpoint and interface-association descriptors.
# Each helper takes named arguments for the fields that vary and computes
# bLength itself, so a caller never writes a raw byte offset.
# ---------------------------------------------------------------------------
_CONFIG_DESC_FMT = "<BBHBBBBB"
CONFIG_DESC_SIZE = struct.calcsize(_CONFIG_DESC_FMT)  # 9

# bmAttributes bit 7 is reserved and must always be set (USB 2.0 s9.6.3).
_CONFIG_ATTR_RESERVED = 1 << 7
CONFIG_ATTR_SELF_POWERED = 1 << 6
CONFIG_ATTR_REMOTE_WAKEUP = 1 << 5


def build_configuration_header(
    total_length: int,
    num_interfaces: int,
    *,
    configuration_value: int = 1,
    i_configuration: int = 0,
    attributes: int = 0,
    max_power_ma: int = 100,
) -> bytes:
    """Build the standard 9-byte configuration descriptor header."""
    return struct.pack(
        _CONFIG_DESC_FMT,
        CONFIG_DESC_SIZE,
        _DESC_TYPE_CONFIGURATION,
        total_length,
        num_interfaces,
        configuration_value,
        i_configuration,
        _CONFIG_ATTR_RESERVED | attributes,
        max_power_ma // 2,
    )


_INTERFACE_DESC_FMT = "<BBBBBBBBB"
INTERFACE_DESC_SIZE = struct.calcsize(_INTERFACE_DESC_FMT)  # 9


def build_interface_descriptor(
    interface_number: int,
    num_endpoints: int,
    *,
    interface_class: int,
    interface_subclass: int,
    interface_protocol: int,
    alternate_setting: int = 0,
    i_interface: int = 0,
) -> bytes:
    """Build the standard 9-byte interface descriptor."""
    return struct.pack(
        _INTERFACE_DESC_FMT,
        INTERFACE_DESC_SIZE,
        _DESC_TYPE_INTERFACE,
        interface_number,
        alternate_setting,
        num_endpoints,
        interface_class,
        interface_subclass,
        interface_protocol,
        i_interface,
    )


_ENDPOINT_DESC_FMT = "<BBBBHB"
ENDPOINT_DESC_SIZE = struct.calcsize(_ENDPOINT_DESC_FMT)  # 7


def build_endpoint_descriptor(
    b_endpoint_address: int,
    max_packet_size: int,
    attributes: int = _EP_ATTR_BULK,
    interval: int = 0,
) -> bytes:
    """Build the standard 7-byte endpoint descriptor. Defaults to bulk."""
    return struct.pack(
        _ENDPOINT_DESC_FMT,
        ENDPOINT_DESC_SIZE,
        _DESC_TYPE_ENDPOINT,
        b_endpoint_address,
        attributes,
        max_packet_size,
        interval,
    )


_IAD_DESC_FMT = "<BBBBBBBB"
IAD_DESC_SIZE = struct.calcsize(_IAD_DESC_FMT)  # 8


def build_interface_association_descriptor(
    first_interface: int,
    interface_count: int,
    function_class: int,
    function_subclass: int,
    function_protocol: int = 0,
    *,
    i_function: int = 0,
) -> bytes:
    """Build the 8-byte Interface Association Descriptor (IAD ECN)."""
    return struct.pack(
        _IAD_DESC_FMT,
        IAD_DESC_SIZE,
        _DESC_TYPE_INTERFACE_ASSOCIATION,
        first_interface,
        interface_count,
        function_class,
        function_subclass,
        function_protocol,
        i_function,
    )


class ConfigurationBuilder:
    """
    Assembles a USB configuration descriptor from interface blocks.

    Both bNumInterfaces and wTotalLength are derived from the blocks
    actually appended, never carried as separate state that a caller could
    forget to update. That is what keeps appending another interface after
    construction, such as a CDC REPL at interfaces 1-2, a matter of
    calling add_interface() or add_block() again
    before reading build(): no byte offset anywhere needs adjusting by
    hand.
    """

    def __init__(
        self,
        *,
        configuration_value: int = 1,
        i_configuration: int = 0,
        attributes: int = 0,
        max_power_ma: int = 100,
    ):
        self._configuration_value = configuration_value
        self._i_configuration = i_configuration
        self._attributes = attributes
        self._max_power_ma = max_power_ma
        self._blocks: list[bytes] = []
        self._next_interface_number = 0

    @property
    def next_interface_number(self) -> int:
        """The bInterfaceNumber the next add_interface() call will assign."""
        return self._next_interface_number

    def add_interface(
        self,
        interface_class: int,
        interface_subclass: int,
        interface_protocol: int,
        endpoints=(),
        *,
        i_interface: int = 0,
    ) -> int:
        """
        Append one interface descriptor plus its endpoint descriptors.

        endpoints is a sequence of tuples, each the positional arguments
        of build_endpoint_descriptor(): at minimum
        (b_endpoint_address, max_packet_size), optionally extended with
        attributes and interval to override the bulk default. Returns the
        assigned bInterfaceNumber.
        """
        interface_number = self._next_interface_number
        block = bytearray(
            build_interface_descriptor(
                interface_number,
                len(endpoints),
                interface_class=interface_class,
                interface_subclass=interface_subclass,
                interface_protocol=interface_protocol,
                i_interface=i_interface,
            )
        )
        for endpoint_args in endpoints:
            block += build_endpoint_descriptor(*endpoint_args)
        self._blocks.append(bytes(block))
        self._next_interface_number += 1
        return interface_number

    def add_interface_group(
        self,
        function_class: int,
        function_subclass: int,
        function_protocol: int,
        interfaces,
        *,
        i_function: int = 0,
    ) -> int:
        """
        Append an Interface Association Descriptor followed by each
        interface in interfaces, a sequence of
        (interface_class, interface_subclass, interface_protocol,
        endpoints) tuples in the shape add_interface() takes. Returns the
        first assigned bInterfaceNumber, matching the IAD's
        bFirstInterface.
        """
        first_interface_number = self._next_interface_number
        self._blocks.append(
            build_interface_association_descriptor(
                first_interface_number,
                len(interfaces),
                function_class,
                function_subclass,
                function_protocol,
                i_function=i_function,
            )
        )
        for interface_class, interface_subclass, interface_protocol, endpoints in interfaces:
            self.add_interface(interface_class, interface_subclass, interface_protocol, endpoints)
        return first_interface_number

    def add_block(self, data, num_interfaces: int = 1) -> int:
        """
        Append an already-encoded descriptor block (for example, one built
        by a driver library that emits its own bytes) and advance the
        interface number counter by num_interfaces. Returns the first
        interface number the block occupies, which the caller must have
        used when encoding it, typically read from
        next_interface_number beforehand.
        """
        first_interface_number = self._next_interface_number
        self._blocks.append(bytes(data))
        self._next_interface_number += num_interfaces
        return first_interface_number

    def build(self) -> bytes:
        """Assemble the complete configuration descriptor from the blocks appended so far."""
        body = b"".join(self._blocks)
        total_length = CONFIG_DESC_SIZE + len(body)
        header = build_configuration_header(
            total_length,
            self._next_interface_number,
            configuration_value=self._configuration_value,
            i_configuration=self._i_configuration,
            attributes=self._attributes,
            max_power_ma=self._max_power_ma,
        )
        return header + body


class GsUsbUsbDevice:
    """
    Wires machine.USBDevice for the gs_usb vendor interface.

    Builds the device descriptor and a configuration descriptor whose first
    interface is the vendor bulk pair the gs_usb protocol runs over, at
    bInterfaceNumber 0 (R3). The configuration builder is exposed as
    ``builder`` rather than consumed and discarded, so a later stage can
    append the CDC REPL (or anything else) before ``desc_cfg`` is read;
    ``desc_cfg`` recomputes from ``builder`` on every access rather than
    caching a snapshot taken at construction time.

    The callbacks handed to USBDevice.config() are thin dispatchers: each
    forwards its call unchanged to a handler supplied by the caller, or
    falls back to stalling (control_xfer_cb, xfer_cb) or doing nothing
    (open_itf_cb, reset_cb) when no handler was given. No gs_usb request
    semantics live here; that belongs to whatever module supplies the
    handlers.

    desc_bos defaults to build_default_bos(): bcdUSB 0x0210 obliges this
    device to answer GET_DESCRIPTOR(BOS) (see that function's docstring), so
    stalling it is not a safe default the way an unimplemented optional
    request elsewhere might be. A caller that passes an explicit desc_bos,
    to append an MS OS 2.0 platform capability to the same BOS, overrides
    this default entirely rather than extending it; building that
    combined descriptor is the caller's job, not this module's.
    """

    def __init__(
        self,
        *,
        bcd_device: int = DEFAULT_DEVICE_VERSION,
        manufacturer: str = DEFAULT_MANUFACTURER,
        product: str = DEFAULT_PRODUCT,
        serial: str = DEFAULT_SERIAL,
        max_power_ma: int = 100,
        desc_bos=None,
        open_itf_handler=None,
        control_xfer_handler=None,
        xfer_handler=None,
        reset_handler=None,
    ):
        self.desc_dev = build_device_descriptor(bcd_device=bcd_device)
        self.desc_bos = build_default_bos() if desc_bos is None else desc_bos
        self.builder = ConfigurationBuilder(max_power_ma=max_power_ma)
        self.vendor_interface_number = self.builder.add_interface(
            VENDOR_INTERFACE_CLASS,
            VENDOR_INTERFACE_SUBCLASS,
            VENDOR_INTERFACE_PROTOCOL,
            endpoints=[
                (EP_BULK_IN, BULK_MAX_PACKET_SIZE),
                (EP_BULK_OUT, BULK_MAX_PACKET_SIZE),
            ],
        )
        self.desc_strs = {
            STR_IDX_MANUFACTURER: manufacturer,
            STR_IDX_PRODUCT: product,
            STR_IDX_SERIAL: serial,
        }
        self._open_itf_handler = open_itf_handler
        self._control_xfer_handler = control_xfer_handler
        self._xfer_handler = xfer_handler
        self._reset_handler = reset_handler

    @property
    def desc_cfg(self) -> bytes:
        """The full configuration descriptor, rebuilt from ``builder`` on every read."""
        return self.builder.build()

    def open_itf_cb(self, itf_desc):
        """Dispatched on Set Configuration; forwards to the injected handler, if any."""
        if self._open_itf_handler is not None:
            self._open_itf_handler(itf_desc)

    def control_xfer_cb(self, stage, request):
        """Dispatched for each control transfer stage; stalls when no handler is injected."""
        if self._control_xfer_handler is not None:
            return self._control_xfer_handler(stage, request)
        return False

    def xfer_cb(self, ep, result, xferred_bytes):
        """Dispatched when a submitted bulk transfer completes; no-ops without a handler."""
        if self._xfer_handler is not None:
            return self._xfer_handler(ep, result, xferred_bytes)
        return False

    def reset_cb(self):
        """Dispatched on a USB bus reset; forwards to the injected handler, if any."""
        if self._reset_handler is not None:
            self._reset_handler()

    def apply(self, usb_device=None):
        """
        Configure usb_device (machine.USBDevice()'s singleton, by default)
        with this device's descriptors and dispatch callbacks.

        Deactivates before reconfiguring, since USBDevice.config() and the
        builtin_driver assignment both require that, and leaves activation
        to the caller: whether to call active(True) immediately, or first
        let a later stage finish appending interfaces to ``builder``, is a
        sequencing decision that belongs to whoever calls apply(), not to
        this method. Returns usb_device for convenience.
        """
        if usb_device is None:
            import machine

            usb_device = machine.USBDevice()  # type: ignore[attr-defined]
        usb_device.active(False)
        usb_device.builtin_driver = usb_device.BUILTIN_NONE
        usb_device.config(
            desc_dev=self.desc_dev,
            desc_cfg=self.desc_cfg,
            desc_strs=self.desc_strs,
            open_itf_cb=self.open_itf_cb,
            reset_cb=self.reset_cb,
            control_xfer_cb=self.control_xfer_cb,
            # The handler directly where there is one, rather than this
            # object's forwarding method: a bulk completion runs once per
            # frame delivered and a hop of its own is not free at that rate.
            xfer_cb=self._xfer_handler if self._xfer_handler is not None else self.xfer_cb,
            desc_bos=self.desc_bos,
        )
        return usb_device
