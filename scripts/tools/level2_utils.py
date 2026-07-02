#!/usr/bin/env python3
"""Utilities for Level 2 target-page XML scoring."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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

POPUP_TERMS = [
    "ok", "okay", "allow", "continue", "skip", "agree", "close", "cancel", "later",
    "确定", "確定", "好的", "允许", "继续", "跳过", "同意", "关闭", "取消", "稍后", "关闭工作表",
]

NON_TARGET_TERMS = GENERIC_SCORE_TERMS | {
    term.lower() for term in NAVIGATION_TERMS + POPUP_TERMS
}


def build_level2_spec(meta: dict[str, Any]) -> dict[str, Any]:
    """Build a lightweight target-node spec from task metadata only."""
    prompt = str(meta.get("prompt", ""))
    explicit = meta.get("level2_target_keywords") or meta.get("target_keywords") or []
    if isinstance(explicit, str):
        explicit = [explicit]

    phrases: list[str] = []
    for item in explicit:
        _add_phrase(phrases, str(item))

    for phrase in _extract_prompt_phrases(prompt):
        _add_phrase(phrases, phrase)

    score_terms = _derive_score_terms(phrases)
    nav_terms = _dedupe(NAVIGATION_TERMS + score_terms + _derive_prompt_nav_terms(prompt))

    return {
        "target_phrases": phrases,
        "score_terms": score_terms,
        "navigation_terms": nav_terms,
        "popup_terms": POPUP_TERMS,
        "match_attributes": [
            "text",
            "content-desc",
            "resource-id",
            "label",
            "name",
        ],
    }


def match_target_xml(xml_text: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Return whether the XML contains a target node, using node attributes only."""
    attrs = spec.get("match_attributes") or []
    target_phrases = [_normalize(x) for x in spec.get("target_phrases") or []]
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
                if phrase and phrase in normalized:
                    matched.append({
                        "attribute": attr,
                        "keyword": phrase,
                        "value": value,
                    })

    if matched:
        return {"matched": True, "matched_nodes": matched}

    # Fallback only when no precise phrase exists: require multiple strong terms.
    if target_phrases:
        return {"matched": False, "matched_nodes": []}

    page_text = _normalize(_visible_xml_text(xml_text, attrs))
    found_terms = [term for term in score_terms if term and term in page_text]
    if len(set(found_terms)) >= 2:
        return {
            "matched": True,
            "matched_nodes": [{
                "attribute": "page_text",
                "keyword": ", ".join(sorted(set(found_terms))),
                "value": "",
            }],
        }
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
    for term in NAVIGATION_TERMS:
        if _normalize(term) in lower:
            terms.append(term)
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
