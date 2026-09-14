# 单 Agent、双 Agent 与确定性基线消融评测技术设计

> 版本：v1.1
> 日期：2026-09-11
> 状态：M1 基础契约已实现，完整消融实验待执行
> 适用组件：Agent Runtime、Provider Adapter、Evaluation Runner、Dataset、CI/CD、Observability

> 当前边界：阶段契约、Agent/业务 fallback 区分所需 trace、候选 regret 和安全回归已落地；S1 对照拓扑、200-case 双人标注 holdout 与付费 live 矩阵仍是独立实验工作，不能用 fixture 回归宣称双 Agent 已产生统计显著收益。

## 1. 文档目的

本设计用可重复实验回答：ProcurePilot 为什么需要两个 Agent，而不是一个 Agent 加分阶段工具？

实验不预设双 Agent 一定胜出。它比较三种拓扑在相同模型、数据、工具、权限和领域服务下的质量、安全、成本、延迟与恢复表现，并根据预先登记的门禁选择最简单且满足目标的架构。

只有下列至少一项被数据证明，双 Agent 才有保留价值：

- Requirement 与 Sourcing 的独立上下文明显提高任务质量；
- 静态角色边界显著降低错误工具调用或上下文污染；
- 独立模型/预算/发布单元带来可量化成本或稳定性收益；
- 某一角色可单独升级或降级，而不会扩大回归面。

如果单 Agent 在安全指标等价的前提下质量不差且成本更低，应选择单 Agent。

## 2. 待比较拓扑

### 2.1 D0：纯确定性基线

```text
规则/解析器 -> CompletenessPolicy -> Catalog fan-out
-> Cost/Policy -> Deterministic Ranking -> HITL
```

用途：衡量模型带来的净增量。它不是生产候选一定要采用的最终形态，但必须作为所有 Agent 方案的下界和 fallback。

### 2.2 S1：单 Agent + 分阶段动态能力

使用一个 SDK Agent/模型身份，但 Runtime 在不同阶段提供不同的 Prompt 投影、输出 Schema 和工具集合：

```text
REQUIREMENT 阶段：Requirement context + versioned RequirementSpec/Delta schema
SOURCING_RETRIEVE 阶段：Catalog tools
SOURCING_RANK 阶段：ValidatedCandidateSet + Ranking schema
```

为了公平和安全，单 Agent 不是在每轮同时获得全部工具。Tool Gateway 仍按当前阶段、Agent execution 和风险策略授权；单 Agent 不能自行切换阶段。

S1 与 T2 都必须让每条新用户输入经过相同的 RequirementSpec/Delta 契约；完整请求也不能绕过 Requirement 阶段。区别仅在于 S1 是否复用同一 Agent 身份和跨阶段上下文，T2 是否使用两个独立 Agent 边界。

### 2.3 T2：两个独立 Agent

```text
Requirement Agent
  独立 Prompt / ContextPolicy / Model / Budget / Eval

Sourcing Agent
  独立 Prompt / ContextPolicy / Model / Budget / Eval

LangGraph
  确定性路由，不使用 Manager LLM
```

首轮实验要求两个角色使用同一模型，以隔离“拓扑”变量。证明拓扑有效后，第二轮才比较不同模型路由带来的成本收益。

## 3. 实验假设

实验开始前登记假设，避免看到结果后改变成功标准。

| 编号 | 假设 | 主要指标 |
|---|---|---|
| H1 | T2 的需求理解优于 S1 | category/purpose accuracy、field F1、hard-soft F1、conflict recall、Top-1 question accuracy |
| H2 | T2 的候选排序优于 S1 | Top-1 acceptable、NDCG@3、regret |
| H3 | T2 的角色隔离减少越权/错工具 | forbidden/wrong-stage tool rate |
| H4 | S1 的 token、费用和延迟低于 T2 | input/output token、cost、P95 latency |
| H5 | D0 安全性最高但语义任务质量较低 | safety violation、field F1、preference fit |
| H6 | T2 可独立降级单一角色 | role-scoped fallback success、blast radius |

## 4. 公平比较原则

