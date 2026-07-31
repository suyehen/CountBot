"""Memory Store - 记忆存储管理

记忆格式: 每行一条，格式为
  日期|来源|内容事项1；事项2；事项3

示例:
  2026-02-15|web-chat|用户询问天气API方案；决定使用OpenWeatherMap；缓存策略选Redis TTL=3600s
  2026-02-15|telegram|用户要求每天早上9点发送日报；已创建cron任务

支持功能:
- 写入记忆（追加一行）
- 关键词搜索（单词/多词，返回匹配行号和内容）
- 按行号读取（支持单行和范围）
- 对话自动总结写入
"""

from typing import Dict, List, Optional, TYPE_CHECKING
from datetime import datetime
from pathlib import Path

from loguru import logger

if TYPE_CHECKING:
    from backend.modules.vector.store import VectorStore


class MemoryStore:
    """记忆存储 - 基于单文件的行式记忆管理

    可选集成向量存储（VectorStore）以支持语义搜索：
    - 传入 vector_store 后，写入时自动索引到向量库
    - 搜索时优先使用向量语义搜索，失败/不可用时回退关键词搜索
    - 未传入 vector_store 时行为与原有完全一致
    """

    def __init__(self, memory_dir: Path, vector_store: Optional["VectorStore"] = None):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "MEMORY.md"
        self._vector_store = vector_store
        logger.debug(f"MemoryStore initialized: {self.memory_file}"
                     f"{' (with vector store)' if vector_store else ''}")

    def _read_lines(self) -> List[str]:
        """读取所有记忆行"""
        if not self.memory_file.exists():
            return []
        content = self.memory_file.read_text(encoding="utf-8")
        if not content.strip():
            return []
        return content.strip().split("\n")

    def _write_lines(self, lines: List[str]) -> None:
        """写入所有记忆行"""
        self.memory_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read_all(self) -> str:
        """读取全部记忆内容"""
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8")

    def write_all(self, content: str) -> None:
        """覆盖写入全部记忆"""
        self.memory_file.write_text(content, encoding="utf-8")

    def get_line_count(self) -> int:
        """获取记忆总行数"""
        return len(self._read_lines())

    def append_entry(self, source: str, content: str) -> int:
        """追加一条记忆

        Args:
            source: 来源（web-chat, telegram, dingtalk, feishu, cron 等）
            content: 记忆内容（多个事项用；分隔）

        Returns:
            int: 写入后的行号（1-based）
        """
        date_str = datetime.now().strftime("%Y-%m-%d")
        
        # 清理内容：移除换行符，避免破坏行式存储格式
        content = content.replace('\n', ' ').replace('\r', ' ')
        # 压缩多个空格为一个
        content = ' '.join(content.split())
        
        entry = f"{date_str}|{source}|{content}"

        lines = self._read_lines()
        lines.append(entry)
        self._write_lines(lines)

        line_num = len(lines)
        logger.info(f"Memory appended at line {line_num}: {entry[:80]}...")

        # 同步写入向量索引（可选）
        if self._vector_store is not None:
            self._vector_store.add_entry(
                entry_id=str(line_num),
                text=entry,
                metadata={
                    "line_number": line_num,
                    "source": source,
                    "date": date_str,
                },
            )

        return line_num

    def read_lines(self, start: int, end: Optional[int] = None) -> str:
        """按行号读取记忆

        Args:
            start: 起始行号（1-based）
            end: 结束行号（1-based，包含），None 表示只读 start 那一行

        Returns:
            str: 格式化的行内容，每行带行号前缀
        """
        lines = self._read_lines()
        total = len(lines)

        if total == 0:
            return "记忆为空"

        if end is None:
            end = start

        # 边界检查
        start = max(1, min(start, total))
        end = max(start, min(end, total))

        result = []
        for i in range(start - 1, end):
            result.append(f"[{i + 1}] {lines[i]}")

        return "\n".join(result)

    def search(self, keywords: List[str], max_results: int = 15, match_mode: str = "or") -> str:
        """搜索记忆（优先语义搜索，回退关键词搜索）

        支持单词和多词搜索，可选择AND或OR逻辑（仅关键词回退时生效）。
        搜索不区分大小写。

        如果配置了向量存储且可用，优先使用语义搜索；
        向量搜索失败或不可用时自动回退到关键词搜索。

        Args:
            keywords: 关键词列表
            max_results: 最大返回条数
            match_mode: 匹配模式，"or"（任意匹配）或"and"（全部匹配），默认"or"
                       仅对关键词回退生效；语义搜索不需要此参数

        Returns:
            str: 格式化的搜索结果，每行带行号前缀
        """
        # 优先尝试向量语义搜索
        if self._vector_store is not None and self._vector_store.available:
            return self._vector_search(keywords, max_results)

        # ── 关键词搜索回退（原有逻辑）──
        lines = self._read_lines()
        if not lines:
            return "记忆为空，无搜索结果"

        if not keywords:
            return "请提供搜索关键词"

        # 清理关键词
        keywords = [kw.strip().lower() for kw in keywords if kw.strip()]
        if not keywords:
            return "请提供有效的搜索关键词"

        results = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            
            # 根据匹配模式选择逻辑
            if match_mode == "and":
                # AND 逻辑：所有关键词都必须匹配
                if all(kw in line_lower for kw in keywords):
                    results.append(f"[{i + 1}] {line}")
            else:
                # OR 逻辑：任意关键词匹配即可
                if any(kw in line_lower for kw in keywords):
                    results.append(f"[{i + 1}] {line}")

        if not results:
            mode_text = "任意" if match_mode == "or" else "全部"
            return f"未找到包含 {mode_text} 关键词 {', '.join(keywords)} 的记忆"

        # 限制结果数
        if len(results) > max_results:
            total_found = len(results)
            results = results[:max_results]
            results.append(f"... 共 {total_found} 条匹配，仅显示前 {max_results} 条")

        return "\n".join(results)

    def delete_lines(self, line_numbers: List[int]) -> int:
        """删除指定行号的记忆

        Args:
            line_numbers: 要删除的行号列表（1-based）

        Returns:
            int: 实际删除的行数
        """
        lines = self._read_lines()
        if not lines:
            return 0

        to_delete = set(line_numbers)
        new_lines = [
            line for i, line in enumerate(lines)
            if (i + 1) not in to_delete
        ]

        deleted = len(lines) - len(new_lines)
        if deleted > 0:
            self._write_lines(new_lines)
            logger.info(f"Deleted {deleted} memory lines: {line_numbers}")

        return deleted

    def get_recent(self, count: int = 10) -> str:
        """获取最近 N 条记忆

        Args:
            count: 返回条数

        Returns:
            str: 格式化的最近记忆
        """
        lines = self._read_lines()
        if not lines:
            return "记忆为空"

        start = max(0, len(lines) - count)
        result = []
        for i in range(start, len(lines)):
            result.append(f"[{i + 1}] {lines[i]}")

        return "\n".join(result)

    def _vector_search(self, keywords: List[str], max_results: int) -> str:
        """向量语义搜索记忆。

        将关键词合并为自然语言查询，调用 VectorStore.search()，
        结果格式化为与关键词搜索一致的 [line_num] content 格式。

        Args:
            keywords: 关键词列表
            max_results: 最大返回结果数

        Returns:
            str: 格式化的搜索结果
        """
        query = " ".join(keywords)
        results = self._vector_store.search(query, n_results=max_results)

        if not results:
            return f"未找到与 '{', '.join(keywords)}' 语义相关的记忆"

        lines_out = []
        for r in results:
            line_num = r.get("metadata", {}).get("line_number", r.get("id", "?"))
            content = r.get("document", "")
            lines_out.append(f"[{line_num}] {content}")

        return "\n".join(lines_out) if lines_out else f"未找到与 '{', '.join(keywords)}' 语义相关的记忆"

    def rebuild_vector_index(self) -> dict:
        """从 MEMORY.md 重建向量索引。

        适用于首次启用向量搜索或索引损坏后恢复的场景。

        Returns:
            dict: {"status": "ok"|"skipped", "count": int, "total_lines": int}
        """
        if self._vector_store is None:
            return {"status": "skipped", "reason": "vector store not configured"}

        lines = self._read_lines()
        count = self._vector_store.rebuild_from_lines(lines)
        return {"status": "ok", "count": count, "total_lines": len(lines)}

    def get_stats(self) -> dict:
        """获取记忆统计信息"""
        lines = self._read_lines()
        total = len(lines)

        if total == 0:
            return {"total": 0, "sources": {}, "date_range": ""}

        # 统计来源分布
        sources: Dict[str, int] = {}
        dates: List[str] = []
        for line in lines:
            parts = line.split("|", 2)
            if len(parts) >= 2:
                dates.append(parts[0])
                src = parts[1]
                sources[src] = sources.get(src, 0) + 1

        date_range = ""
        if dates:
            date_range = f"{dates[0]} ~ {dates[-1]}"

        return {
            "total": total,
            "sources": sources,
            "date_range": date_range,
        }
