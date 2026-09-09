# 第一阶段实现与测试报告

初始报告时间：2026-09-01；最近更新：2026-09-09。测试对象主要为 deterministic fixture 模式，不代表真实商城或生产企业制度效果，也不代表完整 P0 已完成。

## 1. 已实现范围

- FastAPI/Pydantic API 与领域模型；
- OpenAI/DeepSeek 共用双 Agent Provider Adapter：Requirement Agent 与 Sourcing Agent 使用独立 SDK 实例、模型、输入投影和工具权限；
- Provider 无关的确定性 Runtime：根据需求完整度只执行一个所需 Agent，校验状态版本、工具白名单、请求预算、旧结果拒绝和回退；
- 两个 Agent 只挂载需求检查和目录搜索工具；完整 Run 消息进入 Planner，Agent 已查目录结果可被 Engine 复用；Sourcing Agent 可提交带目录证据的候选，服务端完成成本、预算、制度和证据验证后才改变综合推荐；
- 无 API Key 可运行的 deterministic agent planner；
- 主动追问、多来源并行检索、`ProviderRawOffer -> Product -> Variant -> Offer` 确定性归一；
- Python `Decimal` 成本计算；
- 版本化 YAML Policy-as-Code、预算、供应商和审批路由；
- 最小 Agent Harness：显式状态迁移、连续 state version、按 Run 保存的执行预算/用量、Provider timeout/retry/circuit/bulkhead、版本化 Tool Registry、HITL、签名 action token 和幂等草稿；
- 服务端 Fixture Session 将 opaque token 映射为 tenant/user，忽略客户端伪造的身份和成本中心 Header；
- Action Token 绑定需求版本、动作和报价快照，确认前重新读取报价并在变化时要求二次确认；
- 450 条常规原始报价、9 条商品归一 hard cases、429/timeout 和间接 Prompt Injection fixture；
- React 采购对话、推荐比较、审批预览、草稿状态和 Agent Trace 页面；
- PostgreSQL/SQLAlchemy Repository、Alembic migration、租户条件、乐观锁和草稿业务唯一键；
- consent-backed 长期记忆表与 API，覆盖 Profile/Team/Episodic/Semantic、版本化修改、软删除和审计；
- 制度知识独立文档/版本/条款、治理角色发布、PostgreSQL GIN FTS、时态/租户过滤、版本化引用和 Policy-as-Code 一致性门禁；
- 46 个版本化场景、独立 deterministic/live JSON/HTML 自动评测报告；
- Makefile、Dockerfile、Compose 和环境变量模板。

## 2. 已执行验证

| 验证 | 实际结果 |
|---|---:|
| Ruff | 通过 |
| Python pytest | 109/109 通过 |
| Phase 1 scenario eval | 46/46 通过 |
| Veto failures | 0 |
| Frontend ESLint | 通过 |
| TypeScript + Vite production build | 通过 |
| Fixture scenario p95 | 以 `latest.json` 当前结果为准 |
| 本地浏览器核心链路 | 主动追问、补充信息、推荐与 Trace 已验证 |
| 浏览器控制台错误 | 0 |
| Compose 静态配置 | 通过 |
| DeepSeek live Agent | 3/3 通过；p95 10.859 秒 |
| PostgreSQL 17 migration + 完整采购闭环 | 通过 |

PostgreSQL 17 容器、Alembic 升级和数据库端到端采购闭环已经实际运行；完整 backend/frontend Docker 镜像仍需单独执行 `docker-compose build` 验证。

## 3. 当前确定性评测指标

| 指标 | 结果 |
|---|---:|
| Task success rate | 100%（46/46） |
| Product match precision（9 条 hard cases） | 100% |
| Variant match precision（9 条 hard cases） | 100% |
| Hard false merge count | 0 |
| Conflict veto count | 2 |
| Review-required count | 1 |
| Verified candidate rate（hard cases） | 66.67% |
| Money exact match | 100% |
| RAM hard-constraint failures | 0 |
| Unauthorized write count | 0 |
| Duplicate draft count | 0 |
| Prompt injection write count | 0 |
| Replay pass rate | 100% |
| Clarification case pass rate | 100% |
| Policy case pass rate | 100% |
| Partial failure return rate | 100% |
| Selected-candidate evidence-reference presence | 100% |
| State transition validity | 100% |
| Budget enforcement | 100% |
| Retry/circuit policy scenario | 100% |
| Agent tool contract | 100% |
| Agent grounded offer chain | 100% |
| Agent multi-turn context | 100% |
| Agent validated recommendation | 100% |
| Agent invalid-candidate veto | 100% |
| Agent trajectory contract | 100% |
| Agent duplicate-call detection | 100% |
| Database round trip | 100% |
| Database tenant scope | 100% |
| Database optimistic lock | 100% |
| Personal memory scope | 100% |
| Team memory authorization | 100% |
| Memory lifecycle audit | 100% |
| Knowledge citation | 100% |
| Knowledge tenant/effective filter | 100% |
| Policy knowledge version veto | 100% |
| Engine policy citation grounding | 100% |

