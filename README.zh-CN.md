# opencode-mcp

[English](README.md) | **中文**

**为什么需要它**：在 agent 普遍以 ACP(Agent Client Protocol)客户端身份直连 opencode 之前,通过 **HTTP API** 以 MCP 驱动 opencode 是 agent 操作它的最佳方案——本 server 就是为此而建:每个工具对应一套干净、机器可消费的状态机(权威终态、阻塞交互态、增量游标),而不是界面的复刻,天然适合 agent 编排。

实测环境:

- **opencode v2.0.12** —— 开发基准,全部能力均对其 live 验证
- **真实远端实例** —— 完整网络端到端(连接 → 建会话 → 对话 → manual 权限 → 答复 → 等待 → 增量拉取 → 断连)
- **Hermes agent gateway** —— 作为工具提供方挂载并在生产使用
- **oh-my-opencode-slim 编排框架** —— 托管本 MCP 并借此驱动嵌套 opencode 会话

## 范围

这**不是**面面俱到的 opencode 控制面,也不打算是。范围刻意收敛到 agent 日常操作 opencode 真正需要的能力:

- 创建/恢复会话、发送 prompt、收集结果
- 处理两种阻塞交互 —— 权限请求与表单
- 上下文管理(用量查看、压缩)与会话生命周期
- 多 opencode 服务端(本地与远端)的路由

文件系统、凭据、provider、插件、终端、配置等管理面**刻意不做**。工具更少、语义更锋利、出错面更小。

## 用 LLM 安装

把下面这段交给你的编码 agent:

```text
帮我安装并注册 opencode-mcp:

1. 克隆: git clone https://github.com/yitro-z-wang/opencode-mcp ~/opencode-mcp
2. 验证: 运行 `python3 ~/opencode-mcp/test_client.py` —— 必须报告 16 个工具且通过。
3. 注册到 opencode: `opencode mcp add opencode-local -- python3 ~/opencode-mcp/server.py`
4. 重载 opencode 配置,然后新开一个会话确认 16 个 opencode-local 工具可用。

要求: Python 3.10+ 且 opencode CLI 在 PATH 上(v2.0.12 为开发基准)。
出错请原样回报,不要盲目重试。
```

## 特性

- **零依赖** —— 纯 Python 3 标准库,单文件,无构建步骤
- **多服务器** —— MCP 专属拉起本地 serve(随机端口+随机密码,随 MCP 退出)或 `OPENCODE_URL` 显式直连;远端经 `connect_server` 动态注册;会话记住归属连接并自动路由
- **权威状态,不做猜测** —— 终态取自 opencode 的 `Session.outcome` 字段,而非消息形状推断
- **失败分类** —— 每次失败归类为 `[availability] / [compatibility] / [other]`,other 保留原始报错便于回报;版本不一致按 (连接, 会话, 版本) 只告警一次
- **安全默认** —— 远端 `chat` 默认手动审批;凭据 `文件 > env > 明文`,无凭据时不发送 Authorization 头
- **可组合原语** —— `wait_session`(纯状态)与 `get_messages`(增量游标)把“等待”和“读取”分离
- **生产级运行时** —— 并发请求处理、MCP 标准取消、有界等待

## 工具

