"""Human-readable log: <state dir>/approver.log (rotated), one block per command. Tokens are redacted."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

log = logging.getLogger("approver")


class _RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        out = super().format(record)  # includes the traceback, if any
        for s in self.secrets:
            out = out.replace(s, "***")
        return out


def setup(state_dir: Path, secrets: list[str]) -> Path:
    path = Path(state_dir) / "approver.log"
    if not any(getattr(h, "baseFilename", None) == str(path.resolve()) for h in log.handlers):
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(_RedactingFormatter(secrets))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    return path
