# 后端规范值统一与严格评分回归

结论：在后端统一城市别名和操作系统大小写，保持原有标签、评分器和Prompt不变。固定同一批24份真实模型提议重放，严格整条通过从本轮修复前的8/24提高到24/24；剩余19项严格错误降到0，168项标准事实全部正确，遗漏与额外断言仍为0。

本轮没有新增模型调用，不把已见用例重放称为模型准确率或独立盲测结果。首次live报告、上轮8/24报告和上轮对比数据保持原样，本轮使用新目录归档。

## 根因与实现位置

原先规则解析器通常生成`Shanghai`、`linux`，模型提议却可能保留“上海”、`Linux`。这些值经过领域校验后仍未统一，严格评分、下游筛选与delta可能面对不同表示。同一个业务值的规范化应由后端负责，不能靠放宽评分器解决。

新增`backend/src/procure_pilot/services/requirement_values.py`，集中维护精确别名表：

| 输入别名 | 规范值 |
|---|---|
| 上海、上海市、Shanghai及英文大小写变体 | `Shanghai` |
| 深圳、深圳市、Shenzhen及英文大小写变体 | `Shenzhen` |
| 雅加达、Jakarta及英文大小写变体 | `Jakarta` |
| Windows及大小写变体 | `windows` |
| Linux及大小写变体 | `linux` |
| macOS及大小写变体 | `macos` |

匹配时只去除首尾空白并比较已登记别名。不做拼写纠错、前缀匹配或未知地名猜测；`Shanghaix`、北京、`Windows 11`、`Linux Mint`等未登记值保持原值，不强行折叠为已知城市或无版本系统。

三个入口共用同一套转换规则：

1. **规则解析**：城市识别支持既有中文名、带“市”的名称与对应英文名，生成同一规范值。
2. **可信需求**：`RequirementSpecService.validate()`完成证据、授权、约束检查后，深拷贝出最终需求，再调用`normalize_requirement_values()`，随后规范化delta、重算冲突与缺失项。模型原始提议仍另行保留。
3. **领域映射**：`to_procurement_requirement()`同样使用规范值函数，避免旧快照中等价写法进入商品检索和筛选。基础快照本身不被修改。

## 不改变哪些语义

仅规范化`delivery_city.normalized_value`与操作系统约束的语义值，以及对应delta的old/new值。SourcedValue的原始`value`、证据引用内容、字符位置、证据来源与引用ID均不由该转换步骤修改。

delta既支持直接字段路径，也支持约束ID或字段路径及完整约束对象。先规范化值，再把约束路径绑定到实际ID，避免最终约束已经是`windows`，delta却仍含`Windows`或找不到匹配对象。

未知值仍走原有业务边界；原文不支持Windows时，大小写转换不会让它获得授权。Windows和Linux两个不同硬要求仍保留冲突并追问。不得以规范化为由提升软偏好、填入缺失城市或绕过预算/默认值授权。

两个保留的兜底用例在每次重放时会重新生成证据ID，这是原有重建行为；其引用内容与字符位置相同。其余22条的证据对象（含ID）完全相同。规范化函数自身不生成或修改证据ID。

## 本轮修复前后对比

“修复前”使用上轮已完成语义修复的8/24版本，不是最早4/24版本。本轮修改前先重新运行该版本，24条评分全部与上轮最终报告一致，再保存源码和来源哈希。

| 指标 | 本轮修复前 | 本轮修复后 |
|---|---:|---:|
| 严格整条通过 | 8/24（33.33%） | 24/24（100%） |
| 严格正确事实 | 149/168（88.69%） | 168/168（100%） |
| 严格错误提取 | 19 | 0 |
| 需求遗漏 | 0 | 0 |
| 无依据额外断言 | 0 | 0 |
| 证据、缺失项、状态、冲突与delta评分问题 | 0 | 0 |
| 后端兜底次数 | 2 | 2 |
| 评分退化或新增问题的用例 | — | 0 |

16条用例的城市或系统表示得到规范化；其余8条原本就通过，评分保持。16、24号仍因模型从总额擅自推导单价、生成未经服务器授权的状态而被拒绝并兜底，权限和预算门禁未放宽。

比较器核验原始proposal、输入指纹、数据集与评分器哈希一致。正式评分仍要求`Shanghai`、`linux`等规范值，不做事后别名等价计分；本轮也未更改标准答案。

## 回归与数据保留

- 新增34项回归：城市/系统别名、未知值与版本名、规则解析、最终需求与delta一致性、证据保留、旧快照不变、幂等、真实冲突、错误证据及24条严格重放。
- 完整后端测试452通过、1跳过；Ruff与diff检查通过。原有latest报告备份恢复。
- 原60条离线基线仍60/60，单独保存，不能与模型重放合并为模型准确率。
- 本轮新增模型请求0、token 0；上轮真实调用的141,704 token仅保留为历史来源测量。后端重放耗时另存，不将其解释为重新测得的模型耗时或token节省。

查看本轮制品：

- [可读对比HTML](../backend/evals/audits/requirement-canonical-values-20261023/comparison.html)
- [逐例CSV](../backend/evals/audits/requirement-canonical-values-20261023/case-comparison.csv)：包含规范化前后的城市、系统值和逐例分数。
- [完整对比JSON](../backend/evals/audits/requirement-canonical-values-20261023/comparison.json)
- [源码快照及报告索引](../backend/evals/audits/requirement-canonical-values-20261023/README.md)

从backend目录重放，无需模型凭证：

```bash
.venv/bin/python -m procure_pilot.evals.requirement_replay \
  evals/audits/requirement-holdout-v2-live/20261022T135239-22c85f553f/report.json \
  --dataset evals/datasets/requirement_holdout_v2.json \
  --output-dir evals/reports/requirements/canonical-replay
```

本轮仍是已见合成回归集，不能证明生产泛化能力；后续应冻结新的独立业务样本，检验未登记别名、地址与城市区分、跨轮修改及工具来源的真实分布，再独立运行真实模型评测。
