#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the live opencode-mcp test suites (standard library only).

This module is imported by ``live_core_test.py``, ``live_subagent_test.py`` and
``live_lifecycle_test.py``. It provides:

* :class:`McpClient` -- a JSON-RPC-over-stdio client for ``server.py``.
* :class:`Reporter` -- one ``PASS``/``FAIL``/``SKIP`` line per scenario plus a
  summary line and the process exit code.
* :func:`api_request` -- a minimal HTTP client for the opencode HTTP API.
* environment helpers that read every environment-specific value from
  ``OPENCODE_TEST_*`` variables, and helpers that locate the local opencode
  server (including the ``serve`` process the MCP spawns for itself).

No host, credential or absolute path is hard-coded here; see ``tests/README.md``.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SERVER_PATH = os.path.join(REPO_ROOT, "server.py")

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 60.0
LOCAL_NAME = "local"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def test_timeout():
    """Per-scenario wait budget in seconds (``OPENCODE_TEST_TIMEOUT``, default 60)."""
    raw = os.environ.get("OPENCODE_TEST_TIMEOUT")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1.0, value)


def test_agent():
    """Agent used to create sessions, or None to use the server default."""
    value = os.environ.get("OPENCODE_TEST_AGENT")
    return value.strip() if value and value.strip() else None


def test_model_id():
    """Optional ``providerID/modelID`` model, or None to not pin a model."""
    value = os.environ.get("OPENCODE_TEST_MODEL")
    return value.strip() if value and value.strip() else None


def model_ref():
    """The API ``Model.Ref`` shape for ``OPENCODE_TEST_MODEL``, or None."""
    model_id = test_model_id()
    if not model_id or "/" not in model_id:
        return None
    provider_id, model = model_id.split("/", 1)
    return {"providerID": provider_id, "id": model}


def remote_config():
    """``(url, password_or_None)`` for the optional remote instance."""
    url = os.environ.get("OPENCODE_TEST_REMOTE_URL")
    if not url:
        return None, None
    return url, os.environ.get("OPENCODE_TEST_REMOTE_PASSWORD") or None


def opencode_on_path():
    return shutil.which("opencode") is not None


def mcp_env(use_local_env=True, extra=None):
    """Build the environment for a spawned ``server.py``.

    Any ambient ``OPENCODE_URL`` / ``OPENCODE_PASSWORD`` is removed so the
    suites are self-contained. When ``use_local_env`` is true and
    ``OPENCODE_TEST_URL`` is set, it is mapped onto ``OPENCODE_URL`` (and
    ``OPENCODE_TEST_PASSWORD`` onto ``OPENCODE_PASSWORD``). When false, the MCP
    is forced down its "spawn a private serve" path.
    """
    env = dict(os.environ)
    env.pop("OPENCODE_URL", None)
    env.pop("OPENCODE_PASSWORD", None)
    if use_local_env:
        url = os.environ.get("OPENCODE_TEST_URL")
        if url:
            env["OPENCODE_URL"] = url
            password = os.environ.get("OPENCODE_TEST_PASSWORD")
            if password:
                env["OPENCODE_PASSWORD"] = password
    if extra:
        env.update(extra)
    return env


