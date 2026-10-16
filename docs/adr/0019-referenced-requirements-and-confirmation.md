# ADR 0019：需求参考工具与寻源前确认

日期：2026-10-16。状态：已实施。

## 决策

Requirement Agent 在需求理解阶段开放 query_procurement_policy、query_purchase_history 两个只读工具，使用现有 Gateway/Harness 权限、预算和执行日志。ROUTE_MESSAGE 与 INTENT 阶段仍无工具，Sourcing 不获得这两个权限。Requirement 阶段最多四次模型请求、四次工具调用；费用、token、全局调用计数不放宽。

用户明确要求按制度／公司或团队标准、参考历史订单时建立带原文授权的 ReferenceRequest。模型调用工具并输出 reference_bindings，绑定已返回的证据 ID 与字段；服务端验证绑定，保留用户明确值并补齐必要查询。离线、fixture、模型失败与漏调工具均进入相同的来源校验及需求确认关卡。

制度读取复用当前 PolicyKnowledgeService。启用治理知识后，缺失、过期、版本不一致均阻断。未启用治理知识的演示租户使用执行制度 YAML 中明确规定的底线；目前是内存、保修及成色，没有 1TB 存储默认值。查询结果携带版本、生效时间、条款引用、结构化事实与来源摘要，不根据不完整文本猜测制度值。

历史订单通过 PurchaseHistoryReadPort 提供；首轮为 FixturePurchaseHistoryAdapter 模拟数据，独立于采购草稿。身份及部门由认证上下文决定，默认仅本人可见；采购专员可读授权部门。仅返回 APPROVED/FULFILLED，排除取消与未提交草稿。支持明确订单引用、最近一单、指定年份／去年；结果上限三条。无法唯一定位时等待用户选择。

RequirementResolutionDraft 与生效 RequirementSpec 分开。外部条件先成为 PROPOSED_REFERENCE；确认后才转为 CONFIRMED_REFERENCE 并应用需求版本。外部 RequirementEvidence 使用 POLICY/HISTORY 类型，保存真实查询引用与来源摘录，不伪造用户原文位置。模型只能原样携带已确认外部证据，不能新增伪造来源或改写其对应值。新用户数值需与引用的原文数值对应。

合并次序为本轮用户要求优先于历史建议，企业强制底线始终生效。历史配置不足可提出制度升级；与用户明确上限、成色等冲突时阻断并说明。历史预算可供确认复用，历史成交价、库存、实际到货日期和供应商旧承诺不作为本轮事实。未规定的必要字段集中追问。

复用 WAITING_USER，新增 HISTORY_SELECTION、REQUIREMENT_CONFIRMATION、REFERENCE_UNAVAILABLE、REFERENCE_INFORMATION。确认接口为 POST /api/runs/{run_id}/requirement-confirmations，绑定 request_id、draft_id、draft_version、content_hash。确认前重新读取来源、检查现行性及访问范围。需求完整性、版本及来源确认同时成立后，才直接应用已审核草案并恢复寻源，不再让模型猜测一次。

寻源入口和目录工具都有确认门禁。无效／陈旧确认在消费等待检查点前拒绝。已授权但中断的确认按消息 ID 及回执恢复；来源在接受前变化时作废原回执并重新挂起审核。重复确认不重复应用。普通消息附带可选草案 ID 和版本，防止旧历史选择或旧审核视图应用到新草案。

已确认草案保存制度和历史查询快照、来源摘要和确认记录；后续普通需求更新不删除这些归档。当前草案、回执及归档均使用现有 Run JSON/SQL 文档持久化，无新增数据库表迁移。

## 后果

- 需求确认只授权开始寻源，采购草稿仍使用独立 /confirm 和原有写前复核。
- 纯查询可以只回答制度／历史信息，不应用外部需求；缺项、来源不可用及歧义不会自动进入寻源。
- 前端展示历史选项、中文需求字段、来源、差异、缺项与独立需求确认按钮；刷新后恢复待处理的需求确认。
- 历史数据和企业制度是演示数据；未对接 ERP，未进行付费真实模型评测。工具选择的语言泛化质量仍需后续 live 评测。
