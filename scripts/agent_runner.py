#!/usr/bin/env python3
"""
Agent_Runner.py - 实验核心执行逻辑 

职责：
  1. 从 data/app_name/task_xxx/meta.json 读取任务 Prompt
  2. 调用 Retriever 筛选最相关的 3-5 个源码文件（RAG 辅助）
  3. 将 Context + Prompt 发给 LLMClient（支持策略 A/B 切换）
  4. 解析模型输出的代码块并写入 workspace/
  5. RQ5 反馈闭环：失败后将截图 + 错误日志打包重新调用模型

向后兼容：保留旧 run(combined_id) 接口，供 benchmark_runner.py 调用。
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- 模块路径处理 ----------
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from llm_api.client import LLMClient
from tools.retriever import Retriever

# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #
_MAX_FEEDBACK_ROUNDS = 2   # RQ5：最多自动修复轮次
<<<<<<< HEAD
_MAX_AGENT_STEPS = 10      # ReAct / Tool-Planning 最大工具交互轮次
_MAX_RESPONSE_RECOVERY_RETRIES = 2  # 对空/过短/无代码块响应的自动恢复重试次数
_MIN_VALID_RESPONSE_CHARS = 200
=======
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)


class AgentRunner:
    """
    实验核心：加载任务、RAG 检索文件、与 LLM 交互、写入 workspace。

    策略 A（比底座）: 固定 strategy，切换 model
    策略 B（比方法）: 固定 model，切换 strategy
    RQ5 反馈闭环  : run_with_feedback_loop()
    """

    def __init__(
        self,
        model:              str   = "tongji/DeepSeek-R1",
        strategy:           str   = "ReAct",
        retriever_strategy: str   = "keyword",
        retriever_top_k:    int   = 5,
        data_dir:           str   = "data",
        workspace_dir:      str   = "workspace",
        results_dir:        str   = "results",
        temperature:        float = 0.2,
    ):
        """
        Args:
            model:              底座模型，可用简写见 llm_api/client.py MODEL_ALIASES。
            strategy:           Agent 策略 'direct' | 'ReAct' | 'tool_planning'。
            retriever_strategy: 文件检索策略 'keyword' | 'tfidf' | 'ast_analysis'。
            retriever_top_k:    检索返回文件数（默认 5）。
            temperature:        LLM 温度，默认 0.2 保证代码确定性。
        """
        self.root_dir      = Path(__file__).parent.parent
        self.data_dir      = self.root_dir / data_dir
        self.workspace_dir = self.root_dir / workspace_dir
        self.results_dir   = self.root_dir / results_dir
        self.model         = model
        self.strategy      = strategy
        self.retriever_strategy = retriever_strategy
        self.retriever_top_k    = retriever_top_k
        self.temperature        = temperature

        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(model=model, strategy=strategy)
        
    def _parse_id(self, combined_id: str) -> tuple:
        """
        解析组合ID为 app_name 和 task_id
        
        Args:
            combined_id: 组合ID，格式为 'app_name/task_id'
            
        Returns:
            (app_name, task_id) 元组
        """
        if '/' in combined_id:
            parts = combined_id.split('/', 1)
            return parts[0], parts[1]
        else:
            # 兼容旧格式（假设是 app_name）
            return combined_id, None
    
    def load_meta(self, combined_id: str) -> Dict:
        """
        加载应用的元数据
        
        Args:
            combined_id: 组合ID，格式为 'app_name/task_id'
            
        Returns:
            元数据字典
        """
        app_name, task_id = self._parse_id(combined_id)
        if task_id:
            meta_path = self.data_dir / app_name / task_id / "meta.json"
        else:
            # 兼容旧格式
            meta_path = self.data_dir / app_name / "meta.json"
            
        if not meta_path.exists():
            raise FileNotFoundError(f"Meta file not found: {meta_path}")
        
        with open(meta_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    # ----------------------------------------------------------------------- #
    #  主入口 (新) — 供 Experiment_Launcher 直接调用
    # ----------------------------------------------------------------------- #

    def run_task(
        self,
        app_name: str,
        task_id:  str,
        feedback_screenshots: Optional[List[str]] = None,
        feedback_log:         Optional[str]        = None,
        attempt:              int                  = 1,
    ) -> Dict:
        """
        执行一次完整的 Agent 步骤（RAG 检索 → LLM 生成 → 写文件）。

        Args:
            app_name / task_id:   任务标识。
            feedback_screenshots: RQ5 反馈截图路径列表。
            feedback_log:         RQ5 Appium / 编译错误日志。
            attempt:              第几次尝试（Pass@k 用）。

        Returns:
            {"success": bool, "files_written": int, "write_results": {...},
             "llm_response": str, "retrieved_files": [...]}
        """
        print(f"\n{'='*70}")
        print(f"[AgentRunner] {app_name}/{task_id}  "
              f"model={self.model}  strategy={self.strategy}  attempt={attempt}")
        print(f"{'='*70}\n")

        meta        = self.load_meta(f"{app_name}/{task_id}")
        task_prompt = meta.get("prompt", "")

        # RAG 检索
        base_src = self.data_dir / app_name / "base_src"
        retriever = Retriever(
            base_src_dir=base_src,
            strategy=self.retriever_strategy,
            top_k=self.retriever_top_k,
        )
        retrieved_paths, context = retriever.retrieve(task_prompt)
        print(f"[AgentRunner] Retrieved {len(retrieved_paths)} files: {retrieved_paths}")

        # workspace 校验
        workspace_path = self.workspace_dir / app_name / task_id
        if not workspace_path.exists():
            raise FileNotFoundError(
                f"Workspace not initialized: {workspace_path}\n"
                "Call Env_Manager.reset_workspace() first."
            )

<<<<<<< HEAD
        retries = 0
        while True:
            if self.strategy == "direct":
                result = self._run_direct_once(
                    app_name=app_name,
                    task_id=task_id,
                    task_prompt=task_prompt,
                    workspace_path=workspace_path,
                    feedback_screenshots=feedback_screenshots,
                    feedback_log=feedback_log,
                    attempt=attempt,
                )
            else:
                result = self._run_tool_agent_loop(
                    app_name=app_name,
                    task_id=task_id,
                    task_prompt=task_prompt,
                    workspace_path=workspace_path,
                    feedback_screenshots=feedback_screenshots,
                    feedback_log=feedback_log,
                    attempt=attempt,
                )

            if result.get("success"):
                result["response_retries"] = retries
                return result

            llm_response = str(result.get("llm_response", "") or "")
            if retries >= _MAX_RESPONSE_RECOVERY_RETRIES:
                result["response_retries"] = retries
                return result
            if not self._should_retry_response_quality(llm_response):
                result["response_retries"] = retries
                return result

            retries += 1
            print(
                f"[AgentRunner] Retry due to low-quality LLM response "
                f"({retries}/{_MAX_RESPONSE_RECOVERY_RETRIES})"
            )

    def _run_direct_once(
        self,
        app_name: str,
        task_id: str,
        task_prompt: str,
        workspace_path: Path,
        feedback_screenshots: Optional[List[str]],
        feedback_log: Optional[str],
        attempt: int,
    ) -> Dict:
        base_src = self.data_dir / app_name / "base_src"
        retriever = Retriever(
            base_src_dir=base_src,
            strategy=self.retriever_strategy,
            top_k=self.retriever_top_k,
        )
        retrieved_paths, context = retriever.retrieve(task_prompt)

        enriched_context = self._build_direct_baseline_context(
            app_name=app_name,
            task_id=task_id,
            workspace_path=workspace_path,
            rag_context=context,
        )

=======
        # LLM 调用
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)
        llm_response = self.llm.generate_code(
            task_prompt=task_prompt,
            context=context,
            feedback_screenshots=feedback_screenshots,
            feedback_log=feedback_log,
            temperature=self.temperature,
        )

        # 解析 & 写文件
        code_blocks  = self._extract_code_blocks(llm_response)
        write_results = self._write_to_workspace(workspace_path, code_blocks)

        success_count = sum(1 for v in write_results.values() if v == "SUCCESS")
        self._save_llm_response(app_name, task_id, attempt, llm_response, retrieved_paths)

        return {
            "success":         success_count > 0,
            "files_written":   success_count,
            "write_results":   write_results,
            "llm_response":    llm_response,
            "retrieved_files": retrieved_paths,
            # 旧字段兼容
            "total_files": len(write_results),
<<<<<<< HEAD
            "tool_calls": 0,
            "plan_valid": True,
        }

    def _run_tool_agent_loop(
        self,
        app_name: str,
        task_id: str,
        task_prompt: str,
        workspace_path: Path,
        feedback_screenshots: Optional[List[str]],
        feedback_log: Optional[str],
        attempt: int,
    ) -> Dict:
        base_src = self.data_dir / app_name / "base_src"
        retriever = Retriever(
            base_src_dir=base_src,
            strategy=self.retriever_strategy,
            top_k=self.retriever_top_k,
        )
        retrieved_paths, context = retriever.retrieve(task_prompt)

        runtime = ToolRuntime(workspace_path)
        planner = PlanValidator()
        plan_loaded = False
        tool_calls = 0
        trace: list[dict] = []

        system_prompt = self._build_tool_system_prompt(self.strategy)
        user_prompt = self._build_tool_user_prompt(
            task_prompt=task_prompt,
            seed_context=context,
            feedback_log=feedback_log,
            strategy=self.strategy,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        final_response = ""
        final_error = "step_limit_exceeded"

        for step_index in range(1, _MAX_AGENT_STEPS + 1):
            model_text = self.llm.chat(
                messages=messages,
                temperature=self.temperature,
                max_tokens=4096,
                top_p=1.0,
            )
            final_response = model_text
            messages.append({"role": "assistant", "content": model_text})

            if self._looks_like_final_patch(model_text):
                if self.strategy == "tool_planning" and plan_loaded:
                    if planner.summary().get("remaining_steps"):
                        obs = {
                            "ok": False,
                            "error": "final_patch_rejected_plan_incomplete",
                            "plan_summary": planner.summary(),
                        }
                        messages.append({"role": "user", "content": self._format_observation(obs)})
                        trace.append({"step": step_index, "kind": "reject_final", "observation": obs})
                        final_error = "plan_not_completed"
                        continue
                final_error = ""
                break

            payload = parse_agent_json(model_text)
            if not payload:
                obs = {
                    "ok": False,
                    "error": "invalid_action_format",
                    "hint": "Reply with JSON action object or final patch with code blocks.",
                }
                messages.append({"role": "user", "content": self._format_observation(obs)})
                trace.append({"step": step_index, "kind": "invalid_format", "observation": obs})
                final_error = "invalid_action_format"
                continue

            action_type = str(payload.get("type", "")).strip().lower()

            if action_type == "plan":
                if self.strategy != "tool_planning":
                    obs = {"ok": False, "error": "plan_not_allowed_for_strategy"}
                else:
                    result = planner.load_plan(payload)
                    plan_loaded = bool(result.get("ok", False))
                    obs = {
                        "ok": bool(result.get("ok", False)),
                        "result": result,
                        "plan_summary": planner.summary() if plan_loaded else None,
                    }
                messages.append({"role": "user", "content": self._format_observation(obs)})
                trace.append({"step": step_index, "kind": "plan", "action": payload, "observation": obs})
                final_error = "plan_invalid" if not obs.get("ok") else final_error
                continue

            if action_type == "tool_call":
                step_id = str(payload.get("step_id", "")).strip()
                if self.strategy == "tool_planning":
                    if not plan_loaded:
                        obs = {"ok": False, "error": "plan_required_before_tool_calls"}
                        messages.append({"role": "user", "content": self._format_observation(obs)})
                        trace.append({"step": step_index, "kind": "tool_reject", "action": payload, "observation": obs})
                        final_error = "plan_missing"
                        continue
                    gate = planner.can_execute(step_id)
                    if not gate.get("ok", False):
                        obs = {"ok": False, "error": gate.get("error")}
                        messages.append({"role": "user", "content": self._format_observation(obs)})
                        trace.append({"step": step_index, "kind": "tool_reject", "action": payload, "observation": obs})
                        final_error = "plan_order_violation"
                        continue

                tool_name = str(payload.get("tool", "")).strip()
                args = payload.get("args") or {}
                try:
                    tool_result = runtime.execute(tool_name, args)
                    tool_calls += 1
                    if self.strategy == "tool_planning" and payload.get("complete_step"):
                        planner.mark_completed(step_id)
                        tool_result["plan_summary"] = planner.summary()
                    obs = {"ok": True, "result": tool_result}
                except ToolExecutionError as exc:
                    obs = {"ok": False, "error": str(exc)}

                messages.append({"role": "user", "content": self._format_observation(obs)})
                trace.append({"step": step_index, "kind": "tool_call", "action": payload, "observation": obs})
                final_error = "tool_call_failed" if not obs.get("ok") else final_error
                continue

            if action_type == "step_complete":
                if self.strategy != "tool_planning" or not plan_loaded:
                    obs = {"ok": False, "error": "step_complete_only_for_tool_planning"}
                else:
                    sid = str(payload.get("step_id", "")).strip()
                    gate = planner.can_execute(sid)
                    if not gate.get("ok", False):
                        obs = {"ok": False, "error": gate.get("error")}
                    else:
                        planner.mark_completed(sid)
                        obs = {"ok": True, "plan_summary": planner.summary()}
                messages.append({"role": "user", "content": self._format_observation(obs)})
                trace.append({"step": step_index, "kind": "step_complete", "action": payload, "observation": obs})
                final_error = "step_complete_invalid" if not obs.get("ok") else final_error
                continue

            obs = {"ok": False, "error": f"unsupported action type: {action_type}"}
            messages.append({"role": "user", "content": self._format_observation(obs)})
            trace.append({"step": step_index, "kind": "unsupported", "action": payload, "observation": obs})
            final_error = "unsupported_action"

        if not self._validate_agent_output(final_response):
            self._save_llm_response(app_name, task_id, attempt, final_response, retrieved_paths)
            return {
                "success": False,
                "files_written": 0,
                "write_results": {},
                "llm_response": final_response,
                "retrieved_files": retrieved_paths,
                "total_files": 0,
                "error": final_error or "invalid_final_patch_or_step_overflow",
                "tool_calls": tool_calls,
                "plan_valid": (self.strategy != "tool_planning") or plan_loaded,
                "agent_trace": trace,
            }

        code_blocks = self._extract_code_blocks(final_response)
        write_results = self._write_to_workspace(workspace_path, code_blocks)
        success_count = sum(1 for v in write_results.values() if v == "SUCCESS")
        self._save_llm_response(app_name, task_id, attempt, final_response, retrieved_paths)

        return {
            "success": success_count > 0,
            "files_written": success_count,
            "write_results": write_results,
            "llm_response": final_response,
            "retrieved_files": retrieved_paths,
            "total_files": len(write_results),
            "tool_calls": tool_calls,
            "plan_valid": (self.strategy != "tool_planning") or plan_loaded,
            "agent_trace": trace,
=======
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)
        }

    def run_with_feedback_loop(
        self,
        app_name:     str,
        task_id:      str,
        get_feedback,           # callable(attempt) -> (screenshots, log, passed)
        max_rounds:   int = _MAX_FEEDBACK_ROUNDS,
    ) -> List[Dict]:
        """
        RQ5 自我修正闭环。每轮失败后把截图 + 日志喂回模型，直到通过或达到上限。
        """
        history: List[Dict] = []
        screenshots, log, passed = None, None, False

        for attempt in range(1, max_rounds + 2):
            result = self.run_task(
                app_name, task_id,
                feedback_screenshots=screenshots,
                feedback_log=log,
                attempt=attempt,
            )
            history.append(result)
            screenshots, log, passed = get_feedback(attempt)
            if passed:
                print(f"[AgentRunner] ✓ Passed after attempt {attempt}")
                break
            if attempt <= max_rounds:
                print(f"[AgentRunner] ✗ Attempt {attempt} failed, retrying with feedback...")

        return history

    # ----------------------------------------------------------------------- #
    #  向后兼容入口 — benchmark_runner.py 仍调用 run(combined_id)
    # ----------------------------------------------------------------------- #

    def run(self, combined_id: str) -> Dict:
        """向后兼容接口，将 'app_name/task_id' 拆分后调用 run_task()。"""
        app_name, task_id = self._parse_id(combined_id)
        if task_id is None:
            raise ValueError(f"combined_id must be 'app_name/task_id', got: {combined_id}")
        result = self.run_task(app_name, task_id)
        # 保持旧返回结构
        return {
            "combined_id": combined_id,
            "app_name":    app_name,
            "task_id":     task_id,
            "success":     result["files_written"],
            "errors":      sum(1 for v in result["write_results"].values() if v.startswith("ERROR")),
            "total_files": result["total_files"],
        }

    # ----------------------------------------------------------------------- #
    #  私有方法
    # ----------------------------------------------------------------------- #

    def _extract_code_blocks(self, llm_response: str) -> List[Tuple[str, str]]:
        """支持 ```filepath:... 和标准 ```kotlin/xml 两种格式。"""
        blocks: List[Tuple[str, str]] = []

        # 格式 1（优先）
        for m in re.finditer(r"```filepath:([^\n]+)\n(.*?)```", llm_response, re.DOTALL):
            path, content = m.group(1).strip(), m.group(2).strip()
            blocks.append((path, content))
            print(f"  [parse] filepath block → {path}")

        if blocks:
            return blocks

        # 格式 2：标准代码块，从前文推断路径
        for idx, m in enumerate(re.finditer(
            r"```(?:kotlin|java|xml|gradle|json)?\n(.*?)```", llm_response, re.DOTALL
        )):
            content   = m.group(1).strip()
            text_prev = llm_response[: m.start()][-300:]
            path_m    = re.search(
                r"(?:File|file|path|Path)[:：]\s*`?([^\s`\n]+)`?", text_prev
            )
            path = path_m.group(1).strip() if path_m else f"generated_{idx}.kt"
            blocks.append((path, content))
            print(f"  [parse] standard block → {path}")

        return blocks

    def _looks_like_final_patch(self, llm_response: str) -> bool:
        if "[FINAL_PATCH]" in llm_response:
            return True
        return bool(self._extract_code_blocks(llm_response))

    def _should_retry_response_quality(self, llm_response: str) -> bool:
        text = (llm_response or "").strip()
        if not text:
            return True
        if len(text) < _MIN_VALID_RESPONSE_CHARS:
            return True
        if not self._extract_code_blocks(text):
            return True
        return False

    def _write_to_workspace(
        self,
        workspace_path: Path,
        code_blocks:    List[Tuple[str, str]],
    ) -> Dict[str, str]:
        results: Dict[str, str] = {}
        for rel_path, content in code_blocks:
            rel_path = rel_path.replace("\\", "/").strip()
            for prefix in ("FoodYou-develop/", "rssreader-main/"):
                if rel_path.startswith(prefix):
                    rel_path = rel_path[len(prefix):]
            target = workspace_path / rel_path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                results[rel_path] = "SUCCESS"
                print(f"  ✓ Written: {rel_path}")
            except Exception as exc:
                results[rel_path] = f"ERROR: {exc}"
                print(f"  ✗ Failed:  {rel_path}  ({exc})")
        return results

    def _save_llm_response(
        self,
        app_name:  str,
        task_id:   str,
        attempt:   int,
        response:  str,
        retrieved: List[str],
    ) -> None:
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_attempt{attempt}" if attempt > 1 else ""
        (out_dir / f"llm_response{suffix}.txt").write_text(response, encoding="utf-8")
        (out_dir / f"agent_meta{suffix}.json").write_text(
            json.dumps({
                "model":           self.model,
                "strategy":        self.strategy,
                "attempt":         attempt,
                "retrieved_files": retrieved,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

<<<<<<< HEAD
    def _validate_agent_output(self, llm_response: str) -> bool:
        """Validate final output by checking presence of at least one writable code block."""
        return bool(self._extract_code_blocks(llm_response))

=======
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent_Runner — 实验核心执行脚本")
    parser.add_argument("app_name",  help="应用名称，如 app_foodyou")
    parser.add_argument("task_id",   help="任务ID，如 task_001_theme")
    parser.add_argument("--model",    default="deepseek-r1",
                        help="底座模型（默认 deepseek-r1，经 Tongji base_url 路由）")
    parser.add_argument("--strategy", default="ReAct",
                        choices=["direct", "ReAct", "tool_planning"],
                        help="Agent 策略（默认 ReAct）")
    parser.add_argument("--retriever", default="keyword",
                        choices=["keyword", "tfidf", "ast_analysis"],
                        help="检索策略（默认 keyword）")
    parser.add_argument("--top-k", type=int, default=5,
                        help="检索文件数量（默认 5）")
    args = parser.parse_args()

    runner = AgentRunner(
        model=args.model,
        strategy=args.strategy,
        retriever_strategy=args.retriever,
        retriever_top_k=args.top_k,
    )
    try:
        result = runner.run_task(args.app_name, args.task_id)
        status = "✓ SUCCESS" if result["success"] else "✗ NO FILES WRITTEN"
        print(f"\n{status}  files_written={result['files_written']}")
        return 0 if result["success"] else 1
    except Exception as exc:
        import traceback
        print(f"✗ Agent failed: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
