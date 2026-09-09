# 里程碑与交付计划

更新时间：2026-09-09。逐条实时状态以 `08-requirements-traceability.md` 为准。P0 稳定化、DeepSeek 真实多 Agent、制度 RAG、Context、可靠性与面试交付的详细实施方案见 [`11-next-step-plan-and-technical-design.md`](11-next-step-plan-and-technical-design.md)。

## 1. 交付规则

每个纵向切片必须同时包含：需求 ID、业务不变量、ADR、实现、失败测试、场景评测、文档状态更新和学习导读。没有验收证据不得标记为 `VERIFIED`。

开发循环：

```text
需求场景 -> 业务不变量 -> 架构决策 -> 失败模式
-> 最小实现 -> 自动化评测 -> 设计复盘 -> 状态更新
```

## 2. 里程碑

| 里程碑 | 交付目标 | 当前状态 | 退出条件 |
|---|---|---|---|
| M0 项目基线 | 需求追踪、ADR、诚实 README、场景映射、CI | 已完成 | 状态和证据可从需求追到测试 |
| M1 完整 P0 | IAM、主动追问、商品归一、HITL、最小生产 Harness | 进行中 | 38 条 P0 全部 VERIFIED，五类 Demo 可重复 |
| M2 真正 Agent 工具链 | 强类型 function tools、多轮 Orchestrator、Harness 授权和 Agent 轨迹评测 | 已完成最小纵向切片 | Agent 实际选择并调用目录、预算、成本和制度工具，工具选择/参数/权限有稳定基线 |
| M2.1 DeepSeek 真实多 Agent | 显式服务端调度独立 Agent、阶段预算、完整 Live 确认闭环 | 进行中：S2 调度内核与 fixture 验证已完成 | DeepSeek 总控与专家发生独立模型执行，正常 Live case 可安全到达 `DRAFT_CREATED` |
| M3 数据库基础 | PostgreSQL、SQLAlchemy/Alembic、tenant-scoped repository、状态版本和业务唯一约束 | 已完成最小纵向切片 | JSON Repository 可由数据库实现替换，跨租户与并发写入测试通过 |
| M4 长期记忆与 RAG | Profile/Team/Episodic、结构化 Registry/Consent、Agent Context、pgvector + FTS | 进行中：记忆闭环与制度 FTS 首片已完成，vector/RRF 待做 | 记忆准确、安全、可删除且不越租户，制度回答带版本化证据 |
| M5 工程可靠性增强 | checkpoint/resume/cancel、cassette replay、跨进程熔断，必要时 Temporal | 未开始 | 重启和外部依赖故障可恢复，离线轨迹可重复回放 |
| M6 完整评测与自进化 | 60/180 场景、holdout、shadow/canary/rollback | 未开始 | 候选版本不能绕过 veto 和人工批准 |
| M7 面试交付 | 六套 Demo、架构材料、失败复盘、讲解稿 | 未开始 | 一键演示，所有数字可由报告复现 |

## 3. M1 执行队列

| 顺序 | 切片 | 状态 |
|---:|---|---|
| 1 | 需求追踪、文档边界和 CI | 已完成 |
| 2 | 报价快照、旧 Token、写前重校验和单进程并发幂等 | 已完成 |
| 3 | Fixture Session、Tool Registry 和角色 allowlist | 已完成 |
| 4 | 信息价值追问、公司标准授权和三轮停止 | 已完成 |
| 5 | Product/Variant/Offer 真正归一、冲突字段和置信度 | 已完成 |
| 6 | 状态迁移表、budgets、timeout/retry/circuit | 已完成最小纵向切片 |
| 7 | checkpoint/resume/cancel 和 cassette replay | 延后至 M5 |
| 8 | 逐事实 evidence、trade-off UI 和五套 P0 Demo | Agent 闭环后补齐 |

## 4. 当前优先顺序

```text
S2 Provider 无关的双 Agent Runtime（已完成）
-> S3 DeepSeek/OpenAI 双 Agent smoke（12/12，×3 待跑）
-> M4.1/M4.2 记忆 Context 与结构化 Registry（已完成）
-> M4.3 记忆候选确认闭环（已完成）
-> M4.4 制度 FTS 与引用回传（已完成最小纵向切片）
-> M4.5 pgvector + RRF 混合检索与检索评测（下一步）
-> M5 checkpoint/replay 等工程可靠性增强
-> M6 完整评测与受控自进化
-> M7 面试交付
```

