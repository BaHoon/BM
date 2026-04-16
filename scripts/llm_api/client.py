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
_TONGJI_BASE_URL = os.getenv("TONGJI_BASE_URL", "https://llmapi.tongji.edu.cn/v1")
_TONGJI_API_KEY  = os.getenv("TONGJI_API_KEY")

if not _TONGJI_API_KEY:
    raise RuntimeError(
        "TONGJI_API_KEY environment variable not set. "
        "Please export TONGJI_API_KEY=your_api_key before running."
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
        "Produce complete, compilable code for every file that needs to change. "
        "You MUST end your output with a single line: [FINAL_PATCH]. "
        "Output ONLY code blocks using the format:\n"
        "```filepath:relative/path/to/File.ext\n"
        "<complete file content>\n"
        "```\n"
        "Do NOT add explanations before or after the code blocks."
    ),

    "ReAct": (
        "You are an expert Android developer using the ReAct framework.\n"
        "Limit yourself to at most 10 Thought/Action/Observation steps.\n"
        "Follow this pattern:\n"
        "  Thought: analyse the task and the relevant files\n"
        "  Action: decide which files to modify and why\n"
        "  Observation: describe the expected change\n"
        "  ... (repeat as needed, max 10 steps) ...\n"
        "  Final Answer: output ALL modified files as code blocks:\n"
        "```filepath:relative/path/to/File.ext\n"
        "<complete file content>\n"
        "```\n"
        "Every code block MUST contain the full file, not a diff or snippet.\n"
        "After all code blocks, output one final line: [FINAL_PATCH]."
    ),

    "tool_planning": (
        "You are an expert Android developer.\n"
        "Limit total planning/implementation steps to 10.\n"
        "Step 1 – PLAN: List every file you will create or modify, with a one-line "
        "reason for each.\n"
        "Step 2 – IMPLEMENT: For each planned file, output a complete code block:\n"
        "```filepath:relative/path/to/File.ext\n"
        "<complete file content>\n"
        "```\n"
        "Do NOT skip any planned file.\n"
        "After all code blocks, output one final line: [FINAL_PATCH]."
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
        temperature: float = 0.0,
        max_tokens: int = 4096,
        top_p: float = 1.0,
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

        return self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        top_p: float = 1.0,
    ) -> str:
        """Low-level chat completion for multi-turn agent loops."""
        call_params: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        tongji_model = _resolve_tongji_model(self.model)
        if self.model.startswith("gemini"):
            call_params["custom_llm_provider"] = "gemini"
        elif tongji_model:
            call_params["model"] = f"openai/{tongji_model}"
            call_params["api_base"] = _TONGJI_BASE_URL
            call_params["api_key"] = _TONGJI_API_KEY

        print(
            f"[LLMClient] model={self.model}  strategy={self.strategy}  "
            f"prompt_chars={sum(len(str(m.get('content', ''))) for m in messages)}"
        )
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
