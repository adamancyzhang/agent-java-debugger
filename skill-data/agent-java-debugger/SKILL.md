---
name: agent-java-debugger
description: Java JVM debugging CLI for AI agents. Attach to any JVM over its JDWP port (-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:15555) and debug it from the terminal or from agent scripts: source-mapped breakpoints (global / thread-level / conditional / tracepoint / once / per-thread), line stepping (step into / over / out), stack frames, locals, in-memory object inspection, and evaluating expressions with real method calls inside the target JVM. Use when the user asks to debug a Java service, set a breakpoint on a remote JVM, trace why a request behaves unexpectedly, inspect variables/objects at a crash site, verify code paths triggered by an API call, or debug anything on a JVM started with a JDWP port. Triggers include requests to "set a breakpoint at ...", "step through this method", "what is the value of X at line Y", "is this method ever called", "where does this request go", "inspect this variable at the crash site", or "verify this code path with a real request". Emits machine-readable JSON events (--json) for agent consumption with trustworthy exit codes (0 = all commands succeeded, 1 = any error/timeout). A stop-hold guard (--resume-after) auto-resumes the VM if no command arrives, so forgotten breakpoints never freeze request threads. Requires Python 3.10+ on the machine running the CLI; the debugged JVM can be anywhere (any OS, any CPU). The debugger only observes and inspects — it never kills the target. For oinone platform apps (pamirs-designer, gql-driven backends), also load the companion skill via `agent-java-debugger skills oinone`.
allowed-tools: Bash(agent-java-debugger:*)
---

# agent-java-debugger

A JDWP CLI debugger for remote JVMs. Zero Python dependencies; pure stdlib. `npm i -g @adamancyzhang/agent-java-debugger` provides the `agent-java-debugger` command (the launcher finds Python automatically: `py -3`/`python` on Windows, `python3` elsewhere).

Target JVMs must run with:

```
-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:15555
```

`suspend=y` makes the JVM wait for a debugger before running main (deterministic start; pending breakpoints set before main runs still resolve via CLASS_PREPARE). The debugger only **observes and inspects** — it never kills the target: `quit` detaches, `ajd run` leaves the launched JVM running.

## Core workflows

### 1. Scripted debugging (the agent mode)

```bash
agent-java-debugger attach --host 127.0.0.1 --port 15555 \
  --source-dir sources/my-app \
  --json --timeout 60 \
  --exec "bp add --file src/main/java/com/foo/UserService.java --line 42 --condition 'id % 7 == 0' --name every-7th" \
  --exec "continue" \
  --exec "locals" \
  --exec "print user.getName()" \
  --exec "inspect user --depth 2" \
  --exec "finish"
```

Each `--exec` runs one command; `continue`/`next`/`step`/`finish` block until the next stop or the `--timeout` deadline. `--json` emits one JSON document per event/result on stdout — parse it line by line. Without `--exec`, an interactive REPL starts (`attach` alone); pipe commands to stdin to script it.

Key attach flags:

| Flag | Meaning |
|---|---|
| `--source-dir <dir>` | source tree(s) for file breakpoints and the `source` command (repeatable) |
| `--timeout <s>` | deadline for waiting on the next stop (default 60) |
| `--cmd-timeout <s>` | per-command reply timeout (default 15) |
| `--resume-after <s>` | **stop-hold guard**: auto-resume a held stop after N seconds of inactivity so request threads are never blocked (default 60; `0` disables) |

### Agent-mode conventions

* **Exit code**: `0` = every command succeeded and no wait timed out; `1` = any command errored or a wait timed out; `2` = usage/connection failure; `130` = Ctrl-C. Trust a non-zero code as "the script failed" — don't parse stderr for it.
* **Stop-hold guard**: a breakpoint suspends request threads in the target. If no command arrives for `--resume-after` seconds, the session auto-resumes the VM and emits an error event — a forgotten breakpoint can never freeze request threads indefinitely. If you see that error, your script stalled after a stop.
* **Target exceptions**: a method called by `print`/`inspect` that throws surfaces as an error with the exception text — never as a silent `null`.
* **Only one debugger per JDWP port** — close the previous session (or `quit`) before attaching again; attach fails with "connection refused" while another session holds the port.

### 2. Setting breakpoints

```bash
bp add --class com.foo.Bar --line 42          # by class (must contain the line)
bp add --file path/to/Bar.java --line 42      # by source file (inner classes resolved; works before the class loads)
```

Per-breakpoint flavors (choose at add time, IDEA-style):

| Option | Meaning |
|---|---|
| `--suspend all` (default) | **global**: suspend every thread on hit |
| `--suspend thread` | **thread-level**: suspend only the hitting thread (least intrusive on live apps) |
| `--suspend none` | tracepoint: logs the hit, never suspends |
| `--condition 'expr'` | **conditional**: evaluated in the hitting thread's top frame *inside the target JVM*; false auto-resumes silently. Requires suspend thread/all |
| `--thread <id\|name>` | only fire for one specific thread |
| `--once` | auto-remove after the first hit |
| `--name label` | human/agent-friendly id for stop events |

