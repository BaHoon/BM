#!/usr/bin/env python3
"""
Experiment_Launcher.py - 批量实验调度器

用法示例：
  # 策略 A：固定 ReAct，对比三个底座模型，跑所有任务
  python scripts/Experiment_Launcher.py --model gpt-4o --strategy ReAct --tasks all

  # 策略 B：固定 Claude，对比三种策略，只跑 foodyou
  python scripts/Experiment_Launcher.py --model claude-4-5 --strategy ReAct tool_planning --app app_foodyou

  # RQ5 反馈闭环（最多 2 轮自修正）
  python scripts/Experiment_Launcher.py --model gpt-4o --strategy ReAct --feedback-loop

流程（每个任务）：
  1. EnvManager.reset_workspace()     — 还原干净 workspace
  2. AgentRunner.run_task()           — LLM 代码生成（RAG + 策略）
    3. EnvManager.build_project()       — Gradle 编译
    4. AppiumRunner.run_test()          — UI 自动化测试 → 截图 + UI 树
    5. Scorer.score_task()              — L1/L2/L3 三层打分
  6. ExperimentLogger.log()           — 写入 raw_data.jsonl

汇总（全部完成后）：
  - Pass@k（k=1,3）
  - DSI（按 domain 分组）
  - 错误类别分布
  - 模型 / 策略维度对比表
"""

import argparse
import json
import sys
from itertools import product
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from agent_runner import AgentRunner
from Env_Manager  import EnvManager
from scorer       import Scorer
from tools.logger import ExperimentLogger
from tools.task_config import find_task_config, list_task_configs, synthesize_meta_from_task_config


# --------------------------------------------------------------------------- #
#  模型 / 策略 预设列表（策略 A / B 默认选项）
# --------------------------------------------------------------------------- #
STRATEGY_A_MODELS    = ["deepseek-r1", "gpt-4o", "claude-4-5", "gemini"]
STRATEGY_B_STRATEGIES = ["direct", "ReAct", "tool_planning"]


