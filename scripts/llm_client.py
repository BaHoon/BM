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
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from typing import Optional
from openai import OpenAI

# 网络抖动时的重试配置（针对同济端点偶发 500/连接重置）
_DEFAULT_RETRY_TIMES = 3
_DEFAULT_RETRY_BASE_SLEEP = 2.0


def _load_repo_env_file() -> None:
    """从仓库根目录加载 .env（仅填充未设置的环境变量）。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
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
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
_VLM_API_KEY = os.getenv("VLM_API_KEY")
_VLM_BASE_URL = os.getenv("VLM_BASE_URL")
_VLM_MODEL = os.getenv("VLM_MODEL", "Gemini-3.5-flash")
_LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")
_LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8806/v1")
_LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "Qwen3-30B-A3B")
_BENCHMARK_API_KEY = os.getenv("BENCHMARK_API_KEY")
_BENCHMARK_BASE_URL = os.getenv("BENCHMARK_BASE_URL", "https://531288.xyz/v1")
_BENCHMARK_MODEL = os.getenv("BENCHMARK_MODEL", "")


def _ensure_no_proxy_for_tongji(base_url: str) -> None:
    """将同济端点加入 NO_PROXY/no_proxy，避免代理影响校园网访问。"""
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return

    extra = os.getenv("TONGJI_NO_PROXY")
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        entries = [item.strip() for item in current.split(",") if item.strip()]
        if extra:
            entries.extend([item.strip() for item in extra.split(",") if item.strip()])
        if host not in entries:
            entries.append(host)
        os.environ[key] = ",".join(dict.fromkeys(entries))


_ensure_no_proxy_for_tongji(_TONGJI_BASE_URL)


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@contextmanager
def _temporary_clear_proxy(enabled: bool):
    """临时清理代理环境变量，避免请求被强制走代理。"""
    if not enabled:
        yield
        return

    backup = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
    try:
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

# 凡是 model 名以此前缀开头的，都路由到同济端点
_TONGJI_MODEL_PREFIX = "tongji/"
_JUDGE_MODEL_PREFIX = "judge/"
_LOCAL_MODEL_PREFIX = "local/"
_BENCHMARK_MODEL_PREFIX = "benchmark/"


def _resolve_tongji_model(model: str) -> str:
    """返回应通过同济端点访问的真实模型名。"""
    if model.startswith(_TONGJI_MODEL_PREFIX):
        return model[len(_TONGJI_MODEL_PREFIX):]
    return model


def _resolve_routed_model(model: str) -> str:
    """Strip the internal route prefix before sending a model id upstream."""
    if model.startswith(_TONGJI_MODEL_PREFIX):
        return model[len(_TONGJI_MODEL_PREFIX):]
    if model.startswith(_JUDGE_MODEL_PREFIX):
        return model[len(_JUDGE_MODEL_PREFIX):]
    if model.startswith(_LOCAL_MODEL_PREFIX):
        return model[len(_LOCAL_MODEL_PREFIX):]
    if model.startswith(_BENCHMARK_MODEL_PREFIX):
        return model[len(_BENCHMARK_MODEL_PREFIX):]
    return model


def _normalize_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def _uses_tongji(model: str) -> bool:
    return _normalize_model(model).startswith(_TONGJI_MODEL_PREFIX)


def has_configured_api_key(model: str = "deepseek-r1") -> bool:
    """Return whether the API key required by this model route is configured."""
    normalized = _normalize_model(model)
    if normalized.startswith(_JUDGE_MODEL_PREFIX):
        return bool(_VLM_API_KEY)
    if normalized.startswith(_LOCAL_MODEL_PREFIX):
        return bool(_LOCAL_LLM_BASE_URL)
    if normalized.startswith(_BENCHMARK_MODEL_PREFIX):
        return bool(_BENCHMARK_API_KEY and _BENCHMARK_MODEL)
    return bool(_TONGJI_API_KEY if normalized.startswith(_TONGJI_MODEL_PREFIX) else _OPENAI_API_KEY)

# --------------------------------------------------------------------------- #
#  预定义模型别名（便于 CLI 传参）
# --------------------------------------------------------------------------- #
MODEL_ALIASES: dict[str, str] = {
    "deepseek":        "tongji/DeepSeek-R1",
    "deepseek-r1":     "tongji/DeepSeek-R1",
    "DeepSeek-R1":     "tongji/DeepSeek-R1",
    # Dedicated visual evaluator. This route is never used as a tested model.
    "visual-judge":    f"judge/{_VLM_MODEL}",
    "qwen3-local":     f"local/{_LOCAL_LLM_MODEL}",
    "benchmark-model": f"benchmark/{_BENCHMARK_MODEL}",
}

# --------------------------------------------------------------------------- #
#  策略 System Prompt 模板
# --------------------------------------------------------------------------- #
STRATEGY_SYSTEM_PROMPTS: dict[str, str] = {
    "direct": (
        "You are an expert Android developer. "
        "Your only output must be parseable patch blocks. Do not output explanations, planning, or full-file rewrites for existing files. "
        "If UI DOM or activity context is provided in the user input, ground your edits and file choices in those concrete UI elements rather than guessing. "
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
        "PREMISE: Always inspect the UI_DOM_Tree first. Your next tool call MUST be grounded in concrete UI elements (text, ids, class names) observed in the UI DOM. Do NOT guess or brainstorm keywords without UI evidence.\n"
        "If the UI DOM is empty, explicitly note it and then search by app-specific package names or settings-related files, not generic Android classes.\n"
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
        "You are an Android architect controlling source-code discovery tools.\n"
        "\n"
        "You ONLY see: the current Activity/Fragment name, the UI DOM tree (XML structure), and the user's Instruction.\n"
        "You do NOT have source code until you use tools.\n"
        "\n"
        "PREMISE: Always inspect the UI_DOM_Tree first. Your next tool call MUST be grounded in concrete UI elements (text, ids, class names) observed in the UI DOM. Do NOT guess or brainstorm keywords without UI evidence.\n"
        "If the UI DOM is empty, explicitly note it and then search by app-specific package names or settings-related files, not generic Android classes.\n"
        "Work in cycles. In each cycle, choose exactly ONE next tool call based on all previous Observations.\n"
        "Do not make a fixed multi-step plan. After every Observation, re-plan the next single action.\n"
        "When the task is implemented, call submit_patch. A successful submit_patch ends the task.\n"
        "\n"
        "Available Tools (call them EXACTLY as shown):\n"
        "  1. search_keyword(keyword) — Search the project repo for a keyword. Use broader terms when a search returns no matches.\n"
        "  2. read_file(file_path, start_line, end_line) — Read a file's content. Use line numbers from search_keyword.\n"
        "  3. submit_patch(file_path, old_snippet, new_snippet) — Apply a patch (old_snippet -> new_snippet) to file_path. This ends your task.\n"
        "\n"
        "Planning rules:\n"
        "  - Never repeat an identical action after it already produced an Observation.\n"
        "  - If a keyword has zero matches, switch to a different broader keyword, file name, package concept, or UI string.\n"
        "  - Prefer reading concrete search hits before patching.\n"
        "  - old_snippet in submit_patch must EXACTLY match existing code, including whitespace.\n"
        "\n"
        "CRITICAL: Output EXACTLY ONE tool call in plain text. Do NOT output JSON, markdown, explanations, or multiple actions.\n"
        "Examples:\n"
        "  search_keyword(\"Settings\")\n"
        "  read_file(\"app/src/main/res/xml/root_preferences.xml\", 1, 80)\n"
        "  submit_patch(\"app/src/main/res/xml/root_preferences.xml\", \"<old>\", \"<new>\")\n"
        "Once you output an Action, STOP generating. The system will provide the Observation."
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
        self.model = _normalize_model(model)
        self.strategy = strategy
        self.uses_tongji = self.model.startswith(_TONGJI_MODEL_PREFIX)
        self.uses_visual_judge = self.model.startswith(_JUDGE_MODEL_PREFIX)
        self.uses_local = self.model.startswith(_LOCAL_MODEL_PREFIX)
        self.uses_benchmark = self.model.startswith(_BENCHMARK_MODEL_PREFIX)
        if self.uses_benchmark:
            self.api_key = _BENCHMARK_API_KEY
            self.base_url = _BENCHMARK_BASE_URL
        elif self.uses_local:
            self.api_key = _LOCAL_LLM_API_KEY
            self.base_url = _LOCAL_LLM_BASE_URL
        elif self.uses_visual_judge:
            self.api_key = _VLM_API_KEY
            self.base_url = _VLM_BASE_URL
        elif self.uses_tongji:
            self.api_key = _TONGJI_API_KEY
            self.base_url = _TONGJI_BASE_URL
        else:
            self.api_key = _OPENAI_API_KEY
            self.base_url = _OPENAI_BASE_URL
        self._validate_api_keys()
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self._client = OpenAI(**client_kwargs)

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

        model_name = _resolve_routed_model(self.model)
        print(
            f"[LLMClient] model={model_name}  strategy={self.strategy}  "
            f"prompt_chars={sum(len(str(m['content'])) for m in messages)}"
        )

        request_kwargs = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if self.uses_benchmark:
            request_kwargs["max_completion_tokens"] = max_tokens
            request_kwargs["reasoning_effort"] = os.getenv("BENCHMARK_REASONING_EFFORT", "low")
        else:
            request_kwargs["max_tokens"] = max_tokens
        empty_retries = max(0, int(os.getenv("BENCHMARK_EMPTY_RETRIES", "2")))
        response = None
        content = None
        finish_reason = None
        for empty_attempt in range(empty_retries + 1):
            response = self._completion_with_retry(
                lambda: self._client.chat.completions.create(**request_kwargs)
            )
            content = getattr(response.choices[0].message, "content", None)
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if content and content.strip():
                break
            if empty_attempt < empty_retries:
                print(
                    "[LLMClient] WARN: empty response "
                    f"(finish_reason={finish_reason!r}); retry "
                    f"{empty_attempt + 1}/{empty_retries}"
                )
        if not content or not content.strip():
            raise RuntimeError(
                f"Model returned empty content after {empty_retries + 1} attempts "
                f"(finish_reason={finish_reason!r})."
            )
        print(f"[LLMClient] response_chars={len(content)}")
        return content

    def score_visual_binary_checks(
        self,
        task_prompt: str,
        screenshot_paths: list[str],
        checks: list[dict],
        target_node: Optional[dict] = None,
        temperature: float = 0.0,
    ) -> dict:
        """
        用客观二项问题评估 Level 3 视觉语义。

        Returns:
            {
              "passed": bool,
              "checks": [{"id": str, "question": str, "passed": bool, "evidence": str}],
              "reason": str
            }
        """
        images_content = []
        for path in screenshot_paths:
            b64 = _encode_image(path)
            images_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
            })

        normalized_checks = [
            {
                "id": str(item.get("id", f"check_{idx + 1}")),
                "question": str(item.get("question", "")),
            }
            for idx, item in enumerate(checks)
            if item.get("question")
        ]

        eval_prompt = (
            f"Task requirement:\n{task_prompt}\n\n"
            f"Matched UI node from the accessibility tree:\n{target_node or {}}\n\n"
            "You are NOT allowed to give a subjective score. "
            "Answer only the objective binary checks below. "
            "For each check, return true only when the screenshot visibly proves it. "
            "If evidence is missing, ambiguous, off-screen, hidden, or you are unsure, return false.\n\n"
            f"Checks:\n{normalized_checks}\n\n"
            "Return strict JSON only:\n"
            "{"
            "\"passed\": <true only if every check passed>, "
            "\"checks\": [{\"id\": \"...\", \"passed\": <bool>, \"evidence\": \"short visible evidence\"}], "
            "\"reason\": \"one short sentence\""
            "}"
        )

        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": eval_prompt}] + images_content,
            }
        ]

        model_name = _resolve_routed_model(self.model)
        response = self._completion_with_retry(
            lambda: self._client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max(1200, int(os.getenv("VLM_MAX_TOKENS", "2000"))),
            )
        )
        raw = response.choices[0].message.content.strip()
        default = {
            "passed": False,
            "checks": [
                {**item, "passed": False, "evidence": "VLM response was not valid JSON."}
                for item in normalized_checks
            ],
            "reason": raw,
        }
        return _parse_json_safe(raw, default=default)

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

        model_name = _resolve_routed_model(self.model)
        print(
            f"[LLMClient] custom_system_call model={model_name} "
            f"api_base={self.base_url or 'openai_default'} "
            f"prompt_chars={sum(len(str(m['content'])) for m in messages)}"
        )
        try:
            request_kwargs = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
                "stop": stop,
            }
            if self.uses_benchmark:
                request_kwargs["max_completion_tokens"] = max_tokens
                request_kwargs["reasoning_effort"] = os.getenv("BENCHMARK_REASONING_EFFORT", "low")
            else:
                request_kwargs["max_tokens"] = max_tokens
            response = self._completion_with_retry(
                lambda: self._client.chat.completions.create(**request_kwargs)
            )
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
        if self.api_key:
            return
        if self.uses_benchmark:
            key_name = "BENCHMARK_API_KEY/BENCHMARK_MODEL"
        elif self.uses_local:
            key_name = "LOCAL_LLM_BASE_URL"
        elif self.uses_visual_judge:
            key_name = "VLM_API_KEY"
        else:
            key_name = "TONGJI_API_KEY" if self.uses_tongji else "OPENAI_API_KEY"
        raise RuntimeError(f"{key_name} not set for model {self.model}")

    def _completion_with_retry(self, request_fn):
        """对 OpenAI 请求做轻量重试，缓解连接重置/临时 500。"""
        last_exc = None
        force_direct = (
            os.getenv("TONGJI_FORCE_DIRECT", "").lower() in {"1", "true", "yes"}
        )
        for attempt in range(1, _DEFAULT_RETRY_TIMES + 1):
            try:
                with _temporary_clear_proxy(force_direct):
                    return request_fn()
            except Exception as exc:
                last_exc = exc
                # Configuration/channel errors are deterministic; retrying the
                # same unavailable model only wastes judge calls.
                if "model_not_found" in str(exc).lower():
                    break
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
