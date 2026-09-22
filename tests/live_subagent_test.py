#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live subagent / subtree scenarios for opencode-mcp.

Run directly::

    python3 tests/live_subagent_test.py

These scenarios need an environment whose configured agent can delegate to a
child session. The suite probes politely: it dispatches a background subagent
and waits a bounded time for a child session (``GET /api/session?parentID=``).
If no child appears, every scenario reports SKIP (never FAIL). The remote
scenario runs only when the remote environment variables are set. See
``tests/README.md``.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_client import (  # noqa: E402
    McpClient,
    Reporter,
    api_request,
    local_credentials,
    mcp_env,
    model_ref,
    remote_config,
    test_agent,
    test_timeout,
    truncate_id,
    unwrap,
)

ASK_PERMISSION = [{"action": "shell", "resource": "*", "effect": "ask"}]

DISPATCH_RUN = (
    "Use the subagent tool in background mode (background=true), choosing any available agent, "
    "whose only job is to run exactly `id` and report the output. Do NOT wait for it. "
    "Reply immediately with the single word: dispatched."
)
DISPATCH_MULTI = (
    "Use the subagent tool in background mode (background=true) TWICE, choosing any available agent each time. "
    "The first subagent must run exactly `id`; the second must run exactly `whoami`. Do NOT wait for either. "
    "Reply immediately with the single word: dispatched."
)
DISPATCH_FORM = (
    "Use the subagent tool in background mode (background=true), choosing any available agent, "
    "whose only job is to ask me a multiple-choice question using the question/form tool "
    "(ask exactly: which file should I edit? with options A, B and C) and then stop. "
    "Do NOT wait for it. Reply immediately with the single word: dispatched."
)