| 工具 | 作用 |
| --- | --- |
| `create_session` | 创建会话(可选 title / agent / model / location) |
| `chat` | 发送 prompt 并等待终态;支持附件与 `steer` / `queue` 投递;可自动答复权限请求;此处 `wait_for_subagents` 默认 `false`(本轮停止流式输出即返回,并报告仍在运行的内容) |
| `wait_session` | 纯状态等待:`succeeded` / `failed` / `interrupted` / `needs_permission` / `needs_form` / `timeout`;`wait_for_subagents` 默认 `true`(被委派的子 agent 仍在运行时不会报告 `succeeded`) |
| `get_messages` | 读取消息记录;经 `after_message_id` 增量拉取 |
| `permission_reply` | 答复权限请求:`once` / `always` / `reject` |
| `form_reply` | 按字段提交表单答案 |
| `list_agents` | 列出 agent 及其解析后的默认模型(只读) |
| `interrupt` | 中断当前生成 |
| `pending_interactions` | 非阻塞查询待处理权限/表单(聚合该会话的子 agent 子树) |
| `list_sessions` | 枚举/搜索会话 —— 恢复历史话题的句柄 |
| `compact` | 压缩上下文并等待完成 |
| `get_context` | token/成本用量与会话元信息 |
| `delete_session` | 删除会话(不可逆,级联删除子会话) |
| `connect_server` | 注册并验证远端 opencode 连接 |
| `list_servers` | 列出全部连接及版本/基准状态 |
| `disconnect_server` | 移除动态注册的远端连接 |

全部工具支持可选 `server` 参数;带 `session_id` 的调用自动路由到该会话所属连接。


## 连接模型:本地拉起 + 多服务器

**禁止推断性自发现(含 service.json)。** 本地连接两种形态:

1. **显式直连**:`OPENCODE_URL` 设置时,local 直接指向该地址(密码取 `OPENCODE_PASSWORD`,缺省 `opencode`)。
2. **专属拉起**(默认):首次需要 local 时,MCP 自己拉起一个 `opencode serve`——**随机高位端口 + 随机密码**(经 `OPENCODE_SERVER_PASSWORD` 注入),子进程模式随本 MCP 实例退出:**每个 MCP 进程最多拉起一个**(进程内单例),并在 stdin EOF(host 正常关闭)与 `SIGTERM` / `SIGINT` / `SIGHUP` 时被杀掉并回收。多个 MCP 实例靠随机端口互不冲突,也不会累积——每次重启都会清理自己的子进程。唯一缺口是 `SIGKILL`:任何平台的进程都无法拦截它,被硬杀时可能残留一个 serve(后续不会认领它——按设计不做推断性发现)。`PATH` 中无 `opencode` 时返回可用性错误(用户环境问题,不重试)。

**多服务器**:`connect_server(name, url, password_file?/password_env?/password?)` 注册远端(仅进程内有效,不持久化);全部工具带可选 `server` 参数(缺省 local);带 `session_id` 的调用自动路由到创建该会话的连接。凭据优先级:**文件 > env > 明文**;全部缺省时不发送 Authorization 头(存在空用户名/密码的远端)。

**版本基准与失败分类**:每个连接创建时硬门禁(连不上=`[availability]`;可达但非 opencode API=`[compatibility]`);调用失败后重查版本,据此分类为 `[availability] / [compatibility] / [other]`(other 附完整原始报错,可原样回报开发者)。版本与基准不一致时,向触碰该连接会话的**第一个工具结果**注入一次 `api_version_warning`(按 (连接, 会话, 版本) 去重,新会话可见、同会话不轰炸)。

**权限默认**:本地连接 chat 默认 `auto_permission="once"`;**远端连接默认 `manual`**(审批过程必在调用方);`once/always/reject` 均可显式选用。

### 环境变量

- `OPENCODE_URL`:显式指定 local 直连地址(跳过拉起),例如 `http://127.0.0.1:4096`。
- `OPENCODE_PASSWORD`:local 直连的 HTTP Basic 密码;用户名固定 `opencode`,缺省 `opencode`。
- `OPENCODE_MCP_WORKERS`:请求处理线程数,默认 `4`。设为 `1` 则退化为严格串行。
- `OPENCODE_MCP_BASELINE_VERSION`:开发基准版本覆盖(默认 `2.0.12`,主要用于测试)。

## 并发与取消

