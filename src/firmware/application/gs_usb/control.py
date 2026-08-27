"""
gs_usb control plane: the vendor control-transfer handler behind
machine.USBDevice's control_xfer_cb, driving a CanCore instance.

Every gs_usb control request is bmRequestType 0x41 (host-to-device, vendor,
interface) or 0xC1 (device-to-host, vendor, interface); TinyUSB stalls
anything else before it reaches Python. wValue carries either the literal
DEVICE_SCOPE_WVALUE (HOST_FORMAT, DEVICE_CONFIG, both device-scoped) or a
channel index (every other implemented request); wIndex carries the vendor
interface number for the two device-scoped requests and is otherwise 0.
This module enforces both conventions per request and never lets a
channel index past the advertised channel count reach CanCore.

Feature word (BT_CONST.feature)
--------------------------------
FEATURE_LISTEN_ONLY and FEATURE_LOOP_BACK are advertised: both map directly
onto machine.CAN's own MODE_SILENT and MODE_LOOPBACK, so answering them
costs nothing beyond the mapping in _can_mode_for_flags(). Nothing else is
set:

- FEATURE_HW_TIMESTAMP is withheld deliberately. Setting it would commit
  every device-to-host frame to a trailing timestamp_us field; gs_usb.device
  sizes its output queue at protocol.FRAME_SIZE_CLASSIC (20 bytes) and calls
  pack_classic_frame_into with timestamp_us defaulted to None at both call
  sites, and no timestamp source is wired to either. Setting the bit without
  first resizing that queue to FRAME_SIZE_CLASSIC_TS and supplying a real
  timestamp_us at both sites would make the host expect 24-byte reads
  against 20-byte buffers.
- FEATURE_TERMINATION is withheld because this board has no transceiver,
  let alone a switchable termination resistor, for SET/GET_TERMINATION to
  report on.
- FEATURE_FD, FEATURE_IDENTIFY, FEATURE_USER_ID,
  FEATURE_PAD_PKTS_TO_MAX_PKT_SIZE, FEATURE_BERR_REPORTING and
  FEATURE_GET_STATE are excluded per scope (R6) regardless of cost.

Not advertising a feature means never answering the request(s) it gates:
BREQ_TIMESTAMP, BREQ_IDENTIFY, BREQ_GET_USER_ID, BREQ_SET_USER_ID,
BREQ_DATA_BITTIMING, BREQ_BT_CONST_EXT, BREQ_SET_TERMINATION,
BREQ_GET_TERMINATION and BREQ_GET_STATE all stall, the same as a request
number this module has never heard of. BREQ_BERR is never issued by any
host to begin with (R6).

Bit timing
----------
gs_usb's BITTIMING payload (prop_seg, phase_seg1, phase_seg2, sjw, brp) and
machine.CAN's own constructor (bitrate, tseg1, tseg2, sjw, with no brp
parameter of its own) describe the same hardware in different coordinates.
_decode_bittiming() converts one to the other; see its docstring for the
arithmetic. BT_CONST's limits (FCLK_CAN and the TSEG/SJW/BRP ranges below)
are this board's own H5 FDCAN register constraints, not a table copied
from another device (R7): FCLK_CAN is derived in the comment above its
definition, and the TSEG/SJW/BRP ranges are extmod/machine_can.c's
structural defaults together with the FDCAN-specific BRP override in
ports/stm32/machine_can.c, neither of which changes per bitrate.

BITTIMING is accepted whenever the channel is stopped, before or after a
prior BITTIMING or MODE STOP, matching that some hosts send it once at
open (Linux master) and others resend it around every link up (F12,
observed on v6.17). MODE START combines the most recently accepted timing
with its own mode flags and applies both to CanCore in one call, since
machine.CAN configures bitrate and mode together and cannot be retimed
while running.

Response length (R2)
---------------------
An IN response is always exactly wLength bytes. A host asking for fewer
bytes than the underlying struct gets a truncated prefix, its own choice
to make (some gs_usb hosts probe a candidate struct size this way); one
asking for more gets the struct zero-padded to length, since the Linux
gs_usb driver reads every control IN through usb_control_msg_recv(), which
reports -EREMOTEIO unless the device returns exactly wLength bytes, so a
short reply is not a safe default. Padding stops at _MAX_RESPONSE_SIZE:
wLength is a host-controlled u16, and no struct this module packs is
anywhere near that large, so honouring an arbitrary value would let a
single control read force an allocation sized by the host rather than by
the response it is actually reading. A wLength past that bound stalls
instead (see `_in_response`).

Exceptions
----------
control_xfer_cb() catches every exception raised while handling a stage
and stalls the transfer (returns False) rather than propagating it: a
malformed or out-of-sequence request that this module cannot make sense
of is exactly what STALL exists for, and a Python exception must never
reach back into the C callback trampoline.
"""

