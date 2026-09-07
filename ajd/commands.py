"""High-level JDWP command wrappers and the tagged-value codec.

Every wrapper takes a :class:`ajd.jdwp.JDWPConnection` (or the session
holding one) and returns parsed Python data.  Byte layouts follow the
JDWP spec; the line table / variable table entries depend on the target
JDWP minor version (code indexes widened from 4 to 8 bytes in JDWP 1.6),
so those readers are parameterized by ``long_format``.

Tagged values use the JVM's natural sizes per tag:
Z/B = 1 byte, C/S = 2, I/F = 4, J/D = 8; object-ish tags hold an object id.
"""

import struct

from . import jdwp

# --------------------------------------------------------------- tags

TAG_VOID = "V"
TAG_BOOLEAN = "Z"
TAG_BYTE = "B"
TAG_CHAR = "C"
TAG_SHORT = "S"
TAG_INT = "I"
TAG_LONG = "J"
TAG_FLOAT = "F"
TAG_DOUBLE = "D"
TAG_ARRAY = "["
TAG_OBJECT = "L"
TAG_STRING = "s"
TAG_THREAD = "t"
TAG_THREAD_GROUP = "g"
TAG_CLASS_LOADER = "l"
TAG_CLASS_OBJECT = "c"

# Internal-only tag: a Python-side string before it is interned in the
# target VM.  Never appears on the wire; the evaluator converts it to a
# real target string ('s') via VM.CreateString.
TAG_PYSTR = "P"

OBJECT_TAGS = frozenset(
    {TAG_ARRAY, TAG_OBJECT, TAG_STRING, TAG_THREAD, TAG_THREAD_GROUP,
     TAG_CLASS_LOADER, TAG_CLASS_OBJECT}
)

# Reference type tags (from ReferenceType commands / CLASS_PREPARE).
REFTAG_CLASS = 1
REFTAG_INTERFACE = 2
REFTAG_ARRAY = 3

_TAG_SIZE = {
    TAG_BOOLEAN: 1, TAG_BYTE: 1, TAG_CHAR: 2, TAG_SHORT: 2,
    TAG_INT: 4, TAG_FLOAT: 4, TAG_LONG: 8, TAG_DOUBLE: 8,
}
_TAG_UNPACK = {
    TAG_BOOLEAN: ">B", TAG_BYTE: ">b", TAG_CHAR: ">H", TAG_SHORT: ">h",
    TAG_INT: ">i", TAG_FLOAT: ">f", TAG_LONG: ">q", TAG_DOUBLE: ">d",
}
_TAG_PACK = {
    TAG_BOOLEAN: ">B", TAG_BYTE: ">b", TAG_CHAR: ">H", TAG_SHORT: ">h",
    TAG_INT: ">i", TAG_FLOAT: ">f", TAG_LONG: ">q", TAG_DOUBLE: ">d",
}

# ------------------------------------------------------- value codec

def encode_value(tag, value, obj_id_size=8):
    """Encode a tagged value to JDWP wire bytes.  value is a Python int,
    float, bool or (for object-ish tags) an object id."""
    if tag in _TAG_PACK:
        return struct.pack(_TAG_PACK[tag], value)
    if tag in OBJECT_TAGS:
        return value.to_bytes(obj_id_size, "big")
    if tag == TAG_VOID:
        return b""
    raise ValueError(f"cannot encode tag {tag!r}")


def decode_value(reader, obj_id_size=8):
    """Read one tagged value.  Returns (tag, value); object-ish values are
    kept as raw object ids (strings are not fetched here)."""
    tag = chr(reader.read_u1())
    if tag in _TAG_UNPACK:
        return tag, struct.unpack(_TAG_UNPACK[tag], reader.read(_TAG_SIZE[tag]))[0]
    if tag in OBJECT_TAGS:
        return tag, reader.read_id(obj_id_size)
    if tag == TAG_VOID:
        return tag, None
    raise ValueError(f"unknown value tag {tag!r}")


def encode_arg(conn, tag, value):
    """Encode an invoke-method argument (tag byte + value bytes)."""
    return bytes([ord(tag)]) + encode_value(tag, value, conn.id_sizes.object_id_size)


# ------------------------------------------------------- low-level readers