class SubagentSuite:
    def __init__(self, reporter):
        self.reporter = reporter
        self.budget = test_timeout()
        self.client_timeout = self.budget + 30.0
        self.sessions = []  # (session_id, server_or_None)
        self.client = McpClient(env=mcp_env(), client_name="live-subagent")
        self.local_url, self.local_password = local_credentials(self.client)
        self.delegation_ok = None
        self.delegation_reason = "the configured agent did not delegate"

    # -- plumbing ----------------------------------------------------------

    def _create_ask_parent(self, title, url=None, password=None):
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

    def _children(self, parent_id, url=None, password=None):
        url = url or self.local_url
        password = self.local_password if password is None else password
        try:
            data = unwrap(api_request(url, password, "GET", "/api/session?parentID=" + parent_id, timeout=20))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _wait_children(self, parent_id, seconds, minimum=1, url=None, password=None):
        deadline = time.monotonic() + seconds
        children = []
        while time.monotonic() < deadline:
            children = self._children(parent_id, url, password)
            if len(children) >= minimum:
                return children
            time.sleep(1.0)
        return children

    def _dispatch(self, session_id, text, auto_permission="manual", wait_for_subagents=False,
                  server=None, timeout=None):
        args = {
            "session_id": session_id,
            "text": text,
            "timeout_secs": int(timeout or self.budget),
            "auto_permission": auto_permission,
        }
        if wait_for_subagents:
            args["wait_for_subagents"] = True
        if server:
            args["server"] = server
        return self.client.call("chat", args, timeout=self.client_timeout)

    def _wait_state(self, session_id, auto_permission="manual", server=None, timeout=None):
        args = {
            "session_id": session_id,
            "timeout_secs": int(timeout or self.budget),
            "auto_permission": auto_permission,
            "wait_for_subagents": True,
        }
        if server:
            args["server"] = server
        return self.client.call("wait_session", args, timeout=self.client_timeout)

    def _cleanup(self):
        for session_id, server in self.sessions:
            self.client.call("delete_session", {"session_id": session_id, "server": server}
                             if server else {"session_id": session_id}, timeout=30)
        self.client.close()

    # -- scenarios ---------------------------------------------------------

    def probe_delegation(self):
        if not self.local_password:
            self.delegation_reason = (
                "direct API access to the local server is unavailable; "
                "set OPENCODE_TEST_URL/PASSWORD to create ask-permission parents"
            )
            self.delegation_ok = False
            return
        parent = self._create_ask_parent("delegation probe")
        if not parent:
            self.delegation_reason = "could not create a parent session for the delegation probe"
            self.delegation_ok = False
            return
        self._dispatch(parent, DISPATCH_RUN)
        children = self._wait_children(parent, min(45, max(30, self.budget)))
        if children:
            self.delegation_ok = True
        else:
            self.delegation_ok = False
            self.delegation_reason = (
                "the configured agent did not delegate; set OPENCODE_TEST_AGENT to a delegation-capable agent"
            )

    def scenario_manual_wait_points_at_subagent(self):
        name = "manual wait_session points at the subagent"
        parent = self._create_ask_parent("manual subagent")
        if not parent:
            self.reporter.skip(name, "could not create a parent session")
            return
        self._dispatch(parent, DISPATCH_RUN, auto_permission="manual")
        children = self._wait_children(parent, min(45, max(30, self.budget)))
        if not children:
            self.reporter.skip(name, self.delegation_reason)
            return
        child = children[0].get("id")
        state = self._wait_state(parent, auto_permission="manual")
        request = (state.get("requests") or [{}])[0]
        points_at_child = state.get("status") == "needs_permission" and state.get("session_id") == child
        has_root = state.get("root_session_id") == parent
        self.reporter.check(
            name,
            points_at_child and has_root,
            "status=%s session_id=%s root=%s" % (
                state.get("status"), truncate_id(state.get("session_id")), truncate_id(state.get("root_session_id")),
            ),
        )
        reply = self.client.call(
            "permission_reply",
            {"session_id": child, "request_id": request.get("id"), "decision": "once"},
            timeout=30,
        )
        self.reporter.check(
            "permission_reply with the subagent id succeeds",
            reply.get("ok") is True,
            "ok=%s" % reply.get("ok"),
        )

    def scenario_chat_reports_subagents(self):
        name = "chat reports pending_subagents without gating"
        parent = self._create_ask_parent("report subagents")
        if not parent:
            self.reporter.skip(name, "could not create a parent session")
            return
        chat = self._dispatch(parent, DISPATCH_RUN, auto_permission="manual")
        subagents = chat.get("subagents") or []
        self.reporter.check(
            name,
            chat.get("status") == "succeeded"
            and (chat.get("pending_subagents") or 0) >= 1
            and bool(subagents),
            "status=%s pending=%s subagents=%s" % (
                chat.get("status"), chat.get("pending_subagents"),
                [s.get("session_id") and truncate_id(s.get("session_id")) for s in subagents],
            ),
        )

    def scenario_automatic_reaches_subagent(self):
        name = "automatic permission reaches the subagent"
        parent = self._create_ask_parent("automatic subagent")
        if not parent:
            self.reporter.skip(name, "could not create a parent session")
            return
        chat = self._dispatch(
            parent, DISPATCH_RUN, auto_permission="once", wait_for_subagents=True, timeout=max(90, self.budget)
        )
        children = self._children(parent)
        self.reporter.check(
            name,
            chat.get("status") == "succeeded",
            "status=%s children=%s" % (chat.get("status"), len(children)),
            skip_reason=None if children else self.delegation_reason,
        )

    def scenario_form_ownership(self):
        name = "form ownership names the subagent"
        parent = self._create_ask_parent("form subagent")
        if not parent:
            self.reporter.skip(name, "could not create a parent session")
            return
        chat = self._dispatch(
            parent, DISPATCH_FORM, auto_permission="once", wait_for_subagents=True, timeout=max(90, self.budget)
        )
        if chat.get("status") != "needs_form":
            self.reporter.skip(name, "the subagent did not ask a form (status=%s)" % chat.get("status"))
            return
        subagent_ids = [s.get("session_id") for s in (chat.get("subagents") or [])]
        forms = chat.get("forms") or []
        owner = forms[0].get("sessionID") if forms else None
        self.reporter.check(
            name,
            bool(forms)
            and chat.get("session_id") in subagent_ids
            and owner == chat.get("session_id")
            and chat.get("root_session_id") == parent,
            "session_id=%s owner=%s root=%s" % (
                truncate_id(chat.get("session_id")), truncate_id(owner), truncate_id(chat.get("root_session_id")),
            ),
        )
        field_key = ((forms[0].get("fields") or [{}])[0].get("key") if forms else None) or "answer"
        reply = self.client.call(
            "form_reply",
            {"session_id": chat.get("session_id"), "form_id": forms[0].get("id"), "answer": {field_key: "A"}},
            timeout=30,
        )
        self.reporter.check(
            "form_reply with the subagent id succeeds",
            reply.get("ok") is True,
            "ok=%s" % reply.get("ok"),
        )

    def scenario_parallel_subagents(self):
        name = "parallel subagents report their own owner"
        parent = self._create_ask_parent("parallel subagents")
        if not parent:
            self.reporter.skip(name, "could not create a parent session")
            return
        self._dispatch(parent, DISPATCH_MULTI, auto_permission="manual")
        children = self._wait_children(parent, min(45, max(30, self.budget)), minimum=2)
        child_ids = [c.get("id") for c in children]
        if len(child_ids) < 2:
            self.reporter.skip(name, "fewer than two subagents were created")
            return
        owners = set()
        deadline = time.monotonic() + min(30, max(15, self.budget))
        while time.monotonic() < deadline and len(owners) < 2:
            pending = self.client.call("pending_interactions", {"session_id": parent}, timeout=30)
            for request in pending.get("permissions") or []:
                owners.add(request.get("sessionID"))
            if len(owners) >= 2:
                break
            self._wait_state(parent, auto_permission="manual", timeout=10)
        known = owners - {None}
        self.reporter.check(
            name,
            len(known) >= 2 and known.issubset(set(child_ids)),
            "owners=%s children=%s" % ([truncate_id(o) for o in known], [truncate_id(c) for c in child_ids]),
            skip_reason=None if len(known) >= 2 else "fewer than two parallel permission requests appeared",
        )

    def scenario_remote_subagent(self):
        name = "remote subagent reported and routed without an explicit server"
        remote_url, remote_password = remote_config()
        if not remote_url:
            self.reporter.skip(name, "OPENCODE_TEST_REMOTE_URL is not set")
            return
        if not remote_password:
            self.reporter.skip(name, "OPENCODE_TEST_REMOTE_PASSWORD is not set")
            return
        connected = self.client.call(
            "connect_server", {"name": "remote", "url": remote_url, "password": remote_password}, timeout=45
        )
        if connected.get("isError"):
            self.reporter.skip(name, "could not connect to the remote instance")
            return
        parent = self._create_ask_parent("remote subagent", url=remote_url, password=remote_password)
        if not parent:
            self.reporter.skip(name, "could not create a remote parent session")
            self.client.call("disconnect_server", {"name": "remote"}, timeout=30)
            return
        self.sessions[-1] = (parent, "remote")
        self._dispatch(parent, DISPATCH_RUN, auto_permission="manual", server="remote")
        children = self._wait_children(parent, min(45, max(30, self.budget)), url=remote_url, password=remote_password)
        if not children:
            self.reporter.skip(name, self.delegation_reason)
            self.client.call("disconnect_server", {"name": "remote"}, timeout=30)
            return
        child = children[0].get("id")
        state = self._wait_state(parent, auto_permission="manual", server="remote")
        request = (state.get("requests") or [{}])[0]
        self.reporter.check(
            name,
            state.get("status") == "needs_permission" and state.get("session_id") == child,
            "status=%s session_id=%s" % (state.get("status"), truncate_id(state.get("session_id"))),
        )
        # No explicit server: the subtree snapshot routed the child to the remote.
        reply = self.client.call(
            "permission_reply",
            {"session_id": child, "request_id": request.get("id"), "decision": "reject"},
            timeout=30,
        )
        self.reporter.check(
            "remote permission_reply without an explicit server succeeds",
            reply.get("ok") is True,
            "ok=%s" % reply.get("ok"),
        )
        self.client.call("disconnect_server", {"name": "remote"}, timeout=30)

    # -- driver ------------------------------------------------------------

    def run(self):
        self.probe_delegation()
        scenario_names = (
            "manual wait_session points at the subagent",
            "chat reports pending_subagents without gating",
            "automatic permission reaches the subagent",
            "form ownership names the subagent",
            "parallel subagents report their own owner",
            "remote subagent reported and routed without an explicit server",
        )
        if not self.delegation_ok:
            for name in scenario_names:
                self.reporter.skip(name, self.delegation_reason)
            return
        self.scenario_manual_wait_points_at_subagent()
        self.scenario_chat_reports_subagents()
        self.scenario_automatic_reaches_subagent()
        self.scenario_form_ownership()
        self.scenario_parallel_subagents()
        self.scenario_remote_subagent()


def main():
    reporter = Reporter("live_subagent")
    suite = SubagentSuite(reporter)
    try:
        suite.run()
    finally:
        suite._cleanup()
    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
