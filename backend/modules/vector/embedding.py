"""Embedding 函数

提供腾讯云 Embedding 服务（OpenAI 兼容格式），通过 httpx 调用远程 API。
API 调用失败直接抛异常，不做静默降级。
"""

from typing import List

import httpx
from loguru import logger

# 尝试继承 ChromaDB EmbeddingFunction 协议（如果 chromadb 已安装），
# 否则降级为普通类（不影响模块加载）
try:
    from chromadb.api.types import EmbeddingFunction as _ChromaEF
except ImportError:
    _ChromaEF = object  # type: ignore


class TencentEmbedding(_ChromaEF):  # type: ignore
    """腾讯云 Embedding Function。

    调用 tokenhub.tencentmaas.com 的 /v1/embeddings 端点，
    格式兼容 OpenAI Embedding API。
    实现 ChromaDB EmbeddingFunction 协议，可直接传入 ChromaDB Collection。

    API 调用失败直接抛异常，不做静默降级。
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://tokenhub.tencentmaas.com/v1/embeddings",
        model_name: str = "kinfra-text-embedding-4b",
        timeout: float = 30.0,
    ):
        """初始化腾讯云 Embedding 函数。

        Args:
            api_key: API 密钥（Bearer Token）
            api_url: Embedding API 端点
            model_name: 模型名称
            timeout: 单次请求超时秒数
        """
        self._api_url = api_url
        self._api_key = api_key
        self._model_name = model_name
        self._timeout = timeout

    def name(self) -> str:
        """ChromaDB 要求的 embedding function 名称。"""
        return f"tencent/{self._model_name}"

    def __call__(self, input: List[str]) -> List[List[float]]:
        """对文本列表进行 embedding。

        Args:
            input: 待向量化的文本列表

        Returns:
            向量列表，每个向量为 float 列表（2560 维）

        Raises:
            httpx.HTTPError: API 请求失败
            ValueError: 响应格式异常
        """
        resp = httpx.post(
            self._api_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "input": input,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "data" not in data:
            raise ValueError(f"Unexpected embedding response: missing 'data' field. Keys: {list(data.keys())}")

        embeddings = []
        for item in data["data"]:
            emb = item.get("embedding")
            if emb is None:
                raise ValueError(f"Embedding item missing 'embedding' field: {item}")
            embeddings.append(emb)

        return embeddings


def load_embedding_from_keys(keys_path: str = "config/keys.json") -> TencentEmbedding:
    """从 keys.json 文件加载腾讯云 Embedding 配置并创建实例。

    keys.json 格式:
    {
        "tencent_embedding": {
            "api_key": "sk-xxx",
            "api_url": "https://tokenhub.tencentmaas.com/v1/embeddings",
            "model": "kinfra-text-embedding-4b"
        }
    }

    Args:
        keys_path: keys.json 文件路径

    Returns:
        TencentEmbedding 实例

    Raises:
        FileNotFoundError: keys.json 不存在
        KeyError: 缺少 tencent_embedding 配置
        ValueError: api_key 为空
    """
    import json
    from pathlib import Path

    path = Path(keys_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Keys config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        keys = json.load(f)

    cfg = keys.get("tencent_embedding")
    if not cfg:
        raise KeyError("Missing 'tencent_embedding' section in keys.json")

    api_key = cfg.get("api_key", "").strip()
    if not api_key:
        raise ValueError("tencent_embedding.api_key is empty in keys.json")

    ef = TencentEmbedding(
        api_key=api_key,
        api_url=cfg.get("api_url", "https://tokenhub.tencentmaas.com/v1/embeddings"),
        model_name=cfg.get("model", "kinfra-text-embedding-4b"),
    )
    logger.info(f"Loaded TencentEmbedding: model={ef._model_name}, url={ef._api_url}")
    return ef
