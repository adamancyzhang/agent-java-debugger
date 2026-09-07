"""A Java-flavored expression evaluator for conditions, print, and inspect.

Expressions are evaluated inside the debugged JVM's data model via JDWP:

* locals of the suspended frame, ``this``, and its fields
* static members via ``com.foo.Bar.CONST`` / ``Bar.staticMethod(...)``
* instance method calls (``list.size()``, ``s.substring(1)``, ...) which
  are really invoked in the target VM with the suspended thread
* string / char / numeric / boolean / null literals
* arithmetic, comparison, logical operators (Java semantics: integer
  division truncates, ``%`` follows the dividend's sign)

Method overloads are resolved by argument count and tag compatibility;
the invocation itself happens in the target, so a mismatch surfaces as a
thrown-exception EvalError.
"""

import re

from . import commands as C
from .jdwp import JDWPError
from .session import EvalError
from .values import class_chain, find_field, format_value, is_object_tag, tag_name

_NUMERIC = {C.TAG_BYTE: 0, C.TAG_SHORT: 0, C.TAG_CHAR: 0, C.TAG_INT: 0,
            C.TAG_LONG: 1, C.TAG_FLOAT: 2, C.TAG_DOUBLE: 3}

# ---------------------------------------------------------------- lexer

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFdDlL]?)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<op><=|>=|==|!=|&&|\|\||[+\-*/%<>!(),.])
  | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
