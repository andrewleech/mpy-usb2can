"""
Software stand-in for `machine.CAN`, used to test can_core on the unix port
without hardware.

Provenance: this file is a reconstruction, not a copy. The original,
`examples/mock_can.py` in mattytrentini/micropython-lib, branch
`aiocan-initial`, path `micropython/can/aiocan/examples/mock_can.py`, head
`99d67e6913`, is documented in this project's roadmap fact F20 as a
machine.CAN-shaped, import-free pure-Python mock with inject()/set_state()/
tx_log affordances, MODE_* constants declared but unimplemented, a
constructor bool standing in for loopback mode, and filter matching that
ignores flags; the file carries no per-file licence header of its own.
This file is rebuilt from that description together with the authoritative
API contract in `extmod/machine_can.c` and `ports/stm32/machine_can.c`,
then extended past the original's scope with real MODE_*/STATE_* values,
flags-aware filter
matching, FDCAN-shaped filter capacity (F25), FIFO depth and overflow
modelling with rx_overruns counted per episode rather than per dropped
frame (F27), and zero-allocation-capable send()/recv() paths backed by
buffers owned by this instance rather than fresh bytes/memoryview objects
per call (R10). Any resemblance to the original's structure is inherited
through that description, not transcription.

Upstream repository licence: MIT, Copyright (c) 2026 Matt Trentini.

This mock is a single isolated node: there is no shared medium and no
arbitration between instances. Traffic arrives only through inject() and
leaves only into tx_log; wiring two instances together to see each other's
frames is not supported.
"""

# Mode constants (extmod/machine_can_port.h machine_can_mode_t).
MODE_NORMAL = 0
MODE_SLEEP = 1
MODE_LOOPBACK = 2
MODE_SILENT = 3
MODE_SILENT_LOOPBACK = 4
_MODE_MAX = 5

# Bus state constants (extmod/machine_can_port.h machine_can_state_t).
STATE_STOPPED = 0
STATE_ACTIVE = 1
STATE_WARNING = 2
STATE_PASSIVE = 3
STATE_BUS_OFF = 4

# Message flag constants (extmod/machine_can_port.h CAN_MSG_FLAG_*).
FLAG_RTR = 1 << 0
FLAG_EXT_ID = 1 << 1
FLAG_FD_F = 1 << 2
FLAG_BRS = 1 << 3
FLAG_UNORDERED = 1 << 4

# recv() error flags (extmod/machine_can_port.h CAN_RECV_ERR_*).
RECV_ERR_FULL = 1 << 0
RECV_ERR_OVERRUN = 1 << 1
RECV_ERR_ESI = 1 << 2

# irq() trigger/flag constants (extmod/machine_can_port.h MP_CAN_IRQ_*).
IRQ_TX = 1 << 0
IRQ_RX = 1 << 1
IRQ_TX_FAILED = 1 << 2
IRQ_STATE = 1 << 3
IRQ_TX_IDX_SHIFT = 16
IRQ_TX_IDX_MASK = 0xFF
_ALLOWED_TRIGGERS = IRQ_TX | IRQ_RX | IRQ_STATE

# H5 FDCAN message RAM is fixed per instance (F8): 3 elements per RX FIFO,
# 3 in the TX queue. Filter capacity is 28 standard plus 8 extended,
# measured on the board (F25).
TX_QUEUE_LEN = 3
_RX_FIFO_DEPTH = 3
_STD_FILTERS_MAX = 28
_EXT_FILTERS_MAX = 8
FILTERS_MAX = _STD_FILTERS_MAX + _EXT_FILTERS_MAX

MAX_LEN = 64  # FDCAN message RAM element size; classic frames use <= 8 of it.

_STD_ID_LIMIT = 1 << 11
_EXT_ID_LIMIT = 1 << 29

