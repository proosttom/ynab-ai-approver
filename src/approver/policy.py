"""The deterministic gate: AI proposes, policy decides."""
from __future__ import annotations

from .classify import Proposal
from .config import Policy


def decide(p: Proposal, policy: Policy, label: str, auto_so_far: int) -> str:
    """Returns 'auto', 'suggest', 'skip' or 'defer' (auto-eligible, but the per-run cap is reached:
    left untouched so a later run can auto-approve it)."""
    if not p.category_id:
        return "skip"
    mode, min_conf, max_amount = policy.for_category(label)
    if mode == "off":
        return "skip"
    blocked = set(p.anomalies) & set(policy.never_auto_when)
    if (mode == "auto" and not blocked and p.confidence >= min_conf
            and p.txn.eur <= max_amount * (1 + policy.defaults.amount_tolerance)):
        return "auto" if auto_so_far < policy.defaults.max_auto_per_run else "defer"
    return "suggest"
