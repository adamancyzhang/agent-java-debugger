"""DebuggerSession — the core engine binding JDWP commands, events, state
caches (classes / threads / frames), stop contexts, stepping, and the
breakpoint manager into one API used by both the REPL and scripted runs.
"""

import collections
import queue
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

from . import commands as C
from . import events as EV
from . import jdwp
from .jdwp import JDWPError, ConnectionLost
from .sourcemap import SourceMap


class EvalError(Exception):
    """Expression evaluation failed (parse, resolve, or target exception)."""


class VmDead(Exception):
    """The target VM has exited."""


class NoStopError(Exception):
    """The command needs a suspended context but none exists."""


THREAD_STATUS_NAMES = {1: "zombie", 2: "running", 3: "sleeping", 4: "monitor", 5: "wait"}

_SIG_TAGS = frozenset("ZBCSIJFD")


@dataclass
class ClassInfo:
    type_id: int
    signature: str
    ref_tag: int
    status: int
    source_file: str | None = None
    source_fetched: bool = False
    methods: list | None = None          # [(method_id, name, signature, mods)]
    fields: list | None = None           # [(field_id, name, signature, mods)]
    line_tables: dict = field(default_factory=dict)   # method_id -> LineTable
    var_tables: dict = field(default_factory=dict)    # method_id -> VariableTable
    superclass: int | None = None
    super_fetched: bool = False

    @property
    def dotted_name(self):
        return C.dotted_signature(self.signature)

    @property
    def simple_name(self):
        return C.simple_name(self.signature)

    @property
    def prepared(self):
        # ClassStatus: 1 verified, 2 prepared, 4 initialized, 8 error.
        # Initialized classes are past preparation, so accept 2 and 4.
        return bool(self.status & 6)


@dataclass
class StopInfo:
    reason: str                      # 'breakpoint' | 'step' | 'vm_death' | ...
    bp: object | None                # Breakpoint or None
    ev: EV.Event | None
    thread_id: int
    condition_error: str | None = None


@dataclass
class EvalContext:
    """Everything an expression evaluator needs: a suspended frame view."""
    session: "DebuggerSession"
    thread_id: int
    frame_id: int
    class_id: int
    method_id: int
    code_index: int
    vars: dict = field(default_factory=dict)     # name -> (tag, value)
    this: tuple | None = None                    # (tag, obj_id) or None


