#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp 冒烟测试客户端(纯标准库)。

用法:
    python3 test_client.py                # 仅做 MCP 握手 + tools/list 断言(不发网络请求)
    python3 test_client.py --chat "你好"  # 额外做真实对话:create_session + chat(需要 opencode 在运行)

退出码:0 表示通过,1 表示失败。
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

EXPECTED_TOOLS = {
    "create_session",
    "chat",
    "wait_session",
    "get_messages",
    "permission_reply",
    "form_reply",
    "list_agents",
    "interrupt",
    "pending_interactions",
    "connect_server",
    "list_servers",
    "disconnect_server",
    "list_sessions",
    "compact",
    "get_context",
    "delete_session",
}


class McpClient:
    def __init__(self, server_path=SERVER):
        self.proc = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 1

    def send(self, message):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=30.0):
        msg_id = self._next_id
        self._next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
        )
        return self.read_response(msg_id, timeout=timeout)

    def notify(self, method, params=None):
        self.send(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}
        )

    def read_response(self, expected_id, timeout=30.0):
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待响应超时 (id=%s)" % expected_id)
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("server 进程已退出,未收到响应")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == expected_id:
                return message

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def tool_text(response):
    """从 tools/call 响应中取出文本内容。"""
    result = response.get("result") or {}
    content = result.get("content") or []
    if content and content[0].get("type") == "text":
        return content[0]["text"]
    return json.dumps(result, ensure_ascii=False)


def handshake(client):
    """initialize → notifications/initialized → tools/list,并断言工具清单。"""
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "opencode-mcp-test", "version": "1.0.0"},
        },
    )
    result = init.get("result") or {}
    assert result.get("protocolVersion") == "2024-11-05", (
        "protocolVersion 不匹配: %r" % result.get("protocolVersion")
    )
    server_info = result.get("serverInfo") or {}
    assert server_info.get("name") == "opencode-mcp", (
        "serverInfo.name 错误: %r" % server_info.get("name")
    )
    assert "tools" in (result.get("capabilities") or {}), "capabilities.tools 缺失"

    client.notify("notifications/initialized")

    listed = client.request("tools/list")
    tools = (listed.get("result") or {}).get("tools") or []
    names = [tool.get("name") for tool in tools]
    print("工具数量: %d" % len(tools))
    for name in names:
        print("  - %s" % name)

    missing = EXPECTED_TOOLS - set(names)
    assert not missing, "缺少工具: %s" % ", ".join(sorted(missing))
    assert len(tools) == len(EXPECTED_TOOLS), (
        "工具数量不符,期望 %d 个,实际 %d 个" % (len(EXPECTED_TOOLS), len(tools))
    )
    return tools


def chat_roundtrip(client, text):
    """真实对话测试(需要 opencode 在运行)。"""
    resp = client.request(
        "tools/call",
        {
            "name": "create_session",
            "arguments": {"title": "opencode-mcp smoke test"},
        },
        timeout=60.0,
    )
    session = json.loads(tool_text(resp))
    session_id = session.get("session_id")
    print("创建会话: %s" % session_id)
    assert session_id, "create_session 未返回 session_id"

    resp = client.request(
        "tools/call",
        {
            "name": "chat",
            "arguments": {
                "session_id": session_id,
                "text": text,
                "timeout_secs": 120,
            },
        },
        timeout=180.0,
    )
    if resp.get("result", {}).get("isError"):
        print("chat 返回错误: %s" % tool_text(resp))
        return False
    payload = json.loads(tool_text(resp))
    print("chat status: %s" % payload.get("status"))
    if payload.get("status") == "completed":
        print("assistant_text:\n%s" % payload.get("assistant_text"))
        return True
    print("未完成,payload:\n%s" % json.dumps(payload, ensure_ascii=False, indent=2))
    return payload.get("status") in ("completed", "needs_permission", "needs_form")


def main():
    parser = argparse.ArgumentParser(description="opencode-mcp smoke test")
    parser.add_argument("--chat", metavar="TEXT", help="额外执行真实对话测试")
    args = parser.parse_args()

    client = McpClient()
    try:
        handshake(client)
        if args.chat:
            ok = chat_roundtrip(client, args.chat)
            if not ok:
                print("对话测试失败")
                return 1
        print("冒烟测试通过 ✅")
        return 0
    except Exception as exc:
        print("冒烟测试失败 ❌: %s" % exc)
        try:
            err = client.proc.stderr.read()
            if err:
                print("--- server stderr ---")
                print(err)
        except Exception:
            pass
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
