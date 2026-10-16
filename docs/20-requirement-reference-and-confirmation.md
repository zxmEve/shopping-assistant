# 制度、历史采购单与需求确认实施方案

日期：2026-10-16。

## 目标

需求 Agent 查询依据 → 生成有来源的需求草案 → 用户确认需求 → 寻源 → 推荐 → 独立确认创建未提交采购草稿。

保留 Requirement Agent、Sourcing Agent 两个角色。Requirement Agent 在需求处理阶段增加 query_procurement_policy、query_purchase_history 两个只读工具；采购归属路由和对话意图分类保持零工具。模型选择工具与整理需求，Gateway/Harness 控制权限、预算、审计、幂等；服务端验证来源、制度、版本与确认。

## 工具及数据

- query_procurement_policy：查询当前品类、组织适用的有效制度，返回版本、生效时间、条款引用、强制标准和建议标准。复用现有制度服务；结构化要求与执行制度版本一致。未规定的值不作为默认制度值，例如目前制度未规定 1TB 存储。
- query_purchase_history：查询本人／授权团队可访问的历史订单，可按用户给出的订单引用选择；返回至多三个订单摘要及需求相关订单行。第一轮使用明确标记的模拟已完成订单，独立于 DRAFT_NOT_SUBMITTED 采购草稿。
- 租户、用户、组织和可访问范围从认证身份获取，模型不能指定其他身份或执行 SQL。制度版本冲突、无有效条款、没有可访问订单时应明确说明，不伪造结果。
- 同时提到历史与最新制度时，查询两者。无论是否显式提到制度，历史复用均需现行制度复核。没有明确采用依据时，不自动查询历史。

## 需求草案与来源

新增 RequirementResolutionDraft，保存草案 ID／版本、基准需求版本、提议需求、字段来源、引用与采用理由、差异、缺项／冲突、确认状态和用户确认记录。

当前生效需求与草案分开。用户原文、制度条款／订单行、用户确认动作是三类证据；外部数值不得伪造成用户原文字符引用。用户本轮明确要求不被历史条件覆盖；强制制度底线始终生效。历史预算可以建议复用，成交价只供参考；历史库存、实际到货日及当时供应商交付承诺不作为本轮有效事实。

“按公司标准”原先直接填充配置的路径统一迁移至查询、生成、确认。仅有参考依据不能绕过缺项追问。所有已识别缺项集中追问；完整后再确认。草案修改生成新版本，旧确认失效。用户撤回参考依据时，移除未确认建议并保留用户明确要求。

## 工作流与接口

复用 WAITING_USER，新增 HISTORY_SELECTION、REQUIREMENT_CONFIRMATION 等等待原因。历史订单多匹配时明确选择；仅当用户明确指向“最近一单”等唯一订单时自动定位。用户选择不等于确认需求。

新增 POST /api/runs/{run_id}/requirement-confirmations，绑定 request_id、draft_id、draft_version、内容摘要及用户所见版本。确认前检查完整性、版本、制度现行性、历史访问权限；确认后直接应用已校验草案，再恢复工作流进入寻源，不再重新猜测字段。

需求确认仅授权开始寻源；创建采购草稿继续使用原有 /confirm、报价／库存／交期／保修、预算及制度复核。推荐后提出按参考依据重整需求时，暂停旧推荐确认。确认、历史选择及查询过程要支持请求去重、SQL/JSON 持久化与 checkpoint 恢复。

## 模块与实施顺序

1. domain/requirements.py、domain/conversation.py、domain/models.py：外部证据、依据请求、草案及确认记录，旧快照兼容。
2. 新增历史采购只读端口和模拟适配器；制度查询封装及结构化规则映射。
3. agents/tools.py、gateway/tool_gateway.py、harness/runtime.py：工具注册与授权；multi_agent.py、provider_adapter.py、contracts.py：需求阶段工具与预算、fixture 及模型输出。
4. 新增 services/requirement_resolution.py：需求合并、差异、来源、制度校验、选择及确认；requirement_validation.py 与 engine.py：替换直接默认填充，所有寻源入口增加确认门禁。
5. workflow.py、api/app.py、message_routing.py、dialogue.py：确认接口、等待与恢复、意图分发。
6. 前端 types.ts、api.ts、App.tsx：历史选择、来源／差异／缺项与需求确认；保留独立采购草稿确认。
7. 测试与评测：对应工具调用、联合依据、多订单歧义、无结果、跨租户／跨用户拒绝、制度冲突、来源伪造、旧确认失效、修改／撤回、重复确认、中断及 SQL/checkpoint 重建。全量后端回归、确定性评测、前端 lint/构建。

## 边界

历史订单为模拟数据；不接入真实 ERP，不导入未授权订单，不修改真实采购单。当前只支持笔记本。新读工具保留费用/token上限，按需调整需求阶段调用计数。不运行付费模型评测。

## 本轮实施

2026-10-16：按本方案开始实施制度查询、模拟历史采购单查询、带来源的需求草案、用户需求确认后进入寻源，以及对应测试。保留既有采购草稿确认和事实复核。

### 完成记录

已接入两个需求只读工具、版本化来源与外部证据、独立需求草案、模型引用字段选择、模拟历史订单选择、集中补齐缺项、需求确认后寻源，以及独立的采购草稿确认。原先“按公司标准”直接填充 1TB 的运行路径已移除。

历史适配器按租户、本人／授权部门、品类、订单状态及年份过滤，最多返回三条；明确最近一单时可定位，否则等待选择。历史金额仅使用原需求预算字段，不使用成交价和旧库存／交期。用户明确字段保留，企业底线始终校验。

新增 POST /api/runs/{run_id}/requirement-confirmations；普通消息增加可选 expected_resolution_draft_id、expected_resolution_version。需求确认绑定草案版本和内容摘要，重新读取来源；旧确认、修改后的草案、失效访问或制度变化均不能放行。确认及来源查询支持幂等与 checkpoint 恢复，接受前来源变化可作废原回执并重挂审核。确认后归档完整草案及制度／历史查询快照，后续更新仍可追溯。

前端提供历史选择、来源／条款／版本／差异／缺项展示和“确认需求并开始寻源”按钮；刷新可恢复待处理确认。详情见 [ADR 0019](adr/0019-referenced-requirements-and-confirmation.md)。

专项测试 test_requirement_references.py 共 45 项通过，覆盖 offline／fixture、LangGraph／legacy、必要字段、无要求配置、模型绑定、模型漏调工具兜底、跨租户／私有／取消／草稿订单拒绝、指定年份、来源伪造、来源变化、确认去重与中断、SQL／SQLite 重建、自然语言确认和 API 权限。未运行付费模型或真实 ERP 查询。

最终在项目目录验证：后端 Ruff 通过；完整 pytest 304 passed、1 skipped（未配置 TEST_REDIS_URL）；确定性评测 51/51；前端 ESLint、TypeScript 和 Vite 生产构建通过；git diff --check 通过。
