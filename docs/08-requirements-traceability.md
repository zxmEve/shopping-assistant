# P0 需求追踪矩阵

更新时间：2026-09-09。

本文档是 `03-product-requirements.md` 与实际代码、测试、演示之间的追踪入口。状态只表示当前仓库中的事实，不表示路线图目标。

## 1. 状态定义

| 状态 | 定义 |
|---|---|
| `PLANNED` | 需求已确认，但没有可运行实现 |
| `IMPLEMENTING` | 正在实现，接口或行为可能变化 |
| `IMPLEMENTED` | 已有可运行代码，但验收标准尚未被完整验证 |
| `VERIFIED` | 实现存在，且自动化测试或明确人工验收覆盖需求中的验收标准 |
| `BLOCKED` | 存在明确外部阻塞，无法继续验证 |

规则：实现文件、测试证据、Demo 证据三者中，任一验收所需证据缺失时不得标记为 `VERIFIED`。

## 2. P0 功能需求

| ID | 状态 | 当前证据 | 剩余验收缺口 |
|---|---|---|---|
| IAM-01 | VERIFIED | 服务端 Fixture Session 将 opaque token 映射到 tenant/user，企业 Gateway 解析组织信息；测试证明伪造身份/成本中心 Header 不生效 | 生产环境仍需替换为 OIDC/SSO adapter |
| IAM-02 | VERIFIED | Run Repository 的 SQL 查询强制 tenant/user 条件；真实 PostgreSQL 与自动化测试证明跨租户得到同一种拒绝结果 | 生产环境后续增加 PostgreSQL RLS 作为第二道边界 |
| IAM-03 | VERIFIED | 版本化 Tool Registry 同时控制 API 和两个 Agent 的 function tools；未知工具默认拒绝，Agent 工具中没有草稿、审批、下单和支付 | 无；后续新增工具必须继续复用 Registry |
| REQ-01 | IMPLEMENTED | Pydantic Requirement 与中文规则抽取 | 品类和币种基本固定；缺完整 JSON Schema/领域 invariant 场景 |
| REQ-02 | VERIFIED | 多轮增量合并保留已有字段；每轮 Planner 接收当前 Run 的完整用户/助手消息并记录 message_count | 真实模型仍需验证长对话质量与压缩策略 |
| REQ-03 | IMPLEMENTED | 显式信息价值对预算、数量、规格、交付地和税口径排序，场景验证高价值字段优先 | 仍需扩展标注集并计算 required-question recall 与 unnecessary-question rate |
| REQ-04 | VERIFIED | 根据剩余字段和剩余轮数选择最少问题，默认少问且任何一轮不超过三个 | 无 |
| REQ-05 | VERIFIED | 用户授权后只应用有制度依据的公司标准并记录 assumption；三轮后对不可安全假设字段终止 | 无 |
| SRC-01 | IMPLEMENTED | 三个 provider 通过 asyncio task 并发执行 | 缺带真实延迟的并发耗时断言 |
| SRC-02 | VERIFIED | Provider 先返回 `ProviderRawOffer`，Adapter 统一产出 `CatalogSearchResult`；Agent `search_catalog` 只使用服务端需求并缓存可信 Offer，Engine 复用已查结果 | Live Adapter 仍需单独验证 |
| SRC-03 | VERIFIED | Evidence 强制来源、时间、有效期和 content hash | 后续增加过期证据拒绝测试 |
| SRC-04 | IMPLEMENTED | 429/timeout 可进入 Harness 有限重试，单来源熔断后保留其他来源结果；有场景和单元测试 | 缺 schema drift 和真实 Provider 合同测试 |
| CAT-01 | VERIFIED | Raw Offer 经字段/标题解析进入 Product、Variant、Offer；测试证明 3 种 Provider 格式归一到同一身份，RAM/SSD 差异产生不同 Variant | 当前只覆盖 P0 笔记本规则；开放世界召回属于 CAT-05 |
| CAT-02 | VERIFIED | `ProductCondition` 与 `BundleType` 进入 Variant 身份；翻新与新品、单机与扩展坞套装测试为不同 Variant，UI 显式展示 | 二手解析已实现，扩大标注集留给 CAT-05 |
| CAT-03 | VERIFIED | `MatchDecision` 输出置信度、匹配/冲突字段、原因和 evidence refs；`REVIEW_REQUIRED` 与低于 0.90 的报价不能生成 Offer，UI 展示不可比原因 | 阈值仅为 Fixture 基线，需在真实标注集校准 |
| CAT-04 | VERIFIED | 确定性 Match Guard 对 RAM、SSD、系统、成色、套装、型号后缀和错误上游 Variant 提议执行服务端 veto；hard false merge grader 为 0 | 新品类别与新 Provider 仍需扩规则/对抗集 |
| COST-01 | VERIFIED | Decimal 成本计算与 exact-match 测试 | 无 |
| COST-02 | IMPLEMENTED | API 区分税、运费、条件优惠和无条件总价 | UI 未完整展示明细、条件与风险 |
| COST-03 | IMPLEMENTED | 代码支持含税/未税计算 | 缺同一报价双口径 golden test |
| GOV-01 | VERIFIED | 预算由企业 Gateway 查询，Agent 无法覆盖 | 无 |
| GOV-02 | IMPLEMENTED | Policy ID/version 作为规则 evidence ref；独立制度文档按版本、生效期和条款切块，FTS 引用通过版本一致性检查后进入 Candidate evidence | 缺逐结论引用映射、向量/RRF 检索和真实制度引用准确率标注集 |
| GOV-03 | VERIFIED | 确定性 rule evaluator 产生最终政策决定 | 无 |
| GOV-04 | IMPLEMENTED | 支持供应商准入和发票能力 | 缺地区状态和对应测试 |
| GOV-05 | IMPLEMENTED | 金额阈值和供应商例外改变审批路径 | 缺审批路由版本 |
| REC-01 | VERIFIED | 输出最低成本、最快交付、综合最优；Agent 可提交结构化候选，服务端验证 Offer 与证据后才允许改变综合最优 | 无 |
| REC-02 | IMPLEMENTED | Candidate 同时保存原始报价、match、policy 和 budget evidence refs；Agent 候选必须引用目录、预算与制度证据，虚构候选会被 veto；不可比报价展示匹配证据与原因 | 仍缺价格/交付/规格/预算/制度逐事实映射和完整证据抽屉 |
| REC-03 | IMPLEMENTED | 有 policy reasons 和 warnings | UI/总结没有完整展示三类候选 trade-off 与未验证信息 |
| REC-04 | VERIFIED | 规格、预算、condition、Policy 硬约束先过滤后排序 | 无 |
| HITL-01 | VERIFIED | 未确认的 BUSINESS_WRITE 被拒绝 | 无 |
| HITL-02 | VERIFIED | Token 绑定 tenant/user/run/需求版本/动作/报价 fingerprint/过期时间；覆盖旧需求版本、旧报价、跨用户和错误动作 | 无 |
| HITL-03 | IMPLEMENTED | 确认时重新读取报价并刷新身份、预算、制度和供应商；报价变化返回 Diff 并要求重确认 | 尚未为预算、制度和供应商快照建立变化 Diff |
| HITL-04 | VERIFIED | 业务幂等键、同进程并发确认测试和 PostgreSQL 草稿身份唯一约束 | 外部审批系统仍需提供自己的幂等接口 |
| HITL-05 | VERIFIED | API/UI 明确 `DRAFT_NOT_SUBMITTED` | 无 |
| OPS-01 | IMPLEMENTED | 显式迁移表是唯一状态写入口；state_version 与 state_events 连续校验；PostgreSQL `lock_version` 条件更新拒绝旧版本写入 | 缺 pause/resume/cancel 和独立 checkpoint |
| OPS-02 | IMPLEMENTED | Run 固化 state/agent/tool/provider/retry/token/cost/time 额度与用量；DeepSeek live runner 已验证 token 记账和版本化费用估算 | 费用是公开价格估算而非账单；局部业务结果还需 checkpoint 化 |
| OPS-03 | IMPLEMENTED | Tool Policy 声明 timeout/attempt/backoff/circuit；异步 Provider 调用执行有限重试、按来源熔断和并发隔离 | 同步进程内工具尚未模拟外部超时；缺 schema drift 和多进程共享熔断 |
| OPS-04 | IMPLEMENTED | Trace 关联 actor、工具、重试、预算和状态版本；独立 state_events 保存迁移原因；trajectory grader 检查禁止/遗漏/重复工具、候选依据和停止条件；DeepSeek 3 次 live 轨迹通过 | 缺 prompt/model/policy/ranking 完整版本指纹和更大 live 样本 |
| OPS-05 | IMPLEMENTED | 固定 Fixture 两次运行投影一致 | 缺模型、工具、clock 和版本指纹 cassette |

