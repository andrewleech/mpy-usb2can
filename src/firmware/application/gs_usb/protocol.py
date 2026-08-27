"""
gs_usb wire protocol: request numbers, mode/feature bits, and codecs for the
classic gs_host_frame and the control-transfer payload structs.

Pure computation: no USB, no CAN hardware, no asyncio. Every payload here is
little-endian and unpadded, matching the wire, not the host's C struct
layout rules.

struct, not uctypes, is the encoder. uctypes describes a fixed layout over
existing memory (as version.py does for the mboot footer); the frame codec
here instead builds several distinct sizes (20/24 bytes) from field lists
supplied by the caller, which is exactly struct.pack_into's job. Format
strings are module-level constants compiled once at import, so the hot path
never rebuilds one.
"""
import struct

# ---------------------------------------------------------------------------
# Control transfer request numbers (bRequest), enum gs_usb_breq.
# ---------------------------------------------------------------------------
BREQ_HOST_FORMAT = 0
BREQ_BITTIMING = 1
BREQ_MODE = 2
BREQ_BERR = 3  # never issued by the host; not implemented (R6)
BREQ_BT_CONST = 4
BREQ_DEVICE_CONFIG = 5
BREQ_TIMESTAMP = 6
BREQ_IDENTIFY = 7
BREQ_GET_USER_ID = 8  # never issued by the host; not implemented (R6)
BREQ_SET_USER_ID = 9  # never issued by the host; not implemented (R6)
BREQ_DATA_BITTIMING = 10
BREQ_BT_CONST_EXT = 11
BREQ_SET_TERMINATION = 12
BREQ_GET_TERMINATION = 13
BREQ_GET_STATE = 14

# HOST_FORMAT and DEVICE_CONFIG are device-scoped, not per channel, and carry
# this literal value in wValue instead of a channel index.
DEVICE_SCOPE_WVALUE = 1

# ---------------------------------------------------------------------------
# gs_can_mode: the "mode" field of the MODE request payload.
# ---------------------------------------------------------------------------
CAN_MODE_RESET = 0
CAN_MODE_START = 1

# ---------------------------------------------------------------------------
# GS_CAN_MODE_*: the "flags" field of the MODE request payload. Bit positions
# mirror the feature bits below one for one.
# ---------------------------------------------------------------------------
MODE_NORMAL = 0
MODE_LISTEN_ONLY = 1 << 0
MODE_LOOP_BACK = 1 << 1
MODE_TRIPLE_SAMPLE = 1 << 2
MODE_ONE_SHOT = 1 << 3
MODE_HW_TIMESTAMP = 1 << 4
MODE_PAD_PKTS_TO_MAX_PKT_SIZE = 1 << 7  # never set by the host; dead (R6)
MODE_FD = 1 << 8
MODE_BERR_REPORTING = 1 << 12

# ---------------------------------------------------------------------------
# GS_CAN_FEATURE_*: advertised in gs_device_bt_const.feature. Setting a bit
# is a promise to answer the request(s) it gates; see the wire table in the
# research report s4.5 before adding one.
# ---------------------------------------------------------------------------
FEATURE_LISTEN_ONLY = 1 << 0
FEATURE_LOOP_BACK = 1 << 1
FEATURE_TRIPLE_SAMPLE = 1 << 2
FEATURE_ONE_SHOT = 1 << 3
FEATURE_HW_TIMESTAMP = 1 << 4
FEATURE_IDENTIFY = 1 << 5
FEATURE_USER_ID = 1 << 6
FEATURE_PAD_PKTS_TO_MAX_PKT_SIZE = 1 << 7
FEATURE_FD = 1 << 8
FEATURE_REQ_USB_QUIRK_LPC546XX = 1 << 9
FEATURE_BT_CONST_EXT = 1 << 10
FEATURE_TERMINATION = 1 << 11
FEATURE_BERR_REPORTING = 1 << 12
FEATURE_GET_STATE = 1 << 13
FEATURE_MASK = (1 << 14) - 1

# ---------------------------------------------------------------------------
# CAN id flags, packed into the top three bits of a wire can_id. The
# identifier itself is 29 bits (EFF) or 11 bits (SFF/RTR) below that.
# ---------------------------------------------------------------------------
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF

