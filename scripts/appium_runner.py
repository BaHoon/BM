#!/usr/bin/env python3
"""
Appium Runner - 移动应用 UI 自动化测试执行引擎

职责：
  1. 检查/启动 Appium 服务器
  2. 连接到 ADB 设备（本地模拟器或真机）
  3. 使用 UI crawler 自动寻找目标页面
  4. 抓取目标页面 XML 和截图 → 保存到 results/app_name/task_id/
  5. 返回执行结果（success, target_page, log）
"""

import json
import os
import re
import signal
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
import xml.etree.ElementTree as ET

from appium import webdriver
try:
    from appium.webdriver.common.appiumby import AppiumBy
except Exception:
    AppiumBy = None
try:
    from selenium.webdriver.common.by import By
except Exception:
    By = None
# AppiumOptions: different appium client versions expose this in different locations.
# Try several import paths and fall back to None if not available.
try:
    from appium.webdriver.common.appium_options import AppiumOptions
except Exception:
    try:
        from appium.options.common import AppiumOptions
    except Exception:
        try:
            from appium.options.common.base import AppiumOptions
        except Exception:
            AppiumOptions = None

try:
    from runtime_env import ensure_android_runtime_env
except Exception:
    def ensure_android_runtime_env():
        return None

ensure_android_runtime_env()

from tools.level2_utils import build_level2_spec, match_target_xml, normalize_text