""", re.VERBOSE)


class Lexer:
    def __init__(self, text):
        self.text = text
        self.pos = 0

    def tokens(self):
        while self.pos < len(self.text):
            m = _TOKEN_RE.match(self.text, self.pos)
            if m is None:
                raise EvalError(f"unexpected character {self.text[self.pos]!r} "
                                f"at column {self.pos}")
            self.pos = m.end()
            kind = m.lastgroup
            if kind == "ws":
                continue
            if kind == "num":
                yield ("num", m.group())
            elif kind == "str":
                yield ("str", m.group()[1:-1])
            elif kind == "char":
                yield ("char", m.group()[1:-1])
            elif kind == "op":
                yield ("op", m.group())
            else:
                yield ("ident", m.group())
        yield ("eof", None)


def _unescape(s):
    return (s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
            .replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\"))


def _number_literal(text):
    """Parse a numeric literal into (tag, value)."""
    lower = text.lower()
    if lower.endswith("l"):
        return C.TAG_LONG, int(text[:-1])
    if lower.endswith("f"):
        return C.TAG_FLOAT, float(text[:-1])
    if lower.endswith("d"):
        return C.TAG_DOUBLE, float(text[:-1])
    if "." in text or "e" in lower:
        return C.TAG_DOUBLE, float(text)
    return C.TAG_INT, int(text)


# ---------------------------------------------------------------- parser
# AST: tuples — ('lit', tag, value) | ('chain', primary, members)
# members: ('f', name) | ('c', name, [args]) | ('implicitcall', name, args)
# primary: ('name', ident) | ('lit', ...) | ('paren', expr)
# operators: ('binop', op, l, r) | ('and', l, r) | ('or', l, r) | ('not', x)

class Parser:
    def __init__(self, text):
        self.tokens = list(Lexer(text).tokens())
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, kind, value=None):
        tok = self.next()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise EvalError(f"expected {value or kind!r}, got {tok[1]!r}")
        return tok

    # ---- grammar: or := and ('||' and)*
    def parse_or(self):
        left = self.parse_and()
        while self.peek()[0] == "op" and self.peek()[1] == "||":
            self.next()
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_cmp()
        while self.peek()[0] == "op" and self.peek()[1] == "&&":
            self.next()
            left = ("and", left, self.parse_cmp())
        return left

    def parse_cmp(self):
        left = self.parse_add()
        while self.peek()[0] == "op" and self.peek()[1] in ("<", "<=", ">", ">=", "==", "!="):
            op = self.next()[1]
            left = ("binop", op, left, self.parse_add())
        return left

    def parse_add(self):
        left = self.parse_mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            left = ("binop", op, left, self.parse_mul())
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.next()[1]
            left = ("binop", op, left, self.parse_unary())
        return left

    def parse_unary(self):
        tok = self.peek()
        if tok[0] == "op" and tok[1] in ("!", "-"):
            self.next()
            return ("not", self.parse_unary()) if tok[1] == "!" \
                else ("neg", self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        primary = self.parse_primary()
        members = []
        while self.peek()[0] == "op" and self.peek()[1] in (".", "("):
            op = self.next()[1]
            if op == "(":
                # Bare call: the primary itself is the method name, e.g.
                # isEmpty(), size(), foo(1, 2) — resolved on `this` with a
                # static fallback (see _implicit_call).
                if primary[0] != "name" or members:
                    raise EvalError("'(' after non-method expression")
                members.append(("implicitcall", primary[1], self.parse_args()))
                continue
            name = self.expect("ident")[1]
            if self.peek()[0] == "op" and self.peek()[1] == "(":
                self.next()
                members.append(("c", name, self.parse_args()))
            else:
                members.append(("f", name))
        if not members:
            return primary
        if primary[0] == "name" and len(members) == 1 \
                and members[0][0] == "implicitcall":
            # bare call `name(...)`: the name is the method, not a value —
            # emit a dedicated node so the primary isn't resolved as a
            # variable/class first.
            return ("implicit", primary[1], members[0][2])
        return ("chain", primary, members)

    def parse_args(self):
        args = []
        if self.peek()[0] == "op" and self.peek()[1] == ")":
            self.next()
            return args
        args.append(self.parse_or())
        while self.peek()[0] == "op" and self.peek()[1] == ",":
            self.next()
            args.append(self.parse_or())
        self.expect("op", ")")
        return args

    def parse_primary(self):
        kind, value = self.next()
        if kind == "num":
            return ("lit",) + _number_literal(value)
        if kind == "str":
            return ("lit", C.TAG_PYSTR, _unescape(value))
        if kind == "char":
            ch = _unescape(value)
            if len(ch) != 1:
                raise EvalError(f"bad char literal {value!r}")
            return ("lit", C.TAG_CHAR, ord(ch))
        if kind == "ident":
            if value == "true":
                return ("lit", C.TAG_BOOLEAN, 1)
            if value == "false":
                return ("lit", C.TAG_BOOLEAN, 0)
            if value == "null":
                return ("lit", C.TAG_OBJECT, 0)
            return ("name", value)
        if kind == "op" and value == "(":
            inner = self.parse_or()
            self.expect("op", ")")
            return ("paren", inner)
        raise EvalError(f"unexpected token {value!r}")


def parse_expression(text):
    """Parse an expression string into an AST (raises EvalError on syntax)."""
    parser = Parser(text)
    ast = parser.parse_or()
    if parser.peek()[0] != "eof":
        raise EvalError(f"unexpected trailing input {parser.peek()[1]!r}")
    return ast


# ---------------------------------------------------------------- evaluator

def evaluate(ast, ctx):
    """Evaluate an AST against an EvalContext; returns (tag, value)."""
    return _eval(ast, ctx)


def _eval(node, ctx):
    kind = node[0]
    if kind == "lit":
        tag, value = node[1], node[2]
        if tag == C.TAG_PYSTR:
            return C.TAG_STRING, ctx.session.create_string(value)
        return tag, value
    if kind == "name":
        return _resolve_name(ctx, node[1])
    if kind == "paren":
        return _eval(node[1], ctx)
    if kind == "chain":
        return _eval_chain(ctx, node[1], node[2])
    if kind == "implicit":
        return _implicit_call(ctx, node[1], [_eval(a, ctx) for a in node[2]])
    if kind == "and":
        t, v = _eval(node[1], ctx)
        if t != C.TAG_BOOLEAN:
            raise EvalError(f"'&&' applied to {tag_name(t)}")
        if not v:
            return C.TAG_BOOLEAN, 0
        t, v = _eval(node[2], ctx)
        if t != C.TAG_BOOLEAN:
            raise EvalError(f"'&&' applied to {tag_name(t)}")
        return C.TAG_BOOLEAN, v
    if kind == "or":
        t, v = _eval(node[1], ctx)
        if t != C.TAG_BOOLEAN:
            raise EvalError(f"'||' applied to {tag_name(t)}")
        if v:
            return C.TAG_BOOLEAN, 1
        t, v = _eval(node[2], ctx)
        if t != C.TAG_BOOLEAN:
            raise EvalError(f"'||' applied to {tag_name(t)}")
        return C.TAG_BOOLEAN, v
    if kind == "not":
        t, v = _eval(node[1], ctx)
        if t != C.TAG_BOOLEAN:
            raise EvalError(f"'!' applied to {tag_name(t)}")
        return C.TAG_BOOLEAN, 0 if v else 1
    if kind == "neg":
        t, v = _eval(node[1], ctx)
        if t == C.TAG_LONG:
            return t, -v
        if t in (C.TAG_INT, C.TAG_FLOAT, C.TAG_DOUBLE):
            return t, -v
        raise EvalError(f"unary '-' applied to {tag_name(t)}")
    if kind == "binop":
        return _binop(node[1], _eval(node[2], ctx), _eval(node[3], ctx), ctx)
    raise EvalError(f"internal: unknown AST node {kind}")


def _resolve_name(ctx, name):
    if name == "this":
        if ctx.this is not None:
            return ctx.this
        # Some compilers emit `this` as a regular variable-table entry;
        # fall back to it when StackFrame.ThisObject was unavailable.
        if "this" in ctx.vars:
            return ctx.vars["this"]
        raise EvalError("'this' is not available (static context)")
    if name in ctx.vars:
        return ctx.vars[name]
    if ctx.this is not None:
        tag, obj = ctx.this
        info = ctx.session.class_of_object(obj)
        if info is not None:
            found = find_field(ctx.session, info, name)
            if found is not None:
                field_id, _sig, cls = found
                return C.object_get_values(ctx.session.conn, obj, [field_id])[0]
    raise EvalError(f"unknown identifier '{name}'")


def _eval_chain(ctx, primary, members):
    # Try the value interpretation first: local/this/literal base.
    try:
        cur = _eval(primary, ctx)
        consumed = 0
    except EvalError:
        # Class interpretation: join leading idents into a dotted name and
        # match the longest loaded-class prefix.
        if primary[0] != "name":
            raise
        parts = [primary[1]]
        consumed = 0
        call_tail = None
        for m in members:
            if m[0] == "f":
                parts.append(m[1])
                consumed += 1
            elif m[0] == "c":
                parts.append(m[1])
                call_tail = m
                consumed += 1
                break
            else:
                break
        info = None
        for k in range(len(parts), 0, -1):
            info = ctx.session.class_by_name(".".join(parts[:k]))
            if info is not None:
                break
        if info is None:
            raise EvalError(f"unknown identifier '{parts[0]}'")
        rest = parts[k:]
        new_members = [("f", p) for p in rest]
        if call_tail is not None:
            # The last consumed part is the call name — re-attach its args.
            new_members[-1] = ("c", call_tail[1], call_tail[2])
        new_members.extend(members[consumed:])
        cur = ("classref", info.type_id)
        members = new_members
    for m in members:
        if m[0] == "f":
            cur = _read_field(ctx, cur, m[1])
        elif m[0] == "c":
            cur = _invoke(ctx, cur, m[1], [ _eval(a, ctx) for a in m[2] ])
        elif m[0] == "implicitcall":
            cur = _implicit_call(ctx, m[1], [_eval(a, ctx) for a in m[2]])
        else:
            raise EvalError(f"internal: unknown member {m}")
    return cur


def _read_field(ctx, cur, name):
    if cur[0] == "classref":
        info = ctx.session.class_info(cur[1])
        found = find_field(ctx.session, info, name)
        if found is None:
            raise EvalError(f"no static field '{name}' on {info.dotted_name}")
        field_id, _sig, cls = found
        return C.ref_type_get_values(ctx.session.conn, cls.type_id, [field_id])[0]
    tag, value = cur
    if tag == C.TAG_ARRAY and name == "length":
        return C.TAG_INT, C.array_length(ctx.session.conn, value)
    if not is_object_tag(tag) or value == 0:
        raise EvalError(f"field '{name}' on {tag_name(tag)} value")
    info = ctx.session.class_of_object(value)
    if info is None:
        raise EvalError(f"field '{name}' on collected object")
    found = find_field(ctx.session, info, name)
    if found is None:
        raise EvalError(f"no field '{name}' on {info.dotted_name}")
    field_id, _sig, _cls = found
    return C.object_get_values(ctx.session.conn, value, [field_id])[0]


def _implicit_call(ctx, name, args):
    """Bare call: instance method on `this`, falling back to a static
    method of the enclosing class.  The fallback only applies when the
    instance has no matching method — a target exception thrown by the
    instance method must propagate, not be masked by a static retry."""
    if ctx.this is not None:
        tag, obj = ctx.this
        info = ctx.session.class_of_object(obj)
        if info is not None:
            try:
                resolve_method(ctx.session, info, name, args, static=False)
            except EvalError:
                pass
            else:
                return _invoke(ctx, (tag, obj), name, args)
    info = ctx.session.class_info(ctx.class_id)
    if info is None:
        raise EvalError(f"cannot resolve bare call '{name}'")
    return _invoke(ctx, ("classref", info.type_id), name, args)


def _invoke(ctx, cur, name, args):
    if cur[0] == "classref":
        info = ctx.session.class_info(cur[1])
        if info is None:
            raise EvalError("class unloaded")
        method_id, decl = resolve_method(ctx.session, info, name, args, static=True)
        tag, value, exception_obj = C.class_invoke_method(
            ctx.session.conn, decl.type_id, ctx.thread_id, method_id, args)
    else:
        tag, obj = cur
        if not is_object_tag(tag) or obj == 0:
            raise EvalError(f"method '{name}' on {tag_name(tag)} value")
        info = ctx.session.class_of_object(obj)
        if info is None:
            raise EvalError(f"method '{name}' on collected object")
        method_id, decl = resolve_method(ctx.session, info, name, args, static=False)
        tag, value, exception_obj = C.object_invoke_method(
            ctx.session.conn, obj, ctx.thread_id, decl.type_id, method_id, args)
    # The invocation runs a wrapper frame on the target thread's stack,
    # which renumbers its frame ids — drop the cached frame list.
    ctx.session.current_frames = None
    if exception_obj:
        # The reply's exception slot is authoritative: the invoked method
        # threw in the target.  (The is_throwable fallback below catches
        # VMs that surface the exception as the return value instead.)
        raise EvalError(EXCEPTION_MESSAGE(ctx.session, exception_obj))
    if tag == C.TAG_OBJECT and value and is_throwable(ctx.session, value):
        raise EvalError(EXCEPTION_MESSAGE(ctx.session, value))
    return tag, value


# ------------------------------------------------------------- operators

def _promote(t1, v1, t2, v2):
    """Java binary numeric promotion: double > float > long > int.

    byte/short/char never survive the promotion — any mix of the four
    integral types promotes to int (max() would tie-break to the first
    operand, which is wrong)."""
    if t1 not in _NUMERIC or t2 not in _NUMERIC:
        raise EvalError(f"operator on non-numeric operands ({tag_name(t1)}, {tag_name(t2)})")
    if C.TAG_DOUBLE in (t1, t2):
        target = C.TAG_DOUBLE
    elif C.TAG_FLOAT in (t1, t2):
        target = C.TAG_FLOAT
    elif C.TAG_LONG in (t1, t2):
        target = C.TAG_LONG
    else:
        target = C.TAG_INT
    if target in (C.TAG_INT, C.TAG_LONG):
        return target, int(v1), int(v2)
    return target, float(v1), float(v2)


def _java_div(a, b):
    """Java integer division truncates toward zero; div by zero throws."""
    if b == 0:
        raise EvalError("ArithmeticException: / by zero")
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _java_mod(a, b):
    return a - _java_div(a, b) * b


def _binop(op, lv, rv, ctx):
    (t1, v1), (t2, v2) = lv, rv
    # String concatenation: anything + string.
    if op == "+" and (t1 == C.TAG_STRING or t2 == C.TAG_STRING):
        left = C.string_value(ctx.session.conn, v1) if t1 == C.TAG_STRING \
            else format_value(ctx.session, t1, v1)
        right = C.string_value(ctx.session.conn, v2) if t2 == C.TAG_STRING \
            else format_value(ctx.session, t2, v2)
        return C.TAG_STRING, ctx.session.create_string(left + right)
    if op in ("==", "!="):
        result = _equals(ctx, t1, v1, t2, v2)
        if op == "!=":
            result = not result
        return C.TAG_BOOLEAN, 1 if result else 0
    if op in ("<", "<=", ">", ">="):
        t, a, b = _promote(t1, v1, t2, v2)
        if op == "<":
            ok = a < b
        elif op == "<=":
            ok = a <= b
        elif op == ">":
            ok = a > b
        else:
            ok = a >= b
        return C.TAG_BOOLEAN, 1 if ok else 0
    # arithmetic
    t, a, b = _promote(t1, v1, t2, v2)
    if op == "+":
        r = a + b
    elif op == "-":
        r = a - b
    elif op == "*":
        r = a * b
    elif op == "/":
        if t == C.TAG_LONG:
            r = _java_div(a, b)
        elif t == C.TAG_INT:
            r = _java_div(int(a), int(b))
        else:
            r = a / b
    elif op == "%":
        if t == C.TAG_LONG:
            r = _java_mod(a, b)
        elif t == C.TAG_INT:
            r = _java_mod(int(a), int(b))
        else:
            r = a % b
    else:
        raise EvalError(f"unknown operator {op}")
    if t == C.TAG_INT:
        r = ((r + 0x80000000) % 0x100000000) - 0x80000000  # 32-bit wrap
    elif t == C.TAG_LONG:
        r = ((r + 0x8000000000000000) % 0x10000000000000000) - 0x8000000000000000
    return t, r


def _equals(ctx, t1, v1, t2, v2):
    if t1 in _NUMERIC and t2 in _NUMERIC:
        t, a, b = _promote(t1, v1, t2, v2)
        return a == b
    if t1 == C.TAG_BOOLEAN and t2 == C.TAG_BOOLEAN:
        return bool(v1) == bool(v2)
    if is_object_tag(t1) and is_object_tag(t2):
        if v1 == 0 or v2 == 0:
            return v1 == v2
        if t1 == C.TAG_STRING and t2 == C.TAG_STRING:
            return C.string_value(ctx.session.conn, v1) == C.string_value(ctx.session.conn, v2)
        return v1 == v2  # identity for other objects (Java ==)
    return False


# ------------------------------------------------------ method resolution

def _parse_method_signature(sig):
    """'(IILjava/lang/String;)V' -> (['I', 'I', 'Ljava/lang/String;'], 'V')."""
    i = sig.index("(") + 1
    params = []
    while sig[i] != ")":
        if sig[i] == "[":
            end = i
            while sig[end] == "[":
                end += 1
            if sig[end] == "L":
                end = sig.index(";", end) + 1
            else:
                end += 1
        elif sig[i] == "L":
            end = sig.index(";", i) + 1
        else:
            end = i + 1
        params.append(sig[i:end])
        i = end
    return params, sig[i + 1:]


def _match_score(param_sig, tag):
    """None = incompatible; 0 = exact; 1 = object slot accepting any object."""
    if tag not in C.OBJECT_TAGS:
        return 0 if param_sig == tag else None
    if tag == C.TAG_ARRAY:
        if param_sig.startswith("["):
            return 0
        return 1 if param_sig == "Ljava/lang/Object;" else None
    if param_sig.startswith("L"):
        return 1
    return None


def resolve_method(session, info, name, args, static=False):
    """Pick (method_id, declaring_class) by name + argument tags walking the
    superclass chain.  Raises EvalError on no match or ambiguity."""
    arg_tags = [t for t, _v in args]
    candidates = []
    for cls in class_chain(session, info):
        for mid, mname, sig, mods in session.class_methods(cls):
            if mname != name:
                continue
            if static and not (mods & 0x0008):  # ACC_STATIC
                continue
            params, _ret = _parse_method_signature(sig)
            if len(params) != len(args):
                continue
            scores = [_match_score(p, t) for p, t in zip(params, arg_tags)]
            if None in scores:
                continue
            candidates.append((sum(scores), cls, mid, sig))
    if not candidates:
        want = ", ".join(tag_name(t) for t in arg_tags)
        raise EvalError(f"no {'static ' if static else ''}method "
                        f"{name}({want}) on {info.dotted_name}")
    best = min(c[0] for c in candidates)
    top = [c for c in candidates if c[0] == best]
    # The same signature can appear on multiple ids (override + bridge);
    # those are the same method, not an ambiguity.
    if len({c[3] for c in top}) > 1:
        sigs = ", ".join(c[3] for c in top)
        raise EvalError(f"ambiguous call {name}: candidates {sigs}")
    return top[0][2], top[0][1]


def is_throwable(session, obj_id):
    info = session.class_of_object(obj_id)
    if info is None:
        return False
    for cls in class_chain(session, info):
        if cls.dotted_name == "java.lang.Throwable":
            return True
    return False


def EXCEPTION_MESSAGE(session, obj_id):
    text = session.invoke_to_string(obj_id)
    return f"target invocation threw {text or '<unknown exception>'}"
