#!/usr/bin/env python3
"""
Agent_Runner.py - 实验核心执行逻辑 

职责：
  1. 从 data/app_name/task_xxx/meta.json 读取任务 Prompt
  2. 调用 Retriever 筛选最相关的 3-5 个源码文件（RAG 辅助）
  3. 将 Context + Prompt 发给 LLMClient（支持策略 A/B 切换）
  4. 解析模型输出的代码块并写入 workspace/
  5. RQ5 反馈闭环：失败后将截图 + 错误日志打包重新调用模型

向后兼容：保留 run(combined_id) 便于外部脚本按 "app/task" 调用。
"""

import json
import os
import re
import sys
import ast
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ---------- 模块路径处理 ----------
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from llm_client import LLMClient
from retriever import Retriever
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
        retriever_top_k:    int   = 5,
        data_dir:           str   = "data",
        workspace_dir:      str   = "workspace",
        results_dir:        str   = "results",
        temperature:        float = 0.2,
    ):
        """
        Args:
            model:              底座模型，可用简写见 llm_client.py MODEL_ALIASES。
            strategy:           Agent 策略 'direct' | 'ReAct' | 'tool_planning'。
            retriever_top_k:    检索返回文件数（默认 8）。
            temperature:        LLM 温度，默认 0.2 保证代码确定性。
        """
        self.root_dir      = Path(__file__).parent.parent
        self.data_dir      = self.root_dir / data_dir
        self.workspace_dir = self.root_dir / workspace_dir
        self.results_dir   = self.root_dir / results_dir
        self.model         = model
        self.strategy      = strategy
        self.retriever_top_k    = retriever_top_k
        self.temperature        = temperature

        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(model=model, strategy=strategy)
        self._active_workspace_path: Optional[Path] = None
        
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
            react_result = self.run_react_agent(
                app_name=app_name,
                task_id=task_id,
                instruction=task_prompt,
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
            write_results = self._write_results_from_tool_result(
                react_result.get("result"),
                app_name,
                task_id,
            )
            self._save_llm_response(app_name, task_id, attempt, llm_response, retrieved_files_for_log)
            success_count = sum(1 for v in write_results.values() if v == "SUCCESS")
            return {
                "success":         success_count > 0,
                "files_written":   success_count,
                "write_results":   write_results,
                "llm_response":    llm_response,
                "retrieved_files": retrieved_files_for_log,
                "total_files":     len(write_results),
            }

        elif self.strategy == "tool_planning":
            # 动态 Planner + Tools：不做前置 RAG
            print("[AgentRunner] Using tool_planning strategy (no RAG) — Planner will choose one tool at a time")
            planning_result = self.run_tool_planning(
                app_name=app_name,
                task_id=task_id,
                instruction=task_prompt,
                attempt=attempt,
            )
            self._save_agent_trace(
                app_name=app_name,
                task_id=task_id,
                attempt=attempt,
                trace_obj=planning_result,
                trace_name="agent_trace_tool_planning",
            )
            if "result" in planning_result:
                llm_response = json.dumps(planning_result, ensure_ascii=False, indent=2)
                retrieved_files_for_log = []
                write_results = self._write_results_from_tool_result(
                    planning_result.get("result"),
                    app_name,
                    task_id,
                )
                self._save_llm_response(app_name, task_id, attempt, llm_response, retrieved_files_for_log)
                success_count = sum(1 for v in write_results.values() if v == "SUCCESS")
                return {
                    "success":         success_count > 0,
                    "files_written":   success_count,
                    "write_results":   write_results,
                    "llm_response":    llm_response,
                    "retrieved_files": retrieved_files_for_log,
                    "total_files":     len(write_results),
                }
            llm_response = planning_result.get("patch_response") or json.dumps(
                planning_result, ensure_ascii=False, indent=2
            )
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
        return history

    # -------------------------------------------------------------------
    # Tools exposed to agents (schema + simple implementations)
    # -------------------------------------------------------------------
    def tool_search_keyword(self, base_src: Path, keyword: str) -> Dict[str, Any]:
        """
        在项目中搜索关键字，返回匹配结果与可直接回注给模型的 Observation 文本。
        """
        results: List[Dict[str, Any]] = []
        matched_files: set[str] = set()
        keyword_folded = keyword.casefold()
        base_src = base_src.resolve()
        try:
            workspace_root = self.workspace_dir.resolve()
            root_dir = self.root_dir.resolve()
            relative_base = workspace_root if base_src.is_relative_to(workspace_root) else root_dir
        except AttributeError:
            workspace_root = self.workspace_dir.resolve()
            root_dir = self.root_dir.resolve()
            relative_base = workspace_root if str(base_src).startswith(str(workspace_root)) else root_dir

        def _search(case_sensitive: bool) -> None:
            seen = {(item["path"], item["line"], item["line_text"]) for item in results}
            for root, dirs, files in os.walk(base_src):
                dirs[:] = [d for d in dirs if d not in {"build", ".gradle"}]
                for fn in files:
                    if not fn.endswith(('.kt', '.java', '.xml')):
                        continue
                    p = (Path(root) / fn).resolve()
                    rel_path = str(p.relative_to(relative_base))
                    haystack_path = rel_path if case_sensitive else rel_path.casefold()
                    needle = keyword if case_sensitive else keyword_folded
                    if needle and needle in haystack_path:
                        item = {
                            'path': rel_path,
                            'line': 0,
                            'line_text': '<path match>',
                        }
                        key = (item["path"], item["line"], item["line_text"])
                        if key not in seen:
                            results.append(item)
                            seen.add(key)
                        matched_files.add(rel_path)
                    try:
                        with open(p, 'r', encoding='utf-8') as f:
                            for i, line in enumerate(f, start=1):
                                haystack_line = line if case_sensitive else line.casefold()
                                if needle and needle in haystack_line:
                                    item = {
                                        'path': rel_path,
                                        'line': i,
                                        'line_text': line.rstrip('\n'),
                                    }
                                    key = (item["path"], item["line"], item["line_text"])
                                    if key not in seen:
                                        results.append(item)
                                        seen.add(key)
                                    matched_files.add(rel_path)
                    except Exception:
                        continue

        _search(case_sensitive=True)
        used_case_insensitive_fallback = False
        if not results:
            _search(case_sensitive=False)
            used_case_insensitive_fallback = bool(results)

        total_matches = len(results)
        matched_files_list = sorted(matched_files)
        total_files = len(matched_files_list)
        too_many_matches = total_matches > 50
        shown_results = [] if too_many_matches else results[:10]
        if too_many_matches:
            observation = (
                f"Observation: Found {total_matches} matches for '{keyword}' across {total_files} files. "
                "Returning the full matched file list instead of line snippets."
            )
        elif total_matches > 10:
            observation = (
                f"Observation: Found {total_matches} matches for '{keyword}'. "
                "Showing the first 10 matches. Please refine your search keyword to be more specific."
            )
        else:
            observation = f"Observation: Found {total_matches} matches for '{keyword}'."
        if used_case_insensitive_fallback:
            observation += " Used case-insensitive fallback."

        return {
            "matches": shown_results,
            "files": matched_files_list,
            "total_matches": total_matches,
            "total_files": total_files,
            "truncated": total_matches > 10 and not too_many_matches,
            "files_only": too_many_matches,
            "observation": observation,
        }

    def tool_list_dir(self, dir_path: str) -> Dict[str, Any]:
        """列出 workspace 内目录内容（仅一层）。"""
        target = self._resolve_workspace_file_path(dir_path, must_exist=True)
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(str(target))
        entries = []
        # 定义要过滤的噪音文件夹
        ignore_dirs = {".git", ".idea", "build", ".gradle", "bin"} 
        for child in sorted(target.iterdir()):
            if child.name in ignore_dirs:
                continue # 跳过无用文件夹
            name = child.name + ("/" if child.is_dir() else "")
            entries.append(name)
        return {
            "path": str(target.resolve()),
            "entries": entries,
        }
    def tool_read_file(self, file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        """读取 workspace 内文件内容（支持任务路径、项目根路径、嵌套项目路径）。"""
        target = self._resolve_workspace_file_path(file_path, must_exist=True)
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
        """把补丁写入 workspace 的真实项目目录（校验 old_snippet 是否存在）。"""
        target = self._resolve_workspace_file_path(file_path, must_exist=bool(old_snippet.strip()))
        target.parent.mkdir(parents=True, exist_ok=True)
        full_path = str(target)
        content = target.read_text(encoding='utf-8') if target.exists() else ''
        if old_snippet:
            if old_snippet not in content:
                return {'ok': False, 'error': 'SEARCH_NOT_FOUND', 'path': full_path}
            content = content.replace(old_snippet, new_snippet, 1)
        else:
            if target.exists() and content.strip():
                return {
                    'ok': False,
                    'error': 'EMPTY_OLD_SNIPPET_ON_EXISTING_FILE',
                    'path': full_path,
                    'message': 'old_snippet must exactly match existing content when modifying an existing non-empty file',
                }
            content = new_snippet.rstrip() + '\n'
        target.write_text(content, encoding='utf-8')
        print(f"[AgentRunner] submit_patch wrote: {Path(full_path).resolve()}")
        return {'ok': True, 'path': full_path, 'relative_path': self._workspace_relative_path(target)}

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
        """从模型输出中提取最早的一个完整工具调用，丢弃同轮多余内容。"""
        normalized = response_text.strip()
        normalized = re.sub(r"</?think>", "\n", normalized).strip()
        json_action = self._extract_json_action(normalized)
        if json_action:
            normalized = json_action

        tool_re = re.compile(r"\b(search_keyword|list_dir|read_file|submit_patch)\s*\(")
        calls: list[dict[str, Any]] = []
        for match in tool_re.finditer(normalized):
            open_idx = normalized.find("(", match.start())
            end_idx = self._find_matching_paren(normalized, open_idx)
            if end_idx is None:
                continue
            calls.append({
                "tool": match.group(1),
                "start": match.start(),
                "end": end_idx + 1,
                "call": normalized[match.start():end_idx + 1],
                "payload": normalized[open_idx + 1:end_idx],
            })

        if not calls:
            return {
                "ok": False,
                "error": "no_action",
                "final_without_tool": self._looks_like_final_without_tool(normalized),
                "normalized": normalized,
            }

        calls.sort(key=lambda item: item["start"])
        selected = calls[0]
        tool_name = selected["tool"]
        payload = selected["payload"].strip()
        discarded = len(calls) - 1

        try:
            args = ast.literal_eval(f"({payload},)")
        except Exception:
            return {
                "ok": False,
                "error": f"malformed_{tool_name}",
                "normalized": normalized,
                "selected_call": selected["call"],
                "discarded_actions": discarded,
            }
        if not isinstance(args, tuple):
            args = (args,)

        base = {
            "ok": True,
            "tool": tool_name,
            "normalized": normalized,
            "selected_call": selected["call"],
            "discarded_actions": discarded,
        }
        if tool_name == "search_keyword" and len(args) >= 1:
            return {**base, "keyword": str(args[0])}
        if tool_name == "list_dir" and len(args) >= 1:
            return {**base, "path": str(args[0])}
        if tool_name == "read_file" and len(args) >= 1:
            return {
                **base,
                "path": str(args[0]),
                "start_line": int(args[1]) if len(args) >= 2 and args[1] is not None else None,
                "end_line": int(args[2]) if len(args) >= 3 and args[2] is not None else None,
            }
        if tool_name == "submit_patch" and len(args) >= 3:
            return {
                **base,
                "file_path": str(args[0]),
                "old_snip": str(args[1]),
                "new_snip": str(args[2]),
            }
        return {
            "ok": False,
            "error": f"malformed_{tool_name}",
            "normalized": normalized,
            "selected_call": selected["call"],
            "discarded_actions": discarded,
        }

    def _find_matching_paren(self, text: str, open_idx: int) -> Optional[int]:
        """找到工具调用的右括号，跳过字符串里的括号。"""
        if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "(":
            return None
        depth = 0
        quote: Optional[str] = None
        triple = False
        escape = False
        i = open_idx
        while i < len(text):
            ch = text[i]
            if quote:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif triple and text.startswith(quote * 3, i):
                    quote = None
                    triple = False
                    i += 2
                elif not triple and ch == quote:
                    quote = None
            else:
                if text.startswith('"""', i) or text.startswith("'''", i):
                    quote = text[i]
                    triple = True
                    i += 2
                elif ch in {'"', "'"}:
                    quote = ch
                    triple = False
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return None

    def _looks_like_final_without_tool(self, text: str) -> bool:
        """识别模型声称完成但没有合法工具调用的回复，用于快速终止循环。"""
        lower = text.casefold()
        phrases = (
            "no further changes needed",
            "task is complete",
            "finish the task",
            "ready to compile",
            "solution should now be complete",
            "all changes are complete",
            "无需进一步",
            "任务完成",
        )
        return any(phrase in lower for phrase in phrases)

    def _extract_json_action(self, text: str) -> Optional[str]:
        """兼容模型输出 {"action": "read_file(...)"} 的工具调用格式。"""
        stripped = text.strip()
        if not stripped.startswith("{"):
            return None
        try:
            payload = json.loads(stripped)
        except Exception:
            return None
        action = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(action, str):
            return None
        return action.strip()

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
                        attempt: int = 1,
                        max_steps: Optional[int] = None) -> Dict[str, Any]:
        """
        ReAct 模式运行骨架：只在初始 Prompt 中包含 Instruction。
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
        previous_active_workspace = self._active_workspace_path
        self._active_workspace_path = base_src
        history: List[Dict[str, Any]] = []

        def _normalize_task_path(path: str) -> str:
            cleaned = path.strip().lstrip("/")
            if cleaned.startswith("workspace/"):
                cleaned = cleaned[len("workspace/"):]
            task_prefix = f"{app_name}/{task_id}/"
            if not cleaned.startswith(task_prefix):
                cleaned = task_prefix + cleaned
            return cleaned

        def _action_key(parsed: Dict[str, Any]) -> str:
            tool_name = parsed.get("tool")
            if tool_name == "search_keyword":
                return f"search_keyword:{parsed.get('keyword', '').strip().lower()}"
            if tool_name == "list_dir":
                return f"list_dir:{parsed.get('path', '').strip()}"
            if tool_name == "read_file":
                return (
                    f"read_file:{_normalize_task_path(parsed.get('path', ''))}:"
                    f"{parsed.get('start_line')}:{parsed.get('end_line')}"
                )
            if tool_name == "submit_patch":
                return f"submit_patch:{_normalize_task_path(parsed.get('file_path', ''))}:{hash(parsed.get('old_snip', ''))}"
            return str(parsed.get("selected_call") or parsed.get("normalized", "")).strip().lower()

        system_prompt = (
            "You are an expert Android developer. You are currently looking at a mobile app UI screen.\n"
            "You ONLY see: the user's Instruction.\n"
            "You do NOT have access to source code files yet. You must use Tools to discover and retrieve them.\n"
            "\n"
            "You must work in cycles: Thought -> Action -> Observation, repeating until you call submit_patch to finish.\n"
            "\n"
            "Available Tools (call them EXACTLY as shown):\n"
            "  1. search_keyword(keyword) — Search the project repo for a keyword (e.g., 'btn_delete'). Returns list of matching files/lines.\n"
            "  2. list_dir(path) — List directory entries (one level) to explore project structure.\n"
            "  3. read_file(file_path, start_line, end_line) — Read a file's content. Use line numbers from search_keyword.\n"
            "  4. submit_patch(file_path, old_snippet, new_snippet) — Apply a patch (old_snippet -> new_snippet) to file_path. This ends your task.\n"
            "\n"
            "Workflow:\n"
            "  Thought: Analyze the UI DOM and Instruction. What file(s) likely need to change?\n"
            "  Action: Call a Tool, e.g., search_keyword(\"Settings\")\n"
            "\n"
            "CRITICAL: Output EXACTLY ONE tool call in plain text. Do NOT output JSON, markdown, or extra text.\n"
            "Examples (valid):\n"
            "  search_keyword(\"Settings\")\n"
            "  list_dir(\"app/src/main/res\")\n"
            "  read_file(\"app/src/main/res/xml/root_preferences.xml\", 1, 40)\n"
            "  submit_patch(\"app/src/main/res/xml/root_preferences.xml\", \"<old>\", \"<new>\")\n"
            "You are only allowed to output THOUGHT and ACTION. DO NOT output the OBSERVATION yourself. The system will provide the OBSERVATION to you. You MUST wait for the system's response.\n"
            "Once you output an Action, STOP generating. Do NOT write blocks of code outside of submit_patch.\n"
            "\n"
            "Constraints:\n"
            "  - search_keyword output includes line numbers; use them to read_file efficiently.\n"
            "  - Before creating a new settings file, search and read the existing settings screen implementation.\n"
            "  - Do not assume Android XML PreferenceScreen architecture; follow the app's actual UI framework.\n"
            "  - old_snippet in submit_patch must EXACTLY match existing code (including whitespace).\n"
            "  - old_snippet may be empty only for a brand-new file that does not already exist.\n"
            "  - Do NOT output raw code blocks; only use submit_patch to write code.\n"
        )

        # initial message contains only the permitted variables
        prompt_context = {
            'Instruction': instruction,
        }

        # Start conversation history for strict ReAct
        conversation_log = ""
        read_files: set[str] = set()
        executed_actions: set[str] = set()
        max_empty_retries = 2
        
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
        invalid_steps = 0
        duplicate_steps = 0
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
                resp = ""
                for attempt_idx in range(max_empty_retries + 1):
                    resp = self._call_llm_with_system(
                        system_prompt,
                        user_input,
                        temperature=self.temperature,
                        stop=["Observation:", "Observation:\n", "\nObservation:", "\nObservation:\n"],
                    )
                    if resp and not resp.strip().startswith("Observation: System Error: API returned empty content"):
                        break
                    _log_to_file(
                        f"[Step {steps}] Empty response retry {attempt_idx + 1}/{max_empty_retries + 1}"
                    )

                resp_l = resp.strip()
                history.append({'step': steps, 'agent_response': resp_l})
                _log_to_file(f"\n[Step {steps}] Model Output:\n{resp_l}\n")

                if resp_l.startswith("Observation: System Error: API returned empty content"):
                    conversation_log += f"\n{resp_l}\n"
                    _log_to_file(f"[Step {steps}] API empty response persisted; retry next step.")
                    continue
                if resp_l.startswith("Observation: System Error:"):
                    conversation_log += f"\n{resp_l}\n"
                    _log_to_file(f"[Step {steps}] API fallback Observation injected into conversation.")
                    continue
                
                # Append model's response to conversation log
                conversation_log += f"\n{resp_l}\n"

                parsed = self._extract_react_tool_call(resp_l)
                if parsed.get("ok"):
                    if parsed.get("discarded_actions"):
                        _log_to_file(
                            f"[Step {steps}] Notice: discarded {parsed['discarded_actions']} extra tool call(s) from the same model response."
                        )
                        conversation_log += (
                            "\nObservation: Only the first tool call from your previous response was executed. "
                            "Extra tool calls and completion prose were ignored.\n"
                        )
                    action_key = _action_key(parsed)
                    if action_key in executed_actions:
                        duplicate_steps += 1
                        dup_msg = (
                            "Observation: Duplicate Action skipped. This exact action was already executed; "
                            "choose a different keyword, read a different file/range, or patch based on new evidence."
                        )
                        _log_to_file(f"[Step {steps}] Duplicate action skipped: {action_key}")
                        conversation_log += f"\n{dup_msg}\n"
                        if duplicate_steps >= 3:
                            _log_to_file("=== FINISHED (DUPLICATE ACTION LOOP) ===")
                            return {'result': 'DUPLICATE_ACTION_LOOP', 'history': history}
                        continue
                    executed_actions.add(action_key)
                    invalid_steps = 0
                    duplicate_steps = 0
                    tool_name = parsed.get("tool")
                    if tool_name == "search_keyword":
                        kw = parsed["keyword"]
                        obs = self.tool_search_keyword(base_src, kw)
                        conversation_log += f"\n{obs['observation']}\n"
                        if obs.get("files_only"):
                            conversation_log += f"Observation Files: {json.dumps(obs['files'], ensure_ascii=False, indent=2)}\n"
                        else:
                            conversation_log += f"Observation Results: {json.dumps(obs['matches'], ensure_ascii=False, indent=2)}\n"
                        _log_to_file(f"[Step {steps}] Tool: search_keyword('{kw}') -> {obs['observation']}")
                        continue
                    if tool_name == "list_dir":
                        path = parsed["path"]
                        try:
                            listing = self.tool_list_dir(path)
                            _log_to_file(
                                f"[Step {steps}] Tool: list_dir('{path}') -> SUCCESS (entries: {len(listing['entries'])})"
                            )
                            conversation_log += (
                                f"\nObservation: list_dir('{path}') -> {json.dumps(listing['entries'], ensure_ascii=False)}\n"
                            )
                        except Exception as exc:
                            err_msg = f"ERROR: {exc}"
                            _log_to_file(f"[Step {steps}] Tool: list_dir('{path}') -> ERROR: {exc}")
                            conversation_log += f"\nObservation: {err_msg}\n"
                        continue
                    if tool_name == "read_file":
                        path = _normalize_task_path(parsed["path"])
                        s = parsed.get("start_line")
                        e = parsed.get("end_line")
                        try:
                            txt = self.tool_read_file(path, s, e)
                            _log_to_file(f"[Step {steps}] Tool: read_file('{path}') -> SUCCESS (length: {len(str(txt))})")
                            read_files.add(path)
                        except Exception as exc:
                            txt = f"ERROR: {exc}"
                            _log_to_file(f"[Step {steps}] Tool: read_file('{path}') -> ERROR: {exc}")
                        conversation_log += f"\nObservation: {{'path': '{path}', 'content': ... (length: {len(str(txt))})}}\n"
                        if read_files:
                            conversation_log += (
                                "Observation: Read files so far: "
                                + json.dumps(sorted(read_files), ensure_ascii=False)
                                + "\n"
                            )
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
                            if res.get("ok"):
                                return {'result': res, 'history': history}
                            err_msg = f"Observation: submit_patch failed: {res}"
                        except Exception as exc:
                            err_msg = f"Observation: ERROR executing submit_patch: {exc}"
                            _log_to_file(f"[Step {steps}] Observation: {err_msg}")
                        conversation_log += f"\n{err_msg}\n"
                        continue

                # If model didn't call a known tool, inform it
                if parsed.get("final_without_tool"):
                    _log_to_file("=== FINISHED (MODEL STOPPED WITHOUT TOOL CALL) ===")
                    return {'result': 'MODEL_STOPPED_WITHOUT_TOOL_CALL', 'history': history}
                if parsed.get("error", "").startswith("malformed_"):
                    err_msg = f"Observation: Format Error, malformed {parsed.get('error', '').replace('malformed_', '')} call. Output exactly one valid tool call."
                else:
                    err_msg = "Observation: Format Error, please use the exact schema for one of the available tools."
                invalid_steps += 1
                if invalid_steps >= 3:
                    _log_to_file("=== FINISHED (INVALID OUTPUTS) ===")
                    return {'result': 'INVALID_TOOL_CALL', 'history': history}
                _log_to_file(f"[Step {steps}] Notice: Model didn't call any valid tool.")
                conversation_log += f"\n{err_msg}\n"

            return {'result': 'TIMEOUT', 'history': history}

        except Exception as exc:
            err_msg = f"CRITICAL RUNTIME ERROR: {exc}"
            _log_to_file(f"[Exception] {err_msg}")
            history.append({'step': steps, 'agent_response': err_msg})
            return {'result': err_msg, 'history': history}
        finally:
            self._active_workspace_path = previous_active_workspace

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
                          attempt: int = 1) -> Dict[str, Any]:
        """
        Tool-planning mode uses the same dynamic stop condition as ReAct:
        the model plans exactly one next tool call per cycle, receives the
        observation, and stops only after submit_patch succeeds.
        """
        log_dir = self.results_dir / app_name / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_attempt{attempt}" if attempt > 1 else ""
        step_log_file = log_dir / f"tool_planning_steps{suffix}.log"
        step_log_file.write_text("=== Tool-Planning Execution Steps ===\n", encoding="utf-8")

        def _log_to_file(msg: str) -> None:
            with open(step_log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

        planner_system_prompt = (
            "You are an Android architect controlling source-code discovery tools.\n"
            "\n"
            "You ONLY see: the user's Instruction.\n"
            "You do NOT have source code until you use tools.\n"
            "\n"
            "Work in cycles. In each cycle, choose exactly ONE next tool call based on all previous Observations.\n"
            "Do not make a fixed multi-step plan. After every Observation, re-plan the next single action.\n"
            "When the task is implemented, call submit_patch. A successful submit_patch ends the task.\n"
            "\n"
            "Available Tools (call them EXACTLY as shown):\n"
            "  1. search_keyword(keyword) — Search the project repo for a keyword. Use broader terms when a search returns no matches.\n"
            "  2. list_dir(path) — List directory entries (one level) to explore project structure.\n"
            "  3. read_file(file_path, start_line, end_line) — Read a file's content. Use line numbers from search_keyword.\n"
            "  4. submit_patch(file_path, old_snippet, new_snippet) — Apply a patch (old_snippet -> new_snippet) to file_path. This ends your task.\n"
            "\n"
            "Planning rules:\n"
            "  - Never repeat an identical action after it already produced an Observation.\n"
            "  - If a keyword has zero matches, switch to a different broader keyword, file name, package concept, or UI string.\n"
            "  - Prefer reading concrete search hits before patching.\n"
            "  - Before creating a new settings file, search and read the existing settings screen implementation.\n"
            "  - Do not assume Android XML PreferenceScreen architecture; follow the app's actual UI framework.\n"
            "  - old_snippet in submit_patch must EXACTLY match existing code, including whitespace.\n"
            "  - old_snippet may be empty only for a brand-new file that does not already exist.\n"
            "\n"
            "CRITICAL: Output EXACTLY ONE tool call in plain text. Do NOT output JSON, markdown, explanations, or multiple actions.\n"
            "Examples:\n"
            "  search_keyword(\"Settings\")\n"
            "  list_dir(\"app/src/main\")\n"
            "  read_file(\"app/src/main/res/xml/root_preferences.xml\", 1, 80)\n"
            "  submit_patch(\"app/src/main/res/xml/root_preferences.xml\", \"<old>\", \"<new>\")\n"
            "Once you output an Action, STOP generating. The system will provide the Observation."
        )

        prompt_context = {
            'Instruction': instruction,
        }
        _log_to_file("\n[Planner] System Prompt:\n" + planner_system_prompt)
        _log_to_file("\n[Planner] Initial User Input:\n" + json.dumps(prompt_context, ensure_ascii=False, indent=2))

        env_limit = os.getenv("TOOL_PLANNING_MAX_STEPS") or os.getenv("REACT_MAX_STEPS")
        if env_limit is not None:
            try:
                max_steps = int(env_limit)
            except ValueError:
                max_steps = 0
            max_steps = max_steps if max_steps > 0 else None
        else:
            max_steps = 50

        base_src = self.workspace_dir / app_name / task_id
        previous_active_workspace = self._active_workspace_path
        self._active_workspace_path = base_src
        history: List[Dict[str, Any]] = []
        conversation_log = ""
        executed_actions: set[str] = set()
        read_files: set[str] = set()
        max_empty_retries = 2

        def _normalize_task_path(path: str) -> str:
            cleaned = path.strip().lstrip("/")
            if cleaned.startswith("workspace/"):
                cleaned = cleaned[len("workspace/"):]
            task_prefix = f"{app_name}/{task_id}/"
            if not cleaned.startswith(task_prefix):
                cleaned = task_prefix + cleaned
            return cleaned

        def _action_key(parsed: Dict[str, Any]) -> str:
            tool_name = parsed.get("tool")
            if tool_name == "search_keyword":
                return f"search_keyword:{parsed.get('keyword', '').strip().lower()}"
            if tool_name == "list_dir":
                return f"list_dir:{parsed.get('path', '').strip()}"
            if tool_name == "read_file":
                return (
                    f"read_file:{_normalize_task_path(parsed.get('path', ''))}:"
                    f"{parsed.get('start_line')}:{parsed.get('end_line')}"
                )
            if tool_name == "submit_patch":
                return f"submit_patch:{_normalize_task_path(parsed.get('file_path', ''))}:{hash(parsed.get('old_snip', ''))}"
            return str(parsed.get("normalized", "")).strip().lower()

        steps = 0
        invalid_steps = 0
        duplicate_steps = 0
        try:
            while True:
                steps += 1
                if max_steps is not None and steps > max_steps:
                    _log_to_file("=== FINISHED (TIMEOUT) ===")
                    return {'result': 'TIMEOUT', 'history': history}

                user_input = json.dumps(prompt_context, ensure_ascii=False, indent=2)
                if conversation_log:
                    user_input += "\n\n=== Planning / Tool History ===\n" + conversation_log

                resp = ""
                for attempt_idx in range(max_empty_retries + 1):
                    resp = self._call_llm_with_system(
                        planner_system_prompt,
                        user_input,
                        temperature=self.temperature,
                        stop=["Observation:", "Observation:\n", "\nObservation:", "\nObservation:\n"],
                    )
                    if resp and not resp.strip().startswith("Observation: System Error: API returned empty content"):
                        break
                    _log_to_file(
                        f"[Step {steps}] Empty response retry {attempt_idx + 1}/{max_empty_retries + 1}"
                    )
                resp_l = resp.strip()
                history.append({'step': steps, 'planner_response': resp_l})
                _log_to_file(f"\n[Step {steps}] Planner Output:\n{resp_l}\n")

                if resp_l.startswith("Observation: System Error: API returned empty content"):
                    conversation_log += f"\n{resp_l}\n"
                    _log_to_file(f"[Step {steps}] API empty response persisted; retry next step.")
                    continue
                if resp_l.startswith("Observation: System Error:"):
                    conversation_log += f"\n{resp_l}\n"
                    _log_to_file(f"[Step {steps}] API fallback Observation injected into conversation.")
                    continue

                conversation_log += f"\n{resp_l}\n"
                parsed = self._extract_react_tool_call(resp_l)
                if not parsed.get("ok"):
                    invalid_steps += 1
                    if parsed.get("final_without_tool"):
                        _log_to_file("=== FINISHED (MODEL STOPPED WITHOUT TOOL CALL) ===")
                        return {'result': 'MODEL_STOPPED_WITHOUT_TOOL_CALL', 'history': history}
                    if parsed.get("error", "").startswith("malformed_"):
                        err_msg = f"Observation: Format Error, malformed {parsed.get('error', '').replace('malformed_', '')} call. Output exactly one valid tool call."
                    else:
                        err_msg = "Observation: Format Error, use exactly one of search_keyword(...), read_file(...), submit_patch(...)."
                    _log_to_file(f"[Step {steps}] Notice: invalid tool call.")
                    conversation_log += f"\n{err_msg}\n"
                    history[-1]['observation'] = err_msg
                    if invalid_steps >= 3:
                        _log_to_file("=== FINISHED (INVALID OUTPUTS) ===")
                        return {'result': 'INVALID_TOOL_CALL', 'history': history}
                    continue

                if parsed.get("discarded_actions"):
                    _log_to_file(
                        f"[Step {steps}] Notice: discarded {parsed['discarded_actions']} extra tool call(s) from the same model response."
                    )
                    conversation_log += (
                        "\nObservation: Only the first tool call from your previous response was executed. "
                        "Extra tool calls and completion prose were ignored.\n"
                    )

                action_key = _action_key(parsed)
                if action_key in executed_actions:
                    duplicate_steps += 1
                    dup_msg = (
                        "Observation: Duplicate Action skipped. This exact action was already executed; "
                        "choose a different keyword, read a different file/range, or patch based on what you have learned."
                    )
                    _log_to_file(f"[Step {steps}] Duplicate action skipped: {action_key}")
                    conversation_log += f"\n{dup_msg}\n"
                    history[-1]['observation'] = dup_msg
                    if duplicate_steps >= 3:
                        _log_to_file("=== FINISHED (DUPLICATE ACTION LOOP) ===")
                        return {'result': 'DUPLICATE_ACTION_LOOP', 'history': history}
                    continue
                executed_actions.add(action_key)
                invalid_steps = 0
                duplicate_steps = 0

                tool_name = parsed.get("tool")
                if tool_name == "search_keyword":
                    kw = parsed["keyword"]
                    obs = self.tool_search_keyword(base_src, kw)
                    guidance = ""
                    if obs["total_matches"] == 0:
                        guidance = " Try a broader or app-specific term instead of repeating this keyword."
                    observation = obs["observation"] + guidance
                    conversation_log += f"\n{observation}\n"
                    if obs.get("files_only"):
                        conversation_log += f"Observation Files: {json.dumps(obs['files'], ensure_ascii=False, indent=2)}\n"
                    else:
                        conversation_log += f"Observation Results: {json.dumps(obs['matches'], ensure_ascii=False, indent=2)}\n"
                    history[-1]['observation'] = observation
                    history[-1]['matches'] = obs['matches']
                    _log_to_file(
                        f"[Step {steps}] Tool: search_keyword('{kw}') -> {observation}\n"
                        f"{json.dumps(obs['matches'], ensure_ascii=False, indent=2)}"
                    )
                    continue

                if tool_name == "list_dir":
                    path = parsed["path"]
                    try:
                        listing = self.tool_list_dir(path)
                        _log_to_file(
                            f"[Step {steps}] Tool: list_dir('{path}') -> SUCCESS (entries: {len(listing['entries'])})"
                        )
                        conversation_log += (
                            f"\nObservation: list_dir('{path}') -> {json.dumps(listing['entries'], ensure_ascii=False)}\n"
                        )
                        history[-1]['observation'] = listing
                    except Exception as exc:
                        err_msg = f"ERROR: {exc}"
                        _log_to_file(f"[Step {steps}] Tool: list_dir('{path}') -> ERROR: {exc}")
                        conversation_log += f"\nObservation: {err_msg}\n"
                        history[-1]['observation'] = err_msg
                    continue

                if tool_name == "read_file":
                    path = _normalize_task_path(parsed["path"])
                    s = parsed.get("start_line")
                    e = parsed.get("end_line")
                    try:
                        txt = self.tool_read_file(path, s, e)
                        _log_to_file(f"[Step {steps}] Tool: read_file('{path}', {s}, {e}) -> SUCCESS length={len(txt)}")
                        read_files.add(path)
                    except Exception as exc:
                        txt = f"ERROR: {exc}"
                        _log_to_file(f"[Step {steps}] Tool: read_file('{path}', {s}, {e}) -> ERROR: {exc}")
                    conversation_log += f"\nObservation: {{'path': '{path}', 'content': ... (length: {len(str(txt))})}}\n"
                    if read_files:
                        conversation_log += (
                            "Observation: Read files so far: "
                            + json.dumps(sorted(read_files), ensure_ascii=False)
                            + "\n"
                        )
                    conversation_log += f"--- FILE CONTENT START ---\n{txt}\n--- FILE CONTENT END ---\n"
                    history[-1]['observation'] = {'path': path, 'content_length': len(str(txt)), 'error': txt if txt.startswith("ERROR:") else None}
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
                        if res.get("ok"):
                            _log_to_file("=== FINISHED ===")
                            return {'result': res, 'history': history}
                        err_msg = f"Observation: submit_patch failed: {res}"
                    except Exception as exc:
                        err_msg = f"Observation: ERROR executing submit_patch: {exc}"
                    _log_to_file(f"[Step {steps}] {err_msg}")
                    conversation_log += f"\n{err_msg}\n"
                    history[-1]['observation'] = err_msg
                    continue

            return {'result': 'TIMEOUT', 'history': history}

        except Exception as exc:
            err_msg = f"CRITICAL RUNTIME ERROR: {exc}"
            _log_to_file(f"[Exception] {err_msg}")
            history.append({'step': steps, 'planner_response': err_msg})
            return {'result': err_msg, 'history': history}
        finally:
            self._active_workspace_path = previous_active_workspace

    # ----------------------------------------------------------------------- #
    #  向后兼容入口
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

        # Some OpenAI-compatible gateways strip Markdown fences while leaving
        # the patchfile protocol intact. Accept those blocks as well instead
        # of incorrectly reporting no_code_generated.
        if not ops:
            unfenced_re = re.compile(
                r"patchfile:([^\n]+)\n(.*?)(?=patchfile:[^\n]+\n|\Z)",
                re.DOTALL,
            )
            for file_block in unfenced_re.finditer(llm_response):
                rel_path = file_block.group(1).strip().strip("`")
                body = file_block.group(2).rstrip("` \n")
                if rel_path.endswith(":NEW"):
                    new_body = new_file_re.search(body)
                    if new_body:
                        ops.append((rel_path[:-4], "", new_body.group(1)))
                    continue
                for match in op_re.finditer(body):
                    ops.append((rel_path, match.group(1), match.group(2)))

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

        # Empty SEARCH is reserved for genuinely new files. Applying a :NEW
        # block to an existing file previously appended a second XML document
        # (or a second Java/Kotlin class), producing misleading build failures.
        # Reject it so the model/feedback loop must provide a real SEARCH hunk.
        return None

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

        nested_roots = [p for p in workspace_path.iterdir() if p.is_dir()]
        explicit_root = None
        for root in nested_roots:
            prefix = f"{root.name}/"
            if rel_path.startswith(prefix):
                rel_path = rel_path[len(prefix):]
                explicit_root = root
                break

        candidates = [workspace_path / rel_path]
        candidates.extend(root / rel_path for root in nested_roots)

        for c in candidates:
            if c.exists():
                return c

        if must_exist:
            raise FileNotFoundError(
                f"Target file not found for patch: {rel_path}. Tried: "
                + ", ".join(str(p) for p in candidates)
            )

        # If the model explicitly supplied an existing top-level directory
        # such as app/, preserve it for a new file. Previously app/foo.xml was
        # stripped to foo.xml and written at the task root whenever multiple
        # directories (.gradle, gradle, app, ...) existed.
        if explicit_root is not None:
            return explicit_root / rel_path

        # 新文件优先写入嵌套项目根，避免落到 task 根目录。
        if len(nested_roots) == 1:
            return nested_roots[0] / rel_path
        return workspace_path / rel_path

    def _resolve_workspace_file_path(self, file_path: str, must_exist: bool) -> Path:
        """
        Resolve an agent-supplied path into the task workspace.

        Accepted forms include:
          - app/src/...
          - FoodYou-develop/app/src/...
          - app_foodyou/task_005_notice/app/src/...
          - workspace/app_foodyou/task_005_notice/FoodYou-develop/app/src/...
        """
        cleaned = str(file_path).replace("\\", "/").strip().strip('"').strip("'")
        cleaned = cleaned.replace('"', '').replace("'", "").rstrip("\\")
        if not cleaned:
            raise ValueError("empty file_path")

        absolute = Path(cleaned)
        if absolute.is_absolute():
            try:
                cleaned = absolute.relative_to(self.workspace_dir).as_posix()
            except ValueError:
                raise ValueError(f"Path escapes workspace: {file_path}")

        cleaned = cleaned.lstrip("/")
        if cleaned.startswith("workspace/"):
            cleaned = cleaned[len("workspace/"):]

        parts = cleaned.split("/")
        if len(parts) >= 2:
            workspace_path = self.workspace_dir / parts[0] / parts[1]
            if workspace_path.exists():
                rel_path = "/".join(parts[2:])
                return self._resolve_target_path(workspace_path, rel_path, must_exist=must_exist)

        if self._active_workspace_path is not None:
            return self._resolve_target_path(
                self._active_workspace_path,
                cleaned,
                must_exist=must_exist,
            )

        # If the path does not include app/task, fall back only when a single
        # task workspace exists. This keeps ad-hoc CLI usage convenient without
        # guessing across multiple active tasks.
        task_roots = [
            task_dir
            for app_dir in self.workspace_dir.iterdir()
            if app_dir.is_dir()
            for task_dir in app_dir.iterdir()
            if task_dir.is_dir()
        ] if self.workspace_dir.exists() else []
        if len(task_roots) == 1:
            return self._resolve_target_path(task_roots[0], cleaned, must_exist=must_exist)

        target = self.workspace_dir / cleaned
        if must_exist and not target.exists():
            raise FileNotFoundError(
                f"Cannot resolve workspace path: {file_path}. "
                "Use app_name/task_id/path or call from an initialized ReAct task."
            )
        return target

    def _workspace_relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.workspace_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _write_results_from_tool_result(
        self,
        tool_result: Any,
        app_name: str,
        task_id: str,
    ) -> Dict[str, str]:
        """Convert ReAct submit_patch result into the normal write_results shape."""
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(tool_result, dict) and tool_result.get("ok"):
            rel_path = tool_result.get("relative_path")
            if not rel_path and tool_result.get("path"):
                rel_path = self._workspace_relative_path(Path(tool_result["path"]))
            rel_path = rel_path or "unknown"
            summary = {
                "strategy": self.strategy,
                "tool": "submit_patch",
                "ok": True,
                "path": tool_result.get("path"),
                "relative_path": rel_path,
            }
            (out_dir / "patch_result.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return {rel_path: "SUCCESS"}

        if tool_result == "DONE_NO_PATCH":
            return {"__no_patch__": "ERROR: DONE_NO_PATCH"}

        return {"__react_result__": f"ERROR: {tool_result}"}

    def _build_tool_planning_context(
        self,
        observed_files: dict[str, str],
        max_chars: int = 90000,
    ) -> str:
        """Build bounded source context for the tool_planning executor LLM."""
        parts: list[str] = []
        total = 0
        for rel_path, content in observed_files.items():
            block = f"\n## File: {rel_path}\n```\n{content}\n```\n"
            if total + len(block) > max_chars:
                remaining = max_chars - total
                if remaining > 1000:
                    parts.append(block[:remaining] + "\n... (file truncated) ...\n")
                break
            parts.append(block)
            total += len(block)
        return "".join(parts)

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
    parser.add_argument("--top-k", type=int, default=8,
                        help="检索文件数量（默认 8）")
    args = parser.parse_args()

    runner = AgentRunner(
        model=args.model,
        strategy=args.strategy,
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
