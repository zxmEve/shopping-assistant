# 后端规范值修复归档（2026-10-23）

- [完整对比JSON](comparison.json)、[逐例CSV](case-comparison.csv)、[可读HTML](comparison.html)。
- [修复前来源锁定](source-lock.json)、[最终报告索引](final-index.json)、[回归检查](validation-checks.json)。
- `before-replay/`为本轮修改前重新执行的8/24结果，24条均与上轮最终报告复现一致。
- `after-replay/`为规范值修复后的原严格评分结果24/24。两列不是重新采样模型。
- `legacy-regression/`保存原60条基线回归；`.py.txt`保存修复前后源码。
- 原始live报告、上轮8/24报告及上轮对比均保持原始字节；本轮报告在新目录保存。

在项目根目录执行`python3 backend/evals/audits/requirement-canonical-values-20261023/build_comparison.py.txt`可复算本次展示文件，无需模型。

详见[实现与验证说明](../../../../docs/25-requirement-canonical-values.md)。
