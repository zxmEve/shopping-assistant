# Agent 业务扩展与集中追问实施方案

日期：2026-10-15。架构继续采用 Requirement Agent、Sourcing Agent、LangGraph、Harness 与确定性领域服务。

## 本次范围

阶段一、二及消息归属路由已完成。本轮继续实施阶段三至六：查询工具、按需事实补查、无候选协商、推荐后对话和恢复／确认，并补齐阶段七的确定性回归。历史记录保留各轮当时的实施边界。真实供应商接入和付费真实模型评测仍为后续工作。

## 目标流程与边界

用户消息 → Requirement Agent 理解需求或本轮意图 → 集中追问或寻源 → 按需补查 → 服务端验证 → 排序推荐或约束协商 → 用户确认 → 写前事实、预算和制度复核 → 采购草稿。

Requirement Agent 负责语义、硬软分类、追问与需求变更，不读取商品事实。Sourcing Agent 负责工具选择、证据缺口、排序解释和协商表达。金额、制度、事实校验、权限与写入由领域服务和 Harness 执行，LangGraph 管理阶段和恢复。

## 阶段一 契约与状态基础

- 在需求契约中增加字段解决状态：UNRESOLVED、EXPLICIT、NO_PREFERENCE、AUTHORIZED_DEFAULT；分别表达缺失、明确值、无额外要求、标准授权。
- 增加 ClarificationPlan，保存问题集合、覆盖字段和冲突引用。保留 next_question 兼容旧快照及调用方。
- 增加后续阶段需要的只读数据契约：适用范围和有效期明确的事实证据、逐项候选评估、版本化协商方案、TurnDecision、推荐版本及展示项映射。
- Run 增加默认空或可选的基础字段与等待原因；旧快照无新字段时仍可读取。新增类型不自动启用工具、协商分支或推荐后消息入口。
- 延续状态历史连续性、RequirementSpec/Delta 版本与用户证据校验。未来阶段的状态边在对应功能实施时才开放。

主要模块：domain/requirements.py、domain/models.py、新建 domain/sourcing.py、domain/conversation.py、workflow.py。增加契约、旧快照和状态回归测试。

## 阶段二 集中追问

模型一次输出当前可识别的全部必要问题，允许按意图分组。品类不明时先澄清品类；品类明确后补齐适用品类字段。用户可部分回答，后续只问剩余项。

服务端验证问题覆盖、重复字段、已明确字段、冲突与无依据的断言；合格问题保留模型措辞，遗漏或不合格部分使用模板补齐，不再无条件覆盖为固定 Top-1。

预算、数量、税口径和交付地等必要事实不得静默假设。配置字段允许明确的「无要求」，不等于授权模型猜测参数。企业最低标准仍由制度服务强制。只有用户明确授权时才能采用公司或团队标准；标准值与授权来源要区分。

无要求配置需要同步支持可选的目录过滤参数，移除内存、存储必须为数值的工具和 Engine 前置断言。统一 fixture、模型、fallback 和 legacy 的完整性规则。

前端展示问题清单、已识别需求和回答提示，支持自由文本逐项或一次回答；不启用推荐后的输入。

主要模块：services/requirement_validation.py、services/engine.py、agents/provider_adapter.py、agents/multi_agent.py、agents/tools.py、gateway/tool_gateway.py、fixtures/catalog.py、frontend/src/App.tsx 与 types.ts。

验收：全部缺项覆盖、部分回答不重复、完整输入零追问、明确无要求可寻源、授权标准有来源、硬软不混淆、冲突解决、旧快照兼容、模型问题校验与局部修复、工作流中断恢复和草稿确认回归。

## 阶段三 三个查询工具

新增 query_inventory、estimate_delivery、query_warranty。参数绑定来源、报价和服务端需求；结果含 evidence_id、fetched_at、expires_at、source_ref、适用参数及 KNOWN/UNKNOWN/UNAVAILABLE 状态。只允许查询当前 Run 召回的报价。

把目录召回与资格过滤分开，保留库存不足和规格不符的近似候选及证据。Gateway、Harness 注册工具、超时、重试、权限、预算与模拟接口。

## 阶段四 按需补查

