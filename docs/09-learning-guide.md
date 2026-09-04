# 设计学习导读

本文档按真实调用链解释项目设计。建议每完成一个纵向切片，就先阅读 ADR，再沿入口、应用服务、领域模型、Adapter、Harness、测试的顺序阅读代码。

## 1. 当前系统边界

```text
HTTP API
  -> ProcurementEngine：组织业务用例
     -> AgentPlanner：处理语义规划
     -> Catalog Adapter：提供外部报价事实
     -> Enterprise Gateway：提供身份、预算、制度和供应商事实
     -> AgentHarness：执行预算、Trace、授权和确认令牌
     -> RunRepository：保存业务状态
```

核心原则：LLM 管理语义不确定性，领域代码管理金额和规则，Harness 管理执行风险，Adapter 隔离外部系统。

## 2. 本轮主题：为什么确认前必须重取报价

推荐生成和草稿创建之间存在时间窗口。若报价在窗口内发生变化，直接写入会让用户确认的价格与草稿价格不一致，这属于 TOCTOU（检查时与使用时状态不同）问题。

本轮调用链：

1. `_source_and_recommend` 选择综合最优 Offer；
2. `_quote_snapshot` 提取价格、运费、税率、库存、交期、证据时间和 hash；
3. Harness 将 snapshot fingerprint、需求版本和动作签入 Action Token；
4. 用户点击确认；
5. `catalog.get_offer` 重新读取当前报价；
6. Engine 重新计算成本并执行 Policy；
7. fingerprint 相同才允许创建草稿；
8. fingerprint 不同则保存 Diff、签发新 Token，旧 Token 自动失效；
9. 用户确认新事实后才创建草稿。

## 3. 三个容易混淆的概念

### Action Token 不是登录 Token

登录 Token 回答“你是谁”；Action Token 回答“这个用户是否确认了这个需求版本、这个报价和这个动作”。二者不能相互替代。

### 幂等不等于去重请求

幂等的目标是重复请求只有一次业务效果。当前业务键由 run、offer 和 requirement version 生成，同一进程还通过 per-run lock 串行确认。跨进程 exactly-once effect 仍需要 PostgreSQL 唯一约束或外部系统幂等接口。

### 重校验不等于重新生成一段解释

重校验必须重新调用事实来源并执行确定性规则。让模型再次回答“价格是否变化”不能提供安全保证。

## 4. 推荐阅读顺序

1. `docs/adr/0002-quote-snapshot-and-confirmation.md`：理解设计选择；
2. `domain/models.py` 中的 `QuoteSnapshot`、`RevalidationChange`：理解数据契约；
3. `fixtures/catalog.py#get_offer`：理解 Adapter 如何模拟事实变化；
4. `services/engine.py#confirm`：理解应用用例和重校验顺序；
5. `harness/runtime.py`：理解 Token 为什么绑定上下文；
6. `tests/test_domain_and_engine.py`：从验收场景反推设计目的。

## 5. 面试表达

可以用下面这段话介绍本轮设计：

> 推荐页上的确认不能只绑定 runId，因为报价和企业规则可能在用户停留期间变化。我把报价的高影响字段生成稳定 fingerprint，并与用户、租户、需求版本和动作一起签入短期 Action Token。写草稿前重新拉取报价、计算成本并执行 Policy；事实变化时不静默覆盖，而是返回结构化 Diff、签发新 Token 并要求再次确认。这样同时处理了 TOCTOU、旧卡片重放和用户知情确认问题。

进一步追问时要主动说明：当前 per-run lock 只解决单进程并发，跨实例一致性要在 PostgreSQL 中通过唯一业务键和条件更新完成。

## 6. 本轮主题：身份上下文为什么不能来自业务参数

用户输入中的 employeeId、costCenter 或 tenant 都是不可信业务数据。身份必须先由认证层建立，再由企业目录解析组织和角色。Fixture 环境没有真实 SSO，因此使用 opaque demo session 模拟这个边界，但不会直接信任客户端的身份 Header。

调用顺序是：

```text
Authorization Bearer token
  -> Fixture Session Resolver
  -> tenant/user
  -> Enterprise Gateway
  -> department/cost center/roles
  -> Tool Registry
  -> 当前角色可见工具
```

Tool Registry 采用默认拒绝：工具未注册、角色不匹配或 BUSINESS_WRITE 未确认都会失败。要注意“工具可见”和“工具可执行”是两个判断：员工可以看到创建未提交草稿工具，但只有在 Action Token 确认流程通过后才能执行。

面试中可以强调：

> Agent 的工具权限不是 Prompt 建议，而是服务端能力集合。认证层决定主体，企业目录决定组织上下文，Tool Registry 决定可见能力，HITL 再控制高风险动作。即使用户要求忽略规则，模型也无法获得 Registry 中不存在的下单或支付工具。

## 7. 本轮主题：主动追问为什么需要停止条件

任务型 Agent 的目标不是延长对话，而是用最少信息达到安全执行条件。当前实现把追问分成三层：

1. 领域层定义哪些字段是进入寻源的硬前置条件；
2. 信息价值决定先问哪些字段；
3. 轮次预算决定本轮问几个以及何时终止。