Product/Variant 指标由带明确预期的 9 条 hard cases 计算：6 条合法格式/变体（含新品、翻新、二手、单机与套装）、2 条硬冲突和 1 条关键字段缺失。它能证明当前 Fixture 上没有 hard false merge，但样本量很小，不能外推为真实商城准确率。逐事实 evidence coverage 仍未形成完整指标；当前只验证 Candidate 和 Match Decision 的引用存在性。

机器可读报告：`backend/evals/reports/latest.json`；可视化报告：`backend/evals/reports/latest.html`。

## 4. Live Agent 与数据库实测

DeepSeek `deepseek-v4-flash` 使用同一套 Harness 和 fixture 工具连续运行 3 次，3/3 到达 `WAITING_CONFIRMATION`，轨迹检查和服务端候选验证均通过。平均延迟 10.325 秒，p95 10.859 秒，平均输入/输出 token 为 14,399/1,492；按版本化峰时单价保守估算三次合计 0.024915 美元，实际账单仍以 Provider 为准。三次模型输出都包含 JSON 之外的文字，运行时只从最后一条模型消息中提取首个对象并重新执行 `AgentPlan` 校验，同时记录 `model.output_repaired`，不对无效内容放宽 Schema。

上述历史 live 报告只把 `WAITING_CONFIRMATION` 作为成功终点。现在代码已将预算拆为 `agent_read_tool_calls`、`workflow_read_tool_calls` 和带预留的 `commit_tool_calls`，总工具上限调为 21；`confirm()` 在进入 `REVALIDATING` 前预检 3 次状态迁移并预留 3 次 commit 调用，失败会释放余额并保持或恢复到 `WAITING_CONFIRMATION`。Live runner 也已继续执行确认并以 `DRAFT_CREATED`、草稿存在和 trajectory 通过为成功条件。fake live-shape 回归已证明 9 次 Agent 读取后仍可完成 `9 + 1 + 3 = 13` 次分阶段调用；更新后的 DeepSeek 真实模型闭环尚未重跑，因此不能把旧的 3/3 表述为草稿成功率。

数据库验证使用 PostgreSQL 17：Alembic 可从空库升级到 `20260909_0005`；制度表、唯一/时态索引和 GIN FTS 索引已创建，并在临时 PostgreSQL 数据库执行了中文条款导入、发布、FTS 召回和跨租户隔离。采购数据库闭环仍执行 start -> confirm -> draft；跨租户读取、乐观锁和重复草稿身份继续由数据库约束。

## 5. 尚未验证

- 更大规模真实模型稳定性、长对话质量、Provider 账单与估算费用的一致性；
- 真实商城/API、登录态、限流和数据漂移；
- PostgreSQL 行级安全、生产连接池与监控；制度向量/RRF 检索、Temporal、Kafka 和自进化尚未实现；
- Docker 镜像运行；
- 多浏览器和真实手机兼容性。

因此简历可以写“完成确定性 fixture 闭环、DeepSeek Agent 受控工具链小样本验证、PostgreSQL、长期记忆和制度 FTS 首片、46 场景确定性评测”，不能写“生产级跨平台实时比价已上线”，也不能把小样本 live 或 100% fixture 分数表述为生产准确率。

完整 P0 的逐条状态、证据和缺口见 `08-requirements-traceability.md`。独立 `procure-live-eval` 会把成功率、token、延迟、估算费用和轨迹结果写入 `evals/reports/live/`。熔断仍是单进程状态，checkpoint/cassette 也未完成；商品归一只覆盖 Fixture 笔记本表达。Fixture Session 只用于离线演示，生产环境仍需对接 OIDC/SSO。

## 6. 复现命令

```bash
make setup
make test
make eval
# 配置 PROCURE_OPENAI_API_KEY 后才运行
make live-eval
# 启动 PostgreSQL 并执行迁移
docker compose up -d postgres
make migrate
make dev
```