EvidenceRequirementPolicy 根据用户条件及企业制度计算必要证据。模型决定先查哪个候选、调用哪些工具；服务端复核必要证据，不足则有限补查，无进展即停止。有效证据复用，过期事实新建查询执行；中断恢复复用原执行。

首轮仍覆盖三个目录来源。扩展任务级及 Run 级调用预算，阶段结果持久化，预留最终确认额度。排序视图包含完整规格、库存、交期及保修事实。

## 阶段五 无候选约束协商

候选逐项保存 PASS/FAIL/UNKNOWN、要求值、实际值、来源和可否调整。明确无可行候选、证据不足和制度阻断分别处理。

ConstraintNegotiationService 在需求副本上枚举有限调整，重新计算并验证所有未修改条件及制度；Sourcing Agent 只对已验证方案排序和解释，最多展示三个方案。可调整用户预算、交期和用户自定义配置下限，不得降低企业底线。

方案绑定需求版本、具体变更、证据和有效期。接受方案只更新需求并重新寻源，不等于确认创建草稿。接受证据保存用户动作和方案引用，不伪造参数原文引用。

复用 WAITING_USER，等待原因 CONSTRAINT_NEGOTIATION，届时再开放 SOURCING/REVIEWING → WAITING_USER → COLLECTING 状态边。协商轮次与追问轮次分开。

## 阶段六 推荐后对话

本轮意图区分 EXPLAIN、COMPARE、UPDATE_PREFERENCE、UPDATE_REQUIREMENT、ACCEPT_NEGOTIATION。解释不修改需求；偏好变化重排并按需补查；硬条件变化重新验证资格和事实。模糊指代定向追问。

新增已处理消息标识，不依赖 Spec.source_message_id 判断所有消息。按数量、地点、交期及配置依赖复用证据。推荐版本与展示项映射解决「第二款」指代。令牌绑定推荐版本、候选与证据摘要，变更后拒绝旧令牌，写前刷新事实。

调整确定性评分，使受限的偏好配置与 regret 基线一致。扩展 /messages 请求的请求 ID、会话版本、推荐版本和方案引用，保留 /confirm 作为显式草稿确认。

## 阶段七 评测与发布

每阶段补测试并执行现有回归。更新 trajectory 工具白名单与参数化调用指纹，允许合理工具顺序和过期刷新。先做确定性与故障恢复评测，再做付费真实模型评测。

指标：必要条件遗漏率、重复追问率、必要工具漏调率、无效调用数、证据完整率、协商可行率、偏好满足度、延迟与成本。

## 实施顺序

1. 契约与状态基础、旧快照兼容。
2. 集中追问与无要求配置闭环、前端展示。
3. 三个只读查询工具及模拟适配器。
4. 证据缺口、按需补查、缓存刷新及恢复。
5. 候选淘汰分析与约束协商。
6. 推荐后的解释、调整与令牌更新。
7. 完整回归、真实模型评测与文档对齐。

## 验证与记录

### 2026-10-15 阶段一和阶段二完成记录

阶段一已完成：字段解决状态、问题计划、未来寻源事实／候选评估／协商选项／对话意图的被动契约，以及 Run 的等待原因、推荐版本和展示项映射预留字段。旧 Run 和单问题 RequirementSpec 可读取；原状态历史、确认权限及未开放的状态边保持原有约束。

阶段二已完成：一次覆盖当前全部阻塞项，保留合格模型问题并局部修复遗漏；支持问题分组、部分回答、完整输入零追问、未知品类先澄清、配置明确无要求、公司标准授权及多轮冲突解决。前端显示已记录需求和结构化问题，推荐后的输入仍关闭。

离线、fixture、模型失败 fallback 和 legacy 需求处理已统一。不再因为「开发」语境静默补存储。明确无要求可以使用可选目录参数，但实际未回答仍被工具前置校验拦截；企业内存底线在目录截断前应用，制度服务继续最终复核。新增软偏好不覆盖先前硬约束，多条兼容下限取最严格值。

字段状态使用封闭注册表，JSON 保留 budget.maximum 等路径，已通过 Agents SDK 原生严格输出 schema 的离线构造校验。保留 next_question 作为兼容视图。默认配置分别记录用户授权证据和服务端标准来源；当前 1TB 默认配置不属于制度强制存储下限。

