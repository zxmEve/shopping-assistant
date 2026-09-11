# ADR-0016：LangGraph 编排与领域 Harness 分层

- 状态：已采用
- 日期：2026-09-10

## 背景

项目原来的 Application Runtime 能确定性选择 Requirement/Sourcing Agent，但每个 API 请求会一次性执行到等待态，尚无标准化的跨进程 checkpoint、interrupt 和状态历史。下一阶段若继续自研相同基础设施，会与成熟工作流框架重复，同时增加恢复语义和可观测性的维护成本。

## 决策

1. 使用 LangGraph `StateGraph` 作为默认 Application Runtime；它是确定性管理者，不增加 Manager LLM。
2. Requirement/Sourcing 继续使用现有 OpenAI Agents SDK Adapter。LangGraph 管外层生命周期，SDK 管单个 Agent 内部模型/工具循环，避免两套组件同时拥有业务状态。
3. `ProcurementGraphState` 只保存控制字段和业务引用；`ProcurementRun` 继续是完整业务事实的唯一来源。
4. `WAITING_USER` 和 `WAITING_CONFIRMATION` 使用真实 `interrupt`；恢复前同时校验 tenant/user、等待节点与 `state_version`。
5. PostgreSQL 部署使用 `AsyncPostgresSaver`，JSON/测试模式使用 `InMemorySaver`。保留 `PROCURE_WORKFLOW_BACKEND=legacy` 回滚开关。
6. Harness 不被 LangGraph 替代：Tool Registry、风险预算、超时/重试/熔断、合法状态迁移、Action Token、stale result 和写入幂等继续在动作点强制执行。
7. Graph 只对无副作用节点应用通用重试；Provider 和写入重试仍由 Harness 控制，避免嵌套重试放大。
8. Requirement/Sourcing、报价复核、制度复核和草稿写入形成独立关键节点；纯确定性轻量计算不额外拆分。
9. `agent_executions/tool_calls` 保存幂等执行结果；`resume_pending` 只继续非 interrupt 节点，不能替代用户消息或确认事件。

## 原因

LangGraph 的 checkpoint 和 interrupt 解决“流程在哪里、如何恢复”，但不理解采购角色、预算、制度、报价指纹或幂等键。Graph edge 只能描述预期路径，不能成为业务安全边界。保留 Harness 可以使 API、Graph、Agent Tool 或未来任务 Worker 共用同一组不可绕过的不变量。

## 代价

- 同时存在 GraphState 与 ProcurementRun，需要明确前者是控制面、后者是业务真相；
- GraphState、业务 Run 与执行日志三者需要通过 run/version/fingerprint 保持一致；
- Agent SDK 内部工具循环中途退出时仍可能整体重跑 Agent 节点；
- PostgreSQL checkpointer 创建自己的表，部署需要独立 setup、保留期和安全配置；
- 引入 LangGraph/LangChain Core 相关依赖，增加升级与兼容性测试成本。

## 验收

- 两个等待态均产生 interrupt，使用同一 thread 恢复；
- 重建 Graph/Engine/PostgreSQL saver 后继续到下一等待态；
- Agent 完成后 Worker 退出可通过 `resume_pending` 继续，且 Agent execution 不重复；
- 制度复核后 Worker 退出，恢复后只创建一个业务草稿；
- PostgreSQL 中可查询 tenant-scoped Agent/Tool 执行记录；
- 跨租户、错误阶段和陈旧业务版本恢复被拒绝；
- 确认仍经过 Harness Action Token、重校验和幂等草稿；
- legacy 开关可运行原链路；
- deterministic 与 live 评测继续分离。

## 当前限制

当前已达到人工等待和关键节点恢复，不代表 Provider 子步骤级 exactly-once。尚需 lease/heartbeat、自动恢复扫描、cancel，以及 Agent SDK 内部工具级直接 journaling；真实外部写还需要 `RECONCILING` 获得 effectively-once 语义。