import logging
import struct

from gs_usb import protocol

log = logging.getLogger("gs_usb.control")

# ---------------------------------------------------------------------------
# control_xfer_cb stage numbers (machine.USBDevice.rst).
# ---------------------------------------------------------------------------
_STAGE_SETUP = 1
_STAGE_DATA = 2
_STAGE_ACK = 3

# bmRequestType for every gs_usb control request: vendor type, interface
# recipient, direction bit clear (OUT) or set (IN). Nothing else reaches
# this handler (TinyUSB stalls other recipients/types before Python sees
# them), but every handler below still checks it rather than assuming.
_BM_REQUEST_TYPE_OUT = 0x41
_BM_REQUEST_TYPE_IN = 0xC1

_SETUP_FMT = "<BBHHH"

# ---------------------------------------------------------------------------
# Feature word. See the module docstring for what each bit costs and why
# the rest are withheld.
# ---------------------------------------------------------------------------
FEATURE = protocol.FEATURE_LISTEN_ONLY | protocol.FEATURE_LOOP_BACK

# ---------------------------------------------------------------------------
# BT_CONST limits: structural facts about this board's H5 FDCAN, not a
# bitrate table (R7).
#
# FCLK_CAN: RCC_CCIPR5.FDCANSEL resets to hse_ck and nothing in this tree's
# clock setup writes it for H5, so the FDCAN kernel clock is this
# board's HSE, 8_000_000 Hz, independent of the reference device's
# 64 MHz fclk_can, which is not reused anywhere in this module.
#
# TSEG1/TSEG2/SJW: extmod/machine_can.c's CAN_TSEG1_MIN/MAX,
# CAN_TSEG2_MIN/MAX and CAN_SJW_MIN/MAX, unmodified by the stm32 port for
# the nominal (arbitration-phase) bit timing this device uses.
#
# BRP: ports/stm32/machine_can.c's FDCAN-specific override
# (CAN_BRP_MIN/MAX under MICROPY_HW_ENABLE_FDCAN), wider than the classic
# bxCAN default because FDCAN's prescaler register field is wider; the
# increment is 1 because nothing in that code restricts brp to a subset of
# its range.
# ---------------------------------------------------------------------------
FCLK_CAN = 8_000_000
TSEG1_MIN, TSEG1_MAX = 1, 16
TSEG2_MIN, TSEG2_MAX = 1, 8
SJW_MIN, SJW_MAX = 1, 4
BRP_MIN, BRP_MAX, BRP_INC = 1, 512, 1


# The largest struct this module ever packs into an IN response is
# BT_CONST; see _in_response's docstring for why that bounds every wLength
# this module will honour, not just BT_CONST's own.
_MAX_RESPONSE_SIZE = protocol.BT_CONST_SIZE


def _in_response(data, w_length):
    """Trim or zero-pad data to exactly w_length bytes (R2); see the module
    docstring for which direction each mismatch takes and why. Returns None
    when w_length exceeds _MAX_RESPONSE_SIZE, for the caller to stall
    instead of padding: wLength is an untrusted host-supplied u16, and no
    struct this module packs is close to 0xFFFF bytes, so treating every
    value up to that as a legitimate padding request would let one control
    read force an allocation sized by the host rather than by the response
    actually being read."""
    if w_length <= len(data):
        return data[:w_length]
    if w_length > _MAX_RESPONSE_SIZE:
        return None
    return data + bytes(w_length - len(data))


