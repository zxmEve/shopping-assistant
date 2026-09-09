# 项目进度与下一步计划

> 更新日期：2026-09-09
> 当前主线：双 Agent、记忆闭环和制度 RAG 的 FTS 最小纵向切片已完成；下一步补 pgvector + RRF 混合检索和标注检索集。

## 1. 当前结论

项目已经从“五个模型角色”收敛为“两个 Agent + 确定性服务”：

```text
用户请求
  -> Application Runtime（固定路由）
     -> Requirement Agent（需求不完整时）
     -> Sourcing Agent（需求完整时）
  -> 商品匹配 / 成本 / 预算 / 制度 / 证据服务
  -> 等待用户确认
  -> 写前复核
  -> 幂等采购草稿
```

这样调整不是降低项目难度，而是明确 Agent 与企业系统的边界：模型负责语义不确定性，代码负责事实、规则和副作用。

## 2. 已完成

| 能力 | 当前状态 | 代码/证据 |
|---|---|---|
| 双 Agent Runtime | 已完成 | 固定代码在 Requirement/Sourcing 之间路由，无 LLM Orchestrator |
| Provider Adapter | 已完成 | OpenAI/DeepSeek 兼容；恰好创建两个 SDK Agent |
| Agent 权限 | 已完成 | Requirement 仅需求检查；Sourcing 仅目录搜索 |
| 候选复核 | 已完成 | Agent 只返回 ID/目录证据，Engine 校验后才能影响推荐 |
| 确定性业务服务 | 已完成 | 商品硬约束、`Decimal` 成本、预算、政策、证据、排序 |
| Harness 预算 | 已完成 | Agent 回合、模型请求、工具、Provider、token、费用、墙钟时间 |
| 可靠读取 | 已完成 | 三来源并行、超时、有限重试、Provider 独立熔断、部分成功 |
| 安全写入 | 已完成 | HITL、签名 Action Token、写前复核、差异展示、幂等草稿 |
| 状态与持久化 | 已完成首片 | JSON/PostgreSQL Repository、Run 版本、乐观锁、消息和 Trace |
| 长期记忆 | 已完成存储首片 | Profile/Team/Episodic/Semantic 模型、授权 CRUD 和审计 |
| Agent 上下文 | 已完成 M2.1 | ContextPolicy、权限过滤、双 Agent 投影、冲突处理、token 裁剪和 Trace |
| 结构化记忆 | 已完成 M2.2 | 受控 Key/Category/Tag Registry、Schema、有效期、Consent、TEAM 写权限、版本替代 |
| 记忆候选 | 已完成 M2.3 | 安全终态提取、PII/凭证/临时需求过滤、接受/拒绝/过期、幂等写入与 UI |
| 制度知识 FTS | 已完成最小纵向切片 | 独立文档/版本/条款，管理员发布，tenant/category/effective 过滤，引用与规则版本一致性校验 |
| 自动测试 | 已完成当前基线 | 109 个后端测试和 46 个确定性评测通过 |

## 3. 还没有完成

- Requirement Agent 目前主要判断缺失字段；复杂自然语言实体抽取仍由简单规则承担。
- Sourcing Agent 使用 fixture provider；尚未接真实企业目录或商城 API。
- 候选提取当前是保守的确定性规则，只覆盖 Registry 中已登记的笔记本偏好；更复杂表达可在后续接入受 Schema 约束的模型提取器，但不能改变确认边界。
- Registry 当前来自版本控制 YAML，尚未从真实 PIM/ERP 主数据同步；Tag 暂存为正式 ID 数组，未拆关联表。
- 制度结论仍来自版本化 Policy-as-Code；FTS 已提供条款引用，但尚未加入向量召回、RRF、rerank 和真实制度标注集。
- PostgreSQL 按请求边界保存 Run；尚未把每个 Agent execution/checkpoint 做成可租约恢复的记录。
- 两 Agent 新拓扑已完成 DeepSeek 12-case × 1 smoke（12/12），尚未执行 ×3 稳定性门禁。
- “Agent 进化”只有评测和报告基础，还没有形成提案、离线验证、人工审批、canary、回滚闭环。

