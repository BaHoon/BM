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
            self._clear_dir_with_retry(dst)
            print(f"[EnvManager] Cleaned old workspace.")

        # --- 复制 base_src ---
        shutil.copytree(
            base_src, dst,
            ignore=self._ignore_fn,
            # Windows 下目录可能短暂残留；允许拷贝到已存在目录可避免 WinError 183。
            dirs_exist_ok=True,
        )
        self._ensure_gradlew_executable(self._resolve_gradle_project_root(dst))
        self._save_workspace_manifest(app_name, task_id, base_src, dst)
        print(f"[EnvManager] ✓ Workspace ready: {dst}")
        return dst

    def apply_ground_truth(self, app_name: str, task_id: str) -> Path:
        """
        用 data/app_name/task_id/ground_truth_src 覆盖当前 workspace。

        这个入口用于 oracle/标准答案回放：跳过 LLM 生成代码，但仍然让后续
        build_project -> Appium -> Evaluator 走完整评分流程。
        """
        ground_truth_src = self.data_dir / app_name / task_id / "ground_truth_src"
        dst = self.workspace_dir / app_name / task_id

        if not ground_truth_src.exists():
            raise FileNotFoundError(f"ground_truth_src not found: {ground_truth_src}")

        print(f"[EnvManager] Applying ground truth: {ground_truth_src} -> {dst}")

        if dst.exists():
            self._stop_gradle_daemon(dst)
            self._clear_dir_with_retry(dst)

        shutil.copytree(
            ground_truth_src,
            dst,
            ignore=self._ignore_fn,
            dirs_exist_ok=True,
        )
        self._ensure_gradlew_executable(self._resolve_gradle_project_root(dst))
        self._save_workspace_manifest(app_name, task_id, ground_truth_src, dst)
        print(f"[EnvManager] ✓ Ground truth workspace ready: {dst}")
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
        self._ensure_gradlew_executable(project_root)
        meta = self._load_meta(app_name, task_id)
        cmd = self._resolve_build_command(meta)
        required_java = self._detect_required_java_version(project_root)
        java_home_used: Optional[str] = None
        attempted_versions: list[int] = []

        print(f"[EnvManager] Building: {cmd}  (cwd={project_root})")
        if required_java is not None:
            print(f"[EnvManager] Detected Java requirement: {required_java}")

        # 第一次构建：优先按项目声明的 Java 版本切换。
        env = None
        if required_java is not None:
            attempted_versions.append(required_java)
            java_home = self._find_java_home_for_version(required_java)
            if java_home:
                java_home_used = java_home
                env = self._build_env_with_java_home(java_home)
                print(f"[EnvManager] Using JAVA_HOME={java_home}")
            else:
                print(f"[EnvManager] WARN: JDK {required_java} not found, try default JAVA_HOME")

        stdout, stderr, rc, elapsed = self._run_gradle(
            cmd=cmd,
            project_root=project_root,
            timeout=timeout,
            env=env,
        )

        # 失败兜底：从错误文本提取 release 版本并自动重试一次。
        if rc != 0:
            inferred = self._parse_required_java_from_error(stdout + "\n" + stderr)
            should_retry = inferred is not None and inferred not in attempted_versions
            if should_retry:
                fallback_home = self._find_java_home_for_version(inferred)
                if fallback_home:
                    attempted_versions.append(inferred)
                    java_home_used = fallback_home
                    print(f"[EnvManager] Retry with detected JDK {inferred}: {fallback_home}")
                    stdout, stderr, rc, elapsed2 = self._run_gradle(
                        cmd=cmd,
                        project_root=project_root,
                        timeout=timeout,
                        env=self._build_env_with_java_home(fallback_home),
                    )
                    elapsed = round(elapsed + elapsed2, 2)

        success = rc == 0

        # APK 路径
        apk_path, apk_exists, apk_size = self._check_apk(app_name, task_id)

        combined_log = stdout + "\n" + stderr

        # 提取关键诊断信息
        error_summary = self._extract_errors(combined_log)
        warning_lines = self._extract_warnings(combined_log)
        level1 = self._score_level1(
            success=success,
            warning_lines=warning_lines,
            build_log=combined_log,
            project_root=project_root,
        )

        result = {
            "success":       success,
            "return_code":   rc,
            "elapsed_time":  elapsed,
            "apk_exists":    apk_exists,
            "apk_path":      str(apk_path) if apk_path else None,
            "apk_size_mb":   apk_size,
            "required_java": required_java,
            "attempted_java_versions": attempted_versions,
            "java_home_used": java_home_used,
            "stdout":        stdout[-8000:],   # 保留后 8000 字符避免过大
            "stderr":        stderr[-4000:],
            "error_summary": error_summary,
            "warning_count":  len(warning_lines),
            "warning_summary": "\n".join(warning_lines[:30]),
            "level1_score":   level1["score"],
            "level1_category": level1["category"],
            "level1_reason":  level1["reason"],
            "level1_evidence": level1["evidence"],
        }

        status = "✓ BUILD OK" if result["success"] else "✗ BUILD FAILED"
        print(f"[EnvManager] {status}  elapsed={elapsed}s  RC={rc}")

        # 保存编译日志
        self._save_compile_log(app_name, task_id, result)
        return result

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

    def _clear_dir_with_retry(self, path: Path, retries: int = 3) -> None:
        """尽量清空目录；Windows 文件锁场景下进行多次重试。"""
        if not path.exists():
            return

        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                shutil.rmtree(path)
                return
            except Exception as exc:
                last_exc = exc
                # 最后一轮失败后尝试“清空内容但保留目录”，配合 copytree(dirs_exist_ok=True)。
                if attempt == retries:
                    self._clear_dir_contents(path)
                    return
                time.sleep(1)

        if last_exc is not None:
            raise last_exc

    def _clear_dir_contents(self, path: Path) -> None:
        """删除目录下所有子项，保留目录本身。"""
        if not path.exists():
            return
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass

    def _save_workspace_manifest(
        self,
        app_name: str,
        task_id: str,
        source_dir: Path,
        workspace_dir: Path,
    ) -> None:
        """记录本次 workspace 是从哪个 data/base_src 初始化的，方便复现实验。"""
        manifest = {
            "app_name": app_name,
            "task_id": task_id,
            "source_dir": str(source_dir.resolve()),
            "workspace_dir": str(workspace_dir.resolve()),
            "project_root": str(self._resolve_gradle_project_root(workspace_dir).resolve()),
        }
        (workspace_dir / ".workspace_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

    def _ensure_gradlew_executable(self, project_root: Path) -> None:
        """Unix/macOS 下确保 Gradle wrapper 可执行，避免 Permission denied。"""
        if os.name == "nt":
            return
        gradlew = project_root / "gradlew"
        if not gradlew.exists():
            return
        try:
            mode = gradlew.stat().st_mode
            if mode & 0o111:
                return
            gradlew.chmod(mode | 0o755)
            print(f"[EnvManager] chmod +x {gradlew}")
        except Exception as exc:
            print(f"[EnvManager] WARN: failed to chmod +x {gradlew}: {exc}")

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

    def _resolve_build_command(self, meta: dict) -> str:
        """优先使用任务 meta.json 的 build_command，并兼容 macOS/Linux wrapper 写法。"""
        cmd = (meta.get("build_command") or "").strip()
        if not cmd:
            return self._GRADLE_CMD_WIN if os.name == "nt" else self._GRADLE_CMD_UNIX

        if os.name == "nt":
            return cmd.replace("./gradlew", ".\\gradlew.bat").replace("gradlew ", ".\\gradlew.bat ")

        cmd = cmd.replace("gradlew.bat", "gradlew")
        if cmd == "gradlew":
            return "./gradlew"
        if cmd.startswith("gradlew "):
            return "./" + cmd
        return cmd

    def _extract_errors(self, text: str, max_lines: int = 30) -> str:
        """从编译输出中提取包含 error/exception/failure 的关键行。"""
        lines = text.splitlines()
        keywords = (
            "error:",
            "exception",
            "failure",
            "failed",
            "could not",
            "unresolved",
            "cannot find",
            "package ",
            "expecting ",
            "unexpected ",
            "illegal ",
        )
        key_lines = [l for l in lines if any(k in l.lower() for k in keywords)]
        return "\n".join(key_lines[:max_lines])

    def _extract_warnings(self, text: str, max_lines: int = 200) -> list[str]:
        """提取真实编译 warning 行，用于区分 Level 1 的 4 分和 3 分。"""
        warning_lines: list[str] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if not line:
                continue
            if "warning" not in lower and "deprecated" not in lower:
                continue
            if re.search(r"\b(no|0)\s+warnings?\b", lower):
                continue
            if "warning mode" in lower:
                continue
            if "to show individual deprecation warnings" in lower:
                continue
            if line in seen:
                continue
            warning_lines.append(line)
            seen.add(line)
            if len(warning_lines) >= max_lines:
                break
        return warning_lines

    def _score_level1(
        self,
        success: bool,
        warning_lines: list[str],
        build_log: str,
        project_root: Path,
    ) -> dict:
        """
        按用户定义的 Level 1 五档标准评分：
          4: 编译成功且零 warning
          3: 编译成功但有 warning
          2: 编译失败，局部语法/拼写类错误
          1: 编译失败，依赖/上下文错误
          0: 输出不是合法代码
        """
        if success:
            if warning_lines:
                return {
                    "score": 3,
                    "category": "compiled_with_warnings",
                    "reason": "编译成功，但存在 warning。",
                    "evidence": warning_lines[:5],
                }
            return {
                "score": 4,
                "category": "perfect_compile",
                "reason": "编译成功，且未检测到 warning。",
                "evidence": [],
            }

        invalid_evidence = self._detect_invalid_code_artifact(project_root, build_log)
        if invalid_evidence:
            return {
                "score": 0,
                "category": "invalid_code_output",
                "reason": "输出内容不是合法代码，疑似 Markdown/说明文字被写入源码。",
                "evidence": invalid_evidence[:5],
            }

        dependency_evidence = self._find_dependency_context_errors(build_log)
        syntax_evidence = self._find_syntax_local_errors(build_log)

        if dependency_evidence:
            return {
                "score": 1,
                "category": "dependency_or_context_error",
                "reason": "编译失败，错误指向不存在的依赖、包、类或工程上下文。",
                "evidence": dependency_evidence[:5],
            }

        if syntax_evidence:
            return {
                "score": 2,
                "category": "syntax_or_local_error",
                "reason": "编译失败，错误局限在语法、符号拼写或局部代码严谨性问题。",
                "evidence": syntax_evidence[:5],
            }

        fallback_evidence = self._extract_errors(build_log, max_lines=5).splitlines()
        return {
            "score": 2,
            "category": "syntax_or_local_error",
            "reason": "编译失败，但未发现明确依赖/上下文错误，按局部代码错误处理。",
            "evidence": fallback_evidence,
        }

    def _detect_invalid_code_artifact(self, project_root: Path, build_log: str) -> list[str]:
        """检测 Markdown/说明文字等非代码内容是否被写入源码文件。"""
        evidence: list[str] = []
        source_exts = {".kt", ".java", ".xml", ".gradle", ".kts"}
        skip_dirs = {"build", ".gradle", ".idea", ".git"}

        try:
            for path in project_root.rglob("*"):
                if len(evidence) >= 5:
                    break
                if not path.is_file() or path.suffix not in source_exts:
                    continue
                if any(part in skip_dirs for part in path.parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                stripped = text.lstrip()
                if stripped.startswith("```"):
                    evidence.append(f"{path.relative_to(project_root)} starts with Markdown code fence ```")
                elif re.match(r"^(Here is|Below is|Sure[, ]|This code|Explanation:)", stripped, re.IGNORECASE):
                    first_line = stripped.splitlines()[0][:120]
                    evidence.append(f"{path.relative_to(project_root)} starts with prose: {first_line}")
                elif path.suffix in {".kt", ".java", ".gradle", ".kts"} and stripped.startswith("# "):
                    evidence.append(f"{path.relative_to(project_root)} starts with Markdown heading")
        except Exception:
            pass

        lower_log = build_log.lower()
        if "illegal character: '`'" in lower_log or "illegal char <:>" in lower_log:
            evidence.append("compiler reported illegal Markdown-like characters")
        return evidence

    def _find_dependency_context_errors(self, text: str) -> list[str]:
        """匹配依赖、包、类、项目上下文缺失类错误。"""
        patterns = [
            r"could not (find|resolve|get) .+",
            r"failed to resolve .+",
            r"unable to resolve dependency .+",
            r"no matching variant of .+ was found",
            r"module .+ was not found",
            r"package .+ does not exist",
            r"cannot find symbol\s+class\s+\w+",
            r"cannot access class .+",
            r"classnotfoundexception",
            r"noclassdeffounderror",
            r"sdk location not found",
            r"android_home",
            r"android_sdk_root",
            r"sdk\.dir",
            r"unresolved reference: [A-Z][A-Za-z0-9_]*(?:\b|$)",
            r"unresolved reference '[A-Z][A-Za-z0-9_]*'",
            r"unresolved import",
        ]
        return self._matching_lines(text, patterns)

    def _find_syntax_local_errors(self, text: str) -> list[str]:
        """匹配漏分号、括号、局部变量拼写等代码严谨性错误。"""
        patterns = [
            r"\berror:\s*(expecting|expected|unexpected|missing|illegal|modifier|syntax)",
            r"expecting (an element|'.+'|.+)",
            r"unexpected tokens?",
            r"syntax error",
            r"';' expected",
            r"\) expected",
            r"\} expected",
            r"reached end of file while parsing",
            r"variable .+ might not have been initialized",
            r"cannot find symbol\s+(variable|method)\s+\w+",
            r"unresolved reference: [a-z_][A-Za-z0-9_]*",
            r"unresolved reference '[a-z_][A-Za-z0-9_]*'",
            r"type mismatch",
            r"none of the following functions can be called",
            r"too many arguments",
            r"no value passed for parameter",
        ]
        return self._matching_lines(text, patterns)

    def _matching_lines(self, text: str, patterns: list[str], max_lines: int = 20) -> list[str]:
        matches: list[str] = []
        seen: set[str] = set()
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if any(p.search(line) for p in compiled) and line not in seen:
                matches.append(line)
                seen.add(line)
                if len(matches) >= max_lines:
                    break
        return matches

    def _run_gradle(
        self,
        cmd: str,
        project_root: Path,
        timeout: int,
        env: Optional[dict] = None,
    ) -> tuple[str, str, int, float]:
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            elapsed = round(time.time() - t0, 2)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.time() - t0, 2)
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = f"TIMEOUT after {timeout}s"
            rc = -1
        return stdout, stderr, rc, elapsed

    def _detect_required_java_version(self, project_root: Path) -> Optional[int]:
        """从 Gradle 配置中推断项目需要的 Java 主版本（如 21/17/8）。"""
        versions: list[int] = []
        gradle_files = list(project_root.glob("**/*.gradle")) + list(project_root.glob("**/*.gradle.kts"))

        for file_path in gradle_files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for m in re.finditer(r"JavaVersion\.VERSION_(1_\d+|\d+)", text):
                versions.append(self._normalize_java_version_token(m.group(1)))

            for m in re.finditer(r"JavaLanguageVersion\.of\((\d+)\)", text):
                versions.append(int(m.group(1)))

            for m in re.finditer(r"jvmTarget\s*=\s*[\"'](\d+)[\"']", text):
                versions.append(int(m.group(1)))

        if not versions:
            return None
        return max(versions)

    def _normalize_java_version_token(self, token: str) -> int:
        if token.startswith("1_"):
            return int(token.split("_", 1)[1])
        return int(token)

    def _parse_required_java_from_error(self, text: str) -> Optional[int]:
        """从 javac/gradle 报错中提取可能的 Java release 版本。"""
        patterns = [
            r"invalid source release\s*:?\s*(\d+)",
            r"source release\s*(\d+)",
            r"release version\s*(\d+)",
            r"源发行版\D*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    def _build_env_with_java_home(self, java_home: str) -> dict:
        env = os.environ.copy()
        env["JAVA_HOME"] = java_home
        java_bin = str(Path(java_home) / "bin")
        env["PATH"] = java_bin + os.pathsep + env.get("PATH", "")
        return env

    def _find_java_home_for_version(self, version: int) -> Optional[str]:
        """按版本查找可用 JDK 路径，优先环境变量，再尝试常见安装目录。"""
        # 1) 专用环境变量优先。
        env_keys = [
            f"JAVA{version}_HOME",
            f"JDK{version}_HOME",
            f"JAVA_{version}_HOME",
        ]
        for key in env_keys:
            value = os.environ.get(key)
            if value and Path(value).exists():
                return value

        # 2) 复用 JAVA_HOME（若版本匹配）。
        java_home = os.environ.get("JAVA_HOME")
        if java_home and Path(java_home).exists():
            major = self._read_java_major_from_home(java_home)
            if major == version:
                return java_home

        # 3) Windows 常见安装目录扫描。
        if os.name == "nt":
            candidates: list[Path] = []
            for base in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]):
                root = Path(base)
                candidates.extend(root.glob(f"Java/jdk-{version}*"))
                candidates.extend(root.glob(f"Eclipse Adoptium/jdk-{version}*"))
                candidates.extend(root.glob(f"Microsoft/jdk-{version}*"))
            for c in candidates:
                if c.exists() and (c / "bin" / "java.exe").exists():
                    return str(c)

        # 4) macOS: 使用 /usr/libexec/java_home 自动解析版本。
        if sys.platform == "darwin":
            try:
                proc = subprocess.run(
                    ["/usr/libexec/java_home", "-v", str(version)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if proc.returncode == 0:
                    candidate = proc.stdout.strip()
                    if candidate and Path(candidate).exists():
                        return candidate
            except Exception:
                pass

        return None

    def _read_java_major_from_home(self, java_home: str) -> Optional[int]:
        release_file = Path(java_home) / "release"
        if not release_file.exists():
            return None
        try:
            text = release_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'JAVA_VERSION="(\d+)(?:\.\d+)*', text)
            if m:
                return int(m.group(1))
        except Exception:
            return None
        return None

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
