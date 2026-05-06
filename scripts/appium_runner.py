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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

from appium import webdriver
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

from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class AppiumRunner:
    """Appium 自动化测试执行器。"""

    # Appium 服务配置
    APPIUM_HOST = "localhost"
    APPIUM_PORT = 4723
    APPIUM_TIMEOUT = 30  # 等待 Appium 启动的超时

    # ADB 设备配置
    ADB_DEVICE = "127.0.0.1:7555"  # 本地模拟器端口

    def __init__(self):
        self.root_dir = Path(__file__).parent.parent
        self.data_dir = self.root_dir / "data"
        self.results_dir = self.root_dir / "results"
        self.driver: Optional[webdriver.Remote] = None
        self._appium_process: Optional[subprocess.Popen] = None

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
            output = result.stdout.lower()
            if "device" in output and "offline" not in output:
                print(f"[AppiumRunner] ADB device available: {self.ADB_DEVICE}")
                return True
        except Exception as e:
            print(f"[AppiumRunner] ADB check failed: {e}")
        return False

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
            cmd = [
                "appium",
                "--host", self.APPIUM_HOST,
                "--port", str(self.APPIUM_PORT),
                "--allow-insecure", "adb_screen_recording",
            ]
            self._appium_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[AppiumRunner] Appium started on {self.APPIUM_HOST}:{self.APPIUM_PORT}")
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

        # 构建 AppiumOptions 或 desired capabilities（根据客户端可用性回退）
        if AppiumOptions is not None:
            options = AppiumOptions()
            options.platform_name = "Android"
            options.device_name = self.ADB_DEVICE
            options.app = apk_path
            options.app_package = package_name
            options.app_activity = f"{package_name}.MainActivity"
            options.automation_name = "UiAutomator2"
            options.auto_grant_permissions = True
            options.new_command_timeout = 300
            use_options = True
        else:
            # 回退到旧式 desiredCapabilities 字典
            caps = {
                "platformName": "Android",
                "deviceName": self.ADB_DEVICE,
                "app": apk_path,
                "appPackage": package_name,
                "appActivity": f"{package_name}.MainActivity",
                "automationName": "UiAutomator2",
                "autoGrantPermissions": True,
                "newCommandTimeout": 300,
            }
            use_options = False

        # 连接到 Appium（兼容 Appium 1/2）
        candidate_urls = [
            f"http://{self.APPIUM_HOST}:{self.APPIUM_PORT}",
            f"http://{self.APPIUM_HOST}:{self.APPIUM_PORT}/wd/hub",
        ]
        last_error = None
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
                last_error = exc

        raise RuntimeError(f"Failed to connect to Appium endpoints: {last_error}")

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
