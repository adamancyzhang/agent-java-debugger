#!/usr/bin/env bash
# Build and start the sample target with JDWP enabled (port from $PORT).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-15556}"
WORK="$ROOT/tests/tmp"
mkdir -p "$WORK"
# -g: full debug info (line numbers AND local variable tables)
javac -g -d "$WORK" "$ROOT/tests/sample/Tick.java"
# suspend=y: the JVM waits for the debugger before running main, so every
# scenario starts from a deterministic state (i == 0 on the first tick).
echo "starting Tick with JDWP on 127.0.0.1:$PORT (log: $WORK/tick.log)"
java -agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:$PORT \
     -cp "$WORK" Tick > "$WORK/tick.log" 2>&1 &
echo $! > "$WORK/tick.pid"
for i in $(seq 1 50); do
    nc -z 127.0.0.1 "$PORT" 2>/dev/null && exit 0
    sleep 0.2
done
echo "target did not open its JDWP port" >&2
exit 1
