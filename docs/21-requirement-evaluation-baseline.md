# 需求 Agent 评测基线 v1

## 结论与边界

第一版把“固定用例 + 独立语义标签 + 版本记录 + 校验前后评分 + 报告对比”落实为可执行工具。它补充现有 51 项确定性评测和 12 项 live smoke，不替换它们。

本轮只执行本地规则解析器和校验器，没有请求任何真实模型，没有消耗模型 API 额度。`offline` 结果不是模型准确率、不是采购端到端成功率，也不能用于证明 Prompt 优化收益。

用例为工程编写的合成标签，`label_status=draft_engineering_labels`；还需要业务人员复核。即使用例全部通过，也不能当作生产发布证明。

## 已实现的内容

- `backend/evals/datasets/requirement_baseline_v1.json`：60 条用例、30 个改写组，20 条 development、40 条 holdout。相同改写组不允许跨集合。覆盖完整需求、软硬约束、缺失项、多轮局部修改、币种/否定/同句更正、冲突与预算口径。
- 更新用例从手写的已确认需求快照开始，基础数量20、单台8000 CNY含税、上海、16GB/1TB；不是使用被测解析器生成初始标准答案。v1测的是固定前置状态下的一次需求边界调用，不是任意长度的多轮会话。
- `requirements.py`：比较字段、值、单位、运算符和约束强度；错误提取不重复算成遗漏；额外事实单独计数；校验引用、缺失字段、冲突、追问覆盖和增量字段。随机ID、置信度和追问措辞不作字面比较。
- `requirement_runner.py`：分别保留 proposal 与 final；校验前深拷贝，避免校验器原地补齐后污染原始评分；记录降级、错误、完整输入和中间结果。
- 按实验ID生成独立的 JSON/HTML 和 Prompt 文件，不覆盖旧报告。生成目录已加入 `.gitignore`；本轮选定的历史报告已归档至 `backend/evals/audits/requirement-v1-20261022/`，后续大规模报告可使用制品存储。
- 清单记录代码提交号、实际源码指纹、评分器指纹、依赖版本、数据集/Prompt/输出Schema/基础快照/配置指纹及用例范围。工作区有未提交改动时，以源码指纹区分，不仅依赖Git提交号。
- 比较器拒绝混用不同数据、集合、重复次数、模型配置、评分器或输入。既检测通过变失败，也检测原本失败的用例继续恶化。成本下降不能抵消新增质量问题。

## 如何执行

以下命令在项目的 `backend` 目录运行。可以使用 `.venv/bin/python`，也可以使用 `uv run python`。

### 本地开发集

```bash
.venv/bin/python -m procure_pilot.evals.requirement_runner run --mode offline --split development
```

项目根目录也可运行 `make eval-requirements`。评测不通过时退出码为1，并保留报告。这不代表 pytest 测试失败，而是被测解析器没有满足标签。

### 当前完整基线

```bash
.venv/bin/python -m procure_pilot.evals.requirement_runner run --mode offline --split all
```

报告默认位于 `backend/evals/reports/requirements/<实验ID>/`。离线 token、模型费用字段为 null，不伪造为模型实测0消耗。

### 显式运行真实模型

只有配置模型凭证并明确选择 live 才会调用模型。使用项目已有 `PROCURE_MODEL_PROVIDER`、`PROCURE_REQUIREMENT_MODEL` / 模型回退配置、`PROCURE_OPENAI_API_KEY`、`PROCURE_OPENAI_BASE_URL`；凭证不会进入报告。

```bash
.venv/bin/python -m procure_pilot.evals.requirement_runner run --mode live --split development --repetitions 3
```

该模式调用现有 Requirement Adapter / SDK，保留输出结构和工具配置，使用独立内存状态、空记忆、固定前置需求；不进入寻源或创建采购草稿。本套用例没有历史/制度引用授权，不评测引用工具质量。它不等同于经过完整工作流的端到端运行。

先小规模跑开发集并确认费用，再冻结配置运行 holdout。不要一边看 holdout 结果，一边据此调整 Prompt。当前30组仍是小样本，重复采样不等于增加独立业务场景。

### Prompt 单变量对照

