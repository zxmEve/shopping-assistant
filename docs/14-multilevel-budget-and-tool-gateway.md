# 多级费用预算与统一工具网关

## 1. 目标与边界

本次实现把原有单次运行配额扩展为跨运行共享的费用预算：

- 平台级：全平台每日、每月总费用。
- 租户级：每个企业每月费用。
- 用户级：租户内单个用户每日费用。
- Provider（外部服务提供方）级：按模型厂商、目录服务或企业服务配置每日/月度费用。

所有金额都使用整数 `micro_usd`（微美元，1 美元 = 1,000,000 微美元），避免浮点数累计误差。原 `AgentHarness` 继续负责单次运行的步骤数、调用次数、Token（模型文本计量单位）数和运行费用上限；新 `BudgetService` 负责跨运行、跨实例的共享费用上限。

## 2. 调用链

```text
Agent / Workflow（智能体 / 工作流）
        │ 只能提交已注册的工具名和结构化参数
        ▼
Tool Gateway（工具网关）
        ├─ 注册表校验、角色/智能体授权、参数校验
        ├─ ToolCall 租约、幂等控制、Fencing Token 并发保护
        ├─ BudgetService 费用预留
        ├─ AgentHarness 单次运行配额、超时、重试、熔断
        └─ 调用网关内部绑定的 Provider 处理函数
                    │
                    ▼
             外部服务 / 本地可信函数
```

模型请求由 `ModelGateway`（模型网关）包裹。每一次真正发往模型厂商的请求都会先预留费用，响应返回后根据 Token 用量和价格版本结算；没有可识别价格时按预留上限保守结算，不会把未知费用当成零。

Agent（智能体）不能传入任意可执行函数。它只能调用 `GatewayToolRegistry`（网关工具注册表）中标记为 `AGENT` 且授权给当前智能体和角色的工具。目前：

- Requirement Agent（需求智能体）：`inspect_procurement_requirement`。
- Sourcing Agent（寻源智能体）：`search_catalog`。

工作流的报价复核、制度校验和草稿创建也通过同一个网关执行。

## 3. 预算预留与结算顺序

### 3.1 正常调用

1. Redis 使用一段 Lua 脚本，在同一个原子操作中检查平台、租户、用户和 Provider 的全部计数器。
2. 任意一级不足则全部拒绝；全部足够才同时增加 `reserved`（已预留费用）。
3. PostgreSQL 写入 `RESERVE` 流水和预算预留记录。
4. 调用外部服务前，PostgreSQL 先记录 `DISPATCH_INTENT`（准备发送），Redis 再标记 `DISPATCHED`（已经发送或很可能已经发送），最后 PostgreSQL确认 `DISPATCHED`。
5. 响应返回后，PostgreSQL 先持久化实际费用和 Outbox（待同步事件），再更新 Redis：`reserved` 减少，`committed`（已确认费用）增加。
6. Redis 更新成功后，Outbox 标记完成；Redis 更新失败时，数据库结算仍然有效，Outbox 保持待处理，由后台任务重放。

因此不是简单的“先写 Redis，再盲写数据库”。预留阶段必须先在 Redis 原子抢占额度，结算阶段则先保住 PostgreSQL 的永久事实。无法跨两个系统做一个真正的本地事务，所以实现使用幂等键、状态机和 Outbox 达成最终一致，并在不确定时保守占用预算。

实际费用超过预留费用时属于 `OVERAGE`（超额）。该路径先更新 Redis，避免数据库已记账但在线计数器仍允许继续花费。

### 3.2 状态含义

| 状态 | 中文含义 | 是否允许释放 |
|---|---|---|
| `RESERVED` | 已预留，尚未准备发送外部请求 | 可以 |
| `DISPATCH_INTENT` | 数据库已记录“准备发送”；处于发送边界的不确定窗口 | 不可以，进入对账 |
| `DISPATCHED` | 请求已经或很可能已经发送 | 不可以，进入对账 |
| `SETTLED` | 已按真实用量或保守估算结算 | 不需要 |
| `RELEASED` | 已确认没有外部费用，额度已退回 | 已释放 |
| `RECONCILING` | 调用结果不明确，后台正在对账 | 不可以 |
| `OVERAGE` | 实际费用超过预留费用，差额已记账 | 不需要 |

`DISPATCH_INTENT` 不能自动当成“未调用”。进程可能在网络请求已经发出、状态还没来得及更新时崩溃；直接释放会导致真实费用未被占用。

## 4. Redis 宕机与数据恢复

Redis 不可用时采用 fail-closed（失败关闭）：预算服务抛错，网关不会继续调用可能收费的 Provider。PostgreSQL 中已经写入的预算记录和流水不会因 Redis 宕机丢失。

部署配置启用 AOF（追加式持久化文件）并使用 `appendfsync always`，每次写入都要求同步到磁盘；数据目录挂载 Docker Volume（容器持久卷）。如果 Redis 数据目录整体丢失：

1. Redis 中的就绪标记也会消失。
2. 所有预算 Lua 脚本发现没有就绪标记都会拒绝执行。
3. `RedisBudgetService.ensure_ready()` 获取分布式重建锁。
4. 服务从 PostgreSQL 的预留记录重建当前周期的 `reserved`、`committed`、预留状态和幂等映射。
5. 重建全部成功后才写回就绪标记并开放流量。

因此 Redis 是在线并发闸门，PostgreSQL 是可恢复的永久事实源。极端情况下 Redis 会多占额度而不是少算费用；后台任务随后修复。