# ---------------------------------------------------------------------------
# gs_host_frame.flags. Bits 4..7 are unallocated and unread by the host.
# ---------------------------------------------------------------------------
CAN_FLAG_OVERFLOW = 1 << 0
CAN_FLAG_FD = 1 << 1
CAN_FLAG_BRS = 1 << 2
CAN_FLAG_ESI = 1 << 3

# ---------------------------------------------------------------------------
# gs_host_frame.echo_id: this value marks a received frame rather than an
# echo of a TX slot. Echo slots themselves are 0..GS_MAX_TX_URBS-1 (R12).
# ---------------------------------------------------------------------------
ECHO_ID_RX = 0xFFFFFFFF

# gs_host_config.byte_order: the value the host sends on HOST_FORMAT. A
# little-endian-only device does not need to inspect it, only ACK the OUT
# transfer.
HOST_FORMAT_MAGIC = 0x0000BEEF

# gs_device_termination_state.state values, shared by GET_TERMINATION and
# SET_TERMINATION.
TERMINATION_OFF = 0
TERMINATION_ON = 1


def split_can_id(can_id: int):
    """
    Split a wire can_id into (ident, eff, rtr, err).

    ident is masked to 29 bits when eff is set, 11 bits otherwise, so a
    caller never has to mask it again before comparing or filtering on it.
    """
    eff = bool(can_id & CAN_EFF_FLAG)
    rtr = bool(can_id & CAN_RTR_FLAG)
    err = bool(can_id & CAN_ERR_FLAG)
    ident = can_id & (CAN_EFF_MASK if eff else CAN_SFF_MASK)
    return ident, eff, rtr, err


def combine_can_id(ident: int, eff: bool = False, rtr: bool = False, err: bool = False) -> int:
    """
    Combine an identifier and flags into a wire can_id.

    ident is masked to 29 bits when eff is set, 11 bits otherwise, so an
    out-of-range identifier is truncated rather than bleeding into the flag
    bits above it.
    """
    can_id = ident & (CAN_EFF_MASK if eff else CAN_SFF_MASK)
    if eff:
        can_id |= CAN_EFF_FLAG
    if rtr:
        can_id |= CAN_RTR_FLAG
    if err:
        can_id |= CAN_ERR_FLAG
    return can_id


# ---------------------------------------------------------------------------
# DLC handling.
#
# Classic CAN has no payload longer than 8 bytes, so any raw dlc above 8
# saturates to 8 rather than mapping to a longer length (that table only
# exists for CAN FD, which this module does not implement). The reverse
# direction, building a dlc from a payload the CAN controller already gave
# us, is exact because that payload is never longer than 8 bytes to start
# with.
# ---------------------------------------------------------------------------
CLASSIC_MAX_DLEN = 8


def dlc_to_len(dlc: int) -> int:
    """Map a raw classic CAN dlc (0..15) to a payload length in bytes."""
    return dlc if dlc <= CLASSIC_MAX_DLEN else CLASSIC_MAX_DLEN


def len_to_dlc(length: int) -> int:
    """Map a payload length in bytes (0..8) to a raw classic CAN dlc."""
    return length


# ---------------------------------------------------------------------------
# Classic gs_host_frame codec.
#
# Header layout, 12 bytes: echo_id (u32), can_id (__le32), can_dlc (u8),
# channel (u8), flags (u8), reserved (u8). echo_id is nominally host byte
# order on the wire, but every host this device targets is little-endian,
# so it is packed and unpacked as __le32 along with everything else.
#
# The data field is always the full 8 bytes on the wire regardless of dlc;
# unused trailing bytes are zero. Host-to-device transfers are exactly 20
# bytes (header + data) and never carry a timestamp: the host never sends
# one. Device-to-host transfers are 20 or 24 bytes depending on whether
# hardware timestamps were negotiated (MODE_HW_TIMESTAMP); when present the
# timestamp sits immediately after the 8-byte data field, not after the
# frame's actual dlc-derived length.
# ---------------------------------------------------------------------------
_FRAME_HEADER_FMT = "<IIBBBB"
FRAME_HEADER_SIZE = struct.calcsize(_FRAME_HEADER_FMT)  # 12
CLASSIC_DATA_SIZE = 8
FRAME_SIZE_CLASSIC = FRAME_HEADER_SIZE + CLASSIC_DATA_SIZE  # 20
FRAME_SIZE_CLASSIC_TS = FRAME_SIZE_CLASSIC + 4  # 24

