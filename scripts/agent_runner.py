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
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ---------- 模块路径处理 ----------
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from llm_api.client import LLMClient
from tools.retriever import Retriever
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #
_MAX_FEEDBACK_ROUNDS = 2   # RQ5：最多自动修复轮次


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
            retriever_top_k:    检索返回文件数（默认 8）。
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
        执行一次完整的 Agent 步骤。根据策略选择流程：
          - direct:       RAG 检索 → LLM 生成（传统方案）
          - ReAct:        工具调用循环（纯动态发现）
          - tool_planning: Planner → Executor（计划执行）

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

        # workspace 校验
        workspace_path = self.workspace_dir / app_name / task_id
        if not workspace_path.exists():
            raise FileNotFoundError(
                f"Workspace not initialized: {workspace_path}\n"
                "Call Env_Manager.reset_workspace() first."
            )

        # 根据策略选择流程
        if self.strategy == "direct":
            # 传统 RAG + LLM 流程
            base_src = self.data_dir / app_name / "base_src"
            retriever = Retriever(
                base_src_dir=base_src,
                strategy=self.retriever_strategy,
                top_k=self.retriever_top_k,
            )
            retrieved_paths, context = retriever.retrieve(task_prompt)
            print(f"[AgentRunner] Retrieved {len(retrieved_paths)} files: {retrieved_paths}")

            # LLM 调用（RAG 上下文）
            llm_response = self.llm.generate_code(
                task_prompt=task_prompt,
                context=context,
                feedback_screenshots=feedback_screenshots,
                feedback_log=feedback_log,
                temperature=self.temperature,
            )
            retrieved_files_for_log = retrieved_paths

        elif self.strategy == "ReAct":
            # 纯工具调用循环：不做前置 RAG
            print("[AgentRunner] Using ReAct strategy (no RAG) — Agent will discover code via tool calls")
            current_activity = meta.get("current_activity", "unknown")
            ui_dom_tree = meta.get("ui_dom_tree", "")
            react_result = self.run_react_agent(
                app_name=app_name,
                task_id=task_id,
                instruction=task_prompt,
                current_activity=current_activity,
                ui_dom_tree=ui_dom_tree,
                attempt=attempt,
                max_steps=None,
            )
            self._save_agent_trace(
                app_name=app_name,
                task_id=task_id,
                attempt=attempt,
                trace_obj=react_result,
                trace_name="agent_trace_react",
            )
            llm_response = json.dumps(react_result, ensure_ascii=False, indent=2)
            retrieved_files_for_log = []

        elif self.strategy == "tool_planning":
            # Planner + Executor：不做前置 RAG
            print("[AgentRunner] Using tool_planning strategy (no RAG) — Planner will generate execution plan")
            current_activity = meta.get("current_activity", "unknown")
            ui_dom_tree = meta.get("ui_dom_tree", "")
            planning_result = self.run_tool_planning(
                app_name=app_name,
                task_id=task_id,
                instruction=task_prompt,
                current_activity=current_activity,
                ui_dom_tree=ui_dom_tree,
            )
            self._save_agent_trace(
                app_name=app_name,
                task_id=task_id,
                attempt=attempt,
                trace_obj=planning_result,
                trace_name="agent_trace_tool_planning",
            )
            llm_response = json.dumps(planning_result, ensure_ascii=False, indent=2)
            retrieved_files_for_log = []

        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

        # 解析 & 写文件（优先补丁格式，兼容整文件格式）
        write_results = self._apply_llm_output(workspace_path, llm_response)

        success_count = sum(1 for v in write_results.values() if v == "SUCCESS")
        self._save_llm_response(app_name, task_id, attempt, llm_response, retrieved_files_for_log)

        return {
            "success":         success_count > 0,
            "files_written":   success_count,
            "write_results":   write_results,
            "llm_response":    llm_response,
            "retrieved_files": retrieved_files_for_log,
            # 旧字段兼容
            "total_files": len(write_results),
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

    # -------------------------------------------------------------------
    # Tools exposed to agents (schema + simple implementations)
    # -------------------------------------------------------------------
    def tool_search_keyword(self, base_src: Path, keyword: str) -> Dict[str, Any]:
        """
        在项目中搜索关键字，返回匹配结果与可直接回注给模型的 Observation 文本。
        """
        results: List[Dict[str, Any]] = []
        try:
            relative_base = self.workspace_dir if base_src.is_relative_to(self.workspace_dir) else self.root_dir
        except AttributeError:
            relative_base = self.workspace_dir if str(base_src).startswith(str(self.workspace_dir)) else self.root_dir
        for root, _, files in os.walk(base_src):
            for fn in files:
                if not fn.endswith(('.kt', '.java', '.xml')):
                    continue
                p = Path(root) / fn
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        for i, line in enumerate(f, start=1):
                            if keyword in line:
                                results.append({
                                    'path': str(p.relative_to(relative_base)),
                                    'line': i,
                                    'line_text': line.rstrip('\n'),
                                })
                except Exception:
                    continue
        total_matches = len(results)
        shown_results = results[:10]
        if total_matches > 10:
            observation = (
                f"Observation: Found {total_matches} matches for '{keyword}'. "
                "Showing the first 10 matches. Please refine your search keyword to be more specific."
            )
        else:
            observation = f"Observation: Found {total_matches} matches for '{keyword}'."

        return {
            "matches": shown_results,
            "total_matches": total_matches,
            "truncated": total_matches > 10,
            "observation": observation,
        }

    def tool_read_file(self, file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        """读取仓库内相对路径文件的内容（支持行切片）。"""
        if file_path.startswith("workspace/"):
            file_path = file_path[len("workspace/"):]
        workspace_target = self.workspace_dir / file_path
        target = workspace_target if workspace_target.exists() else (self.root_dir / file_path)
        if not target.exists():
            raise FileNotFoundError(str(target))
        text = target.read_text(encoding='utf-8')
        if start_line is None and end_line is None:
            return text
        lines = text.splitlines()
        s = max(1, start_line or 1)
        e = min(len(lines), end_line or len(lines))
        return '\n'.join(lines[s-1:e])

    def tool_submit_patch(self, file_path: str, old_snippet: str, new_snippet: str) -> Dict[str, Any]:
        """把补丁写入 workspace（校验 old_snippet 是否存在）。"""
        target = self.workspace_dir / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        full_path = str(target)
        content = target.read_text(encoding='utf-8') if target.exists() else ''
        if old_snippet:
            if old_snippet not in content:
                return {'ok': False, 'error': 'SEARCH_NOT_FOUND', 'path': full_path}
            content = content.replace(old_snippet, new_snippet, 1)
        else:
            # 插入到文件末尾
            content = (content.rstrip() + '\n\n' + new_snippet.rstrip() + '\n')
        target.write_text(content, encoding='utf-8')
        print(f"[AgentRunner] submit_patch wrote: {Path(full_path).resolve()}")
        return {'ok': True, 'path': full_path}

    def _call_llm_with_system(self, system_prompt: str, user_content: str, temperature: float = 0.2, stop: Optional[list[str]] = None) -> str:
        """
        直接调用 LLM，使用自定义 system prompt（用于工具调用模式）。
        返回模型的原始响应。
        """
        try:
            return self.llm.generate_with_system_prompt(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
                max_tokens=8000,
                stop=stop,
            )
        except Exception as exc:
            # 仅输出必要信息，避免泄露 key 或刷屏长上下文
            print(
                f"[AgentRunner] LLM call failed: model={self.llm.model} "
                f"strategy={self.strategy} error={repr(exc)}"
            )
            raise

    def _extract_react_tool_call(self, response_text: str) -> Dict[str, Any]:
        """从 DeepSeek-R1 输出中剥离 think 内容，并提取单个工具调用。"""
        normalized = response_text.strip()
        if "</think>" in normalized:
            normalized = normalized.rsplit("</think>", 1)[-1].strip()
        elif "<think>" in normalized:
            normalized = re.sub(r"<think>.*?</think>", "", normalized, flags=re.DOTALL).strip()

        # JSON-style tool call: {"tool": "search_keyword", "arguments": {"keyword": "Settings"}}
        try:
            json_match = re.search(r"\{.*\}", normalized, re.DOTALL)
            if json_match:
                payload = json.loads(json_match.group(0))
                if isinstance(payload, dict):
                    tool_name = payload.get("tool") or payload.get("action")
                    arguments = payload.get("arguments", {})
                    if not arguments:
                        arguments = payload.get("action_parameters", {})
                else:
                    tool_name = None
                    arguments = {}
                if isinstance(tool_name, str) and "(" in tool_name and ")" in tool_name:
                    normalized_action = tool_name.strip()
                    patterns = {
                        "search_keyword": re.compile(r"search_keyword\(([^)]+)\)"),
                        "read_file": re.compile(r"read_file\(([^,\)]+)(?:,\s*([0-9]+)\s*,?\s*([0-9]+))?\)"),
                        "submit_patch": re.compile(r"submit_patch\((.*)\)\s*$", re.DOTALL),
                    }
                    for name, pattern in patterns.items():
                        match = pattern.search(normalized_action)
                        if not match:
                            continue
                        if name == "search_keyword":
                            return {
                                "ok": True,
                                "tool": name,
                                "keyword": match.group(1).strip().strip('"').strip("'"),
                                "normalized": normalized_action,
                            }
                        if name == "read_file":
                            return {
                                "ok": True,
                                "tool": name,
                                "path": match.group(1).strip().strip('"').strip("'"),
                                "start_line": int(match.group(2)) if match.group(2) else None,
                                "end_line": int(match.group(3)) if match.group(3) else None,
                                "normalized": normalized_action,
                            }
                        payload_inner = match.group(1).strip()
                        parts = [part.strip() for part in re.split(r",\s*", payload_inner, maxsplit=2)]
                        if len(parts) < 3:
                            return {
                                "ok": False,
                                "error": "malformed_submit_patch",
                                "normalized": normalized_action,
                            }
                        return {
                            "ok": True,
                            "tool": name,
                            "file_path": parts[0].strip('"').strip("'"),
                            "old_snip": parts[1].strip('"').strip("'"),
                            "new_snip": parts[2].strip('"').strip("'"),
                            "normalized": normalized_action,
                        }
                if tool_name == "search_keyword" and "keyword" in arguments:
                    return {
                        "ok": True,
                        "tool": tool_name,
                        "keyword": str(arguments["keyword"]).strip(),
                        "normalized": normalized,
                    }
                if tool_name == "read_file" and "file_path" in arguments:
                    return {
                        "ok": True,
                        "tool": tool_name,
                        "path": str(arguments["file_path"]).strip(),
                        "start_line": int(arguments["start_line"]) if arguments.get("start_line") else None,
                        "end_line": int(arguments["end_line"]) if arguments.get("end_line") else None,
                        "normalized": normalized,
                    }
                if tool_name == "submit_patch" and "file_path" in arguments:
                    return {
                        "ok": True,
                        "tool": tool_name,
                        "file_path": str(arguments["file_path"]).strip(),
                        "old_snip": str(arguments.get("old_snippet", "")),
                        "new_snip": str(arguments.get("new_snippet", "")),
                        "normalized": normalized,
                    }
        except Exception:
            pass

        patterns = {
            "search_keyword": re.compile(r"search_keyword\(([^)]+)\)"),
            "read_file": re.compile(r"read_file\(([^,\)]+)(?:,\s*([0-9]+)\s*,?\s*([0-9]+))?\)"),
            "submit_patch": re.compile(r"submit_patch\((.*)\)\s*$", re.DOTALL),
        }

        matches: List[Dict[str, Any]] = []
        for tool_name, pattern in patterns.items():
            for match in pattern.finditer(normalized):
                matches.append({"tool": tool_name, "match": match})

        if len(matches) != 1:
            return {
                "ok": False,
                "error": "multiple_actions" if len(matches) > 1 else "no_action",
                "normalized": normalized,
            }

        tool_name = matches[0]["tool"]
        match = matches[0]["match"]
        if tool_name == "search_keyword":
            return {
                "ok": True,
                "tool": tool_name,
                "keyword": match.group(1).strip().strip('"').strip("'"),
                "normalized": normalized,
            }
        if tool_name == "read_file":
            return {
                "ok": True,
                "tool": tool_name,
                "path": match.group(1).strip().strip('"').strip("'"),
                "start_line": int(match.group(2)) if match.group(2) else None,
                "end_line": int(match.group(3)) if match.group(3) else None,
                "normalized": normalized,
            }

        payload = match.group(1).strip()
        parts = [part.strip() for part in re.split(r",\s*", payload, maxsplit=2)]
        if len(parts) < 3:
            return {
                "ok": False,
                "error": "malformed_submit_patch",
                "normalized": normalized,
            }
        return {
            "ok": True,
            "tool": tool_name,
            "file_path": parts[0].strip('"').strip("'"),
            "old_snip": parts[1].strip('"').strip("'"),
            "new_snip": parts[2].strip('"').strip("'"),
            "normalized": normalized,
        }

    def _save_agent_trace(
        self,
        app_name: str,
        task_id: str,
        attempt: int,
        trace_obj: Dict[str, Any],
        trace_name: str,
    ) -> None:
        """保存 agent 过程轨迹（每一步 Thought/Action/Observation 或计划执行记录）。"""
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_attempt{attempt}" if attempt > 1 else ""
        (out_dir / f"{trace_name}{suffix}.json").write_text(
            json.dumps(trace_obj, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -------------------------------------------------------------------
    # ReAct strategy skeleton
    # -------------------------------------------------------------------
    def run_react_agent(self,
                        app_name: str,
                        task_id: str,
                        instruction: str,
                        current_activity: str,
                        ui_dom_tree: str,
                        attempt: int = 1,
                        max_steps: Optional[int] = None) -> Dict[str, Any]:
        """
        ReAct 模式运行骨架：只在初始 Prompt 中包含 Instruction/Current_Activity/UI_DOM_Tree。
        Agent 必须通过工具调用获取源码，允许自行决定何时结束。
        """
        if max_steps is None:
            env_limit = os.getenv("REACT_MAX_STEPS")
            if env_limit is not None:
                try:
                    env_val = int(env_limit)
                except ValueError:
                    env_val = 0
                max_steps = env_val if env_val > 0 else None
            else:
                max_steps = 50
        base_src = self.workspace_dir / app_name / task_id
        history: List[Dict[str, Any]] = []

        def _normalize_task_path(path: str) -> str:
            cleaned = path.strip().lstrip("/")
            if cleaned.startswith("workspace/"):
                cleaned = cleaned[len("workspace/"):]
            task_prefix = f"{app_name}/{task_id}/"
            if not cleaned.startswith(task_prefix):
                cleaned = task_prefix + cleaned
            return cleaned

        system_prompt = (
            "You are an expert Android developer. You are currently looking at a mobile app UI screen.\n"
            "You ONLY see: the current Activity/Fragment name, the UI DOM tree (XML structure), and the user's Instruction.\n"
            "You do NOT have access to source code files yet. You must use Tools to discover and retrieve them.\n"
            "\n"
            "You must work in cycles: Thought -> Action -> Observation, repeating until you call submit_patch to finish.\n"
            "\n"
            "Available Tools (call them EXACTLY as shown):\n"
            "  1. search_keyword(keyword) — Search the project repo for a keyword (e.g., 'btn_delete'). Returns list of matching files/lines.\n"
            "  2. read_file(file_path, start_line, end_line) — Read a file's content. Use line numbers from search_keyword.\n"
            "  3. submit_patch(file_path, old_snippet, new_snippet) — Apply a patch (old_snippet -> new_snippet) to file_path. This ends your task.\n"
            "\n"
            "Workflow:\n"
            "  Thought: Analyze the UI DOM and Instruction. What file(s) likely need to change?\n"
            "  Action: Call a Tool, e.g., search_keyword(\"Settings\")\n"
            "\n"
            "CRITICAL: Output EXACTLY ONE tool call in plain text. Do NOT output JSON, markdown, or extra text.\n"
            "Examples (valid):\n"
            "  search_keyword(\"Settings\")\n"
            "  read_file(\"app/src/main/res/xml/root_preferences.xml\", 1, 40)\n"
            "  submit_patch(\"app/src/main/res/xml/root_preferences.xml\", \"<old>\", \"<new>\")\n"
            "You are only allowed to output THOUGHT and ACTION. DO NOT output the OBSERVATION yourself. The system will provide the OBSERVATION to you. You MUST wait for the system's response.\n"
            "Once you output an Action, STOP generating. Do NOT write blocks of code outside of submit_patch.\n"
            "\n"
            "Constraints:\n"
            "  - search_keyword output includes line numbers; use them to read_file efficiently.\n"
            "  - old_snippet in submit_patch must EXACTLY match existing code (including whitespace).\n"
            "  - Do NOT output raw code blocks; only use submit_patch to write code.\n"
        )

        # initial message contains only the three permitted variables
        prompt_context = {
            'Instruction': instruction,
            'Current_Activity': current_activity,
            'UI_DOM_Tree': ui_dom_tree,
        }

        # Start conversation history for strict ReAct
        conversation_log = ""
        
        # Prepare log file
        log_dir = self.results_dir / app_name / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_attempt{attempt}" if attempt > 1 else ""
        step_log_file = log_dir / f"react_steps{suffix}.log"
        step_log_file.write_text("=== ReAct Execution Steps ===\n", encoding="utf-8")

        def _log_to_file(msg: str):
            with open(step_log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

        steps = 0
        try:
            while True:
                steps += 1
                if max_steps is not None and steps > max_steps:
                    _log_to_file("=== FINISHED (TIMEOUT) ===")
                    return {'result': 'TIMEOUT', 'history': history}
                
                # Combine initial context and conversation log
                user_input = json.dumps(prompt_context, ensure_ascii=False, indent=2)
                if conversation_log:
                    user_input += "\n\n=== Conversation History ===\n" + conversation_log

                # Ask model what to do next (it should produce an Action)
                resp = self._call_llm_with_system(
                    system_prompt, 
                    user_input, 
                    temperature=self.temperature,
                    stop=["Observation:", "Observation:\n", "\nObservation:", "\nObservation:\n"]
                )

                resp_l = resp.strip()
                history.append({'step': steps, 'agent_response': resp_l})
                _log_to_file(f"\n[Step {steps}] Model Output:\n{resp_l}\n")

                if resp_l.startswith("Observation: System Error:"):
                    conversation_log += f"\n{resp_l}\n"
                    _log_to_file(f"[Step {steps}] API fallback Observation injected into conversation.")
                    continue
                
                # Append model's response to conversation log
                conversation_log += f"\n{resp_l}\n"

                parsed = self._extract_react_tool_call(resp_l)
                if parsed.get("ok"):
                    tool_name = parsed.get("tool")
                    if tool_name == "search_keyword":
                        kw = parsed["keyword"]
                        obs = self.tool_search_keyword(base_src, kw)
                        conversation_log += f"\n{obs['observation']}\n"
                        conversation_log += f"Observation Results: {json.dumps(obs['matches'], ensure_ascii=False, indent=2)}\n"
                        _log_to_file(f"[Step {steps}] Tool: search_keyword('{kw}') -> {obs['observation']}")
                        continue
                    if tool_name == "read_file":
                        path = _normalize_task_path(parsed["path"])
                        s = parsed.get("start_line")
                        e = parsed.get("end_line")
                        try:
                            txt = self.tool_read_file(path, s, e)
                            _log_to_file(f"[Step {steps}] Tool: read_file('{path}') -> SUCCESS (length: {len(str(txt))})")
                        except Exception as exc:
                            txt = f"ERROR: {exc}"
                            _log_to_file(f"[Step {steps}] Tool: read_file('{path}') -> ERROR: {exc}")
                        conversation_log += f"\nObservation: {{'path': '{path}', 'content': ... (length: {len(str(txt))})}}\n"
                        conversation_log += f"--- FILE CONTENT START ---\n{txt}\n--- FILE CONTENT END ---\n"
                        continue
                    if tool_name == "submit_patch":
                        file_path = _normalize_task_path(parsed["file_path"])
                        old_snip = parsed["old_snip"]
                        new_snip = parsed["new_snip"]
                        if not file_path:
                            _log_to_file(f"[Step {steps}] Tool: submit_patch called with empty file_path; treating as done.")
                            return {'result': 'DONE_NO_PATCH', 'history': history}
                        _log_to_file(f"[Step {steps}] Tool: submit_patch to '{file_path}'")
                        try:
                            res = self.tool_submit_patch(file_path, old_snip, new_snip)
                            _log_to_file(f"[Step {steps}] Result: {res}")
                            return {'result': res, 'history': history}
                        except Exception as exc:
                            err_msg = f"ERROR executing submit_patch: {exc}"
                            _log_to_file(f"[Step {steps}] Observation: {err_msg}")
                            conversation_log += f"\nObservation: {err_msg}\n"
                            continue

                # If model didn't call a known tool, inform it
                if parsed.get("error") == "multiple_actions":
                    err_msg = "Observation: Format Error, multiple Actions detected. Output exactly one tool call after </think>."
                else:
                    err_msg = "Observation: Format Error, please use the exact schema for one of the available tools."
                _log_to_file(f"[Step {steps}] Notice: Model didn't call any valid tool.")
                conversation_log += f"\n{err_msg}\n"

            return {'result': 'TIMEOUT', 'history': history}

        except Exception as exc:
            err_msg = f"CRITICAL RUNTIME ERROR: {exc}"
            _log_to_file(f"[Exception] {err_msg}")
            history.append({'step': steps, 'agent_response': err_msg})
            return {'result': err_msg, 'history': history}

    # -------------------------------------------------------------------
    # Tool-Planning strategy skeleton (Planner + Executor)
    # -------------------------------------------------------------------
    @dataclass
    class PlannerStep:
        step: int
        action: str

    def run_tool_planning(self,
                          app_name: str,
                          task_id: str,
                          instruction: str,
                          current_activity: str,
                          ui_dom_tree: str) -> Dict[str, Any]:
        """
        Planner produces a JSON array plan (<=5 steps). Executor runs steps in order.
        """
        planner_system_prompt = (
            "You are an Android architect. Your task is NOT to write code, but to plan how to find and modify the necessary source code.\n"
            "\n"
            "You ONLY see: the current Activity/Fragment name, the UI DOM tree (XML structure), and the user's Instruction.\n"
            "\n"
            "Output a JSON array of <=5 exploration steps. Each step must specify which Tool to call and what to search/read.\n"
            "\n"
            "Example output format:\n"
            "[\n"
            "  {\"step\": 1, \"action\": \"search_keyword btn_delete\"},\n"
            "  {\"step\": 2, \"action\": \"read_file FoodYou-develop/app/src/.../NotificationSettings.kt 1 50\"},\n"
            "  {\"step\": 3, \"action\": \"search_keyword MutablePreferences\"},\n"
            "  {\"step\": 4, \"action\": \"read_file FoodYou-develop/app/src/.../NotificationPreferences.kt\"},\n"
            "  {\"step\": 5, \"action\": \"submit_patch FoodYou-develop/app/src/.../NotificationPreferences.kt old_code new_code\"}\n"
            "]\n"
            "\n"
            "Available Tools:\n"
            "  - search_keyword <keyword> — Find files containing a keyword.\n"
            "  - read_file <file_path> [start_line] [end_line] — Read file content.\n"
            "  - submit_patch <file_path> <old_snippet> <new_snippet> — Apply and submit a code patch.\n"
            "\n"
            "Do NOT output code blocks or natural language explanations. Output ONLY the JSON array."
        )

        # Phase A: Planner
        planner_input = json.dumps({
            'Instruction': instruction,
            'Current_Activity': current_activity,
            'UI_DOM_Tree': ui_dom_tree,
        }, ensure_ascii=False, indent=2)
        planner_resp = self._call_llm_with_system(planner_system_prompt, planner_input, temperature=self.temperature)

        # Parse planner output (expect JSON array)
        try:
            plan = json.loads(planner_resp)
        except Exception:
            # try to extract JSON substring
            jmatch = re.search(r"\[.*\]", planner_resp, re.DOTALL)
            plan = json.loads(jmatch.group(0)) if jmatch else []

        # Validate into PlannerStep list
        steps: List[AgentRunner.PlannerStep] = []
        for item in plan[:5]:
            steps.append(self.PlannerStep(step=item.get('step'), action=item.get('action')))

        # Phase B: Executor
        exec_history = []
        base_src = self.data_dir / app_name / 'base_src'
        for ps in steps:
            action = ps.action or ''
            exec_history.append({'step': ps.step, 'action': action})
            # support actions like: search_keyword btn_delete
            m = re.match(r"search_keyword\s+(\S+)", action)
            if m:
                kw = m.group(1).strip()
                obs = self.tool_search_keyword(base_src, kw)
                exec_history[-1]['observation'] = obs['observation']
                exec_history[-1]['matches'] = obs['matches']
                continue
            m = re.match(r"read_file\s+(\S+)(?:\s+(\d+)\s*(\d+)?)?", action)
            if m:
                path = m.group(1)
                s = int(m.group(2)) if m.group(2) else None
                e = int(m.group(3)) if m.group(3) else None
                try:
                    txt = self.tool_read_file(path, s, e)
                except Exception as exc:
                    txt = f"ERROR: {exc}"
                exec_history[-1]['observation'] = {'path': path, 'content': txt}
                continue
            # unsupported action -> mark
            exec_history[-1]['observation'] = {'error': 'unsupported_action'}

        return {'plan': plan, 'execution': exec_history}

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

    def _apply_llm_output(self, workspace_path: Path, llm_response: str) -> Dict[str, str]:
        """优先解析补丁格式；若无补丁则回退到整文件覆盖写入。"""
        patch_ops = self._extract_patch_operations(llm_response)
        if patch_ops:
            print(f"[AgentRunner] Parsed {len(patch_ops)} patch operations")
            return self._apply_patch_operations(workspace_path, patch_ops)

        code_blocks = self._extract_code_blocks(llm_response)
        print(f"[AgentRunner] Fallback to full-file blocks: {len(code_blocks)}")
        return self._write_to_workspace(workspace_path, code_blocks)

    def _extract_patch_operations(self, llm_response: str) -> List[Tuple[str, str, str]]:
        """
        解析补丁格式：
        ```patchfile:relative/path/to/File.ext
        <<<<<<< SEARCH
        old text
        =======
        new text
        >>>>>>> REPLACE
        ```
        """
        ops: List[Tuple[str, str, str]] = []
        file_block_re = re.compile(r"```patchfile:([^\n]+)\n(.*?)```", re.DOTALL)
        op_re = re.compile(
            r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
            re.DOTALL,
        )
        new_file_re = re.compile(
            r"<<<<<<< NEW\n(.*?)\n>>>>>>> NEW_END",
            re.DOTALL,
        )

        for file_block in file_block_re.finditer(llm_response):
            rel_path = file_block.group(1).strip()
            body = file_block.group(2)
            if rel_path.endswith(":NEW"):
                new_body = new_file_re.search(body)
                if new_body:
                    ops.append((rel_path[:-4], "", new_body.group(1)))
                continue
            for m in op_re.finditer(body):
                search = m.group(1)
                replace = m.group(2)
                ops.append((rel_path, search, replace))

        return ops

    def _extract_code_blocks(self, llm_response: str) -> List[Tuple[str, str]]:
        """兼容旧版整文件输出：支持 ```filepath:... 和标准代码块。"""
        blocks: List[Tuple[str, str]] = []

        for m in re.finditer(r"```filepath:([^\n]+)\n(.*?)```", llm_response, re.DOTALL):
            path, content = m.group(1).strip(), m.group(2).strip()
            blocks.append((path, content))
            print(f"  [parse] filepath block → {path}")

        if blocks:
            return blocks

        for idx, m in enumerate(re.finditer(
            r"```(?:kotlin|java|xml|gradle|json)?\n(.*?)```", llm_response, re.DOTALL
        )):
            content = m.group(1).strip()
            text_prev = llm_response[: m.start()][-300:]
            path_m = re.search(
                r"(?:File|file|path|Path)[:：]\s*`?([^\s`\n]+)`?", text_prev
            )
            path = path_m.group(1).strip() if path_m else f"generated_{idx}.kt"
            blocks.append((path, content))
            print(f"  [parse] standard block → {path}")

        return blocks

    def _apply_patch_operations(
        self,
        workspace_path: Path,
        patch_ops: List[Tuple[str, str, str]],
    ) -> Dict[str, str]:
        """将 SEARCH/REPLACE 补丁应用到 workspace 原始文件。"""
        results: Dict[str, str] = {}
        ops_by_file: dict[str, list[Tuple[str, str]]] = defaultdict(list)
        for rel_path, search, replace in patch_ops:
            rel_path = self._normalize_rel_path(rel_path)
            ops_by_file[rel_path].append((search, replace))

        for rel_path, ops in ops_by_file.items():
            target = self._resolve_target_path(
                workspace_path,
                rel_path,
                must_exist=any(bool(s.strip()) for s, _ in ops),
            )
            try:
                if target.exists():
                    content = target.read_text(encoding="utf-8")
                else:
                    content = ""

                for idx, (search, replace) in enumerate(ops, start=1):
                    search = search.strip("\n")
                    replace = replace.strip("\n")

                    if not search.strip():
                        inserted = self._apply_empty_search_insertion(content, replace)
                        if inserted is None:
                            raise ValueError(
                                f"Patch hunk {idx} has empty SEARCH and no safe insertion point"
                            )
                        content = inserted
                        continue

                    if search in content:
                        content = content.replace(search, replace, 1)
                        continue

                    # 兼容模型把换行符统一成 \n 的情况。
                    normalized = content.replace("\r\n", "\n")
                    if search in normalized:
                        normalized = normalized.replace(search, replace, 1)
                        content = normalized
                        continue

                    fuzzy = self._apply_fuzzy_patch(content, search, replace)
                    if fuzzy is not None:
                        content = fuzzy
                        continue

                    raise ValueError(
                        f"Patch hunk {idx} not found in target content"
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                results[rel_path] = "SUCCESS"
                print(f"  ✓ Patched: {rel_path}")
                print(f"    ↳ abs: {target.resolve()}")
            except Exception as exc:
                results[rel_path] = f"ERROR: {exc}"
                print(f"  ✗ Patch failed: {rel_path} ({exc})")

        return results

    def _apply_empty_search_insertion(self, content: str, replace: str) -> Optional[str]:
        """对空 SEARCH 的补丁尝试安全插入：优先类内末尾，其次文件末尾。"""
        if not replace.strip():
            return content

        # 如果是空文件，直接写入。
        if not content.strip():
            return replace + ("\n" if not replace.endswith("\n") else "")

        # 常见 Kotlin/Java 文件：尝试插入到文件末尾前一个闭合大括号之前。
        if "\n}" in content:
            idx = content.rfind("\n}")
            if idx != -1:
                insertion = replace.rstrip() + "\n"
                return content[:idx] + "\n" + insertion + content[idx:]

        # 兜底：文件末尾追加一个空行再插入。
        return content.rstrip() + "\n\n" + replace.rstrip() + "\n"

    def _apply_fuzzy_patch(self, content: str, search: str, replace: str) -> Optional[str]:
        """对 SEARCH 轻微漂移的补丁做模糊匹配：按空白折叠并寻找唯一候选。"""
        if not search.strip():
            return None

        normalized_content = content.replace("\r\n", "\n")
        normalized_search = search.replace("\r\n", "\n")

        # 1) 折叠空白后尝试定位。
        compact_content = re.sub(r"\s+", " ", normalized_content)
        compact_search = re.sub(r"\s+", " ", normalized_search)
        pos = compact_content.find(compact_search)
        if pos == -1:
            return None

        # 通过折叠前后位置映射回原文较复杂，这里只在唯一命中时做保守替换。
        if compact_content.count(compact_search) != 1:
            return None

        # 2) 直接使用原始文本中去掉多余空白后的唯一候选片段替换。
        candidate = self._find_unique_whitespace_insensitive_match(normalized_content, normalized_search)
        if candidate is None:
            return None
        return normalized_content.replace(candidate, replace, 1)

    def _find_unique_whitespace_insensitive_match(self, content: str, search: str) -> Optional[str]:
        """返回 content 中与 search 在去空白后完全一致的唯一片段。"""
        compact_search = re.sub(r"\s+", "", search)
        if not compact_search:
            return None

        compact_content = re.sub(r"\s+", "", content)
        idx = compact_content.find(compact_search)
        if idx == -1:
            return None
        if compact_content.count(compact_search) != 1:
            return None

        # 无法稳定反推精确边界时放弃，避免误替换。
        return None

    def _normalize_rel_path(self, rel_path: str) -> str:
        rel_path = rel_path.replace("\\", "/").strip()
        for prefix in ("FoodYou-develop/", "rssreader-main/"):
            if rel_path.startswith(prefix):
                rel_path = rel_path[len(prefix):]
        return rel_path

    def _resolve_target_path(
        self,
        workspace_path: Path,
        rel_path: str,
        must_exist: bool,
    ) -> Path:
        """兼容 workspace 嵌套一层项目目录（如 FoodYou-develop/）。"""
        task_prefix = f"{workspace_path.parent.name}/{workspace_path.name}/"
        if rel_path.startswith(task_prefix):
            rel_path = rel_path[len(task_prefix):]
        elif rel_path.startswith(f"{workspace_path.name}/"):
            rel_path = rel_path[len(workspace_path.name) + 1:]
        rel_path = self._normalize_rel_path(rel_path)
        candidates = [workspace_path / rel_path]

        nested_roots = [p for p in workspace_path.iterdir() if p.is_dir()]
        candidates.extend(root / rel_path for root in nested_roots)

        for c in candidates:
            if c.exists():
                return c

        if must_exist:
            raise FileNotFoundError(
                f"Target file not found for patch: {rel_path}. Tried: "
                + ", ".join(str(p) for p in candidates)
            )

        # 新文件优先写入嵌套项目根，避免落到 task 根目录。
        if len(nested_roots) == 1:
            return nested_roots[0] / rel_path
        return workspace_path / rel_path

    def _write_to_workspace(
        self,
        workspace_path: Path,
        code_blocks:    List[Tuple[str, str]],
    ) -> Dict[str, str]:
        results: Dict[str, str] = {}
        for rel_path, content in code_blocks:
            rel_path = self._normalize_rel_path(rel_path)
            target = self._resolve_target_path(workspace_path, rel_path, must_exist=False)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                results[rel_path] = "SUCCESS"
                print(f"  ✓ Written: {rel_path}")
                print(f"    ↳ abs: {target.resolve()}")
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
    parser.add_argument("--top-k", type=int, default=8,
                        help="检索文件数量（默认 8）")
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
