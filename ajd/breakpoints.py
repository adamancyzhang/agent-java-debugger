"""Breakpoint model and manager.

Breakpoint flavors (chosen per breakpoint at add time, IDEA-style):

* global        ``--suspend all``    — suspends every thread on hit
* thread-level  ``--suspend thread`` — suspends only the hitting thread
* tracepoint    ``--suspend none``   — logs the hit, never suspends
* thread filter ``--thread <id|name>`` — only fires for one specific thread
* conditional   ``--condition EXPR`` — expression evaluated in the hitting
  thread's top frame; false auto-resumes (requires suspend thread/all)
* once          ``--once``           — auto-removed after the first hit

Breakpoints may target a loaded or not-yet-loaded class: pending
breakpoints are resolved against CLASS_PREPARE events, so they survive
class loading order.
"""

from . import commands as C
from . import events as EV
from .jdwp import JDWPError

SUSPEND_POLICIES = {"none": EV.SUSPEND_NONE,
                    "thread": EV.SUSPEND_EVENT_THREAD,
                    "all": EV.SUSPEND_ALL}


class BreakpointError(Exception):
    pass


class Breakpoint:
    def __init__(self, bp_id, name, mode, spec, line, suspend="all",
                 condition=None, thread_id=None, once=False):
        self.bp_id = bp_id
        self.name = name
        self.mode = mode                    # 'class' | 'file'
        self.spec = spec                    # dotted class name or file path
        self.line = line
        self.suspend = suspend              # 'all' | 'thread' | 'none'
        self.condition_ast = condition      # parsed AST or None
        self.condition_text = None
        self.thread_id = thread_id          # thread filter or None
        self.once = once
        self.enabled = True
        self.hit_count = 0
        self.request_ids = set()            # JDWP event request ids
        self.pending = True                 # unresolved (class not loaded yet)
        self.resolved_locations = []        # [(class_id, method_id, index)]

    # ------------------------------------------------------------- view

    def describe(self):
        return {
            "id": self.bp_id, "name": self.name, "mode": self.mode,
            "target": self.spec, "line": self.line, "suspend": self.suspend,
            "condition": self.condition_text, "thread": self.thread_id,
            "once": self.once, "enabled": self.enabled,
            "hit_count": self.hit_count,
            "pending": self.pending,
            "locations": len(self.resolved_locations),
        }


