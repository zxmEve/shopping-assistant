# LangGraph 工作流与 Harness 边界

> 状态：已完成关键执行节点与 pending 恢复纵向切片；更新日期：2026-09-10。

## 1. 结论

LangGraph 是当前默认 Application Runtime，但不是新的 Manager Agent。它负责节点顺序、条件路由、interrupt、checkpoint、关键执行节点和 pending 恢复；OpenAI Agents SDK 继续负责 Requirement/Sourcing 两个 Agent 内部的模型与工具循环；Harness 继续强制权限、预算、状态版本、重试、熔断、HITL Token 和写入幂等。`agent_executions/tool_calls` 保存昂贵调用和外部调用的输入指纹、幂等键、状态、尝试及结果。

```text
FastAPI
  -> LangGraph StateGraph        下一步做什么、在哪里暂停、从哪里恢复
      -> Engine/Agent Adapter    执行 Agent 与确定性业务服务
          -> Harness             这次调用是否允许、安全、未超限
              -> Repository      保存业务事实、执行日志与并发版本
```

## 2. 两种状态

### 2.1 ProcurementGraphState

GraphState 是轻量控制面，只保存：

- 服务端生成的 `run_id/tenant_id/user_id`；
- 本次 message/scenario 或确认 token（节点完成后立即清空）；
- `expected_run_version` 与当前 `run_state`；
- assistant message、suggested replies；
- `wait_reason` 和最后完成节点；
- 当前 Agent 节点结果和确认阶段结果。它们是经过 Pydantic 校验的恢复载荷，不是完整业务事实。

它不保存完整报价、制度文件、embedding、长期记忆、采购草稿或完整 Trace。PostgreSQL checkpointer 使用 `thread_id = tenant_id:run_id`，但 thread ID 不是授权边界；每次恢复前仍必须由 Repository 按 tenant/user 重新加载 Run。

### 2.2 ProcurementRun

`ProcurementRun` 仍是业务事实唯一来源，保存结构化需求、状态迁移、消息、报价快照、推荐、证据、额度用量、Action Token 和草稿。Graph checkpoint 的 `expected_run_version` 若不等于当前 Run `state_version`，恢复被拒绝。

因此 checkpoint 不能覆盖新业务事实，也不能作为绕过乐观锁或 HITL 的第二套业务数据库。

### 2.3 Execution Journal

`agent_executions` 记录 Agent 角色、task、业务状态版本、输入指纹、幂等键、状态、尝试次数和结构化结果。`tool_calls` 记录所属 Agent/节点、工具、Provider、风险、预算池、参数、输入指纹、幂等键和结果。PostgreSQL schema 由 Alembic `20260910_0008` 管理；测试/JSON 模式使用相同 Port 的内存实现。

执行日志是结果复用和审计依据，不替代 LangGraph 游标，也不替代 `ProcurementRun`。API 只允许先按 tenant/user 读取 Run 后再列出日志。

## 3. 当前节点

| 节点 | 当前职责 | 主要保护 |
|---|---|---|
| `authorize_start` | 用服务端身份确认 tenant/user 存在 | 客户端不能提交身份字段 |
| `initialize_start` | 创建 Run，解析并先持久化用户输入 | Agent 开始前输入已落库 |
| `route_agent` | 按当前需求完整度选择唯一 Agent | 确定性路由，不使用 Manager LLM |
| `requirement_agent` | 执行需求 Agent，并保存 `agent_executions` | 输入指纹与幂等结果复用 |
| `sourcing_agent` | 执行寻源 Agent，并保存 Agent/Tool 结果 | 模型和目录工具结果可审计 |
| `complete_turn` | 执行未缓存目录读取和确定性评估 | 每个 Provider 结果进入 `tool_calls`，确定性计算不额外拆节点 |
| `route_after_run` | 依据持久化 RunState 选择等待用户、等待确认或结束 | 不使用 LLM 路由 |
| `wait_for_user` | `interrupt`，只接受 `USER_MESSAGE` resume event | run/thread/user/version 恢复检查 |
| `accept_clarification` | 持久化补充输入，然后重新路由 Agent | 防止未保存输入进入昂贵调用 |
| `wait_for_confirmation` | `interrupt`，只接受 `CONFIRM` resume event | 仅技术暂停，不等于业务授权 |
| `prepare_confirmation` | 验 Token、预检状态预算并预留 commit 调用 | 未通过时不进入写前流程 |
| `revalidate_quote` | 重读选中报价，结果保存到 `tool_calls` | Provider 重试/熔断和结果复用 |
| `revalidate_policy` | 重算成本、预算、制度和版本化引用 | 拒绝、变化或证据缺失时回到安全等待态 |
| `create_draft` | 用业务幂等键创建未提交草稿并记执行结果 | BUSINESS_WRITE 权限、数据库唯一键 |

纯 Context 装配、结构校验、成本计算和候选排序执行便宜且无副作用，保留在相邻节点中，不为追求节点数量而拆分。Agent SDK 内部的一轮模型/工具循环仍作为一个 Agent 节点：Agent 完成后的结果可以复用，但在 SDK 内部单个工具执行到一半时仍会重跑整个 Agent 节点。

