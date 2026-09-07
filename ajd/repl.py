"""Command runner shared by the interactive REPL and scripted (--exec) runs.

Every command handler is a method of :class:`Runner`; handlers render
either human-readable text (REPL) or one JSON document per result
(--json, for agent consumption).
"""

import argparse
import json
import re
import shlex
import sys
import time

_INSPECT_DEPTH_RE = re.compile(r"^(.*?)(?:\s+--depth\s+(\d+))?$")

from . import commands as C
from . import values
from .breakpoints import BreakpointError, BreakpointManager
from .evalexpr import EvalError, evaluate, parse_expression
from .jdwp import JDWPError, ConnectionLost
from .session import DebuggerSession, NoStopError, StopInfo, VmDead

EXIT = object()          # sentinel: session over
VM_DEAD_MSG = "[target VM exited]"


class Runner:
    def __init__(self, session: DebuggerSession, json_mode=False, out=None):
        self.session = session
        self.json_mode = json_mode
        self.out = out or sys.stdout
        self.interactive = not json_mode and hasattr(self.out, "isatty") \
            and self.out.isatty()
        session.trace_callback = self._on_trace

    # ------------------------------------------------------------- output

    def p(self, *args):
        if not self.json_mode:
            print(*args, file=self.out)

    def j(self, obj):
        if self.json_mode:
            print(json.dumps(obj, ensure_ascii=False), file=self.out, flush=True)

    def error(self, message):
        if self.json_mode:
            self.j({"type": "error", "message": message})
        else:
            print(f"error: {message}", file=self.out)

    # ------------------------------------------------------------ dispatch

    def run_line(self, line):
        """Execute one command line.  Returns EXIT when the session is over."""
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self.error(f"bad quoting: {exc}")
            return None
        cmd, rest = parts[0], parts[1:]
        handler = getattr(self, "cmd_" + cmd, None)
        if handler is None:
            self.error(f"unknown command {cmd!r} (try 'help')")
            return None
        try:
            return handler(line[len(cmd):].strip())
        except (EvalError, NoStopError, BreakpointError, JDWPError, VmDead,
                ConnectionLost, TimeoutError) as exc:
            self.error(str(exc))
            return None
        except OSError as exc:
            self.error(str(exc))
            return None

    # ------------------------------------------------------------- events

    def _on_trace(self, bp, thread_id, desc):
        """Tracepoint hit (suspend none): log only."""
        thread = self.session.thread_name(thread_id)
        if self.json_mode:
            self.j({"event": "trace", "bp": bp.describe(), "thread": thread,
                    "location": desc})
        else:
            print(f"✎ trace #{bp.bp_id} {bp.name or ''} — thread \"{thread}\" "
                  f"at {desc['class']}.{desc['method']}({desc['file']}:{desc['line']})",
                  file=self.out, flush=True)

    def _present(self, stop):
        """Render a stop (breakpoint / step / vm death) for the user."""
        desc = self.session.stop_description()
        if desc is None:
            return
        if desc.get("reason") == "vm_death":
            self.j({"event": "vm_death"})
            self.p(VM_DEAD_MSG)
            return
        source = None
        line = desc.get("line")
        if desc.get("file"):
            _matched, rows = self.session.sources.snippet(desc["file"], line or 0)
            if rows:
                source = [{"line": n, "text": t} for n, t in rows]
        if self.json_mode:
            event = {"event": "stop", "reason": desc["reason"],
                     "thread": desc["thread"],
                     "class": desc["class"], "method": desc["method"],
                     "line": line, "file": desc.get("file"),
                     "bp": desc.get("bp")}
            if desc.get("condition_error"):
                event["condition_error"] = desc["condition_error"]
            if source:
                event["source"] = source
            self.j(event)
        else:
            if desc["reason"] == "breakpoint":
                bp = desc.get("bp") or {}
                label = f"#{bp.get('id', '?')}"
                if bp.get("name"):
                    label += f" \"{bp['name']}\""
                print(f"■ Breakpoint {label} hit — thread \"{desc['thread']}\"")
            elif desc["reason"] == "exception":
                print(f"✕ Exception — thread \"{desc['thread']}\"")
            else:
                print(f"◈ Stepped — thread \"{desc['thread']}\"")
            print(f"  at {desc['class']}.{desc['method']}"
                  f"({desc.get('file')}:{line})")
            if desc.get("condition_error"):
                print(f"  ⚠ condition error: {desc['condition_error']}")
            if source:
                for n, text in source:
                    marker = "▸" if n == line else " "
                    print(f"  {marker}{n:5d} {text}")
            print(flush=True)

    def _wait_cycle(self, first_ev, timeout=None):
        """Process stops after a resume; swallows condition-false hits.

        One deadline bounds the WHOLE cycle: a breakpoint whose condition
        never holds would otherwise re-hit forever and reset the per-wait
        timeout on every swallow.
        """
        timeout = self.session.wait_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout if timeout else None
        ev = first_ev
        while True:
            if ev is None or (deadline is not None
                              and time.monotonic() > deadline):
                self.j({"type": "timeout", "event": "no_stop"})
                self.p("(no stop — timed out)")
                return None
            stop = self.session.on_stop(ev)
            if stop is not None:
                self._present(stop)
                return stop
            # Swallowed (condition false, already resumed) — keep waiting.
            if self.session.vm_dead:
                self._present(StopInfo("vm_death", None, None, 0))
                return None
            ev = self.session.wait_for_stop(
                deadline - time.monotonic() if deadline else timeout)

    def _check_pending_stops(self):
        """Deliver stops that arrived while the user was at the prompt."""
        ev = self.session.pending_stop_event()
        while ev is not None:
            stop = self.session.on_stop(ev)
            if stop is not None:
                self._present(stop)
                return True
            ev = self.session.pending_stop_event()
        return False

    # ------------------------------------------------------------ running

    def cmd_continue(self, arg):
        self.p("▶ continue")
        return self._wait_cycle(self.session.continue_run())

    cmd_c = cmd_continue

    def cmd_next(self, arg):
        self.p("▶ next (step over)")
        return self._wait_cycle(self.session.step("over"))

    cmd_n = cmd_next

    def cmd_step(self, arg):
        self.p("▶ step (into)")
        return self._wait_cycle(self.session.step("into"))

    cmd_s = cmd_step

    def cmd_finish(self, arg):
        self.p("▶ finish (step out)")
        return self._wait_cycle(self.session.step("out"))

    cmd_fin = cmd_finish
    cmd_out = cmd_finish

    def cmd_suspend(self, arg):
        self.session.suspend_all()
        self.j({"type": "suspend", "state": "all threads suspended"})
        self.p("∎ all threads suspended — pick one with 'thread <id>', "
               "then 'frames'/'locals'")
        return None

    def cmd_resume(self, arg):
        self.session.resume()
        self.session.current_thread = None
        self.session.current_frames = None
        self.j({"type": "resume", "state": "running"})
        self.p("▶ resumed")
        return None

    # -------------------------------------------------------- breakpoints

    def _bp_parser(self):
        parser = argparse.ArgumentParser(prog="bp add", add_help=False)
        parser.add_argument("--class", dest="class_name")
        parser.add_argument("--file")
        parser.add_argument("--line", type=int, required=True)
        parser.add_argument("--name")
        parser.add_argument("--suspend", choices=("all", "thread", "none"),
                            default="all")
        parser.add_argument("--condition")
        parser.add_argument("--thread")
        parser.add_argument("--once", action="store_true")
        return parser

    def cmd_bp(self, arg):
        args = shlex.split(arg) if arg else []
        if not args:
            self.cmd_bp_list()
            return None
        sub, subargs = args[0], args[1:]
        if sub == "add":
            return self._bp_add(subargs)
        if sub == "list":
            return self._bp_list()
        if sub == "clear":
            self.session.bps.clear()
            self.j({"type": "breakpoints", "cleared": True})
            self.p("all breakpoints cleared")
            return None
        if sub in ("remove", "rm", "enable", "disable"):
            if not subargs:
                raise BreakpointError(f"bp {sub} needs a breakpoint id or name")
            bp = self.session.bps.find(subargs[0])
            if bp is None:
                raise BreakpointError(f"no breakpoint {subargs[0]!r}")
            if sub == "remove":
                self.session.bps.remove(bp)
                self.j({"type": "breakpoint", "action": "removed",
                        "bp": bp.describe()})
                self.p(f"removed breakpoint #{bp.bp_id} {bp.name or ''}")
            else:
                self.session.bps.set_enabled(bp, sub == "enable")
                self.j({"type": "breakpoint", "action": sub,
                        "bp": bp.describe()})
                self.p(f"{sub}d breakpoint #{bp.bp_id} {bp.name or ''}")
            return None
        raise BreakpointError(f"unknown bp subcommand {sub!r}")

    def _bp_add(self, args):
        opts = self._bp_parser().parse_args(args)
        if bool(opts.class_name) == bool(opts.file):
            raise BreakpointError("bp add needs exactly one of --class/--file")
        if opts.class_name:
            mode, spec = "class", opts.class_name
        else:
            mode, spec = "file", opts.file
        if not opts.name:
            base = spec.replace("\\", "/").rsplit("/", 1)[-1]
            if base.endswith(".java"):
                base = base[:-5]
            opts.name = f"{base}:{opts.line}"
        bp = self.session.bps.add(name=opts.name, mode=mode, spec=spec,
                                  line=opts.line, suspend=opts.suspend,
                                  condition=opts.condition,
                                  thread=opts.thread, once=opts.once)
        state = "pending (target class not loaded yet)" if bp.pending \
            else f"set at {len(bp.resolved_locations)} location(s)"
        self.j({"type": "breakpoint", "action": "add", "bp": bp.describe(),
                "state": state})
        self.p(f"+ breakpoint #{bp.bp_id} {bp.name!r} "
               f"[suspend={bp.suspend}"
               + (f", condition={bp.condition_text!r}" if bp.condition_text else "")
               + (f", thread=#{bp.thread_id}" if bp.thread_id else "")
               + (", once" if bp.once else "") + f"] — {state}")
        return None

    def _bp_list(self):
        items = [bp.describe() for bp in self.session.bps.by_id.values()]
        self.j({"type": "breakpoints", "items": items})
        if not items:
            self.p("no breakpoints")
            return None
        self.p("breakpoints:")
        for d in items:
            self.p(f"  #{d['id']} {d['name']!r} {d['mode']}={d['target']}"
                   f":{d['line']} suspend={d['suspend']}"
                   + (f" condition={d['condition']!r}" if d["condition"] else "")
                   + (f" thread={d['thread']}" if d["thread"] else "")
                   + (" once" if d["once"] else "")
                   + ("" if d["enabled"] else " DISABLED")
                   + f" hits={d['hit_count']}"
                   + (" [pending]" if d["pending"] else ""))
        return None

    cmd_bp_list = _bp_list

    # ------------------------------------------------------------ threads

    def cmd_threads(self, arg):
        rows = self.session.list_threads()
        self.j({"type": "threads", "items": rows,
                "current": self.session.current_thread})
        self.p("threads:")
        for t in rows:
            mark = "◉" if t["id"] == self.session.current_thread else " "
            susp = " (suspended)" if t["suspended"] else ""
            self.p(f"  {mark} #{t['id']:<3d} \"{t['name']}\" {t['status']}{susp}")
        return None

    def cmd_thread(self, arg):
        if not arg:
            raise NoStopError("usage: thread <id|name>")
        tid = self.session.find_thread(arg.strip())
        if tid is None:
            raise NoStopError(f"thread {arg!r} not found")
        self.session.current_thread = tid
        self.session.current_frames = None
        self.session.current_frame_index = 0
        self.j({"type": "thread", "id": tid,
                "name": self.session.thread_name(tid)})
        self.p(f"current thread → #{tid} \"{self.session.thread_name(tid)}\"")
        return None

    # ------------------------------------------------------------- frames

    def cmd_frames(self, arg):
        self.session._require_position()
        frames = self.session.frames_of(self.session.current_thread)
        rows = []
        for i, (frame_id, loc) in enumerate(frames):
            desc = self.session.describe_location(loc)
            rows.append({"index": i, "frame_id": frame_id,
                         "class": desc["class"], "method": desc["method"],
                         "line": desc["line"], "file": desc["file"]})
        self.j({"type": "frames", "thread": self.session.current_thread,
                "items": rows})
        self.p(f"stack of thread #{self.session.current_thread}:")
        for r in rows:
            mark = "▸" if r["index"] == self.session.current_frame_index else " "
            self.p(f"  {mark}{r['index']:2d} {r['class']}.{r['method']}"
                   f"({r['file']}:{r['line']})")
        return None

    cmd_bt = cmd_frames

    def _select_frame(self, index):
        frames = self.session.frames_of(self.session.current_thread)
        if not frames:
            raise NoStopError("no frames available")
        if index < 0 or index >= len(frames):
            raise NoStopError(f"frame {index} out of range "
                              f"(0..{len(frames) - 1})")
        self.session.current_frame_index = index
        _fid, loc = frames[index]
        desc = self.session.describe_location(loc)
        self.j({"type": "frame", "index": index,
                "class": desc["class"], "method": desc["method"],
                "line": desc["line"], "file": desc["file"]})
        self.p(f"frame {index}: {desc['class']}.{desc['method']}"
               f"({desc['file']}:{desc['line']})")

    def cmd_frame(self, arg):
        if not arg.strip().isdigit():
            raise NoStopError("usage: frame <n>")
        self._select_frame(int(arg.strip()))

    def cmd_up(self, arg):
        self._select_frame(self.session.current_frame_index + 1)

    def cmd_down(self, arg):
        self._select_frame(max(0, self.session.current_frame_index - 1))

    # -------------------------------------------------------- inspection

    def _eval_ctx(self):
        self.session._require_position()
        return self.session.eval_context_for(
            self.session.current_thread, self.session.current_frame_index)

    def cmd_locals(self, arg):
        ctx = self._eval_ctx()
        rows = []
        for name in sorted(ctx.vars):
            tag, value = ctx.vars[name]
            rows.append({"name": name, "tag": tag,
                         "value": values.format_value(self.session, tag, value),
                         "raw": value if tag not in C.OBJECT_TAGS else None,
                         "object_id": hex(value) if tag in C.OBJECT_TAGS and value else None})
        this = None
        if ctx.this is not None:
            ttag, tval = ctx.this
            this = {"value": values.format_value(self.session, ttag, tval),
                    "object_id": hex(tval)}
        self.j({"type": "locals",
                "thread": self.session.current_thread,
                "frame": self.session.current_frame_index,
                "this": this, "vars": rows})
        self.p(f"locals (frame {self.session.current_frame_index}"
               + (f", this = {this['value']}" if this else "") + "):")
        for r in rows:
            self.p(f"  {r['name']} = {r['value']}")
        return None

    def _eval_expr(self, expr_text):
        ast = parse_expression(expr_text)
        return evaluate(ast, self._eval_ctx())

    def cmd_print(self, arg):
        if not arg:
            raise EvalError("usage: print <expression>")
        tag, value = self._eval_expr(arg)
        text = values.format_value(self.session, tag, value)
        self.j({"type": "print", "expression": arg, "tag": tag, "value": text})
        self.p(f"= {text}")
        return None

    cmd_p = cmd_print

    def cmd_inspect(self, arg):
        if not arg:
            raise EvalError("usage: inspect <expression> [--depth N]")
        depth = 2
        m = _INSPECT_DEPTH_RE.match(arg)
        expr_text, depth_group = m.group(1).strip(), m.group(2)
        if depth_group is not None:
            depth = int(depth_group)
        tag, value = self._eval_expr(expr_text)
        if not values.is_object_tag(tag) or value == 0:
            self.p(f"= {values.format_value(self.session, tag, value)}")
            return None
        lines = values.inspect(self.session, tag, value, depth=depth)
        self.j({"type": "inspect", "expression": expr_text, "depth": depth,
                "tree": lines})
        for ln in lines:
            self.p(ln)
        return None

    def cmd_classes(self, arg):
        pattern = arg.strip()
        infos = sorted((i for i in self.session.classes.values()),
                       key=lambda i: i.dotted_name)
        rows = []
        for info in infos:
            if pattern and pattern not in info.dotted_name:
                continue
            rows.append(info)
            if len(rows) >= 100:
                break
        self.j({"type": "classes", "pattern": pattern, "count": len(rows),
                "items": [{"name": i.dotted_name, "status": i.status,
                           "prepared": i.prepared} for i in rows]})
        for info in rows:
            state = "prepared" if info.prepared else "loading"
            self.p(f"  {info.dotted_name} [{state}]")
        if len(rows) == 100:
            self.p("  … (showing first 100; narrow the pattern)")
        return None

    def cmd_source(self, arg):
        self.session._require_position()
        _fid, loc = self.session.current_frame()
        desc = self.session.describe_location(loc)
        if not desc["file"]:
            self.p("(no source file known for this class — compiled without "
                   "debug info or use 'source <file> --line N')")
            return None
        matched, rows = self.session.sources.snippet(desc["file"],
                                                     desc["line"] or 0)
        if rows is None:
            self.p(f"(source file {desc['file']!r} not found in source dirs)")
            return None
        self.j({"type": "source", "file": desc["file"], "line": matched,
                "source": [{"line": n, "text": t} for n, t in rows]})
        for n, text in rows:
            marker = "▸" if n == matched else " "
            self.p(f"  {marker}{n:5d} {text}")
        return None

    # ------------------------------------------------------------- misc

    def cmd_gql(self, arg):
        from .gql import send_gql
        tokens = shlex.split(arg)
        if not tokens:
            raise EvalError("usage: gql <url> <query> [--vars JSON] "
                            "[--headers JSON]")
        url = tokens[0]
        variables = headers = None
        query_parts = []
        i = 1
        while i < len(tokens):
            if tokens[i] == "--vars" and i + 1 < len(tokens):
                variables = json.loads(tokens[i + 1])
                i += 2
            elif tokens[i] == "--headers" and i + 1 < len(tokens):
                headers = json.loads(tokens[i + 1])
                i += 2
            else:
                query_parts.append(tokens[i])
                i += 1
        query = " ".join(query_parts)
        if not query:
            raise EvalError("usage: gql <url> <query> [--vars JSON]")
        response = send_gql(url, query, variables=variables, headers=headers)
        self.j({"type": "gql", "url": url, "response": response})
        self.p(json.dumps(response, indent=2, ensure_ascii=False))
        return None

    def cmd_vm(self, arg):
        v = self.session.vm_info
        self.j({"type": "vm", "info": v,
                "idsizes": self.session.conn.id_sizes.__dict__,
                "classes": len(self.session.classes)})
        self.p(f"target VM: {v['vm_name']} (JDWP {v['jdwp_major']}."
               f"{v['jdwp_minor']}), {len(self.session.classes)} classes loaded")
        return None

    def cmd_help(self, arg):
        self.p("""commands:
  bp add --class FQCN | --file PATH --line N
         [--name N] [--suspend all|thread|none] [--condition EXPR]
         [--thread ID|NAME] [--once]     add a breakpoint
  bp list | remove <id|name> | enable <id|name> | disable <id|name> | clear
  c/continue            resume and wait for the next stop
  n/next                step over (line)
  s/step                step into (line)
  fin/finish            step out of the current method
  threads               list threads (◉ = current)
  thread <id|name>      select a thread
  bt/frames             stack frames of the current thread
  frame <n> | up | down select a frame
  locals                local variables of the selected frame
  p/print EXPR          evaluate an expression in the target VM
  inspect EXPR [--depth N]   deep-dive an object's fields
  classes [pattern]     list loaded classes
  source                show source around the current line
  gql URL QUERY [--vars JSON]   fire a GraphQL request (e2e trigger)
  suspend | resume      pause / unpause the whole VM
  vm                    target VM info
  quit/exit             leave (target keeps running)""")
        return None

    def cmd_quit(self, arg):
        return EXIT

    cmd_exit = cmd_quit
    cmd_q = cmd_quit

    # -------------------------------------------------------------- repl

    def announce(self):
        banner = (f"attached to {self.session.vm_info['vm_name']} "
                  f"(JDWP {self.session.vm_info['jdwp_major']}."
                  f"{self.session.vm_info['jdwp_minor']}) — "
                  f"{len(self.session.classes)} classes, "
                  f"{len(self.session.threads)} threads")
        self.j({"event": "attach", "host": self.session.host,
                "port": self.session.port, "vm": self.session.vm_info,
                "classes": len(self.session.classes),
                "threads": len(self.session.threads)})
        self.p(banner)
        self.p("type 'help' for commands, Ctrl-C interrupts a wait")

    def repl(self):
        self.announce()
        while True:
            if self._check_pending_stops():
                continue
            if self.session.vm_dead:
                self.p(VM_DEAD_MSG)
                self.j({"event": "vm_death"})
                return
            try:
                state = "∎" if self.session.stop is not None else "▶"
                line = input(f"ajd[{state}]> ")
            except EOFError:
                self.p()
                return
            except KeyboardInterrupt:
                self.p()
                continue
            if self.run_line(line) is EXIT:
                return


def run_commands(runner, commands):
    """Execute a script of command lines (--exec mode)."""
    for line in commands:
        if runner.session.vm_dead:
            runner.p(VM_DEAD_MSG)
            return
        if runner.json_mode:
            runner.j({"type": "command", "line": line})
        else:
            print(f"ajd> {line}", file=runner.out)
        if runner.run_line(line) is EXIT:
            return