class BreakpointManager:
    def __init__(self, session):
        self.session = session
        self.by_id = {}
        self.by_request = {}
        self._next_id = 1
        self._full_scanned = False

    # ---------------------------------------------------------------- add

    def add(self, *, name=None, mode, spec, line, suspend="all",
            condition=None, thread=None, once=False):
        """Create and try to resolve a breakpoint.  Raises BreakpointError."""
        if suspend not in SUSPEND_POLICIES:
            raise BreakpointError(f"invalid suspend policy {suspend!r} "
                                  "(all|thread|none)")
        if suspend == "none" and condition is not None:
            raise BreakpointError("condition requires --suspend thread or all "
                                  "(a tracepoint never suspends, so nothing "
                                  "can evaluate it)")
        if condition is not None:
            from .evalexpr import parse_expression
            try:
                cond_ast = parse_expression(condition)
            except Exception as exc:  # EvalError covers syntax; be safe
                raise BreakpointError(f"bad condition {condition!r}: {exc}")
        else:
            cond_ast = None
        thread_id = None
        if thread is not None:
            thread_id = self.session.find_thread(thread)
            if thread_id is None:
                raise BreakpointError(f"thread {thread!r} not found")
        bp = Breakpoint(self._next_id, name, mode, spec, line,
                        suspend=suspend, condition=cond_ast, thread_id=thread_id,
                        once=once)
        bp.condition_text = condition
        self._next_id += 1
        self.by_id[bp.bp_id] = bp
        try:
            self._resolve(bp)
        except JDWPError as exc:
            self.by_id.pop(bp.bp_id)
            raise BreakpointError(f"failed to set breakpoint: {exc}")
        return bp

    # ----------------------------------------------------------- resolution

    def _resolve(self, bp):
        """Find all (class, method, index) locations and set JDWP requests.

        Leaves bp.pending True when no target class is loaded yet; the
        manager retries on every CLASS_PREPARE event.
        """
        if bp.mode == "class":
            infos = self._class_candidates(bp.spec)
        else:
            infos = self._file_candidates(bp.spec)
        if not infos:
            bp.pending = True
            return
        locations = []
        for info in infos:
            for method_id, _name, _sig, _mods in self.session.class_methods(info):
                try:
                    lt = self.session.line_table(info, method_id)
                except JDWPError:
                    continue  # abstract/native methods have no line table
                for index in lt.indexes_for_line(bp.line):
                    locations.append((info.type_id, method_id, index))
        if not locations:
            bp.pending = True  # class loaded but line has no code yet
            return
        self._set_requests(bp, locations)
        bp.resolved_locations = locations
        bp.pending = False

    def _class_candidates(self, spec):
        info = self.session.class_by_name(spec)
        if info is None or not info.prepared:
            return []
        return [info]

    def _file_candidates(self, path):
        """Classes whose source file matches the breakpoint's file, refined
        by the package hint derived from the path when available."""
        basename = self.session.sources.basename(path)
        if basename.endswith(".java"):
            class_name = basename[:-5]
        elif basename.endswith(".kt"):
            class_name = basename[:-3]
        else:
            class_name = basename
        hint = self.session.sources.package_hint(path)
        # Top-level class plus its inner classes (Outer$Inner share the
        # same SourceFile attribute).
        ids = list(self.session.class_simple.get(class_name, []))
        for key, lst in self.session.class_simple.items():
            if key.startswith(class_name + "$"):
                ids.extend(lst)
        if not ids and not self._full_scanned:
            # Slow path: no class named like the file — scan every loaded
            # class's SourceFile attribute (once per session).
            self._full_scanned = True
            ids = list(self.session.classes.keys())
        candidates = []
        for type_id in ids:
            info = self.session.classes.get(type_id)
            if info is None or not info.prepared:
                continue
            src = self.session.class_source_file(info)
            if src == basename and (not hint
                                    or info.dotted_name == hint
                                    or info.dotted_name.startswith(hint + ".")):
                candidates.append(info)
        return candidates

    def _set_requests(self, bp, locations):
        policy = SUSPEND_POLICIES[bp.suspend]
        for class_id, method_id, index in locations:
            loc = EV.Location(C.REFTAG_CLASS, class_id, method_id, index)
            mods = [EV.mod_location_only(loc, self.session.conn)]
            if bp.thread_id is not None:
                mods.append(EV.mod_thread_only(
                    bp.thread_id, self.session.conn.id_sizes.object_id_size))
            req = C.event_request_set(self.session.conn, EV.EV_BREAKPOINT,
                                      policy, mods)
            bp.request_ids.add(req)
            self.by_request[req] = bp

    def resolve_pending(self):
        """Retry all unresolved breakpoints (called on CLASS_PREPARE)."""
        for bp in self.by_id.values():
            if bp.pending and bp.enabled:
                try:
                    self._resolve(bp)
                except JDWPError:
                    pass  # keep pending; retried on the next prepare

    # ------------------------------------------------------------- manage

    def set_enabled(self, bp, enabled):
        if bp.enabled == enabled:
            return
        bp.enabled = enabled
        if enabled:
            if bp.pending or not bp.resolved_locations:
                self._resolve(bp)
            else:
                self._set_requests(bp, bp.resolved_locations)
        else:
            for req in bp.request_ids:
                try:
                    C.event_request_clear(self.session.conn, EV.EV_BREAKPOINT, req)
                except JDWPError:
                    pass
            bp.request_ids.clear()

    def remove(self, bp):
        self.set_enabled(bp, False)
        bp.removed = True
        self.by_id.pop(bp.bp_id, None)
        # Keep the by_request mapping: a thread suspended ON this
        # breakpoint re-fires it once when resumed (JVMTI defers the
        # removal), and on_stop swallows that ghost hit via `removed`.

    def clear(self):
        for bp in list(self.by_id.values()):
            self.remove(bp)

    def find(self, id_or_name):
        if isinstance(id_or_name, int) or str(id_or_name).isdigit():
            bp = self.by_id.get(int(id_or_name))
            if bp is not None:
                return bp
        for bp in self.by_id.values():
            if bp.name == str(id_or_name):
                return bp
        return None