# Zero padding for short payloads, preallocated so that padding a frame copies
# from an existing buffer instead of building `bytes(n)` per call.
_ZERO_PAD = bytes(CLASSIC_DATA_SIZE)
_ZERO_PAD_VIEWS = [memoryview(_ZERO_PAD)[0:n] for n in range(CLASSIC_DATA_SIZE + 1)]


_TIMESTAMP_FMT = "<I"
# The header's first 4 bytes (echo_id); pack_classic_frame_into writes the
# can_id field that follows it byte by byte instead, see that function's
# docstring for why.
_ECHO_ID_FMT = "<I"


def classic_frame_size(with_timestamp: bool) -> int:
    """Wire size of a classic frame, with or without the trailing timestamp."""
    return FRAME_SIZE_CLASSIC_TS if with_timestamp else FRAME_SIZE_CLASSIC


def _u32le(buf, offset: int) -> int:
    return buf[offset] | (buf[offset + 1] << 8) | (buf[offset + 2] << 16) | (buf[offset + 3] << 24)


def pack_classic_frame_into(
    buf,
    offset: int,
    echo_id: int,
    ident: int,
    can_dlc: int,
    channel: int,
    flags: int,
    data,
    *,
    eff: bool = False,
    rtr: bool = False,
    err: bool = False,
    timestamp_us=None,
) -> int:
    """
    Encode a classic gs_host_frame into buf at offset.

    ident is the bare CAN identifier, already masked to 29 bits for an
    extended id or 11 for a standard one; this function does not mask it
    again. eff/rtr/err are written into the wire can_id field's top three
    bits directly from ident's own bytes, never combined with it into one
    Python int first the way split_can_id()/combine_can_id() do: on a
    32-bit target, ident | CAN_EFF_FLAG no longer fits a small int the
    instant eff is set (CAN_EFF_FLAG is 0x80000000), which would force a
    heap allocation on every single extended-id frame packed this way
    (R10). pack_classic_frame() below still offers a combined-can_id
    convenience API for callers where that cost does not matter; this
    function is for the one caller, the data plane's hot path, where it
    does.

    buf is any object supporting the buffer protocol with item assignment
    (bytearray, or a memoryview onto one) and must have room for the frame
    from offset onward. data is the frame's actual payload, 0..8 bytes; it
    is zero-padded to the full 8-byte field on the wire. timestamp_us, when
    not None, appends the trailing __le32 timestamp and produces a 24-byte
    frame instead of 20.

    Allocates nothing: every field is written by index or through
    struct.pack_into on a value the caller already owns, and the data
    field is copied byte by byte by index, never through a slice. Returns
    the number of bytes written.
    """
    data_len = len(data)
    if data_len > CLASSIC_DATA_SIZE:
        raise ValueError("classic CAN payload longer than 8 bytes")

    struct.pack_into(_ECHO_ID_FMT, buf, offset, echo_id)
    buf[offset + 4] = ident & 0xFF
    buf[offset + 5] = (ident >> 8) & 0xFF
    buf[offset + 6] = (ident >> 16) & 0xFF
    top = (ident >> 24) & 0x1F
    if eff:
        top |= 0x80
    if rtr:
        top |= 0x40
    if err:
        top |= 0x20
    buf[offset + 7] = top
    buf[offset + 8] = can_dlc
    buf[offset + 9] = channel
    buf[offset + 10] = flags
    buf[offset + 11] = 0  # reserved

    # Slice assignment rather than a byte loop: it is one memcpy in C against
    # eight interpreted iterations, and neither form allocates as long as the
    # source is already a buffer (slicing it here would).
    payload_offset = offset + FRAME_HEADER_SIZE
    buf[payload_offset : payload_offset + data_len] = data
    if data_len < CLASSIC_DATA_SIZE:
        buf[payload_offset + data_len : payload_offset + CLASSIC_DATA_SIZE] = _ZERO_PAD_VIEWS[
            CLASSIC_DATA_SIZE - data_len
        ]

    if timestamp_us is None:
        return FRAME_SIZE_CLASSIC

    struct.pack_into(_TIMESTAMP_FMT, buf, payload_offset + CLASSIC_DATA_SIZE, timestamp_us)
    return FRAME_SIZE_CLASSIC_TS