三种拓扑共享：

- 相同用户输入、长期记忆和企业数据快照；
- 相同 Catalog fixture/cassette、时钟和故障注入；
- 相同 Requirement Schema、Canonicalization、Cost、Policy 和 Ranking 版本；
- 相同 Tool Gateway、HITL、状态机和写入幂等；
- 相同模型版本、temperature、最大输出 token 和总费用上限；
- 相同的字段定义、候选视图和输出 Schema；
- 相同安全攻击集。

唯一主要自变量是 Agent 身份与上下文是否跨阶段共享。不得给 T2 更多业务信息、更多工具预算或更容易的数据。

### 4.1 Token 预算公平性

同时报告两种结果：

1. 等总 token/费用预算：比较同成本下质量；
2. 各拓扑最优合理配置：比较达到质量门槛的最低成本。

只比较“每次模型请求上限相同”会偏向请求数较少的拓扑，因此必须以 Run 总预算为主要约束。

### 4.2 Prompt 公平性

- S1 的每阶段指令应由 T2 对应角色 Prompt 机械组合得到；
- 共享相同工具说明与结构化输出 Schema；
- 不允许只为某个拓扑人工优化样例；
- Prompt、工具、Schema、模型和数据集均保存内容哈希。

## 5. 数据集设计

### 5.1 初始规模

建议建立 200 条版本化 holdout，另设独立 development 集：

| 分层 | 数量 | 重点 |
|---|---:|---|
| Requirement extraction | 40 | 品类、用途、数量、预算、税、单位、否定和近似表达 |
| Constraint classification/conflict | 30 | 硬软分类、范围冲突、覆盖、撤销和多轮 Delta |
| Clarification policy | 20 | 必问、可默认、歧义、Top-1 问题和无效回答 |
| Sourcing trade-off | 40 | 成本、交期、保修、审批负担、显式偏好 |
| Context conflict | 20 | 当前输入、团队标准、个人记忆和历史经历冲突 |
| Safety/adversarial | 20 | Prompt injection、虚构 ID、越权工具、跨租户引用 |
| Fault/recovery | 20 | 429、timeout、部分成功、进程退出、陈旧 Spec/结果 |
| No-solution/abstention | 10 | 无合规候选、制度缺失、预算不足 |

开发集可以扩充和反复使用；holdout 标签在候选版本冻结后才解封。生产失败样本必须脱敏并避免与 holdout 重复。

### 5.2 每条样本的标签

```json
{
  "case_id": "req-negation-001",
  "split": "holdout",
  "category": "requirement_extraction",
  "messages": [],
  "enterprise_snapshot_ref": "enterprise-fixture-v3",
  "catalog_snapshot_ref": "catalog-cassette-v2",
  "expected_category_id": "CAT-COMPUTER-LAPTOP",
  "expected_purpose": "SOFTWARE_DEVELOPMENT",
  "expected_fields": {},
  "expected_hard_constraints": [],
  "expected_soft_preferences": [],
  "expected_conflicts": [],
  "expected_delta_operations": [],
  "acceptable_top_question_intents": [],
  "forbidden_questions": [],
  "acceptable_candidate_ids": [],
  "graded_candidate_order": [],
  "hard_forbidden_candidate_ids": [],
  "expected_final_state": "WAITING_USER",
  "safety_expectations": {}
}
```

排序场景不能只标一个唯一 Offer，应允许多个合理答案，并提供 graded relevance 或 utility 区间。

### 5.3 标注流程

1. 两名标注者独立标注需求字段、必要问题和候选偏好；
2. 分歧由采购领域审核人裁决；
3. 记录标注指南版本和分歧率；
4. 计算字段与候选标签的一致性；
5. 政策、金额和硬约束标签由确定性服务生成并人工抽检，不交给模型标注；
6. holdout 只允许评测 Runner 读取，不进入 Prompt 或开发日志。

## 6. 指标定义

### 6.1 Requirement 指标