# sjw/tseg1/tseg2 bounds, matching the port-overridable defaults in
# extmod/machine_can.c. These are structural register limits, not a
# bit-timing table, so deriving get_timings()'s reported bitrate from them
# below does not reproduce anyone else's calculation.
_SJW_MIN, _SJW_MAX = 1, 4
_TSEG1_MIN, _TSEG1_MAX = 1, 16
_TSEG2_MIN, _TSEG2_MAX = 1, 8


def _check_id_range(can_id, flags):
    limit = _EXT_ID_LIMIT if flags & FLAG_EXT_ID else _STD_ID_LIMIT
    if can_id < 0 or can_id >= limit:
        raise ValueError("invalid id")


class _IRQ:
    """Stand-in for the mp_irq object machine.CAN.irq() returns."""

    def __init__(self, can):
        self._can = can
        self.handler = None
        self.ishard = False

    def flags(self):
        return self._can._irq_flags()

    def trigger(self, new_trigger=None):
        if new_trigger is None:
            return self._can._irq_trigger
        self._can._set_trigger(new_trigger)
        return 0


class MockCAN:
    # extmod/machine_can.c exports every one of these as an attribute of the
    # CAN type itself (machine_can_locals_dict_table), reachable from either
    # the class or an instance without constructing one first. Consumer code
    # written against that contract, such as `can.STATE_BUS_OFF`, must see
    # the same shape here; the module-level names above exist so the rest of
    # this file can refer to them without the class prefix.
    MODE_NORMAL = MODE_NORMAL
    MODE_SLEEP = MODE_SLEEP
    MODE_LOOPBACK = MODE_LOOPBACK
    MODE_SILENT = MODE_SILENT
    MODE_SILENT_LOOPBACK = MODE_SILENT_LOOPBACK

    STATE_STOPPED = STATE_STOPPED
    STATE_ACTIVE = STATE_ACTIVE
    STATE_WARNING = STATE_WARNING
    STATE_PASSIVE = STATE_PASSIVE
    STATE_BUS_OFF = STATE_BUS_OFF

    FLAG_RTR = FLAG_RTR
    FLAG_EXT_ID = FLAG_EXT_ID
    FLAG_UNORDERED = FLAG_UNORDERED

    RECV_ERR_FULL = RECV_ERR_FULL
    RECV_ERR_OVERRUN = RECV_ERR_OVERRUN

    IRQ_RX = IRQ_RX
    IRQ_TX = IRQ_TX
    IRQ_TX_FAILED = IRQ_TX_FAILED
    IRQ_STATE = IRQ_STATE
    IRQ_TX_IDX_SHIFT = IRQ_TX_IDX_SHIFT
    IRQ_TX_IDX_MASK = IRQ_TX_IDX_MASK

    TX_QUEUE_LEN = TX_QUEUE_LEN
    FILTERS_MAX = FILTERS_MAX

    def __init__(
        self, id, bitrate, mode=MODE_NORMAL, sample_point=75, sjw=_SJW_MIN, tseg1=-1, tseg2=-1
    ):
        if not isinstance(id, int) or id < 1:
            raise ValueError("CAN(%r) doesn't exist" % (id,))
        self._id = id
        self._irq_obj = None
        self._irq_trigger = 0
        self._irq_state_pending = False
        self._rx_backing = bytearray(MAX_LEN)
        # Precomputed views over the backing buffer, one per possible
        # length, so recv() with a preallocated result list never
        # allocates a memoryview object on the hot path (R10); the real
        # driver reaches the same end by mutating an existing
        # memoryview's length field in place, which pure Python cannot
        # do once the object exists.
        self._rx_views = [memoryview(self._rx_backing)[0:n] for n in range(MAX_LEN + 1)]
        # Per-slot backing buffers for outgoing payloads, so send() never
        # allocates a bytes object on the hot path either.
        self._tx_backing = [bytearray(MAX_LEN) for _ in range(TX_QUEUE_LEN)]
        # Test/inspection affordances.
        self.tx_log = []
        self.tx_log_enabled = True
        self.restart_count = 0
        self.init(bitrate, mode, sample_point, sjw, tseg1, tseg2)

    # -- lifecycle -----------------------------------------------------

    def init(self, bitrate, mode=MODE_NORMAL, sample_point=75, sjw=_SJW_MIN, tseg1=-1, tseg2=-1):
        if mode >= _MODE_MAX:
            raise ValueError("mode")
        if sjw < _SJW_MIN or sjw > _SJW_MAX:
            raise ValueError("sjw")
        if tseg1 != -1 and (tseg1 < _TSEG1_MIN or tseg1 > _TSEG1_MAX):
            raise ValueError("tseg1")
        if tseg2 != -1 and (tseg2 < _TSEG2_MIN or tseg2 > _TSEG2_MAX):
            raise ValueError("tseg2")
        if sample_point <= 0 or sample_point >= 100:
            raise ValueError("sample_point")

        self._bitrate = bitrate
        self._mode = mode
        self._sample_point = sample_point
        self._sjw = sjw
        if tseg1 == -1:
            # Neither tseg1 nor tseg2 was given (the pair is validated
            # together above): split the available time quanta by
            # sample point and clamp each half to its own register
            # range. This is a structural fit only, not a reproduction
            # of any bit-timing table (R7) - get_timings() just needs to
            # report values that are within bounds.
            total = _TSEG1_MAX + _TSEG2_MAX
            self._tseg1 = min(_TSEG1_MAX, max(_TSEG1_MIN, round(sample_point / 100 * total)))
            self._tseg2 = min(_TSEG2_MAX, max(_TSEG2_MIN, total - self._tseg1))
        else:
            self._tseg1 = tseg1
            self._tseg2 = tseg2

        self._started = True
        self._state = STATE_ACTIVE
        # Each slot is None (empty) or (can_id, data, flags, completion_pending, success).
        self._tx_slots: list[tuple[int, bytes, int, bool, bool] | None] = [None] * TX_QUEUE_LEN
        # Two FIFOs of (can_id, data, flags), indexed by matched-filter parity (see inject()).
        self._rx_fifo: tuple[list[tuple[int, bytes, int]], list[tuple[int, bytes, int]]] = ([], [])
        self._rx_errors = 0
        self.counters = {
            "tec": 0,
            "rec": 0,
            "num_warning": 0,
            "num_passive": 0,
            "num_bus_off": 0,
            "rx_overruns": 0,
        }
        # extmod/machine_can.c: no frames are accepted until set_filters()
        # is called, even after a reinit.
        self._filters_std = []
        self._filters_ext = []

    def deinit(self):
        self._started = False
        self._irq_trigger = 0
        self._irq_obj = None

    def _require_started(self):
        if not self._started:
            raise OSError(22)  # EINVAL, matching machine_can_check_initialised

    # -- irq -------------------------------------------------------------

    def irq(self, handler=None, trigger=0, hard=False):
        self._require_started()
        any_args = handler is not None or trigger != 0
        if self._irq_obj is None:
            self._irq_obj = _IRQ(self)
        if any_args:
            if trigger & ~_ALLOWED_TRIGGERS:
                raise ValueError("trigger 0x%08x unsupported" % trigger)
            self._irq_obj.handler = handler
            self._irq_obj.ishard = hard
            self._set_trigger(trigger)
        return self._irq_obj

    def _set_trigger(self, trigger):
        self._irq_trigger = trigger

    def _fire_irq(self):
        if self._irq_obj is not None and self._irq_obj.handler is not None:
            self._irq_obj.handler(self)

    def _irq_flags(self):
        flags = 0
        if self._irq_trigger & IRQ_STATE and self._irq_state_pending:
            flags |= IRQ_STATE
            self._irq_state_pending = False
        if self._irq_trigger & IRQ_RX and (self._rx_fifo[0] or self._rx_fifo[1]):
            flags |= IRQ_RX
        if self._irq_trigger & IRQ_TX:
            for idx, slot in enumerate(self._tx_slots):
                if slot is not None and slot[3]:  # slot[3]: completion pending
                    can_id, data, tx_flags, _pending, success = slot
                    self._tx_slots[idx] = None
                    flags |= IRQ_TX | (idx << IRQ_TX_IDX_SHIFT)
                    if not success:
                        flags |= IRQ_TX_FAILED
                    break
        return flags

    # -- transmit --------------------------------------------------------

    def send(self, id, data, flags=0):
        self._require_started()
        _check_id_range(id, flags)
        max_len = MAX_LEN if flags & FLAG_FD_F else 8
        if len(data) > max_len:
            raise OverflowError("data too long")

        idx_empty = None
        for idx, slot in enumerate(self._tx_slots):
            if slot is None:
                if idx_empty is None:
                    idx_empty = idx
            elif slot[0] == id and not (flags & FLAG_UNORDERED):
                return None
        if idx_empty is None:
            return None

        payload = bytes(data)
        self._tx_slots[idx_empty] = (id, payload, flags, False, True)
        if self.tx_log_enabled:
            self.tx_log.append((id, payload, flags, idx_empty))
        return idx_empty

    def cancel_send(self, idx):
        self._require_started()
        if idx < 0 or idx >= TX_QUEUE_LEN:
            raise IndexError()
        slot = self._tx_slots[idx]
        if slot is None or slot[3]:
            return False
        self._tx_slots[idx] = None
        return True

    def complete_tx(self, index=None, success=True):
        """
        Test affordance: mark a queued transmission as finished, as the
        FDCAN TX-complete interrupt would. Fires IRQ_TX (and
        IRQ_TX_FAILED, if not success) if that channel's irq() is
        listening for it.
        """
        self._require_started()
        if index is None:
            for idx, slot in enumerate(self._tx_slots):
                if slot is not None and not slot[3]:
                    index = idx
                    break
            if index is None:
                raise ValueError("no pending transmission")
        slot = self._tx_slots[index]
        if slot is None or slot[3]:
            raise ValueError("slot %d has no pending transmission" % index)
        can_id, data, tx_flags, _pending, _success = slot
        self._tx_slots[index] = (can_id, data, tx_flags, True, success)
        if self._irq_trigger & IRQ_TX:
            self._fire_irq()
        else:
            self._tx_slots[index] = None

    # -- receive -----------------------------------------------------------

    def recv(self, result=None):
        self._require_started()
        frame = self._pop_rx_frame()
        if frame is None:
            return None
        can_id, data, flags = frame
        errors = self._rx_errors
        self._rx_errors = 0
        dlen = len(data)

        if result is None:
            view = memoryview(bytearray(data))
            return [can_id, view, flags, errors]

        if not isinstance(result[1], memoryview):
            raise TypeError("result[1] must be a memoryview")
        # The real driver mutates the caller's memoryview in place (its
        # backing buffer never moves); a Python memoryview cannot change
        # its own reported length, so this re-slices a buffer this
        # instance owns instead. The large backing allocation still
        # happens exactly once, per instance, not per frame.
        self._rx_backing[0:dlen] = data
        result[0] = can_id
        result[1] = memoryview(self._rx_backing)[0:dlen]
        result[2] = flags
        result[3] = errors
        return result

    def _pop_rx_frame(self):
        for fifo in self._rx_fifo:
            if fifo:
                return fifo.pop(0)
        return None

    # -- filters -------------------------------------------------------

    def set_filters(self, filters):
        self._require_started()
        self._filters_std = []
        self._filters_ext = []
        if filters is None:
            self._filters_std.append((0, 0))
            self._filters_ext.append((0, 0))
            return
        idx_std = 0
        idx_ext = 0
        for item in filters:
            if len(item) != 3:
                raise ValueError("filter items must have length 3")
            can_id, mask, flags = item
            _check_id_range(can_id, flags)
            _check_id_range(mask, flags)
            if flags & ~FLAG_EXT_ID:
                raise ValueError("flags")
            if flags & FLAG_EXT_ID:
                if idx_ext >= _EXT_FILTERS_MAX:
                    raise ValueError("too many filters for this ID type")
                self._filters_ext.append((can_id, mask))
                idx_ext += 1
            else:
                if idx_std >= _STD_FILTERS_MAX:
                    raise ValueError("too many filters for this ID type")
                self._filters_std.append((can_id, mask))
                idx_std += 1

    # -- state / counters / timings -------------------------------------

    def state(self):
        return self._state if self._started else STATE_STOPPED

    def get_counters(self, result=None):
        c = self.counters
        tx_pending = sum(1 for slot in self._tx_slots if slot is not None)
        rx_pending = len(self._rx_fifo[0]) + len(self._rx_fifo[1])
        values = [
            c["tec"],
            c["rec"],
            c["num_warning"],
            c["num_passive"],
            c["num_bus_off"],
            tx_pending,
            rx_pending,
            c["rx_overruns"],
        ]
        if result is None:
            return values
        result[:] = values
        return result

    def get_timings(self, result=None):
        f_clock = 8_000_000  # HSE on this board (F9); independent of any table
        actual = f_clock // (1 + self._tseg1 + self._tseg2)
        # Index 4 is the CAN-FD BRS timing sub-list; this board builds with
        # MICROPY_HW_ENABLE_FDCAN, so the real driver always returns a
        # 4-element list here even though it has no FD timings to report yet
        # (extmod/machine_can.c fd_list->items[0..3] = mp_const_none), never
        # a bare None.
        values = [actual, self._sjw, self._tseg1, self._tseg2, [None, None, None, None], None]
        if result is None:
            return values
        result[:] = values
        return result

    def restart(self):
        self._require_started()
        self.restart_count += 1
        self._tx_slots = [None] * TX_QUEUE_LEN
        self.counters = {k: 0 for k in self.counters}
        self._state = STATE_ACTIVE

    # -- test affordances -------------------------------------------------

    def inject(self, can_id, data, flags=0):
        """
        Push a frame in from the bus, as if the FDCAN receive path had
        just stored it. Returns whether a filter accepted the frame;
        a frame that matches a filter but arrives at an already-full RX
        FIFO (3 deep, F8) is still accounted as accepted and counted as
        an overrun.
        """
        self._require_started()
        ext = bool(flags & FLAG_EXT_ID)
        filters = self._filters_ext if ext else self._filters_std
        matched = None
        for idx, (fid, mask) in enumerate(filters):
            if (can_id & mask) == (fid & mask):
                matched = idx
                break
        if matched is None:
            return False

        fifo = self._rx_fifo[matched & 1]
        if len(fifo) >= _RX_FIFO_DEPTH:
            self._rx_errors |= RECV_ERR_OVERRUN
            self.counters["rx_overruns"] += 1
            return True

        payload = bytes(len(data)) if flags & FLAG_RTR else bytes(data)
        fifo.append((can_id, payload, flags))
        if len(fifo) >= _RX_FIFO_DEPTH:
            self._rx_errors |= RECV_ERR_FULL
        if self._irq_trigger & IRQ_RX:
            self._fire_irq()
        return True

    def set_state(self, state):
        """Force the reported bus state, as a hardware error-state
        transition would, and latch/fire IRQ_STATE if enabled."""
        self._require_started()
        if state == STATE_WARNING:
            self.counters["num_warning"] += 1
        elif state == STATE_PASSIVE:
            self.counters["num_passive"] += 1
        elif state == STATE_BUS_OFF:
            self.counters["num_bus_off"] += 1
        self._state = state
        self._irq_state_pending = True
        if self._irq_trigger & IRQ_STATE:
            self._fire_irq()
