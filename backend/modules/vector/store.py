"""VectorStore - ChromaDB 持久化向量存储

包装 ChromaDB PersistentClient，提供懒初始化、增删改查和批量重建功能。
遵循"构造时零开销"原则：Chromadb 连接延迟到首次使用时触发。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class VectorStore:
    """基于 ChromaDB 的持久化向量存储。

    使用懒初始化策略：
    - __init__ 只保存配置，不连接 ChromaDB
    - 首次访问 available 属性时触发 _ensure_initialized()
    - chromadb 未安装时标记不可用；embedding API 异常直接抛
    """

    def __init__(
        self,
        persist_dir: Path,
        embedding_function: Any,
        collection_name: str = "memory",
    ):
        """初始化 VectorStore 配置（不连接 ChromaDB）。

        Args:
            persist_dir: ChromaDB 持久化数据目录
            embedding_function: ChromaDB EmbeddingFunction 实例（如 TencentEmbedding）
            collection_name: 集合名称
        """
        self._persist_dir = Path(persist_dir)
        self._collection_name = collection_name
        self._embedding_function = embedding_function

        # 懒初始化状态
        self._client: Any = None
        self._collection: Any = None
        self._available: Optional[bool] = None

    # ── 公共属性 ──────────────────────────────────────────

    @property
    def available(self) -> bool:
        """向量存储是否可用。

        首次访问时触发懒初始化。chromadb 未安装时返回 False，
        embedding API 异常直接抛（不静默降级）。
        """
        if self._available is None:
            self._ensure_initialized()
        return bool(self._available)

    # ── 懒初始化 ──────────────────────────────────────────

    def _ensure_initialized(self) -> bool:
        """懒初始化 ChromaDB 连接。

        只在首次调用时执行。后续调用直接返回缓存的可用状态。
        chromadb ImportError 会标记不可用；
        embedding 初始化异常直接抛出，不静默吞掉。

        Returns:
            True 如果初始化成功
        """
        if self._client is not None:
            return True

        # 导入 chromadb（可选依赖）
        try:
            import chromadb  # noqa: F811
        except ImportError:
            logger.debug("chromadb not installed, vector search unavailable")
            self._available = False
            return False

        # 连接 ChromaDB 并创建/获取集合
        # 注意：embedding 相关异常（网络、认证等）直接抛出，不做静默降级
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._persist_dir))

        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=self._embedding_function,
            metadata={"hnsw:space": "cosine"},
        )

        count = self._collection.count()
        self._available = True
        logger.info(
            f"Vector store initialized: {self._persist_dir} "
            f"(collection={self._collection_name}, entries={count})"
        )
        return True

    # ── CRUD 操作 ─────────────────────────────────────────

    def count(self) -> int:
        """获取已索引条目数。"""
        if not self._collection:
            return 0
        try:
            return self._collection.count()
        except Exception as e:
            logger.warning(f"Vector store count failed: {e}")
            return 0

    def add_entry(
        self,
        entry_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加单条记录到向量索引。

        Args:
            entry_id: 唯一标识（Memory 场景为行号字符串）
            text: 要索引的文本
            metadata: 可选的元数据字典

        Returns:
            True 如果添加成功
        """
        if not self._ensure_initialized():
            return False

        try:
            self._collection.add(
                ids=[entry_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
            return True
        except Exception as e:
            logger.warning(f"Vector store add_entry failed (id={entry_id}): {e}")
            return False

    def add_entries_batch(
        self,
        entries: List[Dict[str, Any]],
        batch_size: int = 100,
    ) -> int:
        """批量添加记录到向量索引。

        Args:
            entries: 记录列表，每项需含 "id"(str), "text"(str), 可选 "metadata"(dict)
            batch_size: 每批处理的条目数

        Returns:
            成功添加的条目数
        """
        if not entries:
            return 0
        if not self._ensure_initialized():
            return 0

        total_added = 0
        for i in range(0, len(entries), batch_size):
            batch = entries[i:i + batch_size]
            try:
                ids = [e["id"] for e in batch]
                docs = [e["text"] for e in batch]
                metas = [e.get("metadata", {}) for e in batch]

                self._collection.add(ids=ids, documents=docs, metadatas=metas)
                total_added += len(batch)
            except Exception as e:
                logger.warning(f"Vector store batch add failed (batch {i // batch_size}): {e}")

        return total_added

    def search(
        self,
        query: str,
        n_results: int = 15,
    ) -> List[Dict[str, Any]]:
        """语义搜索。

        注意：embedding API 调用可能抛异常（网络、认证等），不做静默降级。

        Args:
            query: 搜索查询文本
            n_results: 最大返回结果数

        Returns:
            结果列表，每项含 id, document, metadata, distance
        """
        if not self._ensure_initialized():
            return []

        result = self._collection.query(
            query_texts=[query],
            n_results=min(n_results, self._collection.count()),
        )

        # ChromaDB 返回嵌套列表格式：
        # {"ids": [["id1", "id2"]], "documents": [["doc1", "doc2"]], ...}
        # 展开为扁平的 dict 列表
        results: List[Dict[str, Any]] = []
        if result and result.get("ids") and result["ids"][0]:
            for i, doc_id in enumerate(result["ids"][0]):
                item = {
                    "id": doc_id,
                    "document": (result.get("documents") or [[""]])[0][i] if result.get("documents") else "",
                    "metadata": (result.get("metadatas") or [{}])[0][i] if result.get("metadatas") else {},
                    "distance": (result.get("distances") or [[0.0]])[0][i] if result.get("distances") else 0.0,
                }
                results.append(item)

        return results

    def delete_entry(self, entry_id: str) -> bool:
        """删除单条记录。"""
        if not self._collection:
            return False
        try:
            self._collection.delete(ids=[entry_id])
            return True
        except Exception as e:
            logger.warning(f"Vector store delete_entry failed (id={entry_id}): {e}")
            return False

    def rebuild_from_lines(
        self,
        lines: List[str],
    ) -> int:
        """从文本行重建整个向量索引。

        删除现有集合并重新创建，然后批量索引所有行。
        行格式: "date|source|content"（Memory MEMORY.md 格式）。

        embedding API 异常直接抛出。
        """
        if not self._ensure_initialized():
            return 0

        try:
            import chromadb
        except ImportError:
            return 0

        # 删除并重建集合
        try:
            self._client.delete_collection(name=self._collection_name)
        except Exception:
            pass  # 集合可能不存在

        self._collection = self._client.create_collection(
            name=self._collection_name,
            embedding_function=self._embedding_function,
            metadata={"hnsw:space": "cosine"},
        )

        # 解析行并批量索引
        entries = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            entry_id = str(i + 1)
            parts = line.split("|", 2)
            date_str = parts[0] if len(parts) > 0 else ""
            source = parts[1] if len(parts) > 1 else ""
            content = parts[2] if len(parts) > 2 else line

            entries.append({
                "id": entry_id,
                "text": line,
                "metadata": {
                    "line_number": i + 1,
                    "source": source,
                    "date": date_str,
                },
            })

        count = self.add_entries_batch(entries)
        logger.info(f"Vector store rebuilt: {count} entries indexed from {len(lines)} lines")
        self._available = True
        return count
