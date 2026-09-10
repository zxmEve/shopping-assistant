# ADR-0015：制度文件解析与 Embedding 生命周期

- 状态：已采用
- 日期：2026-09-10

## 背景

M3.2 只接受已经提取好的纯文本，并用离线哈希向量建立可重复基线。真实制度通常来自 PDF、DOCX 或扫描件；如果只保存提取文本，就无法证明解析结果来自哪个文件，也无法在解析器升级后复现。若直接覆盖旧向量，模型切换期间还会静默混用不同向量空间。

## 决策

1. 上传只接受 PDF、DOCX、UTF-8 文本和 Markdown，同时校验文件名扩展、MIME 和 PDF/DOCX 签名；限制原文件大小、PDF 页数、DOCX entry 数与解压后体积。
2. 每个 Knowledge Version 保留不可变原文件二进制、SHA-256、文件名、MIME、大小、parser version，以及页数、表格数、OCR 页、警告和提取字符数。API 只返回元数据，不回传二进制。
3. PDF 优先提取文字层；页面文字过少且包含图片时，未配置 OCR 明确返回 `OCR_REQUIRED`，不把空白结果发布。Tesseract 是可替换的本地 OCR 实现，默认关闭。
4. DOCX 按文档顺序遍历标题、段落和顶层表格；表格转为 Markdown，避免把单元格完全拼成无结构文本。解析只做版式清理，不改写金额、日期、否定词或审批角色。
5. embedding provider 与 Agent 模型使用不同配置和密钥。开发/CI 默认 `hash-ngram-v1`；托管语义向量使用 OpenAI Embeddings API，固定存储维度 256，模型身份写入每个 chunk。
6. 回填只选取同租户、已发布且 `embedding_model` 缺失或不等于目标模型的 chunk。每批在一个数据库事务提交，支持 dry-run、最大批数和重复执行；失败后从剩余旧模型记录继续。
7. 查询向量失败时继续使用 FTS 并暴露降级原因；入库或回填向量形状错误时拒绝整批，不能写入部分错误索引。

## 结果

- 原文件与提取结果可追溯，解析器升级后可以从原文件重建。
- 扫描 PDF 不会被静默当作空制度，OCR 故障也不会产生半成品版本。
- 新旧 embedding 由模型 ID 显式区分，批次级事务使回填可恢复。
- PostgreSQL 会保存最多 10 MiB 的原文件，适合当前演示和面试项目；生产应把二进制迁移到带对象锁、加密和保留策略的对象存储，数据库只保存内容寻址 URI 与哈希。

## 当前限制

- OpenAI provider 已通过协议级 fake client 测试，但还没有用真实企业制度调用线上 embedding 建立质量、延迟和费用基线。
- Docker 镜像已验证 Tesseract `chi_sim+eng` 依赖和英文扫描 PDF OCR smoke；中文复杂扫描件的字符准确率仍需人工标注评测。
- 合并单元格、多栏 PDF、脚注、附件、手写扫描件与超大文档需要专门版式评测。
- 12 条标签只验证小型 fixture 的 development/holdout 门禁，不能代表生产 citation precision/recall。
