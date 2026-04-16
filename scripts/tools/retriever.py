#!/usr/bin/env python3
"""
Retriever - RAG / AST 静态分析辅助文件筛选

策略：
  1. 关键词 + 路径启发式 (keyword heuristics) — 零依赖，速度最快，默认启用
  2. TF-IDF 向量检索 (tfidf)                  — 需要 scikit-learn
  3. AST 调用链分析 (ast_analysis)            — 基于 Python 内置 AST，
                                                 对 Kotlin 做正则近似

目标：给定任务 prompt，从 base_src 中返回最相关的 3-5 个文件及其内容，
      拼装成上下文字符串交给 LLMClient。
"""

import re
import os
from pathlib import Path
from typing import Optional


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

_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ui": ("activity", "fragment", "screen", "dialog", "navigation", "navhost", "ui"),
    "state": ("viewmodel", "state", "settings", "repository", "usecase", "domain", "model"),
    "resource": ("theme", "style", "color", "drawable", "values", "xml"),
}

_ACTION_HINTS = {
    "add", "remove", "delete", "update", "modify", "change", "theme", "color", "share", "notice",
    "language", "korea", "bengali", "toggle", "button", "text", "dialog", "settings",
}

# 最多喂给 LLM 的上下文字符数（约 100 K）
_DEFAULT_MAX_CONTEXT = 100_000


