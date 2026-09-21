# opencode-mcp

**English** | [中文](README.zh-CN.md)

## Why this exists

Until agents commonly speak ACP (Agent Client Protocol) directly to opencode, driving opencode over its **HTTP API** through MCP is the best available route for an agent to operate opencode — and this server is built for exactly that: every tool maps to a clean, machine-consumable state machine (authoritative terminal states, blocking interaction states, incremental cursors) rather than a UI replica, which makes it a native fit for agent orchestration.

Tested in real use with:

- **opencode v2.0.12** — the development baseline; every capability is live-verified against it
- **a real remote instance** — full end-to-end over the network (connect → create → chat → manual permission → reply → wait → incremental fetch → disconnect)
- **the Hermes agent gateway** — mounted as a tool provider and used in production
- **the oh-my-opencode-slim agent orchestration framework** — hosts the MCP and drives nested opencode sessions through it

## Scope

This is **not** a complete control surface for every aspect of opencode, and it does not try to be. It is deliberately scoped to what an agent actually needs for day-to-day opencode interaction:

- create and resume sessions, send prompts, collect results
- handle the two blocking interactions — permission requests and forms
- manage context (usage inspection, compaction) and session lifecycle
- route across multiple opencode servers (local and remote)

Management-plane surfaces — filesystem, credentials, providers, plugins, terminals, config — are intentionally out of scope. Fewer tools, sharper semantics, less to get wrong.

## Install with your LLM

Paste this into your coding agent:

```text
Install and register opencode-mcp for me:

1. Clone: git clone https://github.com/yitro-z-wang/opencode-mcp ~/opencode-mcp
2. Verify: run `python3 ~/opencode-mcp/test_client.py` — it must report 16 tools and pass.
3. Register with opencode: `opencode mcp add opencode-local -- python3 ~/opencode-mcp/server.py`
4. Reload opencode config, then start a new session and confirm the 16 opencode-local tools are available.

Requirements: Python 3.10+ and the opencode CLI on PATH (v2.0.12 is the development baseline).
Report any errors verbatim; do not retry blindly.
```

## Features

- **Zero dependencies** — pure Python 3 standard library, one file, no build step.
- **Multi-server** — MCP-spawned local serve (random port + random password, dies with the MCP) or explicit `OPENCODE_URL`; remote instances registered dynamically via `connect_server`; sessions remember their server and route automatically.
- **Authoritative state, no guessing** — terminal states come from opencode's `Session.outcome` field, not message-shape heuristics.
- **Failure classification** — every failure is classified `[availability] / [compatibility] / [other]`, with the raw error preserved for reporting; version mismatches surface once per (connection, session, version).
- **Safe defaults** — remote `chat` defaults to manual permission approval; credentials use `file > env > plaintext` priority and no `Authorization` header is sent when none is configured.
- **Composable primitives** — `wait_session` (pure state) and `get_messages` (incremental cursor) separate waiting from reading.
- **Production-grade runtime** — concurrent request handling, MCP-standard cancellation, bounded waits.

## Tools

| Tool | What it does |
| --- | --- |
| `create_session` | Create a session (optional title / agent / model / location) |
| `chat` | Send a prompt and wait for the terminal state; supports file attachments and `steer` / `queue` delivery; can auto-answer permission requests |
| `wait_session` | Pure state wait: `succeeded` / `failed` / `interrupted` / `needs_permission` / `needs_form` / `timeout` |
| `get_messages` | Read the transcript; incremental pulls via `after_message_id` |
| `permission_reply` | Answer a permission request: `once` / `always` / `reject` |
| `form_reply` | Submit a form answer keyed by field |
| `list_agents` | List agents and their resolved default models (read-only) |
| `interrupt` | Stop the current generation |
| `pending_interactions` | Non-blocking check for pending permissions / forms |
| `list_sessions` | Enumerate / search sessions — the resume handle for earlier conversations |
| `compact` | Compact context and wait for completion |
| `get_context` | Token / cost usage and session metadata |
| `delete_session` | Delete a session (irreversible; cascades to child sessions) |
| `connect_server` | Register and validate a remote opencode connection |
| `list_servers` | List connections with version and baseline status |
| `disconnect_server` | Remove a dynamically registered remote connection |

