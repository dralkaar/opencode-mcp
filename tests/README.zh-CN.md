# opencode-mcp 实机测试

[English](README.md) | **中文**

面向 `server.py` 的实机端到端测试套件。它们通过 stdio 启动真实的 MCP,连接真实的
`opencode` 服务,覆盖连接层、权限/表单交互、子代理子树语义以及派生 serve 的生命周期。

## 前置条件

- Python 3.10 或更高版本。套件只使用标准库:不依赖 pytest,也不依赖任何第三方包。
- 本地/派生场景以及整个生命周期套件需要 `opencode` 位于 `PATH`。如果你希望直接测试
  一个已在运行的服务,改为设置 `OPENCODE_TEST_URL` 即可。
- 运行 `live_subagent_test.py` 需要一套能够委派子会话的代理配置。套件会温和地探测,
  无法委派时报告 SKIP。

## 运行方式

每个套件都可独立运行,输出每个场景一行,外加一行汇总:

```sh
python3 tests/live_core_test.py
python3 tests/live_subagent_test.py
python3 tests/live_lifecycle_test.py
```

仅当至少一个场景 **失败** 时进程退出码才为 `1`。SKIP 不会改变退出码,因此环境无法
支持某个场景时绝不会变成一次失败的构建。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `OPENCODE_TEST_URL` | 让 MCP 连接一个已存在的 opencode 服务(例如 `http://127.0.0.1:4096`)。未设置时,MCP 会自行派生一个私有 `serve`。 |
| `OPENCODE_TEST_PASSWORD` | `OPENCODE_TEST_URL` 对应的密码。未设置时不转发任何密码。 |
| `OPENCODE_TEST_REMOTE_URL` | 供远端场景使用的远端 opencode 实例。未设置时这些场景报告 SKIP。 |
| `OPENCODE_TEST_REMOTE_PASSWORD` | `OPENCODE_TEST_REMOTE_URL` 对应的密码(可选,仅在设置时发送)。 |
| `OPENCODE_TEST_AGENT` | 创建会话时使用的代理。未设置时使用服务自身的默认值。 |
| `OPENCODE_TEST_MODEL` | 可选模型,格式为 `providerID/modelID`。未设置时不固定模型。 |
| `OPENCODE_TEST_TIMEOUT` | 每个场景的等待预算(秒),默认 `60`。 |

当派生服务时,辅助模块会把 `OPENCODE_TEST_URL` / `OPENCODE_TEST_PASSWORD` 映射到
MCP 自身的 `OPENCODE_URL` / `OPENCODE_PASSWORD`,从而走产品的常规环境变量连接路径。

## 覆盖范围

### `live_core_test.py`

- 显式环境变量连接 vs 由 MCP 派生的本地连接(`source=env` 与 `source=spawned`)。
- 失败分类:不可达端口为 `[availability]` 错误;可达但非 opencode 的 HTTP 端点为
  `[compatibility]` 错误;错误的凭据为 `[availability]` 错误。
- 重复连接名与未知连接名错误;本地连接不可断开。
- 跨两个已注册连接的会话自动路由。
- `chat` 正常路径;`wait_session` 终态以及 `get_messages` 增量游标。
- 手动权限循环(`chat` → `needs_permission` → `permission_reply` →
  `wait_session`);表单循环(`pending_interactions` → `form_reply`);自动权限。
- 仍在生成时的 `wait_session` 超时,以及中断后的 `interrupted`。
- `notifications/cancelled` 快速返回并丢弃 chat 响应;chat 进行中时并发调用
  `pending_interactions`。
- `get_context` 与 `compact`。
- 远端端到端循环(连接 → 建会话 → 对话 → 手动权限 → 回复 → 等待 → 增量
  `get_messages` → 断开),仅在设置了远端环境变量时运行。

### `live_subagent_test.py`

所有场景都要求环境具备委派能力;若在有界的探测窗口内没有出现子会话,则每个场景都报告
SKIP。

- 手动模式:`wait_session(wait_for_subagents=true)` 不得过早报告 `succeeded`;它返回
  `needs_permission`,其 `session_id` 是子代理,并且带有 `root_session_id`;回复使用
  子代理 id。
- `chat` 默认(`wait_for_subagents` 未设置/false):返回 `succeeded`,但报告
  `pending_subagents >= 1` 以及非空的 `subagents` 列表。
- 自动模式:`auto_permission="once"` 会替子代理应答其请求,等待最终到达 `succeeded`。
- 表单归属:子代理卡在表单上时返回 `needs_form`,其 `session_id` 与
  `forms[].sessionID` 都是子代理;`form_reply` 使用子代理 id。
- 多个并行子代理:每个待处理请求都以其自身的归属会话 id 上报。
- 远端(仅在设置远端环境变量时):`needs_permission` 指向远端子代理,且 **不带** 显式
  `server` 的 `permission_reply` 仍能到达正确的连接。

### `live_lifecycle_test.py`

在干净环境下,MCP 会派生自己的 `serve`。套件通过 `list_servers` 强制走该路径,定位子
进程,并针对每种关闭路径断言子进程在数秒内消失:stdin EOF、`SIGTERM`、`SIGINT`。
当 `opencode` 不在 `PATH` 时,整个套件报告 SKIP。

## 跳过行为

- `opencode` 不在 `PATH` 且未设置 `OPENCODE_TEST_URL` → 本地场景与生命周期套件报告
  SKIP。
- 未设置 `OPENCODE_TEST_URL` → 显式环境变量场景报告 SKIP;派生场景仍会运行。
- 没有可用的本地密码 → 需要直接访问 HTTP API 的场景(权限循环、表单循环、会话路由、
  重复连接名、远端手动权限)报告 SKIP。
- 没有远端环境 → 远端场景报告 SKIP。
- 子代理环境无法委派 → 子代理场景报告 SKIP,并提示将 `OPENCODE_TEST_AGENT` 设置为
  一个可委派的代理。

当未设置 `OPENCODE_TEST_URL` 时,辅助模块会从 `list_servers` 发现 MCP 派生的本地服务,
并(尽力地)从派生进程的环境中取回其随机密码,使直接访问 API 的场景无需额外配置即可
运行;当无法取回时,这些场景直接报告 SKIP。
