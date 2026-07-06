#!/usr/bin/env python3
"""
Evaluator.py - CSR & VSM 双流评测引擎

双流架构：
  Stream 1 (Static / Compilation):
      检查 APK 是否编译成功 → 计算 CSR (Compilation Success Rate)

  Stream 2 (Dynamic / Functional + Visual):
      2a. 功能性验证：读取 Appium crawler 的目标页 XML
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
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from tools.level2_utils import build_level2_spec, match_target_xml


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
        vlm_model:   str = "visual-judge",
        enable_level3: bool = True,
    ):
        root = Path(__file__).parent.parent
        self.data_dir    = root / data_dir
        self.results_dir = root / results_dir
        self.vlm_model   = vlm_model
        self.enable_level3 = enable_level3
        self._vlm: Optional[object] = None   # 懒加载，仅视觉任务才用

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
            apk_path           : 编译产物路径（None → 尝试读取 compilation_result.json）。
            screenshots        : Appium 截图路径列表（用于 VLM 评分）。
            appium_log         : Appium 测试日志（用于错误归因）。

        Returns:
            {
                "csr":            bool,
                "level1_score":   int,
                "level1_category": str,
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
        stream1 = self._stream1_compilation(app_name, task_id, apk_path)
        csr     = stream1["success"]

        # -------- Stream 2: 动态验证 -------- #
        stream2    = {}
        vsm        = False
        vlm_score  = None
        level2_score = None
        level3_score = None

        if csr:
            # 所有任务都遵循同一三级阶梯：Level 2 满分后才允许进入 Level 3。
            # 缺截图或 API key 时 Level 3 明确跳过，绝不产生模型调用。
            stream2, vsm, vlm_score = self._stream2_visual(
                app_name, task_id, meta, screenshots
            )
            level2_score = stream2.get("level2_score")
            level3_score = stream2.get("level3_score")
        else:
            stream2 = self._stream2_static_fallback_after_compile_failure(
                app_name, task_id, meta
            )
            level2_score = stream2.get("level2_score")
            level3_score = stream2.get("level3_score")

        total_score = self._compute_total_score(
            stream1.get("level1_score"), level2_score, level3_score
        )
        level2_detail = stream2.get("level2_detail") or {}
        file_fallback = level2_detail.get("file_fallback") or {}

        # -------- 错误归因（RQ4）-------- #
        error_category = None
        if not vsm:
            error_category = self._categorize_error(
                stream1, stream2, appium_log or ""
            )

        result = {
            "csr":            csr,
            "level1_score":   stream1.get("level1_score"),
            "level1_category": stream1.get("level1_category"),
            "level1_reason":  stream1.get("level1_reason"),
            "level2_score":   level2_score,
            "level2_reason":  stream2.get("level2_reason"),
            "matched_ui_node": level2_detail.get("matched_node"),
            "modified_file_overlap": {
                "golden_key": file_fallback.get("golden_key"),
                "overlap_files": file_fallback.get("overlap_files", []),
                "overlap_count": file_fallback.get("overlap_count", 0),
                "predicted_count": file_fallback.get("predicted_count", 0),
                "golden_count": file_fallback.get("golden_count", 0),
                "overlap_predicted_ratio": file_fallback.get("overlap_predicted_ratio", 0.0),
                "overlap_golden_ratio": file_fallback.get("overlap_golden_ratio", 0.0),
            },
            "level3_score":   level3_score,
            "level3_reason":  stream2.get("level3_reason"),
            "vlm_binary_checks": stream2.get("vlm_binary_checks"),
            "total_score":    total_score,
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

    def _stream1_compilation(
        self,
        app_name: str,
        task_id: str,
        apk_path: Optional[str],
    ) -> dict:
        """读取 EnvManager 的编译结果，给出 Level 1 分数和 CSR。"""
        compilation = self._load_compilation_result(app_name, task_id)
        if compilation is not None:
            success = bool(compilation.get("success", False))
            score, category, reason = self._normalize_level1_from_compilation(compilation)
            print(
                f"  [S1] {'BUILD OK' if success else 'BUILD FAILED'}  "
                f"level1={score}  category={category}"
            )
            return {
                "success": success,
                "apk_path": compilation.get("apk_path") or apk_path,
                "apk_exists": bool(compilation.get("apk_exists", False)),
                "apk_size_mb": compilation.get("apk_size_mb", 0.0),
                "return_code": compilation.get("return_code"),
                "warning_count": compilation.get("warning_count", 0),
                "warning_summary": compilation.get("warning_summary", ""),
                "error_summary": compilation.get("error_summary", ""),
                "level1_score": score,
                "level1_category": category,
                "level1_reason": reason,
                "level1_evidence": compilation.get("level1_evidence", []),
            }

        # 兼容旧调用：没有 compilation_result.json 时只能退化为 APK 存在性。
        if apk_path and Path(apk_path).exists():
            size_mb = round(Path(apk_path).stat().st_size / 1024 / 1024, 2)
            print(f"  [S1] APK OK  size={size_mb}MB  path={apk_path}")
            return {
                "success": True,
                "apk_path": apk_path,
                "apk_size_mb": size_mb,
                "level1_score": 4,
                "level1_category": "legacy_apk_exists",
                "level1_reason": "未找到 compilation_result.json，仅根据 APK 存在性兼容判定为编译成功。",
                "level1_evidence": [],
            }
        print(f"  [S1] APK not found: {apk_path}")
        return {
            "success": False,
            "apk_path": apk_path,
            "level1_score": 2,
            "level1_category": "compile_failed_unknown",
            "level1_reason": "未找到 compilation_result.json 或 APK，无法进一步区分错误类型。",
            "level1_evidence": [],
        }

    # ----------------------------------------------------------------------- #
    #  Stream 2a: 功能性验证（UI 树硬核校验）
    # ----------------------------------------------------------------------- #

    def _stream2_static_fallback_after_compile_failure(
        self,
        app_name: str,
        task_id: str,
        meta: dict,
    ) -> dict:
        """Level 1 编译失败时，Level 2/3 的前提均不成立。"""
        return {
            "passed": False,
            "skipped": True,
            "source": "compile_failure_gate",
            "reason": "Level 2 skipped because Level 1 compilation failed.",
            "level2_score": 0,
            "level2_reason": "Level 2 prerequisite not met: compilation failed.",
            "level2_detail": {
                "score": 0,
                "skipped": True,
                "reason": "Level 1 compilation must succeed before Level 2 scoring.",
            },
            "level3_score": None,
            "level3_reason": "Level 3 skipped: Level 1 compilation failed.",
        }

    def _stream2_functional(
        self,
        app_name: str,
        task_id:  str,
        meta: dict,
        appium_log: Optional[str],
        screenshots: Optional[list[str]],
    ) -> tuple[dict, bool]:
        """
        功能性校验使用 Appium crawler 执行结果；不再运行人工 UI 脚本。
        """
        level2 = self._score_level2(app_name, task_id, meta)
        appium_result = self._load_appium_result(app_name, task_id)
        if appium_result is not None:
            passed = level2["score"] == 2
            shots = appium_result.get("screenshots") or []
            detail = {
                "passed":      passed,
                "source":      "appium_result",
                "level2_score": level2["score"],
                "level2_reason": level2["reason"],
                "level2_detail": level2,
                "level3_score": None,
                "elapsed_s":   appium_result.get("elapsed_time"),
                "shots_used":  len(shots),
                "target_page_xml": level2.get("target_xml_path"),
                "test_type":   appium_result.get("test_type", "custom"),
                "timestamp":   appium_result.get("timestamp"),
                "appium_log":  (appium_log or "")[-2000:],
            }
            print(f"  [S2-func] level2={level2['score']}  source=appium_result")
            return detail, passed

        if screenshots:
            passed = level2["score"] == 2
            detail = {
                "passed":     passed,
                "source":     "screenshots_only",
                "level2_score": level2["score"],
                "level2_reason": level2["reason"],
                "level2_detail": level2,
                "level3_score": None,
                "shots_used": len(screenshots),
                "target_page_xml": level2.get("target_xml_path"),
                "note":       "appium_result.json missing; Level 2 evaluated from target-page XML or file fallback",
            }
            print(f"  [S2-func] level2={level2['score']}  source=screenshots_only")
            return detail, passed

        if appium_log:
            detail = {
                "passed": False,
                "source": "appium_log_only",
                "level2_score": level2["score"],
                "level2_reason": level2["reason"],
                "level2_detail": level2,
                "level3_score": None,
                "error": "appium_execution_unavailable_or_failed",
                "reason": appium_log[-2000:],
            }
            print("  [S2-func] FAIL  source=appium_log_only")
            return detail, False

        print("  [S2-func] appium_result.json missing")
        return {
            "passed": False,
            "skipped": True,
            "source": "appium_result_missing",
            "level2_score": level2["score"],
            "level2_reason": level2["reason"],
            "level2_detail": level2,
            "level3_score": None,
            "error": "appium_result_missing",
            "reason": "Level 2/functional scoring now relies on Appium crawler output.",
        }, False

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
        """Level 3 视觉语义校验：必须先达到 Level 2=2，再调用 VLM 二项检查。"""
        level2 = self._score_level2(app_name, task_id, meta)
        appium_result = self._load_appium_result(app_name, task_id) or {}
        shots = list(screenshots or appium_result.get("screenshots") or [])
        valid_shots = [p for p in shots if p and Path(p).exists()]
        base_detail = {
            "source": "visual",
            "passed": False,
            "level2_score": level2["score"],
            "level2_reason": level2["reason"],
            "level2_detail": level2,
            "level3_score": 0,
            "level3_reason": "",
            "vlm_binary_checks": [],
            "shots_used": len(valid_shots),
            "model": self.vlm_model,
        }

        if level2["score"] != 2:
            base_detail["skipped"] = True
            base_detail["level3_score"] = None
            base_detail["level3_reason"] = "Level 3 skipped: Level 2 did not prove the target UI node exists."
            print("  [S3-visual] SKIP  level2 gate not satisfied")
            return base_detail, False, None

        if not self.enable_level3:
            base_detail["skipped"] = True
            base_detail["level3_score"] = None
            base_detail["level3_reason"] = "Level 3 disabled for this run."
            print("  [S3-visual] SKIP  explicitly disabled")
            return base_detail, False, None

        if not valid_shots:
            base_detail["skipped"] = True
            base_detail["level3_score"] = None
            base_detail["level3_reason"] = "Level 3 skipped: no valid screenshots available."
            print("  [S3-visual] SKIP  no valid screenshots")
            return base_detail, False, None

        from llm_client import LLMClient, has_configured_api_key

        if not has_configured_api_key(self.vlm_model):
            base_detail["skipped"] = True
            base_detail["level3_score"] = None
            base_detail["level3_reason"] = "VLM unavailable: missing API key"
            print("  [S3-visual] SKIP  missing VLM API key")
            return base_detail, False, None

        checks = self._build_level3_binary_checks(meta, level2.get("matched_node"))
        try:
            if self._vlm is None:
                self._vlm = LLMClient(model=self.vlm_model)
            vlm_result = self._vlm.score_visual_binary_checks(
                task_prompt=meta.get("prompt", ""),
                screenshot_paths=valid_shots,
                checks=checks,
                target_node=level2.get("matched_node"),
            )
        except Exception as exc:
            # A judge outage must not erase valid Level 1/2 evidence, abort the
            # experiment, or falsely score the app as a visual failure.
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            detail = {
                **base_detail,
                "passed": False,
                "skipped": True,
                "level3_score": None,
                "level3_reason": f"Level 3 unavailable: visual judge request failed: {error}",
                "vlm_error": error,
                "vlm_binary_checks": [],
            }
            print(f"  [S3-visual] UNAVAILABLE  judge_error={type(exc).__name__}")
            return detail, False, None
        normalized = self._normalize_vlm_binary_result(vlm_result, checks)
        passed = bool(normalized["passed"])
        score = 1 if passed else 0
        detail = {
            **base_detail,
            "passed": passed,
            "skipped": False,
            "level3_score": score,
            "level3_reason": normalized.get("reason", ""),
            "vlm_binary_checks": normalized.get("checks", []),
        }
        print(f"  [S3-visual] level3={score}  checks={len(detail['vlm_binary_checks'])}")
        return detail, passed, score

    def _build_level3_binary_checks(
        self,
        meta: dict,
        matched_node: Optional[dict],
    ) -> list[dict]:
        """构造客观二项视觉检查，禁止 VLM 自由打分。"""
        configured = meta.get("level3_binary_checks") or meta.get("visual_binary_checks")
        if isinstance(configured, list) and configured:
            checks = []
            for idx, item in enumerate(configured, start=1):
                if isinstance(item, dict) and item.get("question"):
                    checks.append({
                        "id": str(item.get("id", f"custom_{idx}")),
                        "question": str(item["question"]),
                    })
                elif isinstance(item, str):
                    checks.append({"id": f"custom_{idx}", "question": item})
            if checks:
                return checks

        node_text = ""
        if matched_node:
            node_attrs = matched_node.get("node") or {}
            node_text = (
                node_attrs.get("text")
                or node_attrs.get("content-desc")
                or node_attrs.get("resource-id")
                or "the target UI element"
            )
        else:
            node_text = "the target UI element"

        prompt = meta.get("prompt", "")
        checks = [
            {
                "id": "target_visible",
                "question": f"Is {node_text!r} visibly present in the screenshot?",
            },
            {
                "id": "not_obscured",
                "question": f"Is {node_text!r} not covered, clipped, disabled-looking, or hidden behind another UI element?",
            },
            {
                "id": "reasonable_position",
                "question": f"Is {node_text!r} placed in a plausible location for its role, not floating in an unrelated or broken area?",
            },
            {
                "id": "style_consistent",
                "question": f"Does {node_text!r} visually fit the surrounding app theme and layout without obvious rendering defects?",
            },
        ]

        lower_prompt = prompt.casefold()
        if any(word in lower_prompt for word in ("pink", "girly", "girl skull", "skull", "少女骷髅")):
            checks.append({
                "id": "pink_theme",
                "question": "If the task asks for a pink/girly theme, is a visibly pink color treatment present on the relevant UI?",
            })
        if "skull" in lower_prompt or "骷髅" in prompt:
            checks.append({
                "id": "skull_elements",
                "question": "If the task asks for skull styling, are skull-style visual elements visibly present?",
            })
        if any(word in lower_prompt for word in ("button", "按钮", "share", "search", "download", "offline", "save")):
            checks.append({
                "id": "button_affordance",
                "question": "If the target is a button/action, does it look tappable as a button or icon action?",
            })
        return checks

    def _normalize_vlm_binary_result(self, vlm_result: dict, expected_checks: list[dict]) -> dict:
        """只接受二项结果，并用 checks 全部通过重新计算总 passed。"""
        expected_by_id = {str(c["id"]): c for c in expected_checks}
        raw_checks = vlm_result.get("checks") if isinstance(vlm_result, dict) else []
        raw_checks = raw_checks if isinstance(raw_checks, list) else []

        normalized_checks = []
        seen: set[str] = set()
        for raw in raw_checks:
            if not isinstance(raw, dict):
                continue
            check_id = str(raw.get("id", "")).strip()
            if check_id not in expected_by_id:
                continue
            normalized_checks.append({
                "id": check_id,
                "question": expected_by_id[check_id]["question"],
                "passed": bool(raw.get("passed", False)),
                "evidence": str(raw.get("evidence", ""))[:300],
            })
            seen.add(check_id)

        for check_id, check in expected_by_id.items():
            if check_id not in seen:
                normalized_checks.append({
                    "id": check_id,
                    "question": check["question"],
                    "passed": False,
                    "evidence": "Missing check result from VLM response.",
                })

        passed = bool(normalized_checks) and all(c["passed"] for c in normalized_checks)
        reason = ""
        if isinstance(vlm_result, dict):
            reason = str(vlm_result.get("reason", ""))
        if not reason:
            reason = "All binary checks passed." if passed else "At least one binary check failed."
        return {"passed": passed, "checks": normalized_checks, "reason": reason}

    def _compute_total_score(
        self,
        level1_score: Optional[int],
        level2_score: Optional[int],
        level3_score: Optional[int],
    ) -> int:
        """总分为 Level 1 + Level 2 + Level 3；跳过项按 0 计入总分。"""
        return sum(
            int(score)
            for score in (level1_score, level2_score, level3_score)
            if score is not None
        )

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
            level1_category = stream1.get("level1_category")
            if level1_category == "dependency_or_context_error":
                return "missing_context"
            if level1_category == "invalid_code_output":
                return "invalid_code"
            if level1_category == "syntax_or_local_error":
                return "syntax_error"
            return "compile_error"

        if stream2.get("error") in {"appium_result_missing", "appium_runtime_required"}:
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
    #  Level 2: UI 节点命中 + 文件命中 fallback
    # ----------------------------------------------------------------------- #

    def _score_level2(self, app_name: str, task_id: str, meta: dict) -> dict:
        """
        Level 2 评分：
          2 分：crawler 找到的目标页面 XML 中精确匹配关键节点属性。
          1 分：目标页 XML 未命中，但实际改动文件与 TODO1 golden mapping 命中。
          0 分：目标页 XML 未命中，且改动文件未命中 golden mapping。

        注意：这里只使用 crawler 最终认定的目标页 XML，不遍历沿途页面 XML。
        """
        target_xml_path = self._collect_target_xml_path(app_name, task_id)
        expected_nodes = self._expected_level2_nodes(app_name, task_id, meta)
        spec = build_level2_spec(
            meta,
            self.data_dir / app_name / "base_src",
            self.data_dir / app_name / task_id / "golden_src",
        )
        node_match = None
        xml_error = None

        if target_xml_path and target_xml_path.exists():
            node_match = self._match_expected_nodes([target_xml_path], expected_nodes)
            if node_match is None:
                try:
                    xml_text = target_xml_path.read_text(encoding="utf-8", errors="replace")
                    util_match = match_target_xml(xml_text, spec)
                    if util_match.get("matched"):
                        first = (util_match.get("matched_nodes") or [{}])[0]
                        attr = first.get("attribute")
                        node_match = {
                            "tree_path": str(target_xml_path),
                            "matched_attribute": attr,
                            "expected": first.get("keyword"),
                            "node": {attr: first.get("value", "")} if attr else {},
                            "matched_nodes": util_match.get("matched_nodes", []),
                        }
                except Exception as exc:
                    xml_error = str(exc)
        else:
            xml_error = "target_page_xml_missing"

        if node_match is not None:
            return {
                "score": 2,
                "reason": "目标页面 XML 中精确匹配到任务要求的关键节点属性。",
                "target_xml_path": str(target_xml_path) if target_xml_path else None,
                "expected_nodes": expected_nodes,
                "level2_spec": spec,
                "matched_node": node_match,
                "file_fallback": None,
                "xml_error": xml_error,
            }

        file_fallback = self._score_level2_file_fallback(app_name, task_id, meta)
        if file_fallback["passed"]:
            return {
                "score": 1,
                "reason": "目标页面 XML 未匹配关键节点，但实际改动文件命中标准答案关键文件。",
                "target_xml_path": str(target_xml_path) if target_xml_path else None,
                "expected_nodes": expected_nodes,
                "level2_spec": spec,
                "matched_node": None,
                "file_fallback": file_fallback,
                "xml_error": xml_error,
            }

        return {
            "score": 0,
            "reason": "目标页面 XML 未匹配关键节点，实际改动文件也未达到标准答案命中阈值。",
            "target_xml_path": str(target_xml_path) if target_xml_path else None,
            "expected_nodes": expected_nodes,
            "level2_spec": spec,
            "matched_node": None,
            "file_fallback": file_fallback,
            "xml_error": xml_error,
        }

    def _collect_target_xml_path(self, app_name: str, task_id: str) -> Optional[Path]:
        appium_result = self._load_appium_result(app_name, task_id) or {}
        target_page = appium_result.get("target_page") or {}
        xml_path = target_page.get("ui_dom_tree_path")
        if xml_path:
            p = Path(xml_path)
            if p.exists():
                return p

        fallback = self.results_dir / app_name / task_id / "ui_context" / "target_page.xml"
        if fallback.exists():
            return fallback
        return None

    def _expected_level2_nodes(self, app_name: str, task_id: str, meta: dict) -> list[dict]:
        configured = meta.get("level2_expected_nodes") or meta.get("target_ui_nodes")
        if isinstance(configured, dict):
            return [configured]
        if isinstance(configured, list) and configured:
            return [x for x in configured if isinstance(x, dict)]

        terms: list[str] = []
        terms.extend(self._extract_prompt_terms(meta.get("prompt", "")))

        filtered = []
        seen: set[str] = set()
        for term in terms:
            for part in self._split_selector_term(term):
                norm = self._normalize_node_value(part)
                if not norm or norm in seen or self._is_setup_term(norm):
                    continue
                filtered.append(part.strip())
                seen.add(norm)

        # 后出现的 selector 通常更接近最终任务目标，因此反向优先；最多保留 20 个。
        filtered = list(reversed(filtered))[:20]
        return [{"text": term, "content-desc": term} for term in filtered]

    def _extract_prompt_terms(self, prompt: str) -> list[str]:
        terms: list[str] = []
        for pattern in (r"[“\"']([^“”\"']{2,80})[”\"']", r"\*\*([^*]{2,80})\*\*"):
            terms.extend(m.group(1) for m in re.finditer(pattern, prompt))
        keyword_patterns = [
            r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Theme|button|settings?|menu)\b",
            r"\b(?:Bengali|Korean|Pink Skull|Girl Skull|Girly Skull|Color blind|High contrast|Paragraph spacing|Line spacing)\b",
        ]
        for pattern in keyword_patterns:
            terms.extend(m.group(0) for m in re.finditer(pattern, prompt, re.IGNORECASE))
        return terms


    def _split_selector_term(self, term: str) -> list[str]:
        raw_parts = re.split(r"\||/", term)
        parts: list[str] = []
        for part in raw_parts:
            cleaned = re.sub(r"[.*+?^${}()[\]\\]", "", part).strip()
            if cleaned:
                parts.append(cleaned)
        return parts

    def _is_setup_term(self, norm: str) -> bool:
        setup_terms = {
            "ok", "好的", "確定", "同意", "agree", "跳过", "skip", "cancel", "取消",
            "later", "稍后", "settings", "设置", "設定", "more options", "更多选项",
            "更多選項", "automatic", "自动", "default", "white", "allow", "got it",
        }
        return norm in {self._normalize_node_value(x) for x in setup_terms}

    def _match_expected_nodes(self, ui_tree_paths: list[Path], expected_nodes: list[dict]) -> Optional[dict]:
        if not ui_tree_paths or not expected_nodes:
            return None
        for tree_path in ui_tree_paths:
            try:
                root = ET.parse(tree_path).getroot()
            except Exception:
                continue
            # Appium may serialize UI nodes either as generic <node> elements
            # or as class-name elements such as <android.widget.TextView>.
            for node in root.iter():
                if node is root:
                    continue
                attrs = {
                    "text": node.attrib.get("text", ""),
                    "content-desc": node.attrib.get("content-desc", ""),
                    "resource-id": node.attrib.get("resource-id", ""),
                    "class": node.attrib.get("class", ""),
                    "bounds": node.attrib.get("bounds", ""),
                    "clickable": node.attrib.get("clickable", ""),
                    "enabled": node.attrib.get("enabled", ""),
                }
                for expected in expected_nodes:
                    matched = self._node_matches_expected(attrs, expected)
                    if matched:
                        return {
                            "tree_path": str(tree_path),
                            "matched_attribute": matched,
                            "expected": expected,
                            "node": attrs,
                        }
        return None

    def _node_matches_expected(self, attrs: dict, expected: dict) -> Optional[str]:
        attr_aliases = {
            "content_desc": "content-desc",
            "contentDescription": "content-desc",
            "resource_id": "resource-id",
            "resourceId": "resource-id",
        }
        for key, expected_value in expected.items():
            xml_key = attr_aliases.get(key, key)
            if xml_key not in ("text", "content-desc", "resource-id", "class"):
                continue
            if expected_value is None:
                continue
            values = expected_value if isinstance(expected_value, list) else [expected_value]
            actual = self._normalize_node_value(attrs.get(xml_key, ""))
            for value in values:
                if actual and actual == self._normalize_node_value(str(value)):
                    return xml_key
        return None

    def _normalize_node_value(self, value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip().casefold()

    def _score_level2_file_fallback(self, app_name: str, task_id: str, meta: dict) -> dict:
        predicted = self._collect_modified_files(app_name, task_id)
        golden_key, golden_files, candidates = self._load_best_golden_mapping(app_name, task_id, meta)
        core_exts = {".kt", ".xml", ".java", ".kts"}
        predicted_set = {p for p in predicted if Path(p).suffix.lower() in core_exts}
        golden_set = {p for p in golden_files if Path(p).suffix.lower() in core_exts}
        overlap = sorted(predicted_set & golden_set)
        predicted_count = len(predicted_set)
        golden_count = len(golden_set)
        overlap_predicted_ratio = len(overlap) / predicted_count if predicted_count else 0.0
        overlap_golden_ratio = len(overlap) / golden_count if golden_count else 0.0
        passed = len(overlap) >= 1
        return {
            "passed": passed,
            "golden_key": golden_key,
            "golden_candidates_scored": candidates[:5],
            "predicted_modified_files": predicted,
            "golden_modified_files": golden_files,
            "overlap_files": overlap,
            "overlap_count": len(overlap),
            "predicted_count": predicted_count,
            "golden_count": golden_count,
            "core_extensions": sorted(core_exts),
            "thresholds": {
                "min_overlap": 1,
            },
            "overlap_predicted_ratio": round(overlap_predicted_ratio, 4),
            "overlap_golden_ratio": round(overlap_golden_ratio, 4),
        }

    def _collect_modified_files(self, app_name: str, task_id: str) -> list[str]:
        workspace_task = self.results_dir.parent / "workspace" / app_name / task_id
        base_src = self.data_dir / app_name / "base_src"
        return self._collect_modified_files_between_roots(base_src, workspace_task)

    def _collect_modified_files_between_roots(self, base_src: Path, candidate_src: Path) -> list[str]:
        workspace_root = self._resolve_project_root_for_diff(candidate_src)
        base_root = self._resolve_project_root_for_diff(base_src)
        if not workspace_root.exists() or not base_root.exists():
            return []

        ignore_dirs = {"build", ".gradle", ".idea", ".git", ".kotlin", ".cxx", "__pycache__"}
        workspace_files = self._relative_file_hashes(workspace_root, ignore_dirs)
        base_files = self._relative_file_hashes(base_root, ignore_dirs)
        changed = []
        for rel, digest in workspace_files.items():
            if rel not in base_files or base_files[rel] != digest:
                changed.append(rel)
        for rel in base_files:
            if rel not in workspace_files:
                changed.append(rel)
        return sorted(set(changed))

    def _resolve_project_root_for_diff(self, root: Path) -> Path:
        if not root.exists():
            return root
        for wrapper in ("gradlew", "gradlew.bat"):
            if (root / wrapper).exists():
                return root
            candidates = list(root.glob(f"*/{wrapper}"))
            if candidates:
                return candidates[0].parent
        nested = [p for p in root.iterdir() if p.is_dir()]
        return nested[0] if len(nested) == 1 else root

    def _relative_file_hashes(self, root: Path, ignore_dirs: set[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root)
            if any(part in ignore_dirs for part in rel_path.parts):
                continue
            try:
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                hashes[rel_path.as_posix()] = h.hexdigest()
            except Exception:
                continue
        return hashes

    def _load_best_golden_mapping(
        self,
        app_name: str,
        task_id: str,
        meta: dict,
    ) -> tuple[Optional[str], list[str], list[dict]]:
        explicit_key = meta.get("golden_task_key") or meta.get("todo1_key")
        mappings = self._load_golden_mapping_for_app(app_name, meta)
        if not mappings:
            task_dir = self.data_dir / app_name / task_id
            base_src = self.data_dir / app_name / "base_src"
            for source_name in ("golden_src", "ground_truth_src"):
                candidate = task_dir / source_name
                if candidate.exists():
                    files = self._collect_modified_files_between_roots(base_src, candidate)
                    if files:
                        key = f"direct:{source_name}"
                        return key, files, [{"key": key, "score": "direct_ground_truth_diff"}]
            return None, [], []
        if explicit_key and explicit_key in mappings:
            files = self._normalize_golden_files(mappings[explicit_key].get("modified_files", []))
            return explicit_key, files, [{"key": explicit_key, "score": "explicit"}]

        scored = self._score_golden_candidates(app_name, task_id, meta, mappings)
        ground_truth_key = self._infer_golden_key_from_ground_truth(app_name, task_id, mappings)
        if ground_truth_key and ground_truth_key in mappings:
            return (
                ground_truth_key,
                self._normalize_golden_files(mappings[ground_truth_key].get("modified_files", [])),
                [{"key": ground_truth_key, "score": "ground_truth_diff"}] + scored,
            )

        assigned_key = self._infer_golden_key_by_app_assignment(app_name, task_id, mappings)
        if assigned_key and assigned_key in mappings:
            return (
                assigned_key,
                self._normalize_golden_files(mappings[assigned_key].get("modified_files", [])),
                scored,
            )

        best = scored[0]
        if best["score"] <= 0:
            return None, [], scored
        key = best["key"]
        return key, self._normalize_golden_files(mappings[key].get("modified_files", [])), scored

    def _infer_golden_key_from_ground_truth(
        self,
        app_name: str,
        task_id: str,
        mappings: dict,
    ) -> Optional[str]:
        task_dir = self.data_dir / app_name / task_id
        base_src = self.data_dir / app_name / "base_src"
        if not task_dir.exists() or not base_src.exists():
            return None

        candidates = [task_dir / "golden_src", task_dir / "ground_truth_src"]
        candidates.extend(
            p for p in task_dir.iterdir()
            if p.is_dir()
            and p.name not in {"golden_src", "ground_truth_src", "__pycache__"}
        )

        best_key = None
        best_overlap = 0
        for candidate in candidates:
            if not candidate.exists():
                continue
            gt_files = set(self._collect_modified_files_between_roots(base_src, candidate))
            if not gt_files:
                continue
            for key, value in mappings.items():
                golden_files = set(self._normalize_golden_files(value.get("modified_files", [])))
                overlap = len(gt_files & golden_files)
                if overlap > best_overlap:
                    best_key = key
                    best_overlap = overlap
        return best_key if best_overlap > 0 else None

    def _score_golden_candidates(
        self,
        app_name: str,
        task_id: str,
        meta: dict,
        mappings: dict,
    ) -> list[dict]:
        terms = []
        terms.extend(self._extract_prompt_terms(meta.get("prompt", "")))
        terms.extend(self._language_alias_terms(meta.get("prompt", "")))
        normalized_terms = {
            self._normalize_path_token(t)
            for term in terms
            for t in self._split_selector_term(term)
            if len(self._normalize_path_token(t)) >= 3
        }

        scored = []
        for key, value in mappings.items():
            files = self._normalize_golden_files(value.get("modified_files", []))
            haystack = self._normalize_path_token(" ".join([key] + files))
            score = sum(1 for term in normalized_terms if term and term in haystack)
            scored.append({"key": key, "score": score, "total_modified_count": len(files)})
        scored.sort(key=lambda x: (x["score"], -x["total_modified_count"]), reverse=True)
        return scored

    def _infer_golden_key_by_app_assignment(
        self,
        app_name: str,
        task_id: str,
        mappings: dict,
    ) -> Optional[str]:
        """在缺少显式 golden_task_key 时，对同一 app 的任务和 golden key 做唯一匹配。"""
        app_dir = self.data_dir / app_name
        if not app_dir.exists():
            return None

        task_scores: list[dict] = []
        for meta_path in sorted(app_dir.glob("task_*/meta.json")):
            try:
                task_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            current_task_id = meta_path.parent.name
            explicit_key = task_meta.get("golden_task_key") or task_meta.get("todo1_key")
            if explicit_key and explicit_key in mappings:
                if current_task_id == task_id:
                    return explicit_key
                continue
            scored = self._score_golden_candidates(app_name, current_task_id, task_meta, mappings)
            for item in scored:
                if item["score"] > 0:
                    task_scores.append({
                        "task_id": current_task_id,
                        "key": item["key"],
                        "score": item["score"],
                        "total_modified_count": item["total_modified_count"],
                    })

        task_scores.sort(key=lambda x: (-x["score"], x["total_modified_count"], x["task_id"]))
        assigned_tasks: set[str] = set()
        assigned_keys: set[str] = set()
        assignment: dict[str, str] = {}
        for item in task_scores:
            if item["task_id"] in assigned_tasks or item["key"] in assigned_keys:
                continue
            assignment[item["task_id"]] = item["key"]
            assigned_tasks.add(item["task_id"])
            assigned_keys.add(item["key"])
        return assignment.get(task_id)

    def _load_golden_mapping_for_app(self, app_name: str, meta: dict) -> dict:
        todo_dir = self.data_dir / "TODO1_output"
        app_label = (meta.get("name") or app_name.replace("app_", "")).lower()
        if not todo_dir.exists():
            return {}
        candidates = [
            p for p in todo_dir.glob("*_golden_mapping.json")
            if p.stem.replace("_golden_mapping", "").lower() == app_label
        ]
        if not candidates:
            candidates = [
                p for p in todo_dir.glob("*_golden_mapping.json")
                if app_label in p.stem.lower()
            ]
        if not candidates:
            return {}
        try:
            with open(candidates[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _normalize_golden_files(self, files: list[str]) -> list[str]:
        normalized = []
        for path in files:
            path = str(path).replace("\\", "/").strip()
            for prefix in ("FoodYou-develop/", "rssreader-main/"):
                if path.startswith(prefix):
                    path = path[len(prefix):]
            normalized.append(path)
        return sorted(set(normalized))

    def _normalize_path_token(self, value: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff가-힣]+", "", value.casefold())

    def _language_alias_terms(self, prompt: str) -> list[str]:
        lower = prompt.casefold()
        terms = []
        if "bengali" in lower or "孟加拉" in prompt:
            terms.extend(["bengali", "bn", "bd", "valuesbn", "বাংলা"])
        if "korean" in lower or "韩国" in prompt or "韓國" in prompt or "한국" in prompt:
            terms.extend(["korean", "ko", "kr", "valuesko", "한국", "韩国"])
        return terms

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

    def _load_compilation_result(self, app_name: str, task_id: str) -> Optional[dict]:
        """读取 EnvManager 保存的结构化编译结果。"""
        p = self.results_dir / app_name / task_id / "compilation_result.json"
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

    def _normalize_level1_from_compilation(self, compilation: dict) -> tuple[int, str, str]:
        """兼容旧版 compilation_result，同时优先使用 EnvManager 新字段。"""
        score = compilation.get("level1_score")
        category = compilation.get("level1_category")
        reason = compilation.get("level1_reason")
        if score is not None and category and reason:
            return int(score), str(category), str(reason)

        success = bool(compilation.get("success", False))
        warning_count = int(compilation.get("warning_count") or 0)
        if success and warning_count > 0:
            return 3, "compiled_with_warnings", "旧版编译结果缺少 Level 1 字段；根据 warning_count 推断。"
        if success:
            return 4, "perfect_compile", "旧版编译结果缺少 Level 1 字段；根据编译成功且无 warning_count 推断。"
        return 2, "compile_failed_unknown", "旧版编译结果缺少 Level 1 字段；无法细分失败类型。"

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
    parser.add_argument("--vlm-model",    default="visual-judge",
                        help="Level 3 专用视觉评委（默认读取 .env 中的 VLM_* 配置）")
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