用户说“按公司标准”也不是授权系统猜测任意字段。系统只对有 Policy 依据的最低内存和存储应用默认值，并把来源写入 assumption；数量、预算、税口径和交付地仍不能擅自假设。

面试表达：

> 我没有把主动追问实现成循环调用 LLM，而是把必需字段、信息价值、问题预算和停止条件显式化。模型后续可以负责理解和生成自然问题，但是否具备执行条件由确定性状态机判断。这样既保留 Agent 的语义能力，又能测试重复提问、漏问和无限循环。

## 8. 本轮主题：为什么 Product、Variant、Offer 必须分层

- Product 回答“它是什么系列”：品牌、型号、品类。例如 Aster A-240 Pro。
- Variant 回答“具体是哪一种配置”：CPU、RAM、SSD、系统、保修、成色和套装。
- Offer 回答“谁在什么条件下卖”：Provider、卖家、价格、库存、运费、交期和证据。

如果把三层压成一条商品记录，同型号 16GB 与 32GB 容易被标题相似度合并，翻新机或扩展坞套装的低价也会污染单机新品的最低价。分层后，同一 Product 可以有多个 Variant，同一 Variant 可以有多个 Provider Offer，价格比较只发生在身份已验证的对象上。

本轮调用链：

```text
FixtureCatalogAdapter.search
  -> ProviderRawOffer（保留 Provider 原始表达和证据）
  -> CanonicalizationPipeline._parse（结构化字段和标题分别解析）
  -> Product candidate（品牌/完整型号）
  -> Variant candidate（CPU/RAM/SSD/OS/保修/成色/套装）
  -> deterministic Match Guard
       -> MATCH：生成 Offer，允许成本/Policy/排序
       -> REVIEW_REQUIRED：保留复核项，不参与比价
       -> REJECTED：记录冲突 veto，不参与比价
  -> ProcurementEngine（只排名 MATCH）
  -> Recommendation + normalization_issues
  -> UI（展示置信度、冲突、成色、套装和不可比原因）
```

关键领域模型：

- `ProviderRawOffer` 是外部事实边界，不接受 Provider 自报的 canonical ID。
- `MatchDecision` 保存 canonical Product/Variant ID、confidence、matched/conflict fields、decision、reasons 和 evidence refs。
- `OfferMatchAssessment` 让被拒绝或待复核的报价仍可解释、可审计。
- `CatalogSearchResult` 是 Adapter 统一输出，分开已验证 `offers` 和不可比 `assessments`。

## 9. LLM 与确定性代码的分工

LLM 适合处理未知别名、模糊标题和候选召回，因为这些问题具有语义不确定性；它未来可以提出 Product/Variant 候选或 pairwise match 建议。确定性代码负责容量换算、条件枚举、完整型号后缀、关键字段冲突、阈值和“能否进入比价”，因为这些结论会影响金额、制度和确认副作用。

完全交给 LLM 的问题不只是结果波动，还在于模型可能把“看起来很像”误当成“可以按同款比较”。本轮测试会把一个错误 Variant proposal 主动传给管线；即使上游声称同款，服务端仍比较 RAM、SSD 等事实并返回 `REJECTED`。

Precision 与 Recall 的风险并不对称：提高 Recall 可以少漏掉一些候选，但 false merge 会制造一个不存在的最低价，继而污染预算判断、Policy 结果和用户确认。因此 P0 采用 precision-first：hard false merge 是 veto metric；不确定候选进入人工复核。更高 Recall 留给 P1 的 hybrid retrieval 与标注集校准，不能用牺牲安全边界换取。

Confidence 当前由字段覆盖和结构化字段/标题双证据一致性确定，0.90 是 Fixture 门槛，不是生产校准结果。生产版本需要按 Provider、品类和置信度分桶，使用人工标注集检查 calibration curve、precision/recall 和阈值成本，再决定自动匹配区间与人工复核区间。

Evidence/provenance 的作用是让每个 Match Decision 能回到原始报价 ID、source ref、抓取时间、有效期、hash 和原始字段。发生误合并时，可以判断是 Provider 数据冲突、解析器错误、候选召回错误还是阈值错误，而不是只看到一个无法复现的模型结论。

## 10. 失败模式与测试证据

已覆盖的失败模式：

- `32G/32 GB/32768MB`、`1T/1024GB/1TB`、`Win11 Pro/Windows 11 Professional` 被统一表达；
- 同型号不同 RAM、SSD、成色和套装产生不同 Variant ID；
- 型号关键后缀不一致、Provider 字段与标题 RAM 冲突被 `REJECTED`；
- 关键存储缺失进入 `REVIEW_REQUIRED`，不会生成 Offer；
- 错误的上游 Variant proposal 被确定性 guard veto；
- Adapter contract test 保证三个 Provider 只把 `MATCH` 且置信度达标的 Offer 返回给比价；
- deterministic grader 统计 Product/Variant match precision、false merge、conflict veto、review required 和 verified candidate rate；hard false merge 为 0 才能通过 veto gate。

