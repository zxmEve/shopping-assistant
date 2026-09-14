# ProcurePilot：企业采购决策与审批 Agent

> 当前状态：LangGraph 已成为默认工作流 Runtime，负责多轮路由、interrupt 和 checkpoint；Requirement/Sourcing 两个模型角色、现有 Harness 与确定性领域服务继续负责各自边界。Agent/Tool 执行日志、细粒度写前节点和 `resume_pending` 已接入。当前 126 个后端测试、51 个确定性评测通过；DeepSeek 双 Agent 12/12 smoke 已通过，×3 稳定性矩阵待运行。

ProcurePilot 是一个面向企业员工、采购专员和审批人的 AI 采购助手。它将自然语言采购需求转化为结构化申请，主动补齐关键约束，并行查询多个合规来源，完成同款归一、到手成本计算、预算与制度校验、供应商风险检查，最终在人工确认后生成采购申请草稿。

项目不是让模型“代替公司下单”，而是验证如何将不确定的模型能力嵌入确定的企业流程：模型负责需求理解、问题选择和多目标权衡；Python 领域服务与 Harness 负责价格事实、预算、权限、金额、状态、幂等、审计和副作用。

## 文档导航

- [GitHub 调研与技术决策](docs/01-research-and-decisions.md)
- [技术方案与项目架构](docs/02-architecture.md)
- [完整需求与验收标准](docs/03-product-requirements.md)
- [评测、自进化与实施路线](docs/04-evaluation-and-roadmap.md)
- [公开评测集与模拟环境方案](docs/05-simulation-and-datasets.md)
- [第一阶段范围与完成指标](docs/06-phase-one-scope.md)
- [第一阶段实现与测试报告](docs/07-phase-one-implementation-report.md)
- [P0 需求追踪矩阵](docs/08-requirements-traceability.md)
- [架构决策记录](docs/adr/)
- [设计学习导读](docs/09-learning-guide.md)
- [里程碑与交付计划](docs/10-delivery-plan.md)
- [下一步实施规划与技术方案](docs/11-next-step-plan-and-technical-design.md)
- [记忆、上下文与知识检索模块技术设计](docs/12-memory-context-technical-design.md)
- [LangGraph 工作流与 Harness 边界](docs/13-langgraph-workflow-technical-design.md)
- [多级费用预算与统一工具网关](docs/14-multilevel-budget-and-tool-gateway.md)
- [Requirement Agent 与版本化 RequirementSpec 技术设计](docs/15-requirement-agent-boundary-refactor.md)
- [Sourcing Agent 合规候选排序链路技术设计](docs/16-sourcing-decision-pipeline-refactor.md)
- [单 Agent、双 Agent 与确定性基线消融评测技术设计](docs/17-agent-topology-ablation-evaluation.md)

## 快速开始

```bash
cd shopping-assistant
make setup
make test
make eval
make dev
```

打开 `http://127.0.0.1:5173`。`make dev` 会同时启动 FastAPI 与 Vite；也可以使用 `docker compose up --build`。

默认 `PROCURE_AGENT_MODE=deterministic`，用于离线演示和 CI。

工作流默认使用 `PROCURE_WORKFLOW_BACKEND=langgraph`。当 Repository 为 JSON 时，`PROCURE_WORKFLOW_CHECKPOINT_BACKEND=auto` 自动选择内存 checkpoint；当 Repository 为 PostgreSQL 时自动选择 `AsyncPostgresSaver`。生产/Compose 推荐显式设置为 `postgres`，并保持 `LANGGRAPH_STRICT_MSGPACK=true`。如果需要快速回滚，可临时设置 `PROCURE_WORKFLOW_BACKEND=legacy`；此模式不提供 LangGraph interrupt/checkpoint。运行中的节点可通过 `GET /api/runs/{run_id}/workflow` 查看；非 interrupt 节点因 Worker 退出而遗留时，可由授权调用 `POST /api/runs/{run_id}/workflow/resume`；执行日志通过 `GET /api/runs/{run_id}/executions` 查询。

双 Agent fixture 拓扑可用下面的配置离线运行：

```bash
PROCURE_AGENT_MODE=deterministic \
PROCURE_AGENT_TOPOLOGY=two_agent \
make dev
```

它会执行与真实模型相同的 Runtime、权限和工具链，但 Agent 决策来自 deterministic fixture adapter，不产生模型费用。

启用真实 OpenAI Agents SDK 及其兼容 Provider：

```bash
# OpenAI：设置 PROCURE_AGENT_MODE=openai 和 PROCURE_OPENAI_API_KEY
# 双 Agent：设置 PROCURE_AGENT_TOPOLOGY=two_agent
# DeepSeek：再设置 PROCURE_MODEL_PROVIDER=deepseek、
# PROCURE_OPENAI_BASE_URL=https://api.deepseek.com 和对应模型名；可分别配置
# PROCURE_REQUIREMENT_MODEL、PROCURE_SOURCING_MODEL
# 排序结果相对确定性基线的最大绝对效用损失：PROCURE_RANKING_MAX_UTILITY_REGRET=0.05
make dev
```

