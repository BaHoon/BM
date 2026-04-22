#!/usr/bin/env python3
"""
Env_Manager.py - Workspace 重置与编译管理

职责：
  reset_workspace(app_name, task_id):
      将 data/app_name/base_src/ 完整拷贝到 workspace/app_name/task_id/，
      清除上一个任务的残余代码，保证每次实验从干净状态出发。

  build_project(app_name, task_id, timeout):
      在 workspace/app_name/task_id/ 执行 Gradle 编译命令，
      捕获 stdout/stderr，返回包含编译日志的结果字典（用于 CSR 指标）。

  get_apk_path(app_name, task_id):
      从 meta.json 取出 apk_path，返回完整绝对路径。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


class EnvManager:
    """工作空间生命周期管理器：重置 → 编译 → APK 路径查询。"""

    # Gradle 编译命令（Windows vs Unix）
    _GRADLE_CMD_WIN  = ".\\gradlew.bat assembleDebug"
    _GRADLE_CMD_UNIX = "./gradlew assembleDebug"

    # 忽略复制的目录/文件（缓存、构建产物）
    _IGNORE_DIRS = {"build", ".gradle", ".idea", ".kotlin", "cache", ".cxx"}
    _JDK_ENV_KEYS = {
        17: ("JDK17_HOME", "JAVA17_HOME"),
        21: ("JDK21_HOME", "JAVA21_HOME"),
    }

    def __init__(
        self,
        data_dir:      str = "data",
        workspace_dir: str = "workspace",
        results_dir:   str = "results",
    ):
        root = Path(__file__).parent.parent
        self.data_dir      = root / data_dir
        self.workspace_dir = root / workspace_dir
        self.results_dir   = root / results_dir

    # ----------------------------------------------------------------------- #
    #  公开接口
    # ----------------------------------------------------------------------- #

    def reset_workspace(self, app_name: str, task_id: str) -> Path:
        """
        将 base_src 拷贝到 workspace/app_name/task_id/。

        步骤：
          1. 若 workspace 目标目录已存在，先停止 Gradle daemon（防文件锁），再删除。
          2. 复制 base_src → workspace 目标目录（忽略 build/.gradle 缓存）。

        Returns:
            workspace 目标目录的 Path 对象。
        """
        base_src = self.data_dir / app_name / "base_src"
        dst      = self.workspace_dir / app_name / task_id

        if not base_src.exists():
            raise FileNotFoundError(f"base_src not found: {base_src}")

        print(f"[EnvManager] Resetting workspace: {dst}")

        # --- 停止 Gradle daemon 防止文件锁 ---
        if dst.exists():
            self._stop_gradle_daemon(dst)
            shutil.rmtree(dst, ignore_errors=True)
            print(f"[EnvManager] Cleaned old workspace.")

        # --- 复制 base_src ---
        shutil.copytree(
            base_src, dst,
            ignore=self._ignore_fn,
            dirs_exist_ok=False,
        )
        print(f"[EnvManager] ✓ Workspace ready: {dst}")
        return dst

    def build_project(
        self,
        app_name: str,
        task_id:  str,
        timeout:  int = 600,
    ) -> dict:
        """
        在 workspace 执行 Gradle 编译，返回结构化结果。

        Returns:
            {
                "success":      bool,
                "return_code":  int,
                "elapsed_time": float,
                "apk_exists":   bool,
                "apk_path":     str | None,
                "apk_size_mb":  float,
                "stdout":       str,
                "stderr":       str,
                "error_summary": str   # 关键错误行（供 RQ4 分析）
            }
        """
        workspace_path = self.workspace_dir / app_name / task_id
        if not workspace_path.exists():
            raise FileNotFoundError(
                f"Workspace not found: {workspace_path}. Run reset_workspace() first."
            )

        project_root = self._resolve_gradle_project_root(workspace_path)
<<<<<<< HEAD
<<<<<<< HEAD
        meta = self._load_meta(app_name, task_id)
        cmd = meta.get("build_command") or (
            self._GRADLE_CMD_WIN if os.name == "nt" else self._GRADLE_CMD_UNIX
        )
        required_java = self._detect_required_java_version(project_root)
        jdk_home = self._resolve_jdk_home(required_java)
        if jdk_home:
            print(f"[EnvManager] Java required={required_java}, switching JAVA_HOME -> {jdk_home}")
        else:
            print(f"[EnvManager] Java required={required_java}, no explicit JDK home found, using current JAVA_HOME")
=======
        cmd = self._GRADLE_CMD_WIN if os.name == "nt" else self._GRADLE_CMD_UNIX
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)
=======
        cmd = self._GRADLE_CMD_WIN if os.name == "nt" else self._GRADLE_CMD_UNIX
>>>>>>> parent of 42a28ff (按“真实工具 Agent”做了3种完整重构)
        print(f"[EnvManager] Building: {cmd}  (cwd={project_root})")

        t0 = time.time()
        try:
            run_env = os.environ.copy()
            if jdk_home:
                run_env["JAVA_HOME"] = str(jdk_home)
                jdk_bin = str(Path(jdk_home) / "bin")
                run_env["PATH"] = jdk_bin + os.pathsep + run_env.get("PATH", "")

            proc = subprocess.run(
                cmd,
                cwd=project_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=run_env,
            )
            elapsed = round(time.time() - t0, 2)
            stdout  = proc.stdout or ""
            stderr  = proc.stderr or ""
            rc      = proc.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.time() - t0, 2)
            raw_timeout_stdout = exc.stdout or ""
            if isinstance(raw_timeout_stdout, bytes):
                stdout = raw_timeout_stdout.decode("utf-8", errors="replace")
            else:
                stdout = str(raw_timeout_stdout)
            stderr  = f"TIMEOUT after {timeout}s"
            rc      = -1

        success = rc == 0

        # APK 路径
        apk_path, apk_exists, apk_size = self._check_apk(app_name, task_id)

        # 提取关键错误行（最多 30 行含 error/exception）
        error_summary = self._extract_errors(stdout + "\n" + stderr)

        result = {
            "success":       success or apk_exists,
            "return_code":   rc,
            "elapsed_time":  elapsed,
            "apk_exists":    apk_exists,
            "apk_path":      str(apk_path) if apk_path else None,
            "apk_size_mb":   apk_size,
            "java_required": required_java,
            "java_home_used": str(jdk_home) if jdk_home else None,
            "stdout":        stdout[-8000:],   # 保留后 8000 字符避免过大
            "stderr":        stderr[-4000:],
            "error_summary": error_summary,
        }

        status = "✓ BUILD OK" if result["success"] else "✗ BUILD FAILED"
        print(f"[EnvManager] {status}  elapsed={elapsed}s  RC={rc}")

        # 保存编译日志
        self._save_compile_log(app_name, task_id, result)
        return result

    def _detect_required_java_version(self, project_root: Path) -> int:
        """Detect required Java version from Gradle/Kotlin build scripts, default 17."""
        version = 17
        patterns_21 = [
            r"VERSION_21",
            r"JVM_21",
            r"languageVersion\s*=\s*JavaLanguageVersion\.of\(\s*21\s*\)",
            r"sourceCompatibility\s*=\s*JavaVersion\.VERSION_21",
            r"targetCompatibility\s*=\s*JavaVersion\.VERSION_21",
            r"sourceCompatibility\s*=\s*21",
            r"targetCompatibility\s*=\s*21",
            r"jvmTarget\s*=\s*\"?21\"?",
            r"--release\s+21",
        ]
        combined = re.compile("|".join(patterns_21), flags=re.IGNORECASE)

        for ext in ("*.gradle", "*.kts", "gradle.properties"):
            files = list(project_root.rglob(ext)) if "*" in ext else [project_root / ext]
            for f in files:
                if not f.exists() or not f.is_file():
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if combined.search(text):
                    return 21
        return version

    def _resolve_jdk_home(self, java_version: int) -> Optional[Path]:
        """Resolve JDK home for required version from env vars and common install locations."""
        keys = self._JDK_ENV_KEYS.get(java_version, ())
        for key in keys:
            value = os.environ.get(key)
            if value and Path(value).exists():
                return Path(value)

        # If current JAVA_HOME already matches requested version, reuse it.
        current_java_home = os.environ.get("JAVA_HOME")
        if current_java_home:
            p = Path(current_java_home)
            if p.exists() and str(java_version) in p.name:
                return p

        if os.name == "nt":
            candidates = [
                Path(f"C:/Program Files/Java/jdk-{java_version}"),
                Path(f"C:/Program Files/Java/jdk-{java_version}.0.0"),
                Path(f"C:/Program Files/Eclipse Adoptium/jdk-{java_version}"),
                Path(f"C:/Program Files/Microsoft/jdk-{java_version}"),
            ]
            for c in candidates:
                if c.exists():
                    return c

            # Fuzzy fallback under common Java install directory.
            java_dir = Path("C:/Program Files/Java")
            if java_dir.exists():
                for c in sorted(java_dir.glob(f"*{java_version}*")):
                    if c.is_dir() and (c / "bin").exists():
                        return c

        return None

    def get_apk_path(self, app_name: str, task_id: str) -> Optional[Path]:
        """返回 APK 完整路径（不存在返回 None）。"""
        path, exists, _ = self._check_apk(app_name, task_id)
        return path if exists else None

    # ----------------------------------------------------------------------- #
    #  私有方法
    # ----------------------------------------------------------------------- #

    def _ignore_fn(self, directory: str, contents: list) -> set:
        """shutil.copytree 的 ignore 回调，跳过缓存/构建目录。"""
        return {c for c in contents if c in self._IGNORE_DIRS or c.endswith(".lock")}

    def _stop_gradle_daemon(self, workspace_path: Path) -> None:
        """尝试停止 Gradle daemon，防止文件锁阻碍删除。"""
        project_root = self._resolve_gradle_project_root(workspace_path)
        cmd = ".\\gradlew.bat --stop" if os.name == "nt" else "./gradlew --stop"
        try:
            subprocess.run(
                cmd,
                cwd=project_root,
                shell=True,
                capture_output=True,
                timeout=30,
            )
            time.sleep(1)
        except Exception:
            pass

    def _resolve_gradle_project_root(self, workspace_path: Path) -> Path:
        """定位真实 Gradle 根目录（包含 gradlew/gradlew.bat）。"""
        wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"

        if (workspace_path / wrapper).exists():
            return workspace_path

        # 兼容 base_src 下再嵌套一层项目目录（如 rssreader-main）
        candidates = list(workspace_path.glob(f"*/{wrapper}"))
        if candidates:
            return candidates[0].parent

        # 找不到时退回原路径，保持原有行为（后续由 subprocess 报错）
        return workspace_path

    def _check_apk(
        self, app_name: str, task_id: str
    ) -> tuple[Optional[Path], bool, float]:
        """
        检查 APK 是否已存在。
        Returns: (apk_path_or_None, exists_bool, size_mb)
        """
        try:
            meta = self._load_meta(app_name, task_id)
            rel  = meta.get("apk_path", "app/build/outputs/apk/debug/app-debug.apk")
            workspace_task = self.workspace_dir / app_name / task_id

            # 1) 兼容既有配置：相对 task 根目录
            full = workspace_task / rel
            if full.exists():
                size_mb = round(full.stat().st_size / 1024 / 1024, 2)
                return full, True, size_mb

            # 2) 兼容嵌套工程：相对真实 Gradle 根目录
            project_root = self._resolve_gradle_project_root(workspace_task)
            nested_full = project_root / rel
            if nested_full.exists():
                size_mb = round(nested_full.stat().st_size / 1024 / 1024, 2)
                return nested_full, True, size_mb

            # 3) 兜底：扫描常见产物名
            candidates = list(workspace_task.glob("**/app/build/outputs/apk/debug/app-debug.apk"))
            if candidates:
                apk = candidates[0]
                size_mb = round(apk.stat().st_size / 1024 / 1024, 2)
                return apk, True, size_mb
        except Exception:
            pass
        return None, False, 0.0

    def _load_meta(self, app_name: str, task_id: str) -> dict:
        meta_path = self.data_dir / app_name / task_id / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"meta.json not found: {meta_path}")
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _extract_errors(self, text: str, max_lines: int = 30) -> str:
        """从编译输出中提取包含 error/exception/failure 的关键行。"""
        lines = text.splitlines()
        keywords = ("error:", "exception", "failure", "could not", "unresolved")
        key_lines = [l for l in lines if any(k in l.lower() for k in keywords)]
        return "\n".join(key_lines[:max_lines])

    def _save_compile_log(self, app_name: str, task_id: str, result: dict) -> None:
        out_dir = self.results_dir / app_name / task_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # 保留完整 stdout/stderr
        (out_dir / "compile_stdout.txt").write_text(
            result.get("stdout", ""), encoding="utf-8"
        )
        (out_dir / "compile_stderr.txt").write_text(
            result.get("stderr", ""), encoding="utf-8"
        )

        # 结构化摘要（不含大文本）
        summary = {k: v for k, v in result.items()
                   if k not in ("stdout", "stderr")}
        with open(out_dir / "compilation_result.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Env_Manager — Workspace 重置与编译")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # reset 子命令
    p_reset = sub.add_parser("reset", help="重置 workspace（从 base_src 复制）")
    p_reset.add_argument("app_name")
    p_reset.add_argument("task_id")

    # build 子命令
    p_build = sub.add_parser("build", help="执行 Gradle 编译")
    p_build.add_argument("app_name")
    p_build.add_argument("task_id")
    p_build.add_argument("--timeout", type=int, default=600)

    args = parser.parse_args()
    mgr  = EnvManager()

    if args.cmd == "reset":
        mgr.reset_workspace(args.app_name, args.task_id)
    elif args.cmd == "build":
        result = mgr.build_project(args.app_name, args.task_id, args.timeout)
        status = "✓ SUCCESS" if result["success"] else "✗ FAILED"
        print(f"\n{status}  APK={result['apk_path']}  time={result['elapsed_time']}s")


if __name__ == "__main__":
    main()
