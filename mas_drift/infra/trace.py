"""JSONL-трассы прогонов: по одному файлу на run_id.

Пока прогон идёт, трасса пишется в <run_id>.jsonl.part (с flush после каждой строки). Завершённый
прогон получает запись run_end и атомарно переименовывается в <run_id>.jsonl. Прерванный
инфраструктурной ошибкой остаётся .part с записью run_aborted и будет перезапущен.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, default=str)


def trace_path(traces_dir: Path, run_id: str) -> Path:
    return Path(traces_dir) / f"{run_id}.jsonl"


def read_trace(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def is_completed(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        records = read_trace(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(records) and records[-1].get("type") == "run_end"


class TraceWriter:
    def __init__(self, path: Path, header: dict[str, Any]):
        self.path = Path(path)
        self.part = self.path.with_name(self.path.name + ".part")
        self.header = header
        self._fh: TextIO | None = None
        self._step = 0
        self.finished = False

    def __enter__(self) -> "TraceWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.part, "w", encoding="utf-8")
        self._write({"type": "run_start", **self.header, "started_at": _now()})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.finished:
            self.abort(f"{exc_type.__name__}: {exc}" if exc_type else "closed without finish")
        return False

    def _write(self, record: dict) -> None:
        assert self._fh is not None, "TraceWriter не открыт"
        self._fh.write(_dumps(record) + "\n")
        self._fh.flush()

    def step(self, agent: str, **data: Any) -> int:
        self._step += 1
        self._write({"type": "step", "run_id": self.header.get("run_id"), "agent": agent,
                     "step": self._step, "ts": _now(), **data})
        return self._step

    def event(self, kind: str, **data: Any) -> None:
        self._write({"type": kind, "run_id": self.header.get("run_id"), "ts": _now(), **data})

    def finish(self, **summary: Any) -> None:
        self._write({"type": "run_end", "run_id": self.header.get("run_id"), "status": "completed",
                     "ended_at": _now(), **summary})
        self._close()
        os.replace(self.part, self.path)
        self.finished = True

    def abort(self, error: str) -> None:
        if self._fh is not None:
            self._write({"type": "run_aborted", "run_id": self.header.get("run_id"), "error": error,
                         "ended_at": _now()})
            self._close()
        self.finished = True

    def _close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
