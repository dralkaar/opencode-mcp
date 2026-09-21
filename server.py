#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp — MCP (Model Context Protocol) stdio server implemented with the pure Python standard library.

Drives the conversation capabilities of a local opencode (Session / Prompt / permission / form / interrupt).
Zero third-party dependencies; Python 3 standard library only.

Transport: MCP over stdio, one JSON-RPC 2.0 message per line (newline-delimited, not LSP Content-Length framing).
Logs go to stderr; protocol messages go to stdout.
"""

import base64
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_NAME = "opencode-mcp"
SERVER_VERSION = "1.0.0"

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_PASSWORD = "opencode"

HTTP_TIMEOUT = 30.0
POLL_INTERVAL = 1.0

VALID_AUTO_PERMISSION = ("once", "always", "reject", "manual")


def log(*parts):
    """Write logs to stderr to avoid polluting the stdout protocol stream."""
    try:
        sys.stderr.write(" ".join(str(p) for p in parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class OpenCodeError(Exception):
    """Interaction with opencode failed. kind ∈ {availability, compatibility, other}.

    other must carry the raw error so it can be reported to the developer verbatim.
    """

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Connection layer: multiple opencode servers (MCP-spawned local serve + dynamic remotes)
# Local: explicit direct connection via OPENCODE_URL (skips the spawn); otherwise the MCP spawns a dedicated serve
# (random high port + random password; the child process lives as long as this MCP instance).
# No inferential service discovery of any kind (including service.json).
# ---------------------------------------------------------------------------

DEVELOPMENT_BASELINE_VERSION = (
    os.environ.get("OPENCODE_MCP_BASELINE_VERSION") or "2.0.12"
)
DEFAULT_LOCAL_NAME = "local"
SESSION_ROUTE_LIMIT = 1000

_CONNECTIONS = {}  # name -> Connection (guarded by _STATE_LOCK)
_SESSION_ROUTE = {}  # session_id -> connection name (insertion order; oldest evicted past the limit)
_LOCAL_LOCK = threading.Lock()
# Serial registration lock: eliminates the check-then-write race for concurrent connects with the same name (registration is infrequent)
_REGISTER_LOCK = threading.Lock()


class Connection:
    """A single opencode server connection."""

    def __init__(self, name, base_url, password, is_local=False, source="dynamic"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self.is_local = is_local
        self.source = source  # spawned / env / dynamic
        self.server_version = None
        self.sessions_warned = {}  # session_id -> server_version at warning time
        self.spawned_proc = None

    def auth_header(self):
        if not self.password:
            return None  # No credential source: do not send Authorization (some remotes use an empty username/password)
        token = base64.b64encode(
            ("opencode:" + self.password).encode("utf-8")
        ).decode("ascii")
        return "Basic " + token

    def default_auto_permission(self):
        return "once" if self.is_local else "manual"

    def describe(self):
        if self.server_version == DEVELOPMENT_BASELINE_VERSION:
            check = "ok"
        elif self.server_version:
            check = "mismatch(%s)" % self.server_version
        else:
            check = "unknown"
        return {
            "name": self.name,
            "url": self.base_url,
            "local": self.is_local,
            "source": self.source,
            "version": self.server_version,
            "baseline": DEVELOPMENT_BASELINE_VERSION,
            "baseline_check": check,
        }


def _raw_probe(conn, timeout=8.0):
    """Raw GET /api/info, returns the parsed dict; network failure raises a connection exception, a non-JSON response raises ValueError."""
    req = urllib.request.Request(conn.base_url + "/api/info")
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Response is not JSON: %s" % exc)


def _ensure_version(conn):
    """Creation-time check (hard gate).

    Unreachable = availability; reachable but /api/info is non-JSON or returns 404/5xx = compatibility (not the opencode API);
    401 = authentication problem (wrong password).
    """
    try:
        info = _raw_probe(conn)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise OpenCodeError(
                "[availability] %s(%s) authentication rejected (401): wrong password or password changed"
                % (conn.name, conn.base_url),
                kind="availability",
            )
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info returned HTTP %s, which is not the opencode API"
            " (looks like some other service; baseline v%s)"
            % (conn.name, conn.base_url, exc.code, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except ValueError:
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info did not return opencode v2 API JSON"
            " (observed cases: without correct credentials the request falls back to the Web UI, or this is another service; baseline v%s. "
            "If this is confirmed to be opencode, check the password)"
            % (conn.name, conn.base_url, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except Exception as exc:
        raise OpenCodeError(
            "[availability] Cannot connect to opencode server %s(%s): %s"
            % (conn.name, conn.base_url, exc),
            kind="availability",
        )
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise OpenCodeError(
            "[compatibility] %s is reachable but /api/info has no version field; the API looks completely reworked"
            " (development baseline v%s)" % (conn.name, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    conn.server_version = info["version"]
    return info


def _spawn_local_serve():
    """Spawn a dedicated local serve: random high port + random password.

    No opencode on PATH → an availability error that states the user's environment problem; no retry.
    Child-process model: lives as long as this MCP instance; multiple instances use random ports and do not conflict.
    """
    if shutil.which("opencode") is None:
        raise OpenCodeError(
            "[availability] No opencode command on PATH, cannot spawn the local server. "
            "Install opencode or add it to PATH and retry (a user environment problem; the MCP will not try again).",
            kind="availability",
        )
    last_err = None
    for _ in range(3):
        port = random.randint(20000, 60000)
        password = (
            base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
        )
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = password
        try:
            proc = subprocess.Popen(
                ["opencode", "serve", "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            last_err = exc
            continue
        conn = Connection(
            DEFAULT_LOCAL_NAME,
            "http://127.0.0.1:%d" % port,
            password,
            is_local=True,
            source="spawned",
        )
        conn.spawned_proc = proc
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # Port in use, etc.: retry with another port
            try:
                info = _raw_probe(conn, timeout=2.0)
                if isinstance(info, dict) and info.get("version"):
                    conn.server_version = info["version"]
                    return conn
            except Exception:
                pass
            time.sleep(0.3)
        proc.kill()
        try:
            proc.wait(timeout=5)  # Reap the child process to avoid a zombie
        except Exception:
            pass
    raise OpenCodeError(
        "[availability] Failed to spawn the local opencode serve (none of 3 random ports became ready): %s"
        % last_err,
        kind="availability",
    )


def _local_connection():
    """Local connection: explicit env direct connection, otherwise spawn a dedicated serve; a per-process singleton."""
    with _LOCAL_LOCK:
        with _STATE_LOCK:
            conn = _CONNECTIONS.get(DEFAULT_LOCAL_NAME)
        if conn is not None:
            return conn
        url = os.environ.get("OPENCODE_URL")
        if url:
            conn = Connection(
                DEFAULT_LOCAL_NAME,
                url,
                os.environ.get("OPENCODE_PASSWORD") or "opencode",
                is_local=True,
                source="env",
            )
            _ensure_version(conn)
        else:
            conn = _spawn_local_serve()
        with _STATE_LOCK:
            _CONNECTIONS[DEFAULT_LOCAL_NAME] = conn
        log(
            "[opencode-mcp] local connection ready:",
            conn.base_url,
            "version=",
            conn.server_version,
        )
        return conn


def _register_connection(name, base_url, password):
    if not name or not isinstance(name, str):
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("Connection name %r is reserved and cannot be used" % name)
    # Fully serial: eliminates the check-then-write race in concurrent registration of the same name (registration is infrequent, so serial is fine)
    with _REGISTER_LOCK:
        with _STATE_LOCK:
            if name in _CONNECTIONS:
                raise OpenCodeError(
                    "Connection name already exists: %s (see list_servers)" % name
                )
        conn = Connection(name, base_url, password, is_local=False, source="dynamic")
        _ensure_version(conn)  # Creation-time check (hard gate)
        with _STATE_LOCK:
            _CONNECTIONS[name] = conn
    return conn


def _resolve_connection(args):
    """server parameter > session routing > local. An unknown name errors and lists the existing connections."""
    name = args.get("server")
    session_id = args.get("session_id")
    if not name and session_id:
        with _STATE_LOCK:
            name = _SESSION_ROUTE.get(session_id)
    if not name or name == DEFAULT_LOCAL_NAME:
        return _local_connection()
    with _STATE_LOCK:
        conn = _CONNECTIONS.get(name)
        names = sorted(_CONNECTIONS) or [DEFAULT_LOCAL_NAME]
    if conn is None:
        raise OpenCodeError(
            "Unknown connection %r. Existing connections: %s (a remote must be registered with connect_server first)"
            % (name, ", ".join(names))
        )
    return conn


def _route_session(session_id, conn):
    if not session_id:
        return
    with _STATE_LOCK:
        _SESSION_ROUTE[session_id] = conn.name
        while len(_SESSION_ROUTE) > SESSION_ROUTE_LIMIT:
            _SESSION_ROUTE.pop(next(iter(_SESSION_ROUTE)))


def _remove_connection(name):
    with _STATE_LOCK:
        conn = _CONNECTIONS.pop(name, None)
        if conn is not None:
            stale = [sid for sid, n in _SESSION_ROUTE.items() if n == name]
            for sid in stale:
                _SESSION_ROUTE.pop(sid, None)
    return conn


# Version warning: per (connection, session), deduplicated by the version already warned (visible to new sessions, no flooding within the same session)


def _version_warning_for(conn):
    if not conn.server_version:
        return None
    if conn.server_version == DEVELOPMENT_BASELINE_VERSION:
        return None
    major = (
        conn.server_version.split(".")[0]
        != DEVELOPMENT_BASELINE_VERSION.split(".")[0]
    )
    return {
        "server": conn.name,
        "baseline": DEVELOPMENT_BASELINE_VERSION,
        "current": conn.server_version,
        "severity": "high" if major else "low",
        "message": "opencode server (%s) version %s does not match the MCP development baseline %s; %s, behavior may differ."
        % (
            conn.name,
            conn.server_version,
            DEVELOPMENT_BASELINE_VERSION,
            "different major version, high compatibility risk" if major else "minor version difference",
        ),
    }


def _warn_for_result(conn, session_id, result):
    if session_id is None or not isinstance(result, dict):
        return result
    warning = _version_warning_for(conn)
    if not warning:
        return result
    with _STATE_LOCK:
        if conn.sessions_warned.get(session_id) == conn.server_version:
            return result
        conn.sessions_warned[session_id] = conn.server_version
        while len(conn.sessions_warned) > 2000:
            conn.sessions_warned.pop(next(iter(conn.sessions_warned)))
    result = dict(result)
    result["api_version_warning"] = warning
    return result


def _warn_choke(args, result):
    """Unified warning injection point for the tools/call success path: per (connection, session)."""
    if not isinstance(result, dict):
        return result
    session_id = args.get("session_id") or result.get("session_id")
    if not session_id:
        return result
    try:
        conn = _resolve_connection(dict(args, session_id=session_id))
    except OpenCodeError:
        return result
    return _warn_for_result(conn, session_id, result)


def unwrap(payload):
    """opencode responses are usually {"data": ...}; uniformly extract data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------------------
