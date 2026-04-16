#!/usr/bin/env python3
"""Three-level scoring engine (L1 compile, L2 structure, L3 visual)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from llm_api.client import LLMClient
from tools.task_config import (
    find_golden_mapping,
    find_task_config,
    synthesize_meta_from_task_config,
)


_CODE_EXTS = {
    ".kt", ".kts", ".java", ".xml", ".gradle", ".json", ".properties", ".txt", ".md"
}
_IGNORE_PARTS = {"build", ".gradle", ".idea", ".kotlin", "cache", ".git"}


class Scorer:
    """Implements the L1-L3 staircase scoring rubric."""

    def __init__(
        self,
        data_dir: str = "data",
        workspace_dir: str = "workspace",
        results_dir: str = "results",
        vlm_model: str = "gpt-4o",
    ):
        root = Path(__file__).parent.parent
        self.data_dir = root / data_dir
        self.workspace_dir = root / workspace_dir
        self.results_dir = root / results_dir
        self.vlm_model = vlm_model
        self._vlm: Optional[LLMClient] = None

    def score_task(
        self,
        app_name: str,
        task_id: str,
        agent_result: Optional[dict],
        compile_result: Optional[dict],
        appium_result: Optional[dict],
    ) -> dict:
        meta = self._load_meta(app_name, task_id)
        recall = self._compute_file_recall(app_name, task_id, meta.get("task_key"))

        l1 = self._score_l1(agent_result, compile_result, recall)
        l2 = self._score_l2(meta, appium_result, recall, l1)
        l3 = self._score_l3(meta, appium_result, l2)

        total = l1["score"] + l2["score"] + l3["score"]
        result = {
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "total_score": total,
            "recall": recall,
            "pass": total >= 6,
        }
        self._save_result(app_name, task_id, result)
        return result

    def _score_l1(self, agent_result: Optional[dict], compile_result: Optional[dict], recall: float) -> dict:
        if not agent_result or not agent_result.get("success"):
            return {"score": 0, "reason": "agent_output_invalid", "warning_count": 0}

        compile_result = compile_result or {}
        ok = bool(compile_result.get("success", False))
        out = (compile_result.get("stdout") or "") + "\n" + (compile_result.get("stderr") or "")
        warning_count = len(re.findall(r"(?im)\bwarning\b|deprecated", out))

        if ok:
            if warning_count == 0:
                return {"score": 4, "reason": "compile_clean", "warning_count": 0}
            return {"score": 3, "reason": "compile_with_warnings", "warning_count": warning_count}

        lower = out.lower()
        syntax_hit = any(k in lower for k in [
            "syntax", "expecting", "unexpected token", "';' expected", "parse error", "unclosed", "mismatched"
        ])
        dep_hit = any(k in lower for k in [
            "unresolved reference", "cannot find symbol", "package does not exist", "failed to resolve", "classnotfound"
        ])

        if syntax_hit and recall >= 0.5:
            return {"score": 2, "reason": "compile_fail_syntax_local", "warning_count": warning_count}
        if dep_hit and recall > 0:
            return {"score": 1, "reason": "compile_fail_dependency_context", "warning_count": warning_count}
        if recall > 0:
            return {"score": 1, "reason": "compile_fail_low_recall", "warning_count": warning_count}
        return {"score": 0, "reason": "compile_fail_hallucination", "warning_count": warning_count}

    def _score_l2(self, meta: dict, appium_result: Optional[dict], recall: float, l1: dict) -> dict:
        if l1.get("score", 0) < 3:
            return {"score": 0, "reason": "skipped_l1_not_passed"}

        appium_result = appium_result or {}
        xml_path = appium_result.get("ui_xml_path")
        verify = meta.get("target_ui_verification") or {}

        if not xml_path or not Path(xml_path).exists() or not verify:
            if recall > 0:
                return {"score": 1, "reason": "ui_tree_missing_recall_partial"}
            return {"score": 0, "reason": "ui_tree_missing_recall_zero"}

        matched = self._verify_xpath_in_xml(Path(xml_path), verify)
        if matched:
            return {"score": 2, "reason": "ui_target_node_matched"}

        if recall > 0:
            return {"score": 1, "reason": "ui_node_missing_but_file_recall_positive"}
        return {"score": 0, "reason": "ui_node_missing_and_file_recall_zero"}

    def _score_l3(self, meta: dict, appium_result: Optional[dict], l2: dict) -> dict:
        if l2.get("score", 0) < 2:
            return {"score": 0, "reason": "skipped_l2_not_passed"}

        shots = (appium_result or {}).get("screenshots") or []
        valid = [p for p in shots if Path(p).exists()]
        if not valid:
            return {"score": 0, "reason": "no_screenshot_for_visual"}

        if self._vlm is None:
            self._vlm = LLMClient(model=self.vlm_model, strategy="direct")

        task_prompt = meta.get("prompt", "")
        check = meta.get("target_ui_verification", {})
        visual_prompt = (
            task_prompt
            + "\n\nOnly answer whether target UI is visually correct (not occluded, position reasonable, style aligned)."
            + f"\nTarget selector: {check.get('value', '')}"
            + "\nReturn JSON with boolean passed field."
        )
        vlm = self._vlm.score_screenshot(visual_prompt, [valid[-1]], temperature=0.0)
        passed = bool(vlm.get("passed", False))
        return {
            "score": 1 if passed else 0,
            "reason": "visual_pass" if passed else "visual_fail",
            "vlm": vlm,
        }

    def _load_meta(self, app_name: str, task_id: str) -> dict:
        p = self.data_dir / app_name / task_id / "meta.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))

        found = find_task_config(self.data_dir, app_name=app_name, task_id=task_id)
        if found:
            task_key, cfg = found
            cfg = dict(cfg)
            cfg.setdefault("task_key", task_key)
            cfg.setdefault("task_id", task_id)
            return synthesize_meta_from_task_config(cfg) | {
                "target_ui_verification": cfg.get("target_ui_verification"),
                "navigation_steps": cfg.get("navigation_steps", []),
            }
        return {}

    def _resolve_project_root(self, root: Path) -> Path:
        if (root / "gradlew").exists() or (root / "gradlew.bat").exists():
            return root
        cands = list(root.glob("*/gradlew")) + list(root.glob("*/gradlew.bat"))
        return cands[0].parent if cands else root

    def _collect_hashes(self, root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        if not root.exists():
            return hashes
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _CODE_EXTS:
                continue
            if set(p.parts) & _IGNORE_PARTS:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            h = hashlib.md5(p.read_bytes()).hexdigest()
            hashes[rel] = h
        return hashes

    def _diff_files(self, base_root: Path, other_root: Path) -> set[str]:
        a = self._collect_hashes(base_root)
        b = self._collect_hashes(other_root)
        all_keys = set(a) | set(b)
        return {k for k in all_keys if a.get(k) != b.get(k)}

    def _compute_file_recall(self, app_name: str, task_id: str, task_key: Optional[str] = None) -> float:
        base_root = self._resolve_project_root(self.data_dir / app_name / "base_src")
        ws_root = self._resolve_project_root(self.workspace_dir / app_name / task_id)

        golden = find_golden_mapping(self.data_dir, app_name=app_name, task_key=task_key)
        if golden and isinstance(golden.get("modified_files"), list):
            g = {
                str(p).replace("\\", "/")
                for p in golden.get("modified_files", [])
                if str(p).strip()
            }
        else:
            gt_root = self._resolve_project_root(self.data_dir / app_name / task_id / "ground_truth_src")
            g = self._diff_files(base_root, gt_root)

        m = self._diff_files(base_root, ws_root)
        if not g:
            return 0.0
        return round(len(m & g) / len(g), 4)

    def _verify_xpath_in_xml(self, xml_path: Path, verify: dict) -> bool:
        xpath = verify.get("value")
        if not xpath:
            return False

        try:
            from lxml import etree
        except Exception:
            return False

        try:
            root = etree.fromstring(xml_path.read_text(encoding="utf-8", errors="replace").encode("utf-8"))
            nodes = root.xpath(xpath)
        except Exception:
            return False

        if not nodes:
            return False

        expected = verify.get("expected_attributes") or verify.get("expected_attrs") or {}
        if not expected:
            return True

        for node in nodes:
            ok = True
            for key, value in expected.items():
                attr_val = node.attrib.get(key)
                if attr_val is None or str(attr_val).lower() != str(value).lower():
                    ok = False
                    break
            if ok:
                return True
        return False

    def _save_result(self, app_name: str, task_id: str, result: dict) -> None:
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "scorer_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def compute_pass_at_k(successes: list[bool], k: int) -> float:
        import math

        normalized = [bool(x) for x in successes]
        n = len(normalized)
        c = sum(normalized)
        if n < k:
            return float(c > 0)
        if c == 0:
            return 0.0
        log_num = sum(math.log(n - c - i) for i in range(k) if n - c - i > 0)
        log_den = sum(math.log(n - i) for i in range(k))
        return 1.0 - math.exp(log_num - log_den) if log_den > 0 else 1.0

    @staticmethod
    def compute_dsi(model_rate: float, global_avg: float) -> float:
        if global_avg == 0:
            return 0.0
        return round((model_rate - global_avg) / global_avg, 4)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Three-level scorer")
    parser.add_argument("app_name")
    parser.add_argument("task_id")
    parser.add_argument("--vlm-model", default="gpt-4o")
    args = parser.parse_args()

    scorer = Scorer(vlm_model=args.vlm_model)
    out = scorer.score_task(args.app_name, args.task_id, {}, {}, {})
    print(json.dumps(out, ensure_ascii=False, indent=2))
