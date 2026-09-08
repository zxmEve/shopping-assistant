# 第一阶段范围与完成指标

## 1. 阶段目标

第一阶段只交付一条可重复演示、可回放、可量化的企业采购核心闭环：

```text
模糊需求 -> 主动追问 -> 多来源检索 -> 同款归一/成本计算
-> 预算与制度校验 -> 有证据的推荐 -> 人工确认 -> 采购草稿
```

这条链路优先证明 Agent 是否会正确询问、选择工具、分工协作、使用外部事实、遵守副作用边界，并能通过统一评测回归。模拟商城和企业系统只作为可控工具环境，不作为独立产品建设。

## 2. 第一阶段选择的功能

### 2.1 Application Runtime（不是 Agent）

固定代码维护采购状态，根据必填字段是否完整，在两个 Agent 之间选择下一步，并调用成本、预算、政策和证据服务。

选择原因：当前路由条件可以完全枚举。用代码路由更便宜、稳定、可测试，模型不可用时也能安全回退。

### 2.2 Requirement Agent

从自然语言中抽取数量、预算、税口径、交付地、交期和最低配置，按信息增益一次提出 1～3 个关键问题；支持“按团队标准决定”和停止追问。

选择原因：主动追问是普通聊天机器人与任务型 Agent 最明显的差异，也是企业采购中避免错误检索和错误审批的第一道防线。

### 2.3 Sourcing Agent

调用三个逻辑 provider，在服务端已经规范化和验证的报价中比较候选，只返回真实 provider/offer ID 和目录证据。Product/Variant/Offer 分层、冲突否决，以及金额、税费、运费、优惠与批量总价均由确定性领域服务处理。

选择原因：它覆盖 Tool Calling、并行 I/O、实体归一、证据 grounding 和 LLM/确定性代码分工，是项目最有辨识度的 Agent 能力。

### 2.4 Budget & Policy Service（不是 Agent）

读取预算、供应商和版本化制度，直接产出 `ALLOW/DENY/ALLOW_WITH_APPROVAL` 及审批路径。自然语言说明可以由展示层生成，但模型不能覆盖规则结果。

选择原因：企业采购的价值不只是低价，而是“在预算和制度内做决策”；这些结论必须可重复和可审计，所以属于服务而不是第三个 Agent。

### 2.5 有证据的推荐

输出最低成本、最快交付和综合最优候选；每个价格、规格、交付、预算和制度结论都必须引用 `evidence_ref`，并显示未验证信息和 trade-off。

选择原因：把多个 Agent 的结构化产物汇总为可决策结果，也是防止幻觉最容易被面试官验证的部分。

### 2.6 HITL 采购草稿

用户明确确认后，服务端重新校验报价、预算、制度、身份和 action token，只创建 `DRAFT_NOT_SUBMITTED` 草稿。重复确认通过业务幂等键返回同一草稿。

选择原因：没有副作用边界的 Agent Demo 不完整；草稿操作足以展示确认、鉴权、事实复核和幂等，又不会引入真实下单或审批风险。

### 2.7 最小 Agent Harness

第一阶段 Harness 只实现：

- run/state/step 持久化；
- 工具 registry、READ/BUSINESS_WRITE 风险等级和 allowlist；
- 最大步骤、工具调用、token、超时预算；
- 结构化输出校验和有限重试；
- trace、evidence、prompt/model/tool/policy 版本；
- fixture record/replay；
- HITL gate 和 action token。

选择原因：这些能力直接决定 Agent 是否可控、可查和可评测。Temporal、Kafka、复杂熔断和分布式调度先不引入。

### 2.8 39 个核心评测场景

第一阶段不追求 60/180 个数量；初始 20 个高密度场景完成后，又增加 3 个商品归一场景、3 个 Harness 场景、7 个 Agent 场景、3 个数据库场景和 3 个长期记忆场景：

| 类别 | 数量 | 必测内容 |
|---|---:|---|
| 主动追问 | 5 | 缺关键字段、允许默认、不应追问、纠正、停止条件 |
| 同款与成本 | 5 | RAM/SSD 差异、翻新、套装、条件优惠、批量金额 |
| 预算与制度 | 3 | 预算不足、非准入供应商、审批路由变化 |
| Harness/HITL | 4 | 未确认写入、重复确认、旧 token、写成功响应超时 |
| 故障与安全 | 3 | provider 429、部分成功、商品描述间接注入 |
| 商品匹配专项 | 3 | Product/Variant precision、hard false merge/conflict veto、低置信度复核 |
| Harness Runtime | 3 | 合法状态路径、预算前置拦截、有限重试与独立熔断 |
| Agent Runtime | 7 | 严格只读工具面、可信 Offer 链、多轮消息上下文、结构化选品、错误候选 veto、合法轨迹、重复调用识别 |
| Database Runtime | 3 | 完整回写、tenant/user 隔离、旧版本写入冲突 |
| Memory Runtime | 3 | 个人/团队读取边界、创建者写权限、版本化修改/删除和审计 |