真实模型评测与 deterministic 报告分开保存。配置 Key 后，`make live-eval` 运行当前双 Agent 拓扑的 3 次完整闭环；`make live-eval-s3` 运行 12-case × 3-run smoke。报告写入 `backend/evals/reports/live/`；没有 Key 时命令会明确停止，不会生成伪造的 live 基线。真实评测会产生费用，应在确认预算后手动执行。

制度文件默认使用离线可复现的哈希向量。启用 OpenAI embedding 和扫描 PDF OCR 后，可先预览再分批回填已发布条款：

```bash
export PROCURE_REPOSITORY_BACKEND=postgres
export PROCURE_DATABASE_URL='postgresql+psycopg://...'
export PROCURE_KNOWLEDGE_EMBEDDING_PROVIDER=openai
export PROCURE_KNOWLEDGE_EMBEDDING_API_KEY='...'
export PROCURE_KNOWLEDGE_OCR_PROVIDER=tesseract

cd backend
uv run procure-knowledge-backfill --tenant-id demo_cn --dry-run
uv run procure-knowledge-backfill --tenant-id demo_cn --batch-size 64
```

embedding 使用独立密钥，不复用 Agent 模型密钥；`--max-batches` 可限制单次作业并从剩余旧模型 chunk 继续。文件上传接口是 `POST /internal/knowledge/files`，只允许知识治理角色。

当前双 Agent + LangGraph 代码基线为 139 项后端测试通过（包含 1 项真实 Redis 集成测试），确定性评测为 51/51。制度检索集包含 5 条开发标签和 7 条独立留出标签；这仍是小型 fixture 门禁。双 Agent DeepSeek 12-case × 1 smoke 已 12/12 通过；此前五 Agent 和 direct-tools 报告只保留为历史记录，不能作为新拓扑发布证据。发布前仍需完成 12-case × 3 稳定性矩阵。

Fixture API 使用服务端映射的演示会话：`Authorization: Bearer fixture-session-cn-employee-001`。它用于证明客户端不能直接指定 tenant、用户或成本中心，不代表生产 SSO。

## 与个人简历的连接

本项目是现有企业系统经历的自然延伸，而不是突然切换到陌生的消费电商领域：

| 已有经历 | 在本项目中的延伸 |
|---|---|
| 统一审批平台 | 采购申请草稿、动态审批路由、移动端待办 |
| 预算、费用与付款链路 | 预算查询、含税成本、预算占用模拟、申请金额校验 |
| 国内/印尼双 OA 异步协同 | 多来源并行检索、部分成功、重试、任务恢复 |
| 权限治理与灰度迁移 | 工具授权、数据范围、HITL、策略版本与灰度发布 |
| 业务幂等与最终一致性 | 草稿防重、Outbox、审批状态同步和补偿任务 |
| 企业级 Agent 工程 | Tool Calling、显式状态机、服务端事实复核和故障注入 |

项目只说明这些工程经验能够迁移到企业采购场景，不宣称本人曾负责生产采购系统。

## 最终目标 Demo

以下是项目的完整目标，不代表每项均已实现。当前状态和验收证据以 [P0 需求追踪矩阵](docs/08-requirements-traceability.md) 为准。

员工输入：

> 给印尼研发团队采购 20 台开发笔记本，单台不超过 8000 元，两周内到货，需要能稳定运行 Docker。

完整演示应包含：

1. Agent 主动追问预算是否含税、最低内存、交付城市、发票与供应商限制等高影响信息。
2. 同时查询内部协议目录、授权商城和外部公开报价，持续流式返回搜索进度。
3. 区分相同型号的内存、硬盘、系统、保修、翻新状态和套装，避免伪同款比价。
4. 统一币种并计算商品价、税费、运费、优惠条件和可信到手总价。
5. 查询成本中心预算、公司采购制度、供应商准入和申请人权限。
6. 输出“最低成本、最快交付、综合最优”候选及证据、抓取时间和风险。
7. 用户确认后由领域服务重新校验报价、预算和策略，只生成不可提交的采购申请草稿。
8. 重复确认由业务幂等键拦截；外部系统失败进入可查询、可重试、可恢复的任务链路。
9. 后台展示 Agent trace、工具调用、版本、成本、延迟、评测分数和失败归因。
10. 演示一个坏 prompt/坏工具版本被评测门禁阻止，以及候选版本灰度失败后的自动回滚。

## 当前已验证的亮点