验证结果：

- 后端 Ruff 检查通过。
- 后端 pytest：181 passed，1 skipped；跳过项为未设置 TEST_REDIS_URL 的 Redis 集成测试。
- 新增 test_clarification_v2.py：25 项测试通过，覆盖问题保留／修复、无要求安全边界、增量硬软约束、旧快照、SQL 与 SQLite checkpoint 重建、API 和草稿确认回归。
- 确定性业务评测：51/51 通过；集中追问评测描述与断言已同步，报告由测试重新生成。
- 前端 ESLint、TypeScript 与 Vite 生产构建通过。
- git diff --check 通过。

本轮未运行付费真实模型评测，未宣称模型语言效果已经验证。自由文本问题校验采用字段、冲突和关键词门禁，仍需后续 live 数据验证措辞质量。当前目录继续只支持笔记本。

阶段三至六尚未启用：没有新增库存／交期／保修工具，没有按需补查循环，没有约束协商业务路由，没有推荐后对话与新确认令牌逻辑；对应新增类型仅用于后续阶段的数据契约。后续阶段的状态迁移边与工具授权仍未开放。


### 2026-10-16 消息归属路由扩展

消息在合并 RequirementSpec 前先判断 CONTINUE、NEW 或 CLARIFY。CONTINUE 保留当前 Run 和已确认条件；NEW 使用独立 Run、独立需求版本和预算，不继承旧需求字段、消息历史、候选、确认令牌或追问轮次。旧采购保留。CLARIFY 只保存待路由消息，不修改采购条件和业务状态，用户选择后处理原始消息。

真实模型模式复用 Requirement Agent 的模型与零工具权限，新增 ROUTE_MESSAGE 阶段而非第三个 Agent。输入为当前需求及最近六条消息；输出仅 route、confidence、reason。低置信度进入澄清；模型失败采用保守规则，额度不足时请用户明确选择。明确的否定重开、字段修改、品类回答和新任务声明由服务端守卫保护。规则与真实模型质量仍须分别评测，离线 fixture 不代表真实模型效果。

接口沿用 POST /api/runs/{run_id}/messages，新增可选 request_id 与 route_choice（CONTINUE/NEW）。选择只允许匹配的待澄清请求，且需校验原需求和状态版本。携带同一个 request_id 重试不会重复创建采购；不同输入复用 ID、陈旧选择及改投另一目标会被拒绝。前端使用稳定请求 ID，支持选择按钮和简短文字确认；原始待路由消息保留，避免仅处理「新需求」三个字而丢失原来的数量等信息。

路由回执及服务端预分配的目标 Run ID 持久化到 Run JSON；模型路由单独记录 AgentExecution，并使用现有 Model Gateway 与 Harness 计费、限额和审计。分类缓存绑定请求 ID、状态版本及需求版本；中断后依据已接受消息 ID 和 checkpoint 恢复，不能仅凭相同正文判断已处理。授权始终先于分类与目标分发，目标 Run 读取再次校验租户和所有者。

WAITING_CONFIRMATION 现在可以接收新的需求消息，经过同一 Requirement Agent／Validator 后重新寻源；接收更新时清除旧候选、报价快照和确认令牌，重新校验后再签发令牌。已生成草稿和失败任务不直接修改；可以从它们建立新的采购。此扩展只支持需求归属及条件更新，未实现基于证据的推荐解释、偏好重排缓存、库存／交期／保修工具或约束协商。

为覆盖新增路由调用和再次寻源，新 Run 默认 agent_turns=16、specialist_requests=12、stage_model_requests=14、workflow_read_tool_calls=12；总费用、token、总工具数和 Provider 限额不放宽。已保存 Run 的预算与用户显式环境配置不自动覆盖，达到限额仍停止。

路由回归覆盖：同品类独立采购、跨品类采购、补充与修改、否定重开、模糊表达、模型判断／低置信度／失败、旧条件隔离、跨租户拒绝、陈旧选择、重复请求、确认选择后的中断重试、子任务创建中断恢复、SQL 与 SQLite checkpoint 重建，以及旧确认令牌失效。


