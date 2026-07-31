"""向量数据库模块

提供基于 ChromaDB + 腾讯云 Embedding 的语义搜索能力。

核心组件:
- TencentEmbedding: 腾讯云 Embedding API 封装
- VectorStore: 向量存储（ChromaDB 持久化）
- load_embedding_from_keys: 从 keys.json 加载 embedding 配置
"""

from .store import VectorStore
from .embedding import TencentEmbedding, load_embedding_from_keys

__all__ = ["VectorStore", "TencentEmbedding", "load_embedding_from_keys"]
