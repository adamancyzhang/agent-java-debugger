---
name: agent-java-debugger-oinone
description: On-demand extension content for agent-java-debugger — the oinone platform debugging companion. Covers the oinone gql protocol (model/functions, login, request format), the oinone CLI (login / exec / count with cookie-session handling), and ready-made debugging recipes for oinone apps (pamirs-designer, gql-driven backends): login and trigger requests, the per-request breakpoint on DefaultFunctionResolverApi, schema introspection, and the e2e trigger loop. Use when the user asks to debug or drive an oinone/pamirs application, invoke a model function or action via gql, log in to a pamirs backend, replay an export/action the user clicked in the UI, count or query records of a model, discover what models exist on the backend, or understand why an oinone request behaves unexpectedly. Triggers include requests to "call this model function", "trigger this export via API", "log in to the pamirs backend", or "drive the app instead of clicking". This is NOT a separately loaded skill — fetch it with `agent-java-debugger skills oinone` (works offline); pair with the base agent-java-debugger skill for all debugger mechanics.
allowed-tools: Bash(agent-java-debugger:*)
---

# agent-java-debugger — oinone companion

Everything needed to **drive and debug oinone platform apps** (pamirs-designer, gql-driven backends) with the agent-java-debugger CLI. For the debugger mechanics themselves (breakpoints, stepping, JSON events), see the base skill: `agent-java-debugger skills agent-java-debugger`.

## 1. The oinone gql protocol (from the frontend GenericFunctionService)

The backend exposes one POST endpoint (default `/pamirs/api`). Every request is a gql query/mutation built from a **model** (lower-camel gql name) and a **function name**:

```
{ <modelName>Query     { <functionName>(args) { responseFields } } }
mutation { <modelName>Mutation { <functionName>(args) { responseFields } } }
```

* Every `IdModel` model has built-in functions: `countByWrapper` (rsql filter), `queryPage`, `queryOne`, `queryListByWrapper`, `create`/`update`/`delete`, `construct`…
* Model names are lower-camel: model code `demo.gantt.GanttDemoModel` → gql name `ganttDemoModel`; the `Action` model → `action`.
* Long/BigDecimal values come back as strings (precision safety).
* Partial schema introspection works: `{ __schema { types { name kind } } }` (thousands of types); `__type(name:)` is not supported.
* Auth: session cookie `pamirs_uc_session_id` set by the login mutation.

## 2. The oinone CLI (agent-java-debugger oinone …)

Convenient wrappers over the protocol — same building blocks as the frontend `GenericFunctionService`, with cookie-session persistence (default jar `~/.ajd/cookies.txt`).

### login

```bash
agent-java-debugger oinone login --url http://host:8091/pamirs/api \
  --login admin --password admin [--pic-code CODE] [--cookie-jar FILE]
```

Performs `mutation { pamirsUserTransientMutation { login(user: {…}) { errorCode errorMsg broken errorField redirect { id } } } }`, stores the session cookie, prints the response JSON. `errorCode: 0` = success.

### exec — any model function

```bash
agent-java-debugger oinone exec --url http://host:8091/pamirs/api \
  --model action --function countByWrapper \
  --args '{"queryWrapper": {"rsql": "1==1"}}'

agent-java-debugger oinone exec --url … --model ganttDemoModel \
  --function queryPage \
  --args '{"cond": {"name": "x"}, "page": {"pageIndex": 0, "pageSize": 10}}' \
  --fields 'id code name taskStartDate' \
  [--mutation]   # for create/update/delete/action-style functions
```

* `--args` — a JSON object serialized into gql literals (`login: "admin"`, nested objects/arrays supported)
* `--fields` — response fields (space separated)
* `--mutation` — issue a mutation (create/update/delete/login…)

### count — the least intrusive probe

```bash
agent-java-debugger oinone count --url … --model action --rsql "1==1"
# → { actionQuery { countByWrapper: "N" } }
```

## 3. Debugging recipes (verified against a live pamirs-designer)

### 3.1 The per-request breakpoint

Every gql function invocation resolves through
`DefaultFunctionResolverApi.resolveFunction(String funNamespace, String funName)`
— a perfect, single choke point:

```bash
agent-java-debugger attach --port 15555 --json --source-dir sources/pamirs-designer --timeout 90 \
  --exec "bp add --file sources/pamirs-designer/oinone-pamirs/pamirs-framework/pamirs-gateways-graph-java/src/main/java/pro/shushi/pamirs/framework/gateways/graph/java/request/DefaultFunctionResolverApi.java --line 20 --condition 'funName.equals(\"countByWrapper\")' --suspend thread --name resolver-hit" \
  --exec "continue" \
  --exec "locals" \
  --exec "print funName.toUpperCase()" \
  --exec "finish" &
# trigger the request (see 3.2), then `wait` for the stop
```

The stop lands on the request's `Deferred*` pool thread with
`funName='countByWrapper'`, `funNamespace='base.Action'`, a ~70-frame stack
from `RequestController.pamirsPost` through the Spring AOP chain to
`DynamicGraphQLManager`, plus the source window. `finish` releases the
request thread and the gql call completes.

### 3.2 Triggering requests

The backend HTTP port is often NOT published to the host (docker `oinone-backend` publishes only the JDWP port). Fire triggers from inside the container:

```bash
docker exec oinone-backend sh -c 'curl -s -b /tmp/cj.txt -X POST http://127.0.0.1:8091/pamirs/api \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"{ actionQuery { countByWrapper(queryWrapper: {rsql: \\\"1==1\\\"}) } }\"}"'
```

### 3.3 Full e2e loop (login → conditional bp → trigger → inspect)

```bash
# 1. session cookie (once)
docker exec oinone-backend sh -c 'rm -f /tmp/cj.txt; curl -s -c /tmp/cj.txt \
  -X POST http://127.0.0.1:8091/pamirs/api -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { pamirsUserTransientMutation { login(user: { login: \\\"admin\\\", password: \\\"admin\\\" }) { errorCode } } }\"}"'

# 2. armed debugger waiting for the hit (background)
agent-java-debugger attach --port 15555 --json --timeout 90 \
  --exec "bp add … (3.1) …" --exec "continue" --exec "locals" --exec "finish" &

# 3. trigger
docker exec oinone-backend sh -c 'curl -s -b /tmp/cj.txt … countByWrapper …'
```

The recipe is scripted in `tests/e2e_pamirs.sh` (7 assertions, all green).
See also [references/gql-e2e.md](references/gql-e2e.md) for the full verified transcript.

## 4. Handy model/function probes

```bash
# is the backend healthy? (anonymous introspection)
agent-java-debugger gql --url http://host:8091/pamirs/api \
  --query '{ __schema { queryType { name } mutationType { name } } }'

# what models exist? (3833 types on a designer install)
agent-java-debugger gql --url … --query '{ __schema { types { name kind } } }'

# is my session valid?
agent-java-debugger oinone count --url … --model action        # not-logged-in error → re-login

# invoke a mutation-style function (create/update/action)
agent-java-debugger oinone exec --url … --model designerModel \
  --function create --args '{…}' --fields id --mutation
```
