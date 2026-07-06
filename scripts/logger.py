#!/usr/bin/env python3
"""
Logger - 实验全过程结构化记录

职责：
  - 每次 run（一个任务 × 一次尝试）生成一条 ExperimentRecord。
  - 将记录追加写入 results/raw_data.jsonl（每行一条 JSON），
    便于后续 pandas / SQL 分析。
  - 同时维护 results/raw_data_summary.json（聚合统计，方便快速查看进度）。
  - 提供 Python logging 标准输出到 console + 文件（results/experiment.log）。

数据字段（ExperimentRecord）：
  run_id, timestamp, app_name, task_id,
  model, strategy, attempt,
  csr (bool), vsm (bool), vlm_score (0-10),
  latency_s (float), error_category (str|None),
  retrieved_files (list[str]),
  domain (str|None)
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ExperimentRecord:
    """一次任务运行的完整记录。"""
    run_id:           str            = field(default_factory=lambda: uuid.uuid4().hex[:10])
    timestamp:        str            = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    app_name:         str            = ""
    task_id:          str            = ""
    model:            str            = ""
    strategy:         str            = ""
    attempt:          int            = 1          # 第几次尝试（Pass@k 用）
    csr:              Optional[bool] = None        # Compilation Success Rate
    vsm:              Optional[bool] = None        # Visual / Script Match
    vlm_score:        Optional[int]  = None        # VLM 打分 0-10（仅视觉任务）
    latency_s:        float          = 0.0         # 端到端耗时（秒）
    error_category:   Optional[str]  = None        # "logic_error" | "missing_context" | "vague_req" | None
    retrieved_files:  list           = field(default_factory=list)
    domain:           Optional[str]  = None        # 任务 domain 类别（24类之一）
    extra:            dict           = field(default_factory=dict)  # 其他扩展字段

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
#  ExperimentLogger
# --------------------------------------------------------------------------- #

class ExperimentLogger:
    """
    线程安全的实验日志管理器。

    用法：
        logger = ExperimentLogger(results_dir="results")
        record = ExperimentRecord(app_name="app_foodyou", task_id="task_001_theme", ...)
        logger.log(record)
        logger.print_summary()
    """

    _JSONL_FILE    = "raw_data.jsonl"
    _SUMMARY_FILE  = "raw_data_summary.json"
    _LOG_FILE      = "experiment.log"

    def __init__(
        self,
        results_dir: str | Path = "results",
        report_md: str | Path | None = None,
    ):
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.report_md = None
        if report_md:
            report_path = Path(report_md)
            self.report_md = report_path if report_path.is_absolute() else self.results_dir / report_path
            self.report_md.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_markdown_report()

        self._lock  = threading.Lock()
        self._cache: list[dict] = []           # 内存中保留本次进程的所有记录

        self._setup_python_logger()

    # ----------------------------------------------------------------------- #
    #  公开接口
    # ----------------------------------------------------------------------- #

    def log(self, record: ExperimentRecord) -> None:
        """将一条记录写入 JSONL 文件并更新摘要。"""
        row = record.to_dict()
        with self._lock:
            self._cache.append(row)
            self._append_jsonl(row)
            self._update_summary()
            self._append_markdown(row)

        status = "✓ CSR+VSM" if record.csr and record.vsm else \
                 "~ CSR only" if record.csr else "✗ FAIL"
        self.py_logger.info(
            "[%s/%s] model=%s strategy=%s attempt=%d %s  latency=%.1fs",
            record.app_name, record.task_id,
            record.model, record.strategy, record.attempt,
            status, record.latency_s,
        )

    def start_run(
        self,
        app_name: str,
        task_id: str,
        model: str,
        strategy: str,
        attempt: int = 1,
        domain: Optional[str] = None,
    ) -> "_RunContext":
        """
        返回一个上下文管理器，自动计时并在退出时提交记录。

        用法：
            with logger.start_run("app_foodyou", "task_001", "gpt-4o", "ReAct") as run:
                run.record.csr = True
                run.record.vsm = False
        """
        return _RunContext(self, app_name, task_id, model, strategy, attempt, domain)

    def print_summary(self) -> None:
        """在 console 打印当前进程内所有记录的聚合摘要。"""
        if not self._cache:
            print("[Logger] No records yet.")
            return
        total  = len(self._cache)
        csr_ok = sum(1 for r in self._cache if r.get("csr"))
        vsm_ok = sum(1 for r in self._cache if r.get("vsm"))
        print(
            f"\n{'='*60}\n"
            f"  Experiment Summary  (this session)\n"
            f"{'='*60}\n"
            f"  Total runs  : {total}\n"
            f"  CSR pass    : {csr_ok}/{total}  ({100*csr_ok/total:.1f}%)\n"
            f"  VSM pass    : {vsm_ok}/{total}  ({100*vsm_ok/total:.1f}%)\n"
            f"{'='*60}\n"
        )

    # ----------------------------------------------------------------------- #
    #  内部方法
    # ----------------------------------------------------------------------- #

    def _append_jsonl(self, row: dict) -> None:
        jsonl_path = self.results_dir / self._JSONL_FILE
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _ensure_markdown_report(self) -> None:
        if self.report_md is None or self.report_md.exists():
            return
        header = (
            "# Batch Experiment Results\n\n"
            "This file is updated after every task, so it is safe to inspect while the batch is running.\n\n"
            "| Time (UTC) | Task | Model | Strategy | L1 | L2 | L3 | Total | Status | Error / reason | Latency |\n"
            "|---|---|---|---|---:|---:|---:|---:|---|---|---:|\n"
        )
        self.report_md.write_text(header, encoding="utf-8")

    @staticmethod
    def _md_cell(value) -> str:
        if value is None:
            return "—"
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()

    def _append_markdown(self, row: dict) -> None:
        if self.report_md is None:
            return
        extra = row.get("extra") or {}
        l1, l2, l3 = (extra.get("level1_score"), extra.get("level2_score"), extra.get("level3_score"))
        total = extra.get("total_score")
        if l1 == 4 and l2 == 2 and l3 == 1:
            status = "PASS"
        elif row.get("error_category"):
            status = "FAIL"
        else:
            status = "PARTIAL"
        reason = (
            extra.get("exception") or extra.get("agent_error") or
            extra.get("level3_reason") or extra.get("level2_reason") or
            extra.get("level1_reason") or row.get("error_category") or ""
        )
        cells = [
            row.get("timestamp", ""),
            f"{row.get('app_name', '')}/{row.get('task_id', '')}",
            row.get("model", ""), row.get("strategy", ""),
            l1, l2, l3, total, status, reason, f"{row.get('latency_s', 0):.2f}s",
        ]
        with self.report_md.open("a", encoding="utf-8") as f:
            f.write("| " + " | ".join(self._md_cell(v) for v in cells) + " |\n")

    def _update_summary(self) -> None:
        """重写聚合摘要 JSON（基于内存 cache，线程安全由调用者保证）。"""
        total  = len(self._cache)
        csr_ok = sum(1 for r in self._cache if r.get("csr"))
        vsm_ok = sum(1 for r in self._cache if r.get("vsm"))

        summary = {
            "last_updated":  datetime.now(timezone.utc).isoformat(),
            "total_runs":    total,
            "csr_pass":      csr_ok,
            "vsm_pass":      vsm_ok,
            "csr_rate":      round(csr_ok / total, 4) if total else 0,
            "vsm_rate":      round(vsm_ok / total, 4) if total else 0,
            # 按 model 分组
            "by_model":      _group_by(self._cache, "model", csr_ok, vsm_ok),
            # 按 strategy 分组
            "by_strategy":   _group_by(self._cache, "strategy", csr_ok, vsm_ok),
        }
        summary_path = self.results_dir / self._SUMMARY_FILE
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

    def _setup_python_logger(self) -> None:
        self.py_logger = logging.getLogger("experiment")
        if self.py_logger.handlers:
            return                                     # 避免重复添加 handler
        self.py_logger.setLevel(logging.DEBUG)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # 控制台 handler
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        self.py_logger.addHandler(ch)

        # 文件 handler
        log_path = self.results_dir / self._LOG_FILE
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        self.py_logger.addHandler(fh)


# --------------------------------------------------------------------------- #
#  RunContext（上下文管理器）
# --------------------------------------------------------------------------- #

class _RunContext:
    """通过 with 语句自动计时、自动提交记录。"""

    def __init__(
        self,
        logger: ExperimentLogger,
        app_name: str,
        task_id:  str,
        model:    str,
        strategy: str,
        attempt:  int,
        domain:   Optional[str],
    ):
        self._logger = logger
        self.record  = ExperimentRecord(
            app_name=app_name,
            task_id=task_id,
            model=model,
            strategy=strategy,
            attempt=attempt,
            domain=domain,
        )
        self._t0: float = 0.0

    def __enter__(self) -> "_RunContext":
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.record.latency_s = round(time.time() - self._t0, 2)
        if exc_type is not None:
            # 异常时也要记录，标记为失败
            self.record.error_category = self.record.error_category or "exception"
            self.record.extra["exception"] = str(exc_val)
            self._logger.py_logger.error("Exception in run: %s", exc_val)
        self._logger.log(self.record)
        return False  # 不吞异常


# --------------------------------------------------------------------------- #
#  工具函数
# --------------------------------------------------------------------------- #

def _group_by(records: list[dict], key: str, _csr_total: int, _vsm_total: int) -> dict:
    """按某个字段分组，统计每组的 CSR / VSM 通过数。"""
    groups: dict[str, dict] = {}
    for r in records:
        val = r.get(key, "unknown")
        if val not in groups:
            groups[val] = {"total": 0, "csr": 0, "vsm": 0}
        groups[val]["total"] += 1
        if r.get("csr"):
            groups[val]["csr"] += 1
        if r.get("vsm"):
            groups[val]["vsm"] += 1
    # 计算比率
    for g in groups.values():
        t = g["total"] or 1
        g["csr_rate"] = round(g["csr"] / t, 4)
        g["vsm_rate"] = round(g["vsm"] / t, 4)
    return groups
