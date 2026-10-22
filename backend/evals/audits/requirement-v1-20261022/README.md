# 需求评测 v1 修复归档

本目录是2026-10-22选定的离线评测快照，包含修复前、逐组修复及最终报告、当时的Prompt、比较结果和修复前后服务源码。数据为合成采购用例；没有请求模型，token字段为null。60/60仅代表已见回归集上的规则解析及校验结果。

完整指标与SHA-256见[审计记录](../requirement-baseline-v1-fix-review.json)，业务解释见[失败分析](../../../../docs/22-requirement-baseline-failure-analysis.md)。报告按原始字节保存，内部路径和Git版本记录保留实验运行时状态；源码指纹用于区分当时未提交的实现。源码快照以`.py.txt`保存，供核对，不参与运行。

日常生成目录`backend/evals/reports/requirements/`仍被Git忽略，本目录只归档已选定的历史证据。后续实验应创建新的目录，不能覆盖本次快照。
