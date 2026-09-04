# 第一阶段实现与测试报告

初始报告时间：2026-09-01；最近更新：2026-09-04。测试对象为 deterministic fixture 模式，不代表真实商城或生产企业制度效果，也不代表完整 P0 已完成。

## 1. 已实现范围

- FastAPI/Pydantic API 与领域模型；
- OpenAI Agents SDK 的 Manager + 3 specialists-as-tools 拓扑；
- 无 API Key 可运行的 deterministic agent planner；
- 主动追问、多来源并行检索、`ProviderRawOffer -> Product -> Variant -> Offer` 确定性归一；
- Python `Decimal` 成本计算；
- 版本化 YAML Policy-as-Code、预算、供应商和审批路由；
- 最小 Agent Harness：显式状态迁移、连续 state version、按 Run 保存的执行预算/用量、Provider timeout/retry/circuit/bulkhead、版本化 Tool Registry、HITL、签名 action token 和幂等草稿；
- 服务端 Fixture Session 将 opaque token 映射为 tenant/user，忽略客户端伪造的身份和成本中心 Header；
- Action Token 绑定需求版本、动作和报价快照，确认前重新读取报价并在变化时要求二次确认；
- 450 条常规原始报价、9 条商品归一 hard cases、429/timeout 和间接 Prompt Injection fixture；
- React 采购对话、推荐比较、审批预览、草稿状态和 Agent Trace 页面；
- 26 个版本化场景与 JSON/HTML 自动评测报告；
- Makefile、Dockerfile、Compose 和环境变量模板。

## 2. 已执行验证

| 验证 | 实际结果 |
|---|---:|
| Ruff | 通过 |
| Python pytest | 41/41 通过 |
| Phase 1 scenario eval | 26/26 通过 |
| Veto failures | 0 |
| Frontend ESLint | 通过 |
| TypeScript + Vite production build | 通过 |
| Fixture scenario p95 | 以 `latest.json` 当前结果为准 |
| 本地浏览器核心链路 | 主动追问、补充信息、推荐与 Trace 已验证 |
| 浏览器控制台错误 | 0 |
| Compose 静态配置 | 通过 |

Docker 镜像未完成实际构建验证：本机 Docker CLI 可用，但 Docker daemon/Colima 未运行。该项不是应用测试失败，启动 Docker daemon 后仍需执行 `docker-compose build`。

## 3. 当前确定性评测指标

| 指标 | 结果 |
|---|---:|
| Task success rate | 100%（26/26） |
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

Product/Variant 指标由带明确预期的 9 条 hard cases 计算：6 条合法格式/变体（含新品、翻新、二手、单机与套装）、2 条硬冲突和 1 条关键字段缺失。它能证明当前 Fixture 上没有 hard false merge，但样本量很小，不能外推为真实商城准确率。逐事实 evidence coverage 仍未形成完整指标；当前只验证 Candidate 和 Match Decision 的引用存在性。

机器可读报告：`backend/evals/reports/latest.json`；可视化报告：`backend/evals/reports/latest.html`。

## 4. 尚未验证

- 真实 OpenAI Agents SDK 推理质量、token、费用和网络 p95：当前没有配置 API Key；
- 真实商城/API、登录态、限流和数据漂移；
- PostgreSQL、Temporal、Kafka、长期记忆和自进化，均属于 Phase 2/3；
- Docker 镜像运行；
- 多浏览器和真实手机兼容性。

因此简历可以写“完成 Phase 1 fixture 闭环和 26 场景确定性评测”，不能写“真实跨平台实时比价已上线”或把 100% fixture 分数表述为生产准确率。

完整 P0 的逐条状态、证据和缺口见 `08-requirements-traceability.md`。当前 token/cost 额度尚未接入真实模型 usage，熔断仍是单进程状态，checkpoint/cassette 也未完成；商品归一只覆盖 Fixture 笔记本表达。Fixture Session 只用于离线演示，生产环境仍需对接 OIDC/SSO。

## 5. 复现命令

```bash
make setup
make test
make eval
make dev
```