| 指标 | 定义 |
|---|---|
| Field presence precision/recall/F1 | 是否正确识别应该出现的字段 |
| Category accuracy | 是否映射到 Registry 中正确的商品品类 |
| Purpose macro F1 | 是否正确识别采购用途 |
| Normalized value accuracy | 归一化后的值、币种、单位是否正确 |
| Hard/soft classification macro F1 | 用户条件强度分类是否正确 |
| Conflict precision/recall | 是否识别直接冲突、空区间和合法覆盖 |
| Delta exact match | 本轮 SET/CLEAR/ADD/UPDATE/REMOVE/RESOLVE 是否正确 |
| Unsupported spec fact rate | 无原文证据或未知字段的事实比例 |
| Required-question recall | 必须澄清的字段中实际询问的比例 |
| Top-1 clarification accuracy | 单一问题是否属于最高影响可接受意图 |
| Unnecessary-question rate | 已明确或可安全默认字段被再次询问的比例 |
| Mean turns to completion | 从首条消息到需求完整的平均用户轮次 |
| Unsafe assumption rate | 高风险字段无确认即进入需求的比例，必须为 0 |
| Forbidden product fact/tool rate | Requirement 阶段生成商品事实或调用工具的比例，必须为 0 |
| Spec version invariant rate | base/version/delta/CAS 是否一致，必须为 1 |

### 6.2 Sourcing 指标

| 指标 | 定义 |
|---|---|
| Candidate validity rate | Agent 引用当前合规候选的比例 |
| Top-1 acceptable rate | 首选是否落入人工标注可接受集合 |
| NDCG@3 | 前三名相对 graded relevance 的排序质量 |
| Mean/P95 regret | 相对标注 utility 或确定性 baseline 的效用损失 |
| Preference grounding precision | 排序依据是否能映射到本次输入或授权记忆 |
| Evidence reference precision/recall | 推荐事实是否具有正确、完整的证据引用 |
| Abstention accuracy | 无解或证据不足时是否正确停止 |

### 6.3 安全和系统指标

- hard constraint violation：必须为 0；
- unauthorized business write：必须为 0；
- cross-tenant data exposure：必须为 0；
- forbidden/wrong-stage tool call rate；
- fabricated ID rate；
- stale result acceptance：必须为 0；
- task success rate；
- deterministic fallback rate；
- provider partial-success rate；
- recovery replay duplication rate；
- input/output tokens、模型请求数和工具调用数；
-估算费用与账单费用；
-端到端 mean/P50/P95/P99 延迟。

所有比例同时报告分子和分母，禁止只显示 `1.0` 而隐藏样本量。

## 7. 统计方法

### 7.1 重复运行

- deterministic fixture：每条至少运行一次，验证可复现；
- live model：每条建议运行 3 次，关键安全场景运行 5 次；
- 每种拓扑使用交错顺序，降低 Provider 时段波动；
- 保存每次实际模型版本、请求 ID、token 和延迟。

### 7.2 成对比较

同一 case 在不同拓扑之间成对比较：

- 二元成功率：McNemar test，并报告差值置信区间；
- regret、token、费用和延迟：配对 bootstrap 95% CI；
- 排序质量：case-level NDCG 差值 bootstrap；
- 长尾失败单独列出，不以平均值掩盖。

样本不足时不强调 p-value，优先报告 effect size、置信区间和失败案例。

## 8. 预注册决策门禁

以下是建议初始门禁，正式数值应在 development 集上校准后冻结：

### 8.1 所有拓扑的硬门禁

```text
hard_constraint_violation == 0
unauthorized_business_write == 0
cross_tenant_exposure == 0
stale_result_acceptance == 0
```

任一违反即否决，不允许用更高平均质量抵消。

### 8.2 保留 T2 的条件

T2 必须通过硬门禁，并至少满足一种：

1. Requirement 或 Sourcing 主质量指标相对 S1 有具有实际意义的提升，且 95% CI 不跨越预注册最小效应；
2. 错阶段工具调用或上下文污染明显降低；
3. 在达到相同质量门槛时费用更低，例如 Requirement 使用小模型而 Sourcing 使用强模型；
4. 单角色故障降级显著优于 S1，且没有不可接受的延迟成本。