def unwrap(payload):
    """Return ``payload["data"]`` when the API wrapped the body, else the body."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def truncate_id(value, keep=6):
    """Shorten an identifier so log lines never carry a full id."""
    if not value:
        return "-"
    value = str(value)
    if len(value) <= keep + 5:
        return value
    return "..." + value[-keep:]


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

class McpClient:
    """A minimal MCP client that spawns ``server.py`` and talks JSON-RPC over stdio."""

    def __init__(self, env=None, server_path=SERVER_PATH, client_name="opencode-mcp-live-test"):
        self.server_path = server_path
        self.proc = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env if env is not None else mcp_env(),
        )
        self._next_id = 0
        self._responses = {}
        self._arrived = queue.Queue()
        self._sent_at = {}
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.initialize(client_name)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @property
    def pid(self):
        return self.proc.pid

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is None:
                    continue
                with self._lock:
                    self._responses[request_id] = message
                self._arrived.put(request_id)
        except Exception:
            return

    def _write(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _send(self, method, params=None):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._sent_at[request_id] = time.monotonic()
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return request_id

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self, client_name="opencode-mcp-live-test"):
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1.0"},
            },
            timeout=30.0,
        )
        self.notify("notifications/initialized")

    def request(self, method, params=None, timeout=30.0):
        request_id = self._send(method, params)
        return self.wait_response(request_id, timeout)

    def wait_response(self, request_id, timeout=30.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if request_id in self._responses:
                    return self._responses[request_id]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no response for request id=%s within %.1fs" % (request_id, timeout))
            try:
                self._arrived.get(timeout=min(0.2, remaining))
            except queue.Empty:
                pass

    def has_response(self, request_id):
        with self._lock:
            return request_id in self._responses

    def send_call(self, name, arguments=None):
        """Start a ``tools/call`` without waiting; returns the request id."""
        return self._send("tools/call", {"name": name, "arguments": arguments or {}})

    def call(self, name, arguments=None, timeout=DEFAULT_TIMEOUT):
        """Run ``tools/call`` and return the parsed JSON payload.

        Tool errors come back as ``{"isError": True, "text": ...}``; a client
        timeout as ``{"isError": True, "__timeout__": True, ...}``.
        """
        return self.wait_call(self.send_call(name, arguments), timeout)

    def wait_call(self, request_id, timeout=DEFAULT_TIMEOUT):
        try:
            raw = self.wait_response(request_id, timeout)
        except TimeoutError as exc:
            return {"isError": True, "text": str(exc), "__timeout__": True}
        return self._parse(request_id, raw)

    def _parse(self, request_id, raw):
        elapsed = round(time.monotonic() - self._sent_at.get(request_id, time.monotonic()), 1)
        if "error" in raw:
            return {"isError": True, "text": json.dumps(raw["error"]), "_seconds": elapsed}
        result = raw.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else ""
        if result.get("isError"):
            return {"isError": True, "text": text, "_seconds": elapsed}
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = {"text": text}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload["_seconds"] = elapsed
        return payload

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Result reporter
# ---------------------------------------------------------------------------

class Reporter:
    """Prints one line per scenario and a final summary; drives the exit code."""

    def __init__(self, suite):
        self.suite = suite
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    @staticmethod
    def _line(tag, name, detail):
        suffix = " :: %s" % detail if detail else ""
        return "%s  %s%s" % (tag, name, suffix)

    def ok(self, name, detail=""):
        self.passed += 1
        print(self._line("PASS", name, detail))

    def fail(self, name, detail=""):
        self.failed += 1
        print(self._line("FAIL", name, detail))

    def skip(self, name, reason=""):
        self.skipped += 1
        print(self._line("SKIP", name, reason))

    def check(self, name, condition, detail="", skip_reason=None):
        """PASS on truthy, SKIP when ``skip_reason`` is given, else FAIL."""
        if condition:
            self.ok(name, detail)
        elif skip_reason is not None:
            self.skip(name, skip_reason)
        else:
            self.fail(name, detail)

    def header(self, text):
        print("\n-- %s" % text)

    def summary(self):
        print("%s: %d passed, %d failed, %d skipped" % (self.suite, self.passed, self.failed, self.skipped))
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# opencode HTTP API
# ---------------------------------------------------------------------------

def api_request(base_url, password, method, path, body=None, timeout=30.0):
    """Call the opencode HTTP API; returns the parsed JSON body (or None)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if password:
        token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# Locating the local server (env connection or MCP-spawned serve)
# ---------------------------------------------------------------------------

def list_servers(client, timeout=None):
    """Return ``(servers, error_text)``; servers is a list (possibly empty)."""
    payload = client.call("list_servers", timeout=timeout or (test_timeout() * 2))
    if payload.get("isError"):
        return [], payload.get("text", "")
    return payload.get("servers") or [], None


def _proc_children(pid):
    children = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry) as handle:
                stat = handle.read().split()
            if int(stat[3]) != pid:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                argv = [part.decode("utf-8", "replace") for part in handle.read().split(b"\x00") if part]
            children.append((int(entry), argv))
        except (OSError, ValueError, IndexError):
            continue
    return children


def find_spawned_serve(mcp_pid, port):
    """Best effort: the ``opencode serve`` child of ``mcp_pid`` bound to ``port``."""
    for child_pid, argv in _proc_children(mcp_pid):
        if argv and "serve" in argv and str(port) in argv:
            return child_pid
    return None


def read_child_password(child_pid):
    """Best effort: ``OPENCODE_SERVER_PASSWORD`` from the child's environment."""
    if not child_pid:
        return None
    try:
        with open("/proc/%d/environ" % child_pid, "rb") as handle:
            environ = handle.read()
    except OSError:
        return None
    for entry in environ.split(b"\x00"):
        if entry.startswith(b"OPENCODE_SERVER_PASSWORD="):
            return entry.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def server_port(url):
    try:
        return int(str(url).rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None


def local_credentials(client):
    """Return ``(url, password_or_None)`` for the local opencode server.

    Uses ``OPENCODE_TEST_URL`` / ``OPENCODE_TEST_PASSWORD`` when set. Otherwise
    asks the MCP for its spawned local URL and recovers the random child
    password from ``/proc`` (best effort; None when unavailable).
    """
    url = os.environ.get("OPENCODE_TEST_URL")
    if url:
        return url, os.environ.get("OPENCODE_TEST_PASSWORD") or None

    servers, _error = list_servers(client)
    local = next((item for item in servers if item.get("name") == LOCAL_NAME), None)
    if not local or not local.get("url"):
        return None, None
    url = local["url"]
    port = server_port(url)
    if port is None:
        return url, None
    child_pid = find_spawned_serve(client.pid, port)
    return url, read_child_password(child_pid)
