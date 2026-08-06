# Agent tool calling (Memanto)

How to use **agent tool calling** from Memanto (CLI and HTTP).

Memanto runs a private agent runtime for you. You only need an active agent session, `MOORCHEH_API_KEY`, and Docker with the agent image configured.

This is separate from `memanto answer` / `answerMemory` (one-shot RAG). Agent runs can search the agent namespace, call **client tools** you define, pause until you continue, and keep run history.

---

## Prerequisites

1. Active session:
   ```bash
   memanto agent activate <agent-id>
   ```
2. `MOORCHEH_API_KEY` set (`memanto config set-api-key` or env)
3. Docker running
4. Agent image (set in Memanto config / env):
   ```bash
   MOORCHEH_AGENT_IMAGE=moorcheh/moorcheh-agent:latest
   docker pull moorcheh/moorcheh-agent:latest
   ```

Namespace is always `memanto_agent_<agent_id>`. Clients do not send the Moorcheh API key on these Memanto routes.

---

## CLI

```bash
# Search-only run (SSE on stdout)
memanto agent run "What is MIB?"

# With client tools
memanto agent run "Look up order ORD-42 using get_order." --tools tools.json

# After the agent asks for a tool (exit code 10):
memanto agent continue --run-id run_... --tool-result result.json

# Run history (not the same as `memanto agent list` / agent lifecycle)
memanto agent runs list
memanto agent runs get run_...
memanto agent runs get run_... --include-messages
memanto agent runs delete run_...
```

### Exit codes (`run` / `continue`)

| Code | Meaning |
|------|---------|
| `0` | Finished (`end_turn`) |
| `10` | Needs a client tool (`requires_client_tool`) |
| `1` | Error |

### Useful flags

```bash
memanto agent run "..." --top-k 5 --temperature 0.3 --ai-model us.anthropic.claude-sonnet-4-6
memanto agent run "..." --tools ./tools.json --quiet
memanto agent continue --run-id run_... --tool-result ./result.json
memanto agent runs list --limit 20
```

---

## HTTP API

Base: `/api/v2/agents/{agent_id}/…`  
Auth: `X-Session-Token` (session must match `{agent_id}`)

| Method | Path | Response |
|--------|------|----------|
| `POST` | `/api/v2/agents/{agent_id}/run` | SSE |
| `POST` | `/api/v2/agents/{agent_id}/runs/{run_id}/continue` | SSE |
| `GET` | `/api/v2/agents/{agent_id}/runs` | JSON |
| `GET` | `/api/v2/agents/{agent_id}/runs/{run_id}` | JSON |
| `DELETE` | `/api/v2/agents/{agent_id}/runs/{run_id}` | JSON |

### Start a run

```bash
curl -N -X POST "http://127.0.0.1:8000/api/v2/agents/my-agent/run" \
  -H "Content-Type: application/json" \
  -H "X-Session-Token: $SESSION" \
  -H "Accept: text/event-stream" \
  -d '{
    "query": "Say hello in one short sentence.",
    "top_k": 5,
    "temperature": 0.3
  }'
```

Body fields:

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `query` | yes | — | User question |
| `tools` | no | `null` | Client tool definitions array |
| `top_k` | no | `5` | Search depth (`1..50`) |
| `temperature` | no | `0.3` | `0..2` |
| `ai_model` | no | server default | Optional model id |

### Run with tools

```json
{
  "query": "Look up order ORD-42 using get_order. Do not invent the order.",
  "tools": [
    {
      "name": "get_order",
      "description": "Fetch an order by id. Call this when the user asks about an order.",
      "input_schema": {
        "type": "object",
        "properties": {
          "order_id": { "type": "string", "description": "Order identifier" }
        },
        "required": ["order_id"]
      }
    }
  ],
  "top_k": 5
}
```

### Continue after a tool

1. From SSE, take `run_id` and `tool_use_id`
2. Run the tool in your app
3. Continue:

```bash
curl -N -X POST "http://127.0.0.1:8000/api/v2/agents/my-agent/runs/run_abc/continue" \
  -H "Content-Type: application/json" \
  -H "X-Session-Token: $SESSION" \
  -H "Accept: text/event-stream" \
  -d '{
    "tool_result": {
      "tool_use_id": "tooluse_...",
      "content": "{\"order_id\":\"ORD-42\",\"status\":\"shipped\",\"total\":49.99}",
      "status": "success"
    }
  }'
```

`tool_use_id` must match the pending tool.

### List / get / delete

```bash
curl "http://127.0.0.1:8000/api/v2/agents/my-agent/runs?limit=20" \
  -H "X-Session-Token: $SESSION"

curl "http://127.0.0.1:8000/api/v2/agents/my-agent/runs/run_abc" \
  -H "X-Session-Token: $SESSION"

curl "http://127.0.0.1:8000/api/v2/agents/my-agent/runs/run_abc?include=messages" \
  -H "X-Session-Token: $SESSION"

curl -X DELETE "http://127.0.0.1:8000/api/v2/agents/my-agent/runs/run_abc" \
  -H "X-Session-Token: $SESSION"
```

---

## SSE events (`run` / `continue`)

```text
event: <name>
data: { ... }

```

| Event | Meaning |
|-------|---------|
| `run_started` | Run accepted (`run_id`, `namespace`, `model`) |
| `status` | Phase (`searching`, `thinking`, …) |
| `search_completed` | Namespace search finished |
| `tool_use` | Client tool requested (`tool_use_id`, `name`, `input`) |
| `message` | Assistant text |
| `error` | Failure |
| `done` | End of this request (`stop_reason`, `usage`) |

### `stop_reason` on `done`

| Value | What to do |
|-------|------------|
| `end_turn` | Finished |
| `requires_client_tool` | Execute tool → `continue` |
| `error` | Handle failure |

---

## Client tool loop

```text
run (with tools)
  → tool_use + done (requires_client_tool)
  → you execute the tool
  → continue with tool_result
  → message + done (end_turn)
     (or another tool_use)
```

Example `tools.json`:

```json
[
  {
    "name": "get_order",
    "description": "Fetch an order by id from a mock store.",
    "input_schema": {
      "type": "object",
      "properties": {
        "order_id": { "type": "string" }
      },
      "required": ["order_id"]
    }
  }
]
```

Example `result.json` for continue:

```json
{
  "tool_use_id": "tooluse_...",
  "content": "{\"order_id\":\"ORD-42\",\"status\":\"shipped\",\"total\":49.99}",
  "status": "success"
}
```

---

## vs answerMemory

| | `answer` / `answerMemory` | Agent tool calling |
|--|---------------------------|--------------------|
| Purpose | One-shot RAG | Multi-step agent + tools |
| Transport | JSON | SSE + JSON history |
| Client tools | No | Yes |
| Run history | No | Yes |