## 4. 接下来按什么顺序做

### M1：完成双 Agent 的真实模型验收（smoke 已完成，稳定性门禁待执行）

目标：证明代码中的两个角色在 DeepSeek 下真实执行，且费用与失败行为可控。

任务：

1. 已将本地配置显式改为 `PROCURE_AGENT_TOPOLOGY=two_agent`；
2. 已运行 12-case × 1 smoke，12/12 通过；
3. 待运行 12-case × 3 稳定性门禁；
4. 记录成功率、p95 延迟、请求/token/费用、修复输出次数和未授权写入；
5. 增加“只创建两个 Agent”“Sourcing 无预算/政策工具”的 live 断言。

完成标准：36 个 Run 全部满足业务状态预期，未授权写入为 0，陈旧/越权结果为 0，失败时能安全回退。

### M2.1：把数据库与长期记忆接入 Agent 上下文（已完成）

目标：不是“数据库里有记忆表”，而是每次 Run 能按权限取到正确的最小上下文。

完整的数据模型、权限、业务流程、ContextAssembler 和伪代码见[记忆、上下文与知识检索模块技术设计](12-memory-context-technical-design.md)。

已完成：

1. 建立版本化 `ContextPolicy v1` 和唯一上下文入口 `ContextAssembler`；
2. 复用 Repository 的 tenant/user/team/status 可见性边界；
3. Requirement/Sourcing 使用不同 Key、Scope、条数和消息投影；
4. 本次明确输入覆盖记忆；团队硬标准优先，个人软偏好优先；同 Key 只保留一个结果；
5. 使用 Agent 上限和 Harness 剩余输入 token 的较小值，整条装箱、不截断正文；
6. `AgentTaskEnvelope` 已携带最小 Context，Trace 只记录元数据和排除原因；
7. 已覆盖跨租户、跨用户、Agent 投影、冲突、token 裁剪和 Engine 集成测试。

M2.2 已补上 `effective_at/expires_at`，Repository 和 ContextAssembler 现在都会排除未生效或已过期记录。

### M2.2：Registry 与结构化记忆（已完成）

目标：停止依赖用户自由填写 Key，让记忆值可校验、可去重、可设置有效期。

已完成：

1. 建立版本化 `memory_registry.yaml`，定义 Key Schema、Scope、品类、Agent、敏感度、合并策略和 TTL；
2. 建立稳定 Category ID、多语言名称与别名；歧义别名明确拒绝；
3. Tag code/别名统一转换成正式 Tag ID，未知 Tag 拒绝；
4. 记忆增加 `value_json/category_id/key_definition_version/tags/effective_at/expires_at/confidence/metadata`；
5. 新增 `memory_consents`，Consent 与记忆在同一事务写入；`request_id + command_fingerprint` 保证安全幂等；
6. 普通员工不能写 TEAM，只有受信角色可写；普通 API 继续禁止 SEMANTIC；
7. 相同用户/Scope/Key/Category 的新确认记录替代旧记录，旧记录标记 `SUPERSEDED`；
8. ContextAssembler 优先读取 Registry 的 Agent/品类规则，旧字符串前缀只保留为历史数据兼容路径；
9. 新增 Registry、Schema、Tag、歧义、Consent、幂等、TEAM 权限、版本替代和有效期测试。

为什么 Registry 放在版本控制而不是让用户在线维护，见 [ADR-0011](adr/0011-controlled-memory-registry.md)。

### M2.3：记忆候选闭环（已完成）

目标：用户不必手工填写 Key 和 JSON；系统从安全终态 Run 提出候选，但只有用户看过并接受后才进入长期记忆。

已完成：