def unpack_classic_frame_into(buf, offset: int, out, data_out, with_timestamp: bool) -> int:
    """
    Decode a classic gs_host_frame from buf at offset.

    out is a mutable sequence of length 8 (or 9 when with_timestamp) that
    receives [echo_id, ident, can_dlc, channel, flags, eff, rtr, err] and,
    when with_timestamp is true, [..., timestamp_us]. ident/eff/rtr/err
    are read directly from the wire can_id field's bytes rather than
    combined into the single int split_can_id() would produce: that
    combined value overflows a 32-bit target's small-int range the
    instant eff is set (see pack_classic_frame_into's docstring), so a
    caller on the hot path reads the decomposed form here instead and
    never calls split_can_id() at all. data_out is a writable buffer of
    at least 8 bytes that receives the full, zero-padded data field; trim
    it with dlc_to_len(out[2]) for the frame's real length.

    Allocates nothing beyond what representing a value as a Python int
    intrinsically requires: every field is read by indexing buf, never by
    slicing it or by struct.unpack_from, which would allocate a tuple.
    Returns the number of bytes consumed.
    """
    out[0] = _u32le(buf, offset)
    top = buf[offset + 7]
    eff = bool(top & 0x80)
    ident = (
        buf[offset + 4] | (buf[offset + 5] << 8) | (buf[offset + 6] << 16) | ((top & 0x1F) << 24)
    )
    out[1] = ident if eff else ident & CAN_SFF_MASK
    out[2] = buf[offset + 8]
    out[3] = buf[offset + 9]
    out[4] = buf[offset + 10]
    out[5] = eff
    out[6] = bool(top & 0x40)
    out[7] = bool(top & 0x20)
    # offset + 11 is the reserved byte; no consumer reads it.

    # A byte loop, deliberately. The pack direction slice-assigns into a
    # buffer, which copies in C and allocates nothing; unpacking needs a source
    # object for the right-hand side, and building one allocates a memoryview
    # per call. Eight interpreted iterations are cheaper than that.
    payload_offset = offset + FRAME_HEADER_SIZE
    for i in range(CLASSIC_DATA_SIZE):
        data_out[i] = buf[payload_offset + i]

    if not with_timestamp:
        return FRAME_SIZE_CLASSIC

    out[8] = _u32le(buf, payload_offset + CLASSIC_DATA_SIZE)
    return FRAME_SIZE_CLASSIC_TS


def pack_classic_frame(
    echo_id: int, can_id: int, can_dlc: int, channel: int, flags: int, data, timestamp_us=None
) -> bytes:
    """Allocating convenience wrapper around pack_classic_frame_into, for
    tests and other non-hot-path callers: can_id here is combined the way
    split_can_id()/combine_can_id() expect, unlike pack_classic_frame_into
    itself."""
    ident, eff, rtr, err = split_can_id(can_id)
    size = FRAME_SIZE_CLASSIC_TS if timestamp_us is not None else FRAME_SIZE_CLASSIC
    buf = bytearray(size)
    pack_classic_frame_into(
        buf,
        0,
        echo_id,
        ident,
        can_dlc,
        channel,
        flags,
        data,
        eff=eff,
        rtr=rtr,
        err=err,
        timestamp_us=timestamp_us,
    )
    return bytes(buf)


def unpack_classic_frame(buf, with_timestamp: bool):
    """
    Allocating convenience wrapper around unpack_classic_frame_into.

    Returns (echo_id, can_id, can_dlc, channel, flags, data, timestamp_us),
    with can_id combined the way split_can_id()/combine_can_id() expect,
    data as an 8-byte bytes object and timestamp_us as None when
    with_timestamp is false.
    """
    out = [0, 0, 0, 0, 0, 0, 0, 0, 0]
    data = bytearray(CLASSIC_DATA_SIZE)
    unpack_classic_frame_into(buf, 0, out, data, with_timestamp)
    echo_id, ident, can_dlc, channel, flags, eff, rtr, err, timestamp_us = out
    can_id = combine_can_id(ident, eff=bool(eff), rtr=bool(rtr), err=bool(err))
    return (
        echo_id,
        can_id,
        can_dlc,
        channel,
        flags,
        bytes(data),
        (timestamp_us if with_timestamp else None),
    )


# ---------------------------------------------------------------------------
# Control transfer payload codecs.
#
# Each of these is a control-plane operation, issued at most once per
# channel open, so the allocating struct.pack/unpack pair is the right
# tool: there is no steady-state cost to justify a zero-allocation variant
# the way there is for the frame codec above.
# ---------------------------------------------------------------------------

