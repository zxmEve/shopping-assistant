# GitHub 调研与技术决策

调研日期：2026-09-01。项目已从消费购物助手调整为企业采购决策与审批 Agent。

## 1. 参考项目与借鉴边界

| 项目 | 借鉴内容 | 本项目的取舍 |
|---|---|---|
| [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | Agents、agents-as-tools、handoff、guardrails、sessions、HITL 与 tracing 等 Agent 原语 | 作为默认 Agent runtime；领域事实、副作用和流程约束仍由项目 Harness 控制 |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 图状态、checkpoint、durable execution、interrupt、短期与长期状态分离 | 作为可替换 runtime 的对照方案，不与 Agents SDK 同时进入主链路，避免双重状态管理 |
| [NVIDIA-AI-Blueprints/retail-shopping-assistant](https://github.com/NVIDIA-AI-Blueprints/retail-shopping-assistant) | 购物领域多 Agent、检索、记忆、guardrail、流式 UI 和图像搜索 | 保留目录检索与商品比较思想，增加企业预算、供应商、权限和审批，不采用重 GPU 部署栈 |
| [AmineElAtrache/Smart-Shopper](https://github.com/AmineElAtrache/Smart-Shopper) | 多站点并行检索、实体抽取、去重排序、价格监控、治理和部分失败 | 借鉴事件驱动与 provider 隔离；MVP 采用模块化单体 + Worker，避免过早微服务化 |
| [VIVPM/ecommerce-agent](https://github.com/VIVPM/ecommerce-agent) | FastAPI/React、SSE、目录查询、缓存、只读 SQL、安全边界和大规模场景评测 | 借鉴 Python 流式服务与评测思路，并补充跨来源同款归一、预算与审批闭环 |
| [amazon-science/esci-data](https://github.com/amazon-science/esci-data) | 百万级 query-product 相关性标注，含 Exact/Substitute/Complement/Irrelevant | 用于检索、重排和 hard-negative 测试；不把它当作价格、库存或企业制度数据 |
| [ByteDance-BandAI/ShoppingComp](https://github.com/ByteDance-BandAI/ShoppingComp) | 中英文复杂购物任务、报告生成和 safety/trap cases | 用于中文需求、约束满足、报告和安全评测；企业预算/审批场景由本项目补齐 |
| [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | policy、tools、tasks、user simulator 和可变数据库组成的交互评测环境 | 借鉴环境协议和用户模拟器；同时增加过程型 grader，避免只看最终状态而漏判违规轨迹 |
| [sequrity-ai/agentdojo](https://github.com/sequrity-ai/agentdojo) | 工具型 Agent 的间接 prompt injection 攻防评测 | 改造成商品描述、供应商备注和制度文档中的不可信指令测试 |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | 动态网页自动化、结构化抽取和可重复浏览脚本 | 浏览器成本、验证码、反爬和 DOM 漂移风险高，只做合规末级兜底，不作为主要报价来源 |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | 跨会话长期记忆、记忆抽取、搜索和用户隔离 | 初版自建结构化记忆 + pgvector，便于展示 consent、TTL、provenance 和删除边界；保留 Mem0 adapter |
| [nderman/agent-harness](https://github.com/nderman/agent-harness) | record/replay、JSONL trace、轨迹断言、guardrail、基线 diff 和离线 CI | 将这些能力扩展到多 Agent、并行工具、预算制度、审批副作用和异步恢复 |
| [langfuse/langfuse](https://github.com/langfuse/langfuse) | OpenTelemetry traces、会话、prompt 版本、数据集、实验和线上评测 | 仅作为旁路观测与实验平台；运行状态、事实和审批不依赖 Langfuse 可用性 |
| [confident-ai/deepeval](https://github.com/confident-ai/deepeval) | 最终答案、trajectory、工具调用和 handoff 评测 | 先做确定性断言，再用 LLM judge 评价解释质量，并以人工集校准 judge |
| [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy) | 基于数据和指标自动优化 prompt/few-shot | 只在离线 evolution pipeline 生成候选，禁止请求链路在线自改 |
| [microsoft/agent-lightning](https://github.com/microsoft/agent-lightning) | 从真实 Harness 收集 rollout/trajectory，以 reward 优化 Agent | 作为高级演进路线；MVP 不承担 RL 训练和 GPU 集群复杂度 |

## 2. 官方能力基线

官方 OpenAI 文档显示，Responses API 支持自定义函数工具、MCP、并行工具调用、结构化输出、会话状态、后台执行、工具调用上限和长会话 compaction；这些能力适合做模型层的生产基线。OpenAI 官方也建议把业务副作用和内部系统通过自定义工具暴露，并使用 Agent SDK/编排模式管理 tracing、handoff 和状态。

- [Responses API：Create a model response](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [Responses API：Compact a response](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)
- [OpenAI Model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)
- [OpenAI Evals API](https://developers.openai.com/api/reference/java/resources/evals/methods/create)

本项目通过 `ModelGateway` 隔离模型提供方。默认实现使用 OpenAI Agents SDK + Responses API；Pydantic 定义工具与结果 schema，业务代码不直接依赖某个模型 ID。

## 3. 关键技术决策

### 3.1 Python 3.12 主栈

不再受既有技术栈约束，核心后端统一采用 Python 3.12。原因不是“Python 更适合所有企业系统”，而是这个项目的核心竞争力是 Agent 编排、评测、数据合成、浏览器自动化和快速实验；这些能力在 Python 生态中集成成本最低，也与多数公开 benchmark 的运行环境一致。

API 使用 FastAPI/Pydantic，在线 Agent 使用 OpenAI Agents SDK；目标架构中，耗时且需要恢复的流程交给 Temporal Python SDK，但第一阶段先用最小 Harness 跑通核心闭环，Temporal 延后接入。生产事实仍通过确定性领域服务和 Port/Adapter 暴露，不能让 SDK session 代替业务状态。前端继续使用 TypeScript，不强行全栈单语言。

### 3.2 企业采购，而不是泛消费推荐

推荐结果不仅取决于价格和偏好，还必须联合考虑：

- 成本中心预算和金额口径；
- 企业采购制度、目录内采购要求和金额阈值；
- 供应商准入、发票、税率、账期与交付地区；
- 申请人权限和数据范围；
- 动态审批路径；
- 报价有效期和审计证据。

这让项目能够自然承接统一审批、预算费用、权限治理、幂等和异步恢复经验。

### 3.3 Manager + specialists-as-tools

由一个始终面向用户的 Procurement Orchestrator 管理对话和状态，需求澄清、商品归一、制度解释、排序与核验由 specialist agent 提供结构化结果。这样能保持权限边界、统一事实和稳定 UX。

平台查询是确定性 provider tool，不把每个平台包装成 LLM Agent。handoff 作为可选知识点保留，但核心链路使用 manager pattern，避免责任转移后上下文和审批权限混乱。

### 3.4 显式 Workflow 包住 Agent Loop

采购流程有明确状态和副作用边界，不能完全交给开放式 ReAct 循环。外层使用显式状态机约束阶段：

```text
需求收集 -> 信息补全 -> 来源检索 -> 同款归一 -> 预算/制度校验
-> 多目标排序 -> 证据核验 -> 等待确认 -> 服务端复核 -> 草稿生成
```

每个阶段内部允许 Agent 使用工具和有限循环；第一阶段的迁移、终止、审批和写入由 Procure Harness 决定，第二阶段再把长任务、定时器和故障恢复迁入 Temporal workflow。

### 3.5 企业系统使用 Port/Adapter

定义以下端口：`IdentityPort`、`BudgetPort`、`ProcurementPolicyPort`、`VendorMasterPort`、`CatalogProviderPort`、`ApprovalPort`、`NotificationPort`。

- Demo adapter：进程内/数据库 fixture，可故障注入，可确定性回放；
- Live adapter：仅连接获得授权的 API、MCP、商家 feed 或公开结构化数据；
- Production adapter：使用最小权限凭据、网络 allowlist、审计和限流。

### 3.6 事实和副作用必须服务端复核

模型可以推荐商品或调用工具，但不能：

- 自己创造价格、预算、供应商或制度事实；
- 用自然语言计算最终采购金额；
- 绕过授权调用草稿/审批写工具；
- 直接下单、支付或批准采购；
- 把网页文字当作系统指令。

所有金额使用 Python `Decimal`、ISO 4217 币种和显式舍入规则；所有写操作带业务幂等键、乐观锁和审计事件。

## 4. 技术栈

| 层 | 选择 | 原因 |
|---|---|---|
| Web | React + TypeScript + Vite | 与现有项目一致；实现聊天、商品卡、比较表和 trace/eval 控制台 |
| API | Python 3.12 + FastAPI + Pydantic v2 + SSE | 原生 async、schema-first、快速构建工具和模拟服务 |
| Agent | OpenAI Agents SDK + Responses API，经 `ModelGateway` 隔离 | 直接使用 agents-as-tools、guardrails、sessions、HITL 与 tracing 原语 |
| Workflow/Harness | 第一阶段自建最小 Harness；第二阶段接入 Temporal Python SDK | 先验证工具权限、预算、record/replay 与领域状态；再用 Temporal 补 durable execution、timer、retry 和恢复 |
| 数据 | PostgreSQL + pgvector + Alembic/SQLAlchemy 2 | 领域事实、会话、记忆、报价快照、向量召回与审计 |
| 缓存/锁 | Redis | TTL cache、rate limit、短锁；不能替代数据库事实与状态约束 |
| 异步 | Transactional Outbox + Kafka-compatible bus；本地可用 Redpanda | 价格监控、通知、评测、审批同步和可恢复消费 |
| 检索 | PostgreSQL FTS + pgvector + RRF | 采购制度、商品别名和历史案例的 hybrid retrieval |
| 浏览器兜底 | 独立 Playwright worker，可选 browser-use adapter | 与主 API 隔离，配置域名、资源、时限和敏感数据边界 |
| 观测 | OpenTelemetry + Langfuse + Prometheus/Grafana | Agent trace、工具、成本、延迟和业务指标统一关联 |
| 测试 | pytest、pytest-asyncio、Hypothesis、Testcontainers、respx + DeepEval/自建 graders | 覆盖领域、端口契约、性质、故障、轨迹和生成式评测 |
| 交付 | Docker Compose + GitHub Actions | 一键启动 fixture demo，一键运行离线 eval 和回归门禁 |

## 5. 数据源优先级

1. 企业内部协议目录、供应商主数据和采购系统 API；
2. 官方/联盟 API、授权 MCP 或商家 feed；
3. 可公开访问的 JSON-LD 等结构化数据；
4. 搜索 API/网页搜索，Top-K 必须二次核验；
5. 隔离浏览器 worker，遇到登录限制或 CAPTCHA 即停止；
6. fixture providers，保证 CI 和面试演示稳定。

`实时`定义为“请求时刷新，或使用仍在声明 TTL 内的报价快照”。任何报价必须带 `source_url`、`fetched_at`、`expires_at`、规格和置信度；过期数据只能标为 stale，不能标为实时最低价。

## 6. 长期记忆决策

长期记忆分为四类：

- 个人偏好：重量、品牌、系统等非敏感采购偏好；
- 团队标准：研发笔记本基线、常用交付地等共享但受权限控制的信息；
- 历史案例：采购任务的压缩结论、反馈和异常，不保存无必要的完整对话；
- 制度知识：来自版本化知识库，不与个人记忆混存。

每条记忆包含 `tenant_id`、`subject_id`、`scope`、`source`、`confidence`、`consent_status`、`created_at`、`expires_at` 和版本。模型只生成 `MemoryCandidate`，写入策略由服务端决定；用户可查看、修改、删除和禁止召回。

## 7. 自进化决策

自进化采用离线受控闭环：

```text
线上失败/低分 trace -> 脱敏与聚类 -> 根因分类
-> 生成 prompt/tool/routing/ranking 候选
-> train/dev/holdout 评测 -> 安全与成本门禁
-> 人工批准 -> 小流量 canary -> 晋升或自动回滚
```

允许自动生成候选，但不允许 Agent 直接修改生产 prompt、代码、制度、预算、审批策略或用户记忆。任何系统规则和安全策略的变更都必须经过代码评审。

## 8. 明确不做

- MVP 不真实下单、支付或自动提交生产审批。
- 不宣称拥有未取得授权的平台 API，也不绕过平台访问控制。
- 不把模型隐藏推理持久化；只记录可审计的输入摘要、工具、证据、决策标签和输出。
- 不以 Agent 数量证明先进性；金额、状态、规则和幂等优先用确定性代码。
- 不宣称本人有生产采购系统经历；该项目是对既有企业系统能力的迁移验证。
