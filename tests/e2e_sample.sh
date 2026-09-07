#!/usr/bin/env bash
# ajd e2e scenarios against the sample Tick app (tests/run_sample.sh).
# Every scenario runs in --json mode; assertions grep the JSONL output.
# The target is restarted before each scenario so counters start at zero
# and assertions stay deterministic.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/tests/tmp"
PORT="${PORT:-15556}"
AJD=(python3 -m ajd attach --host 127.0.0.1 --port "$PORT" --json)
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

PASS=0; FAIL=0
check() { # check <label> <pattern> <file>
    if grep -q -- "$2" "$3"; then
        echo "  ok: $1"; PASS=$((PASS+1))
    else
        echo "  FAIL: $1 (pattern: $2)"; FAIL=$((FAIL+1))
    fi
}
check_not() { # check_not <label> <pattern> <file>
    if ! grep -q -- "$2" "$3"; then
        echo "  ok: $1"; PASS=$((PASS+1))
    else
        echo "  FAIL: $1 (pattern should be absent: $2)"; FAIL=$((FAIL+1))
    fi
}
restart_target() {
    # Kill the previous sample ONLY.  Pidfile first (verified against the
    # process command line), never a broad pkill pattern — the host may be
    # running other JVMs (IDE debug sessions etc.).
    if [ -f "$WORK/tick.pid" ]; then
        local PID
        PID="$(cat "$WORK/tick.pid" 2>/dev/null || true)"
        if [ -n "$PID" ] && ps -p "$PID" -o command= 2>/dev/null | grep -qF "$WORK Tick"; then
            kill "$PID" 2>/dev/null || true
            sleep 0.5
        fi
    fi
    "$ROOT/tests/run_sample.sh" >/dev/null || { echo "target failed to start" >&2; exit 1; }
}

# ---------------------------------------------------------------- scenario 1
echo "== scenario 1: class breakpoint, locals, print, step/next/finish"
restart_target
OUT="$WORK/s1.jsonl"
"${AJD[@]}" --timeout 25 \
  --exec "bp add --class Tick --line 16 --name main-loop" \
  --exec "continue" \
  --exec "locals" \
  --exec "print i" \
  --exec "print work" \
  --exec "step" \
  --exec "print this.name" \
  --exec "locals" \
  --exec "bt" \
  --exec "next" \
  --exec "finish" \
  > "$OUT" 2>&1
check "breakpoint hit with name" '"reason": "breakpoint".*"main-loop"' "$OUT"
check "locals has i" '"type": "locals".*"name": "i"' "$OUT"
check "print i = 0" '"type": "print".*"value": "0"' "$OUT"
check "print work is object" '"type": "print".*"value": "Tick\$Work@' "$OUT"
check "step into Work.tick" '"reason": "step".*"class": "Tick\$Work".*"method": "tick"' "$OUT"
check "print this.name = 'w1'" '"type": "print".*"value": "'"'"'w1'"'"'"' "$OUT"
check "frames list contains tick" '"type": "frames".*"method": "tick"' "$OUT"
check "next lands on msg line" '"reason": "step".*"line": 30' "$OUT"
check "finish returns to main" '"reason": "step".*"class": "Tick".*"method": "main"' "$OUT"

# ---------------------------------------------------------------- scenario 2
echo "== scenario 2: conditions (false skip + true stop), global suspend"
restart_target
OUT="$WORK/s2.jsonl"
"${AJD[@]}" --timeout 12 \
  --exec "bp add --file tests/sample/Tick.java --line 32 --condition 'i == 3' --name cond3" \
  --exec "continue" \
  --exec "print i" \
  --exec "print msg" \
  --exec "bp remove cond3" \
  --exec "bp add --file tests/sample/Tick.java --line 30 --condition 'i == 999' --name never" \
  --exec "continue" \
  --exec "bp remove never" \
  --exec "bp add --file tests/sample/Tick.java --line 30 --suspend all --condition 'total > 20' --name g5" \
  --exec "continue" \
  --exec "threads" \
  --exec "inspect this --depth 2" \
  > "$OUT" 2>&1
check "condition i==3 stops" '"reason": "breakpoint".*"cond3"' "$OUT"
check "print i == 3" '"type": "print".*"expression": "i".*"value": "3"' "$OUT"
check "print msg == w1#7" '"type": "print".*"value": "'"'"'w1#7'"'"'"' "$OUT"
check "impossible condition times out" '"type": "timeout"' "$OUT"
check "global suspend stops (field condition)" '"reason": "breakpoint".*"g5"' "$OUT"
check "all threads suspended" '"type": "threads".*"suspended": true' "$OUT"
check "inspect tree shows name field" '"type": "inspect".*name = '"'"'w1'"'"'' "$OUT"

# ---------------------------------------------------------------- scenario 3
echo "== scenario 3: file breakpoint (inner class), once, tracepoint"
restart_target
OUT="$WORK/s3.jsonl"
"${AJD[@]}" --timeout 12 \
  --exec "bp add --file tests/sample/Tick.java --line 30 --name f30" \
  --exec "continue" \
  --exec "print doubled" \
  --exec "bp remove f30" \
  --exec "bp add --file tests/sample/Tick.java --line 35 --suspend none --name tr" \
  --exec "bp add --file tests/sample/Tick.java --line 30 --condition 'i == 8' --name c8" \
  --exec "continue" \
  --exec "bp add --file tests/sample/Tick.java --line 36 --once --name one" \
  --exec "continue" \
  --exec "bp list" \
  > "$OUT" 2>&1
check "file bp resolves into inner class" '"reason": "breakpoint".*"class": "Tick\$Work".*"f30"' "$OUT"
check "print doubled = 1" '"type": "print".*"expression": "doubled".*"value": "1"' "$OUT"
check "tracepoint fired" '"event": "trace".*"tr"' "$OUT"
check "condition i==8 stops" '"reason": "breakpoint".*"c8"' "$OUT"
check "once bp hit" '"reason": "breakpoint".*"one"' "$OUT"
if grep '"type": "breakpoints"' "$OUT" | grep -q '"name": "one"'; then
    echo "  FAIL: once bp auto-removed from list"; FAIL=$((FAIL+1))
else
    echo "  ok: once bp auto-removed from list"; PASS=$((PASS+1))
fi
check "remaining bps still listed" '"name": "tr".*"name": "c8"' "$OUT"

# ---------------------------------------------------------------- scenario 4
echo "== scenario 4: thread filter"
restart_target
OUT="$WORK/s4.jsonl"
"${AJD[@]}" --timeout 25 \
  --exec "bp add --file tests/sample/Tick.java --line 30 --thread main --name tmain" \
  --exec "continue" \
  --exec "threads" \
  --exec "bp list" \
  > "$OUT" 2>&1
check "thread-filtered bp hits on main" '"reason": "breakpoint".*"thread": "main".*"tmain"' "$OUT"
check "bp list shows thread filter" '"thread": [0-9]' "$OUT"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