All tools accept an optional `server` parameter; calls carrying a `session_id` are routed automatically to the connection that owns that session.

## Connection model: MCP-spawned local + multi-server

**No inferential service discovery (including `service.json`).** The local connection takes one of two forms:

1. **Explicit direct connect**: when `OPENCODE_URL` is set, `local` points at that address (password from `OPENCODE_PASSWORD`, default `opencode`).
2. **MCP-spawned serve** (default): the first time `local` is needed, the MCP spawns its own `opencode serve` — **random high port + random password** injected via `OPENCODE_SERVER_PASSWORD`. It runs as a child process and **dies with the MCP instance**: exactly one serve is spawned per MCP process (a per-process singleton), and it is killed and reaped on stdin EOF (normal host shutdown) and on `SIGTERM` / `SIGINT` / `SIGHUP`. Multiple MCP instances never collide thanks to the random ports — and never accumulate, since each restart cleans up after itself. The one gap is `SIGKILL`, which no process can intercept on any platform: a hard-killed MCP can leave one stale serve behind (it is not adopted later — no inferential discovery, by design). If `opencode` is not on `PATH`, the call fails with an availability error (user environment issue, no retries).

**Multi-server**: `connect_server(name, url, password_file?/password_env?/password?)` registers a remote connection (process-lifetime only, never persisted). Credential priority: **file > env > plaintext**; when no credential source is given, no `Authorization` header is sent (some remotes accept no auth).

**Version baseline and failure classification**: every connection is hard-gated at creation (unreachable = `[availability]`; reachable but not an opencode API = `[compatibility]`). After a request failure the version is re-queried and the failure is classified as `[availability] / [compatibility] / [other]` — `other` carries the full original error for reporting. When a server version differs from the development baseline, a single `api_version_warning` is injected into the first tool result touching each (connection, session, version) pair — new sessions see it, the same session is never spammed.

**Permission defaults**: local `chat` defaults to `auto_permission="once"`; **remote connections default to `manual`** (approval must live with the caller); `once/always/reject` can always be chosen explicitly.

### Environment variables

- `OPENCODE_URL`: explicit local address (skips spawning), e.g. `http://127.0.0.1:4096`.
- `OPENCODE_PASSWORD`: HTTP Basic password for the explicit local connection; username is always `opencode`, default password `opencode`.
- `OPENCODE_MCP_WORKERS`: worker threads for request handling, default `4` (set to `1` for strict serialization).
- `OPENCODE_MCP_BASELINE_VERSION`: overrides the development baseline (default `2.0.12`, mainly for testing).

## Concurrency and cancellation

- Each JSON-RPC request is handled in its own worker thread (`OPENCODE_MCP_WORKERS`, default 4). Long-blocking calls such as `chat` / `wait_session` never block other tool calls.
- MCP-standard `notifications/cancelled` is honored: cancelling `chat` / `wait_session` stops polling within 1 second (a single in-flight HTTP request can take up to its 30-second timeout to unwind).

## Model selection

- This MCP **never pins models on the caller's behalf**: `create_session` without `model_id` leaves `model=null`, and the run falls back to the **location default model** (`GET /api/model/default`).
- Measured (opencode v2.0.12): the location default does **not** follow agent configuration — the TUI and opencode's internal spawning pin models explicitly at session creation, while bare-API sessions fall back to the location default. Pass `model_id` (`providerID/modelID`) explicitly when you need a specific model.
- **Reading the agent → model mapping**: use `list_agents` (backed by `GET /api/agent`, plugin-resolved). To align a session with an agent's model, read the mapping and pass `model_id` yourself.

## Permission and form flows

### Automatic mode (default locally)

