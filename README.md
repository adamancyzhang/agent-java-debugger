# ajd — agent-java-debugger

[![npm version](https://img.shields.io/npm/v/%40adamancyzhang%2Fagent-java-debugger)](https://www.npmjs.com/package/@adamancyzhang/agent-java-debugger)
[![license](https://img.shields.io/npm/l/%40adamancyzhang%2Fagent-java-debugger)](LICENSE)

A generic CLI debugger for remote JVMs over **JDWP** (Java Debug Wire
Protocol). Attach to any JVM started with

```
-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:15555
```

and get IDE-grade debugging from the terminal — or from an agent, via
machine-readable JSON output.

Zero dependencies: Python ≥ 3.10 standard library only.

## Install

```bash
npm i -g @adamancyzhang/agent-java-debugger   # provides `agent-java-debugger`
# or from a local checkout:
npm pack && npm i -g ./adamancyzhang-agent-java-debugger-*.tgz
# or no npm at all:
bin/ajd attach ...        # POSIX wrapper
python3 -m ajd attach ... # direct, PYTHONPATH=. from the repo root
```

The npm `bin` launcher finds Python automatically (`py -3` / `python` on
Windows, `python3` elsewhere, must be ≥ 3.10) and runs the bundled `ajd`
package — the debugged JVM needs nothing installed.

Claude Code skills for agent usage ship in `skill-data/` — the generic
[`agent-java-debugger`](skill-data/agent-java-debugger/SKILL.md) skill and
the [`oinone` companion](skill-data/oinone/SKILL.md). They are also
browsable from the CLI:

```bash
agent-java-debugger skills           # list installed skills
agent-java-debugger skills oinone    # view the oinone skill content
```

### oinone subcommand (gql-driven platforms)

`agent-java-debugger oinone …` mirrors the frontend
GenericFunctionService — generic gql requests with cookie-session
persistence (default jar `~/.ajd/cookies.txt`):

```bash
agent-java-debugger oinone login --url http://host:8091/pamirs/api --login admin --password admin
agent-java-debugger oinone count  --url … --model action --rsql "1==1"
agent-java-debugger oinone exec  --url … --model ganttDemoModel --function queryPage \
    --args '{"cond": {"name": "x"}, "page": {"pageIndex": 0, "pageSize": 10}}' \
    --fields 'id code name' [--mutation]
```

## Cross-platform notes

* The CLI is **pure Python stdlib** — no compiled extensions, no C
  toolchains. It runs on Windows / macOS / Linux and is **CPU-architecture
  agnostic** (amd64, arm64, anything Python runs on). The debugged JVM may
  live anywhere, on any OS/arch — JDWP is a TCP wire protocol.
* The only interpreter requirement is Python ≥ 3.10 (the code uses `X | None`
  type unions); the npm launcher enforces this and Windows uses `py -3`
  rather than `python3`.
* Console output uses UTF-8 with replacement characters (Windows legacy
  codepages can't encode the Unicode markers ■ ◉ ▸ ✎).
* Path handling accepts Windows separators (source paths are normalized
  before package-hint parsing).
* The test scripts (`tests/*.sh`) are bash + `nc` — dev-side convenience
  for macOS/Linux; the tool itself has no such dependency. On Windows use
  `npm test`-equivalent flows or drive `attach --exec` directly.

* **Source-mapped breakpoints** — by class (`--class com.foo.Bar`) or by
  source file (`--file path/to/Bar.java --line 42`; inner classes, not yet
  loaded classes and package hints from the file path are all handled).
  Pending breakpoints resolve automatically when their class loads
  (CLASS_PREPARE), so setting a breakpoint before the class exists just
  works.
* **IDEA-style breakpoint flavors**, selectable per breakpoint:
  * `--suspend all`    — **global** breakpoint: suspends every thread
  * `--suspend thread` — **thread-level**: suspends only the hitting thread
  * `--suspend none`   — tracepoint: logs the hit, never suspends
  * `--thread <id|name>` — fire only for one specific thread
  * `--condition 'expr'` — **conditional** breakpoint; the expression is
    evaluated in the hitting thread's top frame *inside the target JVM*
    (locals, `this`, fields, statics and real method calls all work).
    False conditions auto-resume silently.
  * `--once` — auto-removed after the first hit
* **Line stepping** — `step` (into), `next` (over), `finish` (out), at
  line granularity.
* **Memory inspection** — `locals`, `print <expr>`, and `inspect <expr>`
  (recursive field tree with depth limit and cycle budget). Expressions
  may call methods in the target (e.g. `user.getName().toUpperCase()`)
  and read statics (`com.foo.Config.MODE`).
* **Stack navigation** — `bt`/`frames`, `frame N`, `up`, `down`, `thread`.
* **gql trigger** — `gql <url> <query>` fires a GraphQL request to drive
  the app into the code path you are debugging (handy for e2e loops:
  set a breakpoint, fire the request, inspect the hit).
* **Agent mode** — `--json` emits one JSON document per event/result;
  `--exec` runs a command script instead of the REPL.
* **`ajd run`** — launch a local JVM with JDWP enabled and attach.

## Quick start

```bash
# interactive REPL
bin/ajd attach --host 127.0.0.1 --port 15555 --source-dir sources/my-app

# scripted, machine-readable (agent mode)
bin/ajd attach --port 15555 --json \
  --exec "bp add --file src/main/java/com/foo/UserService.java --line 42 \
          --condition 'id % 7 == 0' --name every-7th" \
  --exec "continue" \
  --exec "locals" \
  --exec "print user.getName()" \
  --exec "inspect user --depth 2"

# launch a local JVM and debug it
bin/ajd run --port 15556 -- -cp out Tick
```

(`python3 -m ajd` works the same if you prefer; `pip install .` provides
the `ajd` entry point.)

## REPL commands

```
bp add --class FQCN | --file PATH --line N
       [--name N] [--suspend all|thread|none] [--condition EXPR]
       [--thread ID|NAME] [--once]
bp list | remove <id|name> | enable <id|name> | disable <id|name> | clear
c/continue        resume and wait for the next stop
n/next            step over            s/step   step into
fin/finish        step out
threads           list threads (◉ = current)
thread <id|name>  select a thread
bt/frames         stack of the current thread
frame <n>|up|down select a frame
locals            variables of the selected frame
p/print EXPR      evaluate an expression in the target VM
inspect EXPR [--depth N]   deep-dive an object's fields
classes [pattern] list loaded classes
source            show source around the current line
gql URL QUERY [--vars JSON]   fire a GraphQL request
suspend | resume  pause / unpause the whole VM
vm                target VM info        quit/exit  detach
```

## Expression language (conditions, print, inspect)

Java-flavored, evaluated against the suspended frame:

* literals — ints, `long`-suffixed (`1L`), floats/doubles, `'c'`, `"str"`,
  `true/false`, `null`
* locals, `this`, fields of `this` (walking the superclass chain),
  statics via `com.foo.Bar.CONST` (longest loaded-class prefix match)
* instance calls `list.size()`, `s.substring(1)`, static calls
  `Math.abs(x)` — really invoked in the target with the suspended thread;
  overloads resolve by argument tags; a thrown target exception is
  reported as an EvalError
* arrays: `arr.length`, element formatting
* operators `+ - * / % < <= > >= == != && || !` with Java semantics
  (integer division truncates, `%` follows the dividend's sign);
  `==` compares primitives numerically, strings by value, objects by
  identity; `+` concatenates strings

## JSON event reference (agent mode)

```jsonc
{"event":"attach","host":"…","port":15555,"vm":{…},"classes":N,"threads":M}
{"type":"command","line":"bp add …"}                 // echo of each --exec
{"type":"breakpoint","action":"add","bp":{…},"state":"set at 1 location(s)"}
{"event":"trace","bp":{…},"thread":"…","location":{…}}          // tracepoint
{"event":"stop","reason":"breakpoint|step","thread":"main",
 "class":"Tick$Work","method":"tick","line":30,"file":"Tick.java",
 "bp":{"id":1,"name":"cond3"},
 "source":[{"line":27,"text":"…"},…]}                 // stop, with source window
{"type":"locals","thread":1,"frame":0,"this":{…},"vars":[{"name":"i","tag":"I","value":"3","raw":3,"object_id":null},…]}
{"type":"print","expression":"i","tag":"I","value":"3"}
{"type":"inspect","expression":"this","depth":2,"tree":["Tick$Work@0x20b","  name = 'w1'",…]}
{"type":"timeout","event":"no_stop"}                  // wait exceeded
{"type":"error","message":"…"}
{"event":"vm_death"}
```

## Testing

```bash
tests/run_sample.sh      # build & start tests/sample/Tick.java on :15556
tests/e2e_sample.sh      # 25 scenario assertions in --json mode
```

The e2e suite covers: class/file breakpoints (including inner classes and
pending resolution), global/thread/tracepoint suspensions, conditional
breakpoints (true/false/impossible), `--once`, thread filters, stepping,
locals, print, inspect, frames, and JSON output.

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
├── repl.py        command runner shared by REPL and --exec
└── cli.py         attach / gql / run subcommands
```

Design notes worth knowing:

* **One consumer thread** owns all event handling; commands are safe to
  issue from it (never from the reader thread — that would deadlock on
  its own reply).
* **Breakpoints removed while the VM is stopped on them** produce one
  ghost re-fire on resume (JVMTI defers the removal); the engine
  swallows it silently.
* **`continue`'s wait is bounded by one deadline** across all
  auto-resumed (condition-false) hits — a never-true condition times out
  instead of cycling forever.
* Frame ids are valid only for one stop: the frame cache is dropped on
  every resume and every new stop.
* **InvokeMethod packets carry a trailing `options` int (JDWP 1.6+)** —
  omitting it makes HotSpot return error 113 (INTERNAL) on every invoke.
  Verified byte-for-byte against jdb via a logging proxy.
* **A successful invoke renumbers the target thread's frame ids** (the
  invocation's wrapper frame shifts the stack); the frame cache must be
  dropped after every invoke or subsequent locals reads come back empty.

## License

MIT
