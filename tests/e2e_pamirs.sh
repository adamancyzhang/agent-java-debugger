#!/usr/bin/env bash
# e2e against the live pamirs-designer JVM (docker oinone-backend, port 15555).
#
# Full chain: gql login (admin/admin) -> conditional breakpoint on the gql
# function resolver -> authenticated gql trigger -> inspect the hit ->
# step out.  Thread-level suspension only, so the live app keeps serving.
#
# The gql trigger is fired with docker exec curl because the backend HTTP
# port (8091) is not published to the host.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-15555}"
SRC="sources/pamirs-designer/oinone-pamirs/pamirs-framework/pamirs-gateways-graph-java/src/main/java/pro/shushi/pamirs/framework/gateways/graph/java/request/DefaultFunctionResolverApi.java"
OUT="$ROOT/tests/tmp/pamirs.jsonl"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

PASS=0; FAIL=0
check() {
    if grep -q -- "$2" "$3"; then
        echo "  ok: $1"; PASS=$((PASS+1))
    else
        echo "  FAIL: $1 (pattern: $2)"; FAIL=$((FAIL+1))
    fi
}
check_not() {
    if ! grep -q -- "$2" "$3"; then
        echo "  ok: $1"; PASS=$((PASS+1))
    else
        echo "  FAIL: $1 (pattern should be absent: $2)"; FAIL=$((FAIL+1))
    fi
}

if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "no JDWP target on 127.0.0.1:$PORT" >&2
    exit 1
fi

echo "== login via gql (admin/admin)"
docker exec oinone-backend sh -c 'rm -f /tmp/cj.txt; curl -s -c /tmp/cj.txt --max-time 15 -X POST "http://127.0.0.1:8091/pamirs/api" -H "Content-Type: application/json" -d "{\"query\":\"mutation { pamirsUserTransientMutation { login(user: { login: \\\"admin\\\", password: \\\"admin\\\" }) { errorCode } } }\"}"' \
    | grep -q '"errorCode":0' && { echo "  ok: login"; PASS=$((PASS+1)); } \
    || { echo "  FAIL: login"; FAIL=$((FAIL+1)); }

echo "== conditional breakpoint + gql trigger + inspect"
AJD=(python3 -m ajd attach --host 127.0.0.1 --port "$PORT" --json
     --source-dir "$ROOT/sources/pamirs-designer" --timeout 90)
"${AJD[@]}" \
  --exec "bp add --file $SRC --line 20 --condition 'funName.equals(\"countByWrapper\")' --suspend thread --name resolver-hit" \
  --exec "continue" \
  --exec "locals" \
  --exec "print funName.toUpperCase()" \
  --exec "finish" \
  > "$OUT" 2>&1 &
AJD_PID=$!
sleep 12
docker exec oinone-backend sh -c 'curl -s -b /tmp/cj.txt --max-time 15 -X POST "http://127.0.0.1:8091/pamirs/api" -H "Content-Type: application/json" -d "{\"query\":\"{ actionQuery { countByWrapper(queryWrapper: {rsql: \\\"1==1\\\"}) } }\"}"' \
    | grep -q 'countByWrapper' && { echo "  ok: gql trigger"; PASS=$((PASS+1)); } \
    || { echo "  FAIL: gql trigger"; FAIL=$((FAIL+1)); }
wait $AJD_PID

check "conditional bp hit (condition evaluated)" '"reason": "breakpoint".*"resolver-hit"' "$OUT"
check_not "no condition error" '"condition_error"' "$OUT"
check "locals show funName" '"type": "locals".*funName.*countByWrapper' "$OUT"
check "target-side invoke" '"type": "print".*COUNTBYWRAPPER' "$OUT"
check "step out returned" '"reason": "step".*DynamicGraphQLContext' "$OUT"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