class DebuggerSession:
    def __init__(self, host, port, source_dirs=(), cmd_timeout=15.0,
                 wait_timeout=60.0):
        self.host = host
        self.port = port
        self.cmd_timeout = cmd_timeout
        self.wait_timeout = wait_timeout
        self.conn = jdwp.JDWPConnection()
        self.sources = SourceMap(source_dirs)
        self.vm_info = None
        self.vm_dead = False

        # State caches
        self.classes = {}            # type_id -> ClassInfo
        self.class_by_sig = {}       # 'Lx/y;' -> type_id
        self.class_simple = {}       # 'Bar' -> [type_id, ...]
        self.threads = {}            # thread_id -> name|None
        self._obj_class = {}         # obj_id -> type_id
        self.bps = None              # BreakpointManager (set on attach)

        # Current debug position
        self.current_thread = None
        self.current_frames = None   # [(frame_id, Location)]
        self.current_frame_index = 0
        self.stop = None             # StopInfo or None

        # Event plumbing: reader -> raw queue -> consumer thread -> stop pipe
        self._raw_events = queue.Queue()
        self._buffered = collections.deque()
        self._stop_ev = threading.Event()
        self._stop_lock = threading.Lock()
        self._pending_stop = None
        self._consumer = None
        self.trace_callback = None   # fn(bp, thread_name, location_dict)

    # ------------------------------------------------------------- lifecycle

    def attach(self):
        """Connect, handshake, register base events, snapshot state."""
        self.conn.attach(self.host, self.port, timeout=10.0)
        sizes = C.vm_idsizes(self.conn)
        self.conn.id_sizes = SimpleNamespace(**sizes)
        self.vm_info = C.vm_version(self.conn)
        self.conn.long_format = (self.vm_info["jdwp_major"] > 1
                                 or self.vm_info["jdwp_minor"] >= 6)
        self.conn.on_event = self._raw_events.put
        self.conn.on_disconnect = self._on_disconnect
        self._consumer = threading.Thread(target=self._event_consumer,
                                          daemon=True, name="jdwp-events")
        self._consumer.start()
        for kind in (EV.EV_THREAD_START, EV.EV_THREAD_DEATH):
            C.event_request_set(self.conn, kind, EV.SUSPEND_NONE, [])
        C.event_request_set(self.conn, EV.EV_CLASS_PREPARE, EV.SUSPEND_NONE, [])
        self.refresh_all()
        from .breakpoints import BreakpointManager
        self.bps = BreakpointManager(self)

    def close(self):
        self.conn.close()
        if self._consumer is not None:
            self._raw_events.put(None)

    # ------------------------------------------------------------ class cache

    def refresh_all(self):
        self.classes = {}
        self.class_by_sig = {}
        self.class_simple = {}
        self._obj_class = {}
        for ref_tag, type_id, sig, status in C.vm_all_classes(self.conn):
            self._register_class(ref_tag, type_id, sig, status)
        self.threads = {tid: None for tid in C.vm_all_threads(self.conn)}

    def _register_class(self, ref_tag, type_id, signature, status):
        info = self.classes.get(type_id)
        if info is None:
            info = ClassInfo(type_id, signature, ref_tag, status)
            self.classes[type_id] = info
        else:
            info.signature = signature
            info.status = status
        self.class_by_sig[signature] = type_id
        simple = info.simple_name
        ids = self.class_simple.setdefault(simple, [])
        if type_id not in ids:
            ids.append(type_id)

    def class_info(self, ref_id):
        """ClassInfo for a reference type id, fetching signature if unseen."""
        info = self.classes.get(ref_id)
        if info is None:
            try:
                sig = C.ref_type_signature(self.conn, ref_id)
                status = C.ref_type_status(self.conn, ref_id)
            except JDWPError:
                return None
            self._register_class(1, ref_id, sig, status)
            info = self.classes[ref_id]
        return info

    def class_by_name(self, name):
        """Exact dotted name first, then unique simple-name match."""
        sig = "L" + name.replace(".", "/") + ";"
        type_id = self.class_by_sig.get(sig)
        if type_id is not None:
            return self.classes[type_id]
        ids = self.class_simple.get(name)
        if ids and len(ids) == 1:
            return self.classes[ids[0]]
        return None

    def class_by_signature(self, signature):
        type_id = self.class_by_sig.get(signature)
        return self.classes.get(type_id) if type_id is not None else None

    def class_of_object(self, obj_id):
        """Reference type of an object id (cached); None if unavailable."""
        if obj_id in self._obj_class:
            return self.classes.get(self._obj_class[obj_id])
        try:
            _tag, type_id = C.object_reference_type(self.conn, obj_id)
        except JDWPError:
            return None
        self._obj_class[obj_id] = type_id
        return self.class_info(type_id)

    def class_methods(self, info):
        if info.methods is None:
            info.methods = C.ref_type_methods(self.conn, info.type_id)
        return info.methods

    def class_fields(self, info):
        if info.fields is None:
            info.fields = C.ref_type_fields(self.conn, info.type_id)
        return info.fields

    def class_source_file(self, info):
        if not info.source_fetched:
            try:
                info.source_file = C.ref_type_source_file(self.conn, info.type_id)
            except JDWPError:
                info.source_file = None
            info.source_fetched = True
        return info.source_file

    def class_superclass(self, info):
        if not info.super_fetched:
            try:
                sup = C.class_superclass(self.conn, info.type_id)
                info.superclass = sup if sup else None
            except JDWPError:
                info.superclass = None
            info.super_fetched = True
        return info.superclass

    def line_table(self, info, method_id):
        lt = info.line_tables.get(method_id)
        if lt is None:
            lt = C.method_line_table(self.conn, info.type_id, method_id)
            info.line_tables[method_id] = lt
        return lt

    def variable_table(self, info, method_id):
        vt = info.var_tables.get(method_id)
        if vt is None:
            vt = C.method_variable_table(self.conn, info.type_id, method_id)
            info.var_tables[method_id] = vt
        return vt

    def method_of(self, info, method_id):
        """(name, signature) for a method id; (None, None) if unknown."""
        for mid, name, sig, _mods in self.class_methods(info):
            if mid == method_id:
                return name, sig
        return None, None

    # ------------------------------------------------------------- locations

    def describe_location(self, loc):
        """Human + machine readable location: class, method, line, file."""
        info = self.class_info(loc.class_id)
        class_name = info.dotted_name if info else "?"
        method_name = None
        line = None
        if info is not None:
            method_name, _sig = self.method_of(info, loc.method_id)
            try:
                line = self.line_table(info, loc.method_id).line_at(loc.index)
            except JDWPError:
                line = None
        source_file = self.class_source_file(info) if info else None
        return {"class": class_name, "method": method_name, "line": line,
                "file": source_file,
                "ref_type_id": loc.class_id, "method_id": loc.method_id,
                "index": loc.index}

    def source_at(self, desc, line=None):
        """(matched_line, [(lineno, text), ...]) for a location's file."""
        line = desc["line"] if line is None else line
        if not desc["file"]:
            return None, None
        return self.sources.snippet(desc["file"], line or 0)

    # ------------------------------------------------------------ threads

    def list_threads(self):
        rows = []
        for tid in C.vm_all_threads(self.conn):
            name = self.threads.get(tid)
            if name is None:
                try:
                    name = C.thread_name(self.conn, tid)
                except JDWPError:
                    name = "<unknown>"
                self.threads[tid] = name
            try:
                status, susp = C.thread_status(self.conn, tid)
            except JDWPError:
                status, susp = 0, 0
            rows.append({"id": tid, "name": name,
                         "status": THREAD_STATUS_NAMES.get(status, f"status{status}"),
                         "suspended": bool(susp)})
        return rows

    def find_thread(self, name_or_id):
        try:
            tid = int(name_or_id, 0)
            if tid in self.threads or tid in [t["id"] for t in self.list_threads()]:
                return tid
            return None
        except ValueError:
            for t in self.list_threads():
                if t["name"] == name_or_id:
                    return t["id"]
            return None

    def thread_is_suspended(self, thread_id):
        try:
            _status, susp = C.thread_status(self.conn, thread_id)
            return bool(susp)
        except JDWPError:
            return False

    # ------------------------------------------------------------ frames

    def frames_of(self, thread_id):
        """[(frame_id, Location), ...] top frame first (cached per thread)."""
        if thread_id == self.current_thread and self.current_frames is not None:
            return self.current_frames
        # Frames rejects start+length past the actual frame count
        # (INVALID_LENGTH), so ask for the count first.
        count = C.thread_frame_count(self.conn, thread_id)
        frames = (C.thread_frames(self.conn, thread_id, 0, min(count, 500))
                  if count else [])
        if thread_id == self.current_thread:
            self.current_frames = frames
        return frames

    def _require_position(self):
        if self.current_thread is None:
            raise NoStopError("no current thread — stopped position required "
                              "(or use 'thread <id>' after 'suspend')")

    def current_frame(self):
        """(frame_id, Location) of the selected frame of the current thread."""
        self._require_position()
        frames = self.frames_of(self.current_thread)
        if not frames:
            raise NoStopError("no frames on the current thread "
                              "(native or early thread state)")
        idx = min(self.current_frame_index, len(frames) - 1)
        self.current_frame_index = idx
        return frames[idx]

    def locals_of(self, thread_id, frame_id, loc):
        """{name: (tag, value)} for live locals at a suspended location."""
        info = self.class_info(loc.class_id)
        if info is None:
            return {}
        try:
            vt = self.variable_table(info, loc.method_id)
        except JDWPError:
            return {}
        live = vt.variables_at(loc.index)
        if not live:
            return {}
        requests = [(slot, ord(sig[0]) if sig and sig[0] in _SIG_TAGS else ord("L"))
                    for slot, (_name, sig) in live.items()]
        try:
            values = C.stack_frame_get_values(self.conn, thread_id, frame_id,
                                              requests)
        except JDWPError:
            # Some slot is invalid (e.g. optimized out) — fetch individually.
            values = []
            for slot, sig in requests:
                try:
                    values.append(
                        C.stack_frame_get_values(self.conn, thread_id, frame_id,
                                                 [(slot, sig)])[0])
                except JDWPError:
                    values.append((C.TAG_VOID, None))
        result = {}
        for (slot, (name, _sig)), (tag, value) in zip(live.items(), values):
            if tag != C.TAG_VOID:
                result[name] = (tag, value)
        return result

    def this_of(self, thread_id, frame_id):
        try:
            tag, obj = C.stack_frame_this(self.conn, thread_id, frame_id)
            return (tag, obj) if obj else None
        except JDWPError:
            return None

    def eval_context_for(self, thread_id, frame_index=0):
        frames = self.frames_of(thread_id)
        if not frames:
            raise EvalError("thread has no frames (not suspended or dead)")
        frame_id, loc = frames[min(frame_index, len(frames) - 1)]
        return EvalContext(session=self, thread_id=thread_id, frame_id=frame_id,
                           class_id=loc.class_id, method_id=loc.method_id,
                           code_index=loc.index,
                           vars=self.locals_of(thread_id, frame_id, loc),
                           this=self.this_of(thread_id, frame_id))

    # ------------------------------------------------------------- stepping

    def step(self, kind, timeout=None):
        """Single-step the current thread (into/over/out); returns the stop
        Event that followed (may be a breakpoint preempting the step)."""
        self._require_position()
        depth = {"into": EV.STEP_INTO, "over": EV.STEP_OVER,
                 "out": EV.STEP_OUT}[kind]
        tid = self.current_thread
        req = C.event_request_set(
            self.conn, EV.EV_SINGLE_STEP, EV.SUSPEND_EVENT_THREAD,
            [EV.mod_step(tid, self.conn.id_sizes.object_id_size,
                         size=EV.STEP_SIZE_LINE, depth=depth),
             EV.mod_count(1)])
        self.resume()
        ev = self.wait_for_stop(timeout)
        if ev is None:
            try:
                C.event_request_clear(self.conn, EV.EV_SINGLE_STEP, req)
            except JDWPError:
                pass
            return None
        return ev

    def continue_run(self, timeout=None):
        """Resume the VM and return the next stop Event (None on timeout)."""
        self.resume()
        return self.wait_for_stop(timeout)

    def resume(self):
        if self.vm_dead:
            raise VmDead("target VM has exited")
        try:
            C.vm_resume(self.conn)
        except JDWPError as exc:
            # NOT_SUSPENDED: nothing was paused (e.g. 'continue' issued right
            # after attach, before any stop).  Any other error is real.
            if exc.code != 32:
                raise
        # Frames change once the VM runs — the cached stack is stale.
        self.current_frames = None

    def suspend_all(self):
        C.vm_suspend(self.conn)

    # --------------------------------------------------------- event plumbing

    def _on_disconnect(self):
        self.vm_dead = True
        self._deliver_stop(EV.Event(EV.EV_VM_DEATH, 0))

    def _event_consumer(self):
        while True:
            data = self._raw_events.get()
            if data is None:
                return
            try:
                _policy, evs = EV.parse_composite(data, self.conn)
            except (ValueError, IndexError) as exc:
                # Malformed event — surface but keep the loop alive.
                self._note_error(f"failed to parse event: {exc}")
                continue
            for ev in evs:
                self._dispatch(ev)

    def _note_error(self, message):
        if self.trace_callback is not None:
            self.trace_callback("session", None, {"error": message})

    def _dispatch(self, ev):
        if ev.kind == EV.EV_VM_DEATH:
            self.vm_dead = True
            self._deliver_stop(ev)
        elif ev.kind == EV.EV_CLASS_PREPARE:
            self._register_class(ev.ref_tag, ev.type_id, ev.signature, ev.status)
            if self.bps is not None:
                self.bps.resolve_pending()
        elif ev.kind == EV.EV_THREAD_START:
            self.threads.setdefault(ev.thread_id, None)
        elif ev.kind == EV.EV_THREAD_DEATH:
            self.threads.pop(ev.thread_id, None)
        elif ev.kind in (EV.EV_BREAKPOINT, EV.EV_SINGLE_STEP, EV.EV_EXCEPTION):
            self._deliver_stop(ev)
        # other kinds (METHOD_ENTRY, ...) are not requested and ignored

    def _deliver_stop(self, ev):
        with self._stop_lock:
            if self._pending_stop is None:
                self._pending_stop = ev
                self._stop_ev.set()
            else:
                self._buffered.append(ev)

    def pending_stop_event(self):
        """A stop event buffered while no one was waiting, or None."""
        with self._stop_lock:
            return self._buffered.popleft() if self._buffered else None

    def wait_for_stop(self, timeout=None):
        """Block until a stop-class event (or VM death) arrives.

        Returns the Event, or None on timeout, raises VmDead if the VM died.
        """
        with self._stop_lock:
            if self._buffered:
                return self._buffered.popleft()
        timeout = self.wait_timeout if timeout is None else timeout
        if timeout is not None and timeout <= 0:
            return None
        if not self._stop_ev.wait(timeout):
            return None
        with self._stop_lock:
            ev = self._pending_stop
            self._pending_stop = None
            self._stop_ev.clear()
        return ev

    # ------------------------------------------------------------- on stop

    def on_stop(self, ev):
        """Turn a raw stop event into a presented StopInfo.

        Applies breakpoint bookkeeping: tracepoints, conditions, once.
        Returns None when the stop was swallowed (auto-resumed).
        """
        if ev.kind == EV.EV_VM_DEATH:
            self.vm_dead = True
            return StopInfo("vm_death", None, ev, 0)
        # Frame ids from a previous stop are invalid for this new hit —
        # drop any cached stack so evaluation starts from a fresh fetch.
        self.current_frames = None
        bp = self.bps.by_request.get(ev.request_id) if self.bps else None
        if bp is not None and (getattr(bp, "removed", False) or not bp.enabled):
            # Ghost re-fire of a breakpoint cleared or disabled while the
            # thread was suspended on it (JVMTI defers the removal).
            # Swallow it — a disabled breakpoint must never stop again.
            if bp.suspend != "none":
                self.resume()
            return None
        if ev.kind == EV.EV_BREAKPOINT and bp is not None:
            bp.hit_count += 1
            if bp.suspend == "none":
                desc = self.describe_location(ev.location)
                if self.trace_callback is not None:
                    self.trace_callback(bp, ev.thread_id, desc)
                if bp.once:
                    self.bps.remove(bp)
                return None  # tracepoint: never suspends, nothing to resume
            if bp.condition_ast is not None:
                try:
                    ctx = self.eval_context_for(ev.thread_id)
                    from .evalexpr import evaluate
                    tag, value = evaluate(bp.condition_ast, ctx)
                    if tag != C.TAG_BOOLEAN:
                        from .values import tag_name
                        raise EvalError("condition did not evaluate to boolean "
                                        f"(got {tag_name(tag)})")
                    if not value:
                        self.resume()
                        return None
                except EvalError as exc:
                    # Fail loud: present the stop with the error attached.
                    self._condition_error = str(exc)
                except JDWPError as exc:
                    self._condition_error = f"evaluation failed: {exc}"
            if bp.once:
                self.bps.remove(bp)
        self.current_thread = ev.thread_id
        self.current_frames = None
        self.current_frame_index = 0
        stop = StopInfo("step" if ev.kind == EV.EV_SINGLE_STEP
                        else ("exception" if ev.kind == EV.EV_EXCEPTION
                              else "breakpoint"),
                        bp, ev, ev.thread_id)
        if hasattr(self, "_condition_error"):
            stop.condition_error = self._condition_error
            del self._condition_error
        self.stop = stop
        return stop

    # --------------------------------------------------------- target invoke

    def create_string(self, text):
        """Intern a string in the target VM (cached, used by the evaluator)."""
        cache = self.__dict__.setdefault("_string_cache", {})
        if text not in cache:
            cache[text] = C.vm_create_string(self.conn, text)
        return cache[text]

    def invoke_to_string(self, obj_id):
        """Invoke toString() on an object in the target VM; None if unusable."""
        if self.current_thread is None:
            return None
        info = self.class_of_object(obj_id)
        if info is None:
            return None
        try:
            tag, value = self.invoke_method(info, obj_id, "toString", [])
        except (JDWPError, EvalError):
            return None
        if tag == C.TAG_STRING and value:
            try:
                return C.string_value(self.conn, value)
            except JDWPError:
                return None
        return None

    def invoke_method(self, class_info, obj_id, name, args):
        """Invoke an instance method by name + arg tags (best-effort overload
        resolution).  Returns (tag, value).  Raises EvalError on ambiguity
        or a thrown target exception."""
        from .evalexpr import resolve_method, is_throwable, EXCEPTION_MESSAGE
        thread_id = self.current_thread
        if thread_id is None:
            raise EvalError("no current thread to invoke on (must be stopped)")
        method_id, decl = resolve_method(self, class_info, name, args, static=False)
        tag, value, exception_obj = C.object_invoke_method(
            self.conn, obj_id, thread_id, decl.type_id, method_id, args)
        # The invocation's wrapper frame renumbers the target thread's
        # frame ids — drop the cached frame list.
        self.current_frames = None
        if exception_obj:
            raise EvalError(EXCEPTION_MESSAGE(self, exception_obj))
        if tag == C.TAG_OBJECT and value and is_throwable(self, value):
            raise EvalError(EXCEPTION_MESSAGE(self, value))
        return tag, value

    # ------------------------------------------------------------ rendering

    def stop_description(self):
        """The headline for the current stop (class, method, line)."""
        if self.stop is None or self.stop.ev is None:
            return None
        if self.stop.ev.kind == EV.EV_VM_DEATH:
            return {"reason": "vm_death"}
        loc = self.stop.ev.location
        desc = self.describe_location(loc)
        desc["reason"] = self.stop.reason
        desc["thread"] = self.thread_name(self.stop.thread_id)
        desc["bp"] = ({"id": self.stop.bp.bp_id, "name": self.stop.bp.name}
                      if self.stop.bp else None)
        if self.stop.condition_error:
            desc["condition_error"] = self.stop.condition_error
        return desc

    def thread_name(self, thread_id):
        name = self.threads.get(thread_id)
        if name is None:
            try:
                name = C.thread_name(self.conn, thread_id)
            except JDWPError:
                name = "<unknown>"
            self.threads[thread_id] = name
        return name
