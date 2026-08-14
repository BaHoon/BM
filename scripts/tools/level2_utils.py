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
    "go to settings", "open settings", "open navigation drawer", "navigation drawer",
    "drawer", "hamburger",
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
} | {
    "the", "and", "for", "with", "overall", "current", "new", "users", "user",
    "open navigation drawer", "close navigation drawer", "navigate up",
    "app logo", "version", "itinerary planner",
}


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

    prompt_norm = _normalize(prompt)
    if "theme" in prompt_norm or "color scheme" in prompt_norm:
        if any(term in prompt_norm for term in ("girly skull", "girl skull", "skull")):
            for phrase in (
                "Girly Skull",
                "Girly Skull Theme",
                "Girl Skull Theme",
                "Skull Girl Theme",
            ):
                _add_phrase(phrases, phrase)
        if "dark theme" in prompt_norm or "dark mode" in prompt_norm:
            for phrase in ("Dark Theme", "Dark Mode"):
                _add_phrase(phrases, phrase)
        if "high contrast" in prompt_norm or "colorblind" in prompt_norm or "color blind" in prompt_norm:
            for phrase in (
                "High Contrast Mode",
                "High Contrast",
                "Colorblind Mode",
                "Color Blind Mode",
                "高对比度模式",
                "高对比度",
                "高对比",
            ):
                _add_phrase(phrases, phrase)

    if "batch import" in prompt_norm or "root directory" in prompt_norm:
        for phrase in (
            "Download",
            "/storage/emulated/0/Download",
            "CodexSeed",
            "import_root",
            "Select folder",
            "Use this folder",
        ):
            _add_phrase(phrases, phrase)

    if any(term in prompt_norm for term in ("background image", "wallpaper")) and any(
        term in prompt_norm for term in ("upload", "select", "choose", "pick")
    ):
        for phrase in (
            "Background Image",
            "Custom Background",
            "example.png",
        ):
            _add_phrase(phrases, phrase)

    if any(term in prompt_norm for term in ("notification", "reminder", "reminders")):
        for phrase in (
            "Timer Expired Notification",
            "No Activity Notification",
            "Break Notification",
        ):
            _add_phrase(phrases, phrase)

    if any(term in prompt_norm for term in ("search button", "direct search", "search icon")):
        for phrase in ("Search", "action_search", "Search plans"):
            _add_phrase(phrases, phrase)

    if any(term in prompt_norm for term in ("share icon", "share button", "sharing the current page", "share the current page")):
        for phrase in ("Share", "action_share", "Share page", "Share current page"):
            _add_phrase(phrases, phrase)

    if any(term in prompt_norm for term in ("back/up", "back button/gesture", "back button", "navigate up")):
        for phrase in ("Navigate up", "android:id/home", "Back", "Up"):
            _add_phrase(phrases, phrase)

    if "language" in prompt_norm:
        language_aliases = {
            "bengali": ("Bengali", "বাংলা (Bengali)", "বাংলা (বাংলাদেশ)", "বাংলা"),
            "korean": ("Korean", "한국어 (Korean)", "한국어"),
        }
        for marker, aliases in language_aliases.items():
            if marker in prompt_norm:
                for phrase in aliases:
                    _add_phrase(phrases, phrase)

    if (
        any(term in prompt_norm for term in ("completed task", "completed tasks", "completed"))
        and any(term in prompt_norm for term in ("count", "number", "display"))
    ):
        for phrase in (
            "Today:",
            "checklist",
            "checklists",
            "goal",
            "goals",
        ):
            _add_phrase(phrases, phrase)

    if _prompt_names_quick_menu(prompt_norm):
        for phrase in (
            "Quick Menu",
            "main_fab",
            "global_fab",
            "speed_dial",
            "speed_dial_container",
            "global_floating_container",
        ):
            _add_phrase(phrases, phrase)

    _add_localized_equivalent_phrases(phrases, explicit_phrases)

    score_terms = _derive_score_terms(phrases)
    golden_nav_terms = (
        derive_golden_navigation_terms(Path(base_src), Path(golden_src))
        if base_src and golden_src else []
    )
    nav_terms = _dedupe(
        NAVIGATION_TERMS + golden_nav_terms + score_terms + _derive_prompt_nav_terms(prompt)
    )
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
                r"(?:android:(?:text|contentDescription|title|summary)|text|contentDescription|title|summary)\s*=\s*[\"']([^@\"']{2,80})[\"']",
                line,
            ):
                literal_phrases.append(match.group(1))
            match = re.search(r"<string\s+name=[\"']([^\"']+)[\"'][^>]*>([^<]{2,100})</string>", line)
            if match:
                strings.setdefault(match.group(1), match.group(2).strip())
                resource_names.append(match.group(1))
            # Theme/select choices commonly live in string-array <item> nodes,
            # not <string> resources. Missing these made the crawler search for
            # prompt wording ("Girly Skull") while the real option said
            # "Skull Girl".
            for item_text in re.findall(r"<item(?:\s+[^>]*)?>([^<]{2,100})</item>", line):
                if not item_text.strip().startswith("@"):
                    literal_phrases.append(item_text.strip())

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