`chat` with the default `auto_permission="once"` answers permission requests automatically and keeps waiting — usually one call returns `succeeded`.

### Manual mode

1. `chat(auto_permission="manual")` returns `needs_permission` with a `requests` list:
   ```json
   {
     "status": "needs_permission",
     "server": "local",
     "session_id": "ses_abc",
     "requests": [{"id": "per_...", "action": "shell", "resources": ["echo hi"], "save": ["echo *"]}]
   }
   ```
2. Decide per request and call `permission_reply(session_id, request_id, decision, message?)`.
3. Call `wait_session(session_id)` to continue. New permission requests surface as `needs_permission` again; the terminal status is `succeeded / failed / interrupted`, after which `get_messages(after_message_id=...)` pulls the incremental reply.

> If a request was already handled elsewhere, `permission_reply` may error — just call `wait_session` or `pending_interactions` to re-check state.

### Form flow

1. `chat` / `wait_session` returns `needs_form` with field details.
2. Answer with `form_reply(form_id, answer)`.
3. Call `wait_session` to continue until terminal.

## Permission rules and action naming

Permission actions match tool names (measured: `shell`, `bash`, `edit`, `write`, `read`, `glob`, `grep`, `webfetch`, `external_directory`, ...); `resource` is the command text or path pattern (e.g. `*`). Rule `effect` is one of `allow / deny / ask`.

To force `ask` on a session (e.g. to exercise the manual flow), include a ruleset at creation — via the raw API, since `create_session` does not pass permissions through:

```bash
curl -u "opencode:$PASSWORD" -X POST "$URL/api/session" -H 'Content-Type: application/json' \
  -d '{"title": "perm test", "permissions": [{"action": "shell", "resource": "*", "effect": "ask"}]}'
```

Then `chat(..., auto_permission="manual")` deterministically exercises `needs_permission`; answer with `permission_reply` and continue with `wait_session`.

## Verified flows

All verified live against opencode v2.0.12:

1. **Create + chat**: `create_session` → `chat` → `succeeded` with `assistant_text` / `tools_used`.
2. **Manual permission loop**: `chat(auto_permission="manual")` → `needs_permission` → `permission_reply(decision="once")` → `wait_session` → terminal.
3. **Form pipeline**: `chat` / `wait_session` returns `needs_form` (with field details) → `form_reply` → `wait_session` → terminal.
4. **Automatic permission**: default `auto_permission="once"` approves and continues; a single `chat` returns `succeeded`.
5. **Connection layer**: explicit-env and MCP-spawned local paths; spawned serve carries a real conversation and dies with its parent; failure classification (dead port = availability, HTML-only service = compatibility, 401 = credentials); duplicate-name and unknown-server errors; session auto-routing; disconnect rules.
6. **Real remote end-to-end**: `connect_server` over the network → remote `create_session` → remote `chat` → remote manual-permission default (blocked, not auto-approved) → `permission_reply` → `wait_session` → incremental `get_messages` → `disconnect_server`.
7. **Long-session window**: on a 200+ message session, tail-window fetching keeps gate lookup, incremental cursors and `last_message_id` correct.
8. **Cancellation & concurrency**: `notifications/cancelled` stops polling within 1s; concurrent `pending_interactions` returns in milliseconds while `chat` is in flight.
9. **Host integrations**: mounted as a tool provider in the Hermes agent gateway; hosted by the oh-my-opencode-slim orchestration framework to drive nested opencode sessions.

## Testing

```bash
python3 test_client.py                 # handshake + tools/list assertion (16 tools)
python3 test_client.py --chat "hello"  # adds a real create_session + chat round-trip
```

The smoke test spawns its own server process; with `OPENCODE_URL`/`OPENCODE_PASSWORD` unset it exercises the MCP-spawned local path directly.

## Files

- `server.py` — the MCP server (stdlib only).
- `test_client.py` — smoke test client.
- `README.md` / `README.zh-CN.md` — this document, English and Chinese.
- `DESIGN-remote-connections.md` — design record for the multi-connection model.