## 4. 状态路径

```text
START
  -> authorize_start
  -> initialize_start
  -> route_agent
       -> requirement_agent --\
       -> sourcing_agent -----+-> complete_turn -> route_after_run
       -> wait_for_user --interrupt-- USER_MESSAGE
            -> accept_clarification -> route_agent
       -> wait_for_confirmation --interrupt-- CONFIRM
            -> prepare_confirmation
            -> revalidate_quote
            -> revalidate_policy
            -> create_draft
            -> route_after_run
       -> END
```

如果确认时报价变化、制度证据需要人工复核或外部读取安全失败，Engine 回到 `WAITING_CONFIRMATION`，Graph 也会再次进入确认 interrupt；如果进入 `DRAFT_CREATED` 或失败终态，Graph 到达 END。

`POST /api/runs/{run_id}/workflow/resume` 不携带新的业务输入，只允许继续 checkpoint 中唯一的非 interrupt 节点。调用前重新校验 tenant/user 和 `expected_run_version`；停在 `wait_for_user` 或 `wait_for_confirmation` 时必须使用原消息/确认接口，不能用 pending resume 绕过事件类型和 Action Token。

## 5. 为什么保留 Harness

| LangGraph 所有权 | Harness/领域层所有权 |
|---|---|
| 节点编排、条件边 | Tool Registry 和角色授权 |
| checkpoint 与恢复游标 | Agent/tool/token/费用预算 |
| interrupt 技术暂停 | Action Token 与业务 HITL |
| Graph 状态历史 | RunState 合法迁移与 stale result 拒绝 |
| 无副作用节点级重试 | Provider 风险感知重试、熔断、bulkhead |
| 正常流程路径 | 报价/预算/制度复核和草稿幂等 |

Graph 表达正常路径，但错误配置、旧 checkpoint 或直接服务调用仍可能绕开某条边；Harness 必须在动作发生处再次验证不变量。LangGraph 的 recursion/step limit 也不能替代按 Agent、Tool、Provider、token、费用和 wall time 分类的执行预算。

为避免重试放大：LangGraph 只负责无副作用节点的基础设施重试；Provider 429/timeout 和所有写入重试由 Harness/Repository 统一控制。

## 6. Checkpointer 选择

- `PROCURE_WORKFLOW_CHECKPOINT_BACKEND=auto`：PostgreSQL Repository 时选 PostgreSQL，否则选内存；
- `memory`：测试和本地 JSON 演示，进程退出后不保留；
- `postgres`：使用 `AsyncPostgresSaver`，启动时执行幂等 `setup()`；
- `sqlite-test`：只在 kill/restart 自动测试中关闭并重新打开文件 checkpoint，不是生产配置；
- 容器设置 `LANGGRAPH_STRICT_MSGPACK=true`，限制 checkpoint 反序列化类型；
- `PROCURE_WORKFLOW_BACKEND=legacy` 可回滚到原 Engine API 调度，不删除业务数据。

生产部署应把 checkpointer `setup()` 移到独立部署步骤，并评估 checkpoint 加密与保留期；当前 GraphState 已尽量只保存控制字段，但 interrupt resume 输入仍可能进入 checkpoint 历史。

## 7. 已验证证据

- `wait_for_user` 与 `wait_for_confirmation` 都产生真实 interrupt；
- 完整 start -> continue -> confirm 到 `DRAFT_CREATED`；
- 使用同一个 InMemorySaver 重建 Graph 对象后继续；
- 关闭第一个 PostgreSQL saver/Engine，建立新实例后从相同 thread 恢复；
- 在 Sourcing Agent 结果已保存但 Graph checkpoint 尚未落下时终止 Worker；关闭并重新打开 SQLite checkpoint、Run 和 execution 数据库后复用同一 Agent execution；
- 在制度复核 checkpoint 后注入 Worker kill，重建 Runtime 后只创建一个草稿；
- PostgreSQL Alembic 已升级到 `20260910_0008`，真实闭环写入 1 条 Sourcing Agent execution 和 6 条工具记录；
- 跨租户、错误阶段和业务 state_version 变化均拒绝恢复；
- legacy backend 仍可回滚；
- 126 个 pytest 与 51 个确定性评测通过。

## 8. 下一切片

1. 增加 worker lease、heartbeat、cancel、过期回收和自动 pending 扫描；
2. 把 Agent SDK 内部工具开始/完成事件直接写入 `tool_calls`，覆盖模型进程在工具中途退出的恢复；
3. 增加外部写结果未知时的 `RECONCILING` 状态与查询接口；
4. 完成 checkpoint history/cassette 对比；
5. 在新 Runtime 上重跑 DeepSeek 12-case × 3 稳定性门禁。

## 9. 官方参考

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Checkpoint Libraries](https://reference.langchain.com/python/langgraph/checkpoints)
- [PostgreSQL Store/Saver 安全与 setup](https://reference.langchain.com/python/langgraph/store/postgres)
