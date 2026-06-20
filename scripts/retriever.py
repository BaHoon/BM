#!/usr/bin/env python3
"""
Retriever - RAG 辅助文件筛选

给定任务 prompt，从 base_src 中筛选最相关的源码文件，
拼装成上下文字符串交给 LLMClient。
"""

import re
from pathlib import Path


# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #

# 按文件扩展名决定收集优先级
_SUPPORTED_EXTS = {".kt", ".java", ".xml", ".gradle", ".json"}

# 路径关键词 → 主题相关性分数（越高越优先）
_PATH_SCORE_MAP: list[tuple[str, int]] = [
    ("theme",    10),
    ("color",    9),
    ("style",    8),
    ("drawable", 7),
    ("values",   6),
    ("ui",       5),
    ("activity", 4),
    ("fragment", 4),
    ("settings", 3),
    ("adapter",  3),
    ("viewmodel",2),
    ("model",    2),
    ("util",     1),
]

# 最多喂给 LLM 的上下文字符数（约 100 K）
_DEFAULT_MAX_CONTEXT = 100_000


class Retriever:
    """
    使用路径关键词和 prompt 词命中数，为任务筛选相关源码文件。
    """

    def __init__(
        self,
        base_src_dir: str | Path,
        top_k: int = 8,
        max_context_chars: int = _DEFAULT_MAX_CONTEXT,
    ):
        """
        Args:
            base_src_dir:      data/app_name/base_src 路径
            top_k:             最多返回的文件数量（默认 8）
            max_context_chars: 最终拼接上下文的字符上限
        """
        self.base_src_dir = Path(base_src_dir)
        self.top_k = top_k
        self.max_context_chars = max_context_chars

        if not self.base_src_dir.exists():
            raise FileNotFoundError(f"base_src not found: {self.base_src_dir}")

    # ----------------------------------------------------------------------- #
    #  公开接口
    # ----------------------------------------------------------------------- #

    def retrieve(self, task_prompt: str) -> tuple[list[str], str]:
        """
        检索与任务最相关的文件。

        Args:
            task_prompt: meta.json 中的 prompt 字符串

        Returns:
            (selected_paths, context_string)
              - selected_paths: 相对于 base_src_dir 的文件路径列表
              - context_string: 已格式化好的 Markdown 代码块上下文
        """
        all_files = self._collect_all_files()
        ranked = self._rank_keyword(task_prompt, all_files)
        selected = ranked[: self.top_k]
        selected_paths = [rel for rel, _ in selected]

        context = self._build_context(selected)
        print(f"[Retriever] total_files={len(all_files)}  selected={len(selected)}  "
              f"context_chars={len(context)}")
        return selected_paths, context

    # ----------------------------------------------------------------------- #
    #  文件收集
    # ----------------------------------------------------------------------- #

    def _collect_all_files(self) -> list[tuple[str, str]]:
        """
        遍历 base_src_dir，收集所有支持类型的文件。

        Returns:
            [(relative_path_str, file_content)] 列表
        """
        files: list[tuple[str, str]] = []
        for fpath in self.base_src_dir.rglob("*"):
            if not fpath.is_file():
                continue
            # 忽略 build / .gradle cache 目录
            parts = set(fpath.parts)
            if parts & {"build", ".gradle", ".idea", ".kotlin", "cache"}:
                continue
            if fpath.suffix.lower() not in _SUPPORTED_EXTS:
                continue
            rel = str(fpath.relative_to(self.base_src_dir))
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                files.append((rel, content))
            except Exception:
                pass
        return files

    def _rank_keyword(
        self, task_prompt: str, files: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """
        基于路径关键词 + Prompt 词命中数对文件打分并排序。
        """
        prompt_words = set(re.findall(r"[a-z]+", task_prompt.lower()))

        scored: list[tuple[float, str, str]] = []
        for rel, content in files:
            rel_lower = rel.lower()
            score = 0.0

            # 路径关键词得分
            for kw, pts in _PATH_SCORE_MAP:
                if kw in rel_lower:
                    score += pts

            # Prompt 词在文件内容中的命中数（TF 近似）
            content_lower = content.lower()
            for word in prompt_words:
                if len(word) >= 4:  # 忽略过短词
                    score += content_lower.count(word) * 0.05

            # 优先主源码目录
            if "app/src/main" in rel or "shared" in rel.lower():
                score += 2.0

            scored.append((score, rel, content))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(rel, content) for _, rel, content in scored]

    # ----------------------------------------------------------------------- #
    #  上下文拼装
    # ----------------------------------------------------------------------- #

    def _build_context(self, ranked: list[tuple[str, str]]) -> str:
        """
        将排好序的文件列表拼装为 Markdown 代码块字符串，
        超过 max_context_chars 时截断并提示。
        """
        parts: list[str] = []
        total = 0
        for rel, content in ranked:
            block = f"\n## File: {rel}\n```\n{content}\n```\n"
            if total + len(block) > self.max_context_chars:
                parts.append("\n... (remaining files omitted due to context size limit) ...\n")
                break
            parts.append(block)
            total += len(block)
        return "".join(parts)
