#!/usr/bin/env python3
"""
UI tree dump + declarative assertions for mobile task validation.

This module allows task-level validation to be defined by JSON rules
instead of writing a full custom script for each task.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


_ATTR_MAP = {
    "resource_id": "resource-id",
    "text": "text",
    "class_name": "class",
    "content_desc": "content-desc",
    "clickable": "clickable",
    "enabled": "enabled",
    "package": "package",
}


def dump_ui_tree(driver: Any, out_dir: Path, name: str) -> str:
    """Dump current Appium page_source as XML into out_dir and return path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "ui_tree"
    out_path = out_dir / f"{safe_name}.xml"
    xml_text = driver.page_source or ""
    out_path.write_text(xml_text, encoding="utf-8")
    return str(out_path)


def load_checks(checks_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(checks_path.read_text(encoding="utf-8"))
    checks = payload.get("checks", []) if isinstance(payload, dict) else []
    return [c for c in checks if isinstance(c, dict)]


def evaluate_ui_checks(xml_path: str | Path, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate declarative checks against dumped XML tree."""
    xml_path = Path(xml_path)
    if not xml_path.exists():
        return {
            "passed": False,
            "summary": "ui_tree_missing",
            "results": [],
        }

    try:
        root = ET.fromstring(xml_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {
            "passed": False,
            "summary": f"ui_tree_parse_error: {exc}",
            "results": [],
        }

    nodes = list(root.iter())
    results: list[dict[str, Any]] = []

    for idx, check in enumerate(checks):
        name = str(check.get("name") or f"check_{idx + 1}")
        min_count = int(check.get("min_count", 1))
        max_count = check.get("max_count")
        any_of = check.get("any_of")
        if not isinstance(any_of, list) or not any_of:
            any_of = [check.get("match", {})]

        option_counts: list[int] = []
        for matcher in any_of:
            if not isinstance(matcher, dict):
                option_counts.append(0)
                continue
            option_counts.append(_count_matches(nodes, matcher))

        matched_count = max(option_counts) if option_counts else 0
        passed = matched_count >= min_count
        if max_count is not None:
            passed = passed and matched_count <= int(max_count)

        results.append(
            {
                "name": name,
                "passed": bool(passed),
                "matched_count": matched_count,
                "min_count": min_count,
                "max_count": max_count,
            }
        )

    failed = [r for r in results if not r["passed"]]
    if failed:
        summary = "failed: " + ", ".join(r["name"] for r in failed)
    else:
        summary = "all_checks_passed"

    return {
        "passed": len(failed) == 0,
        "summary": summary,
        "results": results,
    }


def _count_matches(nodes: list[ET.Element], matcher: dict[str, Any]) -> int:
    matched = 0
    for node in nodes:
        if _node_matches(node, matcher):
            matched += 1
    return matched


def _node_matches(node: ET.Element, matcher: dict[str, Any]) -> bool:
    for key, expected in matcher.items():
        if key in _ATTR_MAP:
            raw = (node.attrib.get(_ATTR_MAP[key], "") or "").strip()
            if not _value_match(raw, expected):
                return False
        elif key == "text_contains":
            text = (node.attrib.get("text", "") or "").strip().lower()
            if str(expected).strip().lower() not in text:
                return False
        elif key == "content_desc_contains":
            desc = (node.attrib.get("content-desc", "") or "").strip().lower()
            if str(expected).strip().lower() not in desc:
                return False
        elif key == "bounds_contains":
            bounds = (node.attrib.get("bounds", "") or "").strip().lower()
            if str(expected).strip().lower() not in bounds:
                return False
        else:
            # Unknown key: keep strict to avoid silent false positives.
            return False
    return True


def _value_match(raw: str, expected: Any) -> bool:
    if isinstance(expected, bool):
        return raw.lower() == str(expected).lower()
    if isinstance(expected, (int, float)):
        return raw == str(expected)
    if isinstance(expected, list):
        return any(_value_match(raw, item) for item in expected)
    return raw == str(expected)
