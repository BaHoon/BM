#!/usr/bin/env python3
"""Utilities for Level 2 target-page XML scoring."""

from __future__ import annotations

import re
import difflib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


GENERIC_SCORE_TERMS = {
    "setting", "settings", "preference", "preferences", "option", "options",
    "menu", "page", "screen", "button", "app", "application",
    "设置", "菜单", "页面", "按钮", "应用",
}

NAVIGATION_TERMS = [
    "settings", "setting", "preferences", "options", "menu", "more",
    "go to settings", "open settings",
    "设置", "前往设置", "菜单", "更多", "偏好", "选项",
]

# Prompt concepts that commonly name an intermediate settings row.  These are
# navigation hints only; they never count as a Level 2 target-node match.
PROMPT_NAV_CONCEPTS = [
    "theme", "appearance", "color scheme", "display", "layout", "font",
    "navigation", "toolbar", "language", "notification",
    "account", "backup", "sync", "calendar", "editor",
    "主题", "外观", "配色", "显示", "布局", "字体", "导航", "工具栏", "语言",
    "通知", "账户", "备份", "同步", "日历", "编辑器",
]

POPUP_TERMS = [
    "ok", "okay", "allow", "continue", "skip", "agree", "close", "cancel", "later",
    "i've been here before", "let's do it", "get started", "next", "not now",
    "no thanks", "maybe later", "start browsing", "finish",
    "确定", "確定", "好的", "允许", "继续", "跳过", "同意", "关闭", "取消", "稍后", "关闭工作表",
]

NON_TARGET_TERMS = GENERIC_SCORE_TERMS | {
    term.lower() for term in NAVIGATION_TERMS + POPUP_TERMS
} | {"the", "and", "for", "with", "overall", "current", "new", "users", "user"}


def build_level2_spec(
    meta: dict[str, Any],
    base_src: Path | None = None,
    golden_src: Path | None = None,
) -> dict[str, Any]:
    """Build a lightweight target-node spec from task metadata only."""
    prompt = str(meta.get("prompt", ""))
    explicit = meta.get("level2_target_keywords") or meta.get("target_keywords") or []
    if isinstance(explicit, str):
        explicit = [explicit]

    phrases: list[str] = []
    explicit_phrases: list[str] = []
    for item in explicit:
        _add_phrase(phrases, str(item))
        _add_phrase(explicit_phrases, str(item))

    configured_nodes = meta.get("level2_expected_nodes") or meta.get("target_ui_nodes") or []
    if isinstance(configured_nodes, dict):
        configured_nodes = [configured_nodes]
    for node in configured_nodes:
        if not isinstance(node, dict):
            continue
        for attr in ("text", "content-desc", "content_desc", "resource-id", "resource_id"):
            values = node.get(attr)
            values = values if isinstance(values, list) else [values]
            for value in values:
                if value:
                    _add_phrase(phrases, str(value))
                    _add_phrase(explicit_phrases, str(value))

    # Abstract benchmark prompts often do not name the actual UI label.  The
    # golden patch does: added XML/string references are the most reliable
    # description of which node the crawler should seek.
    if base_src and golden_src:
        for phrase in derive_golden_ui_phrases(Path(base_src), Path(golden_src)):
            _add_phrase(phrases, phrase)
            _add_phrase(explicit_phrases, phrase)

    for phrase in _extract_prompt_phrases(prompt):
        _add_phrase(phrases, phrase)

    score_terms = _derive_score_terms(phrases)
    nav_terms = _dedupe(NAVIGATION_TERMS + score_terms + _derive_prompt_nav_terms(prompt))
    interaction_preconditions = _derive_interaction_preconditions(prompt)

    return {
        "target_phrases": phrases,
        "explicit_target_phrases": explicit_phrases,
        "score_terms": score_terms,
        "navigation_terms": nav_terms,
        "popup_terms": POPUP_TERMS,
        "interaction_preconditions": interaction_preconditions,
        "match_attributes": [
            "text",
            "content-desc",
            "resource-id",
            "label",
            "name",
        ],
    }


