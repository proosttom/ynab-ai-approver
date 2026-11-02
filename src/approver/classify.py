"""Proposals: payee rules first, then one batched LLM call per chunk (grouped by candidate category)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Policy
from .ynab_client import Txn

SYSTEM = (Path(__file__).parent / "prompts" / "classify.md").read_text()
CHUNK = 40
SCHEMA = {
    "type": "object", "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "required": ["id", "category_id", "confidence", "reason"],
        "properties": {"id": {"type": "string"}, "category_id": {"type": "string"},
                       "confidence": {"type": "number"}, "reason": {"type": "string"}}}}},
}


@dataclass
class Proposal:
    txn: Txn
    category_id: str | None
    confidence: float = 0.0
    reason: str = ""
    source: str = "ai"  # rule | ai
    anomalies: list[str] = field(default_factory=list)


def _system(policy: Policy) -> str:
    if not policy.instructions:
        return SYSTEM
    return SYSTEM + "\nFamily rules from the policy (trusted; they override payee history and profiles):\n" + \
        "".join(f"- {i}\n" for i in policy.instructions)


def candidate(t: Txn) -> bool:
    return not (t.deleted or t.approved or t.transfer) and t.amount < 0


def _history(t: Txn, profiles: dict) -> dict[str, int]:
    return (profiles["payees"].get(t.payee) or {}).get("cats", {})


def anomalies(p: Proposal, profiles: dict, corrected_payees: set[str]) -> list[str]:
    t, out = p.txn, []
    hist = _history(t, profiles)
    if not hist:
        out.append("new_payee")
    elif sum(hist.values()) >= 3 and p.category_id != max(hist, key=hist.get):
        out.append("category_drift")
    prof = profiles["categories"].get(p.category_id or "")
    if prof and t.eur > prof["p95"] * 1.5:
        out.append("amount_outlier")
    if t.subs:
        out.append("split")
    if t.payee in corrected_payees and p.source != "rule":  # a rule is the family's explicit choice
        out.append("corrected_payee")
    return out


def propose(pending: list[Txn], policy: Policy, profiles: dict, categories: dict, labels: dict,
            llm, corrected_payees: set[str]) -> list[Proposal]:
    by_label = {v: k for k, v in labels.items()}
    rules = {r.payee: by_label.get(r.category) for r in policy.rules}
    out, rest = [], []
    for t in pending:
        if t.subs:  # splits are left to humans in v1
            out.append(Proposal(t, None, reason="split transaction", source="skip"))
        elif rules.get(t.payee):
            out.append(Proposal(t, rules[t.payee], 1.0, "payee rule", "rule"))
        else:
            rest.append(t)

    allowed = {cid for cid, c in categories.items() if not c["deleted"]}
    def desc(cid):
        cp = policy.categories.get(labels[cid])
        return (cp.description if cp else "") or profiles["categories"].get(cid, {}).get("description", "")

    cat_list = [{"id": cid, "label": labels[cid], "description": desc(cid)} for cid in sorted(allowed, key=labels.get)]

    def cand(t):  # most common historical category: groups similar txns into one chunk
        h = _history(t, profiles)
        return max(h, key=h.get) if h else ""

    rest.sort(key=cand)
    for i in range(0, len(rest), CHUNK):
        chunk = rest[i:i + CHUNK]
        cands = {cand(t) for t in chunk} & set(profiles["categories"])
        user = json.dumps({
            "categories": cat_list,
            "profiles": [{"id": c, **{k: profiles["categories"][c][k] for k in ("label", "payees", "p50", "p95")}}
                         for c in sorted(cands)],
            "transactions": [{"id": t.id, "date": t.date, "payee": t.payee, "amount": t.eur,
                              "account": t.account, "memo": t.memo,
                              "payee_history": {labels.get(c, c): n for c, n in _history(t, profiles).items()}}
                             for t in chunk],
        }, ensure_ascii=False)
        res = {r["id"]: r for r in llm(_system(policy), user, SCHEMA).get("results", [])}
        for t in chunk:
            r = res.get(t.id)
            if not r or r["category_id"] not in allowed:
                out.append(Proposal(t, None, reason="no valid AI answer", source="skip"))
            else:
                conf = max(0.0, min(1.0, float(r["confidence"])))
                out.append(Proposal(t, r["category_id"], conf, str(r["reason"])[:60]))

    for p in out:
        if p.category_id:
            p.anomalies = anomalies(p, profiles, corrected_payees)
    return out