# Concurrency and cancellation (MCP: notifications/cancelled + request thread pool)
# ---------------------------------------------------------------------------

# Set of request ids cancelled by the caller (written by the reader thread, read by poll threads)
_CANCELLED = set()
_STATE_LOCK = threading.Lock()
# stdout single-writer lock: multi-threaded responses must be written serially
_OUT_LOCK = threading.Lock()
# Request id currently handled by the worker thread (thread-local)
_CURRENT = threading.local()


def _request_cancelled(request_id):
    with _STATE_LOCK:
        return request_id in _CANCELLED


def _current_request_cancelled():
    request_id = getattr(_CURRENT, "request_id", None)
    return request_id is not None and _request_cancelled(request_id)


# ---------------------------------------------------------------------------
# HTTP (with connection context and failure classification)
# ---------------------------------------------------------------------------


def http_request(conn, method, path, body=None, query=None):
    """Perform a request against the given connection; on failure classify as availability / compatibility / other (dump the raw error)."""
    url = conn.base_url + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url = url + "?" + urllib.parse.urlencode(clean)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _classify_failure(
            conn,
            method,
            path,
            "HTTP %s %s %s -> %s %s"
            % (exc.code, method, path, exc.reason, detail[:1500]),
            http_status=exc.code,
        )
    except Exception as exc:  # URLError / timeout, etc.
        raise _classify_failure(
            conn, method, path, "%s: %s" % (type(exc).__name__, exc)
        )

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OpenCodeError(
            "[other] Response is not valid JSON (%s %s): %s" % (method, path, exc)
        )