本次验证：新增消息路由测试 37 项通过；完整后端 218 passed、1 skipped（未配置 TEST_REDIS_URL），确定性评测 51/51，Ruff、前端 ESLint、TypeScript 和 Vite 生产构建、git diff --check 均通过。未运行付费真实模型评测。


### 2026-10-16 阶段三至六实施记录

查询工具与事实层已启用。Sourcing Agent 新增库存／交期／保修查询权限，模型自主决定工具及查询顺序；EvidenceRequirementPolicy 和 SourcingEvidenceService 对最多六个最终候选补足必需证据。库存、企业保修必须确认；存在交期诉求或交付偏好时补查交期。所有查询经 Gateway/Harness，记录来源、需求范围、状态、查询及过期时间。工具结果和已完成日志均执行 TTL 检查；确认尝试编号用于强制刷新与中断恢复。

目录保留近似候选，服务端执行用户条件、库存、交期、保修、规格及企业制度校验，并保存逐项结果。无候选时，ConstraintNegotiationService 在需求副本上计算可行方案，再交给 Sourcing 排序。最多展示三项，最多三轮；库存不足、事实未知、企业底线和准入不能协商。接受方案保存原始用户消息和结构化同意，更新需求版本并重新寻源，仍需另行确认草稿。

推荐后的 INTENT 阶段复用 Requirement 模型，EXPLAIN/NEGOTIATE 复用 Sourcing 模型且无工具。解释／比较由模型选择证据焦点，精确事实由服务端渲染；偏好改变复用合规池重排并校验效用，硬需求改变重新寻源。支持本轮“第一／二／三款”、显式选择和模糊指代澄清。请求新增可选 expected_requirement_version、expected_recommendation_version，前端重试保持原请求 ID 和版本。

推荐版本、展示编号、所选候选和必需事实摘要进入确认令牌。确认前强制刷新必需事实；不满足或未知时不写入，仍满足但事实变化时发新令牌并再次确认。写入检查点若事实过期也不写入。无效确认不会消费等待检查点；确认重试及草稿幂等沿用持久化日志。事实逐项落库，完成阶段可以从 SOURCING/REVIEWING 恢复；协商方案在模型调用前保存以保持恢复时 ID 稳定。

前端显示协商方案、事实等待、推荐编号、对话快捷入口和查询来源。sessionStorage 只保存最后 Run ID；刷新后按授权接口读取 Run、恢复未完成分发／检查点及归属选择，不将确认令牌写入浏览器存储。

主要新增模块：fixtures/offer_facts.py、services/sourcing.py、services/dialogue.py；契约、Gateway、Harness、Engine、双 Agent 适配器、工作流、API、前端及 trajectory 同步更新。决策详见 [ADR 0018](adr/0018-evidence-negotiation-and-dialogue.md)。

新任务默认执行计数：agent_turns=40、specialist_requests=32、stage_model_requests=36、tool_calls=120、agent_read=48、workflow_read=72、commit=12、provider_calls=100。commit 每次先预留三项基础操作及必需事实查询；费用、token、并发、重试和墙钟上限保持原配置。原任务预算不放大。本地 .env 的旧默认执行计数需要同步迁移，定制的费用／token／供应商费用上限不能被覆盖。

本轮适配器为可替换的本地模拟供应商接口，未连接真实供应商，也未运行付费模型评测。说明中的库存和交期仅用于闭环验证。有限候选池和最低配置协商有明确上限，不代表全市场搜索。

阶段三至六验证：新增 test_sourcing_dialogue.py 共 41 项测试通过；完整后端 259 passed、1 skipped（未设置 TEST_REDIS_URL）。确定性评测 51/51，通过 Ruff、前端 ESLint、TypeScript 与 Vite 生产构建。测试覆盖事实缓存／适用范围／过期日志／重试、三类协商接受与证据留存、制度与库存不可协商、读对话不改条件、偏好与候选的旧令牌失效、陈旧版本、写前事实变化与检查点过期、SQL／SQLite 重建、确认中断重试及重复确认。OpenAI／DeepSeek 对话阶段用假 Runner 验证角色模型、无工具权限和严格输出；没有网络模型费用。

后续需求参考工具与需求确认已独立实施，见 [制度、历史采购单与需求确认方案](20-requirement-reference-and-confirmation.md)。
