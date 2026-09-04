# P0 需求追踪矩阵

更新时间：2026-09-04。

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
| IAM-02 | IMPLEMENTED | Run 读取和确认校验 tenant/user；有跨租户测试 | 错误差异仍可能泄漏 run 是否存在；没有统一 tenant-scoped repository |
| IAM-03 | VERIFIED | 版本化 Tool Registry 按角色和风险返回可见工具，未知工具默认拒绝；测试证明没有审批、下单和支付工具 | 后续 Agents SDK function tools 必须复用同一 Registry |
| REQ-01 | IMPLEMENTED | Pydantic Requirement 与中文规则抽取 | 品类和币种基本固定；缺完整 JSON Schema/领域 invariant 场景 |
| REQ-02 | VERIFIED | 多轮增量合并保留已有字段，并记录存储默认 assumption | 继续保持纠正与覆盖测试 |
| REQ-03 | IMPLEMENTED | 显式信息价值对预算、数量、规格、交付地和税口径排序，场景验证高价值字段优先 | 仍需扩展标注集并计算 required-question recall 与 unnecessary-question rate |
| REQ-04 | VERIFIED | 根据剩余字段和剩余轮数选择最少问题，默认少问且任何一轮不超过三个 | 无 |
| REQ-05 | VERIFIED | 用户授权后只应用有制度依据的公司标准并记录 assumption；三轮后对不可安全假设字段终止 | 无 |
| SRC-01 | IMPLEMENTED | 三个 provider 通过 asyncio task 并发执行 | 缺带真实延迟的并发耗时断言 |
| SRC-02 | VERIFIED | Provider 先返回 `ProviderRawOffer`，Adapter 统一产出 `CatalogSearchResult`；三个 Provider 参数化 contract tests 校验 schema、证据和只返回已验证 Offer | Live Adapter 仍需在 P1 单独验证 |
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
| GOV-02 | IMPLEMENTED | Policy ID/version 作为 evidence ref | 缺生效时间和逐结论证据对象 |
| GOV-03 | VERIFIED | 确定性 rule evaluator 产生最终政策决定 | 无 |
| GOV-04 | IMPLEMENTED | 支持供应商准入和发票能力 | 缺地区状态和对应测试 |
| GOV-05 | IMPLEMENTED | 金额阈值和供应商例外改变审批路径 | 缺审批路由版本 |
| REC-01 | VERIFIED | 输出最低成本、最快交付、综合最优 | 无 |
| REC-02 | IMPLEMENTED | Candidate 同时保存原始报价、match、policy 和 budget evidence refs；不可比报价展示匹配证据与原因 | 仍缺价格/交付/规格/预算/制度逐事实映射和完整证据抽屉 |
| REC-03 | IMPLEMENTED | 有 policy reasons 和 warnings | UI/总结没有完整展示三类候选 trade-off 与未验证信息 |
| REC-04 | VERIFIED | 规格、预算、condition、Policy 硬约束先过滤后排序 | 无 |
| HITL-01 | VERIFIED | 未确认的 BUSINESS_WRITE 被拒绝 | 无 |
| HITL-02 | VERIFIED | Token 绑定 tenant/user/run/需求版本/动作/报价 fingerprint/过期时间；覆盖旧需求版本、旧报价、跨用户和错误动作 | 无 |
| HITL-03 | IMPLEMENTED | 确认时重新读取报价并刷新身份、预算、制度和供应商；报价变化返回 Diff 并要求重确认 | 尚未为预算、制度和供应商快照建立变化 Diff |
| HITL-04 | VERIFIED | 业务幂等键与重复确认测试 | 本轮补同进程并发确认；跨进程唯一约束留给 PostgreSQL |
| HITL-05 | VERIFIED | API/UI 明确 `DRAFT_NOT_SUBMITTED` | 无 |
| OPS-01 | IMPLEMENTED | 显式迁移表是唯一状态写入口；state_version 与 state_events 连续校验并可经 JSON 恢复 | 缺 pause/resume/cancel、独立 checkpoint 和数据库乐观锁 |
| OPS-02 | IMPLEMENTED | Run 固化 state/agent/tool/provider/retry/token/cost/time 额度与用量；动作前扣减，超限保留当前状态和 Trace；有单元与场景测试 | OpenAI Planner 尚未回传真实 token/cost；局部业务结果还需 checkpoint 化 |
| OPS-03 | IMPLEMENTED | Tool Policy 声明 timeout/attempt/backoff/circuit；异步 Provider 调用执行有限重试、按来源熔断和并发隔离 | 同步进程内工具尚未模拟外部超时；缺 schema drift 和多进程共享熔断 |
| OPS-04 | IMPLEMENTED | Trace 关联 actor、工具、重试、预算和状态版本；独立 state_events 保存迁移原因 | 缺 prompt/model/policy/ranking 完整版本指纹和逐事实链路 |
| OPS-05 | IMPLEMENTED | 固定 Fixture 两次运行投影一致 | 缺模型、工具、clock 和版本指纹 cassette |

## 3. P0 Demo 与交付门禁

| 项目 | 状态 | 说明 |
|---|---|---|
| 正常采购闭环 | VERIFIED | 主动追问、寻源、制度校验、推荐、确认、草稿可运行 |
| 越权/忽略制度 | IMPLEMENTED | 服务端确定性规则存在；缺 Tool Registry 演示 |
| 网页 Prompt Injection | VERIFIED | Fixture 中包含注入文本，不能触发写入 |
| 多来源失败与草稿超时恢复 | IMPLEMENTED | 仅单来源 429/timeout 部分成功 |
| 价格变化与旧卡片重放 | VERIFIED | `quote_price_changed` 场景和自动化测试覆盖变化、二次确认、旧 Token 拒绝 |
| 26 个版本化场景 | VERIFIED | 在原 23 条基础上增加合法状态路径、预算拦截和重试/熔断 3 条场景；仍不等同完整 P0 覆盖 |
| 完整 trajectory cassette | PLANNED | 当前只比较确定性结果投影 |
| CI veto gate | VERIFIED | 独立 workflow 执行 Ruff、pytest、scenario eval 和前端构建，并上传评测报告 |
| 质量/安全/性能/成本报告 | IMPLEMENTED | 缺真实模型 token、成本和完整 trajectory 指标 |

## 4. 当前发布结论

当前版本可以描述为：

> Phase 1 核心纵向闭环已实现，并通过 26 个 deterministic fixture 基线场景；状态机和最小执行预算切片已有自动化证据，完整 P0 仍在补齐。