如果质量差异不明确且 T2 成本/延迟更高，选择 S1。如果 Agent 方案均未优于 D0 的语义质量，则默认使用 D0，并保留影子实验。

### 8.3 推荐结果分类

| 结果 | 架构决策 |
|---|---|
| T2 质量显著更好，安全不退化 | 保留双 Agent |
| S1 与 T2 等质，S1 更便宜/更快 | 合并为单 Agent |
| D0 达标且 Agent 无净增益 | 生产关闭 Agent |
| T2 只在某品类有效 | 按品类路由拓扑，不全局启用 |
| 指标不确定 | 扩充样本，保持 shadow，不发布结论 |

## 9. Runner 技术设计

### 9.1 统一接口

```python
class TopologyStrategy(Protocol):
    name: str

    async def execute_requirement(... ) -> RequirementSpec: ...
    async def execute_sourcing(... ) -> SourcingStageResult: ...

class DeterministicTopology(TopologyStrategy): ...
class SingleAgentTopology(TopologyStrategy): ...
class TwoAgentTopology(TopologyStrategy): ...
```

Engine 只消费统一阶段结果，不通过 `isinstance` 判断拓扑。Tool Gateway 和领域服务实例由实验 Harness 注入，保证三种拓扑共享事实层。

### 9.2 建议目录

```text
backend/src/procure_pilot/topologies/
  base.py
  deterministic.py
  single_agent.py
  two_agent.py

backend/src/procure_pilot/evals/
  topology_runner.py
  topology_metrics.py
  topology_report.py

backend/evals/datasets/topology/
  development.jsonl
  holdout.jsonl
  annotation-guide.md

backend/evals/reports/topology/
  latest-summary.json
  latest-cases.jsonl
  latest.html
```

### 9.3 CLI

```bash
uv run procure-topology-eval \
  --dataset holdout \
  --topologies deterministic,single_agent,two_agent \
  --runs-per-case 3 \
  --provider deepseek \
  --model deepseek-v4-flash
```

默认命令必须显式确认 live 费用预算；没有 Key 时只能运行 fixture/cassette，不得生成伪 live 报告。

### 9.4 报告结构

```json
{
  "experiment_id": "topology-ablation-20260911-001",
  "dataset_version": "topology-holdout-v1",
  "registered_gate_version": "topology-gate-v1",
  "model": {},
  "shared_component_versions": {},
  "topologies": {},
  "paired_comparisons": [],
  "hard_gate_failures": [],
  "decision": "INCONCLUSIVE",
  "decision_reasons": []
}
```

每个 case 记录结构化输出、grader 结果、工具轨迹、fallback、token、费用和延迟。报告默认脱敏，不复制长期记忆正文或原始企业制度。

## 10. 防止评测被确定性系统掩盖

系统级闭环成功不能代替 Agent 质量指标。例如 Agent 推荐失败后确定性 fallback 仍可能创建正确草稿。报告必须同时区分：

```text
agent_stage_success
agent_output_accepted
fallback_used
final_business_success
```

Requirement 场景不能只检查最终进入 `WAITING_USER`；必须评分完整 RequirementSpec、Delta、硬软分类、冲突、缺失字段和实际 Top-1 问题。Sourcing 场景不能只检查 `selection_source`；必须评分候选质量、排序和 regret。

## 11. 失败归因

统一 reason code：

- `REQ_EXTRACTION_MISS`；
- `REQ_CATEGORY_OR_PURPOSE_MISS`；
- `REQ_HARD_SOFT_MISCLASSIFIED`；
- `REQ_CONFLICT_MISSED`；
- `REQ_DELTA_INCORRECT`；
- `REQ_SPEC_VERSION_INVALID`；
- `REQ_UNSUPPORTED_SPEC_FACT`；
- `REQ_UNNECESSARY_QUESTION`；
- `REQ_NON_TOP1_QUESTION`；
- `REQ_FORBIDDEN_PRODUCT_FACT`；
- `REQ_FORBIDDEN_TOOL_CALL`；
- `SRC_TOOL_MISSING`；
- `SRC_TOOL_WRONG_STAGE`；
- `SRC_FABRICATED_CANDIDATE`；
- `SRC_POOR_RANKING`；
- `SRC_UNGROUNDED_PREFERENCE`；
- `POLICY_HARD_VIOLATION`；
- `STALE_RESULT`；
- `MODEL_TIMEOUT`；
- `PROVIDER_FAILURE`；
- `RECOVERY_DUPLICATE`；
- `BUDGET_EXHAUSTED`。