def _derive_interaction_preconditions(prompt: str) -> list[str]:
    """Describe state a crawler must create before a conditional node exists."""
    lower = _normalize(prompt)
    conditions: list[str] = []
    if "undo" in lower and any(term in lower for term in ("delete", "removed", "restore")):
        conditions.append("seed_media_then_delete")
    if any(term in lower for term in ("duplicate file name", "file already exists", "name conflict")):
        conditions.append("create_duplicate_filename_conflict")
    if any(term in lower for term in ("edit", "white border", "crop")) and any(
        term in lower for term in ("picture", "photo", "image")
    ):
        conditions.append("seed_media_then_open_editor")
    if "batch import" in lower or "root directory" in lower:
        conditions.append("open_document_tree_with_seed_directory")
    return conditions


def derive_golden_ui_phrases(base_src: Path, golden_src: Path) -> list[str]:
    """Extract labels referenced by added golden source lines."""
    if not base_src.exists() or not golden_src.exists():
        return []

    strings: dict[str, str] = {}
    for path in golden_src.glob("**/src/main/res/values/strings*.xml"):
        try:
            root = ET.parse(path).getroot()
            for node in root.findall("string"):
                name = node.attrib.get("name")
                value = "".join(node.itertext()).strip()
                if name and value and "%" not in value and "\\n" not in value:
                    strings.setdefault(name, value)
        except Exception:
            continue

    resource_names: list[str] = []
    literal_phrases: list[str] = []
    core_suffixes = {".kt", ".java", ".xml"}
    for candidate in golden_src.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in core_suffixes:
            continue
        rel = candidate.relative_to(golden_src)
        if any(part in {"build", ".gradle", ".idea", ".git"} for part in rel.parts):
            continue
        try:
            new_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            old_path = base_src / rel
            old_lines = old_path.read_text(encoding="utf-8", errors="replace").splitlines() if old_path.exists() else []
        except Exception:
            continue
        added = [line[2:] for line in difflib.ndiff(old_lines, new_lines) if line.startswith("+ ")]
        for line in added:
            resource_names.extend(re.findall(r"(?:@string/|R\.string\.)([A-Za-z0-9_]+)", line))
            for match in re.finditer(
                r"(?:android:(?:text|contentDescription)|text|contentDescription)\s*=\s*[\"']([^@\"']{2,80})[\"']",
                line,
            ):
                literal_phrases.append(match.group(1))
            match = re.search(r"<string\s+name=[\"']([^\"']+)[\"'][^>]*>([^<]{2,100})</string>", line)
            if match:
                strings.setdefault(match.group(1), match.group(2).strip())
                resource_names.append(match.group(1))

    phrases: list[str] = []
    weak = NON_TARGET_TERMS | {
        "enabled", "disabled", "not set", "default", "status", "title",
        "description", "yes", "no", "save", "done",
    }
    for value in literal_phrases + [strings.get(name, name.replace("_", " ")) for name in resource_names]:
        normalized = _normalize(value)
        if (
            normalized in weak or len(normalized) < 3 or len(normalized) > 80 or
            any(term in normalized for term in (
                "unknown error", "out of memory", "cannot be empty", "invalid characters",
            ))
        ):
            continue
        _add_phrase(phrases, value)
    return phrases[:24]


