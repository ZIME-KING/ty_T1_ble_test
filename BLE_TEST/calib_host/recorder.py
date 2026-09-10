# -*- coding: utf-8 -*-
"""校准记录：按日期追加 CSV(utf-8-sig，Excel 可直接打开)。"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

FIELDS = ["time", "sn", "result", "note", "attempt",
          "std_v", "std_a", "std_p",
          "meas_v", "meas_a", "meas_p",
          "iac0", "uc0", "pac0",
          "iac", "uc", "pac"]


class Recorder:
    def __init__(self, log_dir: str | Path = "calib_logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._file: Path | None = None

    def _ensure(self):
        d = datetime.now().strftime("%Y%m%d")
        path = self.log_dir / f"calib_{d}.csv"
        new = not path.exists()
        self._file = path
        if new:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(FIELDS)
        return path

    def append(self, rec: dict):
        path = self._ensure()
        row = [rec.get(k, "") for k in FIELDS]
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)

    def current_path(self) -> Path | None:
        return self._file
