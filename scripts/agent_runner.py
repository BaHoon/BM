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

        # LLM 调用
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent_Runner — 实验核心执行脚本")
    parser.add_argument("app_name",  help="应用名称，如 app_foodyou")
    parser.add_argument("task_id",   help="任务ID，如 task_001_theme")
    parser.add_argument("--model",    default="gemini/gemini-2.5-flash",
                        help="底座模型（默认 gemini/gemini-2.5-flash）")
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