```bash
bp list | bp remove <id|name> | bp enable <id|name> | bp disable <id|name> | bp clear
```

### 3. Stepping and the stack

```bash
n/next        # step over (line)
s/step        # step into (line)
fin/finish    # step out of the current method
bt/frames     # stack of the current thread (class.method(file:line) per frame)
frame <n> | up | down
thread <id|name>   # switch threads; threads lists them (◉ = current)
```

### 4. Memory inspection

```bash
locals                     # live variables of the selected frame
p/print <expr>             # evaluate an expression in the target JVM
inspect <expr> [--depth N] # recursive field tree of an object
classes [pattern]          # loaded classes matching a substring
source                     # source window around the current line
```

Expressions are Java-flavored, evaluated against the suspended frame: literals, locals, `this`, fields (superclass chain walked), statics (`com.foo.Bar.CONST`), arrays (`arr.length`), operators with Java semantics — and **real method calls invoked in the target with the suspended thread** (`user.getName().toUpperCase()`, `java.lang.Math.abs(x)`). A thrown target exception surfaces as an EvalError with the exception's toString. Note: invoking methods runs code in the target; prefer cheap pure methods.

### 5. Driving the app with GraphQL (gql)

Fire a request to steer the app into the code path under test, or trigger actions/functions directly:

```bash
agent-java-debugger gql --url http://host/api/graphql --query '{ viewer { id } }' --vars '{"x": 1}'
# inside a session:  gql <url> <query> [--vars JSON]
```

Classic e2e loop: set a breakpoint in a background `attach --exec` run → fire the gql request → the stop lands with the request's stack frame → inspect. Platform-specific recipes live in the companion skills (`agent-java-debugger skills` lists them; for oinone apps: `agent-java-debugger skills oinone`, and the `oinone login/exec/count` subcommands).

### 6. Interactive REPL

```bash
agent-java-debugger attach --port 15555 --source-dir sources/my-app
```

Stop presentation shows the hit (`■ Breakpoint #2 "name" hit — thread "…" at Class.method(File.java:42)` plus a source window); the prompt state is `∎` (suspended) or `▶` (running). `quit` detaches; the target keeps running. `suspend`/`resume` pause/unpause the whole VM manually.

## JSON event reference

```jsonc
{"event":"attach","host":"…","port":15555,"vm":{…},"classes":N,"threads":M}
{"type":"command","line":"bp add …"}                    // echo of each --exec
{"type":"breakpoint","action":"add","bp":{…},"state":"set at 1 location(s)"}
{"event":"trace","bp":{…},"thread":"…","location":{…}}   // tracepoint hit
{"event":"stop","reason":"breakpoint|step","thread":"main",
 "class":"com.foo.Bar","method":"m","line":42,"file":"Bar.java",
 "bp":{"id":1,"name":"every-7th"},
 "source":[{"line":39,"text":"…"},…]}                    // stop + source window
{"type":"locals","thread":1,"frame":0,"this":{…},
 "vars":[{"name":"id","tag":"I","value":"7","raw":7,"object_id":null},…]}
{"type":"print","expression":"user.getName()","tag":"s","value":"'alice'"}
{"type":"inspect","expression":"user","depth":2,"tree":["com.foo.User@0x21f","  name = 'alice'",…]}
{"type":"timeout","event":"no_stop"}                     // wait deadline exceeded
{"type":"error","message":"…"}
{"event":"vm_death"}
```

## Debugging loops worth using

- **Where does this request go?** bp on the request dispatcher with `--suspend thread`, trigger via gql/curl, read `bt` — the full call path lands in your lap.
- **Why is X true here?** bp with `--condition 'x != expected'`, then `print`/`inspect` the frame at the surprising state.
- **Is this code even reached?** tracepoint (`--suspend none`) logs every hit without pausing.
- **Latency-sensitive app:** always prefer `--suspend thread` over `all`; never leave breakpoints armed — `bp clear` before detaching.
- **Class not loaded yet?** set the breakpoint anyway; it resolves automatically when the class loads.

## Companion skills and reference

- `agent-java-debugger skills` — list installed skills; `skills oinone` — oinone platform debugging (gql protocol, login, model functions, pamirs-designer recipes).
- `ajd` package modules: `jdwp.py` (transport) · `commands.py` (JDWP command set + value codec) · `events.py` · `session.py` (engine) · `breakpoints.py` · `evalexpr.py` (expression evaluator) · `values.py` (formatting/inspect) · `sourcemap.py` · `gql.py` · `oinone.py` · `repl.py` (command runner) · `cli.py`
- Full protocol notes: `README.md` "Design notes" (reply header error codes, InvokeMethod options field, frame-id invalidation after invoke, ghost-hit suppression).