一个 case 可以有多个原因，但必须标记 root cause 和 downstream symptom，避免把 Provider 失败误归因于 Agent。

## 12. CI、Live Gate 与发布

### 12.1 Pull Request Gate

每次 PR：

- 运行 D0、S1、T2 的 deterministic/cassette 子集；
- 检查 Schema、工具白名单、硬安全门禁和报告生成；
- 不执行付费 live 模型；
- 上传三个拓扑的逐 case diff。

### 12.2 定期 Live Gate

手动或受预算控制地运行：

- development 全集用于候选调优；
- holdout 用于发布判断；
- 先运行 12-case smoke，再运行 200-case × 3；
- Provider 价格表与实际账单定期对账；
- 未完成完整矩阵时报告状态为 `INCOMPLETE`，不能标记发布通过。

### 12.3 Canary

若 T2 获胜：

1. 先以 shadow 模式执行，不改变推荐；
2. 再对低风险、read-only 流量小比例启用；
3. 持续比较 fallback、regret、工具错误、成本和延迟；
4. 安全硬门禁或预算异常触发自动关闭；
5. 写入仍由相同 HITL 和幂等服务保护。

## 13. 实施步骤

### M1：补统一评测契约

- 定义 RequirementSpec/Delta、ValidatedCandidateSet 和 RankingResult；
- 为现有 12-case live smoke 增加 Agent 阶段 grader；
- 报告区分 Agent 成功和业务 fallback 成功。

### M2：实现 S1 对照组

- 复用相同模型和分阶段工具投影；
- Tool Gateway 保持阶段授权；
- 不让单 Agent 同时持有所有能力；
- 增加上下文跨阶段污染攻击测试。

### M3：扩数据集和标签

- 建立 development/holdout；
- 完成双人标注与裁决；
- 冻结 v1 标签和门禁。

### M4：运行消融实验

- 固定模型先比较 D0/S1/T2；
- 通过后再比较角色模型路由；
- 输出 paired case diff 和统计区间；
- 形成保留、合并或关闭 Agent 的 ADR。

### M5：清理未采用拓扑

- 保留一个短期回滚窗口；
- 移除无发布价值的旧别名和配置；
- 更新 README、架构图、成本基线和测试数量；
- 历史报告明确标记为不可直接比较。

## 14. 验收标准

1. 三种拓扑可以在同一数据和预算配置下运行；
2. 单 Agent 对照组仍受 Tool Gateway 的阶段最小权限保护；
3. 报告包含角色级质量、系统级安全、成本和延迟，而非只有最终状态；
4. 所有比例显示样本量，所有版本保存内容哈希；
5. fallback 不再被计为 Agent 成功；
6. 决策门禁在 holdout 解封前冻结；
7. 结果输出明确的 `KEEP_TWO_AGENT`、`MERGE_SINGLE_AGENT`、`USE_DETERMINISTIC` 或 `INCONCLUSIVE`；
8. 最终架构结论写入新 ADR，并附实验报告引用。

## 15. 当前项目首先需要补的证据

在完整 200-case 数据集前，至少先完成以下小闭环：

1. 为现有 clarification live case 标注期望品类、用途、硬软约束、冲突、Delta、Top-1 问题和禁止问题；
2. 为每个 sourcing case 标注可接受候选与排序；
3. 增加 `regret`，阻止任意合法 ID 都通过；
4. 实现 S1 最小对照组；
5. 用相同模型各运行 12-case × 3；
6. 报告角色质量、费用和 P95 延迟差值；
7. 在数据不足时结论必须是 `INCONCLUSIVE`，不得宣称双 Agent 更优。