class AppiumRunner:
    """Appium 自动化测试执行器。"""

    # Appium 服务配置
    APPIUM_HOST = os.getenv("APPIUM_HOST", "localhost")
    APPIUM_PORT = int(os.getenv("APPIUM_PORT", "4723"))
    APPIUM_TIMEOUT = int(os.getenv("APPIUM_TIMEOUT", "30"))  # 等待 Appium 启动的超时

    # ADB 设备配置
    ADB_DEVICE = os.getenv("ADB_DEVICE", "auto")  # 可用环境变量覆盖
    AVD_NAME = os.getenv("BM_AVD_NAME") or os.getenv("ANDROID_AVD_NAME") or "bm_api36"
    EMULATOR_BOOT_TIMEOUT = int(os.getenv("EMULATOR_BOOT_TIMEOUT", "300"))

    def __init__(self, results_dir: str = "results"):
        self.root_dir = Path(__file__).parent.parent
        self.data_dir = self.root_dir / "data"
        self.results_dir = self.root_dir / results_dir
        self.ADB_DEVICE = os.getenv("ADB_DEVICE", self.ADB_DEVICE)
        self.avd_name = os.getenv("BM_AVD_NAME") or os.getenv("ANDROID_AVD_NAME") or self.AVD_NAME
        self.driver: Optional[webdriver.Remote] = None
        self._target_package_name: Optional[str] = None
        self._appium_process: Optional[subprocess.Popen] = None
        self._emulator_process: Optional[subprocess.Popen] = None
        self._appium_log_handle = None
        self._emulator_log_handle = None
        self._appium_session_url: Optional[str] = None
        self._restart_dedicated_adb_server()

    def _with_alarm(self, seconds: int, fn, description: str):
        """Run a blocking Appium call with a POSIX alarm when on the main thread."""
        if threading.current_thread() is not threading.main_thread() or seconds <= 0:
            return fn()

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

        def _raise_timeout(signum, frame):
            raise TimeoutError(f"{description} timed out after {seconds}s")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer and previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    def _get_page_source(self, timeout: Optional[int] = None) -> str:
        seconds = timeout or int(os.getenv("APPIUM_PAGE_SOURCE_TIMEOUT", "12"))
        if os.getenv("UI_CRAWLER_ADB_DUMP_FIRST", "1") != "0":
            if os.getenv("UI_CRAWLER_TRACE_XML", "0") == "1":
                print("[AppiumRunner] XML dump via adb: start", flush=True)
            xml_text = self._get_page_source_via_adb(seconds)
            if xml_text:
                if os.getenv("UI_CRAWLER_TRACE_XML", "0") == "1":
                    print(
                        f"[AppiumRunner] XML dump via adb: ok {len(xml_text)} bytes",
                        flush=True,
                    )
                return xml_text
            if os.getenv("UI_CRAWLER_TRACE_XML", "0") == "1":
                print("[AppiumRunner] XML dump via adb: fallback to Appium", flush=True)
        if os.getenv("UI_CRAWLER_TRACE_XML", "0") == "1":
            print("[AppiumRunner] XML dump via Appium HTTP: start", flush=True)
        xml_text = self._get_page_source_via_http(seconds)
        if xml_text:
            return xml_text
        if os.getenv("UI_CRAWLER_DRIVER_SOURCE_FALLBACK", "0") != "1":
            raise TimeoutError("page_source unavailable via bounded adb/http sources")
        return self._with_alarm(seconds, lambda: self.driver.page_source, "page_source")

    def _get_page_source_via_adb(self, timeout: int) -> Optional[str]:
        """Fetch the accessibility XML via uiautomator dump before Appium source.

        UiAutomator2's Appium ``page_source`` endpoint can hang on WebView-heavy
        screens. The platform ``uiautomator dump`` command is cruder but gives
        the same node/bounds/text data our crawler needs, and subprocess timeout
        reliably kills it if Android gets stuck.
        """
        device = self.ADB_DEVICE
        if not device or device == "auto":
            return None
        remote_path = "/sdcard/window_dump.xml"
        try:
            dump = subprocess.run(
                [
                    "adb", "-s", device, "shell", "uiautomator", "dump",
                    "--compressed", remote_path,
                ],
                capture_output=True,
                text=True,
                timeout=max(1, timeout),
            )
            if dump.returncode != 0:
                return None
            pull = subprocess.run(
                ["adb", "-s", device, "exec-out", "cat", remote_path],
                capture_output=True,
                timeout=max(1, timeout),
            )
            if pull.returncode != 0 or not pull.stdout:
                return None
            xml_text = pull.stdout.decode("utf-8", errors="ignore")
            if "<hierarchy" in xml_text:
                return xml_text
        except Exception:
            return None
        return None

    def _get_page_source_via_http(self, timeout: int) -> Optional[str]:
        """Fetch Appium page source through a killable curl subprocess."""
        if not self._appium_session_url:
            return None
        url = f"{self._appium_session_url}/source"
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--max-time",
                    str(max(1, timeout)),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=max(2, timeout + 2),
            )
        except Exception:
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        try:
            payload = json.loads(result.stdout)
        except Exception:
            return None
        value = payload.get("value")
        if isinstance(value, str) and "<hierarchy" in value:
            return value
        return None

    def _save_screenshot(self, path: str, timeout: Optional[int] = None) -> bool:
        seconds = timeout or int(os.getenv("APPIUM_SCREENSHOT_TIMEOUT", "12"))
        if self._save_adb_screenshot(path, min(seconds, 8)):
            return True
        return bool(
            self._with_alarm(
                seconds,
                lambda: self.driver.save_screenshot(path),
                "save_screenshot",
            )
        )

    def _save_adb_screenshot(self, path: str, timeout: int) -> bool:
        if not self.ADB_DEVICE:
            return False
        try:
            result = subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode != 0 or not result.stdout:
                return False
            Path(path).write_bytes(result.stdout)
            return True
        except Exception:
            return False

    def check_appium_server(self) -> bool:
        """
        检查 Appium 服务是否可用。
        若不可用则尝试启动；若仍失败则返回 False。
        """
        if self._is_appium_running():
            print("[AppiumRunner] Appium server already running")
            return True

        print("[AppiumRunner] Appium server not running, attempting to start...")
        if self._start_appium_server():
            return True

        print("[AppiumRunner] Failed to start Appium server")
        return False

    def run_test(
        self,
        combined_id: str,
        apk_path: str,
        timeout: int = 300,
    ) -> Dict:
        """
        执行一个测试任务：启动应用 → crawler 自动寻找目标页 → 收集目标页 XML。

        Args:
            combined_id: 任务标识，格式 "app_name/task_id"
            apk_path:    编译产物 APK 路径
            timeout:     测试超时时间（秒）

        Returns:
            {
                "success":      bool,
                "elapsed_time": float,
                "screenshots":  [str],
                "log":          str,
                "test_type":    str,
                "timestamp":    str,
            }
        """
        parts = combined_id.split("/")
        if len(parts) != 2:
            return {
                "success": False,
                "elapsed_time": 0,
                "screenshots": [],
                "log": f"Invalid combined_id format: {combined_id}",
                "test_type": "custom",
                "timestamp": datetime.now().isoformat(),
            }

        app_name, task_id = parts

        t0 = time.time()
        screenshots = []
        target_page = {
            "found": False,
            "current_activity": None,
            "ui_dom_tree_path": None,
            "screenshot": None,
            "matched_nodes": [],
            "source": None,
        }

        try:
            # 步骤 1: 验证 ADB 设备
            if not self._verify_adb_device():
                return {
                    "success": False,
                    "elapsed_time": round(time.time() - t0, 2),
                    "screenshots": [],
                    "log": "ADB device not available",
                    "test_type": "crawler",
                    "target_page": target_page,
                    "timestamp": datetime.now().isoformat(),
                }

            # 步骤 2: 启动应用（通过 Appium）
            self.driver = self._create_driver(apk_path, app_name, task_id)
            print(f"[AppiumRunner] Application started: {app_name}")

            meta = self._load_meta(app_name, task_id)
            print(f"[AppiumRunner] Loaded meta: {app_name}/{task_id}", flush=True)
            if self._needs_special_storage_access(app_name, meta):
                self._grant_special_storage_access()
            level2_spec = build_level2_spec(
                meta,
                self.data_dir / app_name / "base_src",
                self.data_dir / app_name / task_id / "golden_src",
            )
            print(f"[AppiumRunner] Built Level2 spec: {app_name}/{task_id}", flush=True)

            # 创建截图和目标 XML 保存目录
            screenshot_dir = self.results_dir / app_name / task_id / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ui_dir = self.results_dir / app_name / task_id / "ui_context"
            ui_dir.mkdir(parents=True, exist_ok=True)

            self._apply_known_preconditions(app_name, meta, level2_spec)
            print(f"[AppiumRunner] Applied preconditions: {app_name}/{task_id}", flush=True)

            def capture_target_if_match(source_label: str, screenshot_path: Optional[str] = None) -> bool:
                """如果当前页面是目标页，只保存这个目标页 XML。"""
                if target_page["found"]:
                    return True
                try:
                    xml_text = self._get_page_source()
                    self._capture_debug_step(ui_dir, source_label, xml_text)
                    match = match_target_xml(xml_text, level2_spec)
                    if not match.get("matched"):
                        return False
                    if self._reject_premature_target_match(app_name, meta, xml_text, match):
                        return False

                    if self._reposition_target_if_edge_clipped(match):
                        time.sleep(1)
                        refreshed_xml = self._get_page_source()
                        refreshed_match = match_target_xml(refreshed_xml, level2_spec)
                        if refreshed_match.get("matched"):
                            xml_text = refreshed_xml
                            match = refreshed_match

                    if self._close_drawer_if_open_drawer_target_obscured(xml_text, match):
                        time.sleep(1)
                        refreshed_xml = self._get_page_source()
                        refreshed_match = match_target_xml(refreshed_xml, level2_spec)
                        if refreshed_match.get("matched"):
                            xml_text = refreshed_xml
                            match = refreshed_match

                    if screenshot_path is None:
                        filepath = screenshot_dir / "target_page.png"
                        self._save_screenshot(str(filepath))
                        screenshot_path = str(filepath)
                        screenshots.append(screenshot_path)

                    xml_path = ui_dir / "target_page.xml"
                    xml_path.write_text(xml_text, encoding="utf-8")
                    target_page.update({
                        "found": True,
                        "current_activity": self._safe_current_activity(),
                        "ui_dom_tree_path": str(xml_path),
                        "screenshot": screenshot_path,
                        "matched_nodes": match.get("matched_nodes", []),
                        "source": source_label,
                    })
                    visual_path = self._capture_post_target_visual_state(
                        xml_text=xml_text,
                        level2_spec=level2_spec,
                        meta=meta,
                        screenshot_dir=screenshot_dir,
                        ui_dir=ui_dir,
                    )
                    if visual_path:
                        # Judge Level 3 with both pieces of evidence: the
                        # target page where the option/control is visible, and
                        # the post-click visual state where a theme/color choice
                        # has actually been applied.
                        screenshots.append(visual_path)
                        target_page["visual_screenshot"] = visual_path
                        visual_xml_path = ui_dir / "target_visual_state.xml"
                        if visual_xml_path.exists():
                            target_page["visual_ui_dom_tree_path"] = str(visual_xml_path)
                    (ui_dir / "target_page.json").write_text(
                        json.dumps(target_page, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"  [target-page] FOUND via {source_label}")
                    return True
                except Exception as exc:
                    print(f"  [target-page] WARN: {exc}")
                    return False

            # 步骤 3: 使用 crawler 自动找目标页面，不执行人工脚本。
            if capture_target_if_match("precondition:entry"):
                crawler_result = {
                    "screenshots": [],
                    "steps": ["precondition target page"],
                    "log": "Target page found immediately after applying preconditions.",
                }
            else:
                crawler_result = self._crawl_to_target_page(
                    level2_spec=level2_spec,
                    capture_target=capture_target_if_match,
                    timeout=timeout,
                )
            print(f"[AppiumRunner] Crawler finished: {app_name}/{task_id}", flush=True)
            screenshots.extend(crawler_result.get("screenshots", []))
            crawler_debug = {}
            if not target_page["found"]:
                crawler_debug = self._capture_crawler_debug(
                    ui_dir=ui_dir,
                    screenshot_dir=screenshot_dir,
                    level2_spec=level2_spec,
                    steps=crawler_result.get("steps", []),
                    auth_gate=crawler_result.get("auth_gate"),
                )

            elapsed = round(time.time() - t0, 2)
            return {
                "success": bool(target_page["found"]),
                "elapsed_time": elapsed,
                "screenshots": screenshots,
                "log": crawler_result.get("log", ""),
                "crawler_steps": crawler_result.get("steps", []),
                "crawler_debug": crawler_debug,
                "test_type": "crawler",
                "target_page": target_page,
                "level2_spec": level2_spec,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as exc:
            elapsed = round(time.time() - t0, 2)
            error_msg = f"{type(exc).__name__}: {str(exc)}"
            print(f"[AppiumRunner] Test failed: {error_msg}")
            return {
                "success": False,
                "elapsed_time": elapsed,
                "screenshots": screenshots,
                "log": error_msg,
                "test_type": "crawler",
                "target_page": target_page,
                "timestamp": datetime.now().isoformat(),
            }

        finally:
            if self.driver:
                self._cleanup_driver()
                self.driver = None

    def _reject_premature_target_match(self, app_name: str, meta: dict, xml_text: str, match: dict) -> bool:
        prompt = normalize_text(str(meta.get("prompt", "")))
        if app_name == "WebviewKiosk" and "bengali" in prompt and "language" in prompt:
            if "Bengali" in xml_text or "বাংলা" in xml_text:
                return False
            return True
        if app_name == "einkbro" and any(
            term in prompt
            for term in ("refresh mode", "manual refresh", "auto refresh", "content refresh", "refresh interval")
        ):
            if "Refresh mode" in xml_text or "Auto refresh interval" in xml_text:
                return False
            return True
        return False

    def _close_drawer_if_open_drawer_target_obscured(self, xml_text: str, match: dict) -> bool:
        matched_nodes = match.get("matched_nodes") or []
        if "drawer_navigation_view" not in xml_text and "design_navigation_view" not in xml_text:
            return False

        for node in matched_nodes:
            value = normalize_text(str(node.get("value", "")))
            keyword = normalize_text(str(node.get("keyword", "")))
            if value == "open drawer" or keyword == "open drawer":
                return self._go_back()
            parsed = self._parse_bounds(str(node.get("bounds", "")))
            if parsed:
                _, y1, _, y2 = parsed
                if y1 <= 260 and y2 <= 320:
                    return self._go_back()
        return False

    def _cleanup_driver(self) -> None:
        """Best-effort cleanup that must never hide a completed test result."""
        package_name = self._target_package_name
        if package_name:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass

        # Keep this bounded: stale UiAutomator2 sessions leave adb forwards on
        # 820x ports and poison the next benchmark task.
        if os.getenv("APPIUM_QUIT_DRIVER", "1") == "1":
            try:
                self._with_alarm(
                    int(os.getenv("APPIUM_DRIVER_QUIT_TIMEOUT", "5")),
                    lambda: self.driver.quit(),
                    "driver.quit",
                )
            except Exception as exc:
                print(f"[AppiumRunner] WARN: driver.quit skipped/failed: {exc}")
        self._cleanup_stale_uiautomator2_state()

    def _cleanup_stale_uiautomator2_state(self) -> None:
        if not self.ADB_DEVICE:
            return
        commands = (
            ["adb", "-s", self.ADB_DEVICE, "forward", "--remove-all"],
            ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", "io.appium.uiautomator2.server"],
            ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", "io.appium.uiautomator2.server.test"],
        )
        for cmd in commands:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            except Exception:
                pass

    def _capture_post_target_visual_state(
        self,
        xml_text: str,
        level2_spec: dict,
        meta: dict,
        screenshot_dir: Path,
        ui_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Apply stateful visual choices (such as a theme) before Level 3.

        Level 2 must retain the XML where the target option is visible.  Level 3,
        however, needs the resulting visual state when the task explicitly asks
        for a theme/color scheme.  Other buttons remain unclicked so their own
        affordance can be judged on the target-page screenshot.
        """
        prompt = normalize_text(str(meta.get("prompt", "")))
        if not any(term in prompt for term in ("theme", "color scheme", "主题", "配色")):
            return None

        if self._selected_visual_choice_visible(xml_text, level2_spec):
            if ui_dir is not None:
                try:
                    visual_xml_path = ui_dir / "target_visual_state.xml"
                    visual_xml_path.write_text(xml_text, encoding="utf-8")
                except Exception:
                    pass
            filepath = screenshot_dir / "target_visual_state.png"
            if not self._save_screenshot(str(filepath)):
                return None
            return str(filepath)

        if not self._apply_theme_or_color_choice(xml_text, level2_spec):
            return None

        keep_picker_open = False
        try:
            visual_xml = self._get_page_source()
            keep_picker_open = self._selected_visual_choice_visible(visual_xml, level2_spec)
        except Exception:
            visual_xml = ""
        if not keep_picker_open:
            try:
                confirmation = self._choose_confirmation_candidate(self._get_page_source())
                if confirmation:
                    self._tap_candidate(confirmation)
                    time.sleep(2)
                else:
                    time.sleep(1)
            except Exception:
                time.sleep(1)
            try:
                visual_xml = self._get_page_source()
            except Exception:
                visual_xml = ""
        if ui_dir is not None and visual_xml:
            try:
                visual_xml_path = ui_dir / "target_visual_state.xml"
                visual_xml_path.write_text(visual_xml, encoding="utf-8")
            except Exception:
                pass
        filepath = screenshot_dir / "target_visual_state.png"
        if not self._save_screenshot(str(filepath)):
            return None
        return str(filepath)

    def _apply_theme_or_color_choice(self, xml_text: str, level2_spec: dict) -> bool:
        """Open a theme picker if needed, then select the substantive target option."""
        option = self._find_visual_choice_candidate(xml_text, level2_spec)
        if option:
            if not self._tap_candidate(option):
                return False
            time.sleep(1)
            return True

        opener = self._choose_click_candidate(xml_text, level2_spec, set())
        if not opener:
            return False
        opener_label = normalize_text(opener.get("label", ""))
        opener_terms = ("theme", "theme preset", "appearance", "customize", "color", "colors")
        if not any(term in opener_label for term in opener_terms):
            return False
        if not self._tap_candidate(opener):
            return False
        time.sleep(1)

        for _ in range(5):
            try:
                picker_xml = self._get_page_source()
            except Exception:
                return True
            option = self._find_visual_choice_candidate(picker_xml, level2_spec)
            if option:
                if self._tap_candidate(option):
                    time.sleep(1)
                    return True
                return False
            if not self._try_scroll_target_picker(picker_xml, level2_spec):
                if not self._try_scroll(direction="down"):
                    break
            time.sleep(0.5)
        # The opener tap may itself apply/toggle the theme in some apps.
        return True

    def _selected_visual_choice_visible(self, xml_text: str, level2_spec: dict) -> bool:
        substantive_terms = self._substantive_visual_choice_terms(level2_spec)
        if not substantive_terms:
            return False
        generic_only = {"theme", "preset", "theme preset", "active", "switch"}
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return False
        for node in root.iter():
            attrs = node.attrib
            checked = str(attrs.get("checked", "")).lower() == "true"
            selected = str(attrs.get("selected", "")).lower() == "true"
            if not checked and not selected:
                continue
            label = normalize_text(self._label_from_node(node))
            if not label:
                continue
            matched = [
                term for term in substantive_terms
                if term and term not in generic_only and self._label_contains_term(label, term)
            ]
            if matched:
                return True
        return False

    def _find_visual_choice_candidate(self, xml_text: str, level2_spec: dict) -> Optional[dict]:
        substantive_terms = self._substantive_visual_choice_terms(level2_spec)
        if not substantive_terms:
            return None
        scored: list[tuple[int, int, dict]] = []
        candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
        seen_bounds: set[str] = set()
        for candidate in candidates:
            bounds = candidate.get("bounds", "")
            label = normalize_text(candidate.get("label", ""))
            if bounds in seen_bounds and candidate.get("clickable") != "true":
                continue
            seen_bounds.add(bounds)
            if not label:
                continue
            if candidate.get("clickable") != "true" and candidate.get("area", 0) >= 250_000:
                continue
            matched = [
                term for term in substantive_terms
                if term and self._label_contains_term(label, term)
            ]
            if not matched:
                continue
            generic_only = {"theme", "preset", "theme preset", "active", "switch"}
            if all(term in generic_only for term in matched):
                continue
            score = sum(len(term) for term in matched)
            wants_girly_skull = any(
                term in substantive_terms
                for term in ("girly", "girl", "skull", "pink", "girly skull", "skull girl")
            )
            if wants_girly_skull:
                if any(term in label for term in ("girly", "girl", "skull", "pink")):
                    score += 80
                elif any(term in label for term in ("dark", "contrast", "colorblind", "color blind")):
                    score -= 30
            # Prefer explicit option rows/buttons over the broad settings entry.
            if any(term in label for term in ("default", "system default")):
                score -= 20
            scored.append((score, -candidate.get("area", 0), candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    def _text_candidates_from_xml(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return []
        candidates: list[dict] = []
        for node in root.iter():
            attrs = node.attrib
            enabled = attrs.get("enabled", "true").lower()
            displayed = attrs.get("displayed", "true").lower()
            if enabled == "false" or displayed == "false":
                continue
            bounds = attrs.get("bounds", "")
            parsed_bounds = self._parse_bounds(bounds)
            if not parsed_bounds:
                continue
            label = self._label_from_node(node)
            if not label.strip():
                continue
            candidates.append({
                "bounds": bounds,
                "clickable": attrs.get("clickable", "false"),
                "class": attrs.get("class", ""),
                "resource_id": attrs.get("resource-id", ""),
                "label": label,
                "area": self._bounds_area(parsed_bounds),
                "key": self._candidate_action_key(node, bounds, label),
            })
        return candidates

    @staticmethod
    def _substantive_visual_choice_terms(level2_spec: dict) -> list[str]:
        generic = {
            "theme", "preset", "theme preset", "active", "switch", "mode",
            "color", "colors", "appearance", "customize", "setting", "settings",
        }
        terms: list[str] = []
        for raw in level2_spec.get("score_terms", []):
            term = normalize_text(str(raw))
            if not term or term in generic:
                continue
            if len(term) <= 2:
                continue
            terms.append(term)
        for raw in level2_spec.get("target_phrases", []):
            term = normalize_text(str(raw))
            if term and term not in generic:
                terms.append(term)
        joined_terms = " ".join(terms)
        if "skull" in joined_terms and any(term in joined_terms for term in ("girl", "girly")):
            terms.extend(["girl skull", "少女骷髅", "女孩骷髅"])
        seen = set()
        result = []
        for term in terms:
            if term not in seen:
                seen.add(term)
                result.append(term)
        weak_current_values = {
            "system default", "system", "default",
            "custom colors", "custom", "colors",
        }
        strong_result = [term for term in result if term not in weak_current_values]
        if strong_result:
            return strong_result
        return result

    def _apply_known_preconditions(self, app_name: str, meta: dict, level2_spec: dict) -> None:
        prompt = normalize_text(str(meta.get("prompt", "")))
        preconditions = self._precondition_categories_for_prompt(app_name, prompt, level2_spec)
        self._active_preconditions = preconditions
        self._open_known_task_entrypoint(app_name, prompt, level2_spec)
        if preconditions:
            self._seed_device_storage_for_preconditions(preconditions)
        handled_precondition_app = False
        if app_name == "FossifyMessage" and "seed_media_then_delete" in preconditions:
            self._seed_messages_for_delete_precondition()
            handled_precondition_app = True
        if app_name == "MaterialFiles" and "seed_media_then_delete" in preconditions:
            handled_precondition_app = self._prepare_materialfiles_delete_undo()
        if app_name == "Orgzly" and "seed_media_then_delete" in preconditions:
            handled_precondition_app = self._prepare_orgzly_delete_undo()
        if app_name == "Tusky" and "seed_media_then_delete" in preconditions:
            handled_precondition_app = self._prepare_tusky_delete_undo()
        if app_name == "KISS" and any(term in prompt for term in ("theme", "color scheme")):
            if any(term in prompt for term in ("high contrast", "colorblind", "color blind")):
                self._prepare_kiss_high_contrast_setting()
            else:
                self._prepare_kiss_theme_selection()
        if app_name == "FossifyMessage" and any(
            term in prompt
            for term in ("in app sound effects", "sound effects", "play in app sounds")
        ):
            self._prepare_messages_sound_effects_setting()
        if app_name == "FossifyMessage" and any(
            term in prompt
            for term in ("emoji", "rich emojis", "built in emoji library", "emoji library")
        ):
            self._prepare_messages_compose_thread()
        if app_name == "GoalsTracker":
            self._ensure_goalstracker_ready_database(self._target_package_name or "me.timeto.app")
        if app_name == "GoalsTracker" and any(term in prompt for term in ("notification", "reminder")):
            self._prepare_goalstracker_notification_settings()
        if app_name == "GoalsTracker" and "language" in prompt:
            self._prepare_goalstracker_language_picker()
        if app_name == "GoalsTracker" and "share" in prompt:
            self._prepare_goalstracker_summary_share()
        if preconditions & {
            "seed_media_then_delete",
            "seed_media_then_open_editor",
            "create_duplicate_filename_conflict",
        }:
            if app_name != "FossifyMessage" and not handled_precondition_app:
                self._open_seed_media_in_target_app(preconditions)
        if app_name == "TodoAgenda":
            self._open_todoagenda_widget_configuration()
            if any(term in prompt for term in ("entry spacing", "spacing between event entries", "paragraph spacing")):
                self._prepare_todoagenda_entry_spacing_selection()
            elif any(term in prompt for term in ("high contrast", "colorblind", "color blind", "高对比")):
                self._prepare_todoagenda_high_contrast_selection()
            elif any(term in prompt for term in ("theme", "dark mode", "dark theme")):
                self._prepare_todoagenda_theme_selection()
        if app_name == "ItineraryPlanner" and "share" in prompt:
            self._prepare_itineraryplanner_share_page()
        if app_name == "GoalsTracker" and any(term in prompt for term in ("theme", "color scheme")):
            self._prepare_goalstracker_theme_selection()
        if app_name == "GoalsTracker" and any(term in prompt for term in ("background image", "wallpaper")):
            self._prepare_goalstracker_background_image_setting()
        if app_name == "NewsReader" and any(term in prompt for term in ("background image", "wallpaper")):
            self._prepare_newsreader_background_image_setting()
        if app_name == "NewsReader" and any(
            term in prompt for term in ("offline save", "offline download", "caching", "sd card")
        ):
            self._prepare_newsreader_offline_article()
        if app_name == "GoalsTracker" and any(
            term in prompt for term in (
                "emoji library",
                "rich emojis",
                "rich emoji",
                "built in emoji library",
            )
        ):
            self._prepare_goalstracker_emoji_library()
        if app_name == "Tusky":
            if any(term in prompt for term in ("theme", "color scheme")):
                self._open_tusky_general_preferences(open_theme_dialog=True)
            elif any(term in prompt for term in ("background image", "custom background", "upload custom background")):
                self._open_tusky_general_preferences()
            elif any(term in prompt for term in (
                "paragraph spacing", "line spacing", "post line spacing",
                "text display paragraph", "larger paragraph spacing",
            )):
                self._open_tusky_setting_label("Post line spacing")
            elif any(term in prompt for term in (
                "refresh mode", "automatic refresh", "manual refresh",
                "content refresh", "refresh button", "auto refresh",
            )):
                self._open_tusky_refresh_mode_dialog()
            elif any(term in prompt for term in (
                "in-app sound effects", "in app sound effects",
                "sound effects", "sound effect",
            )):
                self._open_tusky_setting_label("In-app sound effects")
            elif any(term in prompt for term in (
                "global navigation", "quick access", "quick menu",
                "core functions", "directly accessible", "home page",
                "in-page word search", "find and match words", "current page",
                "sorting options", "latest publish time", "reading order",
            )):
                if any(term in prompt for term in ("sorting options", "latest publish time", "reading order")):
                    self._open_tusky_reading_order_dialog()
                else:
                    self._open_tusky_main_with_account()

    def _open_known_task_entrypoint(self, app_name: str, prompt: str, level2_spec: dict) -> bool:
        """Start the crawler from a task-relevant first-party screen.

        This is a navigation hint only. It is derived from app name and prompt,
        and applies equally to golden and model-generated APKs.
        """
        package_name = self._target_package_name
        if not package_name:
            return False

        nextcloud_target_text = " ".join([
            prompt,
            " ".join(str(term) for term in level2_spec.get("score_terms", [])),
            " ".join(str(term) for term in level2_spec.get("target_phrases", [])),
        ]).lower()
        if app_name == "DuckDuckGo" and any(term in nextcloud_target_text for term in (
            "background image", "custom background", "overall background image",
            "select background image", "change background image",
        )):
            if self._open_duckduckgo_background_image_setting():
                return True
        if app_name == "NextCloud" and any(term in nextcloud_target_text for term in (
            "theme", "color scheme", "skull", "girly", "girl skull",
            "accessibility", "visual mode", "high contrast", "少女骷髅", "高对比", "辅助视觉",
        )):
            if self._open_nextcloud_theme_selection():
                return True
        if app_name == "foodyou" and any(term in nextcloud_target_text for term in (
            "language", "bengali", "বাংলা", "bn-bd",
        )):
            if self._open_foodyou_language_selection():
                return True
        if app_name == "Orgzly" and any(term in nextcloud_target_text for term in (
            "language", "bengali", "বাংলা",
        )):
            if self._open_orgzly_language_selection():
                return True
        if app_name == "Orgzly" and any(term in nextcloud_target_text for term in (
            "paragraph spacing", "spacing between paragraphs", "pref key paragraph spacing",
        )):
            if self._open_orgzly_paragraph_spacing_selection():
                return True
        if app_name == "Orgzly" and any(term in nextcloud_target_text for term in (
            "theme", "color scheme", "skull", "girly", "girl skull", "pink", "少女", "骷髅",
        )):
            if self._open_orgzly_theme_selection():
                return True
        if app_name == "einkbro" and any(term in nextcloud_target_text for term in (
            "app background image", "custom app background", "overall background image",
            "clear background image", "choose an image", "background image",
        )):
            if self._open_einkbro_app_background_setting():
                return True
        if app_name == "einkbro" and any(term in nextcloud_target_text for term in (
            "refresh mode", "manual refresh", "auto refresh",
            "automatic refresh", "content refresh", "refresh interval",
        )):
            if self._open_einkbro_refresh_settings():
                return True
        if app_name == "einkbro" and any(term in nextcloud_target_text for term in (
            "notifications", "notification reminders", "notification management",
            "in-app notifications", "enable in app notifications",
        )):
            if self._open_einkbro_notification_settings():
                return True
        if app_name == "einkbro" and any(term in nextcloud_target_text for term in (
            "theme", "color scheme", "skull", "girly", "girl skull", "ui theme",
        )):
            if self._open_einkbro_theme_selection():
                return True

        wants_settings = any(term in prompt for term in (
            "theme", "color scheme", "background image", "notification",
            "sound effects", "language", "setting", "settings",
        ))
        wants_folder_import = any(term in prompt for term in (
            "batch import", "root directory", "included folder", "included folders",
            "importing files", "import files",
        ))
        wants_editor = any(term in prompt for term in (
            "edit", "white border", "crop", "emoji library", "rich emojis",
        ))
        wants_share_or_search = any(term in prompt for term in ("share icon", "search button"))

        candidates: list[str] = []
        if app_name == "FossifyGallery":
            if wants_settings or wants_folder_import:
                candidates.append("org.fossify.gallery.activities.SettingsActivity")
            elif wants_editor:
                candidates.append("org.fossify.gallery.activities.EditActivity")
        elif app_name == "FossifyMessage" and wants_settings:
            candidates.append("org.fossify.messages.activities.SettingsActivity")
        elif app_name == "FossifyPaint":
            if wants_settings:
                candidates.append("org.fossify.paint.activities.SettingsActivity")
            elif wants_editor:
                candidates.append("org.fossify.paint.activities.MainActivity")
        elif app_name == "KISS" and wants_settings:
            candidates.extend([
                "fr.neamar.kiss.SettingsActivity",
                "fr.neamar.kiss.preference.SettingsActivity",
                "fr.neamar.kiss.MainActivity",
            ])
        elif app_name == "ItineraryPlanner" and (wants_settings or wants_share_or_search):
            candidates.append("com.pixelarry.itinerary_planner.MainActivity")
        elif app_name == "GoalsTracker":
            candidates.append("me.timeto.app.MainActivity")

        for activity_name in candidates:
            if self._start_activity_via_driver(package_name, activity_name):
                time.sleep(1)
                return True
            if self._start_activity_via_adb(package_name, activity_name):
                time.sleep(1)
                return True
        return False

    def _open_foodyou_language_selection(self) -> bool:
        package_name = self._target_package_name
        if not package_name or not self.ADB_DEVICE:
            return False

        def adb_run(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        def dump_xml() -> str:
            xml_text = self._get_page_source_via_adb(8) or ""
            if xml_text:
                return xml_text
            try:
                return self._get_page_source(timeout=8)
            except Exception:
                return ""

        def tap_label(xml_text: str, terms: tuple[str, ...]) -> bool:
            terms_norm = tuple(normalize_text(term) for term in terms)
            try:
                root = ET.fromstring(xml_text)
            except Exception:
                return False
            candidates: list[tuple[int, int, str, str]] = []
            for node in root.iter():
                attrs = node.attrib
                bounds = attrs.get("bounds", "")
                parsed = self._parse_bounds(bounds)
                if not parsed:
                    continue
                label = self._label_from_node(node)
                normalized = normalize_text(label)
                if not normalized:
                    continue
                if not any(term and term in normalized for term in terms_norm):
                    continue
                x1, y1, x2, y2 = parsed
                area = self._bounds_area(parsed)
                clickable_bonus = 100000000 if attrs.get("clickable", "false") == "true" else 0
                exact_bonus = 50000000 if normalized in terms_norm else 0
                candidates.append((clickable_bonus + exact_bonus + area, (x1 + x2) // 2, (y1 + y2) // 2, label))
            if not candidates:
                return False
            candidates.sort(reverse=True)
            _, x, y, label = candidates[0]
            print(f"[AppiumRunner] Foodyou language entry tap: {label[:80]}", flush=True)
            result = adb_run(["shell", "input", "tap", str(x), str(y)], timeout=5)
            time.sleep(1.1)
            return result.returncode == 0

        def scroll_down() -> bool:
            result = adb_run(["shell", "input", "swipe", "540", "1440", "540", "480", "350"], timeout=5)
            time.sleep(0.8)
            return result.returncode == 0

        def target_visible() -> bool:
            refreshed_xml = dump_xml()
            return "বাংলা (বাংলাদেশ)" in refreshed_xml or "বাংলা" in refreshed_xml

        def tap_xy(x: int, y: int, delay: float = 0.8) -> bool:
            result = adb_run(["shell", "input", "tap", str(x), str(y)], timeout=5)
            time.sleep(delay)
            return result.returncode == 0

        def run_coordinate_language_path() -> bool:
            # Stable Foodyou path observed from the generic crawler: onboarding,
            # import skip, sheet close, settings icon, language row, then scroll.
            print("[AppiumRunner] Foodyou language entry: coordinate fallback", flush=True)
            tap_xy(540, 1680, 1.0)   # Agree & Continue, if present.
            tap_xy(1000, 148, 1.0)   # Skip or top-right settings action.
            tap_xy(540, 480, 0.8)    # Close the optional bottom sheet.
            tap_xy(1000, 148, 1.0)   # Go to settings.
            tap_xy(540, 1620, 1.0)   # Language row.
            for _ in range(3):
                if target_visible():
                    return True
                scroll_down()
            return target_visible()

        component = f"{package_name}/.app.infrastructure.android.MainActivity"
        started = False
        try:
            if self.driver:
                self.driver.activate_app(package_name)
                started = True
        except Exception:
            started = False
        if not started:
            try:
                started = adb_run(["shell", "am", "start", "-n", component], timeout=12).returncode == 0
            except Exception:
                started = False
        if not started:
            started = self._start_activity_via_driver(package_name, ".app.infrastructure.android.MainActivity")
        if not started:
            print("[AppiumRunner] Foodyou language entry: failed to activate app", flush=True)
            return False
        time.sleep(1.8)
        if run_coordinate_language_path():
            print("[AppiumRunner] Foodyou language entry: Bengali visible via coordinate fallback", flush=True)
            return True

        for attempt in range(32):
            xml_text = dump_xml()
            normalized = normalize_text(xml_text)
            if "বাংলা (বাংলাদেশ)" in xml_text or "বাংলা" in xml_text:
                print(f"[AppiumRunner] Foodyou language entry: Bengali visible after {attempt} steps", flush=True)
                return True

            current_package = self._safe_current_package()
            xml_is_target_app = package_name in xml_text
            if self._is_outside_target_app(current_package) or not xml_is_target_app:
                print(f"[AppiumRunner] Foodyou language entry: returning from {current_package}", flush=True)
                try:
                    if self.driver:
                        self.driver.activate_app(package_name)
                    else:
                        adb_run(["shell", "am", "start", "-n", component], timeout=12)
                except Exception:
                    try:
                        adb_run(["shell", "am", "start", "-n", component], timeout=12)
                    except Exception:
                        pass
                time.sleep(1.0)
                continue

            on_language_list = (
                "language" in normalized
                and "system" in normalized
                and "english (united states)" in normalized
                and "go to settings" not in normalized
            )
            if on_language_list:
                print("[AppiumRunner] Foodyou language entry: scroll language list", flush=True)
                scroll_down()
                if target_visible():
                    print(f"[AppiumRunner] Foodyou language entry: Bengali visible after scroll {attempt}", flush=True)
                    return True
                continue

            if any(term in normalized for term in ("agree & continue", "agree and continue")):
                if tap_label(xml_text, ("Agree & Continue", "Agree and Continue")):
                    continue
            if "skip" in normalized:
                if tap_label(xml_text, ("Skip",)):
                    continue
            if any(term in normalized for term in ("close sheet", "close")) and "go to settings" not in normalized:
                if tap_label(xml_text, ("Close sheet", "Close")):
                    continue
            if "go to settings" in normalized:
                if tap_label(xml_text, ("Go to settings",)):
                    continue
            if "language" in normalized and "english (united states)" in normalized:
                if tap_label(xml_text, ("English (United States)", "Language")):
                    continue

            print("[AppiumRunner] Foodyou language entry: fallback scroll", flush=True)
            scroll_down()
            if target_visible():
                print(f"[AppiumRunner] Foodyou language entry: Bengali visible after fallback scroll {attempt}", flush=True)
                return True

        print("[AppiumRunner] Foodyou language entry: Bengali not found", flush=True)
        return False

    def _open_duckduckgo_background_image_setting(self) -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        activity_name = "com.duckduckgo.app.appearance.AppearanceActivity"
        started = self._start_activity_via_adb(package_name, activity_name)
        if not started:
            started = self._start_activity_via_driver(package_name, activity_name)
        if not started:
            return False
        time.sleep(1.5)
        labels = (
            "Background Image",
            "Select Background Image",
            "Change Background Image",
            "Remove Background",
        )
        for _ in range(10):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if any(label in xml_text for label in labels):
                return True
            self._try_scroll("down")
            time.sleep(0.5)
        return True

    def _open_einkbro_ui_setting_containing(self, labels: tuple[str, ...], max_scrolls: int = 8, route: str = "Ui") -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        activity_name = "info.plateaukao.einkbro.activity.SettingActivity"
        if self.ADB_DEVICE:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except Exception:
                pass
        started = self._start_activity_via_adb(package_name, activity_name, extras={"route": route})
        if not started:
            started = self._start_activity_via_driver(package_name, activity_name)
        if not started:
            return False
        time.sleep(1.5)
        for _ in range(max_scrolls):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if any(label in xml_text for label in labels):
                return True
            self._try_scroll("down")
            time.sleep(0.4)
        return True

    def _open_einkbro_refresh_settings(self) -> bool:
        return self._open_einkbro_ui_setting_containing((
            "Refresh mode",
            "Choose manual refresh or auto refresh",
            "Auto refresh interval",
        ), max_scrolls=10, route="Behavior")

    def _open_einkbro_notification_settings(self) -> bool:
        return self._open_einkbro_ui_setting_containing((
            "Notifications",
            "Enable in-app notifications",
        ), max_scrolls=10, route="Behavior")

    def _open_einkbro_app_background_setting(self) -> bool:
        return self._open_einkbro_ui_setting_containing((
            "App background image",
            "Choose an image",
            "Clear background image",
        ), max_scrolls=6)

    def _open_einkbro_theme_selection(self) -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        activity_name = "info.plateaukao.einkbro.activity.SettingActivity"
        if self.ADB_DEVICE:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except Exception:
                pass
        started = self._start_activity_via_adb(package_name, activity_name, extras={"route": "Ui"})
        if not started:
            started = self._start_activity_via_driver(package_name, activity_name)
        if not started:
            return False
        time.sleep(1.5)
        for _ in range(6):
            xml_text = self._get_page_source()
            if "Theme" in xml_text or "Choose a UI theme" in xml_text:
                break
            self._try_scroll("down")
            time.sleep(0.3)
        for label in ("Theme", "Choose a UI theme"):
            if self._click_exact_text(label):
                time.sleep(1)
                return True
        if AppiumBy is not None and self.driver:
            self._set_implicit_wait(1)
            try:
                for label in ("Choose a UI theme", "Theme"):
                    escaped = label.replace('\\', '\\\\').replace('"', '\\"')
                    selector = f'new UiSelector().textContains("{escaped}")'
                    try:
                        self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector).click()
                        time.sleep(1)
                        return True
                    except Exception:
                        pass
            finally:
                self._set_implicit_wait(10)
        return True

    def _precondition_categories_for_prompt(
        self,
        app_name: str,
        prompt: str,
        level2_spec: dict,
    ) -> set[str]:
        categories = set(level2_spec.get("interaction_preconditions") or [])
        theme_task = any(term in prompt for term in ("theme", "color scheme"))
        explicit_background_upload = (
            any(term in prompt for term in ("upload", "select", "choose", "pick"))
            and any(term in prompt for term in ("custom app background image", "overall background image", "wallpaper"))
        )
        if (
            not (theme_task and not explicit_background_upload)
            and any(term in prompt for term in ("upload", "select", "choose", "pick", "attach", "import"))
            and any(
            term in prompt for term in ("photo", "photos", "image", "images", "picture", "file", "files", "folder", "directory")
            )
        ):
            categories.add("seed_storage_and_open_picker")
        if explicit_background_upload:
            categories.add("seed_storage_and_open_picker")
        if "duplicate" in prompt and any(term in prompt for term in ("file name", "filename", "replace", "overwrite", "rename")):
            categories.add("create_duplicate_filename_conflict")
        if "undo" in prompt and any(term in prompt for term in ("delete", "deleted", "removed", "restore")):
            categories.add("seed_media_then_delete")
        if any(term in prompt for term in ("edit", "white border", "crop")) and any(
            term in prompt for term in ("picture", "photo", "image")
        ):
            categories.add("seed_media_then_open_editor")
        if app_name == "TodoAgenda":
            categories.add("open_widget_configuration")
        return categories

    def _prepare_kiss_high_contrast_setting(self) -> bool:
        package_name = self._target_package_name or "fr.neamar.kiss.debug"
        if not self.ADB_DEVICE:
            return False
        self._start_activity_via_adb(package_name, "fr.neamar.kiss.MainActivity")
        time.sleep(1.5)

        steps = (
            ("menu", ("menu",)),
            ("settings", ("kiss settings", "settings")),
            ("user_interface", ("user interface",)),
        )
        for _, terms in steps:
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            candidate = self._best_labeled_candidate(candidates, terms)
            if not candidate or not self._tap_bounds(candidate.get("bounds", "")):
                return False
            time.sleep(1.2)

        target_terms = ("高对比度模式", "高对比度", "高对比", "high contrast", "colorblind", "color blind")
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            candidate = self._best_labeled_candidate(candidates, target_terms)
            if candidate:
                if self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.0)
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _prepare_kiss_theme_selection(self) -> bool:
        package_name = self._target_package_name or "fr.neamar.kiss.debug"
        if not self.ADB_DEVICE:
            return False
        self._start_activity_via_adb(package_name, "fr.neamar.kiss.MainActivity")
        time.sleep(1.5)

        steps = (
            ("menu", ("menu",)),
            ("settings", ("kiss settings", "settings")),
            ("user_interface", ("user interface",)),
        )
        for _, terms in steps:
            if self._kiss_theme_picker_visible():
                return True
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            candidate = self._best_labeled_candidate(candidates, terms)
            if not candidate or not self._tap_bounds(candidate.get("bounds", "")):
                return False
            time.sleep(1.2)

        for _ in range(6):
            if self._kiss_theme_picker_visible():
                return True
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            theme_row = self._find_kiss_theme_row(xml_text)
            if theme_row and self._tap_bounds(theme_row.get("bounds", "")):
                time.sleep(1.5)
                return self._kiss_theme_picker_visible()
            if not self._try_scroll(direction="up"):
                break
            time.sleep(0.6)
        return False

    def _kiss_theme_picker_visible(self) -> bool:
        try:
            xml_text = self._get_page_source()
            visible = normalize_text(xml_text)
        except Exception:
            return False
        if any(term in visible for term in (
            "少女骷髅", "skull girl", "girly skull",
            "high contrast", "colorblind", "color blind",
        )):
            return True
        if "select dialog listview" not in visible and "android:id/select dialog listview" not in visible:
            return False
        if "theme" not in visible:
            return False
        theme_option_count = sum(
            self._label_contains_term(visible, option)
            for option in ("light", "transparent", "dark", "amoled dark")
        )
        return theme_option_count >= 2

    def _find_kiss_theme_row(self, xml_text: str) -> Optional[dict]:
        candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
        scored: list[tuple[int, int, dict]] = []
        for candidate in candidates:
            label = normalize_text(candidate.get("label", ""))
            if "theme" not in label:
                continue
            if any(term in label for term in (
                "themed icons", "theme customisation", "advanced theme",
                "theme shadow", "theme separator", "theme result", "theme wallpaper",
                "theme bar", "icons",
            )):
                continue
            score = 40
            if re.search(r"(?<![a-z0-9])theme(?![a-z0-9])", label):
                score += 30
            if candidate.get("clickable") == "true":
                score += 10
            scored.append((score, -candidate.get("area", 0), candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidate = dict(scored[0][2])
        parsed = self._parse_bounds(candidate.get("bounds", ""))
        if parsed:
            _, y1, _, y2 = parsed
            candidate["bounds"] = f"[0,{max(0, y1 - 42)}][1080,{min(1920, y2 + 51)}]"
        return candidate

    def _prepare_itineraryplanner_share_page(self) -> bool:
        package_name = self._target_package_name or "com.pixelarry.itinerary_planner"
        if not self.ADB_DEVICE:
            return False
        self._seed_itineraryplanner_plan_database(package_name)
        self._start_activity_via_adb(package_name, "com.pixelarry.itinerary_planner.MainActivity")
        time.sleep(2)

        for _ in range(6):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            visible = normalize_text(xml_text)
            if any(term in visible for term in ("share & send", "share via", "action share")):
                return True
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            plan = self._best_labeled_candidate(candidates, ("codex share trip", "benchmark share trip"))
            if plan and self._tap_bounds(plan.get("bounds", "")):
                time.sleep(2)
                continue
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _seed_itineraryplanner_plan_database(self, package_name: str) -> None:
        tmp_db = Path("/tmp/bm_itinerary_plans.db")
        try:
            try:
                tmp_db.unlink(missing_ok=True)
            except Exception:
                pass
            with sqlite3.connect(tmp_db) as conn:
                conn.execute("PRAGMA user_version=3")
                conn.execute(
                    "CREATE TABLE plans ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "title TEXT NOT NULL, "
                    "start TEXT NOT NULL, "
                    "end TEXT NOT NULL, "
                    "image TEXT NOT NULL DEFAULT ''"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE tasks ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "plan_id INTEGER NOT NULL, "
                    "title TEXT NOT NULL, "
                    "start_time TEXT NOT NULL, "
                    "end_time TEXT NOT NULL, "
                    "duration INTEGER NOT NULL, "
                    "cost REAL NOT NULL, "
                    "date TEXT NOT NULL, "
                    "FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE"
                    ")"
                )
                conn.execute(
                    "INSERT INTO plans(title, start, end, image) VALUES (?, ?, ?, ?)",
                    ("Codex Share Trip", "Mon Jul 20 2026", "Tue Jul 21 2026", ""),
                )
                conn.commit()

            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "push", str(tmp_db), "/data/local/tmp/plans.db"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name, "mkdir", "-p", "databases"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "cp", "/data/local/tmp/plans.db", "databases/plans.db",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "rm", "-f", "databases/plans.db-journal", "databases/plans.db-wal", "databases/plans.db-shm",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            print(f"[AppiumRunner] ItineraryPlanner DB seed skipped: {exc}", flush=True)
        finally:
            try:
                tmp_db.unlink(missing_ok=True)
            except Exception:
                pass

    def _prepare_goalstracker_theme_selection(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)

        # The Compose footer exposes "Settings" on a non-clickable child. A
        # coordinate tap on the first-party bottom-right tab is the stable route.
        self._tap_bounds("[720,1710][1080,1857]")
        time.sleep(1.5)

        for _ in range(6):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if any(term in normalize_text(xml_text) for term in ("theme selection", "主题选择")):
                return True
            theme_row = self._find_goalstracker_theme_row(xml_text)
            if theme_row and self._tap_bounds(theme_row["bounds"]):
                time.sleep(1.5)
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _ensure_goalstracker_ready_database(self, package_name: str) -> None:
        tmp_db = Path("/tmp/bm_goalstracker_seed.db")
        try:
            for _ in range(3):
                result = subprocess.run(
                    [
                        "adb", "-s", self.ADB_DEVICE, "exec-out",
                        "run-as", package_name, "cat", "databases/timetome.db",
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout:
                    tmp_db.write_bytes(result.stdout)
                    break
                time.sleep(1)
            else:
                return

            with sqlite3.connect(tmp_db) as conn:
                goal = conn.execute("select id from Goal2Sq order by id limit 1").fetchone()
                if not goal:
                    return
                interval_count = conn.execute("select count(*) from IntervalSq").fetchone()[0]
                if interval_count > 0:
                    return
                conn.execute(
                    "insert into IntervalSq(id, timer, goal_id, note) values (?, ?, ?, ?)",
                    (int(time.time()), 3600, int(goal[0]), None),
                )
                conn.commit()

            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "push", str(tmp_db), "/data/local/tmp/timetome.db"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "cp", "/data/local/tmp/timetome.db", "databases/timetome.db",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "rm", "-f", "databases/timetome.db-journal",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            print(f"[AppiumRunner] GoalsTracker DB seed skipped: {exc}", flush=True)
        finally:
            try:
                tmp_db.unlink(missing_ok=True)
            except Exception:
                pass

    def _find_goalstracker_theme_row(self, xml_text: str) -> Optional[dict]:
        for candidate in self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text):
            label = normalize_text(candidate.get("label", ""))
            if "theme" in label or "主题" in label:
                bounds = candidate.get("bounds", "")
                if bounds:
                    return candidate
        return None

    def _prepare_newsreader_offline_article(self) -> bool:
        package_name = self._target_package_name or "livio.rssreader"
        if not self.ADB_DEVICE:
            return False
        cache_src = self.data_dir / "_preconditions" / "NewsReader" / "codex.cache"
        if not cache_src.exists():
            print(f"[AppiumRunner] WARN: missing NewsReader cache precondition: {cache_src}", flush=True)
            return False
        prefs_xml = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="news_feed">codex</string>
    <string name="refresh_timer">3600</string>
    <string name="download_images">any</string>
    <boolean name="use_external_browser" value="false" />
    <int name="fontsize" value="16" />
</map>
"""
        prefs_tmp = Path("/tmp/newsreader_codex_prefs.xml")
        try:
            prefs_tmp.write_text(prefs_xml, encoding="utf-8")
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "push", str(cache_src), "/data/local/tmp/codex.cache"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "push", str(prefs_tmp), "/data/local/tmp/newsreader_codex_prefs.xml"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "chmod", "644", "/data/local/tmp/codex.cache", "/data/local/tmp/newsreader_codex_prefs.xml"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            install_cmd = (
                "mkdir -p cache shared_prefs; "
                "cp /data/local/tmp/codex.cache cache/codex.cache; "
                "cp /data/local/tmp/newsreader_codex_prefs.xml shared_prefs/livio.rssreader_preferences.xml; "
                "chmod 600 cache/codex.cache shared_prefs/livio.rssreader_preferences.xml"
            )
            result = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell",
                    f"run-as {shlex.quote(package_name)} sh -c {shlex.quote(install_cmd)}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode != 0:
                print(f"[AppiumRunner] WARN: NewsReader cache install failed: {result.stderr[:200]}", flush=True)
                return False
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            print(f"[AppiumRunner] WARN: NewsReader offline seed failed: {exc}", flush=True)
            return False

        self._start_activity_via_adb(package_name, "livio.rssreader.RSSReader")
        time.sleep(2.0)
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            visible = normalize_text(xml_text)
            if "save offline" in visible:
                return True
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            article = self._best_labeled_candidate(candidates, ("codex offline article", "codex benchmark article"))
            if article and self._tap_bounds(article.get("bounds", "")):
                time.sleep(1.5)
                continue
            more = self._best_labeled_candidate(candidates, ("more options", "options"))
            if more and self._tap_bounds(more.get("bounds", "")):
                time.sleep(0.8)
                continue
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.5)
        return False

    def _prepare_newsreader_background_image_setting(self) -> bool:
        package_name = self._target_package_name or "livio.rssreader"
        if not self.ADB_DEVICE:
            return False
        self._start_activity_via_adb(package_name, "livio.rssreader.RSSReader")
        time.sleep(1.5)

        for terms in (("more options", "options"), ("settings",)):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            candidate = self._best_labeled_candidate(candidates, terms)
            if not candidate or not self._tap_bounds(candidate.get("bounds", "")):
                return False
            time.sleep(1.0)

        target_terms = ("background image", "upload custom background image", "custom background")
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            visible = normalize_text(xml_text)
            if any(term in visible for term in target_terms):
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _prepare_goalstracker_background_image_setting(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)
        self._tap_bounds("[720,1710][1080,1857]")
        time.sleep(1.5)

        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            row = self._find_label_row(xml_text, ("background image", "背景图片", "背景图"))
            if row:
                self._tap_bounds(row["bounds"])
                time.sleep(1.5)
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _find_label_row(self, xml_text: str, terms: tuple[str, ...]) -> Optional[dict]:
        normalized_terms = tuple(normalize_text(term) for term in terms)
        candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
        for candidate in candidates:
            label = normalize_text(candidate.get("label", ""))
            if any(term and term in label for term in normalized_terms):
                bounds = candidate.get("bounds", "")
                if bounds:
                    return candidate
        return None

    def _prepare_goalstracker_notification_settings(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)
        self._tap_bounds("[720,1710][1080,1857]")
        time.sleep(1.5)

        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if any(term in normalize_text(xml_text) for term in (
                "timer expired notification",
                "no activity notification",
                "break notification",
            )):
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _prepare_goalstracker_language_picker(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)
        self._tap_bounds("[720,1710][1080,1857]")
        time.sleep(1.5)

        for _ in range(10):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            visible = normalize_text(xml_text)
            if any(term in visible for term in ("বাংলা", "bengali", "한국어", "korean")):
                return True
            row = self._find_label_row(xml_text, ("language", "ভাষা", "언어"))
            if row and self._tap_bounds(row["bounds"]):
                time.sleep(1.5)
                continue
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _prepare_goalstracker_summary_share(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)
        self._tap_bounds("[0,1710][360,1857]")
        time.sleep(1.5)

        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            visible = normalize_text(xml_text)
            if "share" in visible and "summary" in visible:
                return True
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            period = self._best_labeled_candidate(
                [
                    candidate for candidate in candidates
                    if "list" not in normalize_text(candidate.get("label", ""))
                ],
                ("today", "day", "week", "month", "year", "7 days", "30 days"),
            )
            if period and self._tap_bounds(period.get("bounds", "")):
                time.sleep(1.5)
                continue
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _prepare_goalstracker_emoji_library(self) -> bool:
        package_name = self._target_package_name or "me.timeto.app"
        if not self.ADB_DEVICE:
            return False
        self._ensure_goalstracker_ready_database(package_name)
        self._start_activity_via_adb(package_name, "me.timeto.app.MainActivity")
        time.sleep(8)
        # Settings is the bottom-right tab in GoalsTracker's first-party shell.
        self._tap_bounds("[720,1710][1080,1857]")
        time.sleep(1.5)

        in_form = False
        for _ in range(28):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if self._goalstracker_emoji_picker_visible(xml_text):
                return True

            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            if in_form or self._goalstracker_shortcut_form_visible(xml_text):
                in_form = True
                emoji_row = self._best_labeled_candidate(candidates, ("select emoji", "emoji"))
                if emoji_row and self._tap_bounds(emoji_row.get("bounds", "")):
                    time.sleep(1.8)
                    try:
                        if self._goalstracker_emoji_picker_visible(self._get_page_source()):
                            return True
                    except Exception:
                        pass
                    continue
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.6)
                continue

            new_shortcut = self._best_labeled_candidate(candidates, ("new shortcut",))
            if new_shortcut and self._tap_bounds(new_shortcut.get("bounds", "")):
                time.sleep(1.8)
                in_form = True
                continue

            # The SHORTCUTS header is not necessarily clickable; once it is in
            # view, keep paging down until the concrete New Shortcut row appears.
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.6)
        return False

    def _goalstracker_shortcut_form_visible(self, xml_text: str) -> bool:
        visible = normalize_text(xml_text)
        return "new shortcut" in visible and "select emoji" in visible

    def _goalstracker_emoji_picker_visible(self, xml_text: str) -> bool:
        visible = normalize_text(xml_text)
        if "emoji" in visible and any(term in visible for term in ("search emoji", "cancel")):
            return True
        emoji_items = re.findall(
            r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
            xml_text,
        )
        return len(emoji_items) >= 4

    def _seed_device_storage_for_preconditions(self, preconditions: set[str]) -> None:
        storage_conditions = {
            "seed_media_then_delete",
            "seed_media_then_open_editor",
            "seed_storage_and_open_picker",
            "create_duplicate_filename_conflict",
            "open_document_tree_with_seed_directory",
        }
        if not (preconditions & storage_conditions) or not self.ADB_DEVICE:
            return

        seed_script = (
            "mkdir -p /sdcard/Pictures/CodexSeed "
            "/sdcard/Download/CodexSeed/import_root/nested "
            "/sdcard/Download/CodexSeed/source "
            "/sdcard/Download/CodexSeed/target; "
            "screencap -p /sdcard/Pictures/CodexSeed/codex_seed_image.png >/dev/null 2>&1 || true; "
            "cp /sdcard/Pictures/CodexSeed/codex_seed_image.png "
            "/sdcard/Pictures/CodexSeed/codex_seed_delete.png >/dev/null 2>&1 || true; "
            "cp /sdcard/Pictures/CodexSeed/codex_seed_image.png "
            "/sdcard/Pictures/CodexSeed/duplicate_source.png >/dev/null 2>&1 || true; "
            "cp /sdcard/Pictures/CodexSeed/codex_seed_image.png "
            "/sdcard/Pictures/CodexSeed/duplicate_target.png >/dev/null 2>&1 || true; "
            "cp /sdcard/Pictures/CodexSeed/codex_seed_image.png "
            "/sdcard/Download/CodexSeed/codex_seed_upload.png >/dev/null 2>&1 || true; "
            "printf 'codex duplicate source\\n' > /sdcard/Download/CodexSeed/source/duplicate.txt; "
            "printf 'codex duplicate target\\n' > /sdcard/Download/CodexSeed/target/duplicate.txt; "
            "printf 'codex import root file\\n' > /sdcard/Download/CodexSeed/import_root/root_file.txt; "
            "printf 'codex import nested file\\n' > /sdcard/Download/CodexSeed/import_root/nested/nested_file.txt; "
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            "-d file:///sdcard/Pictures/CodexSeed/codex_seed_image.png >/dev/null 2>&1 || true; "
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            "-d file:///sdcard/Pictures/CodexSeed/codex_seed_delete.png >/dev/null 2>&1 || true; "
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            "-d file:///sdcard/Pictures/CodexSeed/duplicate_source.png >/dev/null 2>&1 || true; "
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            "-d file:///sdcard/Pictures/CodexSeed/duplicate_target.png >/dev/null 2>&1 || true; "
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            "-d file:///sdcard/Pictures/CodexSeed/example.png >/dev/null 2>&1 || true"
        )
        try:
            seed_root = self.data_dir / "_preconditions"
            if seed_root.exists():
                push_specs = (
                    (seed_root / "Pictures", "/sdcard/Pictures/CodexSeed"),
                    (seed_root / "Download", "/sdcard/Download/CodexSeed"),
                )
                for local_dir, remote_dir in push_specs:
                    if local_dir.exists():
                        subprocess.run(
                            ["adb", "-s", self.ADB_DEVICE, "shell", "mkdir", "-p", remote_dir],
                            capture_output=True,
                            text=True,
                            timeout=8,
                        )
                        subprocess.run(
                            ["adb", "-s", self.ADB_DEVICE, "push", f"{local_dir}/.", f"{remote_dir}/"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", seed_script],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to seed device storage: {exc}")

        package_name = getattr(self, "_target_package_name", None) or getattr(self, "_active_package", None)
        if package_name:
            self._grant_media_permissions(package_name)

    def _prepare_materialfiles_delete_undo(self) -> bool:
        package_name = self._target_package_name or "me.zhanghai.android.files"
        if not self.ADB_DEVICE:
            return False
        try:
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell",
                    "mkdir -p /sdcard/Pictures/CodexSeed; "
                    "screencap -p /sdcard/Pictures/CodexSeed/codex_seed_delete.png >/dev/null 2>&1 || true",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            self._grant_media_permissions(package_name)
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "appops", "set", package_name, "MANAGE_EXTERNAL_STORAGE", "allow"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass

        try:
            started = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "am", "start", "-S",
                    "-n", f"{package_name}/me.zhanghai.android.files.filelist.FileListActivity",
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            ).returncode == 0
        except Exception:
            started = False
        if not started and not self._start_activity_via_adb(package_name, "me.zhanghai.android.files.filelist.FileListActivity"):
            return False
        time.sleep(2.0)

        for label_terms in (("pictures",), ("codexseed", "codex seed")):
            if not self._tap_materialfiles_labeled_item(label_terms):
                return False
            time.sleep(1.2)

        for _ in range(4):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            menu_bounds = self._materialfiles_row_menu_bounds(xml_text, ("codex_seed_delete", "codex seed delete"))
            if menu_bounds:
                if not self._tap_bounds(menu_bounds):
                    return False
                time.sleep(0.8)
                break
            if not self._try_scroll(direction="down"):
                return False
            time.sleep(0.5)
        else:
            return False

        if not self._tap_materialfiles_popup_item(("delete",)):
            return False
        time.sleep(0.8)
        self._tap_materialfiles_popup_item(("ok", "确定"))
        time.sleep(1.2)
        try:
            visible = normalize_text(self._get_page_source())
            return any(term in visible for term in ("文件已删除", "撤销", "undo"))
        except Exception:
            return True

    def _tap_materialfiles_labeled_item(self, terms: tuple[str, ...]) -> bool:
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            candidate = self._best_labeled_candidate(candidates, terms)
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                return True
            if not self._try_scroll(direction="down"):
                break
            time.sleep(0.5)
        return False

    def _tap_materialfiles_popup_item(self, terms: tuple[str, ...]) -> bool:
        try:
            xml_text = self._get_page_source()
        except Exception:
            return False
        candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
        candidate = self._best_labeled_candidate(candidates, terms)
        if candidate:
            return self._tap_bounds(candidate.get("bounds", ""))
        return False

    def _materialfiles_row_menu_bounds(self, xml_text: str, label_terms: tuple[str, ...]) -> Optional[str]:
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return None
        normalized_terms = tuple(normalize_text(term) for term in label_terms)
        for node in root.iter():
            attrs = node.attrib
            if not str(attrs.get("resource-id", "")).endswith(":id/itemLayout"):
                continue
            row_label_parts: list[str] = []
            menu_bounds = None
            for child in node.iter():
                cattrs = child.attrib
                text_value = cattrs.get("text") or cattrs.get("content-desc") or ""
                if text_value:
                    row_label_parts.append(text_value)
                if str(cattrs.get("resource-id", "")).endswith(":id/menuButton"):
                    menu_bounds = cattrs.get("bounds")
            row_label = normalize_text(" ".join(row_label_parts))
            if menu_bounds and any(term and term in row_label for term in normalized_terms):
                return menu_bounds
        return None

    def _open_seed_media_in_target_app(self, preconditions: set[str]) -> bool:
        package_name = getattr(self, "_target_package_name", None) or getattr(self, "_active_package", None)
        if not package_name or not self.ADB_DEVICE:
            return False
        if "create_duplicate_filename_conflict" in preconditions:
            seed_file = "/sdcard/Pictures/CodexSeed/duplicate_source.png"
        else:
            seed_file = "/sdcard/Pictures/CodexSeed/codex_seed_delete.png"
        action = "android.intent.action.VIEW"
        activity_args: list[str] = ["-p", package_name]
        if "seed_media_then_open_editor" in preconditions:
            action = "android.intent.action.EDIT"
            activity_args = ["-n", f"{package_name}/org.fossify.gallery.activities.EditActivity"]
        try:
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "am", "start",
                    "-a", action,
                    "-d", f"file://{seed_file}",
                    "-t", "image/png",
                ] + activity_args,
                capture_output=True,
                text=True,
                timeout=20,
            )
            time.sleep(2)
            return True
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to open seed media: {exc}")
            return False

    def _grant_media_permissions(self, package_name: str) -> None:
        permissions = (
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.READ_MEDIA_IMAGES",
            "android.permission.READ_MEDIA_VIDEO",
            "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
        )
        for permission in permissions:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "pm", "grant", package_name, permission],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except Exception:
                pass

    def _seed_messages_for_delete_precondition(self) -> None:
        package_name = getattr(self, "_target_package_name", None) or getattr(self, "_active_package", None)
        if not package_name or not self.ADB_DEVICE:
            return
        self._grant_sms_permissions_and_role(package_name)
        now_ms = str(int(time.time() * 1000))
        seed_cmds = [
            [
                "adb", "-s", self.ADB_DEVICE, "shell", "content", "insert",
                "--uri", "content://sms/inbox",
                "--bind", "address:s:+15550143",
                "--bind", "body:s:Codex delete seed message",
                "--bind", f"date:l:{now_ms}",
                "--bind", "read:i:1",
            ],
            [
                "adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name,
            ],
            [
                "adb", "-s", self.ADB_DEVICE, "shell", "monkey", "-p", package_name, "1",
            ],
        ]
        for cmd in seed_cmds:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            except Exception as exc:
                print(f"[AppiumRunner] WARN: failed to seed SMS precondition: {exc}")
        time.sleep(2)
        if not self._compose_seed_message_via_ui(package_name, send_message=True):
            print("[AppiumRunner] WARN: failed to compose seed SMS conversation through UI")

    def _grant_sms_permissions_and_role(self, package_name: str) -> None:
        sms_permissions = (
            "android.permission.READ_SMS",
            "android.permission.WRITE_SMS",
            "android.permission.RECEIVE_SMS",
            "android.permission.SEND_SMS",
            "android.permission.READ_CONTACTS",
        )
        for permission in sms_permissions:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "pm", "grant", package_name, permission],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except Exception:
                pass
        try:
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "cmd", "role",
                    "add-role-holder", "android.app.role.SMS", package_name,
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
        except Exception:
            pass

    def _prepare_messages_compose_thread(self) -> bool:
        package_name = getattr(self, "_target_package_name", None) or getattr(self, "_active_package", None)
        if not package_name or not self.ADB_DEVICE:
            return False
        self._grant_sms_permissions_and_role(package_name)
        return self._compose_seed_message_via_ui(package_name, send_message=False)

    def _compose_seed_message_via_ui(self, package_name: str, send_message: bool = True) -> bool:
        if self.driver is None or AppiumBy is None:
            return False

        def find_by_id(resource_id: str, timeout_s: float = 8.0):
            full_id = f"{package_name}:id/{resource_id}"
            deadline = time.time() + timeout_s
            last_exc = None
            while time.time() < deadline:
                try:
                    return self.driver.find_element(AppiumBy.ID, full_id)
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.4)
            if last_exc:
                raise last_exc
            raise TimeoutError(full_id)

        try:
            fab = find_by_id("conversations_fab", 10)
            fab.click()
            address = find_by_id("new_conversation_address", 10)
            address.click()
            try:
                address.clear()
            except Exception:
                pass
            address.send_keys("+15550143")
            confirm = find_by_id("new_conversation_confirm", 8)
            confirm.click()
            message = find_by_id("thread_type_message", 10)
            message.click()
            if send_message:
                message.send_keys("Codex delete seed message")
                send = find_by_id("thread_send_message", 8)
                send.click()
            time.sleep(2)
            return True
        except Exception as exc:
            print(f"[AppiumRunner] WARN: UI SMS seed failed: {exc}")
            return False

    def _prepare_messages_sound_effects_setting(self) -> bool:
        package_name = getattr(self, "_target_package_name", None) or getattr(self, "_active_package", None)
        if not package_name or self.driver is None:
            return False
        target_markers = (
            f"{package_name}:id/settings_play_sound_effects_holder",
            f"{package_name}:id/settings_play_sound_effects",
            "Play in-app sounds",
        )
        for _ in range(4):
            try:
                xml_text = self._get_page_source(timeout=8)
            except Exception:
                xml_text = ""
            if any(marker in xml_text for marker in target_markers):
                return True
            if not self._try_scroll("down"):
                return False
            time.sleep(0.5)
        return False

    def _prepare_todoagenda_entry_spacing_selection(self) -> bool:
        for step_terms in (("layout",), ("entry spacing", "spacing between event entries")):
            opened = False
            for _ in range(8):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if all(term in xml_text for term in ("SMALL", "MEDIUM", "LARGE")) and "android:id/text1" in xml_text:
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, step_terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False
        try:
            xml_text = self._get_page_source()
            return all(term in xml_text for term in ("SMALL", "MEDIUM", "LARGE"))
        except Exception:
            return True

    def _prepare_todoagenda_high_contrast_selection(self) -> bool:
        for step_terms in (("colors", "color"), ("how text color is defined", "text color")):
            opened = False
            for _ in range(8):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if "High Contrast Mode" in xml_text and ("Color Blind" in xml_text or "high_contrast" in xml_text):
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, step_terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False
        try:
            xml_text = self._get_page_source()
            return "High Contrast Mode" in xml_text or "high_contrast" in xml_text
        except Exception:
            return True

    def _prepare_todoagenda_theme_selection(self) -> bool:
        for step_terms in (("other settings", "other"), ("app theme", "choose the theme")):
            opened = False
            for _ in range(8):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if "Dark theme" in xml_text and "Light theme" in xml_text:
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, step_terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False
        try:
            xml_text = self._get_page_source()
            return "Dark theme" in xml_text and "Light theme" in xml_text
        except Exception:
            return True

    def _open_todoagenda_widget_configuration(self) -> bool:
        """Enter settings with a deterministic synthetic widget id.

        TodoAgenda's launcher activity otherwise only asks the tester to add a
        home-screen widget, so none of its settings targets are reachable by a
        UI-only crawler.
        """
        package_name = self._target_package_name
        if not package_name:
            return False
        widget_id = int(getattr(self, "_todoagenda_widget_id", 0)) + 1
        self._todoagenda_widget_id = widget_id
        # MainActivity is singleTask and only consumes the widget extra from
        # onCreate. A warm `am start` merely delivers onNewIntent and leaves the
        # prerequisite screen unchanged.
        try:
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            pass
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="org.andstatus.todoagenda.MainActivity",
            action="android.appwidget.action.APPWIDGET_CONFIGURE",
            extras={"appWidgetId": widget_id},
        )
        if started:
            time.sleep(2)
            if "WidgetConfigurationActivity" not in (self._safe_current_activity() or ""):
                started = self._start_activity_via_adb(
                    package_name=package_name,
                    activity_name="org.andstatus.todoagenda.MainActivity",
                    action="android.appwidget.action.APPWIDGET_CONFIGURE",
                    extras={"appWidgetId": widget_id},
                )
                time.sleep(2)
        return bool(started and "WidgetConfigurationActivity" in (self._safe_current_activity() or ""))

    def _prepare_orgzly_delete_undo(self) -> bool:
        package_name = self._target_package_name or "com.orgzlyrevived"
        if not self.ADB_DEVICE:
            return False
        try:
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass
        self._start_activity_via_adb(package_name, "com.orgzly.android.ui.LauncherActivity")
        time.sleep(2)

        for _ in range(3):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("ok", "got it", "continue", "确定", "好的"),
            )
            if candidate:
                self._tap_bounds(candidate.get("bounds", ""))
                time.sleep(1)
            else:
                break

        for _ in range(4):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            if "item_book_card_view" in xml_text:
                break
            self._go_back()

        for _ in range(6):
            try:
                xml_text = self._get_page_source()
            except Exception:
                return False
            if "Undo" in xml_text and ("deleted" in xml_text or "Deleted" in xml_text):
                return True
            candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
            book = next((c for c in candidates if str(c.get("resource_id", "")).endswith(":id/item_book_card_view")), None)
            if book and self._long_press_bounds(book.get("bounds", "")):
                time.sleep(1)
                break
            if not self._try_scroll(direction="down"):
                self._go_back()
            time.sleep(0.5)

        for terms in (("delete", "删除"), ("delete", "ok", "确定")):
            clicked = False
            for _ in range(5):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if "Undo" in xml_text and ("deleted" in xml_text or "Deleted" in xml_text):
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    clicked = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.4)
            if not clicked:
                return False

        for _ in range(5):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if "Undo" in xml_text and ("deleted" in xml_text or "Deleted" in xml_text):
                return True
            time.sleep(0.5)
        return False

    def _open_orgzly_paragraph_spacing_selection(self) -> bool:
        package_name = self._target_package_name or "com.orgzlyrevived"
        if not package_name:
            return False
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="com.orgzly.android.ui.settings.SettingsActivity",
        )
        if not started:
            return False
        time.sleep(1.5)

        steps = (
            ("look_and_feel", ("look & feel", "look and feel", "appearance", "外观", "外觀")),
            ("paragraph_spacing", ("paragraph spacing", "adjust spacing between paragraphs")),
        )
        for step_name, terms in steps:
            opened = False
            for _ in range(9):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if step_name == "paragraph_spacing" and "Extra Large" in xml_text and "android:id/text1" in xml_text:
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False

        try:
            xml_text = self._get_page_source()
            return "Extra Large" in xml_text and "Large" in xml_text
        except Exception:
            return True

    def _open_orgzly_language_selection(self) -> bool:
        package_name = self._target_package_name or "com.orgzlyrevived"
        if not package_name:
            return False
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="com.orgzly.android.ui.settings.SettingsActivity",
        )
        if not started:
            return False
        time.sleep(1.5)

        steps = (
            ("look_and_feel", ("look & feel", "look and feel", "appearance", "外观", "外觀")),
            ("language", ("language", "ভাষা", "বাংলা", "bengali")),
        )
        for step_name, terms in steps:
            opened = False
            for _ in range(8):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if step_name == "language" and any(term in xml_text for term in ("বাংলা", "Bengali")) and "android:id/text1" in xml_text:
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False

        try:
            xml_text = self._get_page_source()
            return any(term in xml_text for term in ("বাংলা", "Bengali"))
        except Exception:
            return True

    def _open_orgzly_theme_selection(self) -> bool:
        package_name = self._target_package_name or "com.orgzlyrevived"
        if not package_name:
            return False
        self._seed_orgzly_girly_skull_theme(package_name)
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="com.orgzly.android.ui.settings.SettingsActivity",
        )
        if not started:
            return False
        time.sleep(1.5)

        steps = (
            ("look_and_feel", ("look & feel", "look and feel", "appearance", "外观", "外觀")),
            ("theme", ("theme", "主题", "佈景主題")),
        )
        for step_name, terms in steps:
            opened = False
            for _ in range(7):
                try:
                    xml_text = self._get_page_source()
                except Exception:
                    return False
                if step_name == "theme" and "Girly Skull" in xml_text and "android:id/text1" in xml_text:
                    return True
                candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
                candidate = self._best_labeled_candidate(candidates, terms)
                if candidate and self._tap_bounds(candidate.get("bounds", "")):
                    time.sleep(1.2)
                    opened = True
                    break
                if not self._try_scroll(direction="down"):
                    break
                time.sleep(0.5)
            if not opened:
                return False

        try:
            xml_text = self._get_page_source()
            return "Girly Skull" in xml_text or "girly_skull" in xml_text
        except Exception:
            return True

    def _seed_orgzly_girly_skull_theme(self, package_name: str) -> bool:
        if not self.ADB_DEVICE:
            return False
        pref_name = f"{package_name}_preferences.xml"
        prefs_xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map>\n"
            "    <string name=\"pref_key_color_theme\">girly_skull</string>\n"
            "</map>\n"
        )
        script = (
            "mkdir -p shared_prefs && "
            f"printf %s {shlex.quote(prefs_xml)} > shared_prefs/{shlex.quote(pref_name)} && "
            f"chmod 660 shared_prefs/{shlex.quote(pref_name)}"
        )
        try:
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell",
                    f"run-as {shlex.quote(package_name)} sh -c {shlex.quote(script)}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                print(f"[AppiumRunner] WARN: Orgzly theme seed failed: {result.stderr[:200]}", flush=True)
                return False
            return True
        except Exception as exc:
            print(f"[AppiumRunner] WARN: Orgzly theme seed failed: {exc}", flush=True)
            return False

    def _open_nextcloud_theme_selection(self) -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        self._seed_nextcloud_skull_girl_theme(package_name)
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="com.owncloud.android.ui.activity.ExtendedSettingsActivity",
            extras={"dialog_type": "theme_selection_result"},
        )
        if started:
            time.sleep(1)
        return started

    def _seed_nextcloud_skull_girl_theme(self, package_name: str) -> bool:
        if not self.ADB_DEVICE:
            return False
        pref_name = f"{package_name}_preferences.xml"
        prefs_xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map>\n"
            "    <string name=\"dark_theme_mode\">SKULL_GIRL</string>\n"
            "</map>\n"
        )
        script = (
            "mkdir -p shared_prefs && "
            f"printf %s {shlex.quote(prefs_xml)} > shared_prefs/{shlex.quote(pref_name)} && "
            f"chmod 660 shared_prefs/{shlex.quote(pref_name)}"
        )
        try:
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell",
                    f"run-as {shlex.quote(package_name)} sh -c {shlex.quote(script)}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                print(f"[AppiumRunner] WARN: NextCloud theme seed failed: {result.stderr[:200]}", flush=True)
                return False
            return True
        except Exception as exc:
            print(f"[AppiumRunner] WARN: NextCloud theme seed failed: {exc}", flush=True)
            return False

    def _open_tusky_general_preferences(self, open_theme_dialog: bool = False) -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        self._prepare_tusky_benchmark_account(package_name)
        activity_name = "com.keylesspalace.tusky.components.preference.PreferencesActivity"
        if self.ADB_DEVICE:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name=activity_name,
            extras={"EXTRA_PREFERENCE_TYPE": 0},
        )
        if not started:
            started = self._start_activity_via_driver(
                package_name=package_name,
                activity_name=activity_name,
            )
        if started and self.driver:
            time.sleep(2)
            if open_theme_dialog and self._open_tusky_app_theme_dialog():
                return True
        return started

    def _open_tusky_setting_label(self, label: str) -> bool:
        if not self._open_tusky_general_preferences():
            return False
        escaped = label.replace('\\', '\\\\').replace('\"', '\\"')
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if label in xml_text:
                return True
            if self.driver and AppiumBy is not None:
                try:
                    self.driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiScrollable(new UiSelector().scrollable(true)).scrollTextIntoView("' + escaped + '")',
                    )
                    time.sleep(0.5)
                    continue
                except Exception:
                    pass
            self._try_scroll(direction="down")
            time.sleep(0.4)
        return False

    def _open_tusky_refresh_mode_dialog(self) -> bool:
        if not self._open_tusky_general_preferences():
            return False
        for _ in range(8):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if "Auto refresh" in xml_text and "Manual (tap Refresh)" in xml_text:
                return True
            if self._click_exact_text("Refresh mode"):
                time.sleep(1)
                continue
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("refresh mode",),
            )
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                time.sleep(1)
                continue
            self._try_scroll(direction="down")
            time.sleep(0.4)
        return False

    def _open_tusky_main_with_account(self, open_overflow_menu: bool = False) -> bool:
        package_name = self._target_package_name
        if not package_name:
            return False
        self._prepare_tusky_benchmark_account(package_name)
        if self.ADB_DEVICE:
            try:
                subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
        started = self._start_activity_via_adb(
            package_name=package_name,
            activity_name="com.keylesspalace.tusky.MainActivity",
        )
        if started and self.driver:
            time.sleep(3)
            if open_overflow_menu:
                self._open_tusky_overflow_menu()
        return started

    def _open_tusky_overflow_menu(self) -> bool:
        for _ in range(3):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if ">Sort<" in xml_text or 'text="Sort"' in xml_text:
                return True
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("more options",),
            )
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                time.sleep(1)
                continue
            break
        return False

    def _open_tusky_reading_order_dialog(self) -> bool:
        if not self._open_tusky_main_with_account(open_overflow_menu=True):
            return False
        for _ in range(4):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if any(term in xml_text for term in ("Reading order", "Newest first", "Oldest first")):
                return True
            if self._click_exact_text("Sort"):
                time.sleep(1)
                continue
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("sort",),
            )
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                time.sleep(1)
                continue
            if self._open_tusky_overflow_menu():
                time.sleep(0.5)
                continue
            break
        return False

    def _prepare_tusky_benchmark_account(self, package_name: str) -> bool:
        if not self.ADB_DEVICE:
            return False
        try:
            subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "am", "start",
                    "-n", f"{package_name}/com.keylesspalace.tusky.components.login.LoginActivity",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            time.sleep(1.2)
        except Exception:
            pass
        sql = """
DELETE FROM AccountEntity;
INSERT INTO AccountEntity (id,domain,accessToken,clientId,clientSecret,isActive,accountId,username,displayName,profilePictureUrl,profileHeaderUrl,notificationsEnabled,notificationsMentioned,notificationsFollowed,notificationsFollowRequested,notificationsReblogged,notificationsFavorited,notificationsPolls,notificationsSubscriptions,notificationsUpdates,notificationsAdmin,notificationsOther,notificationSound,notificationVibration,notificationLight,defaultPostPrivacy,defaultReplyPrivacy,defaultMediaSensitivity,defaultPostLanguage,alwaysShowSensitiveMedia,alwaysOpenSpoiler,mediaPreviewEnabled,lastNotificationId,notificationMarkerId,emojis,tabPreferences,notificationsFilter,oauthScopes,unifiedPushUrl,pushPubKey,pushPrivKey,pushAuth,pushServerKey,lastVisibleHomeTimelineStatusId,locked,hasDirectMessageBadge,isShowHomeBoosts,isShowHomeReplies,isShowHomeSelfBoosts)
VALUES (1,'example.social','benchmark-token','','',1,'1','benchmark','Benchmark User','','',0,0,0,0,0,0,0,0,0,1,1,0,0,0,1,0,0,'',0,0,1,'0','0','[]','Home;Notifications;Local;Direct','[]','','','','','','',NULL,0,0,1,1,1);
""".strip()
        try:
            result = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "sqlite3", f"/data/data/{package_name}/databases/tuskyDB",
                ],
                input=sql,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode != 0:
                print(f"[AppiumRunner] WARN: failed to seed Tusky account: {result.stderr[:200]}")
            return result.returncode == 0
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to seed Tusky account: {exc}")
            return False

    def _seed_tusky_delete_status(self, package_name: str) -> bool:
        if not self.ADB_DEVICE:
            return False
        now_ms = int(time.time() * 1000)
        sql = f"""
INSERT OR REPLACE INTO TimelineAccountEntity
(serverId,tuskyAccountId,localUsername,username,displayName,url,avatar,note,emojis,bot)
VALUES ('1',1,'benchmark','benchmark@example.social','Benchmark User','https://example.social/@benchmark','','','[]',0);
INSERT OR REPLACE INTO TimelineStatusEntity
(serverId,url,tuskyAccountId,authorServerId,inReplyToId,inReplyToAccountId,content,createdAt,editedAt,emojis,reblogsCount,favouritesCount,repliesCount,reblogged,bookmarked,favourited,sensitive,spoilerText,visibility,attachments,mentions,tags,application,poll,muted,expanded,contentCollapsed,contentShowing,pinned,card,language,filtered)
VALUES ('codex-delete-status','https://example.social/@benchmark/1',1,'1',NULL,NULL,'<p>Codex undo delete post</p>',{now_ms},NULL,'[]',0,0,0,0,0,0,0,'',0,'[]','[]','[]',NULL,NULL,0,1,0,1,0,NULL,'en','[]');
INSERT OR REPLACE INTO HomeTimelineEntity
(tuskyAccountId,id,statusId,reblogAccountId,loading)
VALUES (1,'codex-delete-status','codex-delete-status',NULL,0);
""".strip()
        try:
            result = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "run-as", package_name,
                    "sqlite3", f"/data/data/{package_name}/databases/tuskyDB",
                ],
                input=sql,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode != 0:
                print(f"[AppiumRunner] WARN: failed to seed Tusky status: {result.stderr[:200]}")
            return result.returncode == 0
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to seed Tusky status: {exc}")
            return False

    def _prepare_tusky_delete_undo(self) -> bool:
        package_name = self._target_package_name or "com.keylesspalace.tusky"
        if not self.ADB_DEVICE:
            return False
        self._prepare_tusky_benchmark_account(package_name)
        self._seed_tusky_delete_status(package_name)
        try:
            subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "am", "force-stop", package_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass
        if not self._start_activity_via_adb(package_name, "com.keylesspalace.tusky.MainActivity"):
            return False
        time.sleep(3)
        for _ in range(10):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            visible = normalize_text(xml_text)
            if any(term in visible for term in ("post deleted", "undo", "an error occurred")):
                return True
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("more options", "delete", "codex undo delete post"),
            )
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                time.sleep(1.0)
                continue
            self._try_scroll(direction="down")
            time.sleep(0.5)
        return False

    def _open_tusky_app_theme_dialog(self) -> bool:
        for _ in range(4):
            try:
                xml_text = self._get_page_source()
            except Exception:
                xml_text = ""
            if "Skullgirl" in xml_text or "Girly Skull" in xml_text:
                return True
            candidate = self._best_labeled_candidate(
                self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text),
                ("app theme", "theme"),
            )
            if candidate and self._tap_bounds(candidate.get("bounds", "")):
                time.sleep(1)
                continue
            break
        return False

    def _start_activity_via_driver(self, package_name: str, activity_name: str) -> bool:
        if not self.driver:
            return False
        try:
            start_activity = getattr(self.driver, "start_activity", None)
            if callable(start_activity):
                start_activity(package_name, activity_name)
                return True
        except TypeError:
            try:
                start_activity(app_package=package_name, app_activity=activity_name)
                return True
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.driver.execute_script(
                "mobile: startActivity",
                {"intent": f"{package_name}/{activity_name}"},
            )
            return True
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to start activity via driver: {exc}")
            return False

    def _start_activity_via_adb(
        self,
        package_name: str,
        activity_name: str,
        extras: Optional[dict[str, int | str]] = None,
        action: Optional[str] = None,
    ) -> bool:
        if not self.ADB_DEVICE:
            return False
        cmd = [
            "adb", "-s", self.ADB_DEVICE, "shell", "am", "start",
            "-n", f"{package_name}/{activity_name}",
        ]
        if action:
            cmd.extend(["-a", action])
        for key, value in (extras or {}).items():
            if isinstance(value, int):
                cmd.extend(["--ei", key, str(value)])
            else:
                cmd.extend(["--es", key, str(value)])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result.returncode == 0
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to start activity via adb: {exc}")
            return False

    def _choose_confirmation_candidate(self, xml_text: str) -> Optional[dict]:
        action_terms = ("set theme", "apply", "save", "done", "confirm", "ok", "确定", "应用", "保存", "完成")
        negative_terms = ("cancel", "close", "later", "取消", "关闭", "稍后")
        scored: list[tuple[int, int, dict]] = []
        for candidate in self._click_candidates_from_xml(xml_text):
            label = normalize_text(candidate.get("label", ""))
            if any(self._label_contains_term(label, term) for term in negative_terms):
                continue
            score = sum(
                4 for term in action_terms if self._label_contains_term(label, normalize_text(term))
            )
            if score:
                scored.append((score, candidate.get("area", 0), candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    # ----------------------------------------------------------------------- #
    #  私有方法
    # ----------------------------------------------------------------------- #

    def _verify_adb_device(self) -> bool:
        """验证 ADB 设备是否可用。"""
        devices = self._list_adb_devices()
        if self._select_adb_device(devices):
            return True

        print("[AppiumRunner] No configured ADB device available; attempting emulator startup")
        if self._start_emulator():
            devices = self._list_adb_devices()
            return self._select_adb_device(devices)

        return False

    def _restart_dedicated_adb_server(self) -> None:
        """Restart only a non-default per-user adb server so env changes take effect."""
        adb_port = os.getenv("ADB_SERVER_PORT")
        if not adb_port or adb_port == "5037":
            return
        if os.getenv("BM_RESTART_ADB_SERVER", "1") == "0":
            return
        try:
            subprocess.run(["adb", "kill-server"], capture_output=True, text=True, timeout=10)
            subprocess.run(["adb", "start-server"], capture_output=True, text=True, timeout=20)
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to restart adb server on {adb_port}: {exc}")

    def _list_adb_devices(self, start_server: bool = True) -> List[str]:
        """Return online ADB device ids."""
        try:
            if start_server:
                subprocess.run(
                    ["adb", "start-server"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self._ensure_tcp_adb_connection()
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            devices = []
            for raw_line in result.stdout.splitlines()[1:]:
                parts = raw_line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            return devices
        except Exception as e:
            print(f"[AppiumRunner] ADB check failed: {e}")
        return []

    def _select_adb_device(self, devices: List[str]) -> bool:
        """Select an online ADB device according to ADB_DEVICE."""
        if self.ADB_DEVICE in {"", "auto"} and devices:
            self.ADB_DEVICE = self._preferred_adb_device(devices)
        if self.ADB_DEVICE in devices:
            print(f"[AppiumRunner] ADB device available: {self.ADB_DEVICE}")
            return True
        if devices:
            print(
                f"[AppiumRunner] ADB devices found {devices}, "
                f"but configured device is {self.ADB_DEVICE}"
            )
        return False

    def _preferred_adb_device(self, devices: List[str]) -> str:
        preferred_targets = [
            target.strip()
            for target in os.getenv("ADB_TCP_DEVICE", "").split(",")
            if target.strip()
        ]
        for target in preferred_targets:
            if target in devices:
                return target
        for device in devices:
            if device.startswith("emulator-"):
                return device
        for device in devices:
            if device.startswith(("127.0.0.1:", "localhost:")):
                return device
        return devices[0]

    def _ensure_tcp_adb_connection(self) -> Optional[str]:
        """Connect to the emulator over TCP so adb forward works reliably on servers."""
        targets = [
            target.strip()
            for target in os.getenv("ADB_TCP_DEVICE", "127.0.0.1:5555").split(",")
            if target.strip()
        ]
        for target in targets:
            try:
                subprocess.run(
                    ["adb", "connect", target],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                continue
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            devices = []
            for raw_line in result.stdout.splitlines()[1:]:
                parts = raw_line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            for target in targets:
                if target in devices:
                    return target
        except Exception:
            pass
        return None

    def _start_emulator(self) -> bool:
        """Start the configured headless Android emulator if no device is online."""
        emulator_bin = shutil.which("emulator")
        if not emulator_bin:
            print("[AppiumRunner] emulator command not found in PATH")
            return False

        try:
            listed = subprocess.run(
                ["emulator", "-list-avds"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            avds = {line.strip() for line in listed.stdout.splitlines() if line.strip()}
            if self.avd_name not in avds:
                print(f"[AppiumRunner] AVD not found: {self.avd_name}; available={sorted(avds)}")
                return False
        except Exception as exc:
            print(f"[AppiumRunner] Failed to list AVDs: {exc}")
            return False

        log_dir = self.results_dir / "_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"emulator_{self.avd_name}.log"
        self._emulator_log_handle = open(log_path, "ab", buffering=0)

        cmd = [
            "emulator",
            "-avd", self.avd_name,
            "-no-window",
            "-no-audio",
            "-no-boot-anim",
            "-no-snapshot",
            "-no-snapshot-save",
            "-gpu", os.getenv("BM_EMULATOR_GPU", "swiftshader_indirect"),
        ]
        extra_args = os.getenv("BM_EMULATOR_EXTRA_ARGS", "").strip()
        if extra_args:
            cmd.extend(shlex.split(extra_args))

        print(f"[AppiumRunner] Starting emulator {self.avd_name}; log={log_path}")
        try:
            self._emulator_process = subprocess.Popen(
                cmd,
                stdout=self._emulator_log_handle,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
        except Exception as exc:
            print(f"[AppiumRunner] Failed to start emulator: {exc}")
            return False

        return self._wait_for_emulator_boot()

    def _wait_for_emulator_boot(self) -> bool:
        deadline = time.time() + self.EMULATOR_BOOT_TIMEOUT
        while time.time() < deadline:
            self._ensure_tcp_adb_connection()
            devices = [
                d for d in self._list_adb_devices(start_server=False)
                if d.startswith(("emulator-", "127.0.0.1:", "localhost:"))
            ]
            for device in devices:
                try:
                    boot = subprocess.run(
                        ["adb", "-s", device, "shell", "getprop", "sys.boot_completed"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if boot.stdout.strip() == "1":
                        self._prepare_device(device)
                        self._ensure_tcp_adb_connection()
                        current_devices = self._list_adb_devices(start_server=False)
                        self.ADB_DEVICE = (
                            self._preferred_adb_device(current_devices)
                            if current_devices
                            else device
                        )
                        print(f"[AppiumRunner] Emulator boot completed: {self.ADB_DEVICE}")
                        return True
                except Exception:
                    pass
            time.sleep(5)

        print(f"[AppiumRunner] Emulator boot timeout after {self.EMULATOR_BOOT_TIMEOUT}s")
        return False

    def _prepare_device(self, device: str) -> None:
        """Make the emulator less flaky for UI automation."""
        commands = [
            ["adb", "-s", device, "shell", "settings", "put", "global", "window_animation_scale", "0"],
            ["adb", "-s", device, "shell", "settings", "put", "global", "transition_animation_scale", "0"],
            ["adb", "-s", device, "shell", "settings", "put", "global", "animator_duration_scale", "0"],
            ["adb", "-s", device, "shell", "input", "keyevent", "82"],
        ]
        for cmd in commands:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception:
                pass

    def _is_appium_running(self) -> bool:
        """检查 Appium 服务器是否在运行（兼容 Appium 1/2 端点）。"""
        try:
            from urllib.request import urlopen

            status_urls = [
                f"http://{self.APPIUM_HOST}:{self.APPIUM_PORT}/status",        # Appium 2 默认
                f"http://{self.APPIUM_HOST}:{self.APPIUM_PORT}/wd/hub/status", # Appium 1 / 兼容模式
            ]
            for url in status_urls:
                try:
                    response = urlopen(url, timeout=2)
                    if response.status == 200:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _start_appium_server(self) -> bool:
        """启动 Appium 服务器（本地）。"""
        try:
            log_dir = self.results_dir / "_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "appium_server.log"
            self._appium_log_handle = open(log_path, "ab", buffering=0)
            cmd = [
                "appium",
                "server",
                "--address", self.APPIUM_HOST,
                "--port", str(self.APPIUM_PORT),
                "--allow-insecure", "*:adb_screen_recording",
            ]
            self._appium_process = subprocess.Popen(
                cmd,
                stdout=self._appium_log_handle,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
            print(
                f"[AppiumRunner] Appium started on {self.APPIUM_HOST}:{self.APPIUM_PORT}; "
                f"log={log_path}"
            )
            time.sleep(3)  # 等待服务启动
            return self._is_appium_running()
        except Exception as e:
            print(f"[AppiumRunner] Failed to start Appium: {e}")
            return False

    def _inspect_apk_identity(self, apk_path: str) -> tuple[Optional[str], Optional[str]]:
        """Read the actual package and launchable activity from the built APK."""
        package_name = None
        app_activity = None
        sdk_root = os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT")
        aapt = shutil.which("aapt")
        if not aapt and sdk_root:
            build_tools = Path(sdk_root) / "build-tools"
            candidates = sorted(build_tools.glob("*/aapt")) if build_tools.exists() else []
            if candidates:
                aapt = str(candidates[-1])
        if aapt:
            try:
                result = subprocess.run(
                    [aapt, "dump", "badging", apk_path],
                    capture_output=True, text=True, timeout=30,
                )
                package_match = re.search(r"^package: name='([^']+)'", result.stdout, re.MULTILINE)
                launch_match = re.search(r"^launchable-activity: name='([^']+)'", result.stdout, re.MULTILINE)
                if package_match:
                    package_name = package_match.group(1)
                if launch_match:
                    app_activity = launch_match.group(1)
            except Exception as exc:
                print(f"[AppiumRunner] WARN: failed to inspect APK with aapt: {exc}")

        # Some flavor manifests expose launcher aliases that aapt badging does
        # not report. Install once, then ask Android's package manager for the
        # authoritative resolved launcher component.
        if package_name and not app_activity:
            try:
                install = subprocess.run(
                    ["adb", "-s", self.ADB_DEVICE, "install", "-r", "-g", apk_path],
                    capture_output=True, text=True, timeout=180,
                )
                if install.returncode == 0:
                    resolved = subprocess.run(
                        ["adb", "-s", self.ADB_DEVICE, "shell", "cmd", "package",
                         "resolve-activity", "--brief", package_name],
                        capture_output=True, text=True, timeout=30,
                    )
                    components = [line.strip() for line in resolved.stdout.splitlines() if "/" in line]
                    if components:
                        candidate = components[-1].split("/", 1)[1]
                        if "ResolverActivity" not in candidate:
                            app_activity = candidate
                    if not app_activity:
                        queried = subprocess.run(
                            ["adb", "-s", self.ADB_DEVICE, "shell", "cmd", "package",
                             "query-activities", "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", package_name],
                            capture_output=True, text=True, timeout=30,
                        )
                        activity_match = re.search(r"^\s*name=([^\s]+)", queried.stdout, re.MULTILINE)
                        if activity_match:
                            app_activity = activity_match.group(1)
            except Exception as exc:
                print(f"[AppiumRunner] WARN: failed to resolve launcher activity: {exc}")
        return package_name, app_activity

    def _grant_special_storage_access(self) -> None:
        """Keep storage-heavy apps out of Android's external settings screen."""
        package_name = getattr(self, "_active_package", None)
        if not package_name:
            return
        try:
            result = subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "appops", "set",
                 package_name, "MANAGE_EXTERNAL_STORAGE", "allow"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                # Avoid driver.activate_app here: on some UiAutomator2 sessions
                # it blocks before the crawler can even start. The Appium
                # session has already launched the app; this best-effort ADB
                # nudge is only to keep storage permission flows from stealing
                # focus.
                subprocess.run(
                    [
                        "adb", "-s", self.ADB_DEVICE, "shell", "monkey",
                        "-p", package_name, "1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                time.sleep(1)
        except Exception as exc:
            print(f"[AppiumRunner] WARN: special storage permission not granted: {exc}")

    def _needs_special_storage_access(self, app_name: str, meta: dict) -> bool:
        prompt = normalize_text(str(meta.get("prompt", "")))
        storage_apps = {
            "FossifyGallery",
            "FossifyPaint",
            "MaterialFiles",
            "NextCloud",
        }
        storage_terms = (
            "photo", "photos", "image", "picture", "file", "folder", "storage",
            "media", "gallery", "upload", "download", "照片", "图片", "文件", "上传",
        )
        return app_name in storage_apps or any(term in prompt for term in storage_terms)

    def _create_driver(self, apk_path: str, app_name: str, task_id: str = None) -> webdriver.Remote:
        """
        创建 Appium WebDriver 实例。

        Args:
            apk_path:  APK 文件路径
            app_name:  应用名称
            task_id:   任务 ID（可选，用于读取 meta.json）

        Returns:
            Appium WebDriver 实例
        """
        # 从 meta.json 中读取包名和入口 Activity
        package_name = None
        app_activity = None
        if task_id:
            meta_path = self.data_dir / app_name / task_id / "meta.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        package_name = meta.get("app_package")
                        app_activity = meta.get("app_activity") or meta.get("target_activity")
                except Exception as e:
                    print(f"[AppiumRunner] Warning: Failed to read meta.json: {e}")

        apk_package, apk_activity = self._inspect_apk_identity(apk_path)
        if apk_package:
            package_name = apk_package
        if apk_activity:
            app_activity = apk_activity

        # 备用包名映射（如果 meta.json 和 APK 均不可用）
        if not package_name:
            package_map = {
                "app_newsreader": "livio.rssreader",
                "app_foodyou": "com.example.foodyou",
                "app_todoagenda": "com.example.todoagenda",
            }
            package_name = package_map.get(app_name, f"com.example.{app_name}")
        if not app_activity:
            app_activity = f"{package_name}.MainActivity"

        # Every benchmark task must start from a deterministic application
        # state. Reusing the same package across golden/model runs otherwise
        # restores the previous Activity and preferences, making crawler paths
        # depend on run order.
        self._clear_package_state(package_name)

        self._active_package = package_name
        self._target_package_name = package_name
        self._cleanup_stale_uiautomator2_state()
        
        print(f"[AppiumRunner] Package name: {package_name}")
        print(f"[AppiumRunner] App activity: {app_activity}")
        adb_server_port = os.getenv("ADB_SERVER_PORT")

        # 构建 AppiumOptions 或 desired capabilities（根据客户端可用性回退）
        if AppiumOptions is not None:
            options = AppiumOptions()
            options.set_capability("platformName", "Android")
            options.set_capability("appium:deviceName", self.ADB_DEVICE)
            options.set_capability("appium:udid", self.ADB_DEVICE)
            options.set_capability("appium:app", apk_path)
            options.set_capability("appium:appPackage", package_name)
            options.set_capability("appium:appActivity", app_activity)
            options.set_capability("appium:automationName", "UiAutomator2")
            options.set_capability("appium:autoGrantPermissions", True)
            options.set_capability("appium:noReset", False)
            options.set_capability("appium:fullReset", False)
            # Golden/model APKs intentionally share package name and version.
            # Without this Appium reuses the previous task's installed binary.
            options.set_capability("appium:enforceAppInstall", True)
            options.set_capability("appium:newCommandTimeout", 300)
            options.set_capability("appium:adbExecTimeout", int(os.getenv("ADB_EXEC_TIMEOUT", "120000")))
            options.set_capability(
                "appium:uiautomator2ServerLaunchTimeout",
                int(os.getenv("UIAUTOMATOR2_SERVER_LAUNCH_TIMEOUT", "120000")),
            )
            options.set_capability(
                "appium:uiautomator2ServerInstallTimeout",
                int(os.getenv("UIAUTOMATOR2_SERVER_INSTALL_TIMEOUT", "120000")),
            )
            options.set_capability("appium:disableWindowAnimation", True)
            options.set_capability("appium:systemPort", int(os.getenv("APPIUM_SYSTEM_PORT", "8200")))
            if adb_server_port:
                options.set_capability("appium:adbPort", int(adb_server_port))
            use_options = True
        else:
            # 回退到旧式 desiredCapabilities 字典
            caps = {
                "platformName": "Android",
                "deviceName": self.ADB_DEVICE,
                "app": apk_path,
                "appPackage": package_name,
                "appActivity": app_activity,
                "automationName": "UiAutomator2",
                "autoGrantPermissions": True,
                "noReset": False,
                "fullReset": False,
                "enforceAppInstall": True,
                "newCommandTimeout": 300,
                "adbExecTimeout": int(os.getenv("ADB_EXEC_TIMEOUT", "120000")),
                "uiautomator2ServerLaunchTimeout": int(
                    os.getenv("UIAUTOMATOR2_SERVER_LAUNCH_TIMEOUT", "120000")
                ),
                "uiautomator2ServerInstallTimeout": int(
                    os.getenv("UIAUTOMATOR2_SERVER_INSTALL_TIMEOUT", "120000")
                ),
                "disableWindowAnimation": True,
                "systemPort": int(os.getenv("APPIUM_SYSTEM_PORT", "8200")),
            }
            if adb_server_port:
                caps["adbPort"] = int(adb_server_port)
            use_options = False

        # 连接到 Appium（兼容 Appium 1/2）
        base_url = f"http://{self.APPIUM_HOST}:{self.APPIUM_PORT}"
        candidate_urls = [base_url]
        if self._status_endpoint_ok(f"{base_url}/wd/hub/status"):
            candidate_urls.append(f"{base_url}/wd/hub")
        errors = []
        for appium_url in candidate_urls:
            try:
                if AppiumOptions is not None and use_options:
                    driver = webdriver.Remote(appium_url, options=options)
                else:
                    driver = webdriver.Remote(appium_url, desired_capabilities=caps)
                self._set_driver_command_timeout(driver)
                session_id = getattr(driver, "session_id", None)
                if session_id:
                    self._appium_session_url = f"{appium_url.rstrip('/')}/session/{session_id}"
                driver.implicitly_wait(10)
                print(f"[AppiumRunner] Connected to Appium endpoint: {appium_url}")
                return driver
            except Exception as exc:
                errors.append(f"{appium_url}: {type(exc).__name__}: {exc}")

        raise RuntimeError(f"Failed to connect to Appium endpoints: {' || '.join(errors)}")

    def _set_driver_command_timeout(self, driver) -> None:
        """
        Bound individual Appium HTTP commands such as page_source/screenshot.

        Some apps (notably WebView-heavy settings screens) can make UiAutomator2
        spend minutes dumping the accessibility tree. The experiment-level
        timeout only wraps the crawler loop; it cannot fire while Selenium is
        blocked inside one HTTP request. Setting the command executor timeout
        turns those stalls into ordinary crawler failures/debug artifacts.
        """
        timeout = int(os.getenv("APPIUM_COMMAND_TIMEOUT", "25"))
        executor = getattr(driver, "command_executor", None)
        setter = getattr(executor, "set_timeout", None)
        if callable(setter):
            try:
                setter(timeout)
                return
            except Exception as exc:
                print(f"[AppiumRunner] WARN: failed to set command timeout: {exc}")
        remote_connection = getattr(executor, "_conn", None)
        if remote_connection is not None and hasattr(remote_connection, "timeout"):
            try:
                remote_connection.timeout = timeout
            except Exception:
                pass

    def _clear_package_state(self, package_name: str) -> bool:
        """Clear persisted Activity/preferences before each independent task."""
        if not package_name:
            return False
        try:
            result = subprocess.run(
                ["adb", "-s", self.ADB_DEVICE, "shell", "pm", "clear", package_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # A package may not be installed yet; Appium will install it fresh.
            return result.returncode == 0 and "success" in result.stdout.lower()
        except Exception as exc:
            print(f"[AppiumRunner] WARN: failed to clear {package_name}: {exc}")
            return False

    def _load_meta(self, app_name: str, task_id: str) -> dict:
        meta_path = self.data_dir / app_name / task_id / "meta.json"
        if not meta_path.exists():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[AppiumRunner] Warning: Failed to read meta.json: {e}")
            return {}

    def _crawl_to_target_page(self, level2_spec: dict, capture_target, timeout: int) -> dict:
        """
        自动探索 App，只保存最终命中的目标页面 XML。

        沿途 XML 只在内存中用于导航，不作为 Level 2 评分输入。
        """
        t0 = time.time()
        screenshots: list[str] = []
        visited: set[str] = set()
        repeat_counts: dict[str, int] = {}
        tried: set[str] = set()
        steps = 0
        max_steps = int(os.getenv("UI_CRAWLER_MAX_STEPS", "48"))
        dead_end_scrolls = int(os.getenv("UI_CRAWLER_DEAD_END_SCROLLS", "5"))
        scroll_streak = 0
        prefer_scroll_up_steps = 0
        log_lines: list[str] = []
        auth_gate: Optional[dict] = None
        embedded_preconditions_tried: set[str] = set()

        while time.time() - t0 < timeout and steps < max_steps:
            steps += 1
            time.sleep(1)

            if capture_target(f"crawler:step{steps}"):
                return {
                    "success": True,
                    "screenshots": screenshots,
                    "log": f"Target page found by crawler in {steps} steps.",
                    "steps": log_lines,
                }

            try:
                xml_text = self._get_page_source()
            except Exception as exc:
                return {"success": False, "screenshots": screenshots, "log": f"crawler page_source failed: {exc}"}

            current_package = self._safe_current_package()
            if self._is_outside_target_app(current_package):
                external_precondition = self._find_external_precondition_candidate(
                    xml_text,
                    current_package,
                    tried,
                )
                if external_precondition and self._tap_bounds(external_precondition["bounds"]):
                    tried.add(external_precondition["key"])
                    log_lines.append(
                        f"step {steps}: external precondition {current_package} "
                        f"{external_precondition.get('label', '')[:80]}"
                    )
                    time.sleep(1)
                    continue
                popup = self._find_popup_candidate(xml_text, level2_spec, tried)
                external_candidate = popup
                if "permissioncontroller" in normalize_text(current_package):
                    external_candidate = self._find_external_flow_candidate(
                        xml_text,
                        current_package,
                        tried,
                    ) or popup
                if external_candidate and self._tap_bounds(external_candidate["bounds"]):
                    tried.add(external_candidate["key"])
                    log_lines.append(
                        f"step {steps}: dismiss external {current_package} "
                        f"{external_candidate.get('label', '')[:80]}"
                    )
                    continue
                if self._return_to_target_app():
                    log_lines.append(f"step {steps}: return to target app from {current_package}")
                    continue
                log_lines.append(f"step {steps}: outside target app {current_package}")
                break

            normalized_xml = normalize_text(xml_text)
            todo_widget_gate = all(
                term in normalized_xml for term in ("please add", "todo agenda", "widget")
            )
            if todo_widget_gate and "todo_widget" not in embedded_preconditions_tried:
                embedded_preconditions_tried.add("todo_widget")
                if self._open_todoagenda_widget_configuration():
                    log_lines.append(f"step {steps}: open TodoAgenda widget configuration")
                    continue

            precondition_action = self._find_precondition_action_candidate(
                xml_text,
                level2_spec,
                tried,
            )
            if precondition_action and self._tap_candidate(precondition_action):
                tried.add(precondition_action["key"])
                log_lines.append(
                    f"step {steps}: precondition action "
                    f"{precondition_action.get('label', '')[:80]} "
                    f"{precondition_action.get('bounds', '')}"
                )
                time.sleep(0.4)
                if capture_target(f"precondition:step{steps}"):
                    return {
                        "success": True,
                        "screenshots": screenshots,
                        "log": f"Target page found after precondition action in {steps} steps.",
                        "steps": log_lines,
                    }
                time.sleep(0.6)
                continue

            signature = self._xml_signature(xml_text)
            current_activity = self._safe_current_activity()
            if auth_gate is None:
                auth_gate = self._detect_auth_gate(current_activity, xml_text)
            if signature in visited:
                repeat_counts[signature] = repeat_counts.get(signature, 0) + 1
            visited.add(signature)

            text_input_action = self._perform_precondition_text_input(xml_text, tried)
            if text_input_action:
                log_lines.append(f"step {steps}: {text_input_action}")
                time.sleep(0.4)
                continue

            dialog_progress = self._find_dialog_progress_candidate(xml_text, level2_spec, tried)
            popup = self._find_popup_candidate(xml_text, level2_spec, tried)
            candidate = dialog_progress or popup or self._choose_click_candidate(xml_text, level2_spec, tried)
            if not candidate:
                if self._try_scroll_target_picker(xml_text, level2_spec):
                    scroll_streak += 1
                    log_lines.append(f"step {steps}: scroll target picker")
                    continue
                # A scroll gesture can report success even when a WebView is already
                # at its edge. Backtrack after repeated/stagnant pages so the crawler
                # explores the next menu branch instead of scrolling until timeout.
                should_backtrack = (
                    scroll_streak >= dead_end_scrolls
                    or repeat_counts.get(signature, 0) >= 2
                )
                if should_backtrack:
                    if self._go_back():
                        log_lines.append(f"step {steps}: back from dead end")
                        scroll_streak = 0
                        prefer_scroll_up_steps = 3
                        continue
                if prefer_scroll_up_steps > 0 and self._try_scroll(direction="up"):
                    prefer_scroll_up_steps -= 1
                    scroll_streak += 1
                    log_lines.append(f"step {steps}: scroll up")
                    continue
                if self._try_scroll(direction="down"):
                    scroll_streak += 1
                    log_lines.append(f"step {steps}: scroll")
                    continue
                if self._has_back_candidate(xml_text) and self._go_back():
                    log_lines.append(f"step {steps}: back after no scroll")
                    scroll_streak = 0
                    prefer_scroll_up_steps = 3
                    continue
                log_lines.append(f"step {steps}: no clickable candidate")
                break

            tried.add(candidate["key"])
            if self._tap_candidate(candidate):
                scroll_streak = 0
                label = candidate.get("label", "")
                log_lines.append(f"step {steps}: tap {label[:80]} {candidate.get('bounds', '')}")
                continue

            log_lines.append(f"step {steps}: tap failed {candidate.get('label', '')[:80]}")
            break

        return {
            "success": False,
            "screenshots": screenshots,
            "log": "Target page not found by crawler. " + " | ".join(log_lines),
            "steps": log_lines,
            "auth_gate": auth_gate,
        }

    def _capture_crawler_debug(
        self,
        ui_dir: Path,
        screenshot_dir: Path,
        level2_spec: dict,
        steps: list[str],
        auth_gate: Optional[dict] = None,
    ) -> dict:
        """Save only the final failed screen for debugging; scoring ignores it."""
        debug = {
            "current_package": self._safe_current_package(),
            "current_activity": self._safe_current_activity(),
            "last_ui_dom_tree_path": None,
            "last_screenshot": None,
            "last_page_match": None,
            "steps": steps,
            "auth_gate": auth_gate,
        }
        try:
            xml_text = self._get_page_source()
            xml_path = ui_dir / "crawler_last.xml"
            xml_path.write_text(xml_text, encoding="utf-8")
            debug["last_ui_dom_tree_path"] = str(xml_path)
            debug["last_page_match"] = match_target_xml(xml_text, level2_spec)
        except Exception as exc:
            debug["xml_error"] = f"{type(exc).__name__}: {exc}"

        try:
            screenshot_path = screenshot_dir / "crawler_last.png"
            self._save_screenshot(str(screenshot_path))
            debug["last_screenshot"] = str(screenshot_path)
        except Exception as exc:
            debug["screenshot_error"] = f"{type(exc).__name__}: {exc}"

        try:
            (ui_dir / "crawler_last.json").write_text(
                json.dumps(debug, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return debug

    def _detect_auth_gate(self, current_activity: Optional[str], xml_text: str) -> Optional[dict]:
        activity = current_activity or ""
        lower_activity = normalize_text(activity)
        if not any(term in lower_activity for term in ("login", "signin", "sign in", "auth")):
            return None

        lowered = normalize_text(xml_text)
        login_terms = (
            "login", "log in", "sign in", "signin", "password",
            "oauth", "authorization", "browser login", "which instance",
        )
        if not any(term in lowered for term in login_terms):
            return None
        return {"activity": activity, "screen": "login_gate"}

    def _capture_debug_step(self, ui_dir: Path, source_label: str, xml_text: str) -> None:
        if os.getenv("UI_CRAWLER_DEBUG_STEPS", "0") != "1":
            return
        safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", source_label)
        try:
            (ui_dir / f"{safe_label}.xml").write_text(xml_text, encoding="utf-8")
        except Exception:
            pass

    def _find_popup_candidate(
        self,
        xml_text: str,
        level2_spec: dict,
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        popup_terms = [normalize_text(x) for x in level2_spec.get("popup_terms", [])]
        candidates = self._click_candidates_from_xml(xml_text)
        scored: list[tuple[int, int, dict]] = []
        for c in candidates:
            if tried is not None and c["key"] in tried:
                continue
            label = normalize_text(c.get("label", ""))
            matched = [
                term for term in popup_terms
                if term and self._label_contains_term(label, term)
            ]
            if not matched:
                continue
            priority = 10
            if any(term in matched for term in (
                "skip", "i've been here before", "not now", "no thanks", "maybe later",
                "start browsing", "finish",
                "跳过", "稍后",
            )):
                priority = 30
            elif any(term in matched for term in (
                "allow", "agree", "continue", "ok", "okay", "允许", "同意", "继续", "确定", "好的",
            )):
                # Permission/onboarding dialogs often close or even finish the
                # app when Cancel wins. Positive actions advance to app content.
                priority = 25
            elif any(term in matched for term in ("cancel", "close", "取消", "关闭")):
                priority = 10
            node_class = normalize_text(c.get("class", ""))
            resource_id = normalize_text(c.get("resource_id", ""))
            if node_class.endswith("button"):
                priority += 20
            elif node_class.endswith("textview"):
                priority -= 10
            if "skiponboardingbutton" in resource_id:
                priority += 30
            scored.append((priority, c.get("area", 0), c))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    def _find_dialog_progress_candidate(
        self,
        xml_text: str,
        level2_spec: dict,
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        """Advance a target-related editor before dismissing it with Cancel."""
        candidates = self._click_candidates_from_xml(xml_text)
        if not candidates:
            return None
        visible = normalize_text(xml_text)
        context_terms = [
            normalize_text(term)
            for term in level2_spec.get("navigation_terms", [])
            if normalize_text(term) not in {"settings", "setting", "menu", "more", "options"}
        ]
        if not any(term and self._label_contains_term(visible, term) for term in context_terms):
            return None
        for candidate in candidates:
            if tried is not None and candidate["key"] in tried:
                continue
            label = normalize_text(candidate.get("label", ""))
            if label == "select" or label.startswith("select ") or label.endswith(" select"):
                return candidate
        return None

    def _find_precondition_action_candidate(
        self,
        xml_text: str,
        level2_spec: dict,
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        preconditions = set(getattr(self, "_active_preconditions", set()) or set())
        if not preconditions:
            return None

        visible = normalize_text(xml_text)
        candidates = self._click_candidates_from_xml(xml_text)
        if not candidates:
            return None

        # Confirm dialogs for destructive/import actions are part of building
        # the requested state. Prefer them before looking for another menu item.
        confirm_terms = (
            "delete", "move to trash", "trash", "remove", "replace", "overwrite",
            "rename", "import", "copy", "move", "ok", "yes", "allow", "confirm",
            "删除", "移至回收站", "替换", "覆盖", "重命名", "导入", "复制", "确定",
        )
        destructive_dialog = any(term in visible for term in (
            "are you sure", "move to trash", "replace", "overwrite",
            "file already exists", "duplicate", "conflict", "确定", "删除", "替换",
        ))
        if destructive_dialog:
            candidate = self._best_labeled_candidate(candidates, confirm_terms, tried)
            if candidate:
                return candidate

        if preconditions & {"seed_media_then_delete", "seed_content_then_delete"}:
            # Once the snackbar/action is visible, capture_target will score it
            # on the next loop; do not keep deleting.
            if any(term in visible for term in ("undo", "restore", "restored", "deleted", "moved to trash", "撤销", "恢复")):
                return None
            candidate = self._best_labeled_candidate(
                candidates,
                ("delete", "trash", "remove", "move to trash", "bin", "删除", "移除", "回收站"),
                tried,
            )
            if candidate:
                return candidate
            # Overflow menus often hide delete behind a content-desc only icon.
            if any(term in visible for term in (
                "photo", "image", "picture", "codex", "gallery",
                "message", "messages", "conversation", "sms",
            )):
                candidate = self._best_labeled_candidate(
                    candidates,
                    ("more options", "overflow", "menu", "select", "more", "更多", "菜单"),
                    tried,
                )
                if candidate:
                    return candidate

        if "seed_media_then_open_editor" in preconditions:
            candidate = self._best_labeled_candidate(
                candidates,
                ("edit", "editor", "crop", "tools", "draw", "remove border", "emoji", "编辑", "裁剪", "工具"),
                tried,
            )
            if candidate:
                return candidate

        if preconditions & {
            "seed_storage_and_open_picker",
            "open_document_tree_with_seed_directory",
            "create_duplicate_filename_conflict",
        }:
            if "create_duplicate_filename_conflict" in preconditions:
                duplicate_context = any(term in visible for term in (
                    "file already exists", "overwrite", "replace", "rename",
                    "copy to", "move to", "duplicate_source", "duplicate_target",
                    "more options", "overflow",
                ))
                if not duplicate_context:
                    return None
                candidate = self._best_labeled_candidate(
                    candidates,
                    (
                        "rename", "copy to", "move to", "copy", "move",
                        "more options", "overflow", "menu", "replace",
                        "overwrite", "overwrite original", "ok", "done",
                    ),
                    tried,
                )
                if candidate:
                    return candidate
                return None

            if "open_document_tree_with_seed_directory" in preconditions:
                tree_context = any(term in visible for term in (
                    "included folders", "manage included folders", "add folder",
                    "select folder", "choose folder", "root directory",
                    "codexseed", "codex seed", "import_root", "internal storage",
                    "download", "downloads",
                ))
                if not tree_context:
                    return None
                detours = ("create new folder", "show all folders content", "search folders")
                filtered_candidates = [
                    candidate for candidate in candidates
                    if not any(term in normalize_text(candidate.get("label", "")) for term in detours)
                ]
                candidate = self._best_labeled_candidate(
                    filtered_candidates,
                    (
                        "manage included folders", "included folders", "add folder",
                        "codexseed", "codex seed", "import_root", "download", "downloads",
                        "use this folder", "select folder", "select", "open", "choose", "ok",
                        "internal storage", "root",
                    ),
                    tried,
                )
                if candidate:
                    return candidate
                return None

            context_terms = [
                term for term in (
                    "background image", "select background image", "change background image",
                    "custom app background image", "overall background image",
                    "batch import", "duplicate", "file already exists", "conflict",
                    "replace", "rename", "folder", "directory",
                )
                if term in visible
            ]
            if not context_terms:
                return None
            candidate = self._best_labeled_candidate(
                candidates,
                (
                    "upload", "choose", "select", "pick", "import", "add file",
                    "add image", "background image", "copy", "move", "paste",
                    "replace", "rename", "folder", "directory", "browse",
                    "上传", "选择", "导入", "添加", "复制", "移动", "粘贴", "替换", "重命名",
                ),
                tried,
            )
            if candidate:
                return candidate

        return None

    def _perform_precondition_text_input(
        self,
        xml_text: str,
        tried: Optional[set[str]] = None,
    ) -> Optional[str]:
        preconditions = set(getattr(self, "_active_preconditions", set()) or set())
        if "create_duplicate_filename_conflict" not in preconditions:
            return None
        visible = normalize_text(xml_text)
        if "rename" not in visible:
            return None
        action_key = "precondition_text:duplicate_target.png"
        if tried is not None and action_key in tried:
            return None
        if AppiumBy is None or self.driver is None:
            return None
        try:
            fields = self.driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.EditText")
        except Exception:
            return None
        for field in fields:
            try:
                if not field.is_displayed() or not field.is_enabled():
                    continue
                field.click()
                field.clear()
                field.send_keys("duplicate_target.png")
                if tried is not None:
                    tried.add(action_key)
                return "precondition text input duplicate_target.png"
            except Exception:
                continue
        return None

    def _find_external_precondition_candidate(
        self,
        xml_text: str,
        current_package: str,
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        preconditions = set(getattr(self, "_active_preconditions", set()) or set())
        if not preconditions:
            return None
        package = normalize_text(current_package)
        if not any(term in package for term in (
            "documentsui", "photopicker", "packageinstaller", "permissioncontroller", "externalstorage",
        )):
            return None

        candidates = self._click_candidates_from_xml(xml_text) + self._text_candidates_from_xml(xml_text)
        if not candidates:
            return None

        visible = normalize_text(xml_text)
        if "permissioncontroller" in package:
            if not any(term in visible for term in (
                "allow", "while using", "only this time", "select photos", "choose photos",
                "允许", "选择照片",
            )):
                return None

        if "seed_storage_and_open_picker" in preconditions:
            candidate = self._best_labeled_candidate(candidates, ("example.png", "example"), tried)
            if candidate and not self._is_external_settings_detour(candidate):
                return candidate

        terms = [
            "example.png", "example", "codexseed", "codex seed", "codex_seed", "codex",
            "pictures", "photos", "images", "download", "downloads",
            "import_root", "root_file", "nested_file", "duplicate",
            "codex_seed_upload", "codex_seed_image", "codex_seed_delete",
            "use this folder", "select", "open", "done", "allow", "choose",
            "使用此文件夹", "选择", "打开", "完成", "允许",
        ]
        if "open_document_tree_with_seed_directory" in preconditions:
            terms = [
                "codexseed", "codex seed", "download", "downloads", "import_root",
                "use this folder", "select", "open", "allow", "使用此文件夹", "选择", "允许",
            ]
        elif "create_duplicate_filename_conflict" in preconditions:
            terms = [
                "codexseed", "codex seed", "download", "downloads", "source", "target",
                "duplicate", "duplicate.txt", "replace", "overwrite", "rename",
                "select", "open", "done", "选择", "替换", "覆盖", "重命名",
            ]
        candidate = self._best_labeled_candidate(candidates, tuple(terms), tried)
        if candidate and self._is_external_settings_detour(candidate):
            return None
        return candidate

    def _is_external_settings_detour(self, candidate: dict) -> bool:
        label = normalize_text(candidate.get("label", ""))
        resource_id = normalize_text(candidate.get("resource_id", ""))
        detour_terms = (
            "opening links", "app info", "default apps", "android system intelligence",
            "settings", "manage apps", "permission manager", "browser app",
            "caller id", "spam app", "apps that allow",
        )
        if any(term in label for term in detour_terms):
            return True
        return "settings" in resource_id and not any(
            term in label for term in ("allow", "select", "choose", "use this folder", "done", "允许", "选择")
        )

    def _best_labeled_candidate(
        self,
        candidates: list[dict],
        terms: tuple[str, ...],
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        normalized_terms = [normalize_text(term) for term in terms]
        scored: list[tuple[int, int, dict]] = []
        for candidate in candidates:
            if tried is not None and candidate.get("key") in tried:
                continue
            label = normalize_text(candidate.get("label", ""))
            if not label:
                continue
            score = 0
            for term in normalized_terms:
                if term and (self._label_contains_term(label, term) or term in label):
                    score += 20 + len(term)
            if score <= 0:
                continue
            node_class = normalize_text(candidate.get("class", ""))
            resource_id = normalize_text(candidate.get("resource_id", ""))
            if candidate.get("clickable") == "true":
                score += 5
            if node_class.endswith("button"):
                score += 5
            if any(term in resource_id for term in ("delete", "trash", "select", "confirm", "picker")):
                score += 8
            area = candidate.get("area", 0)
            if area >= 500_000:
                score -= 8
            if label in {"back", "navigate up", "返回"}:
                score -= 30
            scored.append((score, -area, candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    def _find_external_flow_candidate(
        self,
        xml_text: str,
        current_package: str,
        tried: Optional[set[str]] = None,
    ) -> Optional[dict]:
        candidates = self._click_candidates_from_xml(xml_text)
        if not candidates:
            return None

        target_terms = self._target_app_terms()
        scored: list[tuple[int, int, dict]] = []
        for c in candidates:
            if tried is not None and c["key"] in tried:
                continue
            label = normalize_text(c.get("label", ""))
            if not label:
                continue

            if self._is_external_settings_detour(c):
                continue

            score = 0
            if any(term in label for term in ("cancel", "close", "don't ask again", "dont ask again", "取消", "关闭")):
                score -= 20
            if any(term in label for term in (
                "allow", "agree", "continue", "ok", "okay",
                "set as default", "while using", "only this time",
                "允许", "同意", "继续", "确定",
            )):
                score += 20
            if any(term and self._label_contains_term(label, term) for term in target_terms):
                score += 35
            resource_id = normalize_text(c.get("resource_id", ""))
            node_class = normalize_text(c.get("class", ""))
            if resource_id.endswith("button1"):
                score += 15
            if current_package.endswith("permissioncontroller") and node_class.endswith("button"):
                score += 5
            if label.startswith("android:id/") or label == resource_id:
                score -= 5

            if score <= 0:
                continue
            scored.append((score, c.get("area", 0), c))

        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    def _target_app_terms(self) -> list[str]:
        package_name = normalize_text(getattr(self, "_target_package_name", "") or "")
        if not package_name:
            return []
        terms: list[str] = []
        for part in re.split(r"[^a-z0-9]+", package_name):
            if len(part) < 4 or part in {"com", "org", "app"}:
                continue
            if part not in terms:
                terms.append(part)
        joined = " ".join(terms)
        underscored = "_".join(terms)
        for extra in (joined, underscored):
            if extra and extra not in terms:
                terms.append(extra)
        return terms

    @staticmethod
    def _label_contains_term(label: str, term: str) -> bool:
        """Match popup words without treating `ok` as a substring of Bookmarks."""
        if not label or not term:
            return False
        if re.search(r"[\u3400-\u9fff]", term):
            return term in label
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", label) is not None

    def _choose_click_candidate(self, xml_text: str, level2_spec: dict, tried: set[str]) -> Optional[dict]:
        candidates = self._click_candidates_from_xml(xml_text)
        current_package = normalize_text(self._safe_current_package() or "")
        if "fr.neamar.kiss" in current_package:
            candidates.extend(self._kiss_preference_text_candidates(xml_text))
        if not candidates:
            return None

        nav_terms = [normalize_text(x) for x in level2_spec.get("navigation_terms", [])]
        score_terms = [normalize_text(x) for x in level2_spec.get("score_terms", [])]
        theme_navigation = any(term in nav_terms for term in ("theme", "appearance", "color scheme"))

        scored = []
        for c in candidates:
            if c["key"] in tried:
                continue
            label = normalize_text(c.get("label", ""))
            score = 0
            for term in score_terms:
                if term and self._label_contains_term(label, term):
                    score += 8
            for term in nav_terms:
                if term and self._label_contains_term(label, term):
                    score += 4
            # Generic "menu" resource IDs otherwise dominate useful settings/
            # overflow entries (for example aiChatIconMenu in DuckDuckGo).
            if any(term in label for term in ("ai chat", "aichat", "clear data", "fireicon", "tabsmenu")):
                score -= 8
            if theme_navigation and any(term in label for term in ("appearance", "theme", "app theme", "colors")):
                score += 20
            if theme_navigation and re.search(r"(?<![a-z0-9])theme(?![a-z0-9])", label):
                score += 30
            if theme_navigation and (label == "theme" or label.startswith("theme ")):
                score += 35
            if theme_navigation and "themed icons" in label:
                score -= 35
            if any(term in label for term in ("settings", "preferences", "overflow", "more options")):
                score += 8
            if score <= 0:
                continue
            if c.get("clickable") == "true":
                score += 2
            area = c.get("area", 0)
            if 0 < area <= 40_000:
                score += 3
            elif area >= 350_000:
                score -= 5
            if label in {"back", "返回", "navigate up"}:
                score -= 10
            scored.append((score, area, c))

        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2]

    def _kiss_preference_text_candidates(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return []
        try:
            screen_width = int(root.attrib.get("width") or 1080)
        except Exception:
            screen_width = 1080
        candidates: list[dict] = []
        for node in root.iter():
            attrs = node.attrib
            text = attrs.get("text", "")
            if not text.strip():
                continue
            bounds = attrs.get("bounds", "")
            parsed = self._parse_bounds(bounds)
            if not parsed:
                continue
            x1, y1, x2, y2 = parsed
            row_bounds = f"[0,{max(0, y1 - 42)}][{screen_width},{min(1920, y2 + 51)}]"
            row_parsed = self._parse_bounds(row_bounds)
            candidates.append({
                "bounds": row_bounds,
                "clickable": "true",
                "class": attrs.get("class", ""),
                "resource_id": attrs.get("resource-id", ""),
                "label": text,
                "area": self._bounds_area(row_parsed) if row_parsed else 0,
                "key": f"kiss-pref-row:{normalize_text(text)}:{y1}",
            })
        return candidates

    def _click_candidates_from_xml(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return []

        candidates: list[dict] = []
        for node in root.iter():
            attrs = node.attrib
            bounds = attrs.get("bounds")
            parsed_bounds = self._parse_bounds(bounds)
            if not bounds or not parsed_bounds:
                continue

            clickable = attrs.get("clickable", "false").lower()
            focusable = attrs.get("focusable", "false").lower()
            enabled = attrs.get("enabled", "true").lower()
            displayed = attrs.get("displayed", "true").lower()
            if enabled == "false" or displayed == "false":
                continue

            label = self._label_from_node(node)
            if not label.strip():
                continue
            node_class = normalize_text(attrs.get("class", ""))
            actionable = clickable == "true"
            if not actionable and focusable == "true" and (
                node_class.endswith("listview")
                or node_class.endswith("recyclerview")
            ):
                # Overflow / popup menus often expose only a focusable list
                # container while the visible text sits on non-clickable child
                # rows. Tapping the list bounds still opens the single menu item.
                visible_labels = {
                    normalize_text(
                        child.attrib.get("text", "") or child.attrib.get("content-desc", "")
                    )
                    for child in node.iter()
                    if child is not node
                }
                visible_labels.discard("")
                # A full settings RecyclerView aggregates every descendant label;
                # tapping its center is arbitrary. Only promote compact popup
                # lists where the container represents one menu choice.
                actionable = len(visible_labels) <= 2
            if not actionable:
                continue

            candidates.append({
                "bounds": bounds,
                "clickable": clickable,
                "class": attrs.get("class", ""),
                "resource_id": attrs.get("resource-id", ""),
                "label": label,
                "area": self._bounds_area(parsed_bounds),
                # Prefer a stable view identity. Toggle/select rows often change
                # their visible value after a tap; using the whole label made the
                # crawler treat the same row as a fresh action and tap forever.
                "key": self._candidate_action_key(node, bounds, label),
            })
        return candidates

    def _candidate_action_key(self, node: ET.Element, bounds: str, label: str) -> str:
        resource_ids: list[str] = []
        for current in node.iter():
            resource_id = normalize_text(current.attrib.get("resource-id", ""))
            if resource_id and resource_id not in resource_ids:
                resource_ids.append(resource_id)
        if resource_ids:
            return "resource:" + "|".join(resource_ids[:3])

        semantic = normalize_text(label)
        semantic = re.sub(
            r"\b(?:true|false|enabled|disabled|on|off|select|selected|edit|checked|unchecked)\b",
            " ",
            semantic,
        )
        semantic = re.sub(r"\s+", " ", semantic).strip()
        return f"bounds:{bounds}:{semantic[:120]}"

    def _bounds_area(self, bounds: tuple[int, int, int, int]) -> int:
        x1, y1, x2, y2 = bounds
        return max(0, x2 - x1) * max(0, y2 - y1)

    def _label_from_node(self, node: ET.Element) -> str:
        """Compose often puts visible text on non-clickable children."""
        label_parts: list[str] = []
        seen: set[str] = set()
        for current in node.iter():
            for attr in ("text", "content-desc", "resource-id"):
                value = current.attrib.get(attr, "")
                if not value:
                    continue
                normalized = normalize_text(value)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                label_parts.append(value)
            if len(label_parts) >= 8:
                break
        return " ".join(label_parts)

    def _has_back_candidate(self, xml_text: str) -> bool:
        back_terms = {"back", "go back", "navigate up", "返回"}
        for candidate in self._click_candidates_from_xml(xml_text):
            label = normalize_text(candidate.get("label", ""))
            if label in back_terms or any(term in label for term in back_terms):
                return True
        return False

    def _go_back(self) -> bool:
        try:
            self.driver.back()
            time.sleep(1)
            return True
        except Exception:
            return False

    def _click_exact_text(self, text: str) -> bool:
        if not self.driver or AppiumBy is None:
            return False
        try:
            self._set_implicit_wait(1)
            selector = 'new UiSelector().text("' + text.replace('\\', '\\\\').replace('\"', '\\"') + '")'
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector).click()
            return True
        except Exception:
            return False
        finally:
            self._set_implicit_wait(10)

    def _tap_candidate(self, candidate: dict) -> bool:
        label = normalize_text(candidate.get("label", ""))
        if AppiumBy is not None:
            self._set_implicit_wait(0)
            try:
                for phrase in ("Go to settings", "Settings", "More options"):
                    if normalize_text(phrase) in label:
                        try:
                            self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, phrase).click()
                            return True
                        except Exception:
                            pass
            finally:
                self._set_implicit_wait(10)
        return self._tap_bounds(candidate.get("bounds", ""))

    def _long_press_bounds(self, bounds: str, duration_ms: int = 900) -> bool:
        parsed = self._parse_bounds(bounds)
        if not parsed:
            return False
        x1, y1, x2, y2 = parsed
        x = int((x1 + x2) / 2)
        y = int((y1 + y2) / 2)
        try:
            self.driver.execute_script("mobile: longClickGesture", {"x": x, "y": y, "duration": duration_ms})
            return True
        except Exception:
            try:
                action = TouchAction(self.driver)
                action.long_press(x=x, y=y, duration=duration_ms).release().perform()
                return True
            except Exception:
                return False

    def _tap_bounds(self, bounds: str) -> bool:
        parsed = self._parse_bounds(bounds)
        if not parsed:
            return False
        if By is not None:
            self._set_implicit_wait(0)
            try:
                for element in self.driver.find_elements(By.XPATH, f"//*[@bounds='{bounds}']"):
                    if str(element.get_attribute("clickable")).lower() == "true":
                        element.click()
                        return True
            except Exception:
                pass
            finally:
                self._set_implicit_wait(10)
        x1, y1, x2, y2 = parsed
        x = int((x1 + x2) / 2)
        y = int((y1 + y2) / 2)
        if self._bounds_area(parsed) <= 40_000 and self._tap_with_driver_tap(x, y):
            return True
        try:
            self.driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
            return True
        except Exception:
            if self._tap_with_driver_tap(x, y):
                return True
            print("[AppiumRunner] tap failed")
            return False

    def _tap_with_driver_tap(self, x: int, y: int) -> bool:
        try:
            self.driver.tap([(x, y)])
            return True
        except Exception:
            return False

    def _set_implicit_wait(self, seconds: int) -> None:
        try:
            self.driver.implicitly_wait(seconds)
        except Exception:
            pass

    def _try_scroll(self, direction: str = "down") -> bool:
        try:
            size = self.driver.get_window_size()
            width = int(size.get("width", 0))
            height = int(size.get("height", 0))
            if width <= 0 or height <= 0:
                return False
            start_y = int(height * (0.75 if direction == "down" else 0.25))
            end_y = int(height * (0.25 if direction == "down" else 0.75))
            try:
                adb_swipe = subprocess.run(
                    [
                        "adb", "-s", self.ADB_DEVICE, "shell", "input", "swipe",
                        str(width // 2), str(start_y), str(width // 2), str(end_y), "350",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if adb_swipe.returncode == 0:
                    return True
            except Exception:
                pass
            gesture_direction = "up" if direction == "down" else "down"
            self.driver.execute_script("mobile: swipeGesture", {
                "left": int(width * 0.1),
                "top": int(height * 0.2),
                "width": int(width * 0.8),
                "height": int(height * 0.6),
                "direction": gesture_direction,
                "percent": 0.7,
            })
            return True
        except Exception:
            return False

    def _try_scroll_target_picker(self, xml_text: str, level2_spec: dict) -> bool:
        """Scroll a lower-screen dropdown without dismissing its popup surface."""
        visible = normalize_text(xml_text)
        nav_terms = {normalize_text(term) for term in level2_spec.get("navigation_terms", [])}
        known_theme_options = ("system", "light", "transparent", "dark", "amoled dark")
        theme_picker = ("theme" in nav_terms or "theme" in visible) and sum(
            self._label_contains_term(visible, option)
            for option in known_theme_options
        ) >= 2
        if not theme_picker:
            return False

        target_blob = normalize_text(" ".join(
            str(term)
            for term in (level2_spec.get("target_phrases") or []) + (level2_spec.get("score_terms") or [])
        ))
        scroll_labels: list[str] = []
        for label in (
            "High Contrast", "High Contrast Mode",
            "Colorblind Mode", "Color Blind Mode",
            "Girly Skull", "Skull Girl", "Girl Skull", "少女骷髅",
        ):
            norm_label = normalize_text(label)
            if norm_label and norm_label in target_blob:
                scroll_labels.append(label)
        if AppiumBy is not None:
            for label in scroll_labels:
                escaped = label.replace('\\', '\\\\').replace('"', '\\"')
                try:
                    self.driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiScrollable(new UiSelector().resourceId("android:id/select_dialog_listview"))'
                        f'.setAsVerticalList().scrollTextIntoView("{escaped}")',
                    )
                    time.sleep(0.4)
                    return True
                except Exception:
                    pass

        try:
            size = self.driver.get_window_size()
            width = int(size.get("width", 0))
            height = int(size.get("height", 0))
            if width <= 0 or height <= 0:
                return False
        except Exception:
            return False

        scrolled = False
        try:
            self.driver.execute_script("mobile: swipeGesture", {
                "left": int(width * 0.12),
                "top": int(height * 0.28),
                "width": int(width * 0.76),
                "height": int(height * 0.48),
                # Appium's gesture direction is the finger movement. Swiping up
                # reveals options below the currently visible theme choices.
                "direction": "up",
                "percent": 0.9,
            })
            scrolled = True
        except Exception:
            pass
        try:
            adb_swipe = subprocess.run(
                [
                    "adb", "-s", self.ADB_DEVICE, "shell", "input", "swipe",
                    str(width // 2), str(int(height * 0.72)),
                    str(width // 2), str(int(height * 0.30)), "650",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            scrolled = scrolled or adb_swipe.returncode == 0
        except Exception:
            pass
        return scrolled

    def _reposition_target_if_edge_clipped(self, match: dict) -> bool:
        """Nudge a matched node away from screen edges before evidence capture."""
        try:
            size = self.driver.get_window_size()
            width = int(size.get("width", 0))
            height = int(size.get("height", 0))
            if width <= 0 or height <= 0:
                return False
            parsed_bounds = [
                self._parse_bounds(item.get("bounds", ""))
                for item in match.get("matched_nodes", [])
            ]
            parsed_bounds = [bounds for bounds in parsed_bounds if bounds]
            if not parsed_bounds:
                return False
            y1 = min(bounds[1] for bounds in parsed_bounds)
            y2 = max(bounds[3] for bounds in parsed_bounds)
            direction = None
            if y2 >= int(height * 0.88):
                direction = "down"
            elif y1 <= int(height * 0.12):
                direction = "up"
            if not direction:
                return False
            self.driver.execute_script("mobile: scrollGesture", {
                "left": int(width * 0.1),
                "top": int(height * 0.15),
                "width": int(width * 0.8),
                "height": int(height * 0.7),
                "direction": direction,
                "percent": 0.3,
            })
            return True
        except Exception:
            return False

    def _parse_bounds(self, bounds: str) -> Optional[tuple[int, int, int, int]]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
        if not m:
            return None
        return tuple(int(x) for x in m.groups())

    def _xml_signature(self, xml_text: str) -> str:
        return str(hash(xml_text[:5000]))

    def _safe_current_activity(self) -> Optional[str]:
        try:
            return self.driver.current_activity
        except Exception:
            return None

    def _safe_current_package(self) -> Optional[str]:
        try:
            return self.driver.current_package
        except Exception:
            return None

    def _is_outside_target_app(self, current_package: Optional[str]) -> bool:
        if not self._target_package_name or not current_package:
            return False
        return current_package != self._target_package_name

    def _return_to_target_app(self) -> bool:
        if not self.driver or not self._target_package_name:
            return False
        try:
            self.driver.back()
        except Exception:
            pass
        try:
            self.driver.activate_app(self._target_package_name)
            time.sleep(1)
            return True
        except Exception as exc:
            print(f"[AppiumRunner] failed to activate target app: {exc}")
            return False

    def _status_endpoint_ok(self, url: str) -> bool:
        try:
            from urllib.request import urlopen

            response = urlopen(url, timeout=2)
            return response.status == 200
        except Exception:
            return False

    def __del__(self):
        """清理：关闭驱动和 Appium 进程。"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        if self._appium_process:
            try:
                self._appium_process.terminate()
                self._appium_process.wait(timeout=5)
            except Exception:
                pass
        for handle in (self._appium_log_handle, self._emulator_log_handle):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass


def main():
    """CLI 入口（用于测试）。"""
    import argparse

    parser = argparse.ArgumentParser(description="AppiumRunner — 移动应用自动化测试")
    parser.add_argument("app_name", help="应用名称，如 app_newsreader")
    parser.add_argument("task_id", help="任务ID，如 task_001")
    parser.add_argument("--apk", required=True, help="APK 文件路径")
    args = parser.parse_args()

    runner = AppiumRunner()
    if not runner.check_appium_server():
        print("Failed to check Appium server")
        sys.exit(1)

    result = runner.run_test(f"{args.app_name}/{args.task_id}", args.apk)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
