# ADR-0014：制度检索使用 pgvector、FTS 与 RRF

- 状态：已采用
- 日期：2026-09-09
- 决策者：ProcurePilot 项目

## 背景

ADR-0013 建立了版本化制度知识库和 PostgreSQL 全文检索基线。FTS 对制度编号、金额、角色和精确条款可解释性强，但无法独立覆盖同义表达、轻微改写和模糊查询。只用向量检索又可能削弱精确词、版本号与否定词的权重，也不能放松租户、品类和生效期边界。

pgvector 官方支持 cosine 距离与 HNSW，并明确给出和 PostgreSQL FTS 组成 hybrid search、用 Reciprocal Rank Fusion 或 cross-encoder 合并结果的方案。SQLAlchemy 适配器提供 `VECTOR(n)` 和 `cosine_distance`。

## 决策

1. `knowledge_chunks` 增加 nullable `vector(256)` 和 `embedding_model`，迁移安装 `vector` 扩展并创建 `vector_cosine_ops` HNSW 索引；已有行由显式 reindex 回填，不在 DDL 中伪造向量。
2. FTS 与向量查询必须复用相同的 `tenant_id/category_id/PUBLISHED/effective_at` 数据库预过滤，不能先跨边界召回再在应用层删除。
3. 两个通道分别取得 Top-K，再以 RRF `1/(60 + rank)` 合并。融合后使用确定性词项覆盖、RRF 归一分和 cosine similarity 重排；当前不让 LLM 决定检索排序。
4. 引用公开 `fts_rank/vector_rank/rrf_score/rerank_score/vector_similarity/retrieval_channels`，便于 Trace、评测和面试说明，不只返回一个不可解释总分。
5. Embedding 通过小型 Provider 接口注入。默认 `hash-ngram-v1` 用于离线开发和可复现评测，它是检索管线基线，不宣称具备生产语义效果。
6. 查询 embedding 失败时记录 vector channel 降级并继续 FTS；文档入库 embedding 失败则整次拒绝，避免发布半索引的新版本。
7. RAG 仍只返回依据。Policy-as-Code 负责最终决定；无有效版本、无足够相关条款或知识版本冲突继续安全停止。

## 结果

- PostgreSQL 17 + pgvector 0.8.6 已从空库升级到 Alembic `20260909_0006`，真实创建 GIN/HNSW 索引并完成双路召回。
- 小型条款标注集输出 Hit@3、MRR、无答案准确率和双路信号覆盖率；fixture 的 1.0 只作为回归门禁，不作为生产准确率。
- SQLite 测试以 JSON 保存向量并精确计算 cosine，确保 CI 无需 PostgreSQL；发布验证仍必须包含真实 PostgreSQL 路径。

## 当前限制

- 哈希 n-gram 主要提供词形与局部字符相似性，不等于语义模型；下一步需接真实 embedding provider，并用新模型 ID 完整回填。
- 256 维是当前数据库契约，更换维度需要新列或迁移，不能混写不同维度。
- 5 条 fixture 标签太小，尚未覆盖长制度、表格、跨条款问题和 hard negatives。
- HNSW 的过滤召回、`ef_search`、索引大小和延迟尚未做规模压测；数据量小的时候精确扫描也可能更合适。
- 尚无 PDF/DOCX、扫描 OCR、表格专用切块和 cross-encoder rerank。

## 参考

- [pgvector 官方 README](https://github.com/pgvector/pgvector)
- [pgvector-python SQLAlchemy 用法](https://github.com/pgvector/pgvector-python)