将完整 Requirement 指令分别保存在本地 `prompt-a.txt` 和 `prompt-b.txt`，只替换它们，不修改代码、模型、Schema、上下文和用例；提供的文件替换语义指令，适配器仍按现有逻辑追加同一Schema要求。

```bash
.venv/bin/python -m procure_pilot.evals.requirement_runner run --mode live --split development --repetitions 3 --prompt-file prompt-a.txt
.venv/bin/python -m procure_pilot.evals.requirement_runner run --mode live --split development --repetitions 3 --prompt-file prompt-b.txt
.venv/bin/python -m procure_pilot.evals.requirement_runner compare /path/to/A/report.json /path/to/B/report.json
```

`offline --prompt-file` 会被拒绝，避免规则解析器“比较”实际上未执行的Prompt。比较器标记 `prompt_only` 只是清单中的可观测变量检查，不是因果关系的统计证明；服务端模型变化、缓存和运行时段仍可能影响结果。当前命令每次运行一个版本，尚未实现A/B交错调度或置信区间。

## 指标如何解释

- `case_exact_match_rate`：整条用例语义、证据、状态、追问与指定增量检查全部通过的比例。
- `fact_accuracy`：正确事实数 / 标准事实数。
- `omission_rate`：缺失事实数 / 标准事实数。
- `wrong_extraction_rate`：字段存在但数值、币种、运算符或强度错误的事实数 / 标准事实数。
- `unsupported_addition_count`：标准答案之外的额外断言数，不与错误提取重复计数。
- proposal：适配器解析、兼容修复和路由元数据绑定之后，领域校验之前的提议。它不是未经SDK处理的原始字符串；live另存 `provider_output` 用于追踪。
- final：当前领域校验及必要的确定性降级之后的结果。记录校验错误与fallback次数，防止兜底掩盖模型缺陷。
- live费用来自现有网关的token估算，不代表实际账单。失败调用即使有部分usage也标记不完整；比较器不会在消耗不完整时给出总token差值。
- 汇总同时按类别和development/holdout拆分。P95按最近秩方法计算，但离线小样本耗时不能当作模型性能。

## 首轮离线结果与待办

初次完整运行，proposal与final均44/60通过；422项标准事实，10项遗漏、4项错误提取、2项无依据新增。该结果基于工程草案标签，只说明当前规则解析与校验能力。

失败集中于：

1. 部分512GB/2048GB存储表述漏提取，存储上下界冲突没有完整保留。
2. 同句“20台，改成12台”仍取第一个数量。
3. 明确美元预算被归一化为人民币。
4. 明确全新/拒绝翻新的条件没有进入需求约束。
5. 整批预算160000元被当成单件预算。v1标签采用“先确认单件上限”的保守口径；是否允许按数量换算，必须业务复核后版本化修改标签。

首次建立基线时未修改业务解析器。后续已在不修改标签和评分器的前提下逐项修复至60/60，详见[16条失败分析与修复对比](22-requirement-baseline-failure-analysis.md)。正式业务标签复核仍待完成。某些项目评分维度暂不覆盖：用途分类、交付偏好强度（当前校验器会移除对应冗余约束）、真实制度/历史工具、路由、完整多轮演化、寻源、外部写入与跨进程取消。已有端到端门禁仍应独立执行。

## 下一阶段

1. 业务复核60条标签，尤其是预算口径，完成标签审核记录。
2. 经授权执行小规模真实模型开发集，确认proposal/final差异和消耗采集。
3. 固定版本后运行保留集及Prompt对照；不要将离线表现作为模型选型依据。
4. 扩展引用、工具故障、旧结果晚返回和恢复用例，接入原有51项门禁；增加明确的安全硬门禁和发布阈值。
5. 为报告增加A/B交错运行、按独立case group计算的不确定性，以及制品归档与CI任务。

## 新留出集真实模型执行

已新增并冻结24条新编合成留出用例，完成一轮真实模型评测，记录校验前后差异、token及分阶段耗时，详见[新留出集实测报告](23-requirement-live-holdout-evaluation.md)。结果暴露了校验与兜底引入遗漏的问题，不能用旧离线60/60代表真实模型效果。
