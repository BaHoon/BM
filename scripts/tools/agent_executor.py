#!/usr/bin/env python3
"""Structured tool execution and response parsing for real multi-step agents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class ToolExecutionError(Exception):
    """Raised when a tool call cannot be executed safely."""


@dataclass
class PlanStep:
    step_id: str
    goal: str
    depends_on: list[str]


class PlanValidator:
    """Validate and track execution order for tool_planning strategy."""

    def __init__(self):
        self.steps: dict[str, PlanStep] = {}
        self.completed: set[str] = set()

    def load_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            return {"ok": False, "error": "plan.steps must be a non-empty list"}

        parsed: dict[str, PlanStep] = {}
        for item in steps:
            if not isinstance(item, dict):
                return {"ok": False, "error": "each plan step must be an object"}
            sid = str(item.get("id", "")).strip()
            goal = str(item.get("goal", "")).strip()
            deps = item.get("depends_on", [])
            if not sid or not goal:
                return {"ok": False, "error": "each plan step requires id and goal"}
            if not isinstance(deps, list):
                return {"ok": False, "error": f"depends_on must be list for step {sid}"}
            deps_norm = [str(x).strip() for x in deps if str(x).strip()]
            parsed[sid] = PlanStep(step_id=sid, goal=goal, depends_on=deps_norm)

        for sid, step in parsed.items():
            for dep in step.depends_on:
                if dep not in parsed:
                    return {"ok": False, "error": f"step {sid} depends on unknown step {dep}"}

        cycle_error = self._detect_cycle(parsed)
        if cycle_error:
            return {"ok": False, "error": cycle_error}

        self.steps = parsed
        self.completed = set()
        return {"ok": True, "steps": len(parsed)}

    def can_execute(self, step_id: str) -> dict[str, Any]:
        if step_id not in self.steps:
            return {"ok": False, "error": f"unknown step_id: {step_id}"}
        step = self.steps[step_id]
        unmet = [d for d in step.depends_on if d not in self.completed]
        if unmet:
            return {"ok": False, "error": f"step {step_id} has unmet dependencies: {unmet}"}
        return {"ok": True}

    def mark_completed(self, step_id: str) -> None:
        if step_id in self.steps:
            self.completed.add(step_id)

    def summary(self) -> dict[str, Any]:
        return {
            "total_steps": len(self.steps),
            "completed_steps": len(self.completed),
            "remaining_steps": [sid for sid in self.steps if sid not in self.completed],
        }

    def _detect_cycle(self, steps: dict[str, PlanStep]) -> Optional[str]:
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for dep in steps[node].depends_on:
                if dfs(dep):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        for sid in steps:
            if dfs(sid):
                return "plan has cyclic dependencies"
        return None


class ToolRuntime:
    """Execute safe local tools against workspace directory."""

    def __init__(self, root: Path, max_read_lines: int = 600):
        self.root = root.resolve()
        self.max_read_lines = max_read_lines

    def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = str(tool).strip()
        args = args or {}
        if tool == "list_dir":
            return self._list_dir(args)
        if tool == "read_file":
            return self._read_file(args)
        if tool == "search_text":
            return self._search_text(args)
        raise ToolExecutionError(f"unsupported tool: {tool}")

    def _resolve_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ToolExecutionError("path is required")
        p = Path(raw_path)
        if not p.is_absolute():
            p = (self.root / p).resolve()
        else:
            p = p.resolve()
        if self.root not in [p, *p.parents]:
            raise ToolExecutionError("path escapes workspace root")
        return p

    def _list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", ".")))
        if not path.exists() or not path.is_dir():
            raise ToolExecutionError(f"directory not found: {path}")

        max_entries = int(args.get("max_entries", 200))
        rows = []
        for child in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:max_entries]:
            rows.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "relative_path": str(child.relative_to(self.root)).replace("\\", "/"),
            })

        return {
            "tool": "list_dir",
            "path": str(path.relative_to(self.root)).replace("\\", "/"),
            "entries": rows,
            "entry_count": len(rows),
        }

    def _read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", "")))
        if not path.exists() or not path.is_file():
            raise ToolExecutionError(f"file not found: {path}")

        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", start + self.max_read_lines - 1))
        if start < 1:
            start = 1
        if end < start:
            end = start
        if (end - start + 1) > self.max_read_lines:
            end = start + self.max_read_lines - 1

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        snippet = lines[start - 1 : end]

        return {
            "tool": "read_file",
            "path": str(path.relative_to(self.root)).replace("\\", "/"),
            "start_line": start,
            "end_line": min(end, total),
            "total_lines": total,
            "content": "\n".join(snippet),
        }

    def _search_text(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ToolExecutionError("pattern is required")

        search_root = self._resolve_path(str(args.get("path", ".")))
        if not search_root.exists() or not search_root.is_dir():
            raise ToolExecutionError(f"directory not found: {search_root}")

        include = str(args.get("include", "**/*"))
        max_hits = int(args.get("max_hits", 50))
        is_regex = bool(args.get("is_regex", False))
        include_patterns = [p.strip() for p in re.split(r"[;,]", include) if p.strip()] or ["**/*"]
        normalized_patterns: list[str] = []
        for p in include_patterns:
            if p.startswith("**/") or "/" in p or "\\" in p:
                normalized_patterns.append(p.replace("\\", "/"))
            else:
                normalized_patterns.append(f"**/{p}")

        hits = []
        compiled = re.compile(pattern, re.IGNORECASE) if is_regex else None

        for include_pattern in normalized_patterns:
            for p in search_root.glob(include_pattern):
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    continue
                for i, line in enumerate(text, start=1):
                    ok = bool(compiled.search(line)) if compiled else (pattern.lower() in line.lower())
                    if ok:
                        hits.append(
                            {
                                "path": str(p.relative_to(self.root)).replace("\\", "/"),
                                "line": i,
                                "content": line[:300],
                            }
                        )
                        if len(hits) >= max_hits:
                            break
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break

        return {
            "tool": "search_text",
            "pattern": pattern,
            "is_regex": is_regex,
            "hits": hits,
            "hit_count": len(hits),
        }


def parse_agent_json(text: str) -> Optional[dict[str, Any]]:
    """Parse first JSON object from model output (plain or fenced)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    candidate = fence.group(1) if fence else cleaned

    if not candidate.startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)

    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
