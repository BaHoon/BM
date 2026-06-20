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
import shlex
import shutil
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

try:
    from runtime_env import ensure_android_runtime_env
except Exception:
    def ensure_android_runtime_env():
        return None

ensure_android_runtime_env()


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
        ui_trees = []
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
            ui_tree_dir = self.results_dir / app_name / task_id / "ui_trees"
            ui_tree_dir.mkdir(parents=True, exist_ok=True)

            # 准备注入的全局环境
            screenshot_count = [0]  # 使用列表以允许嵌套函数修改

            def take_screenshot(name: str) -> str:
                """截图函数，注入到 test_script 的全局环境中。"""
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                idx = screenshot_count[0]
                filename = f"{idx:02d}_{name}_{timestamp}.png"
                filepath = screenshot_dir / filename
                try:
                    self.driver.save_screenshot(str(filepath))
                    ui_tree_path = self._save_ui_tree(ui_tree_dir, idx, name, timestamp)
                    if ui_tree_path:
                        ui_trees.append({
                            "name": name,
                            "path": str(ui_tree_path),
                            "screenshot": str(filepath),
                        })
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
            final_ui_tree = self._save_ui_tree(ui_tree_dir, screenshot_count[0], "final_state")
            if final_ui_tree:
                ui_trees.append({
                    "name": "final_state",
                    "path": str(final_ui_tree),
                    "screenshot": None,
                })

            elapsed = round(time.time() - t0, 2)
            return {
                "success": True,
                "elapsed_time": elapsed,
                "screenshots": screenshots,
                "ui_trees": ui_trees,
                "final_ui_tree": str(final_ui_tree) if final_ui_tree else None,
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
                "ui_trees": ui_trees,
                "final_ui_tree": ui_trees[-1]["path"] if ui_trees else None,
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
                        app_activity = meta.get("target_activity")
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

    def _status_endpoint_ok(self, url: str) -> bool:
        try:
            from urllib.request import urlopen

            response = urlopen(url, timeout=2)
            return response.status == 200
        except Exception:
            return False

    def _save_ui_tree(
        self,
        ui_tree_dir: Path,
        index: int,
        name: str,
        timestamp: Optional[str] = None,
    ) -> Optional[Path]:
        """保存当前页面 UI hierarchy，供 Level 2 节点匹配使用。"""
        if not self.driver:
            return None
        timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)[:80]
        path = ui_tree_dir / f"{index:02d}_{safe_name}_{timestamp}.xml"
        try:
            source = self.driver.page_source or ""
            path.write_text(source, encoding="utf-8")
            print(f"  [ui-tree] {path.name}")
            return path
        except Exception as exc:
            print(f"  [ui-tree] ERROR: {exc}")
            return None

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