- **并发**:每个 JSON-RPC 请求在独立工作线程中处理(`OPENCODE_MCP_WORKERS` 控制,默认 4)。`chat` / `wait_session` 这类长阻塞调用不会卡住其他工具调用。
- **取消**:支持 MCP 标准的 `notifications/cancelled`。调用方取消 `chat` / `wait_session` 后,轮询会在 1 秒内停止(该请求不再回写响应;正在途中的单次 HTTP 请求最长 30 秒自然超时)。

## 版本基准与告警

版本与告警机制已并入「连接模型:本地拉起 + 多服务器」一节:每连接创建时硬门禁、失败后重查版本并分类(availability / compatibility / other)、告警按 (连接, 会话, 版本) 去重注入。基准版本默认 `2.0.12`,可用 `OPENCODE_MCP_BASELINE_VERSION` 覆盖(主要用于测试)。

## 模型选择说明

- 本 MCP **不替调用方钉扎模型**:`create_session` 不传 `model_id` 时,会话 `model=null`,运行时会回落到 **位置默认模型**(可通过 `GET /api/model/default` 查询)。
- 实测(opencode v2.0.12):位置默认模型**不会**跟随 agent 配置——TUI 和 opencode 内部 spawn 在创建会话时会显式钉模型,裸 API 建的会话则回落位置默认。需要指定模型时显式传 `model_id`(格式 `providerID/modelID`)。
- **读取 agent→模型映射**:用 `list_agents` 工具(数据来自 `GET /api/agent`,已包含插件解析结果)。想与会话与某 agent 的行为对齐,由调用方读取映射后显式传 `model_id`。



## 注册到 opencode

```bash
opencode mcp add opencode-local -- python3 /root/opencode-mcp/server.py
```

也可以直接手动配置(示例):

```json
{
  "mcp": {
    "opencode-local": {
      "type": "local",
      "command": ["python3", "/root/opencode-mcp/server.py"]
    }
  }
}
```

## 工具清单

### 1. `create_session`

创建一个新的 opencode 会话。

参数:

- `title?: string` — 会话标题。
- `agent?: string` — 使用的 agent 名称。
- `model_id?: string` — 模型,格式 `providerID/modelID`,例如 `anthropic/claude-sonnet-4`。
- `location?: { directory: string }` — 会话的位置,用于**在指定目录/项目创建会话**。按 opencode `Location.PublicRef` 形状透传(`directory` 必填,绝对路径);不传则不指定。

返回:`{ "session_id": "ses_...", "title": ..., "agent": ..., "model": ... }`

```json
{"name": "create_session", "arguments": {"title": "我的任务", "model_id": "anthropic/claude-sonnet-4"}}
```

带 `location` 的示例:

```json
{"name": "create_session", "arguments": {"title": "在项目里工作", "location": {"directory": "/root/my-project"}}}
```

### 2. `chat`

向指定会话发送一条 prompt 并等待回复。内部按 1 秒间隔轮询消息、权限请求与表单请求。

参数:

- `session_id: string` — 会话 ID(`ses_...`)。
- `text: string` — 提示词内容。
- `timeout_secs?: int` — 最长等待秒数,默认 `120`。
- `auto_permission?: "once" | "always" | "reject" | "manual"` — 权限处理方式,默认 `once`。
- `delivery?: "steer" | "queue"` — 发送方式,不传则不下发该字段。`steer` = 运行中直接转向(打断当前生成方向);`queue` = 排队到本轮结束后生效。非法值报错。
- `files?: [{uri, name?, description?}]` — 随 prompt 附带发送的文件数组,透传为 prompt body 的 `files`。每项 `uri` 必填,缺失则报错。

返回 `status` 的语义:

