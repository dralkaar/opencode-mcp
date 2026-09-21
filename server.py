#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp — 纯 Python 标准库实现的 MCP (Model Context Protocol) stdio server。

用于操作本机 opencode 的对话能力(Session / Prompt / 权限 / 表单 / 中断)。
零第三方依赖,仅使用 Python 3 标准库。

传输:MCP over stdio,每行一个 JSON-RPC 2.0 消息(换行分隔,非 LSP Content-Length 帧)。
日志输出到 stderr,协议消息输出到 stdout。
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
# 常量
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
    """把日志写到 stderr,避免污染 stdout 的协议流。"""
    try:
        sys.stderr.write(" ".join(str(p) for p in parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class OpenCodeError(Exception):
    """与 opencode 交互失败。kind ∈ {availability, compatibility, other}。

    other 必须携带可原样回报开发者的原始报错。
    """

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# 连接层:多 opencode 服务端(本地专属拉起 + 动态远端)
# 本地:OPENCODE_URL 显式直连(跳过拉起);否则 MCP 拉起专属 serve
# (随机高位端口 + 随机密码,子进程随本 MCP 实例生命周期)。
# 禁止任何推断性自发现(含 service.json)。
# ---------------------------------------------------------------------------

DEVELOPMENT_BASELINE_VERSION = (
    os.environ.get("OPENCODE_MCP_BASELINE_VERSION") or "2.0.12"
)
DEFAULT_LOCAL_NAME = "local"
SESSION_ROUTE_LIMIT = 1000

_CONNECTIONS = {}  # name -> Connection(_STATE_LOCK 保护)
_SESSION_ROUTE = {}  # session_id -> connection name(插入序,超限淘汰最旧)
_LOCAL_LOCK = threading.Lock()


class Connection:
    """一个 opencode 服务端连接。"""

    def __init__(self, name, base_url, password, is_local=False, source="dynamic"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self.is_local = is_local
        self.source = source  # spawned / env / dynamic
        self.server_version = None
        self.version_checked = False
        self.sessions_warned = {}  # session_id -> 告警时的 server_version
        self.spawned_proc = None

    def auth_header(self):
        if not self.password:
            return None  # 无凭据来源:不发送 Authorization(存在空用户名/密码的远端)
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
    """裸 GET /api/info,返回解析 dict;网络失败抛连接异常,响应非 JSON 抛 ValueError。"""
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
        raise ValueError("响应不是 JSON: %s" % exc)


def _ensure_version(conn):
    """创建时检查(硬门禁)。

    连不上=可用性;可达但 /api/info 非 JSON 或报 404/5xx=兼容性(不是 opencode API);
    401=认证问题(密码不对)。
    """
    try:
        info = _raw_probe(conn)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise OpenCodeError(
                "[availability] %s(%s)认证被拒(401):密码错误或已更换"
                % (conn.name, conn.base_url),
                kind="availability",
            )
        raise OpenCodeError(
            "[compatibility] %s(%s)可达,但 /api/info 返回 HTTP %s,不是 opencode API"
            "(疑似其他服务,基准 v%s)"
            % (conn.name, conn.base_url, exc.code, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except ValueError:
        raise OpenCodeError(
            "[compatibility] %s(%s)可达,但 /api/info 返回的不是 opencode v2 API JSON"
            "(实测案例:未携带正确凭据时请求落到 Web UI 回退,或为其他服务;基准 v%s。"
            "若确认是 opencode,请检查密码)"
            % (conn.name, conn.base_url, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except Exception as exc:
        raise OpenCodeError(
            "[availability] 无法连接 opencode 服务 %s(%s):%s"
            % (conn.name, conn.base_url, exc),
            kind="availability",
        )
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise OpenCodeError(
            "[compatibility] %s 服务可达但 /api/info 无 version 字段,疑似 API 已彻底重构"
            "(开发基准 v%s)" % (conn.name, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    conn.server_version = info["version"]
    conn.version_checked = True
    return info


def _spawn_local_serve():
    """拉起专属本地 serve:随机高位端口 + 随机密码。

    PATH 无 opencode → 可用性错误明示用户环境问题,不重试。
    子进程模式:随本 MCP 实例生命周期,多实例靠随机端口互不冲突。
    """
    if shutil.which("opencode") is None:
        raise OpenCodeError(
            "[availability] PATH 中没有 opencode 命令,无法拉起本地服务。"
            "请安装 opencode 或将其加入 PATH 后重试(用户环境问题,MCP 不再尝试)。",
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
                break  # 端口被占等:换端口重试
            try:
                info = _raw_probe(conn, timeout=2.0)
                if isinstance(info, dict) and info.get("version"):
                    conn.server_version = info["version"]
                    conn.version_checked = True
                    return conn
            except Exception:
                pass
            time.sleep(0.3)
        proc.kill()
    raise OpenCodeError(
        "[availability] 本地 opencode serve 拉起失败(3 个随机端口均未就绪):%s"
        % last_err,
        kind="availability",
    )


def _local_connection():
    """本地连接:显式 env 直连,否则拉起专属 serve;进程内单例。"""
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
            "[opencode-mcp] 本地连接就绪:",
            conn.base_url,
            "version=",
            conn.server_version,
        )
        return conn


def _register_connection(name, base_url, password):
    if not name or not isinstance(name, str):
        raise OpenCodeError("缺少必填参数 name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("连接名 %r 为保留名,不可使用" % name)
    with _STATE_LOCK:
        if name in _CONNECTIONS:
            raise OpenCodeError(
                "连接名已存在:%s(用 list_servers 查看)" % name
            )
    conn = Connection(name, base_url, password, is_local=False, source="dynamic")
    _ensure_version(conn)  # 创建时检查(硬门禁)
    with _STATE_LOCK:
        _CONNECTIONS[name] = conn
    return conn


def _resolve_connection(args):
    """server 参数 > session 路由 > 本地。未知名报错并列出现有连接。"""
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
            "未知连接 %r。现有连接:%s(远端需先 connect_server 注册)"
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


# 版本告警:per-(connection, session),按已告警版本去重(新会话可见,同会话不轰炸)


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
        "message": "opencode 服务(%s)版本 %s 与 MCP 开发基准 %s 不一致,%s,行为可能有差异。"
        % (
            conn.name,
            conn.server_version,
            DEVELOPMENT_BASELINE_VERSION,
            "大版本不同,兼容性风险高" if major else "小版本差异",
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
    result = dict(result)
    result["api_version_warning"] = warning
    return result


def _warn_choke(args, result):
    """tools/call 成功路径的统一告警注入点:per-(connection, session)。"""
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
    """opencode 的响应通常是 {"data": ...};统一取出 data。"""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------------------
# 并发与取消(MCP: notifications/cancelled + 请求线程池)
# ---------------------------------------------------------------------------

# 已被调用方取消的请求 id 集合(读线程写入,poll 线程读取)
_CANCELLED = set()
_STATE_LOCK = threading.Lock()
# stdout 单写者锁:多线程响应必须串行写入
_OUT_LOCK = threading.Lock()
# 当前工作线程正在处理的请求 id(thread-local)
_CURRENT = threading.local()


def _request_cancelled(request_id):
    with _STATE_LOCK:
        return request_id in _CANCELLED


def _current_request_cancelled():
    request_id = getattr(_CURRENT, "request_id", None)
    return request_id is not None and _request_cancelled(request_id)


# ---------------------------------------------------------------------------
# HTTP(带连接上下文与失败分类)
# ---------------------------------------------------------------------------


def http_request(conn, method, path, body=None, query=None):
    """对指定连接执行请求;失败按裁定分类:可用性 / 兼容性 / 其他(dump 原始报错)。"""
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
    except Exception as exc:  # URLError / 超时等
        raise _classify_failure(
            conn, method, path, "%s: %s" % (type(exc).__name__, exc)
        )

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OpenCodeError(
            "[other] 响应不是合法 JSON(%s %s):%s" % (method, path, exc)
        )


def _classify_failure(conn, method, path, original, http_status=None):
    """失败后重查版本号,据此分类 availability / compatibility / other。

    主判据是原始失败形态(GET 404=端点消失=兼容性;网络层=可用性),
    版本重查作佐证并更新连接的版本记录;other 必须保留完整原始报错便于回报。
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
    if http_status == 404 and method == "GET":
        raise OpenCodeError(
            "[compatibility] 端点消失(GET %s -> 404),疑似 API 重构(%s)。原始:%s"
            % (path, ctx, original),
            kind="compatibility",
        )
    if probe is None:
        raise OpenCodeError(
            "[availability] 服务不可达(%s);重查 /api/info 亦失败:%s。原始:%s"
            % (conn.base_url, probe_err, original),
            kind="availability",
        )
    if not (isinstance(probe, dict) and probe.get("version")):
        raise OpenCodeError(
            "[compatibility] 重查 /api/info 无 version 字段,疑似 API 重构(%s)。原始:%s"
            % (ctx, original),
            kind="compatibility",
        )
    raise OpenCodeError(
        "[other] 请求失败(%s)。原始报错:%s。可原样回报开发者。"
        % (ctx, original),
        kind="other",
    )


# ---------------------------------------------------------------------------
# 数据整形辅助
# ---------------------------------------------------------------------------

def _text_parts(message):
    """取 assistant 消息里所有 text part 的文本。"""
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
    """取任意消息的可读文本。"""
    if isinstance(message.get("text"), str):
        return message["text"]
    return "\n".join(_text_parts(message))


def _is_pending_form(item):
    """列表项可能带 state.status,只把 pending 视为待处理;无 state 视为 pending。"""
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


def fetch_messages(conn, session_id, order="asc", limit=100):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/message" % urllib.parse.quote(session_id, safe=""),
        query={"order": order, "limit": limit},
    )
    data = unwrap(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    return []


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
# 统一等待核心(chat / wait_session / compact 共用)
# 终态判定只信权威字段 Session.outcome(succeeded/failed/interrupted)+ gate 消息
# 时间戳;不做消息形状推断(历史五轮 bug 均源于形状启发式,已全部废弃)。
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
    """终态返回体;last_message_id 恒供增量游标,with_result 时附带本轮新回复。"""
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
                "note", "会话已处于完成/空闲态,本轮无新增回复。"
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
    """轮询会话直到终态 / 待交互 / 超时 / 取消(统一等待核心)。

    返回 (status, payload)。status ∈ {succeeded, failed, interrupted,
    compaction_failed, needs_permission, needs_form, timeout, cancelled}
    """
    started = time.monotonic()
    gate_created = None
    while True:
        if _current_request_cancelled():
            return "cancelled", {"status": "cancelled", "note": "调用方已取消本次请求"}

        # a. 权限请求
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
                        log("[opencode-mcp] 权限自动答复失败", rid, exc)
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
                    "note": "用 permission_reply 答复后调用 wait_session 继续等待。",
                }

        # b. 表单请求
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
                "note": "用 form_reply 答复后调用 wait_session 继续等待。",
            }

        # c. 会话权威状态
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

        # gate 消息:缓存 created 时间戳;compact 场景直接认其消息终态
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
                                "note": "上下文压缩失败(compaction status=failed)。",
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

        # d. 超时(诊断块附带最后一条消息与本轮部分文本)
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
                        "get_messages 查看当前进度",
                        "pending_interactions 检查待处理交互",
                        "wait_session 继续等待",
                        "interrupt 中断生成",
                    ],
                },
                "note": "等待超时(%s 秒),会话仍在生成中。" % timeout_secs,
            }

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

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
                "model_id 需为 'providerID/modelID' 格式,收到: %r" % model_id
            )
        provider_id, model = model_id.split("/", 1)
        # Model.Ref 的形状是 {"id": ..., "providerID": ...}(实测 "modelID" 键会被 400 拒绝)
        body["model"] = {"providerID": provider_id, "id": model}

    location = args.get("location")
    if location is not None:
        if not isinstance(location, dict):
            raise OpenCodeError(
                "location 必须是对象,格式 {\"directory\": \"/path/to/project\"}"
            )
        directory = location.get("directory")
        if not isinstance(directory, str) or not directory:
            raise OpenCodeError("location.directory 为必填字符串")
        # 按 openapi.json 的 Location.PublicRef 形状透传:{directory}
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
        raise OpenCodeError("缺少必填参数 session_id")
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
        raise OpenCodeError("缺少必填参数 session_id")
    timeout_secs = int(args.get("timeout_secs", 120) or 120)
    # 远端连接默认 manual(审批过程必在调用方),本地默认 once
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()
    if auto_permission not in VALID_AUTO_PERMISSION:
        raise OpenCodeError(
            "auto_permission 必须是 once/always/reject/manual 之一,收到: %r"
            % auto_permission
        )

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))

    text = args.get("text")
    if text is None or text == "":
        raise OpenCodeError("缺少必填参数 text")
    body = {"text": text}

    delivery = args.get("delivery")
    if delivery is not None:
        if delivery not in ("steer", "queue"):
            raise OpenCodeError(
                "delivery 必须是 steer / queue,收到: %r" % delivery
            )
        body["delivery"] = delivery

    files = args.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise OpenCodeError("files 必须是数组,例如 [{\"uri\": \"...\"}]")
        for idx, item in enumerate(files):
            if not isinstance(item, dict) or not item.get("uri"):
                raise OpenCodeError("files[%d] 缺少必填字段 uri" % idx)
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
    """等待会话进入终态或待交互状态(纯状态原语,不返回消息内容)。"""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("缺少必填参数 session_id")
    timeout_secs = int(args.get("timeout_secs", 120) or 120)
    _route_session(session_id, conn)
    status, payload = _run_until_terminal(
        conn, session_id, timeout_secs, auto_permission="manual", with_result=False
    )
    if status == "succeeded" and "note" not in payload:
        payload["note"] = "用 get_messages(after_message_id=...) 拉取增量回复。"
    return payload


def tool_get_messages(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("缺少必填参数 session_id")
    limit = int(args.get("limit", 50) or 50)
    after = args.get("after_message_id")
    note = None
    _route_session(session_id, conn)
    if after:
        # 增量拉取:取最近最多 200 条,截掉 after_message_id 及之前的
        messages = fetch_messages(conn, session_id, order="asc", limit=200)
        idx = next(
            (i for i, m in enumerate(messages) if m.get("id") == after), -1
        )
        if idx >= 0:
            messages = messages[idx + 1 :]
        else:
            note = "after_message_id 不在最近 200 条内,已返回全量列表。"
        if len(messages) > limit:
            messages = messages[-limit:]
    else:
        messages = fetch_messages(conn, session_id, order="asc", limit=limit)
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
        raise OpenCodeError("缺少必填参数 session_id / request_id")
    if decision not in ("once", "always", "reject"):
        raise OpenCodeError(
            "decision 必须是 once / always / reject,收到: %r" % decision
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
        raise OpenCodeError("缺少必填参数 session_id / form_id")
    if not isinstance(answer, dict):
        raise OpenCodeError("answer 必须是对象,例如 {\"字段key\": 值}")
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
        raise OpenCodeError("缺少必填参数 session_id")
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
        "note": "model 为 null 表示该 agent 未显式配置模型(运行时回落位置默认模型)。"
        "如需会话使用某 agent 的模型,请由调用方将 providerID/modelID 传给 create_session 的 model_id。",
    }


def tool_pending_interactions(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("缺少必填参数 session_id")
    permissions = fetch_permissions(conn, session_id)
    forms = [_form_summary(f) for f in fetch_forms(conn, session_id, pending_only=True)]
    return {
        "server": conn.name,
        "session_id": session_id,
        "permissions": permissions,
        "forms": forms,
    }


def _session_time(raw):
    """从 Session.Info 的 time 字段取 updated / idle。"""
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
        raise OpenCodeError("缺少必填参数 session_id")
    timeout_secs = int(args.get("timeout_secs", 120) or 120)
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
        raise OpenCodeError("缺少必填参数 session_id")
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
    """注册并验证一个远端连接(创建时检查硬门禁)。仅进程内有效,不持久化。"""
    name = args.get("name")
    url = args.get("url")
    if not name or not url:
        raise OpenCodeError("缺少必填参数 name / url")

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
                "[other] password_file 读取失败(%s):%s" % (password_file, exc)
            )
        if not first:
            raise OpenCodeError(
                "[other] password_file 首行为空(%s)" % password_file
            )
        password = first
        source = "file"
    if password is None and password_env:
        password = os.environ.get(password_env)
        if not password:
            raise OpenCodeError(
                "[availability] 环境变量 %s 未设置或为空" % password_env,
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
    _local_connection()  # 确保本地连接已就绪(拉起或直连)
    with _STATE_LOCK:
        conns = list(_CONNECTIONS.values())
    return {
        "count": len(conns),
        "servers": [c.describe() for c in conns],
    }


def tool_disconnect_server(args):
    name = args.get("name")
    if not name:
        raise OpenCodeError("缺少必填参数 name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("本地连接不可移除")
    conn = _remove_connection(name)
    if conn is None:
        raise OpenCodeError("连接不存在:%s(用 list_servers 查看)" % name)
    if conn.spawned_proc is not None:
        try:
            conn.spawned_proc.kill()
        except Exception:
            pass
    return {"ok": True, "removed": name}




# ---------------------------------------------------------------------------
# 工具清单(schema + 中文说明)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_session",
        "description": (
            "在本机 opencode 上创建一个新的对话会话。返回 session_id(ses_...),"
            "后续 chat / get_messages 等工具都使用它。可选指定标题、agent、模型,"
            "以及 location(在指定目录/项目位置创建会话)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "title": {"type": "string", "description": "会话标题(可选)"},
                "agent": {"type": "string", "description": "使用的 agent 名称(可选)"},
                "model_id": {
                    "type": "string",
                    "description": "模型,格式为 providerID/modelID,例如 \"anthropic/claude-sonnet-4\"(可选)",
                },
                "location": {
                    "type": "object",
                    "description": "会话的位置(可选),用于在指定目录/项目创建会话。按 opencode Location.PublicRef 形状透传。",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "工作目录绝对路径(必填)",
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
            "向指定会话发送一条 prompt 并等待 opencode 回复。内部会轮询消息、"
            "权限请求和表单请求。auto_permission=once/always/reject 时自动答复权限请求;"
            "manual 时遇到权限请求立即返回,需再调用 permission_reply + wait_session。"
            "遇到表单请求会返回 needs_form,需调用 form_reply + wait_session。"
            "可选 delivery:steer=运行中直接转向(打断当前生成方向),"
            "queue=排队到本轮结束后生效;不传则该字段不下发。"
            "可选 files:随 prompt 附带的文件数组,每项 {uri(必填), name?, description?}。"
            "返回 status: succeeded(成功,含 assistant_text/tools_used/reasoning)、"
            "failed(失败)、interrupted(被中断)、"
            "needs_permission(等待授权,含 requests 列表)、needs_form(等待填表)、"
            "timeout(超时,含 partial_text 与 diagnostics)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "text": {"type": "string", "description": "要发送的提示词内容"},
                "timeout_secs": {
                    "type": "integer",
                    "description": "最长等待秒数,默认 120",
                    "default": 120,
                },
                "auto_permission": {
                    "type": "string",
                    "enum": ["once", "always", "reject", "manual"],
                    "description": "对权限请求的处理方式,默认 once(本次允许)。manual 表示不做自动答复,交由调用方处理。",
                    "default": "once",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["steer", "queue"],
                    "description": "发送方式(可选)。steer=运行中直接转向(打断当前生成方向),queue=排队到本轮结束后生效;不传则不下发该字段。",
                },
                "files": {
                    "type": "array",
                    "description": "随 prompt 附带发送的文件数组(可选)。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "description": "文件 URI(必填)"},
                            "name": {"type": "string", "description": "文件名(可选)"},
                            "description": {"type": "string", "description": "文件说明(可选)"},
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
            "等待指定会话进入终态或待交互状态(纯状态原语,不返回消息内容)。"
            "终态 status:succeeded(本轮成功结束)/ failed(失败)/ interrupted(被中断),"
            "来自会话权威字段 outcome;阻塞态:needs_permission / needs_form(答复后再次调用);"
            "timeout 表示超时时仍在生成。返回 last_message_id 作为 get_messages 的增量游标,"
            "配合 get_messages(after_message_id=...) 拉取新回复。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "timeout_secs": {
                    "type": "integer",
                    "description": "最长等待秒数,默认 120",
                    "default": 120,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_messages",
        "description": (
            "按时间升序获取会话消息记录,格式化输出用户/助手文本、工具调用摘要与时间戳。"
            "支持增量拉取:传 after_message_id(上次返回的 last_message_id 或某条消息 id),"
            "只返回其之后的新消息;响应含 last_message_id 供下次游标。"
            "典型组合:wait_session 等到终态后用它拉取新回复。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "limit": {
                    "type": "integer",
                    "description": "返回的消息条数上限,默认 50",
                    "default": 50,
                },
                "after_message_id": {
                    "type": "string",
                    "description": "增量游标(可选):只返回该消息之后的新消息",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "permission_reply",
        "description": (
            "答复某个权限请求。decision=once 表示仅本次允许,always 表示始终允许并保存,"
            "reject 表示拒绝。可选 message 用于附带说明。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "request_id": {"type": "string", "description": "权限请求 ID(per_...)"},
                "decision": {
                    "type": "string",
                    "enum": ["once", "always", "reject"],
                    "description": "授权决定",
                },
                "message": {"type": "string", "description": "可选说明信息"},
            },
            "required": ["session_id", "request_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "form_reply",
        "description": (
            "提交某个表单(form)的答案。answer 是对象,键为字段 key,值支持 "
            "string / number / boolean / string[]。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "form_id": {"type": "string", "description": "表单 ID(frm_...)"},
                "answer": {
                    "type": "object",
                    "description": "答案对象,例如 {\"name\": \"foo\", \"count\": 3}",
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
            "列出本机 opencode 的全部 agent 及其解析后的默认模型(只读)。"
            "model 为 null 表示未显式配置(将回落位置默认模型)。"
            "需要会话与某 agent 的模型一致时,由调用方将对应模型以 providerID/modelID "
            "格式传给 create_session 的 model_id;本工具只提供信息,不替调用方钉扎模型。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "interrupt",
        "description": "中断指定会话当前正在进行的生成。适合在 chat 返回 timeout 后取消长时间任务。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_interactions",
        "description": (
            "查询指定会话当前待处理的人工交互,返回 permissions(权限请求)和 forms(表单)列表。"
            "用于在不阻塞的情况下了解会话是否在等待授权或填表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "枚举 / 搜索既有会话,支持关键词、排序、目录过滤与游标翻页。"
            "适合查找历史话题,拿到 session_id 后配合 chat 恢复之前的对话(session_id 即恢复句柄)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "search": {"type": "string", "description": "按标题/内容搜索的关键词(可选)"},
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限,默认 20",
                    "default": 20,
                },
                "order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "按更新时间排序,默认 desc(最新在前)",
                    "default": "desc",
                },
                "directory": {"type": "string", "description": "按工作目录过滤(可选)"},
                "cursor": {"type": "string", "description": "翻页游标,取自上次返回的 cursor.next(可选)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "压缩指定会话的上下文,等待压缩结束并返回结果。"
            "返回 status: succeeded(压缩完成)、compaction_failed(压缩失败)、"
            "timeout(超时)。适合上下文接近上限时主动瘦身,之后可继续 chat。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"},
                "timeout_secs": {
                    "type": "integer",
                    "description": "最长等待秒数,默认 120",
                    "default": 120,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context",
        "description": (
            "查看指定会话的上下文占用(token / 成本)与元信息,"
            "用于配合 compact 判断是否需要压缩。tokens / cost 缺省为 null。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "会话 ID(ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_session",
        "description": (
            "删除指定会话。警告:该操作不可逆,且会级联删除其所有子会话"
            "(实测删除父会话后,子会话再访问返回 404)。删除前请确认不再需要这些会话。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "(可选)目标连接名,缺省 local;带 session_id 的调用可自动路由到创建它的连接"},
                "session_id": {"type": "string", "description": "要删除的会话 ID(ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "connect_server",
        "description": (
            "注册并验证一个远端 opencode 连接(仅本进程有效,不持久化)。"
            "建连即做创建时检查:连不上=可用性错;无版本=兼容性错;版本与基准不一致会返回告警。"
            "凭据优先级:password_file(首行)> password_env > password 明文;"
            "全部缺省时不发送 Authorization(存在空用户名/密码的远端)。"
            "返回 {name, url, version, baseline, baseline_check}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "连接别名(句柄),不可为 local"},
                "url": {"type": "string", "description": "如 http://host:4096"},
                "password_file": {"type": "string", "description": "(可选)密码文件路径,取首行"},
                "password_env": {"type": "string", "description": "(可选)密码所在环境变量名"},
                "password": {"type": "string", "description": "(可选)明文密码,最差选择"},
            },
            "required": ["name", "url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_servers",
        "description": (
            "列出当前全部连接(local + 动态远端):名称、地址、来源、版本、与开发基准的比对状态。"
            "会确保本地连接就绪(必要时拉起本地 serve)。"
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
        "description": "移除一个动态注册的远端连接(local 不可移除)。其下会话路由一并清除。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "连接别名"},
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
# MCP JSON-RPC 处理
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
    """处理单条 JSON-RPC 消息,返回响应 dict 或 None(通知无需响应)。"""
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

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

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
                "result": _tool_error("未知工具: %s" % name),
            }
        try:
            result = handler(arguments)
            result = _warn_choke(arguments, result)
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_result(result)}
        except OpenCodeError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_error(str(exc))}
        except Exception as exc:  # 任何异常都转成工具错误,不让 server 崩溃
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("工具执行失败 (%s): %s" % (name, exc)),
            }

    # 通知(无 id)一律忽略
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
    """在工作线程中处理单个请求;已取消的请求不再回写响应。"""
    msg_id = message.get("id")
    _CURRENT.request_id = msg_id
    try:
        try:
            response = handle_message(message)
        except Exception as exc:  # 兜底,避免任何异常终止进程
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal error: %s" % exc},
            }
        if response is None:
            return
        if _request_cancelled(msg_id):
            log("[opencode-mcp] 请求已取消,丢弃响应:", msg_id)
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
    # 读线程只负责解析与分发,保证取消通知能即时送达
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

        # 取消通知:在读线程即时处理,不进线程池
        if method == "notifications/cancelled":
            cancelled_id = (message.get("params") or {}).get("requestId")
            if cancelled_id is not None:
                with _STATE_LOCK:
                    _CANCELLED.add(cancelled_id)
                log("[opencode-mcp] 收到取消请求:", cancelled_id)
            continue

        # 其余通知(无 id)一律忽略
        if msg_id is None:
            continue

        executor.submit(_handle_request, message)


if __name__ == "__main__":
    main()
