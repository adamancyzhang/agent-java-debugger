# agent-java-debugger

A JDWP CLI debugger for remote JVMs — IDE-grade breakpoints, stepping, and in-memory inspection from the terminal. Built for AI agents: stable machine-readable JSON events, trustworthy exit codes, and a stop-hold guard that never leaves request threads frozen.

[![npm version](https://img.shields.io/npm/v/%40adamancyzhang%2Fagent-java-debugger)](https://www.npmjs.com/package/@adamancyzhang/agent-java-debugger)
[![license](https://img.shields.io/npm/l/%40adamancyzhang%2Fagent-java-debugger)](LICENSE)
[![python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](package.json)

## What it does

`agent-java-debugger` speaks the JDWP wire protocol directly (pure Python standard library, zero dependencies), so it can attach to **any** JVM that started with a debugging port — no IDE, no agent jar, nothing installed on the target machine:

```
-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:15555
```

```bash
agent-java-debugger attach --port 15555 --json --timeout 60 \
  --exec "bp add --file src/main/java/com/foo/UserService.java --line 42 --condition 'id % 7 == 0' --name every-7th" \
  --exec "continue" \
  --exec "locals" \
  --exec "print user.getName()"
```

Each `--exec` runs one command; `--json` emits one JSON document per event/result on stdout. Interactive REPL, source-mapped breakpoints (global / thread-level / conditional / tracepoint / once / per-thread), line stepping, stack frames, locals, recursive object inspection, and expression evaluation with real method calls inside the target JVM.

## Installation

### Global (recommended)

```bash
npm install -g @adamancyzhang/agent-java-debugger
```

The npm launcher finds Python automatically (`py -3` / `python` on Windows, `python3` elsewhere) and runs the bundled `ajd` package.

### From source

```bash
git clone https://github.com/adamancyzhang/agent-java-debugger.git
cd agent-java-debugger
bin/ajd attach ...            # POSIX wrapper
python3 -m ajd attach ...     # direct, PYTHONPATH=. from the repo root
# or: pip install .
```

### Requirements

* Python ≥ 3.10 on the machine running the CLI.
* The debugged JVM must run with the `-agentlib:jdwp=…` flag above (`suspend=y` makes it wait for a debugger before running `main` — deterministic start). The JVM itself needs nothing installed and may live anywhere: any OS, any CPU — JDWP is a TCP wire protocol.
* The debugger only **observes and inspects** — it never kills the target: `quit` detaches, `ajd run` leaves the launched JVM running.

## Quick Start

```bash
# interactive REPL
agent-java-debugger attach --host 127.0.0.1 --port 15555 --source-dir sources/my-app

# scripted, machine-readable (agent mode)
agent-java-debugger attach --port 15555 --json \
  --exec "bp add --file src/main/java/com/foo/UserService.java --line 42 --condition 'id % 7 == 0'" \
  --exec "continue" --exec "locals" --exec "print user.getName()" --exec "finish"

# launch a local JVM with JDWP enabled and attach
agent-java-debugger run --port 15556 -- -cp out Tick
```

## Commands

### attach

```
agent-java-debugger attach [--host H] [--port P] [--source-dir DIR] [--exec CMD]... [--json] [--timeout S] [--cmd-timeout S] [--resume-after S]
```

| Flag | Description |
|---|---|
| `--host` / `--port` | JDWP endpoint (default `127.0.0.1:15555`) |
| `--source-dir <dir>` | Source tree(s) for file breakpoints and the `source` command (repeatable) |
| `--exec <cmd>` | Run one command line instead of the REPL (repeatable; `continue`/`step`/`finish` block until the next stop or `--timeout`) |
| `--json` | Machine-readable output: one JSON document per event/result |
| `--timeout <s>` | Deadline for waiting on the next stop (default 60) |
| `--cmd-timeout <s>` | Per-command reply timeout (default 15) |
| `--resume-after <s>` | Stop-hold guard: auto-resume a held stop after N seconds of inactivity so request threads are never blocked (default 60; `0` disables) |

Without `--exec` an interactive REPL starts; pipe commands to stdin to script it.

### Breakpoints (bp)

```bash
bp add --class com.foo.Bar --line 42          # by class (must contain the line)
bp add --file path/to/Bar.java --line 42      # by source file (inner classes resolved; works before the class loads)
bp list | bp remove <id|name> | bp enable <id|name> | bp disable <id|name> | bp clear
```

| Option | Meaning |
|---|---|
| `--suspend all` (default) | **global**: suspend every thread on hit |
| `--suspend thread` | **thread-level**: suspend only the hitting thread (least intrusive on live apps) |
| `--suspend none` | tracepoint: logs the hit, never suspends |
| `--condition 'expr'` | **conditional**: evaluated in the hitting thread's top frame *inside the target JVM*; false auto-resumes silently. Requires suspend thread/all |
| `--thread <id\|name>` | only fire for one specific thread |
| `--once` | auto-remove after the first stop it produces |
| `--name label` | human/agent-friendly id for stop events |

Pending breakpoints resolve automatically when their class loads (CLASS_PREPARE) — set them before the class exists and they just work.

### Stepping and the stack

```bash
n/next        # step over (line)
s/step        # step into (line)
fin/finish    # step out of the current method
bt/frames     # stack of the current thread (class.method(file:line) per frame)
frame <n> | up | down
thread <id|name>   # switch threads; threads lists them (◉ = current)
```

### Memory inspection

```bash
locals                     # live variables of the selected frame
p/print <expr>             # evaluate an expression in the target JVM
inspect <expr> [--depth N] # recursive field tree of an object
classes [pattern]          # loaded classes matching a substring
source                     # source window around the current line
```

### gql — fire a GraphQL request (e2e trigger)

```bash
agent-java-debugger gql --url http://host/api/graphql --query '{ viewer { id } }' --vars '{"x": 1}'
# inside a session:  gql <url> <query> [--vars JSON]
```

Classic e2e loop: set a breakpoint in a background `attach --exec` run → fire the gql request → the stop lands with the request's stack frame → inspect.

### oinone — gql-driven platform helpers

`agent-java-debugger oinone …` mirrors the frontend GenericFunctionService — generic gql requests with cookie-session persistence (default jar `~/.ajd/cookies.txt`, chmod 0600):

```bash
agent-java-debugger oinone login --url http://host:8091/pamirs/api --login admin --password admin
agent-java-debugger oinone count  --url … --model action --rsql "1==1"
agent-java-debugger oinone exec  --url … --model ganttDemoModel --function queryPage \
    --args '{"cond": {"name": "x"}, "page": {"pageIndex": 0, "pageSize": 10}}' \
    --fields 'id code name' [--mutation]
```

Login failures are detected (`errorCode != 0` → stderr + exit 1).

### skills

```bash
agent-java-debugger skills           # list installed skills
agent-java-debugger skills oinone    # view the oinone companion skill
```

Claude Code skills for agent usage ship in `skill-data/` — the generic [`agent-java-debugger`](skill-data/agent-java-debugger/SKILL.md) skill and the [`oinone` companion](skill-data/oinone/SKILL.md).

## Expression language (conditions, print, inspect)

Java-flavored, evaluated against the suspended frame:

* literals — ints, `long`-suffixed (`1L`), floats/doubles, `'c'`, `"str"`, `true/false`, `null`
* locals, `this`, fields of `this` (walking the superclass chain), statics via `com.foo.Bar.CONST` (longest loaded-class prefix match)
* instance calls `list.size()`, `s.substring(1)`, bare calls `isEmpty()`, static calls `Math.abs(x)` — really invoked in the target with the suspended thread; overloads resolve by argument tags; a thrown target exception surfaces as an error with its message
* arrays: `arr.length`, element formatting
* operators `+ - * / % < <= > >= == != && || !` with Java semantics (binary numeric promotion, integer division truncates, `%` follows the dividend's sign, int/long wraparound); `==` compares primitives numerically, strings by value, objects by identity; `+` concatenates strings

## JSON event reference (agent mode)

```jsonc
{"event":"attach","host":"…","port":15555,"vm":{…},"classes":N,"threads":M}
{"type":"command","line":"bp add …"}                 // echo of each --exec
{"type":"breakpoint","action":"add","bp":{…},"state":"set at 1 location(s)"}
{"event":"trace","bp":{…},"thread":"…","location":{…}}          // tracepoint
{"event":"stop","reason":"breakpoint|step","thread":"main",
 "class":"com.foo.Bar","method":"m","line":42,"file":"Bar.java",
 "bp":{"id":1,"name":"every-7th"},
 "source":[{"line":39,"text":"…"},…]}                 // stop, with source window
{"type":"locals","thread":1,"frame":0,"this":{…},"vars":[{"name":"id","tag":"I","value":"7","raw":7,"object_id":null},…]}
{"type":"print","expression":"user.getName()","tag":"s","value":"'alice'"}
{"type":"inspect","expression":"user","depth":2,"tree":["com.foo.User@0x21f","  name = 'alice'",…]}
{"type":"timeout","event":"no_stop"}                  // wait exceeded
{"type":"error","message":"…"}
{"event":"vm_death"}
```

Agent-mode conventions:

* **Exit code**: `0` only when every `--exec` command succeeded and no wait timed out; `1` when any command errored or a wait timed out; `2` for usage/connection failures; `130` on Ctrl-C. An agent can trust a non-zero code as "the script failed".
* **Stop-hold guard**: a breakpoint suspends request threads in the target. If no command arrives for `--resume-after` seconds (default 60; `0` disables) the session auto-resumes the VM and reports `{"type":"error","message":"stop held for … — VM auto-resumed …"}` so a forgotten breakpoint can never freeze request threads indefinitely.
* **Target exceptions in expressions**: a method called by `print`/`inspect` that throws surfaces as an error with the exception text — never as a silent `null`.

## Testing

```bash
tests/run_sample.sh      # build & start tests/sample/Tick.java on :15556
PORT=15560 tests/e2e_sample.sh   # 25 scenario assertions in --json mode
tests/e2e_pamirs.sh      # 7 live-assertions against a pamirs backend on :15555
```

The e2e suites cover: class/file breakpoints (including inner classes and pending resolution), global/thread/tracepoint suspensions, conditional breakpoints (true/false/impossible), `--once`, thread filters, stepping, locals, print, inspect, frames, gql login + trigger, target-side invocation, step-out, and JSON output.

## Architecture

```
ajd/
├── jdwp.py        transport: handshake, packet framing, reader thread,
│                  reply dispatch (error codes live in the reply HEADER)
├── commands.py    typed wrappers for the JDWP command set + value codec
├── events.py      composite-event parsing, event kinds, request modifiers
├── session.py     engine: class/thread caches, stop contexts, frames,
│                  locals, stepping, resume/suspend, target invocation
├── breakpoints.py breakpoint model + manager (resolution, pending,
│                  ghost-hit suppression for clears while stopped)
├── evalexpr.py    lexer/parser/evaluator for conditions & print
├── values.py      value formatting and recursive object inspection
├── sourcemap.py   local source lookup, line windows, package hints
├── gql.py         GraphQL POST helper
├── oinone.py      oinone login/exec/count + cookie jar
├── repl.py        command runner shared by REPL and --exec
└── cli.py         attach / gql / oinone / run / skills subcommands
```

Design notes worth knowing:

* **One consumer thread** owns all event handling; commands are safe to issue from it (never from the reader thread — that would deadlock on its own reply).
* **Breakpoints removed while the VM is stopped on them** produce one ghost re-fire on resume (JVMTI defers the removal); the engine swallows it silently (also for disabled breakpoints).
* **`continue`'s wait is bounded by one deadline** across all auto-resumed (condition-false) hits — a never-true condition times out instead of cycling forever.
* Frame ids are valid only for one stop: the frame cache is dropped on every resume and every new stop.
* **InvokeMethod packets carry a trailing `options` int (JDWP 1.6+)** — omitting it makes HotSpot return error 113 (INTERNAL) on every invoke. Verified byte-for-byte against jdb via a logging proxy.
* **InvokeMethod replies carry two values** — the return value AND the thrown exception; reading only the first would silently turn target exceptions into `null`.
* **A successful invoke renumbers the target thread's frame ids** (the invocation's wrapper frame shifts the stack); the frame cache must be dropped after every invoke or subsequent locals reads come back empty.

## License

MIT