class JDWPReader:
    """Sequential reader over one reply's data bytes."""

    def __init__(self, data, conn):
        self.data = data
        self.pos = 0
        self.conn = conn

    def read(self, n):
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) < n:
            raise ValueError("truncated JDWP reply")
        self.pos += n
        return chunk

    def read_u1(self):
        return struct.unpack(">B", self.read(1))[0]

    def read_u2(self):
        return struct.unpack(">H", self.read(2))[0]

    def read_u4(self):
        return struct.unpack(">I", self.read(4))[0]

    def read_u8(self):
        return struct.unpack(">Q", self.read(8))[0]

    def read_i4(self):
        return struct.unpack(">i", self.read(4))[0]

    def read_str(self):
        return self.read(self.read_u4()).decode("utf-8", "replace")

    def read_id(self, size):
        raw = self.read(size)
        return int.from_bytes(raw, "big")

    def read_frame_id(self):
        return self.read_u8()  # frame ids are always 8 bytes

    def read_location(self):
        from .events import Location
        return Location(self.read_u1(), self.read_id(self.conn.id_sizes.reference_type_id_size),
                        self.read_id(self.conn.id_sizes.method_id_size), self.read_u8())

    def read_value(self):
        return decode_value(self, self.conn.id_sizes.object_id_size)

    def read_line_table(self):
        """Method.LineTable reply body (version-adaptive)."""
        from .events import LineTable
        start, end = self.read_u8(), self.read_u8()
        count = self.read_u4()
        lines = []
        for _ in range(count):
            idx = self.read_u8() if self.conn.long_format else self.read_u4()
            lines.append((idx, self.read_u4()))
        return LineTable(start, end, lines)

    def read_variable_table(self):
        """Method.VariableTable reply body (version-adaptive)."""
        from .events import VariableTable
        arg_cnt = self.read_u4()
        count = self.read_u4()
        slots = []
        for _ in range(count):
            if self.conn.long_format:
                code_index, name, sig, length, slot = (
                    self.read_u8(), self.read_str(), self.read_str(),
                    self.read_u4(), self.read_u4())
            else:
                code_index, name, sig, length, slot = (
                    self.read_u4(), self.read_str(), self.read_str(),
                    self.read_u2(), self.read_u2())
            slots.append((code_index, name, sig, length, slot))
        return VariableTable(arg_cnt, slots)

    def eof(self):
        return self.pos >= len(self.data)


# ------------------------------------------------------- command wrappers
# Each returns parsed data; see the JDWP spec for reply layouts.

def vm_version(conn):
    r = JDWPReader(conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 1), conn)
    desc, major, minor = r.read_str(), r.read_u4(), r.read_u4()
    vm_version_str, vm_name = r.read_str(), r.read_str()
    return {"description": desc, "jdwp_major": major, "jdwp_minor": minor,
            "vm_version": vm_version_str, "vm_name": vm_name}


def vm_idsizes(conn):
    r = JDWPReader(conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 7), conn)
    return {
        "field_id_size": r.read_u4(),
        "method_id_size": r.read_u4(),
        "object_id_size": r.read_u4(),
        "reference_type_id_size": r.read_u4(),
        "frame_id_size": r.read_u4(),
    }


def vm_all_classes(conn):
    r = JDWPReader(conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 3), conn)
    count = r.read_u4()
    classes = []
    for _ in range(count):
        ref_tag = r.read_u1()
        type_id = r.read_id(conn.id_sizes.reference_type_id_size)
        signature = r.read_str()
        status = r.read_u4()
        classes.append((ref_tag, type_id, signature, status))
    return classes


def vm_all_threads(conn):
    r = JDWPReader(conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 4), conn)
    count = r.read_u4()
    return [r.read_id(conn.id_sizes.object_id_size) for _ in range(count)]


def vm_suspend(conn):
    conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 8)


def vm_resume(conn):
    conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 9)


def vm_dispose(conn):
    conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 6)


def vm_create_string(conn, text):
    data = struct.pack(">I", len(text.encode("utf-8"))) + text.encode("utf-8")
    r = JDWPReader(conn.command(jdwp.CMDSET_VIRTUAL_MACHINE, 11, data), conn)
    return r.read_id(conn.id_sizes.object_id_size)


