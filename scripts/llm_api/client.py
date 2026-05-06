#!/usr/bin/env python3
"""
LLM API Client - 统一封装 OpenAI / Claude / Gemini 的调用接口

支持的模型：
  - OpenAI:  gpt-4o, gpt-4o-mini
  - Claude:  claude-sonnet-4-5, claude-3-5-sonnet-20241022
  - Gemini:  gemini/gemini-2.5-flash, gemini/gemini-2.5-pro

策略 (strategy) 影响 system prompt 的构造方式：
  - "direct"       : 无逐步推理，直接输出代码
  - "ReAct"        : Reason + Act 循环，先分析后行动
  - "tool_planning": 先制定文件修改计划，再逐一实施
"""

import os
import time
import random
import base64
import litellm
from pathlib import Path
from typing import Optional

# 关闭 litellm 的冗余日志
litellm.set_verbose = False

# 网络抖动时的重试配置（针对同济端点偶发 500/连接重置）
_DEFAULT_RETRY_TIMES = 3
_DEFAULT_RETRY_BASE_SLEEP = 2.0


def _load_repo_env_file() -> None:
    """从仓库根目录加载 .env（仅填充未设置的环境变量）。"""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        print(f"[LLMClient] warning: failed to load .env: {exc}")


_load_repo_env_file()

# --------------------------------------------------------------------------- #
#  同济 OpenAI-Compatible 端点配置
# --------------------------------------------------------------------------- #
_TONGJI_BASE_URL = os.getenv("TONGJI_BASE_URL") or os.getenv(
    "OPENAI_BASE_URL", "https://llmapi.tongji.edu.cn/v1"
)
_TONGJI_API_KEY  = os.getenv("TONGJI_API_KEY") or os.getenv("OPENAI_API_KEY")

if not _TONGJI_API_KEY:
    raise RuntimeError(
        "API key not set. Please set TONGJI_API_KEY (preferred) or "
        "OPENAI_API_KEY before running."
    )


# 凡是 model 名以此前缀开头的，都路由到同济端点
_TONGJI_MODEL_PREFIX = "tongji/"


def _resolve_tongji_model(model: str) -> Optional[str]:
    """返回应通过同济 OpenAI-Compatible 端点访问的真实模型名。"""
    if model.startswith(_TONGJI_MODEL_PREFIX):
        return model[len(_TONGJI_MODEL_PREFIX):]

    # 默认将 OpenAI 兼容模型也路由到同济 base_url。
    if "/" not in model and model.startswith(("gpt-", "o1", "o3", "o4", "DeepSeek", "deepseek")):
        return model

    return None

# --------------------------------------------------------------------------- #
#  预定义模型别名（便于 CLI 传参）
# --------------------------------------------------------------------------- #
MODEL_ALIASES: dict[str, str] = {
    "gpt-4o":          "gpt-4o",
    "gpt-4o-mini":     "gpt-4o-mini",
    "claude":          "claude-sonnet-4-5",
    "claude-3-5":      "claude-3-5-sonnet-20241022",
    "claude-4-5":      "claude-sonnet-4-5",
    "gemini-flash":    "gemini/gemini-2.5-flash",
    "gemini-pro":      "gemini/gemini-2.5-pro",
    "gemini":          "gemini/gemini-2.5-flash",
    #   # 同济 DeepSeek 端点
    "deepseek":        "tongji/DeepSeek-R1",
    "deepseek-r1":     "tongji/DeepSeek-R1",
    "DeepSeek-R1":     "tongji/DeepSeek-R1",
}

# --------------------------------------------------------------------------- #
#  策略 System Prompt 模板
# --------------------------------------------------------------------------- #
STRATEGY_SYSTEM_PROMPTS: dict[str, str] = {
    "direct": (
        "You are an expert Android developer. "
        "Your only output must be parseable patch blocks. Do not output explanations, planning, or full-file rewrites for existing files. "
        "Use this exact format for existing files:\n"
        "```patchfile:RELATIVE_PATH\n"
        "<<<<<<< SEARCH\n"
        "<exact existing text copied from the current file; never empty>\n"
        "=======\n"
        "<replacement text>\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "Use this exact format for new files:\n"
        "```patchfile:RELATIVE_PATH:NEW\n"
        "<<<<<<< NEW\n"
        "<complete new file content>\n"
        ">>>>>>> NEW_END\n"
        "```\n"
        "Rules: RELATIVE_PATH must be a repository-relative path from the workspace root; include module subdirectories; never use absolute paths, drive letters, or ~; SEARCH text must match file content exactly and should include enough context to identify the target uniquely; do not use empty SEARCH for existing files; do not invent placeholder names such as generated1; if you need a new file, give it a concrete descriptive filename."
    ),

    "ReAct": (
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
        "CRITICAL: You are only allowed to output THOUGHT and ACTION. DO NOT output the OBSERVATION yourself. The system will provide the OBSERVATION to you. You MUST wait for the system's response.\n"
        "Once you output an Action, STOP generating. Do NOT write blocks of code outside of submit_patch."
    ),

    "tool_planning": (
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
    ),
}


