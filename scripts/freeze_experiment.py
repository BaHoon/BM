#!/usr/bin/env python3
"""Create a reproducibility manifest without recording API secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_DIRS = {
    ".git", ".gradle", ".idea", ".kotlin", "build", ".cxx", "__pycache__",
}


def command(*args: str) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return (result.stdout or result.stderr).strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    data_dir = root / args.data_dir
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    entries: list[tuple[str, str, int]] = []
    aggregate = hashlib.sha256()
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(data_dir)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        digest = sha256(path)
        size = path.stat().st_size
        entries.append((rel.as_posix(), digest, size))
        aggregate.update(rel.as_posix().encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")

    checksum_path = output_dir / "dataset_manifest.sha256"
    checksum_path.write_text(
        "".join(f"{digest}  {rel}\n" for rel, digest, _ in entries),
        encoding="utf-8",
    )

    scoring_files = [
        "scripts/Env_Manager.py", "scripts/Evaluator.py",
        "scripts/appium_runner.py", "scripts/tools/level2_utils.py",
        "scripts/logger.py", "scripts/Experiment_Launcher.py",
    ]
    manifest = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": command("git", "-C", str(root), "rev-parse", "HEAD"),
        "git_status": command("git", "-C", str(root), "status", "--porcelain"),
        "git_diff_sha256": hashlib.sha256(
            command("git", "-C", str(root), "diff", "--binary").encode("utf-8")
        ).hexdigest(),
        "dataset_root": str(data_dir),
        "dataset_file_count": len(entries),
        "dataset_total_bytes": sum(size for _, _, size in entries),
        "dataset_aggregate_sha256": aggregate.hexdigest(),
        "scoring_file_sha256": {
            name: sha256(root / name) for name in scoring_files if (root / name).exists()
        },
        "platform": platform.platform(),
        "python": platform.python_version(),
        "java_versions": {
            "default": command("java", "-version"),
            "java_11": command("/usr/lib/jvm/java-11-openjdk-amd64/bin/java", "-version"),
            "java_17": command("/usr/lib/jvm/java-17-openjdk-amd64/bin/java", "-version"),
            "java_21": command("/usr/lib/jvm/java-21-openjdk-amd64/bin/java", "-version"),
        },
        "android": {
            "sdk_root": os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT"),
            "adb": command("adb", "version"),
            "ndk_27_source_properties_sha256": sha256(
                Path("/home/liuyihan/android-sdk/ndk/27.0.12077973/source.properties")
            ) if Path("/home/liuyihan/android-sdk/ndk/27.0.12077973/source.properties").exists() else None,
        },
        "api_configuration": {
            "benchmark_base_url": os.getenv("BENCHMARK_BASE_URL"),
            "benchmark_model": os.getenv("BENCHMARK_MODEL"),
            "vlm_base_url": os.getenv("VLM_BASE_URL"),
            "vlm_model": os.getenv("VLM_MODEL"),
            "benchmark_key_present": bool(os.getenv("BENCHMARK_API_KEY")),
            "vlm_key_present": bool(os.getenv("VLM_API_KEY")),
        },
        "rubric": {
            "level1": "4 compile/no introduced warnings; 3 compile/introduced warnings; 2 local syntax; 1 dependency/context; 0 invalid code",
            "level2": "2 exact target node; 1 no node and >=1 core-file overlap; 0 no node and no overlap; skipped if L1 fails",
            "level3": "1 all binary visual checks pass; 0 otherwise; only evaluated when L2=2",
        },
    }
    (output_dir / "freeze_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "pip_freeze.txt").write_text(command("python", "-m", "pip", "freeze") + "\n")
    print(json.dumps({
        "output_dir": str(output_dir),
        "dataset_file_count": len(entries),
        "dataset_aggregate_sha256": aggregate.hexdigest(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
