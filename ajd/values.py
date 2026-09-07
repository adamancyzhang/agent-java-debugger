"""Formatting of JDWP tagged values for display, and deep object inspection.

The session argument is duck-typed (needs class_of_object, invoke helpers);
kept out of the import graph to avoid cycles.
"""

from . import commands as C
from .jdwp import JDWPError

_TAG_NAMES = {
    C.TAG_BOOLEAN: "boolean", C.TAG_BYTE: "byte", C.TAG_CHAR: "char",
    C.TAG_SHORT: "short", C.TAG_INT: "int", C.TAG_LONG: "long",
    C.TAG_FLOAT: "float", C.TAG_DOUBLE: "double", C.TAG_VOID: "void",
    C.TAG_OBJECT: "object", C.TAG_STRING: "String", C.TAG_ARRAY: "array",
    C.TAG_THREAD: "Thread", C.TAG_THREAD_GROUP: "ThreadGroup",
    C.TAG_CLASS_LOADER: "ClassLoader", C.TAG_CLASS_OBJECT: "Class",
}


def tag_name(tag):
    return _TAG_NAMES.get(tag, tag)


def is_object_tag(tag):
    return tag in C.OBJECT_TAGS


def format_value(session, tag, value, max_elems=10):
    """One-line human-readable rendering of a tagged value."""
    if tag in (C.TAG_INT, C.TAG_LONG, C.TAG_FLOAT, C.TAG_DOUBLE,
               C.TAG_SHORT, C.TAG_BYTE, C.TAG_CHAR):
        if tag == C.TAG_CHAR and 32 <= value < 127:
            return f"'{chr(value)}' ({value})"
        return str(value)
    if tag == C.TAG_BOOLEAN:
        return "true" if value else "false"
    if tag == C.TAG_VOID:
        return "void"
    if not is_object_tag(tag):
        return f"<tag {tag} value {value}>"
    if value == 0:
        return "null"
    if tag == C.TAG_STRING:
        try:
            return repr(C.string_value(session.conn, value))
        except JDWPError:
            return "String@0x%x" % value
    if tag == C.TAG_ARRAY:
        try:
            length = C.array_length(session.conn, value)
            shown = min(length, max_elems)
            elems = C.array_get_values(session.conn, value, 0, shown)
            body = ", ".join(format_value(session, t, v, max_elems) for t, v in elems)
            if length > shown:
                body += ", …"
            return f"[{body}] (len={length})"
        except JDWPError:
            return "array@0x%x" % value
    info = session.class_of_object(value)
    if info is None:
        return "<collected>@0x%x" % value
    return f"{info.dotted_name}@0x{value:x}"


def class_chain(session, info):
    """Walk a class and its superclasses; yields ClassInfo, nearest first."""
    seen = set()
    current = info
    while current is not None and current.type_id not in seen:
        seen.add(current.type_id)
        yield current
        parent_id = session.class_superclass(current)
        current = session.class_info(parent_id) if parent_id else None


def find_field(session, info, name):
    """Locate a field id by name walking the superclass chain; None if absent."""
    for cls in class_chain(session, info):
        for field_id, fname, sig, _mods in session.class_fields(cls):
            if fname == name:
                return field_id, sig, cls
    return None


def inspect(session, tag, value, depth=2, max_elems=8, budget=300):
    """Recursively render an object's fields (and array elements) as a tree.

    Returns a list of strings (one per line).  ``budget`` caps the total
    number of nodes to keep hostile object graphs bounded.
    """
    out = []
    state = {"spent": 0}

    def walk(t, v, indent, remaining):
        if state["spent"] >= budget:
            out.append(indent + "… (budget exhausted)")
            return
        state["spent"] += 1
        prefix = ""
        if not is_object_tag(t) or v == 0:
            out.append(indent + format_value(session, t, v))
            return
        info = session.class_of_object(v)
        if info is None:
            out.append(indent + "<collected>@0x%x" % v)
            return
        if t == C.TAG_STRING:
            out.append(indent + repr(C.string_value(session.conn, v)))
            return
        if t == C.TAG_ARRAY:
            try:
                length = C.array_length(session.conn, v)
            except JDWPError:
                out.append(indent + "array@0x%x (unavailable)" % v)
                return
            shown = min(length, max_elems)
            out.append(indent + f"{info.dotted_name}[{length}]")
            if remaining <= 0:
                return
            elems = C.array_get_values(session.conn, v, 0, shown)
            for i, (et, ev) in enumerate(elems):
                walk(et, ev, indent + f"  [{i}] ", remaining - 1)
            if length > shown:
                out.append(indent + f"  … ({length - shown} more)")
            return
        # Plain object: summary line + fields.
        summary = f"{info.dotted_name}@0x{v:x}"
        try:
            text = session.invoke_to_string(v)
            if text is not None and text != summary:
                summary += f'  "{text}"'
        except JDWPError:
            pass
        out.append(indent + summary)
        if remaining <= 0:
            return
        child_indent = indent + "  "
        for cls in class_chain(session, info):
            for field_id, fname, sig, _mods in session.class_fields(cls):
                try:
                    ft, fv = C.object_get_values(session.conn, v, [field_id])[0]
                except JDWPError:
                    out.append(f"{child_indent}{fname} = <unavailable>")
                    continue
                out.append(f"{child_indent}{fname} = {format_value(session, ft, fv, max_elems)}")
                # Recurse only into live objects, one level deeper.
                if is_object_tag(ft) and fv != 0 and ft not in (C.TAG_STRING,):
                    walk(ft, fv, child_indent + "    ", remaining - 1)

    walk(tag, value, "", depth)
    return out