def match_target_xml(xml_text: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Return whether the XML contains a target node, using node attributes only."""
    attrs = spec.get("match_attributes") or []
    target_phrases = [_normalize(x) for x in spec.get("target_phrases") or []]
    explicit_phrases = {
        _normalize(x) for x in spec.get("explicit_target_phrases") or []
    }
    score_terms = [_normalize(x) for x in spec.get("score_terms") or []]

    matched: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
        nodes = list(root.iter())
    except Exception:
        nodes = []

    for node in nodes:
        for attr in attrs:
            value = node.attrib.get(attr)
            if not value:
                continue
            normalized = _normalize(value)
            for phrase in target_phrases:
                exact = phrase and phrase == normalized
                inferred_contains = (
                    phrase and phrase not in explicit_phrases and
                    _contains_normalized_phrase(normalized, phrase)
                )
                if exact or inferred_contains:
                    matched.append({
                        "attribute": attr,
                        "keyword": phrase,
                        "value": value,
                        "match_mode": "exact" if exact else "inferred_contains",
                        "bounds": node.attrib.get("bounds", ""),
                    })

    if matched:
        return {"matched": True, "matched_nodes": matched}

    # Fallback only when no precise phrase exists: require multiple strong terms.
    if target_phrases:
        return {"matched": False, "matched_nodes": []}

    # 没有精确目标短语时，不以整页关键词共现冒充节点属性精确命中。
    return {"matched": False, "matched_nodes": []}


def normalize_text(text: str) -> str:
    return _normalize(text)


def _extract_prompt_phrases(prompt: str) -> list[str]:
    phrases: list[str] = []
    patterns = [
        r"\*\*[\"“”']?(.+?)[\"“”']?\*\*",
        r"[\"“]([^\"”]{3,80})[\"”]",
        r"'([^']{3,80})'",
        r"\bcalled\s+[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\bnamed\s+[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\bnew\s+(?:theme|menu|feature|page|screen)\s+(?:called|named)?\s*[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\b(?:add|implement)\s+(?:an?|the)?\s*(.{3,70}?)\s+function\b",
        r"\bfunction\s+to\s+(.{3,70}?)(?:\s+to|[.,])",
        r"\bprovide\s+(?:an?|the)?\s*(.{3,70}?)(?:,|\s+allowing|\.)",
        r"\ballow(?:ing)?\s+users\s+to\s+(.{3,70}?)(?:,|\s+and|\.)",
        r"\badd\s+(.{3,70}?buttons.{0,40}?)(?:,|\s+enabling|\.)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, prompt, flags=re.IGNORECASE):
            _add_phrase(phrases, match.group(1))
    return phrases


def _derive_score_terms(phrases: list[str]) -> list[str]:
    terms: list[str] = []
    for phrase in phrases:
        normalized = _normalize(phrase)
        if normalized and normalized not in GENERIC_SCORE_TERMS:
            terms.append(normalized)
        for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", normalized):
            token = token.strip()
            if len(token) >= 3 and token not in GENERIC_SCORE_TERMS:
                terms.append(token)
    return _dedupe(terms)


def _derive_prompt_nav_terms(prompt: str) -> list[str]:
    lower = _normalize(prompt)
    terms = []
    for term in NAVIGATION_TERMS + PROMPT_NAV_CONCEPTS:
        if _normalize(term) in lower:
            terms.append(term)
    # Common UI taxonomy aliases: prompts often say "theme" while an app's
    # intermediate settings category is labelled "Appearance".
    if "theme" in lower or "color scheme" in lower:
        terms.extend(["appearance", "personalization", "customize"])
    if any(term in lower for term in ("background image", "wallpaper", "background color")):
        terms.extend(["appearance", "personalization", "customize"])
    if "notification" in lower:
        terms.append("alerts")
    if "backup" in lower or "sync" in lower:
        terms.extend(["import", "export"])
    return terms


def _visible_xml_text(xml_text: str, attrs: list[str]) -> str:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return xml_text
    values: list[str] = []
    for node in root.iter():
        for attr in attrs:
            value = node.attrib.get(attr)
            if value:
                values.append(value)
    return " ".join(values)


def _add_phrase(phrases: list[str], phrase: str) -> None:
    cleaned = phrase.strip().strip("\"'“”`* ")
    cleaned = cleaned.split("**", 1)[0]
    cleaned = re.split(r"\s+(?:in|within|inside|under|to)\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 2:
        return
    if _normalize(cleaned) in NON_TARGET_TERMS:
        return
    if all(_normalize(existing) != _normalize(cleaned) for existing in phrases):
        phrases.append(cleaned)


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        normalized = _normalize(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(item)
    return out


def _normalize(text: str) -> str:
    text = str(text).lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contains_normalized_phrase(value: str, phrase: str) -> bool:
    """Allow a prompt-derived phrase inside one node value, never page-wide."""
    if not value or not phrase or value == phrase:
        return False
    # Word/phrase boundary containment avoids matches such as red in colored.
    if f" {phrase} " in f" {value} ":
        return True
    # Chinese/Japanese UI labels commonly have no spaces.
    if re.search(r"[\u3400-\u9fff]", phrase) and len(phrase) >= 2:
        return phrase in value
    return False
