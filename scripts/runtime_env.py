#!/usr/bin/env python3
"""Runtime environment helpers for server-side Android execution."""

import os
from pathlib import Path
from typing import Optional


def _prepend_path(path: Path) -> None:
    if not path.exists():
        return
    path_str = str(path)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if path_str not in parts:
        os.environ["PATH"] = path_str + (os.pathsep + current if current else "")


def _add_no_proxy(values: list[str]) -> None:
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        parts = [p.strip() for p in current.split(",") if p.strip()]
        changed = False
        for value in values:
            if value not in parts:
                parts.append(value)
                changed = True
        if changed or not current:
            os.environ[key] = ",".join(parts)


def ensure_android_runtime_env() -> Optional[str]:
    """Prefer a user-local Android SDK and protect localhost Appium traffic."""
    explicit = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    mnt_user_dir = Path("/mnt/mybook") / Path.home().name
    candidates.extend([
        mnt_user_dir / "android-sdk-wrapper",
        mnt_user_dir / "android-sdk-runtime",
        Path.home() / "android-sdk",
        Path("/opt/android-sdk"),
        Path("/usr/local/android-sdk"),
        Path("/usr/lib/android-sdk"),
    ])

    sdk_home = None
    for candidate in candidates:
        if (candidate / "platform-tools" / "adb").exists():
            sdk_home = candidate
            break

    if sdk_home:
        os.environ["ANDROID_HOME"] = str(sdk_home)
        os.environ["ANDROID_SDK_ROOT"] = str(sdk_home)
        for child in (
            sdk_home / "platform-tools",
            sdk_home / "cmdline-tools" / "latest" / "bin",
            sdk_home / "emulator",
        ):
            _prepend_path(child)

    if not os.environ.get("ANDROID_AVD_HOME"):
        mnt_avd_home = mnt_user_dir / "android-avd"
        if mnt_avd_home.exists():
            os.environ["ANDROID_AVD_HOME"] = str(mnt_avd_home)

    if mnt_user_dir.exists():
        adb_server_port = os.environ.get("BM_ADB_SERVER_PORT", "5047")
        os.environ.setdefault("ADB_SERVER_PORT", adb_server_port)
        os.environ.setdefault("ANDROID_ADB_SERVER_PORT", os.environ["ADB_SERVER_PORT"])
        os.environ.setdefault("ADB_LOCAL_TRANSPORT_MAX_PORT", "5554")
        os.environ.setdefault("ADB_TCP_DEVICE", "127.0.0.1:5555")
        os.environ.setdefault("APPIUM_PORT", "4725")
        os.environ.setdefault("APPIUM_SYSTEM_PORT", "8202")
        os.environ.setdefault("ADB_EXEC_TIMEOUT", "180000")
        os.environ.setdefault("UIAUTOMATOR2_SERVER_LAUNCH_TIMEOUT", "180000")
        os.environ.setdefault("UIAUTOMATOR2_SERVER_INSTALL_TIMEOUT", "180000")

    _add_no_proxy(["localhost", "127.0.0.1", "::1"])
    return str(sdk_home) if sdk_home else None
