from __future__ import annotations

import json
import threading
from pathlib import Path

from .schemas import HistoryRecord


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self, *, limit: int | None = None) -> list[HistoryRecord]:
        with self._lock:
            records = self._read()
        records.sort(key=lambda item: item.updated_at, reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def upsert(self, record: HistoryRecord) -> None:
        with self._lock:
            records = [item for item in self._read() if item.job_id != record.job_id]
            records.append(record)
            self._write(records)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            records = self._read()
            kept = [item for item in records if item.job_id != job_id]
            if len(kept) == len(records):
                return False
            self._write(kept)
            return True

    def clear(self) -> int:
        with self._lock:
            count = len(self._read())
            self._write([])
            return count

    def _read(self) -> list[HistoryRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        records: list[HistoryRecord] = []
        for item in data:
            try:
                records.append(HistoryRecord.model_validate(item))
            except ValueError:
                continue
        return records

    def _write(self, records: list[HistoryRecord]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps([record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