def ref_type_signature(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 1, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    return r.read_str()


def ref_type_source_file(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 7, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    return r.read_str()


def ref_type_fields(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 4, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    count = r.read_u4()
    fields = []
    for _ in range(count):
        field_id = r.read_id(conn.id_sizes.field_id_size)
        name, sig = r.read_str(), r.read_str()
        mod_bits = r.read_u4()
        fields.append((field_id, name, sig, mod_bits))
    return fields


def ref_type_get_values(conn, ref_id, field_ids):
    """Read static field values on a reference type."""
    data = _pack_id(conn, "reference_type_id_size", ref_id) + struct.pack(">I", len(field_ids))
    for f in field_ids:
        data += _pack_id(conn, "field_id_size", f)
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 6, data), conn)
    count = r.read_u4()
    return [r.read_value() for _ in range(count)]


def ref_type_interfaces(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 10, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    count = r.read_u4()
    return [r.read_id(conn.id_sizes.reference_type_id_size) for _ in range(count)]


def ref_type_methods(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 5, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    count = r.read_u4()
    methods = []
    for _ in range(count):
        method_id = r.read_id(conn.id_sizes.method_id_size)
        name, sig = r.read_str(), r.read_str()
        mod_bits = r.read_u4()
        methods.append((method_id, name, sig, mod_bits))
    return methods


def ref_type_status(conn, ref_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_REFERENCE_TYPE, 9, _pack_id(conn, "reference_type_id_size", ref_id)), conn)
    return r.read_u4()


def class_superclass(conn, class_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_CLASS_TYPE, 1, _pack_id(conn, "reference_type_id_size", class_id)), conn)
    return r.read_id(conn.id_sizes.reference_type_id_size)


def class_invoke_method(conn, class_id, thread_id, method_id, args):
    """Invoke a static method; returns (tag, value, exception_obj).

    The JDWP reply carries the return value AND the thrown exception —
    reading both is what lets callers surface target exceptions instead
    of silently presenting a null return."""
    data = (_pack_id(conn, "reference_type_id_size", class_id)
            + _pack_id(conn, "object_id_size", thread_id)
            + _pack_id(conn, "method_id_size", method_id)
            + struct.pack(">I", len(args)))
    for tag, value in args:
        data += encode_arg(conn, tag, value)
    if conn.long_format:
        data += struct.pack(">I", 0)  # invoke options (JDWP 1.6+)
    r = JDWPReader(conn.command(jdwp.CMDSET_CLASS_TYPE, 3, data), conn)
    tag, value = r.read_value()
    _exc_tag, exception_obj = r.read_value()
    return tag, value, exception_obj


def object_reference_type(conn, obj_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_OBJECT_REFERENCE, 1, _pack_id(conn, "object_id_size", obj_id)), conn)
    return chr(r.read_u1()), r.read_id(conn.id_sizes.reference_type_id_size)


def object_get_values(conn, obj_id, field_ids):
    data = _pack_id(conn, "object_id_size", obj_id) + struct.pack(">I", len(field_ids))
    for f in field_ids:
        data += _pack_id(conn, "field_id_size", f)
    r = JDWPReader(conn.command(jdwp.CMDSET_OBJECT_REFERENCE, 2, data), conn)
    count = r.read_u4()
    return [r.read_value() for _ in range(count)]


def object_invoke_method(conn, obj_id, thread_id, class_id, method_id, args):
    """Invoke an instance method; returns (tag, value, exception_obj).

    The JDWP reply carries the return value AND the thrown exception —
    reading both is what lets callers surface target exceptions instead
    of silently presenting a null return."""
    data = (_pack_id(conn, "object_id_size", obj_id)
            + _pack_id(conn, "object_id_size", thread_id)
            + _pack_id(conn, "reference_type_id_size", class_id)
            + _pack_id(conn, "method_id_size", method_id)
            + struct.pack(">I", len(args)))
    for tag, value in args:
        data += encode_arg(conn, tag, value)
    if conn.long_format:
        data += struct.pack(">I", 0)  # invoke options (JDWP 1.6+)
    r = JDWPReader(conn.command(jdwp.CMDSET_OBJECT_REFERENCE, 6, data), conn)
    tag, value = r.read_value()
    _exc_tag, exception_obj = r.read_value()
    return tag, value, exception_obj


def string_value(conn, obj_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_STRING_REFERENCE, 1, _pack_id(conn, "object_id_size", obj_id)), conn)
    return r.read_str()


def thread_name(conn, thread_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_THREAD_REFERENCE, 1, _pack_id(conn, "object_id_size", thread_id)), conn)
    return r.read_str()


def thread_status(conn, thread_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_THREAD_REFERENCE, 4, _pack_id(conn, "object_id_size", thread_id)), conn)
    return r.read_u4(), r.read_u4()  # status, suspend_status


def thread_suspend(conn, thread_id):
    conn.command(jdwp.CMDSET_THREAD_REFERENCE, 2, _pack_id(conn, "object_id_size", thread_id))


def thread_resume(conn, thread_id):
    conn.command(jdwp.CMDSET_THREAD_REFERENCE, 3, _pack_id(conn, "object_id_size", thread_id))


