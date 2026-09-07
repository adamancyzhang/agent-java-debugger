# gql e2e against a live pamirs-designer (reference target)

Verified 2026-09-07 against the docker `oinone-backend` container (JDWP on
host port 15555). The backend HTTP port 8091 is not published to the host,
so gql triggers go through `docker exec` — adapt to your environment.

## 1. Login and keep the session cookie

```bash
docker exec oinone-backend sh -c 'rm -f /tmp/cj.txt; curl -s -c /tmp/cj.txt \
  -X POST http://127.0.0.1:8091/pamirs/api -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { pamirsUserTransientMutation { login(user: { login: \\\"admin\\\", password: \\\"admin\\\" }) { errorCode } } }\"}"'
```

`errorCode: 0` = success; the cookie jar `/tmp/cj.txt` holds `pamirs_uc_session_id`.

## 2. gql request format (oinone)

```
{ <modelName>Query { <functionName>(args) { responseFields } } }
mutation { <modelName>Mutation { <functionName>(args) { ... } } }
```

`<modelName>` is the lower-camel model name (`action`, `ganttDemoModel`, …).
Built-in IdModel functions: `countByWrapper` (rsql filter), `queryPage`,
`queryOne`, `create`/`update`/`delete`. Example:

```bash
curl -s -b /tmp/cj.txt -X POST http://127.0.0.1:8091/pamirs/api \
  -H "Content-Type: application/json" \
  -d '{"query":"{ actionQuery { countByWrapper(queryWrapper: {rsql: \"1==1\"}) } }"}'
```

Schema discovery (partial introspection supported):
`{ __schema { types { name kind } } }` → 3833 types; `__type(name:)` is NOT
supported by the engine.

## 3. Breakpoint + trigger + inspect (the full loop)

Every gql function invocation resolves through
`DefaultFunctionResolverApi.resolveFunction(String funNamespace, String funName)`
(line 20 of the source file below) — the perfect per-request breakpoint:

```bash
SRC=sources/pamirs-designer/oinone-pamirs/pamirs-framework/pamirs-gateways-graph-java/src/main/java/pro/shushi/pamirs/framework/gateways/graph/java/request/DefaultFunctionResolverApi.java

agent-java-debugger attach --port 15555 --json --source-dir sources/pamirs-designer --timeout 90 \
  --exec "bp add --file $SRC --line 20 --condition 'funName.equals(\"countByWrapper\")' --suspend thread --name resolver-hit" \
  --exec "continue" \
  --exec "locals" \
  --exec "print funName.toUpperCase()" \
  --exec "finish" &
sleep 12   # let attach finish (39k classes)
docker exec oinone-backend sh -c 'curl -s -b /tmp/cj.txt -X POST http://127.0.0.1:8091/pamirs/api -H "Content-Type: application/json" -d "{\"query\":\"{ actionQuery { countByWrapper(queryWrapper: {rsql: \\\"1==1\\\"}) } }\"}"'
wait
```

The stop lands on the request's thread (`Deferred*` pool) with
`funName='countByWrapper'`, `funNamespace='base.Action'`, a ~70-frame stack
from `RequestController.pamirsPost` through the Spring AOP chain to
`DynamicGraphQLManager`, and the source window from the real source tree.
`finish` releases the request thread and the gql call returns normally.

Run the whole recipe with: `tests/e2e_pamirs.sh` (7 assertions, all green).