class ExperimentLauncher:
    """批量实验调度器。"""

    def __init__(
        self,
        models:            list[str],
        strategies:        list[str],
        tasks:             str        = "all",        # "all" | "app_foodyou" | "app_foodyou/task_001"
        app_filter:        Optional[str] = None,
        retriever_strategy: str       = "keyword",
        retriever_top_k:   int        = 5,
        compile_timeout:   int        = 600,
        appium_timeout:    int        = 300,
        feedback_loop:     bool       = False,
        feedback_rounds:   int        = 2,
        skip_if_apk:       bool       = False,
        vlm_model:         str        = "gpt-4o",
        pass_k:            list[int]  = None,
        direct_repeats:    int        = 3,
    ):
        self.models             = models
        self.strategies         = strategies
        self.tasks_arg          = tasks
        self.app_filter         = app_filter
        self.retriever_strategy = retriever_strategy
        self.retriever_top_k    = retriever_top_k
        self.compile_timeout    = compile_timeout
        self.appium_timeout     = appium_timeout
        self.feedback_loop      = feedback_loop
        self.feedback_rounds    = feedback_rounds
        self.skip_if_apk        = skip_if_apk
        self.vlm_model          = vlm_model
        self.pass_k_values      = pass_k or [1, 3]
        self.direct_repeats     = max(1, int(direct_repeats))

        self.root        = Path(__file__).parent.parent
        self.data_dir    = self.root / "data"
        self.results_dir = self.root / "results"

        self.env_mgr  = EnvManager()
        self.scorer = Scorer(vlm_model=vlm_model)
        self.logger   = ExperimentLogger(results_dir=self.results_dir)

        # 懒加载 AppiumRunner
        self._appium = None

    # ----------------------------------------------------------------------- #
    #  启动入口
    # ----------------------------------------------------------------------- #

    def launch(self) -> None:
        """遍历所有任务 × 模型 × 策略，依次执行完整 pipeline。"""
        task_list = self._discover_tasks()
        combos    = list(product(self.models, self.strategies))
        plans: list[tuple[str, str, str, str, int]] = []
        for app_name, task_id in task_list:
            for model, strategy in combos:
                repeats = self.direct_repeats if (strategy == "direct" and not self.feedback_loop) else 1
                for attempt in range(1, repeats + 1):
                    plans.append((app_name, task_id, model, strategy, attempt))

        total = len(plans)
        print(f"\n{'='*70}")
        print(f"  Experiment Launch")
        print(f"  Tasks={len(task_list)}  Models={self.models}  Strategies={self.strategies}")
        print(f"  Total runs: {total}")
        print(f"{'='*70}\n")

        run_idx = 0
        for app_name, task_id, model, strategy, attempt in plans:
            run_idx += 1
            print(f"\n[{run_idx}/{total}] {app_name}/{task_id}  "
                  f"model={model}  strategy={strategy}  attempt={attempt}")
            try:
                self._run_one(app_name, task_id, model, strategy, attempt)
            except Exception as exc:
                import traceback
                print(f"[Launcher] ✗ UNHANDLED ERROR: {exc}")
                traceback.print_exc()

        # 全部完成后汇总
        self._print_summary()
        self._save_final_summary()
        self.logger.print_summary()

    # ----------------------------------------------------------------------- #
    #  单任务完整 pipeline
    # ----------------------------------------------------------------------- #

    def _run_one(
        self,
        app_name: str,
        task_id:  str,
        model:    str,
        strategy: str,
        run_attempt: int = 1,
    ) -> None:
        meta      = self._load_meta(app_name, task_id)
        domain    = meta.get("domain")

        with self.logger.start_run(
            app_name, task_id, model, strategy, domain=domain
        ) as run:
            # Step 1: 重置 workspace
            try:
                self.env_mgr.reset_workspace(app_name, task_id)
            except Exception as exc:
                run.record.error_category = "env_error"
                run.record.extra["reset_error"] = str(exc)
                print(f"  [reset] FAIL: {exc}")
                return

            # Step 2: Agent 代码生成
            agent = AgentRunner(
                model=model,
                strategy=strategy,
                retriever_strategy=self.retriever_strategy,
                retriever_top_k=self.retriever_top_k,
            )

            agent_result = None
            compile_result = None
            appium_result = None

            try:
                if self.feedback_loop:
                    # RQ5：注入反馈闭环
                    histories = agent.run_with_feedback_loop(
                        app_name, task_id,
                        get_feedback=self._make_feedback_fn(app_name, task_id),
                        max_rounds=self.feedback_rounds,
                    )
                    agent_result = histories[-1]
                    run.record.attempt = len(histories)
                else:
                    agent_result = agent.run_task(app_name, task_id, attempt=run_attempt)
                    run.record.attempt = run_attempt
            except Exception as exc:
                run.record.extra["agent_error"] = str(exc)
                agent_result = {"success": False, "error": str(exc)}
                print(f"  [agent] FAIL: {exc}")

            run.record.retrieved_files = (agent_result or {}).get("retrieved_files", [])
            run.record.extra["tool_calls"] = (agent_result or {}).get("tool_calls", 0)
            run.record.extra["plan_valid"] = (agent_result or {}).get("plan_valid", strategy != "tool_planning")
            if (agent_result or {}).get("agent_trace"):
                run.record.extra["agent_trace"] = (agent_result or {}).get("agent_trace")

            # Step 3: 编译
            if agent_result and agent_result.get("success"):
                compile_result = self.env_mgr.build_project(
                    app_name, task_id, timeout=self.compile_timeout
                )
            else:
                compile_result = {
                    "success": False,
                    "stdout": "",
                    "stderr": (agent_result or {}).get("error", "agent_failed"),
                }
            run.record.csr = bool(compile_result.get("success", False))

            # Step 4: Appium 测试（获取截图）
            apk_path    = (compile_result or {}).get("apk_path")
            screenshots = []
            appium_log  = ""

            if apk_path and self._get_appium():
                try:
                    appium_result = self._get_appium().run_test(
                        f"{app_name}/{task_id}",
                        apk_path,
                        timeout=self.appium_timeout,
                    )
                    screenshots = appium_result.get("screenshots", [])
                    appium_log  = appium_result.get("log", "")
                except Exception as exc:
                    print(f"  [appium] WARN: {exc}")

            # Step 5: 评测
            score_result = self.scorer.score_task(
                app_name=app_name,
                task_id=task_id,
                agent_result=agent_result,
                compile_result=compile_result,
                appium_result=appium_result,
            )

            run.record.vsm = bool(score_result.get("pass", False))
            run.record.vlm_score = ((score_result.get("l3") or {}).get("vlm") or {}).get("score")
            run.record.error_category = self._derive_error_category(score_result)
            run.record.l1_score = (score_result.get("l1") or {}).get("score", 0)
            run.record.l2_score = (score_result.get("l2") or {}).get("score", 0)
            run.record.l3_score = (score_result.get("l3") or {}).get("score", 0)
            run.record.total_score = score_result.get("total_score", 0)
            run.record.recall = score_result.get("recall", 0.0)
            run.record.extra.update({
                "l1_reason": (score_result.get("l1") or {}).get("reason"),
                "l2_reason": (score_result.get("l2") or {}).get("reason"),
                "l3_reason": (score_result.get("l3") or {}).get("reason"),
            })

    # ----------------------------------------------------------------------- #
    #  Appium 懒加载
    # ----------------------------------------------------------------------- #

    def _get_appium(self):
        if self._appium is not None:
            return self._appium
        try:
            from appium_runner import AppiumRunner
            runner = AppiumRunner()
            if runner.check_appium_server():
                self._appium = runner
                return self._appium
        except Exception:
            pass
        return None

    # ----------------------------------------------------------------------- #
    #  RQ5 反馈回调工厂
    # ----------------------------------------------------------------------- #

    def _make_feedback_fn(self, app_name: str, task_id: str):
        """
        返回一个 get_feedback(attempt) 函数，供 run_with_feedback_loop 调用。
        该函数：编译 → Appium → 返回 (screenshots, log, passed)。
        """
        def get_feedback(attempt: int):
            try:
                compile_result = self.env_mgr.build_project(app_name, task_id)
                if not compile_result.get("success"):
                    log = compile_result.get("error_summary", "")
                    return [], log, False

                apk_path    = compile_result.get("apk_path")
                screenshots = []
                appium_log  = compile_result.get("error_summary", "")

                if apk_path and self._get_appium():
                    ar = self._get_appium().run_test(
                        f"{app_name}/{task_id}", apk_path, timeout=self.appium_timeout
                    )
                    screenshots = ar.get("screenshots", [])
                    appium_log  = ar.get("log", "")
                    if ar.get("success"):
                        return screenshots, appium_log, True

                return screenshots, appium_log, False
            except Exception as exc:
                return [], str(exc), False

        return get_feedback

    # ----------------------------------------------------------------------- #
    #  任务发现
    # ----------------------------------------------------------------------- #

    def _discover_tasks(self) -> list[tuple[str, str]]:
        """遍历 data/ 目录，收集所有 (app_name, task_id) 对。"""
        tasks: list[tuple[str, str]] = []

        arg = self.tasks_arg  # "all" | "app_foodyou" | "app_foodyou/task_001"

        if "/" in (arg or ""):
            # 精确指定单任务
            parts = arg.split("/", 1)
            return [(parts[0], parts[1])]

        unified_rows = list_task_configs(self.data_dir, app_name=self.app_filter)
        for app_name, task_id, _, _ in unified_rows:
            if arg not in ("all", None) and app_name != arg:
                continue
            tasks.append((app_name, task_id))

        for app_dir in sorted(self.data_dir.iterdir()):
            if not app_dir.is_dir() or app_dir.name == "base_src":
                continue
            if self.app_filter and app_dir.name != self.app_filter:
                continue
            if arg not in ("all", None) and app_dir.name != arg:
                continue
            for task_dir in sorted(app_dir.iterdir()):
                if not task_dir.is_dir() or task_dir.name == "base_src":
                    continue
                meta_file = task_dir / "meta.json"
                if meta_file.exists():
                    tasks.append((app_dir.name, task_dir.name))

            tasks = sorted(set(tasks))

        print(f"[Launcher] Discovered {len(tasks)} tasks.")
        return tasks

    # ----------------------------------------------------------------------- #
    #  汇总统计
    # ----------------------------------------------------------------------- #

    def _print_summary(self) -> None:
        """在终端打印 Pass@k、DSI 等汇总指标。"""
        records = self.logger._cache
        if not records:
            return

        print(f"\n{'='*70}")
        print("  EXPERIMENT SUMMARY")
        print(f"{'='*70}")

        # --- 按 model × strategy 打印 CSR / VSM ---
        from collections import defaultdict
        grid: dict[tuple, list] = defaultdict(list)
        for r in records:
            grid[(r["model"], r["strategy"])].append(r)

        header = f"  {'Model':<25} {'Strategy':<16} {'N':>4} {'CSR':>7} {'VSM':>7}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for (m, s), recs in sorted(grid.items()):
            n    = len(recs)
            csr  = sum(1 for r in recs if r.get("csr"))
            vsm  = sum(1 for r in recs if r.get("vsm"))
            print(f"  {m:<25} {s:<16} {n:>4} {csr/n*100:>6.1f}% {vsm/n*100:>6.1f}%")

        # --- Pass@k ---
        for k in self.pass_k_values:
            # 按任务分组，计算每个任务是否有 >= 1 次成功
            by_task: dict[tuple, list] = defaultdict(list)
            for r in records:
                by_task[(r["app_name"], r["task_id"])].append(r.get("vsm", False))
            pass_at_k_vals = [
                Scorer.compute_pass_at_k(slist, k)
                for slist in by_task.values()
            ]
            avg = sum(pass_at_k_vals) / len(pass_at_k_vals) if pass_at_k_vals else 0
            print(f"\n  Pass@{k} (avg over {len(by_task)} tasks): {avg*100:.1f}%")

        print(f"{'='*70}\n")

    def _save_final_summary(self) -> None:
        """将汇总写入 results/experiment_summary.json。"""
        records    = self.logger._cache
        out_path   = self.results_dir / "experiment_summary.json"
        from collections import defaultdict

        by_model: dict = defaultdict(lambda: {"total": 0, "csr": 0, "vsm": 0})
        by_strategy: dict = defaultdict(lambda: {"total": 0, "csr": 0, "vsm": 0})

        for r in records:
            for grp, key in [(by_model, r["model"]), (by_strategy, r["strategy"])]:
                grp[key]["total"] += 1
                if r.get("csr"):
                    grp[key]["csr"] += 1
                if r.get("vsm"):
                    grp[key]["vsm"] += 1

        def add_rates(d):
            return {
                k: {**v,
                    "csr_rate": round(v["csr"]/v["total"], 4) if v["total"] else 0,
                    "vsm_rate": round(v["vsm"]/v["total"], 4) if v["total"] else 0}
                for k, v in d.items()
            }

        summary = {
            "total_runs":    len(records),
            "by_model":      add_rates(by_model),
            "by_strategy":   add_rates(by_strategy),
            "error_distribution": _count_field(records, "error_category"),
        }

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[Launcher] Experiment summary saved to {out_path}")

    # ----------------------------------------------------------------------- #
    #  工具方法
    # ----------------------------------------------------------------------- #

    def _load_meta(self, app_name: str, task_id: str) -> dict:
        p = self.data_dir / app_name / task_id / "meta.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)

        found = find_task_config(self.data_dir, app_name=app_name, task_id=task_id)
        if found:
            task_key, cfg = found
            cfg = dict(cfg)
            cfg.setdefault("task_key", task_key)
            cfg.setdefault("task_id", task_id)
            return synthesize_meta_from_task_config(cfg)
        return {}

    def _derive_error_category(self, score_result: dict) -> str:
        l1_reason = (score_result.get("l1") or {}).get("reason", "")
        l2_reason = (score_result.get("l2") or {}).get("reason", "")
        if "dependency" in l1_reason:
            return "missing_context"
        if "syntax" in l1_reason or "compile_fail" in l1_reason:
            return "logic_error"
        if "ui_node_missing" in l2_reason:
            return "vague_req"
        if score_result.get("pass"):
            return "none"
        return "vague_req"


