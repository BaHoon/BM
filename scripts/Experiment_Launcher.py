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
  3. EnvManager.build_project()       — Gradle 编译 → CSR
  4. AppiumRunner.run_test()          — UI 自动化测试 → 截图
  5. Evaluator.evaluate()             — Stream2 校验 → VSM / VLM 打分
  6. ExperimentLogger.log()           — 写入 raw_data.jsonl

汇总（全部完成后）：
  - Pass@k（k=1,3）
  - DSI（按 domain 分组）
  - 错误类别分布
  - 模型 / 策略维度对比表
"""

import argparse
import json
import os
import shutil
import sys
import time
from itertools import product
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from agent_runner import AgentRunner
from Env_Manager  import EnvManager
from Evaluator    import Evaluator
from logger import ExperimentLogger, ExperimentRecord


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
        retriever_top_k:   int        = 5,
        compile_timeout:   int        = 600,
        appium_timeout:    int        = 300,
        feedback_loop:     bool       = False,
        feedback_rounds:   int        = 2,
        skip_if_apk:       bool       = False,
        vlm_model:         str        = "gpt-4o",
        pass_k:            list[int]  = None,
        use_ground_truth:   bool       = False,
    ):
        self.models             = models
        self.strategies         = strategies
        self.tasks_arg          = tasks
        self.app_filter         = app_filter
        self.retriever_top_k    = retriever_top_k
        self.compile_timeout    = compile_timeout
        self.appium_timeout     = appium_timeout
        self.feedback_loop      = feedback_loop
        self.feedback_rounds    = feedback_rounds
        self.skip_if_apk        = skip_if_apk
        self.vlm_model          = vlm_model
        self.pass_k_values      = pass_k or [1, 3]
        self.use_ground_truth   = use_ground_truth

        self.root        = Path(__file__).parent.parent
        self.data_dir    = self.root / "data"
        self.results_dir = self.root / "results"

        self.env_mgr  = EnvManager()
        self.evaluator = Evaluator(vlm_model=vlm_model)
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

        total = len(task_list) * len(combos)
        print(f"\n{'='*70}")
        print(f"  Experiment Launch")
        print(f"  Tasks={len(task_list)}  Models={self.models}  Strategies={self.strategies}")
        print(f"  Total runs: {total}")
        print(f"{'='*70}\n")

        run_idx = 0
        for app_name, task_id in task_list:
            for model, strategy in combos:
                run_idx += 1
                print(f"\n[{run_idx}/{total}] {app_name}/{task_id}  "
                      f"model={model}  strategy={strategy}")
                try:
                    self._run_one(app_name, task_id, model, strategy)
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
    ) -> None:
        self._clear_task_results(app_name, task_id)

        meta      = self._load_meta(app_name, task_id)
        domain    = meta.get("domain")
        eval_type = meta.get("eval_type", "functional")

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

            # Step 2: Agent 代码生成；oracle 模式下直接应用标准答案源码。
            if self.use_ground_truth:
                try:
                    gt_path = self.env_mgr.apply_ground_truth(app_name, task_id)
                    agent_result = {
                        "success": True,
                        "files_written": 0,
                        "write_results": {},
                        "llm_response": "",
                        "retrieved_files": [str(gt_path)],
                        "total_files": 0,
                    }
                    run.record.attempt = 1
                    run.record.extra["oracle_mode"] = True
                    run.record.extra["ground_truth_src"] = str(gt_path)
                    print("  [agent] SKIP: using ground_truth_src")
                except Exception as exc:
                    run.record.csr = False
                    run.record.vsm = False
                    run.record.error_category = "ground_truth_error"
                    run.record.extra["ground_truth_error"] = str(exc)
                    print(f"  [ground_truth] FAIL: {exc}")
                    return
            else:
                agent = AgentRunner(
                    model=model,
                    strategy=strategy,
                    retriever_top_k=self.retriever_top_k,
                )

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
                        agent_result = agent.run_task(app_name, task_id, attempt=1)
                        run.record.attempt = 1
                except Exception as exc:
                    run.record.csr = False
                    run.record.vsm = False
                    run.record.error_category = "llm_api_error"
                    run.record.extra["agent_error"] = str(exc)
                    print(f"  [agent] FAIL: {exc}")
                    return

            run.record.retrieved_files = agent_result.get("retrieved_files", [])

            if not agent_result.get("success"):
                run.record.csr = False
                run.record.vsm = False
                run.record.error_category = "no_code_generated"
                return

            # Step 3: 编译
            compile_result = self.env_mgr.build_project(
                app_name, task_id, timeout=self.compile_timeout
            )
            run.record.csr = compile_result.get("success", False)
            run.record.extra["level1_score"] = compile_result.get("level1_score")
            run.record.extra["level1_category"] = compile_result.get("level1_category")
            run.record.extra["level1_reason"] = compile_result.get("level1_reason")
            run.record.extra["warning_count"] = compile_result.get("warning_count", 0)

            if not run.record.csr:
                run.record.vsm = False
                run.record.error_category = self._level1_error_category(compile_result)
                return

            # Step 4: Appium 测试（获取截图）
            apk_path    = compile_result.get("apk_path")
            screenshots = []
            appium_log  = ""
            appium_result = None

            if apk_path:
                runner = self._get_appium()
                if runner:
                    try:
                        appium_result = runner.run_test(
                            f"{app_name}/{task_id}",
                            apk_path,
                            timeout=self.appium_timeout,
                        )
                        screenshots = appium_result.get("screenshots", [])
                        appium_log  = appium_result.get("log", "")
                        self._save_appium_result(app_name, task_id, appium_result)
                    except Exception as exc:
                        appium_log = f"Appium run_test exception: {exc}"
                        print(f"  [appium] WARN: {exc}")
                else:
                    appium_log = (
                        "Appium runner unavailable: failed to import appium_runner "
                        "or check/start Appium server"
                    )
                    print("  [appium] WARN: Appium runner unavailable")

            # Step 5: 评测
            eval_result = self.evaluator.evaluate(
                app_name, task_id,
                apk_path=apk_path,
                screenshots=screenshots,
                appium_log=appium_log,
            )
            run.record.vsm            = eval_result.get("vsm", False)
            run.record.vlm_score      = eval_result.get("vlm_score")
            run.record.error_category = eval_result.get("error_category")
            run.record.extra["level1_score"] = eval_result.get("level1_score")
            run.record.extra["level1_reason"] = eval_result.get("level1_reason")
            run.record.extra["level2_score"] = eval_result.get("level2_score")
            run.record.extra["level2_reason"] = eval_result.get("level2_reason")
            run.record.extra["level3_score"] = eval_result.get("level3_score")
            run.record.extra["level3_reason"] = eval_result.get("level3_reason")
            run.record.extra["total_score"] = eval_result.get("total_score")
            run.record.extra["vlm_binary_checks"] = eval_result.get("vlm_binary_checks")
            level2_detail = (eval_result.get("stream2") or {}).get("level2_detail") or {}
            file_fallback = level2_detail.get("file_fallback") or {}
            run.record.extra["modified_files"] = file_fallback.get("predicted_modified_files", [])
            run.record.extra["golden_key"] = file_fallback.get("golden_key")
            run.record.extra["golden_overlap_files"] = file_fallback.get("overlap_files", [])

    def _clear_task_results(self, app_name: str, task_id: str) -> None:
        """每次运行前清空 results/app_name/task_id 下的历史产物。"""
        task_results_dir = self.results_dir / app_name / task_id
        if task_results_dir.exists():
            for child in task_results_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except FileNotFoundError:
                        pass
        task_results_dir.mkdir(parents=True, exist_ok=True)
        print(f"[Launcher] Cleared result dir: {task_results_dir}")

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
            print("[Launcher] WARN: Appium server check failed")
        except Exception as exc:
            print(f"[Launcher] WARN: failed to initialize AppiumRunner: {exc}")
        return None

    def _save_appium_result(self, app_name: str, task_id: str, appium_result: dict) -> None:
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "appium_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(appium_result, f, ensure_ascii=False, indent=2)

    def _level1_error_category(self, compile_result: dict) -> str:
        """将 Level 1 编译失败分类映射到实验日志错误类别。"""
        category = compile_result.get("level1_category")
        if category == "dependency_or_context_error":
            return "missing_context"
        if category == "invalid_code_output":
            return "invalid_code"
        if category == "syntax_or_local_error":
            return "syntax_error"
        return "compile_error"

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

        header = (
            f"  {'Model':<25} {'Strategy':<16} {'N':>4} {'CSR':>7} {'VSM':>7} "
            f"{'L1':>5} {'L2':>5} {'L3':>5} {'Total':>7}"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))
        for (m, s), recs in sorted(grid.items()):
            n    = len(recs)
            csr  = sum(1 for r in recs if r.get("csr"))
            vsm  = sum(1 for r in recs if r.get("vsm"))
            l1 = _avg_extra(recs, "level1_score")
            l2 = _avg_extra(recs, "level2_score")
            l3 = _avg_extra(recs, "level3_score")
            total_score = _avg_extra(recs, "total_score")
            print(
                f"  {m:<25} {s:<16} {n:>4} {csr/n*100:>6.1f}% {vsm/n*100:>6.1f}% "
                f"{l1:>5.2f} {l2:>5.2f} {l3:>5.2f} {total_score:>7.2f}"
            )

        # --- Pass@k ---
        for k in self.pass_k_values:
            # 按任务分组，计算每个任务是否有 >= 1 次成功
            by_task: dict[tuple, list] = defaultdict(list)
            for r in records:
                by_task[(r["app_name"], r["task_id"])].append(r.get("vsm", False))
            pass_at_k_vals = [
                Evaluator.compute_pass_at_k(slist, k)
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

        by_model: dict = defaultdict(lambda: _empty_summary_bucket())
        by_strategy: dict = defaultdict(lambda: _empty_summary_bucket())

        for r in records:
            for grp, key in [(by_model, r["model"]), (by_strategy, r["strategy"])]:
                _add_record_to_summary_bucket(grp[key], r)

        def add_rates(d):
            return {k: _finalize_summary_bucket(v) for k, v in d.items()}

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
        return {}


def _count_field(records: list, field: str) -> dict:
    from collections import Counter
    return dict(Counter(r.get(field, "none") for r in records))


def _extra_value(record: dict, key: str):
    return (record.get("extra") or {}).get(key)


def _avg_extra(records: list[dict], key: str) -> float:
    vals = [_extra_value(r, key) for r in records]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else 0.0


def _empty_summary_bucket() -> dict:
    return {
        "total": 0,
        "csr": 0,
        "vsm": 0,
        "level1_sum": 0.0,
        "level1_n": 0,
        "level2_sum": 0.0,
        "level2_n": 0,
        "level3_sum": 0.0,
        "level3_n": 0,
        "total_score_sum": 0.0,
        "total_score_n": 0,
    }


def _add_score(bucket: dict, sum_key: str, n_key: str, value) -> None:
    if isinstance(value, (int, float)):
        bucket[sum_key] += float(value)
        bucket[n_key] += 1


def _add_record_to_summary_bucket(bucket: dict, record: dict) -> None:
    bucket["total"] += 1
    if record.get("csr"):
        bucket["csr"] += 1
    if record.get("vsm"):
        bucket["vsm"] += 1
    extra = record.get("extra") or {}
    _add_score(bucket, "level1_sum", "level1_n", extra.get("level1_score"))
    _add_score(bucket, "level2_sum", "level2_n", extra.get("level2_score"))
    _add_score(bucket, "level3_sum", "level3_n", extra.get("level3_score"))
    _add_score(bucket, "total_score_sum", "total_score_n", extra.get("total_score"))


def _finalize_summary_bucket(bucket: dict) -> dict:
    total = bucket["total"]
    return {
        "total": total,
        "csr": bucket["csr"],
        "vsm": bucket["vsm"],
        "csr_rate": round(bucket["csr"] / total, 4) if total else 0,
        "vsm_rate": round(bucket["vsm"] / total, 4) if total else 0,
        "avg_level1_score": round(bucket["level1_sum"] / bucket["level1_n"], 4)
        if bucket["level1_n"] else 0,
        "avg_level2_score": round(bucket["level2_sum"] / bucket["level2_n"], 4)
        if bucket["level2_n"] else 0,
        "avg_level3_score": round(bucket["level3_sum"] / bucket["level3_n"], 4)
        if bucket["level3_n"] else 0,
        "avg_total_score": round(bucket["total_score_sum"] / bucket["total_score_n"], 4)
        if bucket["total_score_n"] else 0,
    }


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
             "可用简写见 llm_client.py MODEL_ALIASES"
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
    parser.add_argument("--top-k",       type=int, default=8)
    parser.add_argument("--vlm-model",   default="gpt-4o",
                        help="VLM 视觉评分模型（默认 gpt-4o，经 Tongji base_url 路由）")
    parser.add_argument("--feedback-loop",  action="store_true",
                        help="开启 RQ5 反馈闭环（最多 feedback-rounds 次自修正）")
    parser.add_argument("--feedback-rounds", type=int, default=2)
    parser.add_argument("--compile-timeout", type=int, default=600)
    parser.add_argument("--appium-timeout",  type=int, default=300)
    parser.add_argument("--pass-k",      nargs="+", type=int, default=[1, 3],
                        help="计算 Pass@k 的 k 值列表")
    parser.add_argument("--use-ground-truth", action="store_true",
                        help="跳过 LLM 代码生成，直接用 data/<app>/<task>/ground_truth_src 跑完整评分流程")
    return parser.parse_args()


def main():
    args = parse_args()
    launcher = ExperimentLauncher(
        models             = args.model,
        strategies         = args.strategy,
        tasks              = args.tasks,
        app_filter         = args.app,
        retriever_top_k    = args.top_k,
        compile_timeout    = args.compile_timeout,
        appium_timeout     = args.appium_timeout,
        feedback_loop      = args.feedback_loop,
        feedback_rounds    = args.feedback_rounds,
        vlm_model          = args.vlm_model,
        pass_k             = args.pass_k,
        use_ground_truth   = args.use_ground_truth,
    )
    launcher.launch()


if __name__ == "__main__":
    main()