| status | 含义 | 建议动作 |
| --- | --- | --- |
| `succeeded` | 本轮成功结束。含 `assistant_text`(合并后的助手文本)、`tools_used`(工具名与状态)、可选的 `reasoning`、`last_message_id`(增量游标) | 直接使用结果 |
| `failed` | 本轮失败(会话权威 outcome=failed) | `get_messages` 查看失败前进度 |
| `interrupted` | 本轮被中断 | 需要的话重新 chat 继续 |
| `needs_permission` | 有权限请求且 `auto_permission=manual`,未自动答复。含 `requests` 列表(id/action/resources/save) | 调用 `permission_reply` 后调用 `wait_session` |
| `needs_form` | 有表单需要填写。含 `forms`(字段 key/title/type/required/options/description) | 调用 `form_reply` 后调用 `wait_session` |
| `timeout` | 超时未完成。含 `partial_text` 与 `diagnostics`(最后消息类型/状态/completed、待处理权限/表单数、建议动作) | 按 `diagnostics.suggested_actions` 处理:`get_messages` / `pending_interactions` / `wait_session` / `interrupt` |
| `compaction_failed` | 轮询期间检测到压缩消息 `status=failed`(通常由 `compact` 触发)。 | 检查会话状态后重试或改用其他方式 |

终态判定只信会话权威字段 `outcome`(`GET /api/session`):`succeeded / failed / interrupted`;发送过 prompt 时以「`time.idle` 晚于该 prompt 消息的创建时间」确认是本轮的终态(排队/转向时上一轮的旧终态不算)。无消息形状推断。

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "列出当前目录文件", "timeout_secs": 120, "auto_permission": "once"}}
```

带 `delivery` / `files` 的示例:

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "根据附件继续", "delivery": "steer", "files": [{"uri": "file:///root/a.md", "name": "a.md", "description": "参考文档"}]}}
```

### 3. `wait_session`

**纯状态原语**:等待会话进入终态或待交互状态,不返回消息内容。终态来自会话权威字段 `outcome`。

参数:`session_id`(必填)、`timeout_secs?`(默认 120)。

返回 `status`:
- `succeeded` / `failed` / `interrupted` — 本轮终态(含 `last_message_id` 增量游标、`time_idle`)
- `needs_permission` — 被权限请求阻塞(含 `requests` 列表)
- `needs_form` — 被表单阻塞(含 `forms` 字段详情)
- `timeout` — 超时时仍在生成(含 `diagnostics`)

