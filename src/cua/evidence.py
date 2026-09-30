"""Run evidence: a structured JSONL event log plus richer artifacts (masked screenshots,
redacted DOM snapshots, rendered observations) in one directory per run.

Every write goes through the redactor. Playwright traces and HAR files are deliberately
*not* captured: they contain unredacted request bodies and DOM, and there is no safe way
to scrub them after the fact.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import Redactor


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


class Evidence:
    def __init__(self, root: Path, run_id: str, redactor: Redactor) -> None:
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screens").mkdir(exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._t0 = time.monotonic()
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, kind: str, /, **fields: Any) -> dict:
        self._seq += 1
        rec = {
            "seq": self._seq,
            "t": round(time.monotonic() - self._t0, 3),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": kind,
            **self.redactor.obj(fields),
        }
        self._log.write(json.dumps(rec, default=str) + "\n")
        self._log.flush()
        return rec

    def path(self, name: str) -> Path:
        return self.dir / name

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.dir))

    def write_text(self, name: str, content: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.redactor.text(content), encoding="utf-8")
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str) + "\n", encoding="utf-8")
        return p

    def close(self) -> None:
        self._log.close()
