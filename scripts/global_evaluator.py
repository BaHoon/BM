#!/usr/bin/env python3
"""Global evaluator driven by unified JSON task config."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agent_runner import AgentRunner
from Env_Manager import EnvManager
from appium_runner import AppiumRunner
from scorer import Scorer
from tools.task_config import find_task_config


def run_global_task(task_key: str, config_path: Path, model: str, strategy: str) -> dict:
    data_dir = ROOT / "data"

    cfg_all = json.loads(config_path.read_text(encoding="utf-8"))
    if task_key not in cfg_all:
        raise KeyError(f"Task key not found in config: {task_key}")

    cfg = dict(cfg_all[task_key])
    app_name = cfg.get("app_name", "app_foodyou")
    task_id = cfg.get("task_id")
    if not task_id:
        raise ValueError(f"task_id missing in config for key {task_key}")

    # Ensure app-level lookup can also find this entry by task_id.
    found = find_task_config(data_dir, app_name=app_name, task_id=task_id)
    if not found:
        raise FileNotFoundError(f"Unified task config not discoverable for {app_name}/{task_id}")

    env = EnvManager()
    agent = AgentRunner(model=model, strategy=strategy)
    appium = AppiumRunner()
    scorer = Scorer()

    result = {
        "task_key": task_key,
        "app_name": app_name,
        "task_id": task_id,
        "agent_success": False,
        "build_success": False,
        "ui_success": False,
        "total_score": 0,
        "apk_path": None,
        "error": None,
    }

    try:
        env.reset_workspace(app_name, task_id)

        agent_result = agent.run_task(app_name, task_id)
        result["agent_success"] = bool(agent_result.get("success"))
        if not result["agent_success"]:
            result["error"] = "agent_failed"
            return result

        build = env.build_project(app_name, task_id)
        result["build_success"] = bool(build.get("success"))
        result["apk_path"] = build.get("apk_path")
        if not result["build_success"] or not result["apk_path"]:
            result["error"] = "build_failed"
            return result

        if not appium.check_appium_server():
            result["error"] = "appium_server_unavailable"
            return result

        appium_run = appium.run_navigation_test(cfg, result["apk_path"])

        score = scorer.score_task(
            app_name=app_name,
            task_id=task_id,
            agent_result=agent_result,
            compile_result=build,
            appium_result=appium_run,
        )
        result["score"] = score
        result["total_score"] = score.get("total_score", 0)
        result["ui_success"] = (score.get("l2") or {}).get("score", 0) >= 2
        result["screenshots"] = appium_run.get("screenshots", [])
        result["ui_log"] = appium_run.get("log", "")
        if not score.get("pass"):
            result["error"] = "scoring_not_passed"

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one unified task via global evaluator")
    parser.add_argument("task_key", help="Task key in unified JSON, e.g. FoodYou_Thm_01")
    parser.add_argument(
        "--config",
        default="data/app_foodyou/FoodYou_ui_verification.json",
        help="Path to unified JSON config",
    )
    parser.add_argument("--model", default="deepseek-r1", help="LLM model alias")
    parser.add_argument(
        "--strategy",
        default="ReAct",
        choices=["direct", "ReAct", "tool_planning"],
        help="Agent strategy",
    )
    args = parser.parse_args()

    config_path = (ROOT / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    result = run_global_task(
        task_key=args.task_key,
        config_path=config_path,
        model=args.model,
        strategy=args.strategy,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result.get("score") or {}).get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
