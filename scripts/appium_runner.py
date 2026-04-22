#!/usr/bin/env python3
"""
Appium Runner - 移动应用 UI 自动化测试执行引擎

职责：
  1. 检查/启动 Appium 服务器
  2. 连接到 ADB 设备（本地模拟器或真机）
  3. 执行 test_script.py（注入 driver/take_screenshot/AppiumBy 上下文）
  4. 采集截图 → 保存到 results/app_name/task_id/screenshots/
  5. 返回执行结果（success, screenshots[], log）
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from urllib.request import urlopen

from appium import webdriver
from appium.webdriver.common.appium_options import AppiumOptions
from appium.webdriver.common.by import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class AppiumRunner:
    """Appium 自动化测试执行器。"""

    APPIUM_TIMEOUT = 30  # 等待 Appium 启动的超时

    def __init__(self):
        self.root_dir = Path(__file__).parent.parent
        self.data_dir = self.root_dir / "data"
        self.results_dir = self.root_dir / "results"
        self.driver: Optional[webdriver.Remote] = None
        self._appium_process: Optional[subprocess.Popen] = None
        self.appium_host = os.getenv("APPIUM_HOST", "127.0.0.1")
        self.appium_port = int(os.getenv("APPIUM_PORT", "4723"))
        self.appium_base_path = os.getenv("APPIUM_BASE_PATH", "/wd/hub")
        self.appium_url = os.getenv(
            "APPIUM_URL",
            f"http://{self.appium_host}:{self.appium_port}{self.appium_base_path}",
        )
        self.appium_autostart = os.getenv("APPIUM_AUTOSTART", "1").lower() in {"1", "true", "yes"}
        self.appium_start_cmd = os.getenv("APPIUM_START_CMD", "").strip()
        self.adb_device = os.getenv("ANDROID_SERIAL", "127.0.0.1:7555")
        self.emulator_start_cmd = os.getenv("EMULATOR_START_CMD", "").strip()
        self.emulator_wait_s = int(os.getenv("EMULATOR_WAIT_SECONDS", "35"))

    def check_appium_server(self) -> bool:
        """
        检查 Appium 服务是否可用。
        若不可用则尝试启动；若仍失败则返回 False。
        """
        if self._is_appium_running():
            print("[AppiumRunner] Appium server already running")
            return True

        if not self.appium_autostart:
            print("[AppiumRunner] Appium auto-start disabled (APPIUM_AUTOSTART=0)")
            return False

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
        执行一个测试任务：启动应用 → 运行 test_script → 收集截图。

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
        log = ""

        try:
            # 步骤 1: 验证 ADB 设备
            if not self._verify_adb_device():
                return {
                    "success": False,
                    "elapsed_time": round(time.time() - t0, 2),
                    "screenshots": [],
                    "log": "ADB device not available",
                    "test_type": "custom",
                    "timestamp": datetime.now().isoformat(),
                }

            # 步骤 2: 启动应用（通过 Appium）
            self.driver = self._create_driver(apk_path, app_name, task_id)
            print(f"[AppiumRunner] Application started: {app_name}")

            # 步骤 3: 加载并执行 test_script
            test_script_path = self.data_dir / app_name / task_id / "test_script.py"
            if not test_script_path.exists():
                self.driver.quit()
                return {
                    "success": False,
                    "elapsed_time": round(time.time() - t0, 2),
                    "screenshots": [],
                    "log": f"test_script not found: {test_script_path}",
                    "test_type": "custom",
                    "timestamp": datetime.now().isoformat(),
                }

            # 创建截图保存目录
            screenshot_dir = self.results_dir / app_name / task_id / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            # 准备注入的全局环境
            screenshot_count = [0]  # 使用列表以允许嵌套函数修改

            def take_screenshot(name: str) -> str:
                """截图函数，注入到 test_script 的全局环境中。"""
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{screenshot_count[0]:02d}_{name}_{timestamp}.png"
                filepath = screenshot_dir / filename
                try:
                    self.driver.save_screenshot(str(filepath))
                    screenshot_count[0] += 1
                    screenshots.append(str(filepath))
                    print(f"  [screenshot] {filename}")
                    return str(filepath)
                except Exception as e:
                    print(f"  [screenshot] ERROR: {e}")
                    raise

            # 注入全局环境
            globals_dict = {
                "driver": self.driver,
                "take_screenshot": take_screenshot,
                "AppiumBy": AppiumBy,
                "time": time,
                "WebDriverWait": WebDriverWait,
                "EC": EC,
            }

            # 执行 test_script
            print(f"[AppiumRunner] Running test_script: {test_script_path}")
            with open(test_script_path, "r", encoding="utf-8") as f:
                test_code = f.read()

            exec(test_code, globals_dict)
            print(f"[AppiumRunner] test_script completed successfully")

            elapsed = round(time.time() - t0, 2)
            return {
                "success": True,
                "elapsed_time": elapsed,
                "screenshots": screenshots,
                "log": f"Test passed. {len(screenshots)} screenshots captured.",
                "test_type": "custom",
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
                "test_type": "custom",
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
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            devices = self._parse_adb_devices(result.stdout)
            if self.adb_device in devices:
                print(f"[AppiumRunner] ADB device available: {self.adb_device}")
                return True

            # 若指定设备不可用，且有其他在线设备，自动使用第一个。
            if devices:
                self.adb_device = devices[0]
                print(f"[AppiumRunner] ADB fallback device selected: {self.adb_device}")
                return True

            if self.emulator_start_cmd:
                print("[AppiumRunner] No ADB device found, starting emulator...")
                self._start_emulator_once()
                result2 = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
                devices2 = self._parse_adb_devices(result2.stdout)
                if devices2:
                    self.adb_device = devices2[0]
                    print(f"[AppiumRunner] ADB device ready after emulator start: {self.adb_device}")
                    return True
        except Exception as e:
            print(f"[AppiumRunner] ADB check failed: {e}")
        return False

    def _parse_adb_devices(self, adb_output: str) -> list[str]:
        devices = []
        for line in (adb_output or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices attached"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def _is_appium_running(self) -> bool:
        """检查 Appium 服务器是否在运行。"""
        candidate_urls = []
        base = self.appium_url.rstrip("/")
        candidate_urls.append(base + "/status")
        if not base.endswith("/wd/hub"):
            candidate_urls.append(base + "/wd/hub/status")
        candidate_urls.append(f"http://{self.appium_host}:{self.appium_port}/status")
        candidate_urls.append(f"http://{self.appium_host}:{self.appium_port}/wd/hub/status")

        for url in candidate_urls:
            try:
                response = urlopen(url, timeout=2)
                if response.status == 200:
                    return True
            except Exception:
                continue
        return False

    def _start_appium_server(self) -> bool:
        """启动 Appium 服务器（本地）。"""
        commands = self._candidate_appium_commands()
        for cmd in commands:
            try:
                self._appium_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[AppiumRunner] Appium start command: {' '.join(cmd)}")
                deadline = time.time() + self.APPIUM_TIMEOUT
                while time.time() < deadline:
                    if self._is_appium_running():
                        print(f"[AppiumRunner] Appium started on {self.appium_host}:{self.appium_port}")
                        return True
                    time.sleep(1)
            except Exception as e:
                print(f"[AppiumRunner] Failed to start with {' '.join(cmd)}: {e}")
                continue
        return False

    def _candidate_appium_commands(self) -> list[list[str]]:
        if self.appium_start_cmd:
            return [self.appium_start_cmd.split()]

        common_args = [
            "--host", self.appium_host,
            "--port", str(self.appium_port),
            "--allow-insecure", "adb_screen_recording",
        ]
        if self.appium_base_path and self.appium_base_path != "/wd/hub":
            common_args += ["--base-path", self.appium_base_path]

        candidates: list[list[str]] = []
        if shutil.which("appium"):
            candidates.append(["appium", *common_args])
        if shutil.which("npx"):
            candidates.append(["npx", "appium", *common_args])
        if not candidates:
            candidates.append(["appium", *common_args])
        return candidates

    def _start_emulator_once(self) -> None:
        if not self.emulator_start_cmd:
            return
        try:
            subprocess.Popen(
                self.emulator_start_cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(max(1, self.emulator_wait_s))
        except Exception as e:
            print(f"[AppiumRunner] Emulator start failed: {e}")

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
        # 从 meta.json 中读取包名
        package_name = None
        if task_id:
            meta_path = self.data_dir / app_name / task_id / "meta.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        package_name = meta.get("app_package")
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
        
        print(f"[AppiumRunner] Package name: {package_name}")

        # 构建 Desired Capabilities
        options = AppiumOptions()
        options.platform_name = "Android"
        options.device_name = self.adb_device
        options.udid = self.adb_device
        options.app = apk_path
        options.app_package = package_name
        options.app_activity = f"{package_name}.MainActivity"
        options.automation_name = "UiAutomator2"
        options.auto_grant_permissions = True
        options.new_command_timeout = 300

        # 连接到 Appium
        driver = webdriver.Remote(self.appium_url, options=options)
        driver.implicitly_wait(10)

        return driver

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