def derive_golden_navigation_terms(base_src: Path, golden_src: Path) -> list[str]:
    """Infer the containing settings section from changed preference XML paths."""
    terms: list[str] = []
    for path in golden_src.glob("**/src/main/res/xml/*.xml"):
        rel = path.relative_to(golden_src)
        base_path = base_src / rel
        try:
            changed = not base_path.exists() or path.read_bytes() != base_path.read_bytes()
        except OSError:
            continue
        if not changed:
            continue
        stem = normalize_text(path.stem)
        words = [
            word for word in stem.split()
            if word not in {"preference", "preferences", "pref", "prefs", "setting", "settings", "screen"}
        ]
        if not words or words == ["root"]:
            continue
        phrase = " ".join(words)
        terms.append(phrase)
        if phrase == "other":
            terms.append("other settings")
    return _dedupe(terms)


def _add_localized_equivalent_phrases(phrases: list[str], explicit_phrases: list[str]) -> None:
    equivalents = {
        "通知设置": "Notification settings",
        "管理通知偏好": "Manage notification preferences",
        "启用通知": "Enable notifications",
        "允许应用发送通知": "Allow the app to send notifications",
        "用餐提醒": "Meal reminders",
        "获取记录餐食的提醒": "Get reminded to log your meals",
        "目标提醒": "Goal reminders",
        "获取每日目标的通知": "Get notified about your daily goals",
        "导入/导出通知": "Import/export notifications",
        "显示导入和导出操作的通知": "Show notifications for import and export operations",
        "Bengali": "বাংলা (বাংলাদেশ)",
        "বাংলা": "বাংলা (বাংলাদেশ)",
        "বাংলা (Bengali)": "বাংলা (বাংলাদেশ)",
    }
    existing = {_normalize(p) for p in phrases}
    explicit_existing = {_normalize(p) for p in explicit_phrases}
    for source, equivalent in equivalents.items():
        if _normalize(source) not in existing:
            continue
        if _normalize(equivalent) not in existing:
            phrases.append(equivalent)
            existing.add(_normalize(equivalent))
        if _normalize(source) in explicit_existing and _normalize(equivalent) not in explicit_existing:
            explicit_phrases.append(equivalent)
            explicit_existing.add(_normalize(equivalent))


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

    if _matches_theme_target_page(xml_text, spec):
        return {
            "matched": True,
            "matched_nodes": [{
                "attribute": "page_signature",
                "keyword": "theme_target_page",
                "value": "appearance theme system light dark dynamic colors",
                "match_mode": "theme_page_signature",
                "bounds": "",
            }],
        }

    core_buttons = _matches_core_function_home_buttons(xml_text, spec)
    if core_buttons:
        return {
            "matched": True,
            "matched_nodes": [{
                "attribute": "page_signature",
                "keyword": "core_function_home_buttons",
                "value": " ".join(core_buttons),
                "match_mode": "core_function_buttons_signature",
                "bounds": "",
            }],
        }

    if _matches_emoji_library_panel(xml_text, spec):
        return {
            "matched": True,
            "matched_nodes": [{
                "attribute": "page_signature",
                "keyword": "emoji_library_panel",
                "value": "emojiRecycler rich emoji choices",
                "match_mode": "emoji_library_panel_signature",
                "bounds": "",
            }],
        }

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
        r"\b[A-Ba-b]\s*:\s*(.+?)(?=,\s*[A-Ba-b]\s*:|[.;\n]|$)",
        r"\bcalled\s+[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\bnamed\s+[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\bnew\s+(?:theme|menu|feature|page|screen)\s+(?:called|named)?\s*[\"“']?([^\"”'.]{3,80})[\"”']?",
        r"\b(?:add|implement)\s+(?:an?|the)?\s*(.{3,70}?)\s+function\b",
        r"\bfunction\s+to\s+(.{3,70}?)(?:\s+to|[.,])",
        r"\bprovide\s+(?:an?|the)?\s*(.{3,70}?)(?:,|\s+allowing|\.)",
        r"\ballow(?:ing)?\s+users\s+to\s+(.{3,70}?)(?:,|\s+and|\.)",
        r"\badd\s+(.{3,70}?buttons.{0,40}?)(?:,|\s+enabling|\.)",
        r"\badd\s+(?:an?|the)?\s*([a-z0-9 /-]{3,50}?\s+button)\b",
        r"\badd\s+(?:an?|the)?\s*([a-z0-9 /-]{3,50}?\s+icon)\b",
        r"\badd\s+(?:an?|the)?\s*(built-in emoji library)\b",
        r"\bprovides?\s+(rich emojis?)\b",
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