def thread_frame_count(conn, thread_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_THREAD_REFERENCE, 7, _pack_id(conn, "object_id_size", thread_id)), conn)
    return r.read_u4()


def thread_frames(conn, thread_id, start=0, length=64):
    data = (_pack_id(conn, "object_id_size", thread_id)
            + struct.pack(">II", start, length))
    r = JDWPReader(conn.command(jdwp.CMDSET_THREAD_REFERENCE, 6, data), conn)
    count = r.read_u4()
    frames = []
    for _ in range(count):
        frame_id = r.read_frame_id()
        frames.append((frame_id, r.read_location()))
    return frames


def array_length(conn, arr_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_ARRAY_REFERENCE, 1, _pack_id(conn, "object_id_size", arr_id)), conn)
    return r.read_u4()


def array_get_values(conn, arr_id, first=0, length=10):
    data = (_pack_id(conn, "object_id_size", arr_id)
            + struct.pack(">II", first, length))
    r = JDWPReader(conn.command(jdwp.CMDSET_ARRAY_REFERENCE, 2, data), conn)
    count = r.read_u4()
    return [r.read_value() for _ in range(count)]


def event_request_set(conn, event_kind, suspend_policy, modifiers):
    """modifiers: list of (mod_kind, payload_bytes).  Returns request id."""
    data = struct.pack(">BB", event_kind, suspend_policy)
    data += struct.pack(">I", len(modifiers))
    for kind, payload in modifiers:
        data += struct.pack(">B", kind) + payload
    r = JDWPReader(conn.command(jdwp.CMDSET_EVENT_REQUEST, 1, data), conn)
    return r.read_u4()


def event_request_clear(conn, event_kind, request_id):
    data = struct.pack(">BI", event_kind, request_id)
    conn.command(jdwp.CMDSET_EVENT_REQUEST, 2, data)


def event_request_clear_all_breakpoints(conn):
    conn.command(jdwp.CMDSET_EVENT_REQUEST, 3)


def stack_frame_get_values(conn, thread_id, frame_id, slots):
    """slots: list of (slot_number, sig_byte).  Returns [(tag, value), ...]."""
    data = (_pack_id(conn, "object_id_size", thread_id)
            + struct.pack(">Q", frame_id)
            + struct.pack(">I", len(slots)))
    for slot, sig in slots:
        data += struct.pack(">IB", slot, sig)
    r = JDWPReader(conn.command(jdwp.CMDSET_STACK_FRAME, 1, data), conn)
    count = r.read_u4()
    return [r.read_value() for _ in range(count)]


def stack_frame_this(conn, thread_id, frame_id):
    data = (_pack_id(conn, "object_id_size", thread_id)
            + struct.pack(">Q", frame_id))
    r = JDWPReader(conn.command(jdwp.CMDSET_STACK_FRAME, 3, data), conn)
    tag = chr(r.read_u1())
    obj = r.read_id(conn.id_sizes.object_id_size)
    return tag, obj


def method_line_table(conn, ref_id, method_id):
    data = (_pack_id(conn, "reference_type_id_size", ref_id)
            + _pack_id(conn, "method_id_size", method_id))
    return JDWPReader(conn.command(jdwp.CMDSET_METHOD, 1, data), conn).read_line_table()


def method_variable_table(conn, ref_id, method_id):
    data = (_pack_id(conn, "reference_type_id_size", ref_id)
            + _pack_id(conn, "method_id_size", method_id))
    return JDWPReader(conn.command(jdwp.CMDSET_METHOD, 2, data), conn).read_variable_table()


def class_object_reflected_type(conn, class_obj_id):
    r = JDWPReader(conn.command(jdwp.CMDSET_CLASS_OBJECT_REFERENCE, 1, _pack_id(conn, "object_id_size", class_obj_id)), conn)
    return r.read_u1(), r.read_id(conn.id_sizes.reference_type_id_size)


# ------------------------------------------------------- helpers

def _pack_id(conn, size_name, value):
    return value.to_bytes(getattr(conn.id_sizes, size_name), "big")


def dotted_signature(signature):
    """'Lcom/foo/Bar;' -> 'com.foo.Bar'; '[I' -> 'int[]'."""
    if signature.startswith("L"):
        return signature[1:-1].replace("/", ".")
    return signature


def simple_name(signature):
    """'Lcom/foo/Bar;' -> 'Bar'."""
    if signature.startswith("L"):
        return signature[1:-1].rsplit("/", 1)[-1]
    return signature
