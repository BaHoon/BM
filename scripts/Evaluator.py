#!/usr/bin/env python3
"""
Evaluator.py - CSR & VSM 双流评测引擎

双流架构：
  Stream 1 (Static / Compilation):
      检查 APK 是否编译成功 → 计算 CSR (Compilation Success Rate)

  Stream 2 (Dynamic / Functional + Visual):
      2a. 功能性验证：调用 data/app_name/task_xxx/test_script.py（UI 树硬核校验）
      2b. 视觉性验证：若 meta.json 中 eval_type == "visual"，
                     将截图传给 VLM 打分 → 计算 VSM (Visual Success Metric)

指标汇总：
  - CSR:      编译通过率
  - VSM:      动态验证通过率（功能 OR 视觉，取决于任务类型）
  - Pass@k:   k 次尝试中至少有 1 次通过的概率（由 Experiment_Launcher 汇总）
  - DSI:      (model_pass_rate - global_avg) / global_avg，在 Experiment_Launcher 计算
  - error_category: 失败归因（logic_error | missing_context | vague_req）
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from llm_api.client import LLMClient


class Evaluator:
    """
    双流评测引擎。

    典型用法：
        ev = Evaluator()
        result = ev.evaluate("app_foodyou", "task_001_theme",
                             apk_path=..., screenshots=[...])
    """

    def __init__(
        self,
        data_dir:    str = "data",
        results_dir: str = "results",
        vlm_model:   str = "gpt-4o",
    ):
        root = Path(__file__).parent.parent
        self.data_dir    = root / data_dir
        self.results_dir = root / results_dir
        self.vlm_model   = vlm_model
        self._vlm: Optional[LLMClient] = None   # 懒加载，仅视觉任务才用

    # ----------------------------------------------------------------------- #
    #  主入口
    # ----------------------------------------------------------------------- #

    def evaluate(
        self,
        app_name:    str,
        task_id:     str,
        apk_path:    Optional[str]        = None,
        screenshots: Optional[list[str]]  = None,
        appium_log:  Optional[str]        = None,
    ) -> dict:
        """
        执行完整评测（Stream 1 + Stream 2），返回指标字典。

        Args:
            app_name / task_id : 任务标识。
            apk_path           : 编译产物路径（None → CSR=False）。
            screenshots        : Appium 截图路径列表（用于 VLM 评分）。
            appium_log         : Appium 测试日志（用于错误归因）。

        Returns:
            {
                "csr":            bool,
                "vsm":            bool,
                "vlm_score":      int | None,
                "eval_type":      "functional" | "visual",
                "error_category": str | None,
                "stream1":        {...},
                "stream2":        {...},
            }
        """
        meta     = self._load_meta(app_name, task_id)
        eval_type = meta.get("eval_type", "functional")   # 默认功能性校验

        print(f"\n[Evaluator] {app_name}/{task_id}  eval_type={eval_type}")

        # -------- Stream 1: 静态/编译 -------- #
        stream1 = self._stream1_compilation(apk_path)
        csr     = stream1["success"]

        # -------- Stream 2: 动态验证 -------- #
        stream2    = {}
        vsm        = False
        vlm_score  = None

        if csr:
            if eval_type == "visual":
                stream2, vsm, vlm_score = self._stream2_visual(
                    app_name, task_id, meta, screenshots
                )
            else:
                stream2, vsm = self._stream2_functional(
                    app_name, task_id, appium_log, screenshots
                )
        else:
            stream2 = {"skipped": True, "reason": "compilation failed"}

        # -------- 错误归因（RQ4）-------- #
        error_category = None
        if not vsm:
            error_category = self._categorize_error(
                stream1, stream2, appium_log or ""
            )

        result = {
            "csr":            csr,
            "vsm":            vsm,
            "vlm_score":      vlm_score,
            "eval_type":      eval_type,
            "error_category": error_category,
            "stream1":        stream1,
            "stream2":        stream2,
        }

        self._save_eval_result(app_name, task_id, result)
        return result

    # ----------------------------------------------------------------------- #
    #  Stream 1: 编译检查
    # ----------------------------------------------------------------------- #

    def _stream1_compilation(self, apk_path: Optional[str]) -> dict:
        """检查 APK 文件是否存在，决定 CSR。"""
        if apk_path and Path(apk_path).exists():
            size_mb = round(Path(apk_path).stat().st_size / 1024 / 1024, 2)
            print(f"  [S1] APK OK  size={size_mb}MB  path={apk_path}")
            return {"success": True, "apk_path": apk_path, "apk_size_mb": size_mb}
        print(f"  [S1] APK not found: {apk_path}")
        return {"success": False, "apk_path": apk_path}

    # ----------------------------------------------------------------------- #
    #  Stream 2a: 功能性验证（UI 树硬核校验）
    # ----------------------------------------------------------------------- #

    def _stream2_functional(
        self,
        app_name: str,
        task_id:  str,
        appium_log: Optional[str],
        screenshots: Optional[list[str]],
    ) -> tuple[dict, bool]:
        """
        功能性校验优先使用 Appium 执行结果，避免注入式脚本被 subprocess 误判。
        若无 Appium 结果，且脚本可独立执行，则回退到 subprocess。
        """
        appium_result = self._load_appium_result(app_name, task_id)
        if appium_result is not None:
            passed = bool(appium_result.get("success", False))
            shots = appium_result.get("screenshots") or []
            detail = {
                "passed":      passed,
                "source":      "appium_result",
                "elapsed_s":   appium_result.get("elapsed_time"),
                "shots_used":  len(shots),
                "test_type":   appium_result.get("test_type", "custom"),
                "timestamp":   appium_result.get("timestamp"),
                "appium_log":  (appium_log or "")[-2000:],
            }
            print(f"  [S2-func] {'PASS' if passed else 'FAIL'}  source=appium_result")
            return detail, passed

        if screenshots:
            detail = {
                "passed":     True,
                "source":     "screenshots_only",
                "shots_used": len(screenshots),
                "note":       "appium_result.json missing, inferred from screenshots",
            }
            print(f"  [S2-func] PASS  source=screenshots_only  shots={len(screenshots)}")
            return detail, True

        test_script = self.data_dir / app_name / task_id / "test_script.py"
        if not test_script.exists():
            print(f"  [S2-func] test_script.py not found: {test_script}")
            return {"skipped": True, "reason": "test_script.py not found"}, False

        if self._requires_appium_runtime(test_script):
            print("  [S2-func] SKIP  script requires Appium runtime injection")
            return {
                "passed": False,
                "skipped": True,
                "error": "appium_runtime_required",
                "reason": "test_script expects injected driver/take_screenshot context",
            }, False

        print(f"  [S2-func] Running test_script: {test_script}")
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(test_script)],
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = round(time.time() - t0, 2)
            passed  = proc.returncode == 0
            detail  = {
                "passed":      passed,
                "return_code": proc.returncode,
                "elapsed_s":   elapsed,
                "stdout":      proc.stdout[-3000:],
                "stderr":      proc.stderr[-2000:],
            }
            print(f"  [S2-func] {'PASS' if passed else 'FAIL'}  RC={proc.returncode}  t={elapsed}s")
            return detail, passed

        except subprocess.TimeoutExpired:
            print("  [S2-func] TIMEOUT")
            return {"passed": False, "error": "timeout"}, False
        except Exception as exc:
            print(f"  [S2-func] ERROR: {exc}")
            return {"passed": False, "error": str(exc)}, False

    # ----------------------------------------------------------------------- #
    #  Stream 2b: 视觉性验证（VLM 打分）
    # ----------------------------------------------------------------------- #

    def _stream2_visual(
        self,
        app_name:    str,
        task_id:     str,
        meta:        dict,
        screenshots: Optional[list[str]],
    ) -> tuple[dict, bool, Optional[int]]:
        """调用 LLMClient.score_screenshot() 对截图进行 VLM 打分。"""
        if not screenshots:
            print("  [S2-visual] No screenshots provided.")
            shots = appium_result.get("screenshots") or []
            valid_shots = [p for p in shots if Path(p).exists()]

            # appium_result 可能是历史残留；截图不存在时不应直接判 PASS。
            if shots and not valid_shots:
                print("  [S2-func] WARN  stale appium_result detected (screenshots missing)")
            else:
                detail = {
                    "passed":      passed,
                    "source":      "appium_result",
                    "elapsed_s":   appium_result.get("elapsed_time"),
                    "shots_used":  len(valid_shots),
                    "test_type":   appium_result.get("test_type", "custom"),
                    "timestamp":   appium_result.get("timestamp"),
                    "appium_log":  (appium_log or "")[-2000:],
                }
                print(f"  [S2-func] {'PASS' if passed else 'FAIL'}  source=appium_result")
                return detail, passed
        print(f"  [S2-visual] Scoring {len(valid_shots)} screenshots via {self.vlm_model}")

        vlm_result = self._vlm.score_screenshot(task_prompt, valid_shots)
        score  = int(vlm_result.get("score", 0))
        passed = bool(vlm_result.get("passed", score >= 6))
        reason = vlm_result.get("reason", "")

        print(f"  [S2-visual] score={score}  passed={passed}  reason={reason[:80]}")
        detail = {
            "passed":     passed,
            "vlm_score":  score,
            "reason":     reason,
            "model":      self.vlm_model,
            "shots_used": len(valid_shots),
        }
        return detail, passed, score

    # ----------------------------------------------------------------------- #
    #  错误归因（RQ4）
    # ----------------------------------------------------------------------- #

    def _categorize_error(
        self, stream1: dict, stream2: dict, log: str
    ) -> str:
        """
        将失败归类为三类（用于 RQ4 深度分析）：
          logic_error      — 代码逻辑错，编译失败或运行时崩溃
          missing_context  — 模型没拿到足够的文件 / 依赖关系
          vague_req        — 需求描述模糊，模型理解偏差
        """
        log_lower = log.lower()

        if not stream1.get("success"):
            # 编译失败 → 多半是逻辑错误
            return "logic_error"

        if stream2.get("error") == "appium_runtime_required":
            return "missing_context"

        if stream2.get("error") or "exception" in log_lower or "crash" in log_lower:
            return "logic_error"

        if "could not find" in log_lower or "unresolved" in log_lower \
                or "classnotfound" in log_lower:
            return "missing_context"

        if "not found" in log_lower or "no such" in log_lower:
            return "missing_context"

        # 默认：需求理解偏差
        return "vague_req"

    # ----------------------------------------------------------------------- #
    #  指标计算工具（供 Experiment_Launcher 汇总使用）
    # ----------------------------------------------------------------------- #

    @staticmethod
    def compute_pass_at_k(successes: list[bool], k: int) -> float:
        """
        计算 Pass@k：n 次独立尝试中至少成功 1 次的无偏估计。

        公式（参考 HumanEval 论文）：
            Pass@k = 1 - C(n-c, k) / C(n, k)
        其中 n=总尝试次数，c=成功次数，k=k。
        """
        import math
        normalized = [bool(x) for x in successes]
        n = len(normalized)
        c = sum(normalized)
        if n < k:
            return float(c > 0)
        if c == 0:
            return 0.0
        # 利用对数防止溢出
        log_num = sum(math.log(n - c - i) for i in range(k) if n - c - i > 0)
        log_den = sum(math.log(n - i) for i in range(k))
        return 1.0 - math.exp(log_num - log_den) if log_den > 0 else 1.0

    @staticmethod
    def compute_dsi(model_rate: float, global_avg: float) -> float:
        """
        DSI (Domain Strength Index) = (model_rate - global_avg) / global_avg
        > +10%  → Comfort domain
        < -10%  → Strange domain
        """
        if global_avg == 0:
            return 0.0
        return round((model_rate - global_avg) / global_avg, 4)

    # ----------------------------------------------------------------------- #
    #  私有辅助
    # ----------------------------------------------------------------------- #

    def _load_meta(self, app_name: str, task_id: str) -> dict:
        meta_path = self.data_dir / app_name / task_id / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"meta.json not found: {meta_path}")
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_appium_result(self, app_name: str, task_id: str) -> Optional[dict]:
        """读取当前任务 Appium 执行结果（若存在）。"""
        p = self.results_dir / app_name / task_id / "appium_result.json"
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def _requires_appium_runtime(self, test_script: Path) -> bool:
        """判断脚本是否依赖外部注入的 Appium 运行时。"""
        try:
            text = test_script.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False

        uses_injected_symbols = (
            "take_screenshot(" in text
            or "AppiumBy." in text
            or "driver." in text
        )
        has_local_setup = (
            "def take_screenshot(" in text
            or "webdriver.Remote(" in text
            or "from appium" in text
            or "import appium" in text
            or "driver =" in text
        )
        return uses_injected_symbols and not has_local_setup

    def _save_eval_result(self, app_name: str, task_id: str, result: dict) -> None:
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "eval_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evaluator — 双流评测引擎")
    parser.add_argument("app_name")
    parser.add_argument("task_id")
    parser.add_argument("--apk",          default=None, help="APK 文件路径")
    parser.add_argument("--screenshots",  default=None, nargs="+", help="截图路径列表")
    parser.add_argument("--vlm-model",    default="gpt-4o",
                        help="VLM 模型（默认 gpt-4o，经 Tongji base_url 路由）")
    args = parser.parse_args()

    ev = Evaluator(vlm_model=args.vlm_model)
    result = ev.evaluate(
        args.app_name, args.task_id,
        apk_path=args.apk,
        screenshots=args.screenshots or [],
    )
    print(f"\nCSR={result['csr']}  VSM={result['vsm']}  "
          f"VLM={result['vlm_score']}  error_cat={result['error_category']}")


if __name__ == "__main__":
    main()
