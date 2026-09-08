# ADR-0007：Agent 只能使用服务端提供的采购事实

- 状态：已被 ADR-0010 取代（本文保留为历史设计记录）
- 日期：2026-09-04
- 对应需求：IAM-03、REQ-02、SRC-02、COST-01、GOV-03、OPS-02、OPS-04

## 一句话结论

本 ADR 记录了早期“三个专家 + 五个只读工具”方案。当前实现已按 ADR-0010 收敛为 Requirement/Sourcing 两个 Agent；成本、预算和制度工具不再暴露给模型。创建草稿仍留在服务端确认流程中。

## 为什么现在要做这件事

此前 OpenAI Agents SDK 只生成一个“下一步计划”，目录、预算和制度仍全部由固定 Python 流程调用。这能证明业务闭环，却不能证明 Agent 会正确选择工具、填写参数和利用多轮上下文。

如果直接把现有 Python 函数全部暴露给模型，又会产生新的问题：模型可能在工具参数里填写一个更低的价格、更高的预算，或者虚构一个不存在的商品 ID。工具调用看起来成功，实际事实已经被模型改写。

因此第一片先完成只读事实链路，不急着把写操作交给 Agent。

## 当前工具怎样分配

```text
Clarification Agent
  -> inspect_procurement_requirement

Sourcing & Comparison Agent
  -> inspect_procurement_requirement
  -> search_catalog
  -> calculate_landed_cost

Policy Reviewer Agent
  -> get_budget
  -> evaluate_policy
```

OpenAI 路径由总控 Agent 通过 specialists-as-tools 调用三个专家。DeepSeek 路径让同一个总控 Agent 直接调用这五个函数，因为实测中动态移除专家工具会让该 Provider 继续请求已经不可用的工具，既浪费预算又不能完成任务。两条路径的业务权限、工具实现、Harness 和最终服务端校验完全相同。审批、下单和支付工具不存在；创建采购草稿也没有进入这组只读工具。

## 为什么工具参数很少

`search_catalog` 只允许模型选择三个已登记 Provider 之一。数量、最低内存和最低存储直接读取当前 Run 中已经确认的需求。

`calculate_landed_cost` 和 `evaluate_policy` 只接收 `provider + offer_id`。这个报价 ID 必须来自本轮成功的 `search_catalog`；价格、运费、税率、供应商和证据由服务器缓存的规范化 Offer 提供。

为什么这样选择：模型适合决定“查哪个来源、检查哪个候选”，不适合充当采购事实的来源。减少参数也减少了填错和篡改事实的空间。

## 多轮上下文怎样进入 Agent

每次规划不再只传用户最新一句话，而是传入当前 Run 的完整消息：

```text
用户最初需求
-> 系统追问
-> 用户补充
-> 当前规划
```

这样 Agent 能看到哪些问题已经问过、用户确认过哪些条件，避免把多轮任务当成几个无关请求。业务需求仍保存在结构化 `ProcurementRequirement` 中，消息历史不能覆盖服务端事实。

## Agent 已查过的结果为什么要复用

如果 Sourcing Agent 已调用内部目录，后面的确定性推荐流程不会再次查询同一个 Provider，而是复用同一份 `CatalogSearchResult`。没有被 Agent 查询的来源仍由 Engine 补齐。

为什么：第一阶段采用渐进接入，Agent 和确定性流程暂时并存。复用可以避免重复网络调用，同时保持完整的三来源推荐和现有安全门禁。

## Agent 怎样参与最终选品

Orchestrator 可以返回最多三个结构化候选，每个候选只包含 `provider`、`offer_id` 和使用过的 `evidence_refs`。Engine 不直接相信这个结果，而是检查：报价确实存在于本轮候选、原始报价证据存在、引用没有超出该候选的可信证据，并且同时包含预算和制度证据。

全部通过时，该候选才成为综合最优，并记录 `selection_source=agent_validated`；任一检查失败就记录拒绝事件，并继续使用确定性评分结果。这样模型可以做有意义的取舍，但不能借结构化输出制造商品或证据。

## Harness 仍然负责什么

每个 SDK function tool 都复用同一个 Tool Registry：

- 未登记工具不能调用；
- 角色不允许时拒绝；
- 工具和 Provider 调用占用 Run 预算；
- Provider 调用继续执行 timeout、retry、circuit 和并发限制；
- 每个结果写入原有 Trace。

模型看到工具并不代表模型获得权限，权限仍由服务器决定。

## 测试如何证明它有效

- SDK 拓扑能够离线构造，三个专家只获得各自需要的工具；
- JSON Schema 禁止额外参数，Provider 是三个固定枚举值；
- 预算、目录、成本和制度工具能形成真实的只读证据链；
- 模型虚构的报价 ID 不能进入成本或制度判断；
- Engine 每轮把完整消息历史交给 Planner；
- Agent 已查询的 Provider 结果只执行一次，后续流程直接复用；
- 带完整目录、预算和制度证据的 Agent 候选可以参与最终推荐；
- 虚构候选或证据会被服务端拒绝，并安全回退到确定性排序；
- trajectory grader 会检查必需工具是否遗漏、同参数调用是否重复、候选是否经过制度工具以及推荐后是否等待人工确认；
- Agent 工具表不存在创建草稿、审批、下单和支付。

## 当前边界

- M2 最小 Agent 闭环已经完成；这只表示固定 fixture 工具下的基线通过，不表示生产质量已经得到证明。
- Agent 已通过 `AgentPlan` 的结构化候选参与推荐；最终候选有效性、状态迁移和草稿写入仍由 Engine 决定。
- trajectory grader 已能检查工具遗漏、重复调用、候选依据和停止条件；DeepSeek 3 次 live 运行全部通过，p95 为 10.859 秒。
- DeepSeek 三次都在 JSON 前后输出了额外文字。运行时只对最后一条模型消息做受限 JSON 提取，再执行完全相同的 Pydantic 校验，并将修复写入 Trace；没有合法对象时仍然失败。
- 当前费用是按版本化公开单价做的保守估算，真实账单以 Provider 为准；3 次样本不能代表长期稳定性。

## 术语对照

- Function tool：模型可以选择调用、但实际由服务器执行的函数。
- Grounded / 有事实依据：结果来自目录、预算或制度工具，而不是模型猜测。
- Schema：工具允许接收哪些字段和值的明确格式。
- Runtime context：服务器在本次 Agent 运行中提供的用户、Run、工具和已查询结果。