def _classify_failure(conn, method, path, original, http_status=None):
    """After a failure, re-probe the version and classify as availability / compatibility / other accordingly.

    The primary criterion is the shape of the original failure (GET 404 = endpoint gone = compatibility; network layer = availability),
    the version re-probe corroborates and updates the connection's version record; other must preserve the full raw error for reporting.
    """
    probe = None
    probe_err = None
    try:
        probe = _raw_probe(conn, timeout=5.0)
    except Exception as exc:
        probe_err = exc
    if isinstance(probe, dict) and probe.get("version"):
        conn.server_version = probe["version"]
    ctx = "server=%s version=%s baseline=%s" % (
        conn.name,
        conn.server_version,
        DEVELOPMENT_BASELINE_VERSION,
    )
    if http_status in (401, 403):
        raise OpenCodeError(
            "[availability] %s authentication rejected (HTTP %s): wrong or expired password (%s). Original: %s"
            % (conn.name, http_status, ctx, original),
            kind="availability",
        )
    if http_status == 404 and method == "GET":
        raise OpenCodeError(
            "[compatibility] Endpoint gone (GET %s -> 404); the API looks reworked (%s). Original: %s"
            % (path, ctx, original),
            kind="compatibility",
        )
    if probe is None:
        raise OpenCodeError(
            "[availability] Service unreachable (%s); re-probing /api/info also failed: %s. Original: %s"
            % (conn.base_url, probe_err, original),
            kind="availability",
        )
    if not (isinstance(probe, dict) and probe.get("version")):
        raise OpenCodeError(
            "[compatibility] Re-probe of /api/info has no version field; the API looks reworked (%s). Original: %s"
            % (ctx, original),
            kind="compatibility",
        )
    raise OpenCodeError(
        "[other] Request failed (%s). Raw error: %s. Can be reported to the developer verbatim."
        % (ctx, original),
        kind="other",
    )


# ---------------------------------------------------------------------------
# Data shaping helpers
# ---------------------------------------------------------------------------

def _text_parts(message):
    """Get the text of all text parts in an assistant message."""
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _reasoning_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "reasoning":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _tool_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool":
                state = part.get("state") or {}
                out.append(
                    {
                        "name": part.get("name"),
                        "status": state.get("status"),
                    }
                )
    return out


def _message_text(message):
    """Get the readable text of any message."""
    if isinstance(message.get("text"), str):
        return message["text"]
    return "\n".join(_text_parts(message))


def _is_pending_form(item):
    """List items may carry state.status; only pending counts as outstanding; no state counts as pending."""
    state = item.get("state")
    if not isinstance(state, dict):
        return True
    return state.get("status") == "pending"


def _form_summary(item):
    fields = []
    for field in item.get("fields") or []:
        if not isinstance(field, dict):
            continue
        fields.append(
            {
                "key": field.get("key"),
                "title": field.get("title"),
                "type": field.get("type"),
                "required": field.get("required", False),
                "options": field.get("options"),
                "description": field.get("description"),
            }
        )
    return {
        "id": item.get("id"),
        "sessionID": item.get("sessionID"),
        "title": item.get("title"),
        "fields": fields,
    }


def _format_message(message):
    mtype = message.get("type")
    created = (message.get("time") or {}).get("created")
    result = {
        "id": message.get("id"),
        "type": mtype,
        "time": created,
    }
    if mtype == "assistant":
        result["agent"] = message.get("agent")
        result["model"] = message.get("model")
        result["text"] = "\n".join(_text_parts(message))
        reasoning = _reasoning_parts(message)
        if reasoning:
            result["reasoning"] = reasoning
        tools = _tool_parts(message)
        if tools:
            result["tools"] = tools
        result["completed"] = bool((message.get("time") or {}).get("completed"))
    else:
        result["text"] = _message_text(message)
        if mtype == "shell":
            result["command"] = message.get("command")
    return result


def fetch_messages(conn, session_id, limit=100):
    """Fetch the **latest** limit messages of a session, returned in ascending time order.

    In practice opencode's order=asc&limit returns the "earliest N" (verified on 200+ message sessions),
    which misaligns gate lookup / incremental cursors on long sessions; so we uniformly use order=desc to take the tail window and then reverse it.
    """
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/message" % urllib.parse.quote(session_id, safe=""),
        query={"order": "desc", "limit": limit},
    )
    data = unwrap(payload)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        data = data["messages"]
    if not isinstance(data, list):
        return []
    data.reverse()
    return data


