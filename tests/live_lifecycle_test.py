#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live spawned-serve lifecycle scenarios for opencode-mcp.

Run directly::

    python3 tests/live_lifecycle_test.py

With a clean environment (no ``OPENCODE_URL``) the MCP spawns its own
``opencode serve``. This suite forces that path via ``list_servers``, locates
the child process and then asserts the child is gone within a few seconds for
each shutdown path: stdin EOF, SIGTERM and SIGINT. The whole suite SKIPs when
``opencode`` is not on PATH. See ``tests/README.md``.
"""

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_client import (  # noqa: E402
    McpClient,
    Reporter,
    find_spawned_serve,
    list_servers,
    mcp_env,
    opencode_on_path,
    server_port,
)


def _serve_alive(pid):
    """True when the process exists and is not a zombie."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            state = handle.read().split()[2]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _wait_serve_gone(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _serve_alive(pid):
            return True
        time.sleep(0.2)
    return not _serve_alive(pid)


def _force_kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def run_case(reporter, name, killer):
    client = McpClient(env=mcp_env(use_local_env=False), client_name="live-lifecycle")
    child_pid = None
    try:
        servers, error = list_servers(client)
        local = next((item for item in servers if item.get("name") == "local"), None)
        if not local:
            reporter.skip(name, "could not start a spawned local serve: %s" % (error or "")[:60])
            return
        port = server_port(local.get("url"))
        if port is None:
            reporter.skip(name, "the spawned serve url has no port")
            return
        child_pid = find_spawned_serve(client.pid, port)
        if child_pid is None:
            reporter.skip(name, "could not locate the spawned serve process")
            return
        if not _serve_alive(child_pid):
            reporter.skip(name, "the spawned serve exited before the shutdown test")
            return

        killer(client)
        try:
            client.proc.wait(timeout=15)
        except Exception:
            pass
        gone = _wait_serve_gone(child_pid, 8)
        reporter.check(name, gone, "serve pid %s gone within the timeout" % child_pid)
        if not gone:
            _force_kill(child_pid)
    finally:
        client.close()


def main():
    reporter = Reporter("live_lifecycle")
    if not opencode_on_path():
        reporter.skip("spawned-serve lifecycle", "opencode is not on PATH")
        return reporter.summary()

    run_case(reporter, "stdin EOF stops the spawned serve", lambda c: c.proc.stdin.close())
    run_case(reporter, "SIGTERM stops the spawned serve", lambda c: c.proc.send_signal(signal.SIGTERM))
    run_case(reporter, "SIGINT stops the spawned serve", lambda c: c.proc.send_signal(signal.SIGINT))
    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