## 5. TTL 与后台任务

TTL（存活时间）配置和清理规则：

- 调用预留默认 300 秒，可通过 `PROCURE_BUDGET_RESERVATION_TTL_SECONDS` 修改。
- Redis 预留详情保留到“预留到期时间 + 30 天”。
- 幂等映射默认保留 30 天。
- 每日/月度计数器保留到“预算周期结束 + 30 天”。
- PostgreSQL 流水不使用 TTL，作为审计记录长期保留；归档周期应按企业合规策略另行配置。

`budget-worker`（预算后台任务进程）默认每 30 秒执行一次：

1. 重放 PostgreSQL Outbox 中未同步到 Redis 的结算。
2. 释放已过期且仍是 `RESERVED` 的安全预留。
3. 释放“Redis 已预留、但进程在写 PostgreSQL 前崩溃”留下的孤儿预留。
4. 对已发送但结果未知的请求，按 `ProviderUsageReconciler`（服务方用量查询适配器）查询真实用量。
5. 查询到用量后自动补写 PostgreSQL 并结算 Redis。
6. 如果 Provider 不提供按请求查询接口，或没有配置查询适配器，则保持 `RECONCILING`；超过 72 小时后按原预留金额保守结算。

“查询真实 usage（用量）”的位置取决于 Provider：模型厂商通常查询响应/请求详情或账单用量接口，目录和企业服务则查询其请求审计或计费接口。本项目提供统一适配接口；fixture（本地演示数据）没有外部账单接口，所以走立即结算或 72 小时保守结算。补写和结算由后台任务自动完成，不需要人工触发。

## 6. PostgreSQL 数据

- `budget_policies`：预算规则及版本。
- `budget_reservations`：一次模型或工具调用的预留、发送、结算状态。
- `budget_ledger_entries`：不可变预算流水；一次事件会按每个适用层级各写一条。
- `budget_outbox`：数据库已提交、等待同步到 Redis 的事件。
- `tool_calls` 新增租约所有者、租约过期时间和 Fencing Token。

四个常用标识：

- `reservation_id`：一次费用预留的唯一编号，贯穿预留、发送、结算和释放。
- `event_id`：一次状态变化的唯一编号。同一事件会产生多条不同预算层级的流水，用它们归组并防止重复记账。
- `provider_invocation_id`：发送前由本系统生成的一次外部调用编号，用来识别“准备发送/已经发送”的调用边界。
- `idempotency_key`：幂等键。同一个业务动作重复提交时复用原结果和原预算，不再次调用、不重复扣费。

`provider_request_id` 是 Provider 返回的请求编号；与本系统提前生成的 `provider_invocation_id` 不同。后台对账适配器优先使用前者查询外部真实用量。

## 7. 配置与运行

主要配置见 `.env.example`：

- `PROCURE_BUDGET_PLATFORM_DAILY_MICRO_USD`：平台每日额度。
- `PROCURE_BUDGET_PLATFORM_MONTHLY_MICRO_USD`：平台每月额度。
- `PROCURE_BUDGET_TENANT_MONTHLY_MICRO_USD`：租户默认每月额度。
- `PROCURE_BUDGET_USER_DAILY_MICRO_USD`：用户默认每日额度。
- `PROCURE_BUDGET_PROVIDER_LIMITS_JSON`：Provider 级额度，例如 `{"MODEL:openai":{"DAILY":10000000}}`。
- `PROCURE_BUDGET_TENANT_MONTHLY_OVERRIDES_JSON`：指定租户覆盖值。
- `PROCURE_BUDGET_USER_DAILY_OVERRIDES_JSON`：指定用户覆盖值，键格式为 `tenant_id/user_id`。
- `PROCURE_TOOL_PROVIDER_COSTS_MICRO_USD_JSON`：外部工具固定费用表。

启动生产形态的本地环境：

```bash
docker-compose up --build
```

查看当前登录用户可见的租户、用户和 Provider 预算：

```bash
curl -H 'Authorization: Bearer fixture-session-cn-employee-001' \
  'http://localhost:8000/api/budgets/me?provider_kind=MODEL&provider_id=openai'
```

平台额度不会通过普通用户接口返回，避免泄露全平台财务容量。

## 8. 英语名词对照

- Agent：智能体，负责特定任务的模型执行单元。
- Gateway：网关，所有工具或模型调用必须经过的统一控制入口。
- Provider：外部服务提供方，例如模型厂商、目录服务或企业系统。
- Budget Service：预算服务，负责预留、结算、释放和查询额度。
- Reservation：预留，在真正调用前暂时占住预计费用。
- Settlement：结算，用实际费用替换预留费用。
- Ledger：流水账，按事件追加且不修改的审计记录。
- Outbox：待发箱，数据库事务内保存的待同步事件。
- TTL：存活时间，数据自动过期前允许保留的时长。
- Idempotency Key：幂等键，保证重复请求只产生一次业务效果。
- Lease：租约，某个工作进程在限定时间内拥有执行权。
- Fencing Token：栅栏令牌，递增编号，用于拒绝旧工作进程的迟到写入。
- Reconciliation：对账，把本地记录与外部服务真实用量核对并修正。
- fail-closed：失败关闭，控制组件故障时拒绝收费调用，而不是放行。
- Lua Script：Lua 脚本，在 Redis 内一次性原子执行多条检查和更新。
- AOF：追加式持久化文件，Redis 将写命令追加到磁盘用于恢复。