def fetch_permissions(conn, session_id):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/permission" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    return data if isinstance(data, list) else []


def fetch_forms(conn, session_id, pending_only=True):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/form" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    if not isinstance(data, list):
        return []
    if pending_only:
        data = [item for item in data if _is_pending_form(item)]
    return data


# ---------------------------------------------------------------------------
# Unified wait core (shared by chat / wait_session / compact)
# Terminal determination trusts only the authoritative field Session.outcome (succeeded/failed/interrupted) + the gate message
# timestamp; no message-shape inference (five historical rounds of bugs all came from shape heuristics, now fully removed).
# ---------------------------------------------------------------------------


def _build_result(messages):
    assistant_texts = []
    reasoning_texts = []
    tools_used = []
    for message in messages:
        if message.get("type") != "assistant":
            continue
        text = "\n".join(_text_parts(message))
        if text:
            assistant_texts.append(text)
        reasoning_texts.extend(_reasoning_parts(message))
        tools_used.extend(_tool_parts(message))
    result = {
        "assistant_text": "\n\n".join(assistant_texts),
        "tools_used": tools_used,
    }
    if reasoning_texts:
        result["reasoning"] = reasoning_texts
    return result


def _result_payload(conn, session_id, status, baseline=None, with_result=False, time_idle=None):
    """Terminal response body; last_message_id always provides the incremental cursor, and with_result attaches this round's new replies."""
    try:
        messages = fetch_messages(conn, session_id)
    except OpenCodeError:
        messages = []
    payload = {
        "status": status,
        "server": conn.name,
        "session_id": session_id,
        "time_idle": time_idle,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if with_result:
        new = [
            m
            for m in messages
            if baseline is None or m.get("id") not in (baseline or set())
        ]
        payload.update(_build_result(new))
        if not any(m.get("type") == "assistant" for m in new):
            payload.setdefault(
                "note", "Session is already completed/idle; no new replies this round."
            )
    return payload


def _run_until_terminal(
    conn,
    session_id,
    timeout_secs,
    auto_permission="manual",
    gate_message_id=None,
    gate_is_compaction=False,
    baseline=None,
    with_result=False,
):
    """Poll the session until terminal / needs interaction / timeout / cancellation (the unified wait core).

    Returns (status, payload). status ∈ {succeeded, failed, interrupted,
    compaction_failed, needs_permission, needs_form, timeout, cancelled}
    """
    started = time.monotonic()
    gate_created = None
    while True:
        if _current_request_cancelled():
            return "cancelled", {"status": "cancelled", "note": "The caller cancelled this request"}

        # a. Permission requests
        try:
            permissions = fetch_permissions(conn, session_id)
        except OpenCodeError:
            permissions = []
        if permissions:
            if auto_permission in ("once", "always", "reject"):
                for req in permissions:
                    rid = req.get("id")
                    if not rid:
                        continue
                    try:
                        http_request(
                            conn,
                            "POST",
                            "/api/session/%s/permission/%s/reply"
                            % (
                                urllib.parse.quote(session_id, safe=""),
                                urllib.parse.quote(rid, safe=""),
                            ),
                            body={"decision": auto_permission},
                        )
                    except OpenCodeError as exc:
                        log("[opencode-mcp] permission auto-reply failed", rid, exc)
            else:
                return "needs_permission", {
                    "status": "needs_permission",
                    "server": conn.name,
                    "session_id": session_id,
                    "requests": [
                        {
                            "id": p.get("id"),
                            "action": p.get("action"),
                            "resources": p.get("resources"),
                            "save": p.get("save"),
                        }
                        for p in permissions
                    ],
                    "note": "Reply with permission_reply, then call wait_session to keep waiting.",
                }

        # b. Form requests
        try:
            forms = fetch_forms(conn, session_id, pending_only=True)
        except OpenCodeError:
            forms = []
        if forms:
            return "needs_form", {
                "status": "needs_form",
                "server": conn.name,
                "session_id": session_id,
                "forms": [_form_summary(f) for f in forms],
                "note": "Reply with form_reply, then call wait_session to keep waiting.",
            }

        # c. Authoritative session state
        info = unwrap(
            http_request(
                conn,
                "GET",
                "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
            )
        )
        if not isinstance(info, dict):
            info = {}
        outcome = info.get("outcome")
        time_idle = (info.get("time") or {}).get("idle")

        # Gate message: cache the created timestamp; in the compact case accept its message terminal state directly
        if gate_message_id is not None and gate_created is None:
            try:
                gate_msgs = fetch_messages(conn, session_id)
            except OpenCodeError:
                gate_msgs = []
            for gm in gate_msgs:
                if gm.get("id") == gate_message_id:
                    gate_created = (gm.get("time") or {}).get("created")
                    if gate_is_compaction:
                        gstatus = gm.get("status")
                        if gstatus == "completed":
                            return "succeeded", _result_payload(
                                conn, session_id, "succeeded", baseline, with_result, time_idle
                            )
                        if gstatus == "failed":
                            return "compaction_failed", {
                                "status": "compaction_failed",
                                "server": conn.name,
                                "session_id": session_id,
                                "note": "Context compaction failed (compaction status=failed).",
                            }
                    break

        if outcome in ("succeeded", "failed", "interrupted"):
            gate_ok = gate_message_id is None or (
                gate_created is not None and (time_idle or 0) > gate_created
            )
            if gate_ok:
                return outcome, _result_payload(
                    conn, session_id, outcome, baseline, with_result, time_idle
                )

        # d. Timeout (the diagnostics block includes the last message and this round's partial text)
        if time.monotonic() - started >= timeout_secs:
            try:
                msgs = fetch_messages(conn, session_id)
            except OpenCodeError:
                msgs = []
            last = msgs[-1] if msgs else None
            new = [
                m
                for m in msgs
                if baseline is None or m.get("id") not in (baseline or set())
            ]
            return "timeout", {
                "status": "timeout",
                "server": conn.name,
                "session_id": session_id,
                "partial_text": _build_result(new)["assistant_text"],
                "diagnostics": {
                    "server": conn.name,
                    "outcome": outcome,
                    "last_message": (
                        {
                            "id": last.get("id"),
                            "type": last.get("type"),
                            "status": last.get("status"),
                            "completed": bool(
                                (last.get("time") or {}).get("completed")
                            ),
                        }
                        if isinstance(last, dict)
                        else None
                    ),
                    "pending_permissions": len(permissions),
                    "pending_forms": len(forms),
                    "suggested_actions": [
                        "get_messages to check current progress",
                        "pending_interactions to check pending interactions",
                        "wait_session to keep waiting",
                        "interrupt to stop generation",
                    ],
                },
                "note": "Wait timed out (%s seconds); the session is still generating." % timeout_secs,
            }

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _arg_timeout(args, default=120):
    """Parse the optional timeout_secs parameter and clamp it to [1, 3600] seconds."""
    value = int(args.get("timeout_secs", default) or default)
    return max(1, min(value, 3600))


def tool_create_session(args):
    conn = _resolve_connection(args)
    body = {}
    if args.get("title"):
        body["title"] = args["title"]
    if args.get("agent"):
        body["agent"] = args["agent"]
    model_id = args.get("model_id")
    if model_id:
        if "/" not in model_id:
            raise OpenCodeError(
                "model_id must be in 'providerID/modelID' format, got: %r" % model_id
            )
        provider_id, model = model_id.split("/", 1)
        # Model.Ref shape is {"id": ..., "providerID": ...} (observed: the "modelID" key is rejected with 400)
        body["model"] = {"providerID": provider_id, "id": model}

    location = args.get("location")
    if location is not None:
        if not isinstance(location, dict):
            raise OpenCodeError(
                "location must be an object of the form {\"directory\": \"/path/to/project\"}"
            )
        directory = location.get("directory")
        if not isinstance(directory, str) or not directory:
            raise OpenCodeError("location.directory is a required string")
        # Pass through in the Location.PublicRef shape from openapi.json: {directory}
        body["location"] = {"directory": directory}

    data = unwrap(http_request(conn, "POST", "/api/session", body=body))
    if not isinstance(data, dict):
        data = {}
    _route_session(data.get("id"), conn)
    return {
        "session_id": data.get("id"),
        "server": conn.name,
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
    }


def tool_delete_session(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "DELETE",
        "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
    )
    with _STATE_LOCK:
        _SESSION_ROUTE.pop(session_id, None)
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_chat(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    # Remote connections default to manual (approval must stay with the caller), local defaults to once
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()
    if auto_permission not in VALID_AUTO_PERMISSION:
        raise OpenCodeError(
            "auto_permission must be one of once/always/reject/manual, got: %r"
            % auto_permission
        )

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))

    text = args.get("text")
    if text is None or text == "":
        raise OpenCodeError("Missing required parameter text")
    body = {"text": text}

    delivery = args.get("delivery")
    if delivery is not None:
        if delivery not in ("steer", "queue"):
            raise OpenCodeError(
                "delivery must be steer / queue, got: %r" % delivery
            )
        body["delivery"] = delivery

    files = args.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise OpenCodeError("files must be an array, e.g. [{\"uri\": \"...\"}]")
        for idx, item in enumerate(files):
            if not isinstance(item, dict) or not item.get("uri"):
                raise OpenCodeError("files[%d] is missing the required field uri" % idx)
        if files:
            body["files"] = files

    prompt_payload = unwrap(
        http_request(
            conn,
            "POST",
            "/api/session/%s/prompt" % urllib.parse.quote(session_id, safe=""),
            body=body,
        )
    )
    gate_id = (
        prompt_payload.get("id") if isinstance(prompt_payload, dict) else None
    )

    _route_session(session_id, conn)
    status, payload = _run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        baseline=baseline,
        with_result=True,
    )
    return payload