## 3. P0 Demo 与交付门禁

| 项目 | 状态 | 说明 |
|---|---|---|
| 正常采购闭环 | VERIFIED | 主动追问、寻源、制度校验、推荐、确认、草稿可运行 |
| 越权/忽略制度 | IMPLEMENTED | 服务端确定性规则存在；缺 Tool Registry 演示 |
| 网页 Prompt Injection | VERIFIED | Fixture 中包含注入文本，不能触发写入 |
| 多来源失败与草稿超时恢复 | IMPLEMENTED | 仅单来源 429/timeout 部分成功 |
| 价格变化与旧卡片重放 | VERIFIED | `quote_price_changed` 场景和自动化测试覆盖变化、二次确认、旧 Token 拒绝 |
| 46 个版本化场景 | VERIFIED | Agent Runtime 覆盖工具与轨迹；数据库覆盖回写、租户隔离与乐观锁；长期记忆覆盖个人/团队边界、生命周期和候选确认安全；制度知识覆盖引用、时态/租户过滤、版本冲突和 Engine grounding |
| 制度知识检索与引用 | IMPLEMENTED | 治理角色导入/发布，PostgreSQL GIN FTS，tenant/category/effective 过滤，推荐和确认前 Policy 版本校验，无答案或冲突安全停止 | pgvector/RRF、PDF/DOCX ingestion、逐结论 citation grader 和管理 UI 待完成 |
| 完整 trajectory cassette | PLANNED | 当前只比较确定性结果投影 |
| CI veto gate | VERIFIED | 独立 workflow 执行 Ruff、pytest、scenario eval 和前端构建，并上传评测报告 |
| 质量/安全/性能/成本报告 | IMPLEMENTED | deterministic 与 live 报告隔离；DeepSeek 3/3 live 基线包含成功率、token、延迟、trajectory 和版本化价格估算 | 样本过小，费用尚未与 Provider 账单核对 |

## 4. 当前发布结论

当前版本可以描述为：

> 确定性核心闭环通过 46 个 fixture 场景；双 Agent Runtime 已通过 fixture 测试，能够证明路由、并行、权限、预算、陈旧结果拒绝、回退和完整草稿闭环。双 Agent DeepSeek 12-case × 1 smoke 已完成；历史单总控报告只作为旧架构记录。

长期记忆授权 CRUD、Agent Context、结构化 Registry/Consent、候选确认和制度 FTS 最小纵向切片均已完成；后端回归 109/109，确定性评测 46/46，双 Agent DeepSeek 12-case × 1 smoke 为 12/12。下一步实现 pgvector + RRF 混合检索和检索标注集；发布前补齐双 Agent ×3 稳定性矩阵。
