#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live core scenarios for opencode-mcp.

Run directly::

    python3 tests/live_core_test.py

Covers the connection layer (explicit-env vs MCP-spawned local, failure
classification, duplicate/unknown names, session routing), the chat /
permission / form loops, ``wait_session`` terminal and timeout states, request
cancellation, non-blocking ``pending_interactions`` and
``get_context`` / ``compact``. The remote end-to-end loop runs only when the
remote environment variables are set. See ``tests/README.md``.
"""

import json
import os
import secrets
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_client import (  # noqa: E402
    McpClient,
    Reporter,
    api_request,
    local_credentials,
    mcp_env,
    model_ref,
    opencode_on_path,
    remote_config,
    test_agent,
    test_timeout,
    truncate_id,
    unwrap,
)

ASK_PERMISSION = [{"action": "shell", "resource": "*", "effect": "ask"}]


def _ok(payload):
    return isinstance(payload, dict) and not payload.get("isError")


class CoreSuite:
    def __init__(self, reporter):
        self.reporter = reporter
        self.budget = test_timeout()
        self.client_timeout = self.budget + 30.0
        self.sessions = []  # (session_id, server_or_None)
        self._clients = []

        self.env_configured = bool(os.environ.get("OPENCODE_TEST_URL"))
        self.env_client = None
        self.spawn_client = None

        if self.env_configured:
            self.env_client = self._start(True, "live-core-env")
            self.spawn_client = self._start(False, "live-core-spawn")
            self.primary = self.env_client
        else:
            self.spawn_client = self._start(False, "live-core-spawn")
            self.primary = self.spawn_client

        self.local_url, self.local_password = local_credentials(self.primary)
        servers, error = self._servers(self.primary)
        self.local_ready = bool(servers)
        self.local_error = error or ""

    # -- plumbing ----------------------------------------------------------

    def _start(self, use_local_env, name):
        client = McpClient(env=mcp_env(use_local_env=use_local_env), client_name=name)
        self._clients.append(client)
        return client

    def _servers(self, client):
        payload = client.call("list_servers", timeout=self.budget * 2)
        if payload.get("isError"):
            return [], payload.get("text", "")
        return payload.get("servers") or [], None

    def _create_session(self, title, server=None):
        args = {"title": title}
        if test_agent():
            args["agent"] = test_agent()
        if model_ref() and "/" in (os.environ.get("OPENCODE_TEST_MODEL") or ""):
            args["model_id"] = os.environ["OPENCODE_TEST_MODEL"]
        if server:
            args["server"] = server
        payload = self.primary.call("create_session", args, timeout=self.client_timeout)
        if not _ok(payload):
            return None, payload
        session_id = payload.get("session_id")
        if session_id:
            self.sessions.append((session_id, server))
        return session_id, payload

    def _create_ask_session(self, title, url=None, password=None):
        """Create a session whose shell tool asks for permission (direct API)."""
        url = url or self.local_url
        password = self.local_password if password is None else password
        if not (url and password):
            return None
        body = {"title": title, "permissions": ASK_PERMISSION}
        if test_agent():
            body["agent"] = test_agent()
        ref = model_ref()
        if ref:
            body["model"] = ref
        try:
            data = unwrap(api_request(url, password, "POST", "/api/session", body, timeout=30))
        except Exception:
            return None
        session_id = data.get("id") if isinstance(data, dict) else None
        if session_id:
            self.sessions.append((session_id, None))
        return session_id

    def _api(self, url, password, method, path, body=None, timeout=30):
        try:
            return api_request(url, password, method, path, body, timeout=timeout)
        except Exception:
            return None

    def _drop(self, session_id, server=None):
        args = {"session_id": session_id}
        if server:
            args["server"] = server
        self.primary.call("delete_session", args, timeout=30)

    def _cleanup(self):
        for session_id, server in self.sessions:
            try:
                self._drop(session_id, server)
            except Exception:
                pass
        for client in self._clients:
            client.close()

    # -- connection layer --------------------------------------------------

    def scenario_explicit_env_connection(self):
        name = "explicit-env connection reports source=env"
        if not self.env_configured:
            self.reporter.skip(name, "OPENCODE_TEST_URL is not set")
            return
        servers, _ = self._servers(self.env_client)
        local = next((item for item in servers if item.get("name") == "local"), {})
        source = local.get("source")
        self.reporter.check(
            name,
            source == "env" and bool(local.get("version")),
            "source=%s version=%s check=%s" % (source, local.get("version"), local.get("baseline_check")),
        )

    def scenario_spawned_local(self):
        name = "MCP-spawned local connection"
        if not opencode_on_path() and not self.env_configured:
            self.reporter.skip(name, "opencode is not on PATH")
            return
        servers, error = self._servers(self.spawn_client)
        local = next((item for item in servers if item.get("name") == "local"), {})
        url = local.get("url") or ""
        self.reporter.check(
            name,
            local.get("source") == "spawned" and bool(local.get("version")) and url.startswith("http://127.0.0.1:"),
            "source=%s version=%s check=%s url=%s" % (
                local.get("source"), local.get("version"), local.get("baseline_check"), url or error,
            ),
        )

    def scenario_failure_dead_port(self):
        name = "unreachable port classified as availability"
        payload = self.primary.call(
            "connect_server", {"name": "unreachable", "url": "http://127.0.0.1:1"}, timeout=30
        )
        text = payload.get("text", "")
        self.reporter.check(
            name,
            bool(payload.get("isError")) and "[availability]" in text,
            text[:80],
        )

    def scenario_failure_non_opencode_endpoint(self):
        name = "reachable non-opencode endpoint classified as compatibility"
        import http.server

        class HtmlHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<!doctype html><html><body>not the api</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HtmlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            payload = self.primary.call("connect_server", {"name": "webui", "url": url}, timeout=30)
            text = payload.get("text", "")
            self.reporter.check(
                name,
                bool(payload.get("isError"))
                and "[compatibility]" in text
                and "did not return opencode v2 API JSON" in text,
                text[:80],
            )
        finally:
            server.shutdown()

    def scenario_failure_wrong_credentials(self):
        name = "wrong credentials classified as availability"
        servers, _ = self._servers(self.spawn_client)
        local = next((item for item in servers if item.get("name") == "local"), {})
        url = local.get("url")
        if not url:
            self.reporter.skip(name, "no local server url available")
            return
        wrong = "invalid-" + secrets.token_hex(8)
        payload = self.primary.call(
            "connect_server", {"name": "wrong-creds", "url": url, "password": wrong}, timeout=30
        )
        if not payload.get("isError"):
            # A server that enforces no authentication accepts any password.
            self.reporter.skip(name, "the local server does not enforce authentication")
            return
        self.reporter.check(name, "[availability]" in payload.get("text", ""), payload.get("text", "")[:80])

    def scenario_duplicate_and_unknown_names(self):
        duplicate = "duplicate connection name rejected"
        unknown = "unknown connection name reported"
        if not self.local_password:
            self.reporter.skip(duplicate, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
        else:
            first = self.primary.call(
                "connect_server",
                {"name": "alias", "url": self.local_url, "password": self.local_password},
                timeout=30,
            )
            if not _ok(first):
                self.reporter.skip(duplicate, "could not register the alias: %s" % first.get("text", "")[:60])
            else:
                second = self.primary.call(
                    "connect_server", {"name": "alias", "url": "http://127.0.0.1:2"}, timeout=30
                )
                self.reporter.check(
                    duplicate,
                    bool(second.get("isError")) and "already exists" in second.get("text", ""),
                    second.get("text", "")[:70],
                )
                self.primary.call("disconnect_server", {"name": "alias"}, timeout=30)

        payload = self.primary.call("list_agents", {"server": "no-such-connection"}, timeout=30)
        self.reporter.check(
            unknown,
            bool(payload.get("isError")) and "Unknown connection" in payload.get("text", ""),
            payload.get("text", "")[:70],
        )

    def scenario_disconnect_local_refused(self):
        name = "local connection cannot be disconnected"
        payload = self.primary.call("disconnect_server", {"name": "local"}, timeout=30)
        self.reporter.check(name, bool(payload.get("isError")), payload.get("text", "")[:60])

    def scenario_session_routing(self):
        name = "session auto-routing across two connections"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        registered = self.primary.call(
            "connect_server",
            {"name": "second", "url": self.local_url, "password": self.local_password},
            timeout=30,
        )
        if not _ok(registered):
            self.reporter.skip(name, "could not register the second connection")
            return
        created = self.primary.call("create_session", {"title": "routing", "server": "second"}, timeout=30)
        session_id = created.get("session_id")
        if not session_id:
            self.primary.call("disconnect_server", {"name": "second"}, timeout=30)
            self.reporter.fail(name, "create_session on the second connection failed: %s" % created.get("text", "")[:60])
            return
        self.sessions.append((session_id, "second"))
        # No server argument: the call must route back to "second".
        messages = self.primary.call("get_messages", {"session_id": session_id, "limit": 5}, timeout=30)
        self.reporter.check(
            name,
            messages.get("server") == "second" and not messages.get("isError"),
            "created_on=%s routed_to=%s" % (created.get("server"), messages.get("server")),
        )
        self.primary.call("disconnect_server", {"name": "second"}, timeout=30)

    # -- chat / waits ------------------------------------------------------

    def scenario_chat_and_wait_terminal(self):
        name = "chat happy path and wait_session terminal state"
        session_id, created = self._create_session("chat and wait")
        if not session_id:
            self.reporter.skip(name, "could not create a session: %s" % created.get("text", "")[:60])
            return
        chat = self.primary.call(
            "chat",
            {"session_id": session_id, "text": "Reply with exactly: OK", "timeout_secs": int(self.budget)},
            timeout=self.client_timeout,
        )
        self.reporter.check(
            "chat happy path",
            chat.get("status") == "succeeded" and "OK" in (chat.get("assistant_text") or ""),
            "status=%s text=%r %ss" % (chat.get("status"), (chat.get("assistant_text") or "")[:20], chat.get("_seconds")),
        )
        wait = self.primary.call(
            "wait_session",
            {"session_id": session_id, "timeout_secs": int(self.budget)},
            timeout=self.client_timeout,
        )
        self.reporter.check(
            "wait_session terminal and cursor",
            wait.get("status") == "succeeded"
            and bool(wait.get("last_message_id"))
            and (wait.get("_seconds") or 0) < 15,
            "status=%s last=%s %ss" % (wait.get("status"), truncate_id(wait.get("last_message_id")), wait.get("_seconds")),
        )

        messages = self.primary.call("get_messages", {"session_id": session_id}, timeout=30)
        cursor = wait.get("last_message_id")
        incremental = self.primary.call(
            "get_messages", {"session_id": session_id, "after_message_id": cursor}, timeout=30
        )
        self.reporter.check(
            "get_messages incremental cursor",
            incremental.get("count") == 0,
            "count=%s after %s" % (incremental.get("count"), truncate_id(cursor)),
        )

        user_id = next(
            (m.get("id") for m in (messages.get("messages") or []) if m.get("type") == "user"), None
        )
        if user_id:
            after_user = self.primary.call(
                "get_messages", {"session_id": session_id, "after_message_id": user_id}, timeout=30
            )
            has_reply = any(
                m.get("type") == "assistant" and "OK" in (m.get("text") or "")
                for m in (after_user.get("messages") or [])
            )
            self.reporter.check("get_messages returns the reply after the user turn", has_reply,
                                "count=%s" % after_user.get("count"))

    def scenario_manual_permission_loop(self):
        name = "manual permission loop"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        session_id = self._create_ask_session("manual permission")
        if not session_id:
            self.reporter.skip(name, "could not create an ask-permission session")
            return
        chat = self.primary.call(
            "chat",
            {
                "session_id": session_id,
                "text": "Run exactly `echo live-core-ok` with the shell tool and report the output.",
                "timeout_secs": int(self.budget),
                "auto_permission": "manual",
            },
            timeout=self.client_timeout,
        )
        if chat.get("status") != "needs_permission":
            self.reporter.fail(name, "expected needs_permission, got %s" % chat.get("status"))
            return
        request = (chat.get("requests") or [{}])[0]
        reply = self.primary.call(
            "permission_reply",
            {"session_id": session_id, "request_id": request.get("id"), "decision": "once"},
            timeout=30,
        )
        wait = self.primary.call(
            "wait_session", {"session_id": session_id, "timeout_secs": int(self.budget)}, timeout=self.client_timeout
        )
        messages = self.primary.call("get_messages", {"session_id": session_id}, timeout=30)
        has_marker = "live-core-ok" in json.dumps(messages.get("messages") or [], ensure_ascii=False)
        self.reporter.check(
            name,
            reply.get("ok") is True and wait.get("status") == "succeeded" and has_marker,
            "reply_ok=%s status=%s marker=%s" % (reply.get("ok"), wait.get("status"), has_marker),
        )

    def scenario_form_loop(self):
        name = "form loop"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        session_id, created = self._create_session("form loop")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        try:
            created_form = unwrap(
                api_request(
                    self.local_url,
                    self.local_password,
                    "POST",
                    "/api/session/%s/form" % session_id,
                    {
                        "title": "Live form",
                        "fields": [
                            {"key": "city", "type": "string", "title": "Which city?", "required": True},
                            {
                                "key": "level",
                                "type": "string",
                                "title": "Level",
                                "options": [
                                    {"value": "beginner", "label": "Beginner"},
                                    {"value": "expert", "label": "Expert"},
                                ],
                            },
                        ],
                    },
                    timeout=30,
                )
            )
        except Exception as exc:
            self.reporter.skip(name, "could not create a form: %s" % exc)
            return
        form_id = (created_form or {}).get("id")
        if not form_id:
            self.reporter.skip(name, "form creation returned no id")
            return

        pending = self.primary.call("pending_interactions", {"session_id": session_id}, timeout=30)
        forms_seen = pending.get("forms") or []
        self.reporter.check(
            "pending_interactions reports the form",
            bool(forms_seen),
            "forms=%s" % len(forms_seen),
        )
        reply = self.primary.call(
            "form_reply",
            {"session_id": session_id, "form_id": form_id, "answer": {"city": "Testville", "level": "expert"}},
            timeout=30,
        )
        after = self.primary.call("pending_interactions", {"session_id": session_id}, timeout=30)
        self.reporter.check(
            "form_reply clears the pending form",
            reply.get("ok") is True and len(after.get("forms") or []) == 0,
            "reply_ok=%s remaining=%s" % (reply.get("ok"), len(after.get("forms") or [])),
        )

    def scenario_automatic_permission(self):
        name = "automatic permission"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        session_id = self._create_ask_session("automatic permission")
        if not session_id:
            self.reporter.skip(name, "could not create an ask-permission session")
            return
        chat = self.primary.call(
            "chat",
            {
                "session_id": session_id,
                "text": "Run exactly `echo auto-perm-ok` with the shell tool and report the output.",
                "timeout_secs": int(self.budget),
            },
            timeout=self.client_timeout,
        )
        self.reporter.check(
            name,
            chat.get("status") == "succeeded" and "auto-perm-ok" in (chat.get("assistant_text") or ""),
            "status=%s text=%r" % (chat.get("status"), (chat.get("assistant_text") or "")[:40]),
        )

    def scenario_wait_session_timeout(self):
        name = "wait_session timeout while generating"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        session_id, _created = self._create_session("wait timeout")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        self._api(
            self.local_url,
            self.local_password,
            "POST",
            "/api/session/%s/prompt" % session_id,
            {"text": "Write a very long, detailed essay about the history of computing (at least 2000 words)."},
        )
        time.sleep(1.0)
        try:
            wait = self.primary.call("wait_session", {"session_id": session_id, "timeout_secs": 3}, timeout=30)
            self.reporter.check(
                name,
                wait.get("status") == "timeout",
                "status=%s %ss" % (wait.get("status"), wait.get("_seconds")),
            )
        finally:
            self.primary.call("interrupt", {"session_id": session_id}, timeout=30)

    def scenario_wait_session_interrupted(self):
        name = "wait_session reports interrupted"
        if not self.local_password:
            self.reporter.skip(name, "local password unavailable; set OPENCODE_TEST_URL/PASSWORD")
            return
        session_id, _created = self._create_session("wait interrupted")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        self._api(
            self.local_url,
            self.local_password,
            "POST",
            "/api/session/%s/prompt" % session_id,
            {"text": "Write a very long, detailed essay about the history of mathematics (at least 2000 words)."},
        )
        time.sleep(2.0)
        self.primary.call("interrupt", {"session_id": session_id}, timeout=30)
        wait = self.primary.call(
            "wait_session", {"session_id": session_id, "timeout_secs": int(self.budget)}, timeout=self.client_timeout
        )
        self.reporter.check(name, wait.get("status") == "interrupted", "status=%s" % wait.get("status"))

    def scenario_cancel_promptly(self):
        name = "notifications/cancelled returns promptly"
        session_id, _created = self._create_session("cancel")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        request_id = self.primary.send_call(
            "chat",
            {
                "session_id": session_id,
                "text": "Write a very long, detailed essay about the history of science (at least 2000 words).",
                "timeout_secs": 120,
            },
        )
        time.sleep(0.5)
        self.primary.notify("notifications/cancelled", {"requestId": request_id})
        start = time.monotonic()
        try:
            self.primary.request("ping", timeout=3)
            ping_ms = round((time.monotonic() - start) * 1000)
        except Exception:
            ping_ms = 9999
        time.sleep(3)
        dropped = not self.primary.has_response(request_id)
        self.reporter.check(
            name,
            ping_ms < 2000 and dropped,
            "ping=%sms response_dropped=%s" % (ping_ms, dropped),
        )
        self.primary.call("interrupt", {"session_id": session_id}, timeout=30)

    def scenario_concurrent_pending(self):
        name = "pending_interactions does not block behind an in-flight chat"
        session_id, _created = self._create_session("concurrent pending")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        request_id = self.primary.send_call(
            "chat",
            {"session_id": session_id, "text": "Reply with exactly: OK", "timeout_secs": int(self.budget)},
        )
        time.sleep(0.6)
        start = time.monotonic()
        pending = self.primary.call("pending_interactions", {"session_id": session_id}, timeout=5)
        pending_ms = round((time.monotonic() - start) * 1000)
        chat = self.primary.wait_call(request_id, timeout=self.client_timeout)
        self.reporter.check(
            name,
            not pending.get("isError") and pending_ms < 2000 and chat.get("status") == "succeeded",
            "pending=%sms chat=%s" % (pending_ms, chat.get("status")),
        )

    def scenario_context_and_compact(self):
        name = "get_context and compact"
        session_id, created = self._create_session("context compact")
        if not session_id:
            self.reporter.skip(name, "could not create a session")
            return
        chat = self.primary.call(
            "chat",
            {"session_id": session_id, "text": "Reply with exactly: OK", "timeout_secs": int(self.budget)},
            timeout=self.client_timeout,
        )
        if chat.get("status") != "succeeded":
            self.reporter.fail(name, "chat did not succeed: %s" % chat.get("status"))
            return
        context = self.primary.call("get_context", {"session_id": session_id}, timeout=30)
        self.reporter.check("get_context returns the session", context.get("id") == session_id,
                            "id=%s" % truncate_id(context.get("id")))
        compact = self.primary.call(
            "compact", {"session_id": session_id, "timeout_secs": int(self.budget)}, timeout=self.client_timeout
        )
        self.reporter.check(
            "compact reaches a terminal state",
            compact.get("status") in ("succeeded", "compaction_failed"),
            "status=%s" % compact.get("status"),
        )

    # -- remote ------------------------------------------------------------

    def scenario_remote_end_to_end(self):
        name = "remote end-to-end loop"
        remote_url, remote_password = remote_config()
        if not remote_url:
            self.reporter.skip(name, "OPENCODE_TEST_REMOTE_URL is not set")
            return
        connected = self.primary.call(
            "connect_server",
            {"name": "remote", "url": remote_url, "password": remote_password or ""},
            timeout=45,
        )
        if connected.get("isError"):
            self.reporter.skip(name, "could not connect to the remote instance: %s" % connected.get("text", "")[:60])
            return
        self.reporter.check(
            "remote connect validates the instance",
            connected.get("baseline_check") == "ok" and bool(connected.get("version")),
            "version=%s check=%s" % (connected.get("version"), connected.get("baseline_check")),
        )

        created = self.primary.call(
            "create_session", {"title": "remote e2e", "server": "remote"}, timeout=30
        )
        session_id = created.get("session_id")
        if session_id:
            self.sessions.append((session_id, "remote"))
        self.reporter.check(
            "remote create_session", bool(session_id) and created.get("server") == "remote",
            "server=%s id=%s" % (created.get("server"), truncate_id(session_id)),
        )
        if not session_id:
            self.primary.call("disconnect_server", {"name": "remote"}, timeout=30)
            return

        chat = self.primary.call(
            "chat",
            {"session_id": session_id, "text": "Reply with exactly: OK", "timeout_secs": int(self.budget)},
            timeout=self.client_timeout,
        )
        self.reporter.check(
            "remote chat",
            chat.get("status") == "succeeded" and "OK" in (chat.get("assistant_text") or ""),
            "status=%s text=%r" % (chat.get("status"), (chat.get("assistant_text") or "")[:20]),
        )

        if remote_password:
            ask_id = self._create_ask_session("remote manual", url=remote_url, password=remote_password)
            if ask_id:
                self.sessions[-1] = (ask_id, "remote")
                ask_chat = self.primary.call(
                    "chat",
                    {
                        "server": "remote",
                        "session_id": ask_id,
                        "text": "Run exactly `echo remote-e2e-ok` with the shell tool and report the output.",
                        "timeout_secs": int(self.budget),
                    },
                    timeout=self.client_timeout,
                )
                self.reporter.check(
                    "remote defaults to manual permission",
                    ask_chat.get("status") == "needs_permission" and ask_chat.get("server") == "remote",
                    "status=%s server=%s" % (ask_chat.get("status"), ask_chat.get("server")),
                )
                if ask_chat.get("status") == "needs_permission":
                    request = (ask_chat.get("requests") or [{}])[0]
                    self.primary.call(
                        "permission_reply",
                        {"server": "remote", "session_id": ask_id, "request_id": request.get("id"), "decision": "once"},
                        timeout=30,
                    )
                    wait = self.primary.call(
                        "wait_session",
                        {"server": "remote", "session_id": ask_id, "timeout_secs": int(self.budget)},
                        timeout=self.client_timeout,
                    )
                    messages = self.primary.call(
                        "get_messages", {"server": "remote", "session_id": ask_id, "limit": 50}, timeout=30
                    )
                    marker = "remote-e2e-ok" in json.dumps(messages.get("messages") or [], ensure_ascii=False)
                    self.reporter.check(
                        "remote manual permission flow",
                        wait.get("status") == "succeeded" and marker,
                        "status=%s marker=%s" % (wait.get("status"), marker),
                    )
                self._api(remote_url, remote_password, "DELETE", "/api/session/" + ask_id)

        messages = self.primary.call("get_messages", {"server": "remote", "session_id": session_id, "limit": 5}, timeout=30)
        incremental = self.primary.call(
            "get_messages",
            {"session_id": session_id, "after_message_id": messages.get("last_message_id")},
            timeout=30,
        )
        self.reporter.check(
            "remote incremental get_messages",
            incremental.get("count") == 0,
            "count=%s" % incremental.get("count"),
        )

        disconnected = self.primary.call("disconnect_server", {"name": "remote"}, timeout=30)
        gone = self.primary.call("list_agents", {"server": "remote"}, timeout=30)
        self.reporter.check(
            "disconnect removes the remote connection",
            disconnected.get("ok") is True and gone.get("isError") and "Unknown connection" in gone.get("text", ""),
            "",
        )

    # -- driver ------------------------------------------------------------

    def run(self):
        if not self.local_ready:
            reason = self.local_error[:80] or "no local opencode server available"
            for name in (
                "MCP-spawned local connection", "chat happy path",
            ):
                self.reporter.skip(name, reason)
            return
        self.scenario_explicit_env_connection()
        self.scenario_spawned_local()
        self.scenario_failure_dead_port()
        self.scenario_failure_non_opencode_endpoint()
        self.scenario_failure_wrong_credentials()
        self.scenario_duplicate_and_unknown_names()
        self.scenario_disconnect_local_refused()
        self.scenario_session_routing()
        self.scenario_chat_and_wait_terminal()
        self.scenario_manual_permission_loop()
        self.scenario_form_loop()
        self.scenario_automatic_permission()
        self.scenario_wait_session_timeout()
        self.scenario_wait_session_interrupted()
        self.scenario_cancel_promptly()
        self.scenario_concurrent_pending()
        self.scenario_context_and_compact()
        self.scenario_remote_end_to_end()


def main():
    reporter = Reporter("live_core")
    suite = CoreSuite(reporter)
    try:
        suite.run()
    finally:
        suite._cleanup()
    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