def tool_wait_session(args):
    """Wait for the session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content)."""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    _route_session(session_id, conn)
    status, payload = _run_until_terminal(
        conn, session_id, timeout_secs, auto_permission="manual", with_result=False
    )
    if status == "succeeded" and "note" not in payload:
        payload["note"] = "Use get_messages(after_message_id=...) to fetch new replies."
    return payload


def tool_get_messages(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    limit = int(args.get("limit", 50) or 50)
    after = args.get("after_message_id")
    note = None
    _route_session(session_id, conn)
    if after:
        # Incremental fetch: take at most the latest 200, drop after_message_id and everything before it
        messages = fetch_messages(conn, session_id, limit=200)
        idx = next(
            (i for i, m in enumerate(messages) if m.get("id") == after), -1
        )
        if idx >= 0:
            messages = messages[idx + 1 :]
        else:
            note = "after_message_id is not among the latest 200 messages; returned the full list instead."
        if len(messages) > limit:
            messages = messages[-limit:]
    else:
        messages = fetch_messages(conn, session_id, limit=limit)
    messages = sorted(
        messages, key=lambda m: (m.get("time") or {}).get("created") or 0
    )
    formatted = [_format_message(m) for m in messages]
    result = {
        "server": conn.name,
        "session_id": session_id,
        "count": len(formatted),
        "messages": formatted,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if note:
        result["note"] = note
    return result


def tool_permission_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    request_id = args.get("request_id")
    decision = args.get("decision")
    if not session_id or not request_id:
        raise OpenCodeError("Missing required parameter session_id / request_id")
    if decision not in ("once", "always", "reject"):
        raise OpenCodeError(
            "decision must be once / always / reject, got: %r" % decision
        )
    body = {"decision": decision}
    if args.get("message"):
        body["message"] = args["message"]
    http_request(
        conn,
        "POST",
        "/api/session/%s/permission/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(request_id, safe=""),
        ),
        body=body,
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "request_id": request_id, "decision": decision}


def tool_form_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    form_id = args.get("form_id")
    answer = args.get("answer")
    if not session_id or not form_id:
        raise OpenCodeError("Missing required parameter session_id / form_id")
    if not isinstance(answer, dict):
        raise OpenCodeError("answer must be an object, e.g. {\"fieldKey\": value}")
    http_request(
        conn,
        "POST",
        "/api/session/%s/form/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(form_id, safe=""),
        ),
        body={"answer": answer},
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "form_id": form_id, "answer": answer}