选择原因：先验证场景 Schema、grader 和回放是否可靠，再扩数据；低质量的大评测集会掩盖系统问题。

## 3. 最小模拟范围

第一阶段只有一个品类和两组 fixture 文件：

- 商品：40 个基线 Product、120 个基线 Variant、450 条常规 Raw Offer 和 9 条归一 hard cases，三个逻辑 provider；
- 企业：2 个 tenant、6 个员工、3 个成本中心、8 个供应商、8 条制度规则、3 条审批路由；
- 实现：一个进程内 `FixtureCatalogAdapter` + 一个 `FixtureEnterpriseGateway`；
- 故障：通过 scenario manifest 切换，不建设独立故障平台；
- 存储：默认 fixture 使用原子 JSON Repository；同一 Repository Port 已接入 PostgreSQL/SQLAlchemy adapter，可通过配置切换；
- UI：只做采购对话、候选比较、确认草稿和 trace 时间线四个页面/区域。

不建设模拟商城后台、供应商后台、ERP UI、复杂商品管理、爬虫、大规模搜索集群或独立微服务。

## 4. 第一阶段明确不做

- 长期记忆和个性化学习；
- 价格 Watcher、通知、Kafka/Redpanda；
- Temporal durable workflow；
- 浏览器自动化和真实商城 adapter；
- 大规模向量库、ESCI/ShoppingBench 全量导入；
- 自动 prompt 优化、自进化、shadow/canary；
- 真实审批提交、下单或支付；
- 完整管理后台和移动端。

这些功能在核心链路指标达标后按失败数据决定优先级，而不是预先全部建设。

## 5. 阶段完成指标

Phase 1 已完成核心闭环的首轮实现和确定性评测，但不等同完整 P0 已验收。当前真实结果以 `backend/evals/reports/latest.json` 为准，逐条需求状态以 `08-requirements-traceability.md` 为准；下面的门槛继续作为回归标准。DeepSeek live 小样本独立报告，不能和 deterministic fixture 分数混算。

### 5.1 一票否决指标

| 指标 | 门槛 |
|---|---:|
| 未确认或未授权的 `BUSINESS_WRITE` | 0 次 |
| 重复确认产生的重复草稿 | 0 个 |
| 跨租户预算、制度或草稿泄漏 | 0 次 |
| 金额 golden exact match | 100% |
| 价格/规格/预算/制度事实 evidence coverage | 100% |
| 商品描述间接 prompt injection 导致策略/工具越权 | 0 次 |
| 39 个场景 deterministic replay | 100% 可复现 |

任何一项失败，第一阶段不得标记完成。

### 5.2 Agent 质量指标

| 指标 | 初始门槛 |
|---|---:|
| 39 个场景端到端任务成功率 | >= 85%（至少 34/39） |
| 必要追问字段 Recall | >= 95% |
| 不必要问题率 | <= 10% |
| 最大连续追问轮数 | <= 3 |
| Pydantic 结构化输出最终有效率 | 100% |
| Product/Variant false merge | 0 个 hard case |
| 硬约束满足率 | >= 98% |
| 制度判定准确率 | 100%（首批确定性规则集） |
| 必需工具遗漏率 | <= 5% |
| 禁止工具调用率 | 0% |

### 5.3 工程与性能指标

| 指标 | 初始门槛 |
|---|---:|
| fixture 模式端到端 p95 | <= 20 秒 |
| 首个进度事件 p95 | <= 2 秒 |
| 单次成功任务工具调用数 | <= 12 次 |
| 单次 run 最大 Agent step | <= 15 |
| 单一 provider 失败后的可用结果返回率 | 100% |
| 同一 cassette 的状态、工具和结果 diff | 0 |
| 单元/领域/adapter contract tests | 100% 通过 |

模型 token 和金额成本先记录基线，不在没有真实运行数据时虚构绝对门槛；首个基线完成后，以“质量不下降的最低成本版本”作为后续回归比较对象。

## 6. 阶段交付物

第一阶段只有在以下产物同时存在时才算完成：

1. 一键启动的 fixture Demo；
2. 一条正常采购和一条攻击/故障演示脚本；
3. 39 个版本化场景、golden state、grader 和确定性结果投影；完整 cassette 仍按追踪矩阵补齐；
4. 可查看的 Agent/tool/state/evidence trace；
5. 自动生成的 HTML/JSON 评测报告；
6. 架构说明、已知限制和失败案例复盘；
7. CI 中的确定性测试与 veto gate。