class GsUsbControl:
    """
    Answers gs_usb control requests for a device with `num_channels`
    channels, driving `can_core` (a CanCore instance).

    `num_channels`, left unset, is `can_core.num_channels`: this class is
    not itself a second source of truth for how many channels exist, and
    an explicit value that disagrees with `can_core`'s own count raises
    immediately rather than silently answering DEVICE_CONFIG or BT_CONST
    with a channel count CanCore does not actually have (F25).

    The machine.CAN-shaped type whose MODE_* constants translate gs_usb
    mode flags is `can_core.resolve_can_class()`, not a second class
    resolved independently here: `can_core` and this class must agree on
    it, and asking `can_core` for the class it already resolved (or will
    resolve to machine.CAN on first use) is the only way that is always
    true (F26).

    `interface_number` is the vendor interface's bInterfaceNumber, which
    HOST_FORMAT and DEVICE_CONFIG carry in wIndex; the caller assigns it
    (GsUsbUsbDevice.vendor_interface_number) since only the USB device
    layer knows where the vendor interface landed in the configuration.
    """

    def __init__(
        self,
        can_core,
        num_channels=None,
        sw_version=2,
        hw_version=1,
        interface_number=0,
    ):
        self._can = can_core
        if num_channels is None:
            num_channels = can_core.num_channels
        elif num_channels != can_core.num_channels:
            raise ValueError(
                "num_channels %d does not match can_core's %d"
                % (num_channels, can_core.num_channels)
            )
        self._num_channels = num_channels
        self._sw_version = sw_version
        self._hw_version = hw_version
        self._interface_number = interface_number
        self._started = [False] * num_channels
        # Each entry is None (no bit timing accepted yet) or
        # (tseg1, tseg2, sjw, bitrate), the coordinates machine.CAN.configure
        # wants, decoded once by _decode_bittiming and reapplied verbatim at
        # every MODE START until a new BITTIMING replaces it.
        self._bittiming: list[tuple[int, int, int, int] | None] = [None] * num_channels
        # At most one OUT request has a data stage outstanding at a time
        # (single control endpoint); set at SETUP, consumed and cleared at
        # ACK. None when the pending request is an IN transfer, which needs
        # no action once the response has been handed over at SETUP.
        self._pending_ack = None

        # Called with a channel index once that channel is stopped. The data
        # plane holds per-channel state that only that channel's
        # on_tx_complete would otherwise resolve, and stopping it guarantees
        # none will fire again, so it has to be told. Set by whoever composes
        # the two; None leaves behaviour unchanged.
        self.on_channel_stopped = None

    def _valid_channel(self, channel):
        return 0 <= channel < self._num_channels

    # -- machine.USBDevice runtime callback --------------------------------

    def control_xfer_cb(self, stage, request):
        """
        One call per control-transfer stage. See the module docstring for
        the exception-boundary contract; every other outcome is decided by
        _handle_setup / _handle_ack below.
        """
        try:
            _bm_request_type, b_request, w_value, w_index, w_length = struct.unpack(
                _SETUP_FMT, request
            )
            if stage == _STAGE_SETUP:
                return self._handle_setup(_bm_request_type, b_request, w_value, w_index, w_length)
            if stage == _STAGE_ACK:
                return self._handle_ack()
            return True  # DATA stage: the SETUP handler already decided.
        except Exception as exc:  # noqa: BLE001 - the boundary of last resort
            log.warning("control transfer stalled: %r", exc)
            self._pending_ack = None
            return False

    def _handle_setup(self, bm_request_type, b_request, w_value, w_index, w_length):
        # A new SETUP always invalidates whatever the previous transfer
        # left armed: a transfer whose SETUP is accepted but that never
        # reaches its own ACK stage (host timeout, cable pull, bus reset
        # handled elsewhere) would otherwise leave this closure to fire
        # over a still-zeroed buffer on some later, unrelated transfer's
        # ACK stage.
        self._pending_ack = None
        handler = _SETUP_HANDLERS.get(b_request)
        if handler is None:
            return False
        return handler(self, bm_request_type, w_value, w_index, w_length)

    def _handle_ack(self):
        pending = self._pending_ack
        self._pending_ack = None
        if pending is not None:
            pending()
        return True

    # -- per-request SETUP handlers -----------------------------------------
    #
    # Each returns False to stall, a response object for an IN transfer
    # (built with _in_response, R2), or a fresh writable buffer for an OUT
    # transfer with a data stage, having also set self._pending_ack to the
    # closure that will apply it at ACK.

    def _setup_host_format(self, bm_request_type, w_value, w_index, w_length):
        if bm_request_type != _BM_REQUEST_TYPE_OUT:
            return False
        if w_value != protocol.DEVICE_SCOPE_WVALUE or w_index != self._interface_number:
            return False
        if w_length != protocol.HOST_CONFIG_SIZE:
            return False
        # byte_order is accepted, not inspected: this device only ever
        # speaks little-endian, so there is nothing to branch on.
        return bytearray(w_length)

    def _setup_device_config(self, bm_request_type, w_value, w_index, w_length):
        if bm_request_type != _BM_REQUEST_TYPE_IN:
            return False
        if w_value != protocol.DEVICE_SCOPE_WVALUE or w_index != self._interface_number:
            return False
        data = protocol.pack_device_config(
            self._num_channels - 1, self._sw_version, self._hw_version
        )
        response = _in_response(data, w_length)
        return False if response is None else response

    def _setup_bt_const(self, bm_request_type, w_value, w_index, w_length):
        if bm_request_type != _BM_REQUEST_TYPE_IN:
            return False
        if w_index != 0 or not self._valid_channel(w_value):
            return False
        data = protocol.pack_bt_const(
            FEATURE,
            FCLK_CAN,
            TSEG1_MIN,
            TSEG1_MAX,
            TSEG2_MIN,
            TSEG2_MAX,
            SJW_MAX,
            BRP_MIN,
            BRP_MAX,
            BRP_INC,
        )
        response = _in_response(data, w_length)
        return False if response is None else response

    def _setup_bittiming(self, bm_request_type, w_value, w_index, w_length):
        if bm_request_type != _BM_REQUEST_TYPE_OUT:
            return False
        if w_index != 0 or not self._valid_channel(w_value):
            return False
        if w_length != protocol.BITTIMING_SIZE:
            return False
        channel = w_value
        buf = bytearray(w_length)
        self._pending_ack = lambda: self._apply_bittiming(channel, buf)
        return buf

    def _setup_mode(self, bm_request_type, w_value, w_index, w_length):
        if bm_request_type != _BM_REQUEST_TYPE_OUT:
            return False
        if w_index != 0 or not self._valid_channel(w_value):
            return False
        if w_length != protocol.DEVICE_MODE_SIZE:
            return False
        channel = w_value
        buf = bytearray(w_length)
        self._pending_ack = lambda: self._apply_mode(channel, buf)
        return buf

    # -- ACK-stage application ----------------------------------------------

    def _apply_bittiming(self, channel, buf):
        """
        Validate and store one channel's bit timing; never touches CanCore
        (F12: BITTIMING may arrive at any time while the channel is
        stopped, independent of MODE, and CanCore itself refuses to be
        reconfigured while started). Raises if the channel is running or
        any field is outside this board's own register limits; the caller
        (control_xfer_cb) turns that into a stall.
        """
        if self._started[channel]:
            raise RuntimeError("channel %d is running; stop before changing bit timing" % channel)
        tseg1, tseg2, sjw, bitrate = _decode_bittiming(buf)
        self._bittiming[channel] = (tseg1, tseg2, sjw, bitrate)

    def _apply_mode(self, channel, buf):
        mode, flags = protocol.unpack_device_mode(buf)
        if mode == protocol.CAN_MODE_START:
            self._start_channel(channel, flags)
        elif mode == protocol.CAN_MODE_RESET:
            self._stop_channel(channel)
        else:
            raise ValueError("mode %d" % mode)

    def _start_channel(self, channel, flags):
        if self._started[channel]:
            return  # MODE START is idempotent, not a renegotiation.
        timing = self._bittiming[channel]
        if timing is None:
            raise RuntimeError("channel %d has no bit timing configured" % channel)
        tseg1, tseg2, sjw, bitrate = timing
        mode = _can_mode_for_flags(self._can.resolve_can_class(), flags)
        self._can.configure(channel, bitrate, mode=mode, sjw=sjw, tseg1=tseg1, tseg2=tseg2)
        self._can.start(channel)
        self._started[channel] = True

    def _stop_channel(self, channel):
        self._can.stop(channel)  # a no-op if the channel was never started
        self._started[channel] = False
        if self.on_channel_stopped is not None:
            self.on_channel_stopped(channel)

    def reset(self):
        """
        Recover from a USB bus reset (F1, F13): every channel this control
        plane believes is running is stopped through the same path MODE
        RESET uses. Re-enumeration after a bus reset means the host is
        going to reconfigure every channel from scratch and carries no
        memory of what was running before the reset, so there is nothing
        for this device to preserve by leaving a channel running underneath
        a host that no longer knows it asked for that.

        Bit timing already accepted is left alone: BITTIMING is accepted
        independently of MODE (see the module docstring), and a host that
        already sent it once for a channel before this reset is not
        guaranteed to resend it identically after; discarding it here would
        make MODE START fail with "no bit timing configured" for a channel
        the host reasonably believes is already set up.

        Any control transfer's ACK-stage closure still pending across the
        reset is also discarded: the DATA stage that would complete it, or
        the ACK stage itself, is exactly what a bus reset interrupts.
        """
        self._pending_ack = None
        for channel in range(self._num_channels):
            self._stop_channel(channel)


