# 需求校验修复证据

本目录使用同一批原始模型输出做验证层重放，不调用模型。

- [最终对比JSON](comparison.json)、[逐例CSV](case-comparison.csv)、[可读HTML](comparison.html)。
- [来源锁定记录](source-lock.json)、[最终报告索引](final-index.json)、[检查记录](validation-checks.json)。
- 修复前源码与修复后校验器/证据解析器以`.py.txt`保存，不参与运行。
- `before-replay/`使用匹配原始live源码哈希的旧校验器，24条的最终评分和事实全部复现。
- `after-replay/`与`final-replay/`保留各次独立运行；仅以`final-index.json`指向的结果为最终结果。调试中间版本曾使“保留16GB硬下限、增加32GB软偏好”的用例退化，该问题已修复并增加回归。
- `legacy-regression/`为原60条基线的修复后运行结果。所有报告按实验ID单独保存，没有覆盖原始live报告。

复算展示文件：在项目根目录执行`python3 backend/evals/audits/requirement-holdout-v2-fixes/build_comparison.py.txt`，无需模型。

详细根因、实现和复现命令见[修复说明](../../../../docs/24-requirement-validation-fixes.md)。