class Retriever:
    """
    从 base_src 目录中为给定任务筛选最相关的源码文件，
    返回可直接拼入 Prompt 的上下文字符串。
    """

    def __init__(
        self,
        base_src_dir: str | Path,
        strategy: str = "keyword",
        top_k: int = 5,
        max_context_chars: int = _DEFAULT_MAX_CONTEXT,
    ):
        """
        Args:
            base_src_dir:      data/app_name/base_src 路径
            strategy:          'keyword' | 'tfidf' | 'ast_analysis'
            top_k:             最多返回的文件数量
            max_context_chars: 最终拼接上下文的字符上限
        """
        self.base_src_dir = Path(base_src_dir)
        self.strategy = strategy
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

        if self.strategy == "tfidf":
            ranked = self._rank_tfidf(task_prompt, all_files)
        elif self.strategy == "ast_analysis":
            ranked = self._rank_ast(task_prompt, all_files)
        else:
            ranked = self._rank_keyword(task_prompt, all_files)

        candidate_size = max(20, self.top_k * 4)
        candidates = ranked[:candidate_size]

        if self._is_low_confidence(candidates):
            candidates = self._expand_candidates(candidates, ranked, all_files)

        reranked = self._rerank_by_edit_criticality(task_prompt, candidates, all_files)
        selected = self._select_with_coverage_constraints(reranked, self.top_k)
        selected_paths = [rel for rel, _, _ in selected]

        context = self._build_context([(rel, content) for rel, content, _ in selected])
        print(f"[Retriever] strategy={self.strategy}  "
              f"total_files={len(all_files)}  selected={len(selected)}  "
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

    # ----------------------------------------------------------------------- #
    #  策略 1：关键词启发式排序
    # ----------------------------------------------------------------------- #

    def _rank_keyword(
        self, task_prompt: str, files: list[tuple[str, str]]
    ) -> list[tuple[str, str, float]]:
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
        return [(rel, content, float(score)) for score, rel, content in scored]

    # ----------------------------------------------------------------------- #
    #  策略 2：TF-IDF 向量检索
    # ----------------------------------------------------------------------- #

    def _rank_tfidf(
        self, task_prompt: str, files: list[tuple[str, str]]
    ) -> list[tuple[str, str, float]]:
        """
        使用 scikit-learn TF-IDF 计算 prompt 与每个文件的余弦相似度。
        若 sklearn 未安装，自动降级为 keyword 策略。
        """
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            import numpy as np
        except ImportError:
            print("[Retriever] scikit-learn not installed, falling back to keyword strategy")
            return self._rank_keyword(task_prompt, files)

        contents = [c for _, c in files]
        corpus = [task_prompt] + contents
        vectorizer = TfidfVectorizer(max_features=8000, sublinear_tf=True)
        tfidf_matrix = vectorizer.fit_transform(corpus)

        prompt_vec = tfidf_matrix[0]
        doc_vecs   = tfidf_matrix[1:]
        sims = cosine_similarity(prompt_vec, doc_vecs).flatten()

        ranked_idx = np.argsort(sims)[::-1]
        return [(files[i][0], files[i][1], float(sims[i])) for i in ranked_idx]

    # ----------------------------------------------------------------------- #
    #  策略 3：AST 调用链分析（Kotlin 正则近似）
    # ----------------------------------------------------------------------- #

    def _rank_ast(
        self, task_prompt: str, files: list[tuple[str, str]]
    ) -> list[tuple[str, str, float]]:
        """
        对 Kotlin/Java 文件做轻量 AST 分析：
          - 提取 import 语句判断文件间依赖
          - 识别 class / function 声明
          - 结合 prompt 关键词匹配得分

        对 XML 文件采用 keyword 策略回退。
        """
        prompt_words = set(re.findall(r"[A-Za-z][a-z]+", task_prompt))

        # Step 1: 收集每个文件声明的 class / fun 名称
        declarations: dict[str, set[str]] = {}
        for rel, content in files:
            names: set[str] = set()
            names.update(re.findall(r"(?:class|object|interface)\s+(\w+)", content))
            names.update(re.findall(r"(?:fun|def|void)\s+(\w+)", content))
            declarations[rel] = names

        scored: list[tuple[float, str, str]] = []
        for rel, content in files:
            score = 0.0
            # Prompt 关键词与 class/fun 名称的交集
            for word in prompt_words:
                if any(word.lower() in name.lower() for name in declarations[rel]):
                    score += 3.0

            # Import 分析：被多个文件 import 的文件（核心文件）加权
            import_count = sum(
                1 for _, c in files if rel.replace("/", ".") in c
            )
            score += import_count * 0.5

            # 路径关键词回退
            rel_lower = rel.lower()
            for kw, pts in _PATH_SCORE_MAP:
                if kw in rel_lower:
                    score += pts * 0.5

            scored.append((score, rel, content))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(rel, content, float(score)) for score, rel, content in scored]

    def _is_low_confidence(self, candidates: list[tuple[str, str, float]]) -> bool:
        """低置信检测：分数扁平、测试文件占比高、关键角色缺失。"""
        if not candidates:
            return True

        head = candidates[: max(1, min(self.top_k, len(candidates)))]
        scores = [s for _, _, s in head]
        max_s = max(scores)
        min_s = min(scores)
        flat_scores = (max_s - min_s) <= max(0.05, abs(max_s) * 0.05)

        roles = [self._classify_role(rel, content) for rel, content, _ in head]
        test_ratio = sum(1 for r in roles if r == "test") / len(roles)
        no_core_role = not any(r in {"ui", "state", "resource"} for r in roles)

        return flat_scores or test_ratio >= 0.6 or no_core_role

    def _expand_candidates(
        self,
        candidates: list[tuple[str, str, float]],
        ranked: list[tuple[str, str, float]],
        all_files: list[tuple[str, str]],
    ) -> list[tuple[str, str, float]]:
        """低置信时扩检：扩大排名窗口 + 同目录 sibling + import 近邻。"""
        target = max(30, self.top_k * 6)
        rel_to_item = {rel: (rel, content, score) for rel, content, score in candidates}

        for rel, content, score in ranked:
            if len(rel_to_item) >= target:
                break
            rel_to_item.setdefault(rel, (rel, content, score))

        all_map = {rel: content for rel, content in all_files}
        stem_to_files: dict[str, list[str]] = {}
        for rel in all_map:
            stem = Path(rel).stem.lower()
            stem_to_files.setdefault(stem, []).append(rel)

        seed_paths = [rel for rel, _, _ in candidates[: max(1, self.top_k)]]

        # 同目录补充（优先非测试文件）
        for seed in seed_paths:
            parent = str(Path(seed).parent)
            siblings = [
                rel for rel in all_map
                if str(Path(rel).parent) == parent and rel not in rel_to_item
            ]
            siblings.sort(key=lambda x: ("test" in x.lower(), x))
            for sib in siblings[:2]:
                if len(rel_to_item) >= target:
                    break
                rel_to_item[sib] = (sib, all_map[sib], 0.0)

        # import 邻居补充
        for seed in seed_paths:
            content = all_map.get(seed, "")
            imports = re.findall(r"^\s*import\s+([\w\.]+)", content, flags=re.MULTILINE)
            for imp in imports:
                cls = imp.split(".")[-1].lower()
                for rel in stem_to_files.get(cls, []):
                    if rel in rel_to_item:
                        continue
                    if len(rel_to_item) >= target:
                        break
                    rel_to_item[rel] = (rel, all_map[rel], 0.0)

        return list(rel_to_item.values())

    def _rerank_by_edit_criticality(
        self,
        task_prompt: str,
        candidates: list[tuple[str, str, float]],
        all_files: list[tuple[str, str]],
    ) -> list[tuple[str, str, float]]:
        """将“相关性”重排为“可编辑关键性”，提升关键文件召回稳定性。"""
        if not candidates:
            return []

        prompt_tokens = set(re.findall(r"[a-z]+", task_prompt.lower()))
        prompt_hints = {w for w in prompt_tokens if len(w) >= 4} | _ACTION_HINTS

        decl_cache: dict[str, set[str]] = {}
        for rel, content in all_files:
            names = set(re.findall(r"(?:class|object|interface|enum)\s+(\w+)", content))
            names.update(re.findall(r"(?:fun|def|void)\s+(\w+)", content))
            decl_cache[rel] = {n.lower() for n in names}

        base_vals = [score for _, _, score in candidates]
        max_base = max(base_vals) if base_vals else 1.0
        min_base = min(base_vals) if base_vals else 0.0
        span = max(1e-6, max_base - min_base)

        reranked: list[tuple[str, str, float]] = []
        for rel, content, base_score in candidates:
            rel_lower = rel.lower()
            role = self._classify_role(rel, content)

            norm_base = (base_score - min_base) / span

            path_bonus = 0.0
            for kw, pts in _PATH_SCORE_MAP:
                if kw in rel_lower:
                    path_bonus += pts
            path_bonus = min(path_bonus / 20.0, 1.0)

            decls = decl_cache.get(rel, set())
            symbol_hits = sum(1 for tok in prompt_hints if any(tok in d for d in decls))
            symbol_bonus = min(symbol_hits / 5.0, 1.0)

            editability = 1.0 if re.search(r"\b(class|object|interface|enum|fun|void)\b", content) else 0.0

            role_bonus = {
                "ui": 1.0,
                "state": 0.9,
                "resource": 0.8,
                "other": 0.5,
                "test": 0.1,
            }.get(role, 0.5)

            final = (
                0.55 * norm_base
                + 0.15 * path_bonus
                + 0.15 * symbol_bonus
                + 0.10 * role_bonus
                + 0.05 * editability
            )
            reranked.append((rel, content, final))

        reranked.sort(key=lambda x: x[2], reverse=True)
        return reranked

    def _select_with_coverage_constraints(
        self,
        ranked: list[tuple[str, str, float]],
        k: int,
    ) -> list[tuple[str, str, float]]:
        """覆盖约束：优先保证 ui/state/resource 至少各 1 个（如果存在）。"""
        if not ranked:
            return []

        selected: list[tuple[str, str, float]] = []
        used: set[str] = set()

        def add_first_for_role(role: str) -> None:
            for rel, content, score in ranked:
                if rel in used:
                    continue
                if self._classify_role(rel, content) == role:
                    selected.append((rel, content, score))
                    used.add(rel)
                    return

        for role in ("ui", "state", "resource"):
            add_first_for_role(role)
            if len(selected) >= k:
                return selected[:k]

        # 兜底补齐：先非测试，再测试
        for rel, content, score in ranked:
            if rel in used:
                continue
            if self._classify_role(rel, content) != "test":
                selected.append((rel, content, score))
                used.add(rel)
                if len(selected) >= k:
                    return selected[:k]

        for rel, content, score in ranked:
            if rel in used:
                continue
            selected.append((rel, content, score))
            used.add(rel)
            if len(selected) >= k:
                break

        return selected[:k]

    def _classify_role(self, rel: str, content: str) -> str:
        rel_lower = rel.lower()
        if (
            "test" in rel_lower
            or rel_lower.endswith("test.kt")
            or rel_lower.endswith("test.java")
        ):
            return "test"

        for role, kws in _ROLE_KEYWORDS.items():
            if any(kw in rel_lower for kw in kws):
                return role

        # 内容弱特征兜底
        if re.search(r"@Composable|Fragment|Activity|NavHost", content):
            return "ui"
        if re.search(r"ViewModel|StateFlow|Repository|UseCase", content):
            return "state"
        if "<resources" in content or "<style" in content:
            return "resource"

        return "other"

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