class LLMClient:
    """统一 LLM 调用客户端，支持多模态（图片）输入用于 Feedback Loop。"""

    def __init__(self, model: str = "deepseek-r1", strategy: str = "ReAct"):
        """
        Args:
            model:    模型名称，可用 MODEL_ALIASES 中的简写。
            strategy: 'direct' | 'ReAct' | 'tool_planning'
        """
        self.model = MODEL_ALIASES.get(model, model)
        self.strategy = strategy
        self._validate_api_keys()

    # ----------------------------------------------------------------------- #
    #  公开接口
    # ----------------------------------------------------------------------- #

    def generate_code(
        self,
        task_prompt: str,
        context: str,
        feedback_screenshots: Optional[list[str]] = None,
        feedback_log: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 16000,
    ) -> str:
        """
        调用 LLM 生成/修复代码。

        Args:
            task_prompt:          来自 meta.json 的任务描述。
            context:              已由 retriever 筛选好的源码上下文字符串。
            feedback_screenshots: 图片路径列表（RQ5 Feedback Loop 用）。
            feedback_log:         Appium / 编译错误日志字符串（RQ5 用）。
            temperature:          采样温度，默认 0.2 保证代码确定性。
            max_tokens:           最大输出 token 数。

        Returns:
            LLM 原始响应文本。
        """
        system_prompt = STRATEGY_SYSTEM_PROMPTS.get(
            self.strategy, STRATEGY_SYSTEM_PROMPTS["ReAct"]
        )
        user_content = self._build_user_content(
            task_prompt, context, feedback_screenshots, feedback_log
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        call_params: dict = {
            "model":       self.model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
        }
        tongji_model = _resolve_tongji_model(self.model)
        if self.model.startswith("gemini"):
            call_params["custom_llm_provider"] = "gemini"
        elif tongji_model:
            call_params["model"]    = f"openai/{tongji_model}"
            call_params["api_base"] = _TONGJI_BASE_URL
            call_params["api_key"]  = _TONGJI_API_KEY

        print(f"[LLMClient] model={self.model}  strategy={self.strategy}  "
              f"prompt_chars={sum(len(str(m['content'])) for m in messages)}")

        response = self._completion_with_retry(call_params)
        content: str = response.choices[0].message.content
        print(f"[LLMClient] response_chars={len(content)}")
        return content

    def score_screenshot(
        self,
        task_prompt: str,
        screenshot_paths: list[str],
        temperature: float = 0.0,
    ) -> dict:
        """
        调用视觉模型（GPT-4o-vision / Gemini）对截图进行审美打分（VLM 评测）。

        Returns:
            {"score": 0-10, "passed": bool, "reason": str}
        """
        images_content = []
        for path in screenshot_paths:
            b64 = _encode_image(path)
            images_content.append({
                "type":      "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
            })

        eval_prompt = (
            f"Task requirement:\n{task_prompt}\n\n"
            "Look at the screenshot(s) of the Android application.\n"
            "Score how well the UI satisfies the visual requirement on a scale of 0-10.\n"
            "Respond in JSON: {\"score\": <int>, \"passed\": <bool>, \"reason\": \"<1-2 sentences>\"}"
        )

        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": eval_prompt}] + images_content,
            }
        ]

        call_params = {
            "model":       self.model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  512,
        }
        tongji_model = _resolve_tongji_model(self.model)
        if self.model.startswith("gemini"):
            call_params["custom_llm_provider"] = "gemini"
        elif tongji_model:
            call_params["model"]    = f"openai/{tongji_model}"
            call_params["api_base"] = _TONGJI_BASE_URL
            call_params["api_key"]  = _TONGJI_API_KEY

        response = self._completion_with_retry(call_params)
        raw = response.choices[0].message.content.strip()
        return _parse_json_safe(raw, default={"score": 0, "passed": False, "reason": raw})

    def generate_with_system_prompt(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.2,
        max_tokens: int = 8000,
        stop: Optional[list[str]] = None,
    ) -> str:
        """使用自定义 system prompt 调用 LLM（复用统一路由与重试逻辑）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        call_params: dict = {
            "model":       self.model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
        }
        if stop:
            call_params["stop"] = stop

        tongji_model = _resolve_tongji_model(self.model)
        if self.model.startswith("gemini"):
            call_params["custom_llm_provider"] = "gemini"
        elif tongji_model:
            call_params["model"]    = f"openai/{tongji_model}"
            call_params["api_base"] = _TONGJI_BASE_URL
            call_params["api_key"]  = _TONGJI_API_KEY

        print(
            f"[LLMClient] custom_system_call model={call_params.get('model')} "
            f"api_base={call_params.get('api_base', '<provider-default>')} "
            f"prompt_chars={sum(len(str(m['content'])) for m in messages)}"
        )
        try:
            response = self._completion_with_retry(call_params)
            if response is None:
                return (
                    "Observation: System Error: API returned no response. "
                    "Please try a narrower search or shorter context."
                )
            content = getattr(response.choices[0].message, "content", None)
            if not content:
                return (
                    "Observation: System Error: API returned empty content. "
                    "Please try a narrower search or shorter context."
                )
            print(f"[LLMClient] custom_system_response_chars={len(content)}")
            return content
        except Exception as exc:
            print(f"[LLMClient] custom_system_call failed: {exc}")
            return (
                "Observation: System Error: API request failed. "
                f"Please try a narrower search or shorter context. Details: {exc}"
            )

    # ----------------------------------------------------------------------- #
    #  私有助手方法
    # ----------------------------------------------------------------------- #

    def _build_user_content(
        self,
        task_prompt: str,
        context: str,
        feedback_screenshots: Optional[list[str]],
        feedback_log: Optional[str],
    ):
        """构造 user message 内容（支持多模态）。"""
        text_parts = [
            "# Task Description\n",
            task_prompt,
            "\n\n# Relevant Source Code\n",
            context,
        ]

        if feedback_log:
            text_parts += [
                "\n\n# Previous Attempt — Error Feedback\n",
                "The code was compiled and tested but FAILED. Fix the issues below.\n",
                f"```\n{feedback_log}\n```",
            ]

        full_text = "".join(text_parts)

        # 纯文本模型（或无截图）：直接返回字符串
        if not feedback_screenshots:
            return full_text

        # 多模态模型：构造 content list
        content: list = [{"type": "text", "text": full_text}]
        for path in feedback_screenshots:
            try:
                b64 = _encode_image(path)
                content.append({
                    "type":      "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
                })
            except Exception as exc:
                print(f"[LLMClient] Warning: could not encode screenshot {path}: {exc}")
        return content

    def _validate_api_keys(self):
        """启动时检查必要的 API Key 是否已设置，仅打印警告。"""
        if _resolve_tongji_model(self.model):
            return

        checks = {
            "gpt":    ("OPENAI_API_KEY",  "https://platform.openai.com/api-keys"),
            "claude": ("ANTHROPIC_API_KEY", "https://console.anthropic.com/"),
            "gemini": ("GEMINI_API_KEY",   "https://aistudio.google.com/app/apikey"),
        }
        for prefix, (env_var, url) in checks.items():
            if self.model.startswith(prefix) and not os.environ.get(env_var):
                print(f"[LLMClient] ⚠  {env_var} not set – "
                      f"get it from {url}")
        # 同济端点：key 已硬编码，无需额外检查

    def _completion_with_retry(self, call_params: dict):
        """对 litellm.completion 做轻量重试，缓解连接重置/临时 500。"""
        last_exc = None
        for attempt in range(1, _DEFAULT_RETRY_TIMES + 1):
            try:
                return litellm.completion(**call_params)
            except Exception as exc:
                last_exc = exc
                if attempt >= _DEFAULT_RETRY_TIMES:
                    break
                sleep_s = _DEFAULT_RETRY_BASE_SLEEP * attempt + random.uniform(0.0, 1.0)
                print(
                    f"[LLMClient] request failed (attempt {attempt}/{_DEFAULT_RETRY_TIMES}): {exc}. "
                    f"retrying in {sleep_s:.1f}s"
                )
                time.sleep(sleep_s)
        raise last_exc


# --------------------------------------------------------------------------- #
#  模块级工具函数
# --------------------------------------------------------------------------- #

def _encode_image(path: str) -> str:
    """将图片文件编码为 base64 字符串。"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_json_safe(text: str, default: dict) -> dict:
    """安全解析 JSON，失败时返回 default。"""
    import json, re
    # 尝试从 ```json ... ``` 代码块中提取
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    # 尝试直接寻找 {...}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except Exception:
        return default