def _prompt_names_quick_menu(prompt_norm: str) -> bool:
    return (
        "quick menu" in prompt_norm
        or (
            "global" in prompt_norm
            and "navigation" in prompt_norm
            and any(term in prompt_norm for term in ("quick access", "access at any time"))
        )
    )


def _derive_prompt_nav_terms(prompt: str) -> list[str]:
    lower = _normalize(prompt)
    terms = []
    for term in NAVIGATION_TERMS + PROMPT_NAV_CONCEPTS:
        if _normalize(term) in lower:
            terms.append(term)
    # Common UI taxonomy aliases: prompts often say "theme" while an app's
    # intermediate settings category is labelled "Appearance".
    if "theme" in lower or "color scheme" in lower:
        terms.extend(["appearance", "personalization", "customize", "color", "colors", "palette"])
    if any(term in lower for term in ("background image", "wallpaper", "background color")):
        terms.extend(["appearance", "personalization", "customize", "color", "colors", "palette"])
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


def _matches_theme_target_page(xml_text: str, spec: dict[str, Any]) -> bool:
    nav_terms = {_normalize(x) for x in spec.get("navigation_terms") or []}
    if not {"theme", "appearance", "colors"} & nav_terms:
        return False

    visible = _normalize(_visible_xml_text(xml_text, spec.get("match_attributes") or ["text", "content-desc"]))
    if not visible:
        return False

    signature_terms = ["theme", "appearance", "system", "light", "dark", "dynamic colors"]
    hits = sum(1 for term in signature_terms if _contains_normalized_phrase(visible, term))
    return hits >= 4 and "theme" in visible


def _matches_core_function_home_buttons(xml_text: str, spec: dict[str, Any]) -> list[str]:
    prompt_terms = {
        _normalize(x)
        for x in (spec.get("target_phrases") or []) + (spec.get("navigation_terms") or [])
    }
    prompt_text = " ".join(prompt_terms)
    if not (
        "home" in prompt_text
        and "core" in prompt_text
        and "button" in prompt_text
        and any(term in prompt_text for term in ("direct", "accessible", "access"))
    ):
        return []

    visible = _normalize(_visible_xml_text(xml_text, spec.get("match_attributes") or ["text", "content-desc", "resource-id"]))
    if not visible:
        return []

    groups = {
        "undo": ("undo",),
        "redo": ("redo",),
        "eraser": ("eraser",),
        "eyedropper": ("eyedropper", "eye dropper"),
        "bucket_fill": ("bucket fill", "bucket_fill"),
        "change_color": ("change color", "change_color", "color picker"),
    }
    hits: list[str] = []
    for name, aliases in groups.items():
        if any(_contains_normalized_phrase(visible, _normalize(alias)) or _normalize(alias) in visible for alias in aliases):
            hits.append(name)
    return hits if len(hits) >= 3 else []


def _matches_emoji_library_panel(xml_text: str, spec: dict[str, Any]) -> bool:
    prompt_text = _normalize(" ".join(
        str(x) for x in (spec.get("target_phrases") or []) + (spec.get("score_terms") or [])
    ))
    if "emoji" not in prompt_text or not any(term in prompt_text for term in ("library", "rich", "emojis")):
        return False

    visible = _normalize(_visible_xml_text(xml_text, spec.get("match_attributes") or ["text", "content-desc", "resource-id"]))
    if _contains_normalized_phrase(visible, "emojirecycler") or _contains_normalized_phrase(visible, "emoji recycler"):
        return True

    emoji_items = re.findall(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
        _visible_xml_text(xml_text, spec.get("match_attributes") or ["text", "content-desc"]),
    )
    return len(emoji_items) >= 4


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
    # Word/phrase boundary containment avoids matches such as red in colored,
    # while still handling resource IDs with separators like :id/main_fab.
    if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", value):
        return True
    # Chinese/Japanese UI labels commonly have no spaces.
    if re.search(r"[\u3400-\u9fff]", phrase) and len(phrase) >= 2:
        return phrase in value
    return False