def _count_field(records: list, field: str) -> dict:
    from collections import Counter
    return dict(Counter(r.get(field, "none") for r in records))


# --------------------------------------------------------------------------- #
#  CLI 入口
# --------------------------------------------------------------------------- #

def parse_args():
    parser = argparse.ArgumentParser(
        description="Experiment_Launcher — 批量实验调度器",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--model", nargs="+", default=["deepseek-r1"],
        help="底座模型列表，例如: gpt-4o claude-4-5 gemini\n"
             "可用简写见 llm_api/client.py MODEL_ALIASES"
    )
    parser.add_argument(
        "--strategy", nargs="+", default=["ReAct"],
        choices=["direct", "ReAct", "tool_planning"],
        help="Agent 策略列表，例如: ReAct tool_planning"
    )
    parser.add_argument(
        "--tasks", default="all",
        help='任务范围：\n'
             '  "all"                  — 所有任务\n'
             '  "app_foodyou"          — 某一 App 的全部任务\n'
             '  "app_foodyou/task_001" — 单个任务'
    )
    parser.add_argument(
        "--app", default=None,
        help="按 App 名称过滤（与 --tasks 合用）"
    )
    parser.add_argument(
        "--retriever", default="keyword",
        choices=["keyword", "tfidf", "ast_analysis"],
        help="文件检索策略（默认 keyword）"
    )
    parser.add_argument("--top-k",       type=int, default=5)
    parser.add_argument("--vlm-model",   default="gpt-4o",
                        help="VLM 视觉评分模型（默认 gpt-4o，经 Tongji base_url 路由）")
    parser.add_argument("--feedback-loop",  action="store_true",
                        help="开启 RQ5 反馈闭环（最多 feedback-rounds 次自修正）")
    parser.add_argument("--feedback-rounds", type=int, default=2)
    parser.add_argument("--compile-timeout", type=int, default=600)
    parser.add_argument("--appium-timeout",  type=int, default=300)
    parser.add_argument("--pass-k",      nargs="+", type=int, default=[1, 3],
                        help="计算 Pass@k 的 k 值列表")
    parser.add_argument("--direct-repeats", type=int, default=3,
                        help="direct 策略每任务重复次数（默认 3）")
    return parser.parse_args()


def main():
    args = parse_args()
    launcher = ExperimentLauncher(
        models             = args.model,
        strategies         = args.strategy,
        tasks              = args.tasks,
        app_filter         = args.app,
        retriever_strategy = args.retriever,
        retriever_top_k    = args.top_k,
        compile_timeout    = args.compile_timeout,
        appium_timeout     = args.appium_timeout,
        feedback_loop      = args.feedback_loop,
        feedback_rounds    = args.feedback_rounds,
        vlm_model          = args.vlm_model,
        pass_k             = args.pass_k,
        direct_repeats     = args.direct_repeats,
    )
    launcher.launch()


if __name__ == "__main__":
    main()
