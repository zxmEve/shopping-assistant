# ProcurePilot：企业采购决策与审批 Agent

> 当前状态：Phase 1 核心闭环已实现，完整 P0 正在按需求追踪矩阵补齐。默认使用可复现 fixture 模式；真实 OpenAI、长期记忆、自进化和生产工作流尚未完成。

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

## 快速开始

```bash
cd shopping-assistant
make setup
make test
make eval
make dev
```

打开 `http://127.0.0.1:5173`。`make dev` 会同时启动 FastAPI 与 Vite；也可以使用 `docker compose up --build`。

默认 `PROCURE_AGENT_MODE=deterministic`，用于离线演示和 CI。启用真实 OpenAI Agents SDK：

```bash
cp .env.example .env
# 在 .env 中设置 PROCURE_AGENT_MODE=openai 和 PROCURE_OPENAI_API_KEY
make dev
```

当前自动评测基线：41/41 Python 测试和 26/26 deterministic 场景通过，veto failures 为 0。商品 hard-case grader 的 Product/Variant match precision 均为 100%，hard false merge 为 0；状态迁移、预算拦截和重试/熔断场景均通过。报告位于 `backend/evals/reports/latest.html` 和 `latest.json`。这些结果只代表确定性 fixture，不代表真实商城、真实模型或生产采购效果。

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
- 多 Agent 拓扑：OpenAI Agents SDK Manager + specialists-as-tools；默认离线模式使用确定性 Planner。
- 写前重校验：Token 绑定需求版本、动作和报价快照；价格变化时展示 Diff 并要求重新确认。
- 可重复评测：固定 seed、clock 和故障场景，生成 JSON/HTML 基线报告。

## 规划中的能力

- 更大别名库、hybrid retrieval、pairwise matcher 与真实 Provider 标注集上的召回/置信度校准；
- 真实模型 token/cost 回传、跨进程熔断、checkpoint 和 cassette replay；
- PostgreSQL/Temporal 与可恢复执行；
- Profile/Team/Episodic/Semantic 长期记忆；
- 60/180 场景分层评测、真实模型成本与延迟基线；
- 经过 holdout、人工审批、canary 和 rollback 的受控自进化。

## 边界

- MVP 不下单、不付款、不保存银行卡，也不直接提交生产审批。
- 不绕过验证码、登录限制、robots 或平台条款；浏览器只作为合规兜底。
- 价格、库存、预算和制度结论只能来自工具证据，不能来自模型记忆。
- 任何采购草稿写入都需要显式确认，并由服务端重新鉴权和复核。
- fixture 模式保证面试现场离线可演示；live 模式证明真实集成能力，但不影响核心演示稳定性。