- 主动澄清：从中文需求抽取关键字段，支持多轮增量补齐和单轮最多三个问题。
- Fixture 比价：三个逻辑 provider 返回不一致的原始表达，经确定性管线归一为 Product、Variant、Offer；关键冲突由服务端 veto，低置信度报价不进入排名，金额使用 Decimal。
- 企业决策：预算、采购制度、供应商状态和审批路径由确定性领域服务判断。
- 最小 Harness：合法状态迁移与连续版本、按 Run 保存的执行额度/用量、Provider 超时/有限重试/独立熔断/并发隔离、版本化 Tool Registry、HITL、签名 Action Token 和幂等草稿。
- 多级费用预算：Redis 原子检查平台日/月、租户月、用户日和 Provider 额度；PostgreSQL 保存不可变流水与 Outbox，Redis 数据丢失时从数据库重建；模型与工具只能经统一 Gateway 预留、调用和结算。
- Agent 拓扑：LangGraph StateGraph 是确定性管理者，通过条件边只执行 Requirement Agent 或 Sourcing Agent；两者使用独立 SDK 实例、输入投影和静态工具白名单。它不是 Manager LLM，Policy/Verification 仍是确定性服务。
- Durable Workflow 首片：`WAITING_USER` 和 `WAITING_CONFIRMATION` 使用真实 LangGraph `interrupt`；API 使用 tenant-scoped `thread_id` 恢复，恢复前检查业务 `state_version`。JSON/测试模式使用内存 checkpointer，PostgreSQL 模式使用 `AsyncPostgresSaver`，并保留 `legacy` 回滚开关。
- Agent 工具闭环：完整多轮消息进入 Planner；模型不能传入价格、预算或虚构 Offer，已查询目录结果会被 Engine 复用；结构化候选只有通过服务端证据检查后才能改变综合推荐。独立 live runner 已记录真实模型的轨迹、token、延迟和估算费用。
- PostgreSQL 数据库：SQLAlchemy Repository 与 Alembic migration 已接入；Run 使用 JSONB 保存完整快照，消息和 Trace 同时写入可查询表；租户条件、乐观锁和草稿唯一键已在真实 PostgreSQL 17 上验证。
- 长期记忆第一片：Profile/Team/Episodic/Semantic 四种类型已落库；创建必须显式同意，个人/团队读取边界和创建者写权限由 Repository 强制，支持版本化修改、软删除和全生命周期审计。
- M2.1 上下文装配：版本化 ContextPolicy 控制两个 Agent 各自可见的 Key、Scope、消息数和 token；同 Key 冲突只保留一个结果，本次明确输入优先；Context Trace 记录选择/排除元数据但不复制私人正文。
- M2.2 结构化记忆：Registry 定义允许保存的 Key、Value Schema、品类和 Tag；歧义品类、未知 Tag、错误值和越权 TEAM 写入会被拒绝；Consent、请求幂等和版本替代在数据库事务中完成。
- M2.3 记忆候选：仅从已完成 Run 的用户原话保守提取长期偏好；临时需求、助手建议和凭证不会生成候选；候选经用户接受后才复用 Consent 与幂等链路写入长期记忆，支持拒绝、过期、去重和冲突提示。
- 制度 RAG 混合检索：制度原文按租户、品类、版本和生效期独立治理；PostgreSQL GIN FTS 与 pgvector HNSW 各取 Top-K，经 RRF 融合和确定性重排返回条款级引用；每条引用暴露通道、rank、相似度与融合分，向量服务故障时降级到 FTS；引用与 Policy-as-Code 版本不一致或无有效条款时安全停止。
- 文件与向量管线：支持 PDF、DOCX、UTF-8 文本和 Markdown 上传，保留不可变原文件、SHA-256、解析器版本与提取统计；DOCX 表格转为结构化 Markdown，扫描 PDF 可显式接 Tesseract OCR。embedding 可在离线哈希基线与独立密钥的 OpenAI provider 间切换，模型变更通过 tenant-scoped 分批回填任务恢复。
- 写前重校验：Token 绑定需求版本、动作和报价快照；价格变化时展示 Diff 并要求重新确认。
- 可重复评测：固定 seed、clock 和故障场景，生成 JSON/HTML 基线报告。

## 规划中的能力

- 在受控预算下执行并归档双 Agent DeepSeek 12-case × 3 稳定性矩阵；
- 扩大到真实企业制度语料，补复杂合并表格、多栏 PDF 与 OCR 人工抽检；
- 补 Recall@K、citation precision/recall、cross-encoder rerank 与真实语义模型对比；
- 更大别名库、hybrid retrieval、pairwise matcher 与真实 Provider 标注集上的召回/置信度校准；
- 真实模型扩大样本、费用账单校对、Agent SDK 内部单次工具调用中断恢复、跨进程租约与 cassette replay；
- 60/180 场景分层评测、真实模型成本与延迟基线；
- 经过 holdout、人工审批、canary 和 rollback 的受控自进化。

## 边界

- MVP 不下单、不付款、不保存银行卡，也不直接提交生产审批。
- 不绕过验证码、登录限制、robots 或平台条款；浏览器只作为合规兜底。
- 价格、库存、预算和制度结论只能来自工具证据，不能来自模型记忆。
- 任何采购草稿写入都需要显式确认，并由服务端重新鉴权和复核。
- fixture 模式保证面试现场离线可演示；live 模式证明真实集成能力，但不影响核心演示稳定性。
