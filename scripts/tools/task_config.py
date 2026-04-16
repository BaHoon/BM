#!/usr/bin/env python3
"""Utilities for loading unified task configuration files."""

import json
from pathlib import Path
from typing import Optional, Tuple


def _load_unified_configs(app_dir: Path) -> dict:
    """Load all entries from *_ui_verification.json under one app directory."""
    merged: dict = {}
    for path in sorted(app_dir.glob("*ui_verification.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            continue

    # 兼容仓库根目录的全局配置文件（例如 FoodYou_ui_verification.json）
    repo_root = app_dir.parent.parent
    for path in sorted(repo_root.glob("*ui_verification.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            # 仅吸收当前 app 的任务，避免跨应用串扰
            for key, cfg in data.items():
                if isinstance(cfg, dict) and cfg.get("app_name", "app_foodyou") == app_dir.name:
                    merged[key] = cfg
        except Exception:
            continue
    return merged


def find_task_config(
    data_dir: Path,
    app_name: str,
    task_id: Optional[str] = None,
    task_key: Optional[str] = None,
) -> Optional[Tuple[str, dict]]:
    """Find one task config by key or by task_id from unified JSON files."""
    app_dir = data_dir / app_name
    if not app_dir.exists():
        return None

    entries = _load_unified_configs(app_dir)
    if task_key and task_key in entries:
        return task_key, entries[task_key]

    if task_id:
        for key, cfg in entries.items():
            if isinstance(cfg, dict) and cfg.get("task_id") == task_id:
                return key, cfg

    return None


def list_task_configs(data_dir: Path, app_name: Optional[str] = None) -> list[Tuple[str, str, str, dict]]:
    """List unified task configs as (app_name, task_id, task_key, cfg)."""
    rows: list[Tuple[str, str, str, dict]] = []

    app_dirs = []
    if app_name:
        app_dirs = [data_dir / app_name]
    else:
        app_dirs = [p for p in sorted(data_dir.iterdir()) if p.is_dir()]

    for app_dir in app_dirs:
        if not app_dir.exists() or not app_dir.is_dir():
            continue
        merged = _load_unified_configs(app_dir)
        for task_key, cfg in merged.items():
            if not isinstance(cfg, dict):
                continue
            tid = cfg.get("task_id")
            if not tid:
                continue
            rows.append((app_dir.name, tid, task_key, cfg))

    return rows


def synthesize_meta_from_task_config(cfg: dict) -> dict:
    """Convert unified task config to the old meta-like shape."""
    return {
        "id": cfg.get("id") or cfg.get("task_id"),
        "name": cfg.get("name", ""),
        "app_package": cfg.get("app_package"),
        "target_activity": cfg.get("target_activity"),
        "build_command": cfg.get("build_command", "gradlew.bat assembleDebug"),
        "apk_path": cfg.get("apk_path", "app/build/outputs/apk/debug/app-debug.apk"),
        "prompt": cfg.get("prompt", ""),
        "eval_type": cfg.get("eval_type", "functional"),
        "task_key": cfg.get("task_key"),
        "task_id": cfg.get("task_id"),
    }