```json
{"name": "wait_session", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

典型组合:答复权限/表单后 `wait_session` 等终态,再 `get_messages(after_message_id=...)` 拉增量回复。

### 4. `get_messages`

按时间升序获取并格式化会话历史。

参数:`session_id`(必填)、`limit?`(默认 50)。

返回 `{ "session_id", "count", "messages": [...] }`,每条消息包含 `id`、`type`、`time`;用户/助手消息含 `text`,助手消息另含 `tools`(工具摘要)与 `completed`,工具/推理内容会一并汇总。

```json
{"name": "get_messages", "arguments": {"session_id": "ses_abc", "limit": 20}}
```

### 5. `permission_reply`

答复某个权限请求。

参数:

- `session_id: string`
- `request_id: string` — 权限请求 ID(`per_...`)。
- `decision: "once" | "always" | "reject"` — `once` 本次允许;`always` 始终允许并保存;`reject` 拒绝。
- `message?: string` — 可选说明。

```json
{"name": "permission_reply", "arguments": {"session_id": "ses_abc", "request_id": "per_xyz", "decision": "once"}}
```

### 6. `form_reply`

提交表单答案。`answer` 的键为字段 `key`,值支持 `string` / `number` / `boolean` / `string[]`。

参数:`session_id`、`form_id`(`frm_...`)、`answer`(对象)。

```json
{"name": "form_reply", "arguments": {"session_id": "ses_abc", "form_id": "frm_xyz", "answer": {"name": "foo", "count": 3, "tags": ["a", "b"]}}}
```

### 7. `interrupt`

中断指定会话当前正在进行的生成。

```json
{"name": "interrupt", "arguments": {"session_id": "ses_abc"}}
```

### 8. `list_agents`

列出全部 agent 及其解析后的默认模型(只读,不修改任何东西):

```json
{ "count": 12, "agents": [ { "name": "build", "mode": "primary", "model": null }, ... ] }
```

用途:读取 agent→模型映射。想创建与某 agent 模型一致的会话时,由调用方显式传 `create_session(model_id="providerID/modelID")`。

### 9. `pending_interactions`

查询会话当前待处理的人工交互,返回 `{ "permissions": [...], "forms": [...] }`(表单只列出 pending 状态)。不阻塞,适合先探一下会话是否在等授权/填表。

```json
{"name": "pending_interactions", "arguments": {"session_id": "ses_abc"}}
```

### 10. `list_sessions`

枚举 / 搜索既有会话,支持关键词、排序、目录过滤与游标翻页。拿到 `session_id` 后配合 `chat` 即可恢复之前的话题。

参数(全部可选):

- `search?: string` — 按标题/内容搜索的关键词。
- `limit?: int` — 返回条数上限,默认 `20`。
- `order?: "asc" | "desc"` — 按更新时间排序,默认 `desc`。
- `directory?: string` — 按工作目录过滤。
- `cursor?: string` — 翻页游标,取自上次返回的 `cursor.next`。

返回:`{ "count": N, "sessions": [ { "id", "title", "agent", "model", "parentID", "time": { "updated", "idle"? } } ], "cursor": { "previous", "next" } }`;缺失字段为 `null`。

```json
{"name": "list_sessions", "arguments": {"search": "部署", "limit": 20, "order": "desc"}}
```

### 11. `compact`

压缩指定会话的上下文,等待压缩结束并返回结果。

参数:`session_id`(必填)、`timeout_secs?`(默认 `120`)。

返回与 `chat` 同构的 `status`:

- `succeeded` — 压缩完成(compaction 消息 `status=completed` 或会话 outcome 终态)。
- `compaction_failed` — 压缩失败(`compaction status=failed`)。
- `timeout` — 超时。

```json
{"name": "compact", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

### 12. `get_context`

查看指定会话的上下文占用(token / 成本)与元信息,配合 `compact` 判断是否需要压缩。

参数:`session_id`(必填)。

返回:`{ "id", "title", "agent", "model", "parentID", "tokens", "cost", "time": { "updated", "idle"? }, "revert" }`;`tokens` / `cost` / `revert` 缺省为 `null`。

```json
{"name": "get_context", "arguments": {"session_id": "ses_abc"}}
```

### 13. `delete_session`

删除指定会话。

> ⚠️ **不可逆**:会话一旦删除无法恢复。
> ⚠️ **级联删除**:删除父会话会一并删除其所有子会话(实测删除父会话后,子会话再访问返回 `404`)。

参数:`session_id`(必填)。

返回:`{ "ok": true, "session_id": "ses_..." }`(opencode 返回 204 无 body)。

```json
{"name": "delete_session", "arguments": {"session_id": "ses_abc"}}
```

## 权限 / 表单交互流程

### 自动模式(默认)

`chat` 的 `auto_permission` 默认为 `once`,遇到权限请求会自动答复并继续等待,通常一次调用即可拿到 `succeeded`。

### 手动模式

当希望人工决定是否授权时,使用 `auto_permission: "manual"`:

1. 调用 `chat(..., auto_permission="manual")`,若遇到权限请求,返回:

   ```json
   {
     "status": "needs_permission",
     "requests": [
       {"id": "per_1", "sessionID": "ses_abc", "action": "bash", "resources": ["rm -rf ..."]}
     ]
   }
   ```

2. 根据 `requests` 内容决定策略,调用 `permission_reply(session_id, request_id, decision, message?)`。
3. 调用 `wait_session(session_id)` 继续等待。若又出现新的权限请求,会返回 `needs_permission`,重复步骤 2–3;终态返回 `succeeded` / `failed` / `interrupted`,再用 `get_messages(after_message_id=...)` 拉取增量回复。

### 表单流程

1. 调用 `chat` 或 `wait_session` 时若遇到表单,返回:

   ```json
   {
     "status": "needs_form",
     "forms": [
       {
         "id": "frm_1",
         "title": "请选择部署环境",
         "fields": [
           {"key": "env", "title": "环境", "type": "string", "required": true,
            "options": [{"value": "dev", "label": "开发"}, {"value": "prod", "label": "生产"}]}
         ]
       }
     ]
   }
   ```

   `fields` 里的 `type` 可能为 `string` / `number` / `integer` / `boolean` / `multiselect` / `external`;`multiselect` 的答案用字符串数组。

2. 调用 `form_reply(session_id, form_id, answer)`,例如 `answer = {"env": "prod"}`。
3. 调用 `wait_session(session_id)` 继续等待生成完成。

> 提示:若某个权限请求已经被其他途径处理,`permission_reply` 可能返回错误;此时直接调用 `wait_session` 或 `pending_interactions` 重新确认状态即可。

## 子会话感知等待(多 agent 会话)

主 agent 委派子 agent 之后,**自己的这一轮会先结束,而子 agent 还在跑**。opencode 的 `outcome` 是**按轮**的 —— 它的含义是"这个会话此刻没有生成在跑",而不是"任务完成" —— 所以朴素地等待会提前宣布成功,甚至让子 agent 卡在一个永远无人看到的审批上。

因此本 server 会依据权威的 `parentID` 关系解析出会话的**子 agent 子树**(`GET /api/session?parentID=…`,深度 ≤ 3、节点 ≤ 64,完全不解析对话内容),并且在子树里还有任何活着的节点时拒绝报告 `succeeded`:

- `wait_session` 的 `wait_for_subagents` 默认 `true` —— 只有整棵子树静止(无节点在生成、且子树内无待处理权限/表单)才返回 `succeeded`。
- `chat` 的 `wait_for_subagents` 默认 `false` —— 本轮停止流式输出即返回,但 payload 始终带 `subagents` / `pending_subagents` / `subtree_truncated` / `subtree_verified`,调用方据此可看到仍有工作在跑,再用 `wait_session` 继续跟进。
- **子 agent 的阻塞交互同样会被上报**:子会话卡在审批时返回 `needs_permission`,其 `session_id` 是**真正持有该请求的子会话**(并附 `root_session_id`),答复会自动路由到持有该会话的那台服务器。`auto_permission` 取 `once` / `always` / `reject` 时,答复作用于整棵子树;表单永不自动答复。
- **fail-closed**:若活动表或待处理交互状态无法校验,绝不返回 `succeeded` —— 等待会持续到超时,并明确告知哪些无法校验(`subtree_verified: false`)。缺失这些端点的老服务端按连接探测一次后回退到旧行为,并如实标注,绝不谎称已验证。

## 权限规则与 action 命名

权限请求 `Permission.Request` 的关键字段:

- `action`:要执行的动作,**命名与工具名一致**。实测出现过的 action 包括:
  `shell`、`bash`、`edit`、`write`、`read`、`glob`、`grep`、`webfetch`、`external_directory` 等。
- `resources`:动作作用的对象列表。对命令类工具(`shell`/`bash`)通常是**命令文本**,对文件类工具(`edit`/`write`/`read`/`glob`/`grep`)通常是**路径模式**(如 `*`、`/root/project/**`),`webfetch` 为 URL,`external_directory` 为目录路径。
- `save?`:可被 `always` 记住的资源列表。
- `message?`:可选的说明文本。

因此 `permission_reply` 的 `decision` 语义是:`once` 仅本次放行;`always` 放行并把该 `action` + `resource` 规则保存;`reject` 拒绝。

### 在会话上强制把某类动作设为 ask

opencode 的会话支持在 `session.create` 时通过 `permissions` 传入规则(`effect` 可为 `allow` / `deny` / `ask`),用来覆盖默认行为。例如强制所有 shell 命令都必须询问:

```bash
curl -u opencode:$OPENCODE_PASSWORD \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:49374/api/session \
  -d '{
    "title": "权限演示",
    "permissions": [
      {"action": "shell", "resource": "*", "effect": "ask"},
      {"action": "bash",  "resource": "*", "effect": "ask"}
    ]
  }'
```

等价的 JSON body:

```json
{
  "title": "权限演示",
  "permissions": [
    {"action": "shell", "resource": "*", "effect": "ask"},
    {"action": "bash",  "resource": "*", "effect": "ask"}
  ]
}
```

配置后调用 `chat(..., auto_permission="manual")`,即可稳定复现 `needs_permission` 流程;用 `permission_reply` 逐条答复后再 `wait_session` 继续。

> 说明:`create_session` 工具当前只透传 `title` / `agent` / `model_id`;若需要自定义 `permissions` 规则,可按上面的例子直接调用 opencode HTTP API 创建会话,拿到 `ses_...` 后继续用本 server 的其他工具。

## 已验证流程

以下链路均在**本机 opencode v2.0.12** 上完成过 live 验证:

1. **创建 + 对话**:`create_session` 创建会话,`chat` 发送 prompt,返回 `status: succeeded` 并带 `assistant_text` / `tools_used`。
2. **manual 权限全链路**:`chat(auto_permission="manual")` 返回 `needs_permission` → `permission_reply(decision="once")` → `wait_session` 直至终态。
3. **表单管道**:`chat` / `wait_session` 返回 `needs_form`(含字段详情)→ `form_reply(form_id, answer)` → `wait_session` 直至终态。
4. **默认自动授权**:`chat` 使用默认 `auto_permission="once"`,遇到权限请求自动放行并继续,一次调用即返回 `succeeded`。

5. **连接层**:显式 env 与 MCP 专属拉起两条本地路径;专属 serve 能承载真实对话并随 MCP 退出而退出;失败分类(死端口 = availability、仅返回 HTML 的服务 = compatibility、401 = 凭据问题);重名与未知服务器报错;会话自动路由;断连规则。
6. **真实远端端到端**:`connect_server` 跨网络连接 → 远端 `create_session` → 远端 `chat` → 远端默认手动审批(被阻塞而非自动放行)→ `permission_reply` → `wait_session` → 增量 `get_messages` → `disconnect_server`。
7. **长会话窗口**:在 200+ 条消息的会话上,尾部窗口拉取保证 gate 查找、增量游标与 `last_message_id` 正确。
8. **取消与并发**:`notifications/cancelled` 在 1 秒内停止轮询;`chat` 在途时并发调用 `pending_interactions` 毫秒级返回。
9. **宿主集成**:作为工具提供方挂载进 Hermes agent gateway;由 oh-my-opencode-slim 编排框架托管并驱动嵌套 opencode 会话。
10. **子会话感知等待**:主 agent 后台委派一个卡在 `shell` 审批上的子 agent —— `wait_session`(默认)返回 `needs_permission` 且指向**子会话**的 `session_id`(并带 `root_session_id`),而不是提前的 `succeeded`;`chat`(默认)返回 `succeeded` 但报告 `pending_subagents: 1`。另已验证:三个并行子 agent 各自待审批、子 agent 卡在表单上、`auto_permission="once"` 答复子会话后到达 `succeeded`,以及同样的流程在**远端连接**上不带 `server` 也能正确路由。

## 测试

冒烟测试(仅 MCP 握手 + `tools/list`,不发任何网络请求):

```bash
cd /root/opencode-mcp
python3 -m py_compile server.py test_client.py
python3 test_client.py
```

预期输出 13 个工具名并以退出码 0 结束。

带真实对话测试(需要本机 opencode 正在运行):

```bash
python3 test_client.py --chat "用一句话介绍你自己"
```

## 文件

- `server.py` — MCP stdio server 主文件。
- `test_client.py` — 冒烟测试客户端。
- `README.md` — 本文件。
