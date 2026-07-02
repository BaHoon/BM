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
import shlex
import shutil
import subprocess
import sys
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

    def __init__(self):
        self.root_dir = Path(__file__).parent.parent
        self.data_dir = self.root_dir / "data"
        self.results_dir = self.root_dir / "results"
        self.ADB_DEVICE = os.getenv("ADB_DEVICE", self.ADB_DEVICE)
        self.avd_name = os.getenv("BM_AVD_NAME") or os.getenv("ANDROID_AVD_NAME") or self.AVD_NAME
        self.driver: Optional[webdriver.Remote] = None
        self._target_package_name: Optional[str] = None
        self._appium_process: Optional[subprocess.Popen] = None
        self._emulator_process: Optional[subprocess.Popen] = None
        self._appium_log_handle = None
        self._emulator_log_handle = None
        self._restart_dedicated_adb_server()

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
            level2_spec = build_level2_spec(meta)

            # 创建截图和目标 XML 保存目录
            screenshot_dir = self.results_dir / app_name / task_id / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ui_dir = self.results_dir / app_name / task_id / "ui_context"
            ui_dir.mkdir(parents=True, exist_ok=True)

            def capture_target_if_match(source_label: str, screenshot_path: Optional[str] = None) -> bool:
                """如果当前页面是目标页，只保存这个目标页 XML。"""
                if target_page["found"]:
                    return True
                try:
                    xml_text = self.driver.page_source
                    self._capture_debug_step(ui_dir, source_label, xml_text)
                    match = match_target_xml(xml_text, level2_spec)
                    if not match.get("matched"):
                        return False

                    if screenshot_path is None:
                        filepath = screenshot_dir / "target_page.png"
                        self.driver.save_screenshot(str(filepath))
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
            crawler_result = self._crawl_to_target_page(
                level2_spec=level2_spec,
                capture_target=capture_target_if_match,
                timeout=timeout,
            )
            screenshots.extend(crawler_result.get("screenshots", []))
            crawler_debug = {}
            if not target_page["found"]:
                crawler_debug = self._capture_crawler_debug(
                    ui_dir=ui_dir,
                    screenshot_dir=screenshot_dir,
                    level2_spec=level2_spec,
                    steps=crawler_result.get("steps", []),
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
                try:
                    self.driver.quit()
                except Exception:
                    pass

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

        # 备用包名映射（如果 meta.json 不可用）
        if not package_name:
            package_map = {
                "app_newsreader": "livio.rssreader",
                "app_foodyou": "com.example.foodyou",
                "app_todoagenda": "com.example.todoagenda",
            }
            package_name = package_map.get(app_name, f"com.example.{app_name}")
        if not app_activity:
            app_activity = f"{package_name}.MainActivity"
        self._target_package_name = package_name
        
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
                driver.implicitly_wait(10)
                print(f"[AppiumRunner] Connected to Appium endpoint: {appium_url}")
                return driver
            except Exception as exc:
                errors.append(f"{appium_url}: {type(exc).__name__}: {exc}")

        raise RuntimeError(f"Failed to connect to Appium endpoints: {' || '.join(errors)}")

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
        tried: set[str] = set()
        steps = 0
        max_steps = int(os.getenv("UI_CRAWLER_MAX_STEPS", "18"))
        dead_end_scrolls = int(os.getenv("UI_CRAWLER_DEAD_END_SCROLLS", "5"))
        scroll_streak = 0
        prefer_scroll_up_steps = 0
        log_lines: list[str] = []

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
                xml_text = self.driver.page_source
            except Exception as exc:
                return {"success": False, "screenshots": screenshots, "log": f"crawler page_source failed: {exc}"}

            current_package = self._safe_current_package()
            if self._is_outside_target_app(current_package):
                popup = self._find_popup_candidate(xml_text, level2_spec)
                if popup and self._tap_bounds(popup["bounds"]):
                    log_lines.append(
                        f"step {steps}: dismiss external {current_package} "
                        f"{popup.get('label', '')[:80]}"
                    )
                    continue
                if self._return_to_target_app():
                    log_lines.append(f"step {steps}: return to target app from {current_package}")
                    continue
                log_lines.append(f"step {steps}: outside target app {current_package}")
                break

            signature = self._xml_signature(xml_text)
            if signature in visited and len(visited) > 0:
                if not self._try_scroll():
                    log_lines.append(f"step {steps}: repeated page, no scroll")
            visited.add(signature)

            popup = self._find_popup_candidate(xml_text, level2_spec)
            candidate = popup or self._choose_click_candidate(xml_text, level2_spec, tried)
            if not candidate:
                if scroll_streak >= dead_end_scrolls and self._has_back_candidate(xml_text):
                    if self._go_back():
                        log_lines.append(f"step {steps}: back from dead end")
                        scroll_streak = 0
                        prefer_scroll_up_steps = 3
                        tried.clear()
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
                    tried.clear()
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
        }

    def _capture_crawler_debug(
        self,
        ui_dir: Path,
        screenshot_dir: Path,
        level2_spec: dict,
        steps: list[str],
    ) -> dict:
        """Save only the final failed screen for debugging; scoring ignores it."""
        debug = {
            "current_package": self._safe_current_package(),
            "current_activity": self._safe_current_activity(),
            "last_ui_dom_tree_path": None,
            "last_screenshot": None,
            "last_page_match": None,
            "steps": steps,
        }
        try:
            xml_text = self.driver.page_source
            xml_path = ui_dir / "crawler_last.xml"
            xml_path.write_text(xml_text, encoding="utf-8")
            debug["last_ui_dom_tree_path"] = str(xml_path)
            debug["last_page_match"] = match_target_xml(xml_text, level2_spec)
        except Exception as exc:
            debug["xml_error"] = f"{type(exc).__name__}: {exc}"

        try:
            screenshot_path = screenshot_dir / "crawler_last.png"
            self.driver.save_screenshot(str(screenshot_path))
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

    def _capture_debug_step(self, ui_dir: Path, source_label: str, xml_text: str) -> None:
        if os.getenv("UI_CRAWLER_DEBUG_STEPS", "0") != "1":
            return
        safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", source_label)
        try:
            (ui_dir / f"{safe_label}.xml").write_text(xml_text, encoding="utf-8")
        except Exception:
            pass

    def _find_popup_candidate(self, xml_text: str, level2_spec: dict) -> Optional[dict]:
        popup_terms = [normalize_text(x) for x in level2_spec.get("popup_terms", [])]
        candidates = self._click_candidates_from_xml(xml_text)
        for c in candidates:
            label = normalize_text(c.get("label", ""))
            if any(term and (term == label or term in label) for term in popup_terms):
                return c
        return None

    def _choose_click_candidate(self, xml_text: str, level2_spec: dict, tried: set[str]) -> Optional[dict]:
        candidates = self._click_candidates_from_xml(xml_text)
        if not candidates:
            return None

        nav_terms = [normalize_text(x) for x in level2_spec.get("navigation_terms", [])]
        score_terms = [normalize_text(x) for x in level2_spec.get("score_terms", [])]

        scored = []
        for c in candidates:
            if c["key"] in tried:
                continue
            label = normalize_text(c.get("label", ""))
            score = 0
            for term in score_terms:
                if term and term in label:
                    score += 8
            for term in nav_terms:
                if term and term in label:
                    score += 4
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
            enabled = attrs.get("enabled", "true").lower()
            displayed = attrs.get("displayed", "true").lower()
            if enabled == "false" or displayed == "false":
                continue
            if clickable != "true":
                continue

            label = self._label_from_node(node)
            if not label.strip():
                continue

            candidates.append({
                "bounds": bounds,
                "clickable": clickable,
                "label": label,
                "area": self._bounds_area(parsed_bounds),
                "key": f"{bounds}:{normalize_text(label)}",
            })
        return candidates

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

    def _tap_candidate(self, candidate: dict) -> bool:
        label = normalize_text(candidate.get("label", ""))
        if AppiumBy is not None:
            self._set_implicit_wait(0)
            try:
                for phrase in ("Go to settings", "Settings"):
                    if normalize_text(phrase) in label:
                        try:
                            self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, phrase).click()
                            return True
                        except Exception:
                            pass
            finally:
                self._set_implicit_wait(10)
        return self._tap_bounds(candidate.get("bounds", ""))

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
            self.driver.execute_script("mobile: scrollGesture", {
                "left": int(width * 0.1),
                "top": int(height * 0.2),
                "width": int(width * 0.8),
                "height": int(height * 0.6),
                "direction": direction,
                "percent": 0.7,
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