def tool_interrupt(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "POST",
        "/api/session/%s/interrupt" % urllib.parse.quote(session_id, safe=""),
    )
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_list_agents(args):
    conn = _resolve_connection(args)
    data = unwrap(http_request(conn, "GET", "/api/agent"))
    if not isinstance(data, list):
        data = []
    agents = []
    for agent in data:
        if not isinstance(agent, dict):
            continue
        agents.append(
            {
                "name": agent.get("name"),
                "mode": agent.get("mode"),
                "model": agent.get("model"),
            }
        )
    return {
        "server": conn.name,
        "count": len(agents),
        "agents": agents,
        "note": "model being null means the agent has no explicitly configured model (it falls back to the position default model at runtime). "
        "If a session should use a particular agent's model, the caller passes providerID/modelID to create_session's model_id.",
    }


def tool_pending_interactions(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    permissions = fetch_permissions(conn, session_id)
    forms = [_form_summary(f) for f in fetch_forms(conn, session_id, pending_only=True)]
    return {
        "server": conn.name,
        "session_id": session_id,
        "permissions": permissions,
        "forms": forms,
    }


def _session_time(raw):
    """Get updated / idle from the time field of Session.Info."""
    info = raw.get("time") or {}
    result = {"updated": info.get("updated")}
    if "idle" in info:
        result["idle"] = info.get("idle")
    return result


def tool_list_sessions(args):
    conn = _resolve_connection(args)
    query = {
        "search": args.get("search"),
        "limit": args.get("limit", 20),
        "order": args.get("order", "desc"),
        "directory": args.get("directory"),
        "cursor": args.get("cursor"),
    }
    payload = http_request(conn, "GET", "/api/session", query=query)
    data = unwrap(payload)
    sessions = []
    cursor = {}
    if isinstance(data, list):
        sessions = data
    elif isinstance(data, dict):
        sessions = data.get("sessions") or data.get("data") or []
        cursor = data.get("cursor") or {}
    out = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "agent": s.get("agent"),
                "model": s.get("model"),
                "parentID": s.get("parentID"),
                "time": _session_time(s),
            }
        )
    return {
        "server": conn.name,
        "count": len(out),
        "sessions": out,
        "cursor": {
            "previous": cursor.get("previous"),
            "next": cursor.get("next"),
        },
    }


def tool_compact(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))
    compact_payload = http_request(
        conn,
        "POST",
        "/api/session/%s/compact" % urllib.parse.quote(session_id, safe=""),
        body={},
    )
    gate_id = (
        compact_payload.get("data", {}).get("id")
        if isinstance(compact_payload, dict)
        else None
    )
    _route_session(session_id, conn)
    status, payload = _run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        gate_is_compaction=True,
        baseline=baseline,
        with_result=True,
    )
    return payload