def _can_mode_for_flags(can_class, flags):
    """
    Map gs_usb mode flags onto a machine.CAN MODE_* constant.

    Only the bits FEATURE actually advertises are honoured: a compliant
    host never sets a bit this device did not offer in BT_CONST.feature,
    and one that did anyway (or a stale flag left over from a host talking
    to a different feature set) is silently ignored rather than rejected,
    since every other bit in gs_can_mode_flags names a request this module
    does not implement (R6) and has no CAN-core equivalent to apply.
    """
    loop_back = bool(flags & protocol.MODE_LOOP_BACK)
    listen_only = bool(flags & protocol.MODE_LISTEN_ONLY)
    if loop_back and listen_only:
        return can_class.MODE_SILENT_LOOPBACK
    if loop_back:
        return can_class.MODE_LOOPBACK
    if listen_only:
        return can_class.MODE_SILENT
    return can_class.MODE_NORMAL


def _decode_bittiming(buf):
    """
    Convert a gs_device_bittiming payload into the (tseg1, tseg2, sjw,
    bitrate) machine.CAN.configure() wants, validated against this board's
    own register limits (raises ValueError outside them).

    gs_usb's tseg1 is the Linux CAN core's split of the propagation
    segment: tseg1 = prop_seg + phase_seg1, tseg2 = phase_seg2. machine.CAN
    takes the same two combined segments directly, so that part is a sum,
    not a conversion.

    machine.CAN has no brp parameter of its own: extmod/machine_can.c's
    calculate_brp(), given explicit tseg1/tseg2, searches brp downward from
    its maximum for the value that minimizes the error between the
    resulting bitrate and the bitrate it is asked for. Handing it the
    exact bitrate the host's own brp implies makes that search exact
    rather than approximate:

        bit_time_tq = 1 (sync) + tseg1 + tseg2
        bitrate = fclk_can / (brp * bit_time_tq)

    which is the inverse of how a bxCAN/FDCAN prescaler derives its bit
    rate from the kernel clock in the first place. Passing this bitrate
    together with the same tseg1/tseg2/sjw back into configure() therefore
    reproduces the host's own brp, against this board's FCLK_CAN rather
    than whatever clock the host computed brp against originally.
    """
    prop_seg, phase_seg1, phase_seg2, sjw, brp = protocol.unpack_bittiming(buf)
    tseg1 = prop_seg + phase_seg1
    tseg2 = phase_seg2

    if not (TSEG1_MIN <= tseg1 <= TSEG1_MAX):
        raise ValueError("tseg1 %d out of range" % tseg1)
    if not (TSEG2_MIN <= tseg2 <= TSEG2_MAX):
        raise ValueError("tseg2 %d out of range" % tseg2)
    if not (SJW_MIN <= sjw <= SJW_MAX):
        raise ValueError("sjw %d out of range" % sjw)
    if not (BRP_MIN <= brp <= BRP_MAX):
        raise ValueError("brp %d out of range" % brp)

    bit_time_tq = 1 + tseg1 + tseg2
    bitrate = FCLK_CAN // (brp * bit_time_tq)
    if bitrate <= 0:
        raise ValueError("brp %d too large for fclk_can" % brp)
    return tseg1, tseg2, sjw, bitrate


# bRequest -> unbound SETUP handler. Every BREQ this module does not list
# here stalls: BREQ_BERR, BREQ_TIMESTAMP, BREQ_IDENTIFY, BREQ_GET_USER_ID,
# BREQ_SET_USER_ID, BREQ_DATA_BITTIMING, BREQ_BT_CONST_EXT,
# BREQ_SET_TERMINATION, BREQ_GET_TERMINATION and BREQ_GET_STATE all gate on
# feature bits FEATURE does not set (see the module docstring), and any
# bRequest this module has never heard of stalls the same way.
_SETUP_HANDLERS = {
    protocol.BREQ_HOST_FORMAT: GsUsbControl._setup_host_format,
    protocol.BREQ_BITTIMING: GsUsbControl._setup_bittiming,
    protocol.BREQ_MODE: GsUsbControl._setup_mode,
    protocol.BREQ_BT_CONST: GsUsbControl._setup_bt_const,
    protocol.BREQ_DEVICE_CONFIG: GsUsbControl._setup_device_config,
}