1. 只在 `DRAFT_CREATED` 后读取用户消息，要求“以后、默认、记住”等长期意图；“这次”需求和助手消息不提取；
2. 生成受 Registry Schema、Scope、Category 和 Tag 约束的 `MemoryCandidate`，候选不进入 Agent Context；
3. 凭证、邮箱、手机号、银行卡等内容被拒绝，实时预算和价格没有可保存的候选 Key；
4. 精确重复已有记忆会被抑制，变更会标记 `REPLACES_EXISTING`；接受时若目标记忆已变化，会拒绝旧候选并要求重新生成；
5. 提供候选查询、重新提取、接受和拒绝 API；14 天未处理自动标记 `EXPIRED`；
6. 接受复用 M2.2 Consent、幂等和版本替代链路，Run Trace 只记录候选元数据；
7. 前端展示可选确认卡，明确“不接受就不会保存”；
8. SQLite 自动化回归和真实 PostgreSQL 17 migration `20260908_0004` 已通过。

### M3：接入制度 RAG（FTS 首片已完成）

目标：RAG 负责“找到并引用适用条款”，Policy Service 仍负责最终决定。

流程：

```text
制度文档 -> 解析/切块 -> tenant + 生效日期 + 版本元数据
         -> FTS + vector 混合检索 -> rerank
         -> 返回条款与引用 -> Policy Rule Service 计算最终决定
```

RAG 不直接成为第三个 Agent。它是 Policy Service 的只读检索能力。若检索不到有效条款，系统返回 `NEEDS_REVIEW` 或安全停止，不能让模型补写制度。

已完成：原文哈希、结构切块、文档版本/生效期、治理角色导入和发布、PostgreSQL GIN FTS、tenant/category/effective 过滤、条款引用、Engine 推荐/确认前版本复核，以及无答案/无有效版本/版本冲突安全停止。Alembic `20260909_0005` 已在 PostgreSQL 17 验证。

下一切片：增加 pgvector embedding、FTS/vector 各自 Top-K、RRF 融合、可选 rerank，并建立带相关/不相关条款标注的检索评测集。规则结论继续用固定输入完全复现。

### M4：持久化执行与检查点

目标：进程退出后能从最后一个安全步骤恢复，而不是重新执行整个 Run。

增加：

- `agent_executions`：角色、task、state_version、状态、模型、token、费用；
- `tool_calls`：参数指纹、风险、尝试次数、结果引用；
- `checkpoints`：状态快照、待办动作、租约和过期时间；
- Outbox：草稿/通知事件与数据库事务一起提交；
- 跨进程幂等和 reservation 回收。

### M5：Agent 评测与受控进化

目标：让 Agent 改进依靠失败数据，而不是手工感觉。

闭环：

```text
生产/模拟 Trace
  -> 脱敏失败聚类
  -> 生成 Prompt/工具描述/路由规则候选
  -> 固定回归集 + holdout 离线评测
  -> 人工审批
  -> 小流量 canary
  -> 指标恶化自动回滚
```

模型不能自行修改生产 Prompt、工具权限、制度或代码。所谓“Agent 进化”是一个受版本、评测和审批控制的软件发布过程。

## 5. 面试时如何解释这套设计

可以按下面四句话回答：

1. “我没有按函数数量拆 Agent，而是按语义不确定性拆，只保留需求理解和多目标寻源两个角色。”
2. “总价、预算、政策、状态和写入都有唯一可验证答案，所以由确定性服务执行。”
3. “Agent 只有最小只读工具权限；它返回的是建议，Engine 会用本 Run 的事实和证据再次否决或接受。”
4. “Harness 管理版本、预算、超时、重试、熔断、Trace、人工确认和幂等，所以模型出错不会扩大成业务副作用。”

## 6. 最近的一个具体开发任务

下一次开发应完成 **M3.2 pgvector—FTS/vector 双路召回—RRF—检索评测集**。制度知识继续使用独立知识库，不能塞进用户记忆；Policy-as-Code 仍负责最终允许、拒绝或人工复核。DeepSeek ×3 稳定性门禁在发布前补齐，但不阻塞本地检索开发。
