"""JDWP event kinds, composite-event parsing, and event-request modifiers."""

import struct
from dataclasses import dataclass, field

from . import jdwp
from .commands import JDWPReader

# Event kinds
EV_SINGLE_STEP = 1
EV_BREAKPOINT = 2
EV_FRAME_POP = 3
EV_EXCEPTION = 4
EV_THREAD_START = 6
EV_THREAD_DEATH = 7
EV_CLASS_PREPARE = 8
EV_CLASS_UNLOAD = 9
EV_FIELD_ACCESS = 20
EV_FIELD_MODIFICATION = 21
EV_METHOD_ENTRY = 40
EV_METHOD_EXIT = 41
EV_METHOD_EXIT_WITH_RETURN_VALUE = 42
EV_VM_START = 90
EV_VM_DEATH = 99

# Suspend policies
SUSPEND_NONE = 0
SUSPEND_EVENT_THREAD = 1
SUSPEND_ALL = 2

# Step depth / size
STEP_INTO = 0
STEP_OVER = 1
STEP_OUT = 2
STEP_SIZE_MIN = 0
STEP_SIZE_LINE = 1

# Modifier kinds
MOD_COUNT = 1
MOD_CONDITIONAL = 2
MOD_THREAD_ONLY = 3
MOD_CLASS_ONLY = 4
MOD_CLASS_MATCH = 5
MOD_CLASS_EXCLUDE = 6
MOD_LOCATION_ONLY = 7
MOD_EXCEPTION_ONLY = 8
MOD_FIELD_ONLY = 9
MOD_STEP = 10

_EVENT_NAMES = {
    EV_SINGLE_STEP: "step", EV_BREAKPOINT: "breakpoint", EV_FRAME_POP: "frame_pop",
    EV_EXCEPTION: "exception", EV_THREAD_START: "thread_start",
    EV_THREAD_DEATH: "thread_death", EV_CLASS_PREPARE: "class_prepare",
    EV_CLASS_UNLOAD: "class_unload", EV_FIELD_ACCESS: "field_access",
    EV_FIELD_MODIFICATION: "field_modification", EV_METHOD_ENTRY: "method_entry",
    EV_METHOD_EXIT: "method_exit",
    EV_METHOD_EXIT_WITH_RETURN_VALUE: "method_exit_with_return_value",
    EV_VM_START: "vm_start", EV_VM_DEATH: "vm_death",
}


def event_name(kind):
    return _EVENT_NAMES.get(kind, f"event_{kind}")


@dataclass
class Location:
    tag: int
    class_id: int
    method_id: int
    index: int  # code index (8 bytes)


@dataclass
class LineTable:
    start: int
    end: int
    lines: list  # [(code_index, line_number), ...]

    def line_at(self, code_index):
        """The source line a code index belongs to (greatest entry <= index)."""
        line = None
        for idx, ln in self.lines:
            if idx <= code_index:
                line = ln
            else:
                break
        return line

    def indexes_for_line(self, line_number):
        """All code indexes whose entry maps to the given source line."""
        return [idx for idx, ln in self.lines if ln == line_number]


@dataclass
class VariableTable:
    arg_cnt: int
    slots: list  # [(code_index, name, signature, length, slot), ...]

    def variables_at(self, code_index):
        """Variables live at a code index: the innermost (latest declared)
        entry per slot whose range covers the index."""
        chosen = {}
        for idx, name, sig, length, slot in self.slots:
            if idx <= code_index < idx + length:
                chosen[slot] = (name, sig)
        return chosen


@dataclass
class Event:
    kind: int
    request_id: int
    thread_id: int = 0
    location: Location | None = None
    # CLASS_PREPARE extras
    ref_tag: int = 0
    type_id: int = 0
    signature: str = ""
    status: int = 0
    # EXCEPTION extras
    exception_tag: str = ""
    exception_obj: int = 0
    catch_location: Location | None = None
    # METHOD_EXIT_WITH_RETURN_VALUE
    value: tuple | None = None

    @property
    def name(self):
        return event_name(self.kind)


def parse_composite(data, conn):
    """Parse one Composite event reply body into a list of Events."""
    r = JDWPReader(data, conn)
    suspend_policy = r.read_u1()
    count = r.read_u4()
    events = []
    for _ in range(count):
        kind = r.read_u1()
        request_id = r.read_u4()
        ev = Event(kind, request_id)
        if kind in (EV_SINGLE_STEP, EV_BREAKPOINT, EV_METHOD_ENTRY, EV_METHOD_EXIT,
                    EV_FRAME_POP, EV_FIELD_ACCESS, EV_FIELD_MODIFICATION):
            ev.thread_id = r.read_id(conn.id_sizes.object_id_size)
            ev.location = r.read_location()
        elif kind in (EV_THREAD_START, EV_THREAD_DEATH, EV_VM_START):
            ev.thread_id = r.read_id(conn.id_sizes.object_id_size)
        elif kind == EV_CLASS_PREPARE:
            ev.thread_id = r.read_id(conn.id_sizes.object_id_size)
            ev.ref_tag = r.read_u1()
            ev.type_id = r.read_id(conn.id_sizes.reference_type_id_size)
            ev.signature = r.read_str()
            ev.status = r.read_u4()
        elif kind == EV_EXCEPTION:
            ev.thread_id = r.read_id(conn.id_sizes.object_id_size)
            ev.location = r.read_location()
            ev.exception_tag = chr(r.read_u1())
            ev.exception_obj = r.read_id(conn.id_sizes.object_id_size)
            ev.catch_location = r.read_location()
        elif kind == EV_METHOD_EXIT_WITH_RETURN_VALUE:
            ev.thread_id = r.read_id(conn.id_sizes.object_id_size)
            ev.location = r.read_location()
            ev.value = r.read_value()
        elif kind == EV_VM_DEATH:
            pass  # request id only
        elif kind == EV_CLASS_UNLOAD:
            ev.signature = r.read_str()
        else:
            raise ValueError(f"unhandled event kind {kind}")
        events.append(ev)
    return suspend_policy, events


# ------------------------------------------------- event-request modifiers

def mod_count(count):
    return (MOD_COUNT, struct.pack(">I", count))


def mod_conditional(expr_id):
    return (MOD_CONDITIONAL, struct.pack(">I", expr_id))


def mod_thread_only(thread_id, obj_id_size):
    return (MOD_THREAD_ONLY, thread_id.to_bytes(obj_id_size, "big"))


def mod_class_only(class_id, ref_id_size):
    return (MOD_CLASS_ONLY, class_id.to_bytes(ref_id_size, "big"))


def mod_class_match(pattern):
    b = pattern.encode("utf-8")
    return (MOD_CLASS_MATCH, struct.pack(">I", len(b)) + b)


def mod_class_exclude(pattern):
    b = pattern.encode("utf-8")
    return (MOD_CLASS_EXCLUDE, struct.pack(">I", len(b)) + b)


def mod_location_only(location, conn):
    return (MOD_LOCATION_ONLY,
            struct.pack(">B", location.tag)
            + location.class_id.to_bytes(conn.id_sizes.reference_type_id_size, "big")
            + location.method_id.to_bytes(conn.id_sizes.method_id_size, "big")
            + struct.pack(">Q", location.index))


def mod_step(thread_id, obj_id_size, size=STEP_SIZE_LINE, depth=STEP_INTO):
    return (MOD_STEP, thread_id.to_bytes(obj_id_size, "big")
            + struct.pack(">II", size, depth))
