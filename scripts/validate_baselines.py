#!/usr/bin/env python3
"""Compile one pristine base_src checkout per app and persist an audit report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from Env_Manager import EnvManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default="results_baseline_validation")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--apps", nargs="*", default=[])
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    # Keep the invocation path: on the server /data/liuyihan/scripts is a
    # symlink to the repository, while data/workspace/results live beside it.
    root = Path(__file__).parent.parent
    data_dir = root / "data"
    output_dir = root / args.results_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "baseline_validation.json"
    previous = {}
    if args.resume and report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8")).get("apps", {})

    selected = set(args.apps)
    apps = sorted(
        path.name for path in data_dir.iterdir()
        if path.is_dir() and (path / "base_src").exists()
        and (not selected or path.name in selected)
    )
    manager = EnvManager(
        data_dir=str(data_dir),
        workspace_dir=str(root / "workspace"),
        results_dir=str(output_dir),
    )
    results = dict(previous)

    for index, app in enumerate(apps, 1):
        if args.resume and results.get(app, {}).get("success"):
            print(f"[{index}/{len(apps)}] {app}: already passed")
            continue
        tasks = sorted(
            path.parent.name for path in (data_dir / app).glob("*/meta.json")
        )
        if not tasks:
            results[app] = {"success": False, "error": "no task metadata"}
            continue
        task = tasks[0]
        print(f"\n[{index}/{len(apps)}] baseline {app} via {task}")
        try:
            manager.reset_workspace(app, task)
            compilation = manager.build_project(app, task, timeout=args.timeout)
            results[app] = {
                "success": bool(compilation.get("success")),
                "task_metadata_used": task,
                "level1_score": compilation.get("level1_score"),
                "level1_category": compilation.get("level1_category"),
                "elapsed_time": compilation.get("elapsed_time"),
                "error_summary": compilation.get("error_summary", ""),
                "warning_count": compilation.get("warning_count", 0),
                "introduced_warning_count": compilation.get("introduced_warning_count", 0),
                "apk_path": compilation.get("apk_path"),
            }
        except Exception as exc:
            results[app] = {"success": False, "task_metadata_used": task, "error": str(exc)}

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_apps": len(apps),
            "passed": sum(bool(item.get("success")) for item in results.values()),
            "apps": results,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [name for name, item in results.items() if not item.get("success")]
    print(f"\nBaseline validation: {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("Failed:", ", ".join(sorted(failed)))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