这样排序的原因：现有数据库基线已经提供租户条件、版本和唯一约束，当前先消除 Live 提交链路的确定性阻塞，再证明 DeepSeek 的独立 Agent 调度确实带来可测量收益。制度 RAG 和记忆接入依赖稳定的 Agent 契约与数据边界；恢复、回放和分布式调度在主链路、RAG 与 Context 结构稳定后实现，可以减少返工。

## 5. M2 执行队列

| 顺序 | Agent 切片 | 状态 |
|---:|---|---|
| 1 | 两个只读 Agent 工具、角色隔离、完整多轮消息和结果复用 | 已完成 |
| 2 | Sourcing Agent 输出候选 Offer ID 和目录 evidence refs，服务端验证后参与推荐 | 已完成 |
| 3 | Agent 工具选择、参数、遗漏、重复调用和停止条件的 trajectory grader | 已完成确定性 grader |
| 4 | 真实模型 token、延迟、成本基线与 deterministic/live 报告隔离 | 已完成：DeepSeek 3/3，p95 10.859 秒，token 与版本化费用估算已记录 |
| 5 | 完成 Agent 闭环验收并冻结数据库所需 Run/Message/Trace 数据契约 | 已完成最小纵向切片 |
| 6 | 修复 Live Agent 确认阶段工具预算不足和 `REVALIDATING` 卡死；Live runner 覆盖草稿创建 | 已完成代码与 fake live-shape 回归；DeepSeek 待重跑 |
| 7 | Provider 无关的双 Agent Runtime、静态权限、Agent 预算与 fixture 测试 | 已完成 |
| 8 | DeepSeek/OpenAI 两 Agent Adapter 与真实模型 12-case × 3 门禁 | Adapter 已完成，真实门禁待重跑 |

草稿创建继续走现有服务端 HITL 和重校验流程，不为了展示 Agent 自主性把业务写权限交给模型。

## 6. M3 执行队列

| 顺序 | 数据库切片 | 状态 |
|---:|---|---|
| 1 | SQLAlchemy Repository、Alembic migration、PostgreSQL Compose | 已完成 |
| 2 | 完整 Run JSONB 快照与 Message/Trace 查询投影原子写入 | 已完成 |
| 3 | tenant/user 查询条件、旧版本条件更新和草稿唯一约束 | 已完成并在真实 PostgreSQL 17 验证 |
| 4 | API 通过配置切换 JSON/PostgreSQL Repository | 已完成 |
| 5 | PostgreSQL RLS、生产连接池健康指标 | 延后到生产强化，不阻塞 M4 |

为什么保留完整 Run 快照又拆出 Message/Trace：快照让恢复一条采购任务简单可靠；单独的消息和轨迹表让后续长期记忆、RAG 和审计无需反复扫描整块 JSON。当前不把每个候选和报价全部拆表，避免在 Agent 数据结构仍会变化时制造大量迁移成本。

## 7. M4 执行队列

| 顺序 | 记忆与 RAG 切片 | 状态 |
|---:|---|---|
| 1 | Profile/Team/Episodic/Semantic 数据模型与 migration | 已完成 |
| 2 | 显式同意、个人/团队读取边界、创建者写权限 | 已完成 |
| 3 | 版本化修改、软删除和全生命周期审计 API | 已完成并在真实 PostgreSQL 17 验证 |
| 4 | ContextPolicy、双 Agent 投影、冲突、token 裁剪与 Trace | 已完成 |
| 5 | 受控 Key/Category/Tag、结构化值、有效期、TEAM 写权限和 Consent | 已完成并迁移真实 PostgreSQL 17 |
| 6 | 记忆候选提取与用户确认，不自动写长期记忆 | 已完成并在真实 PostgreSQL 17 验证 |
| 7 | 制度文档分块、版本/生效期、FTS 检索和 Policy 引用回传 | 已完成最小纵向切片 |
| 8 | pgvector + FTS + RRF 混合检索与证据引用 | 待开始 |

## 8. 版本与发布门禁

- 金额、权限、租户、Policy、旧 Token、重复草稿和 Prompt Injection 为 veto；
- Fixture 和 live-model 指标分开报告；
- 每个指标名称必须与 grader 真正检查的行为一致；
- P0 完成前不把长期记忆、自进化和 live provider 写成当前能力；
- 主线代码已推送到远端；后续发布前仍需整理增量提交、打 Tag，并从干净目录复现测试和评测。