这些测试证明的是当前 9 条人工标注 hard cases 和 450 条常规 Fixture 的行为。它们不能证明开放世界别名召回、真实商城 schema drift、跨品类匹配或真实概率校准。生产版本仍需要更大的 train/dev/holdout 标注集、Provider contract 监控、人工复核队列、阈值分桶、误合并审计和 live/fixture 分离报告。

## 11. 两分钟面试表达

> 采购比价最危险的不是漏掉一个报价，而是把不同配置合并后制造虚假的最低价。我把 Provider 输入改成不可信的 Raw Offer，先分别解析结构化字段和标题，再建立 Product、Variant、Offer 三层身份。Product 是品牌型号，Variant 包含 RAM、SSD、系统、成色和套装，Offer 才是价格、库存和交期。上游可以由规则、检索或 LLM 提出候选，但服务端 Match Guard 对完整型号后缀和关键规格做确定性校验：冲突直接 REJECTED，缺字段或低于 0.90 进入 REVIEW_REQUIRED，只有 MATCH 能进入成本、预算、Policy 和排序。每个决策保存 confidence、matched/conflict fields、reasons 和 evidence refs，UI 也解释为何不能比价。我用 9 条 hard cases 建了真正的 grader，当前 Fixture 的 Product/Variant precision 都是 100%，hard false merge 为 0。这个数字只代表确定性 Fixture；生产上还要用真实标注集做分桶校准和 Recall 权衡。

## 12. 面试官可能追问

**为什么不直接用标题 embedding？**

Embedding 适合召回候选，不适合做最终安全门禁。高相似标题仍可能只差 RAM、SSD、Pro 后缀、翻新状态或套装；这些字段必须精确比较。

**为什么 false merge 是 veto，而 false split 不是？**

False split 主要损失比价覆盖率，可以人工合并；false merge 会让错误价格进入排名并传播到预算、Policy 和确认，是决策完整性问题，不能被其他平均分抵消。

**置信度 0.90 从哪里来？**

当前是 P0 Fixture 的显式初始门槛，基于字段覆盖和双来源一致性，不声称已经统计校准。生产会用标注集按分数分桶计算 observed precision，并根据误合并成本选择阈值。

**LLM 判断同款时为什么还要规则层？**

LLM 的判断是候选 proposal，不是授权事实。规则层拥有关键字段和能否比价的最终决定权；测试已经证明错误 proposal 会被 veto。

**为什么低置信度不直接丢弃？**

直接丢弃会损失 Recall，也无法改善数据。进入带证据的人工复核既保护价格排序，又能积累标注，反哺别名、解析器和阈值。

**当前方案离生产还有什么差距？**

当前只覆盖一个品类和规则化 Fixture；缺真实 Provider schema contract、hybrid retrieval/pairwise matcher、大规模标注集、概率校准、复核工作流和数据漂移监控。这些不会被 9 条 hard-case 的 100% precision 掩盖。

## 13. 本轮主题：状态机和执行预算怎样配合

状态机回答“下一步能不能去这个阶段”，执行预算回答“这次任务还有没有资源继续做”。两者都由 Harness 控制，但不能混为一个计数器。

正常闭环现在是：

```text
COLLECTING
  -> SOURCING
  -> REVIEWING
  -> WAITING_CONFIRMATION
  -> REVALIDATING
  -> DRAFT_CREATING
  -> DRAFT_CREATED
```

需要追问时走 `COLLECTING -> WAITING_USER -> COLLECTING`；报价变化时走 `REVALIDATING -> WAITING_CONFIRMATION`。业务代码不再直接赋值 `run.state`，而是请求 Harness 迁移。Harness 先检查路线是否合法和迁移额度是否足够，再更新状态、版本和事件。

工具调用使用两个计数：一次“搜索三个来源”会占用三个逻辑工具调用；某个来源因 429 重试两次，还会额外占用两次 Provider 请求和两次重试额度。这样可以看见失败放大，而不会把三次网络尝试伪装成一次调用。

Provider 调用的顺序是：

```text
权限检查 -> 时间/工具额度 -> Provider/重试额度
-> 并发槽位 -> timeout -> 执行
-> 成功关闭熔断，或失败后有限重试
-> 连续失败达到阈值后按来源打开熔断
```

面试表达：

> 我把状态和 Trace 分开：状态由集中迁移表保护，Trace 只记录发生过什么。每个 Run 固化当时的额度和累计用量，所有额度都在动作前扣减。Provider 的超时、重试、熔断按工具和来源隔离，一个商城失败不会拖掉其他来源。状态事件和预算用量随 Run 保存，因此可以解释任务为什么走到当前阶段、为什么停止。当前真实模型 usage 和跨进程熔断仍未完成，我没有把数据模型等同于生产能力。

推荐阅读顺序：

1. `docs/adr/0006-explicit-state-machine-and-execution-budgets.md`：先理解为什么；
2. `domain/models.py`：看状态事件、预算和用量的数据契约；
3. `harness/runtime.py`：看迁移、前置扣减和工具故障策略；
4. `services/engine.py`：看业务流程如何只通过 Harness 推进；
5. `tests/test_harness_state_machine.py`：从失败测试确认安全边界。
