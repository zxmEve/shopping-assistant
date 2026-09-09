# ADR-0010：只保留两个 Agent，其余能力用确定性服务实现

- 状态：已采用，替代原“五个 Agent”方案
- 日期：2026-09-08
- 影响范围：Agent Runtime、Provider Adapter、工具权限、评测、架构说明

## 决策

ProcurePilot 只保留两个模型角色：

1. **Requirement Agent**：处理用户表达不完整、含糊和追问选择。
2. **Sourcing Agent**：在可信目录返回的真实商品中做多目标权衡并提出候选。

原来的 Orchestrator Agent 改为固定的 `Application Runtime`。原来的 Policy Agent 和 Verification Agent 改为普通 Python 服务。成本计算从 Sourcing Agent 的工具中移出。

## 为什么这样决定

### 原方案的问题

原方案一共有五个模型角色：Orchestrator、Clarification、Sourcing、Policy、Verification。它看起来更像“多 Agent”，但其中三类工作其实没有开放式推理空间：

- Orchestrator 只需根据“需求是否完整”二选一；
- Policy 的最终结论来自预算比较和版本化规则；
- Verification 只需检查 ID、状态版本和证据集合是否匹配。

让模型执行这些工作会产生额外请求、延迟和失败点，也让读代码的人难以回答“为什么这里必须使用 Agent”。

### 选择两个 Agent 的理由

Requirement Agent 必须理解自然语言中真正缺失的信息，并判断哪个问题最值得先问。Sourcing Agent 必须在价格、交付、匹配程度之间做权衡。这两项工作存在多个合理答案，适合模型。

其余工作都有可计算或可枚举的答案，适合确定性代码。企业采购尤其要求金额、预算、制度、权限和副作用可重复、可审计，因此不能把最终决定交给模型。

## 具体边界

| 组件 | 是 Agent 吗 | 权限 |
|---|---|---|
| Application Runtime | 否 | 选择阶段、调用 Agent/服务、推进状态 |
| Requirement Agent | 是 | 仅 `inspect_procurement_requirement` |
| Sourcing Agent | 是 | 仅 `search_catalog` |
| Cost Service | 否 | 使用 `Decimal` 计算到手价 |
| Budget & Policy Service | 否 | 读取可信企业数据并返回规则结论 |
| Evidence Validation | 否 | 校验本 Run 的 offer ID 与 evidence refs |
| Harness | 否 | 权限、预算、超时、重试、状态、Trace、HITL、幂等 |

Agent 看不到预算、制度、写入或状态迁移工具。即使 Sourcing Agent 返回一个真实但不合规的商品，服务端规则仍会否决它。

## 为什么不用 LLM Orchestrator

当前路由只有一个可信条件：必要字段是否齐全。固定代码可以 100% 复现这个判断，也可以在模型不可用时继续工作。使用 LLM 路由不会带来新的业务能力。

将来只有在出现多个无法由状态直接决定的语义任务，而且评测证明固定路由成为瓶颈时，才重新考虑 LLM Orchestrator。

## 为什么 Policy 和 Verification 不是 Agent

Policy 是公司规则的执行，不是自由讨论。模型可以将规则解释成自然语言，但不能决定 `ALLOW` 或 `DENY`。

Verification 是集合和版本校验：候选是否来自当前 Run、证据是否属于该商品、结果是否陈旧。这些条件应该写成断言和测试，而不是提示模型“认真检查”。

## 工具和错误处理

- 两个 Agent 使用独立 SDK Agent 实例、独立输入和静态工具白名单；
- 参数由 Schema 校验，provider 使用枚举；
- 重复调用、越权工具和陈旧结果由 Runtime 拒绝；
- 模型输出只能提交 provider/offer ID 和目录证据，不能提交价格或政策结论；
- Engine 对候选做第二次服务端校验；
- Agent 失败时回退到确定性推荐；
- 任何业务写入仍需用户确认、写前复核和幂等键。

## 代价

- “Agent 数量”从五个减少为两个，演示上不再追求角色数量；
- Sourcing Agent 不能直接看到内部预算和政策，因此它提出的是“寻源偏好”，不是最终采购结论；
- 现有 `direct_tools` 和 `explicit_multi_agent` 配置名需要迁移。代码暂时把它们映射到 `two_agent`，但运行时不会恢复旧拓扑。

这些代价是可接受的，因为架构更便宜、更快、更容易测试，也更符合企业系统对决定权的要求。

## 验收证据

- Provider Adapter 只创建两个独立 Agent/模型实例；
- 完整需求只执行 Sourcing Agent，缺字段只执行 Requirement Agent；
- Agent 工具白名单只有需求检查与目录搜索；
- Orchestrator 模型请求为 0；
- 政策、推荐和证据 Trace 归属于确定性服务；
- 陈旧结果与越权调用会被拒绝并安全回退；
- 当前后端回归测试为 103/103 通过，包含 M2.1 上下文装配、M2.2 结构化记忆和 M2.3 候选确认测试。