# gs_host_config, HOST_FORMAT payload, 4 bytes.
_HOST_CONFIG_FMT = "<I"
HOST_CONFIG_SIZE = struct.calcsize(_HOST_CONFIG_FMT)


def pack_host_config(byte_order: int = HOST_FORMAT_MAGIC) -> bytes:
    return struct.pack(_HOST_CONFIG_FMT, byte_order)


def unpack_host_config(data) -> int:
    return struct.unpack(_HOST_CONFIG_FMT, data)[0]


# gs_device_config, DEVICE_CONFIG payload, 12 bytes.
_DEVICE_CONFIG_FMT = "<BBBBII"
DEVICE_CONFIG_SIZE = struct.calcsize(_DEVICE_CONFIG_FMT)


def pack_device_config(
    icount: int,
    sw_version: int,
    hw_version: int,
    reserved1: int = 0,
    reserved2: int = 0,
    reserved3: int = 0,
) -> bytes:
    """icount is the channel count minus one: a single-channel device packs 0."""
    return struct.pack(
        _DEVICE_CONFIG_FMT, reserved1, reserved2, reserved3, icount, sw_version, hw_version
    )


def unpack_device_config(data):
    """Returns (reserved1, reserved2, reserved3, icount, sw_version, hw_version)."""
    return struct.unpack(_DEVICE_CONFIG_FMT, data)


# gs_device_bt_const, BT_CONST payload, 40 bytes.
_BT_CONST_FMT = "<10I"
BT_CONST_SIZE = struct.calcsize(_BT_CONST_FMT)


def pack_bt_const(
    feature: int,
    fclk_can: int,
    tseg1_min: int,
    tseg1_max: int,
    tseg2_min: int,
    tseg2_max: int,
    sjw_max: int,
    brp_min: int,
    brp_max: int,
    brp_inc: int,
) -> bytes:
    return struct.pack(
        _BT_CONST_FMT,
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
    )


def unpack_bt_const(data):
    """Returns (feature, fclk_can, tseg1_min, tseg1_max, tseg2_min,
    tseg2_max, sjw_max, brp_min, brp_max, brp_inc)."""
    return struct.unpack(_BT_CONST_FMT, data)


# gs_device_bittiming, the BITTIMING payload, 20 bytes.
_BITTIMING_FMT = "<5I"
BITTIMING_SIZE = struct.calcsize(_BITTIMING_FMT)


def pack_bittiming(prop_seg: int, phase_seg1: int, phase_seg2: int, sjw: int, brp: int) -> bytes:
    return struct.pack(_BITTIMING_FMT, prop_seg, phase_seg1, phase_seg2, sjw, brp)


def unpack_bittiming(data):
    """Returns (prop_seg, phase_seg1, phase_seg2, sjw, brp)."""
    return struct.unpack(_BITTIMING_FMT, data)


# gs_device_mode, the MODE payload, 8 bytes.
_DEVICE_MODE_FMT = "<II"
DEVICE_MODE_SIZE = struct.calcsize(_DEVICE_MODE_FMT)


def pack_device_mode(mode: int, flags: int) -> bytes:
    return struct.pack(_DEVICE_MODE_FMT, mode, flags)


def unpack_device_mode(data):
    """Returns (mode, flags)."""
    return struct.unpack(_DEVICE_MODE_FMT, data)


# gs_device_termination_state, the GET_TERMINATION / SET_TERMINATION
# payload, 4 bytes.
_TERMINATION_STATE_FMT = "<I"
TERMINATION_STATE_SIZE = struct.calcsize(_TERMINATION_STATE_FMT)


def pack_termination_state(state: int) -> bytes:
    return struct.pack(_TERMINATION_STATE_FMT, state)


def unpack_termination_state(data) -> int:
    return struct.unpack(_TERMINATION_STATE_FMT, data)[0]


# TIMESTAMP payload: a bare __le32 microsecond counter, no wrapping struct.
_DEVICE_TIMESTAMP_FMT = "<I"
DEVICE_TIMESTAMP_SIZE = struct.calcsize(_DEVICE_TIMESTAMP_FMT)


def pack_device_timestamp(timestamp_us: int) -> bytes:
    return struct.pack(_DEVICE_TIMESTAMP_FMT, timestamp_us)


def unpack_device_timestamp(data) -> int:
    return struct.unpack(_DEVICE_TIMESTAMP_FMT, data)[0]
