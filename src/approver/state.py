"""Small JSON/JSONL files under the state dir."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def load(self, name: str, default=None):
        p = self.root / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else default

    def save(self, name: str, obj) -> None:
        p = self.root / f"{name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
        tmp.replace(p)

    def audit(self, **entry) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        with (self.root / "audit.jsonl").open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def audit_entries(self) -> list[dict]:
        p = self.root / "audit.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