def tool_get_context(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    data = unwrap(
        http_request(
            conn,
            "GET",
            "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
        )
    )
    if not isinstance(data, dict):
        data = {}
    return {
        "server": conn.name,
        "id": data.get("id"),
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
        "parentID": data.get("parentID"),
        "tokens": data.get("tokens"),
        "cost": data.get("cost"),
        "time": {
            "updated": (data.get("time") or {}).get("updated"),
            "idle": (data.get("time") or {}).get("idle"),
        },
        "revert": data.get("revert"),
    }

def tool_connect_server(args):
    """Register and validate a remote connection (creation-time hard gate). Valid only within this process; not persisted."""
    name = args.get("name")
    url = args.get("url")
    if not name or not url:
        raise OpenCodeError("Missing required parameter name / url")

    password = None
    source = None
    password_file = args.get("password_file")
    password_env = args.get("password_env")
    if password_file:
        try:
            with open(password_file, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
        except Exception as exc:
            raise OpenCodeError(
                "[other] Failed to read password_file (%s): %s" % (password_file, exc)
            )
        if not first:
            raise OpenCodeError(
                "[other] password_file first line is empty (%s)" % password_file
            )
        password = first
        source = "file"
    if password is None and password_env:
        password = os.environ.get(password_env)
        if not password:
            raise OpenCodeError(
                "[availability] Environment variable %s is not set or is empty" % password_env,
                kind="availability",
            )
        source = "env"
    if password is None and args.get("password"):
        password = args["password"]
        source = "plaintext"

    conn = _register_connection(name, url, password)
    result = conn.describe()
    result["password_source"] = source or "none"
    warning = _version_warning_for(conn)
    if warning:
        result["api_version_warning"] = warning
    return result


def tool_list_servers(args):
    _local_connection()  # Ensure the local connection is ready (spawn or direct connect)
    with _STATE_LOCK:
        conns = list(_CONNECTIONS.values())
    return {
        "count": len(conns),
        "servers": [c.describe() for c in conns],
    }


def tool_disconnect_server(args):
    name = args.get("name")
    if not name:
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("The local connection cannot be removed")
    conn = _remove_connection(name)
    if conn is None:
        raise OpenCodeError("Connection does not exist: %s (see list_servers)" % name)
    if conn.spawned_proc is not None:
        try:
            conn.spawned_proc.kill()
        except Exception:
            pass
    return {"ok": True, "removed": name}




# ---------------------------------------------------------------------------
# Tool catalog (schema + descriptions)
# ---------------------------------------------------------------------------

# Shared parameter schemas (read-only reuse; for serialization, never mutated)
_SHARED_SERVER_PARAM = {
    "type": "string",
    "description": "(optional) target connection name, defaults to local; a call with a session_id is auto-routed to the connection that created it",
}
_SHARED_SESSION_ID_PARAM = {"type": "string", "description": "Session ID (ses_...)"}
_SHARED_TIMEOUT_PARAM = {
    "type": "integer",
    "description": "Maximum wait in seconds, default 120",
    "default": 120,
}


TOOLS = [
    {
        "name": "create_session",
        "description": (
            "Create a new conversation session on the local opencode. Returns session_id (ses_...), "
            "which later tools such as chat / get_messages use. Optionally specify a title, agent, model, "
            "and location (to create the session at a given directory/project location)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "title": {"type": "string", "description": "Session title (optional)"},
                "agent": {"type": "string", "description": "Name of the agent to use (optional)"},
                "model_id": {
                    "type": "string",
                    "description": "Model in providerID/modelID format, e.g. \"anthropic/claude-sonnet-4\" (optional)",
                },
                "location": {
                    "type": "object",
                    "description": "Session location (optional), used to create the session in a given directory/project. Passed through in the opencode Location.PublicRef shape.",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Absolute path of the working directory (required)",
                        }
                    },
                    "required": ["directory"],
                    "additionalProperties": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "chat",
        "description": (
            "Send a prompt to the given session and wait for opencode to reply. Internally polls messages, "
            "permission requests and form requests. With auto_permission=once/always/reject it answers permission requests automatically; "
            "with manual it returns immediately on a permission request, and you must call permission_reply + wait_session again. "
            "On a form request it returns needs_form, and you must call form_reply + wait_session. "
            "Optional delivery: steer=steer directly while running (interrupts the current generation direction), "
            "queue=queue it to take effect after this round ends; if omitted the field is not sent. "
            "Optional files: an array of files attached to the prompt, each {uri (required), name?, description?}. "
            "Returns status: succeeded (success, with assistant_text/tools_used/reasoning), "
            "failed (failure), interrupted (interrupted), "
            "needs_permission (waiting for authorization, with a requests list), needs_form (waiting for form input), "
            "timeout (timed out, with partial_text and diagnostics)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "text": {"type": "string", "description": "Prompt text to send"},
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
                "auto_permission": {
                    "type": "string",
                    "enum": ["once", "always", "reject", "manual"],
                    "description": "How to handle permission requests. Local connections default to once (allow this time); remote connections default to manual (approval must stay with the caller). You may explicitly set once/always/reject/manual.",
                    "default": "once",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["steer", "queue"],
                    "description": "Delivery mode (optional). steer=steer directly while running (interrupts the current generation direction), queue=queue it to take effect after this round ends; if omitted the field is not sent.",
                },
                "files": {
                    "type": "array",
                    "description": "Array of files to send with the prompt (optional).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "description": "File URI (required)"},
                            "name": {"type": "string", "description": "File name (optional)"},
                            "description": {"type": "string", "description": "File description (optional)"},
                        },
                        "required": ["uri"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["session_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_session",
        "description": (
            "Wait for the given session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content). "
            "Terminal status: succeeded (this round ended successfully) / failed (failure) / interrupted (interrupted), "
            "taken from the authoritative session field outcome; blocking states: needs_permission / needs_form (call again after replying); "
            "timeout means it was still generating when the wait timed out. Returns last_message_id as the get_messages incremental cursor, "
            "to be used with get_messages(after_message_id=...) to fetch new replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_messages",
        "description": (
            "Fetch session message records in ascending time order, formatting user/assistant text, tool-call summaries and timestamps. "
            "Supports incremental fetch: pass after_message_id (the last_message_id returned previously, or any message id), "
            "and only messages after it are returned; the response includes last_message_id for the next cursor. "
            "Typical combination: wait_session until terminal, then use this to fetch new replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return, default 50",
                    "default": 50,
                },
                "after_message_id": {
                    "type": "string",
                    "description": "Incremental cursor (optional): only return new messages after this one",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "permission_reply",
        "description": (
            "Answer a permission request. decision=once allows this time only, always allows always and saves it, "
            "reject denies it. Optional message is an attached note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "request_id": {"type": "string", "description": "Permission request ID (per_...)"},
                "decision": {
                    "type": "string",
                    "enum": ["once", "always", "reject"],
                    "description": "Authorization decision",
                },
                "message": {"type": "string", "description": "Optional explanatory message"},
            },
            "required": ["session_id", "request_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "form_reply",
        "description": (
            "Submit the answer for a form. answer is an object whose keys are field keys; values may be "
            "string / number / boolean / string[]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "form_id": {"type": "string", "description": "Form ID (frm_...)"},
                "answer": {
                    "type": "object",
                    "description": "Answer object, e.g. {\"name\": \"foo\", \"count\": 3}",
                    "additionalProperties": True,
                },
            },
            "required": ["session_id", "form_id", "answer"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List all agents of the local opencode and their resolved default models (read-only). "
            "model being null means it is not explicitly configured (it falls back to the position default model). "
            "If a session should match a particular agent's model, the caller passes that model in providerID/modelID "
            "format to create_session's model_id; this tool only provides information and does not pin the model for the caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "interrupt",
        "description": "Interrupt the generation currently in progress in the given session. Useful to cancel a long-running task after chat returns timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_interactions",
        "description": (
            "Query the human interactions currently pending in the given session, returning lists of permissions (permission requests) and forms. "
            "Use it to learn, without blocking, whether the session is waiting for authorization or form input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "Enumerate / search existing sessions; supports keywords, ordering, directory filtering and cursor pagination. "
            "Useful for finding past topics; once you have a session_id, use it with chat to resume the previous conversation (the session_id is the resume handle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "search": {"type": "string", "description": "Keyword to search by title/content (optional)"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results, default 20",
                    "default": 20,
                },
                "order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Order by update time, default desc (newest first)",
                    "default": "desc",
                },
                "directory": {"type": "string", "description": "Filter by working directory (optional)"},
                "cursor": {"type": "string", "description": "Pagination cursor, taken from the cursor.next returned previously (optional)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "Compact the context of the given session, wait for the compaction to finish and return the result. "
            "Returns status: succeeded (compaction complete), compaction_failed (compaction failed), "
            "timeout (timed out). Useful to proactively trim when the context nears its limit; you can keep chatting afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context",
        "description": (
            "View the context usage (tokens / cost) and metadata of the given session, "
            "to be used with compact to decide whether compaction is needed. tokens / cost default to null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_session",
        "description": (
            "Delete the given session. Warning: this operation is irreversible and cascades to all of its child sessions "
            "(observed: after deleting the parent, accessing a child returns 404). Confirm these sessions are no longer needed before deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": {"type": "string", "description": "ID of the session to delete (ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "connect_server",
        "description": (
            "Register and validate a remote opencode connection (valid only within this process; not persisted). "
            "Connecting performs the creation-time check: unreachable = availability error; no version = compatibility error; a version differing from the baseline returns a warning. "
            "Credential priority: password_file (first line) > password_env > plaintext password; "
            "when all are absent, no Authorization is sent (some remotes use an empty username/password). "
            "Returns {name, url, version, baseline, baseline_check}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias (handle); cannot be local"},
                "url": {"type": "string", "description": "e.g. http://host:4096"},
                "password_file": {"type": "string", "description": "(optional) path to a password file; its first line is used"},
                "password_env": {"type": "string", "description": "(optional) name of the environment variable holding the password"},
                "password": {"type": "string", "description": "(optional) plaintext password, the worst option"},
            },
            "required": ["name", "url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_servers",
        "description": (
            "List all current connections (local + dynamic remotes): name, address, source, version, and baseline check status. "
            "Ensures the local connection is ready (spawning the local serve if necessary)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "disconnect_server",
        "description": "Remove a dynamically registered remote connection (local cannot be removed). Its session routing is cleared as well.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]




HANDLERS = {
    "create_session": tool_create_session,
    "chat": tool_chat,
    "wait_session": tool_wait_session,
    "get_messages": tool_get_messages,
    "permission_reply": tool_permission_reply,
    "form_reply": tool_form_reply,
    "list_agents": tool_list_agents,
    "interrupt": tool_interrupt,
    "pending_interactions": tool_pending_interactions,
    "list_sessions": tool_list_sessions,
    "compact": tool_compact,
    "get_context": tool_get_context,
    "delete_session": tool_delete_session,
    "connect_server": tool_connect_server,
    "list_servers": tool_list_servers,
    "disconnect_server": tool_disconnect_server,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC handling
# ---------------------------------------------------------------------------

def _tool_result(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message):
    return {
        "content": [{"type": "text", "text": str(message)}],
        "isError": True,
    }


def handle_message(message):
    """Handle a single JSON-RPC message; returns a response dict or None (notifications need no response)."""
    if not isinstance(message, dict):
        return None

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Unknown tool: %s" % name),
            }
        try:
            result = handler(arguments)
            result = _warn_choke(arguments, result)
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_result(result)}
        except OpenCodeError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_error(str(exc))}
        except Exception as exc:  # Any exception becomes a tool error so the server never crashes
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Tool execution failed (%s): %s" % (name, exc)),
            }

    # Notifications (no id) are always ignored
    if msg_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def write_message(response):
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _handle_request(message):
    """Handle a single request in a worker thread; cancelled requests are no longer written back."""
    msg_id = message.get("id")
    _CURRENT.request_id = msg_id
    try:
        try:
            response = handle_message(message)
        except Exception as exc:  # Catch-all so no exception terminates the process
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal error: %s" % exc},
            }
        if response is None:
            return
        if _request_cancelled(msg_id):
            log("[opencode-mcp] request cancelled, discarding response:", msg_id)
            return
        write_message(response)
    finally:
        with _STATE_LOCK:
            _CANCELLED.discard(msg_id)
        _CURRENT.request_id = None


def main():
    workers = max(1, int(os.environ.get("OPENCODE_MCP_WORKERS") or "4"))
    log(
        "[opencode-mcp] started, workers=%d, waiting for JSON-RPC on stdin" % workers
    )
    executor = ThreadPoolExecutor(max_workers=workers)
    # The reader thread only parses and dispatches, so cancellation notifications arrive immediately
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception as exc:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: %s" % exc},
                }
            )
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        msg_id = message.get("id")

        # Cancellation notification: handled immediately in the reader thread, not sent to the thread pool
        if method == "notifications/cancelled":
            cancelled_id = (message.get("params") or {}).get("requestId")
            if cancelled_id is not None:
                with _STATE_LOCK:
                    _CANCELLED.add(cancelled_id)
                log("[opencode-mcp] received cancel request:", cancelled_id)
            continue

        # All other notifications (no id) are ignored
        if msg_id is None:
            continue

        executor.submit(_handle_request, message)


if __name__ == "__main__":
    main()
