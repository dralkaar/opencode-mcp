# opencode-mcp

**English** | [中文](README.zh-CN.md)

An MCP (Model Context Protocol) stdio server implemented in the **pure Python 3 standard library**, for driving **opencode** conversations programmatically: create sessions, send prompts, wait for replies, handle permission requests and forms, compact context, and manage multiple opencode servers.

Zero third-party dependencies. All capabilities are live-tested against opencode v2.0.12, including a real remote end-to-end run.

## Design in one paragraph

MCP is a stdio JSON-RPC 2.0 server (newline-delimited, not LSP Content-Length framing). It speaks to opencode's HTTP API over a connection layer. Local opencode is either connected explicitly via `OPENCODE_URL` or **spawned by the MCP itself** (random high port, random password, child process lifetime). Remote opencode instances are registered dynamically with `connect_server` and addressed by alias; sessions remember which connection they live on and route automatically. Terminal states are judged solely from the authoritative `Session.outcome` field — never from message-shape heuristics.

## Connection model: MCP-spawned local + multi-server

**No inferential service discovery (including `service.json`).** The local connection takes one of two forms:

1. **Explicit direct connect**: when `OPENCODE_URL` is set, `local` points at that address (password from `OPENCODE_PASSWORD`, default `opencode`).
2. **MCP-spawned serve** (default): the first time `local` is needed, the MCP spawns its own `opencode serve` — **random high port + random password** injected via `OPENCODE_SERVER_PASSWORD`. It runs as a child process and dies with the MCP instance; multiple MCP instances never collide thanks to the random ports. If `opencode` is not on `PATH`, the call fails with an availability error (user environment issue, no retries).

**Multi-server**: `connect_server(name, url, password_file?/password_env?/password?)` registers a remote connection (process-lifetime only, never persisted). Every tool accepts an optional `server` parameter (defaults to `local`); calls carrying a `session_id` are routed automatically to the connection that owns that session. Credential priority: **file > env > plaintext**; when no credential source is given, no `Authorization` header is sent (some remotes accept no auth).

**Version baseline and failure classification**: every connection is hard-gated at creation (unreachable = `[availability]`; reachable but not an opencode API = `[compatibility]`). After a request failure the version is re-queried and the failure is classified as `[availability] / [compatibility] / [other]` — `other` carries the full original error for reporting. When a server version differs from the development baseline, a single `api_version_warning` is injected into the first tool result touching each (connection, session, version) pair — new sessions see it, the same session is never spammed.

**Permission defaults**: local `chat` defaults to `auto_permission="once"`; **remote connections default to `manual`** (approval must live with the caller); `once/always/reject` can always be chosen explicitly.

### Environment variables

- `OPENCODE_URL`: explicit local address (skips spawning), e.g. `http://127.0.0.1:4096`.
- `OPENCODE_PASSWORD`: HTTP Basic password for the explicit local connection; username is always `opencode`, default password `opencode`.
- `OPENCODE_MCP_WORKERS`: worker threads for request handling, default `4` (set to `1` for strict serialization).
- `OPENCODE_MCP_BASELINE_VERSION`: overrides the development baseline (default `2.0.12`, mainly for testing).

## Concurrency and cancellation

- **Concurrency**: each JSON-RPC request is handled in its own worker thread (`OPENCODE_MCP_WORKERS`, default 4). Long-blocking calls such as `chat` / `wait_session` never block other tool calls.
- **Cancellation**: MCP-standard `notifications/cancelled` is honored. Cancelling `chat` / `wait_session` stops polling within 1 second (the request no longer writes a response; a single in-flight HTTP request can take up to its 30-second timeout to unwind).

## Version baseline and warnings

Covered by the connection-model section above: per-connection hard gate at creation, version re-query and failure classification after errors, and warnings de-duplicated per (connection, session, version). Baseline defaults to `2.0.12`, overridable with `OPENCODE_MCP_BASELINE_VERSION`.

## Model selection

- This MCP **never pins models on the caller's behalf**: `create_session` without `model_id` leaves `model=null`, and the run falls back to the **location default model** (`GET /api/model/default`).
- Measured behavior (opencode v2.0.12): the location default does **not** follow agent configuration — the TUI and opencode's internal spawning pin models explicitly at session creation, while bare-API sessions fall back to the location default. Pass `model_id` (`providerID/modelID`) explicitly when you need a specific model.
- **Reading the agent → model mapping**: use the `list_agents` tool (backed by `GET /api/agent`, plugin-resolved). To align a session with an agent's model, read the mapping and pass `model_id` yourself.

## Registering with opencode

```bash
opencode mcp add opencode-local -- python3 /root/opencode-mcp/server.py
```

## Tools (16)

All tools accept an optional `server` parameter. Tool results are JSON text with a stable `status` vocabulary: `succeeded / failed / interrupted / needs_permission / needs_form / timeout / cancelled / compaction_failed`.

### 1. `create_session`

Create a session. Optional `title`, `agent`, `model_id` (`providerID/modelID`), `location` (`{"directory": "..."}`). Returns `session_id`, the owning `server`, plus title/agent/model as reported by the API.

### 2. `chat`

Send a prompt and wait for the turn to reach a terminal state.

Parameters: `session_id` (required), `text` (required), `timeout_secs?` (default 120, clamped 1–3600), `auto_permission?` (defaults: local `once`, remote `manual`), `delivery?` (`steer` redirects a running generation, `queue` waits for the current turn to finish), `files?` (array of `{uri(required), name?, description?}`).

Statuses: `succeeded` (with `assistant_text`, `tools_used`, optional `reasoning`, `last_message_id`), `failed`, `interrupted`, `needs_permission` (with `requests`), `needs_form`, `timeout` (with `partial_text` and `diagnostics`).

### 3. `wait_session`

**Pure state primitive**: waits until the session reaches a terminal or blocked state. Never returns message content and never answers permissions itself.

Returns `succeeded / failed / interrupted` (from the authoritative `outcome`), `needs_permission` (`requests`), `needs_form` (`forms` with field key/title/type/required/options/description), or `timeout` (`diagnostics`). Always includes `last_message_id` as the incremental cursor.

Typical composition: after answering a permission or form, `wait_session` until terminal, then pull new content with `get_messages(after_message_id=...)`.

### 4. `get_messages`

Messages in ascending time order, formatted (role, text, tool summaries, timestamps). Supports incremental pulls: pass `after_message_id` (the previous `last_message_id`) to get only newer messages; the response returns a fresh `last_message_id`.

Note: the underlying API windows on the **tail** of the conversation (the newest N messages), which keeps long sessions correct.

### 5. `permission_reply`

Answer a permission request: `decision` = `once` (allow this time), `always` (allow and save the rule), `reject`. Optional `message`.

### 6. `form_reply`

Submit a form answer: `answer` is an object keyed by field `key`; values may be string / number / boolean / string[].

### 7. `list_agents`

Read-only listing of all agents and their resolved default models (`model: null` means no explicit model; the run falls back to the location default). This tool provides information only — it never pins models for you.

### 8. `interrupt`

Interrupt the session's current generation. Useful after `chat` returns `timeout`.

### 9. `pending_interactions`

Non-blocking check for pending permission requests and forms: `{server, session_id, permissions, forms}`.

### 10. `list_sessions`

Enumerate / search existing sessions: `search?`, `limit?` (default 20), `order?` (`asc|desc`, default `desc`), `directory?`, `cursor?`. Returns `{count, sessions: [{id, title, agent, model, parentID, time}], cursor}` — combine with `chat` to resume earlier conversations.

### 11. `compact`

Compact the session context and wait for completion. Statuses: `succeeded`, `compaction_failed`, `timeout`. Useful when context approaches the limit; you can continue chatting afterwards.

### 12. `get_context`

Context usage and metadata: `{server, id, title, agent, model, parentID, tokens, cost, time, revert}` (`tokens` / `cost` may be null). Pair with `compact` to decide whether to shrink.

### 13. `delete_session`

Delete a session. ⚠️ **Irreversible**, and **cascades to child sessions** (children return 404 once the parent is deleted).

### 14. `connect_server`

Register and validate a remote opencode connection (process-lifetime only, never persisted). Validation at creation: unreachable = availability error; no version = compatibility error; version mismatch returns a warning. Credentials: `password_file` (first line) > `password_env` > plaintext `password`; with none given, no `Authorization` header is sent. Returns `{name, url, version, baseline, baseline_check}`.

### 15. `list_servers`

List all current connections (local + dynamic remotes): name, URL, source, version, and baseline-check status. Ensures the local connection is ready (spawning it if needed).

### 16. `disconnect_server`

Remove a dynamically registered remote connection (`local` cannot be removed). Session routes belonging to it are cleared.

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

## Verified flows (opencode v2.0.12)

1. **Create + chat**: `create_session` → `chat` → `succeeded` with `assistant_text` / `tools_used`.
2. **Manual permission loop**: `chat(auto_permission="manual")` → `needs_permission` → `permission_reply(decision="once")` → `wait_session` → terminal.
3. **Form pipeline**: `chat` / `wait_session` returns `needs_form` (with field details) → `form_reply` → `wait_session` → terminal.
4. **Automatic permission**: default `auto_permission="once"` approves and continues; a single `chat` returns `succeeded`.
5. **Connection layer**: explicit-env and MCP-spawned local paths; spawned serve carries a real conversation and dies with its parent; failure classification (dead port = availability, HTML-only service = compatibility, 401 = credentials); duplicate-name and unknown-server errors; session auto-routing; disconnect rules.
6. **Real remote end-to-end**: `connect_server` with plaintext credentials → remote `create_session` → remote `chat` → remote manual-permission default (blocked, not auto-approved) → `permission_reply` → `wait_session` → incremental `get_messages` → `disconnect_server`.
7. **Long-session window**: on a 200+ message session, tail-window fetching keeps gate lookup, incremental cursors and `last_message_id` correct.
8. **Cancellation & concurrency**: `notifications/cancelled` stops polling within 1s; concurrent `pending_interactions` returns in milliseconds while `chat` is in flight.

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
