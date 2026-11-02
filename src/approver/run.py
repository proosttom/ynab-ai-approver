"""run / undo / backtest. A live run costs 1–3 YNAB requests."""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import ynab

from .classify import Proposal, candidate, propose
from .config import Policy
from .log import log
from .distill import build_profiles, labels as make_labels
from .policy import decide
from .state import State
from .ynab_client import Txn, Ynab

TRACK_DAYS = 30
WRITE_BATCH = 100  # transactions per bulk PATCH; ~700 in one request made YNAB drop the connection
EPOCH = date(2000, 1, 1)  # without since_date YNAB returns only about the last 12 months


def load_categories(y: Ynab, st: State, force: bool = False) -> dict:
    """Delta-refresh categories at most once a day."""
    sync, cats = st.load("sync", {}), st.load("categories", {})
    if force or not cats or time.time() - sync.get("cat_refreshed", 0) > 86400:
        new, sk = y.categories(sync.get("cat_knowledge") if cats else None)
        cats.update(new)
        sync.update(cat_knowledge=sk, cat_refreshed=time.time())
        st.save("categories", cats)
        st.save("sync", sync)
    return cats


def _patch(p: Proposal, action: str, policy: Policy):
    flag = policy.defaults.flag_auto if action == "auto" else policy.defaults.flag_suggest
    memo = f"{p.txn.memo} [AI: {p.reason}]".strip()[:200]
    return ynab.SaveTransactionWithIdOrImportId(
        id=p.txn.id, category_id=p.category_id, approved=action == "auto",
        flag_color=ynab.TransactionFlagColor(flag), memo=memo)


def _corrections(txns: list[Txn], handled: dict, sync: dict, profiles: dict, st: State) -> None:
    """Human edits to AI-handled transactions show up in the delta feed."""
    corrected = set(sync.get("corrected_payees", []))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRACK_DAYS)).isoformat()
    for t in txns:
        h = handled.get(t.id)
        if not h:
            continue
        if t.deleted:
            handled.pop(t.id)
        elif t.approved and t.category_id != h["category_id"]:
            st.audit(event="corrected", txn_id=t.id, payee=t.payee_name, ai=h["category_id"], human=t.category_id)
            corrected.add(t.payee)
            handled.pop(t.id)
        elif t.approved and h["action"] == "suggest":
            st.audit(event="accepted", txn_id=t.id)
            corrected.discard(t.payee)  # the AI got this payee right again
            handled.pop(t.id)
        else:
            continue
        if t.approved and t.payee and t.category_id:  # keep payee history current
            p = profiles["payees"].setdefault(t.payee, {"cats": {}, "recent": []})
            p["cats"][t.category_id] = p["cats"].get(t.category_id, 0) + 1
    for k in [k for k, h in handled.items() if h["ts"] < cutoff]:
        handled.pop(k)
    sync["corrected_payees"] = sorted(c for c in corrected if c)


def run(y: Ynab, st: State, policy: Policy, llm, dry_run: bool, all_unapproved: bool = False) -> Counter:
    """all_unapproved: also take every unapproved transaction not handled yet (old ones, earlier skips)."""
    profiles = st.load("profiles")
    if not profiles:
        raise SystemExit("No state/profiles.json: run `approver distill` first.")
    cats = load_categories(y, st)
    labels = make_labels(cats)
    sync, handled = st.load("sync", {}), st.load("ai_txns", {})

    if dry_run:  # never advance the delta cursor in dry-run
        txns, _ = y.transactions(since=EPOCH, type="unapproved")  # all unapproved, any date
        seen = set(st.load("dry_seen", []))
        pending = [t for t in txns if candidate(t) and t.id not in seen and t.id not in handled and "[AI:" not in t.memo]
    else:
        k = sync.get("txn_knowledge")
        txns, sk = y.transactions(knowledge=k) if k else y.transactions(since=EPOCH, type="unapproved")  # first run: all
        _corrections(txns, handled, sync, profiles, st)
        if all_unapproved and k:
            txns, _ = y.transactions(since=EPOCH, type="unapproved")
        pending = [t for t in txns if candidate(t) and t.id not in handled and "[AI:" not in t.memo]

    stats = Counter()
    if pending:
        props = propose(pending, policy, profiles, cats, labels, llm, set(sync.get("corrected_payees", [])))
        decided = []
        for p in props:
            action = decide(p, policy, labels.get(p.category_id, ""), stats["auto"])
            stats[action] += 1
            decided.append((p, action))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i in range(0, len(decided), WRITE_BATCH):  # record each batch as soon as YNAB accepted it
            batch = decided[i:i + WRITE_BATCH]
            if not dry_run:
                y.update([_patch(p, action, policy) for p, action in batch if action in ("auto", "suggest")])
            _record(batch, st, labels, handled, now, dry_run)
            if not dry_run:  # partial progress survives a failure later on; the cursor only moves at the end
                st.save("ai_txns", handled)
                st.save("sync", sync)
                st.save("profiles", profiles)
        if dry_run:
            st.save("dry_seen", sorted(seen | {t.id for t in pending}))

    if not dry_run:
        sync["txn_knowledge"] = sk
        st.save("sync", sync)
        st.save("ai_txns", handled)
        st.save("profiles", profiles)
    stats["ynab_requests"] = y.used
    log.info("%srun: %d pending, %s", "[dry] " if dry_run else "", len(pending), dict(stats))
    return stats


def _record(batch, st: State, labels: dict, handled: dict, now: str, dry_run: bool) -> None:
    for p, action in batch:
        t = p.txn
        st.audit(event="decision", dry_run=dry_run, txn_id=t.id, date=t.date, payee=t.payee_name, amount=t.eur,
                 action=action, source=p.source, category=labels.get(p.category_id, p.category_id),
                 category_id=p.category_id, confidence=round(p.confidence, 3), reason=p.reason,
                 anomalies=p.anomalies,
                 before={"category_id": t.category_id, "flag": t.flag, "memo": t.memo, "approved": t.approved})
        log.info("%s%-7s %s | %-30.30s | %9.2f | %s (%s, %.2f)%s | %s", "[dry] " if dry_run else "", action,
                 t.date, t.payee_name, t.eur, labels.get(p.category_id, "-"), p.source, p.confidence,
                 f" [{', '.join(p.anomalies)}]" if p.anomalies else "", p.reason)
        if not dry_run and action in ("auto", "suggest"):
            handled[t.id] = {"category_id": p.category_id, "action": action, "ts": now}


def absorb_reviews(txns: list[Txn], st: State) -> Counter:
    """Called by distill: approved AI-handled transactions are now part of the rebuilt profiles,
    so record them as accepted/corrected, stop tracking them and clear the correction marks."""
    handled, sync, stats = st.load("ai_txns", {}), st.load("sync", {}), Counter()
    for t in txns:
        h = handled.get(t.id)
        if h and t.approved and not t.deleted:
            event = "accepted" if t.category_id == h["category_id"] else "corrected"
            st.audit(event=event, txn_id=t.id, payee=t.payee_name, ai=h["category_id"], human=t.category_id)
            stats[event] += 1
            handled.pop(t.id)
    sync["corrected_payees"] = []
    st.save("ai_txns", handled)
    st.save("sync", sync)
    return stats


def undo(y: Ynab, st: State, since: str) -> int:
    """Revert live AI decisions since an ISO date. YNAB can't un-set a category, so an originally
    uncategorized transaction keeps its category but becomes unapproved and unflagged again."""
    first = {}
    for e in st.audit_entries():
        if e.get("event") == "decision" and not e["dry_run"] and e["action"] in ("auto", "suggest") and e["ts"] >= since:
            first.setdefault(e["txn_id"], e)  # earliest before-state wins
    patches = [ynab.SaveTransactionWithIdOrImportId(
        id=tid, approved=e["before"]["approved"], memo=e["before"]["memo"] or "",
        flag_color=ynab.TransactionFlagColor(e["before"]["flag"] or ""),
        **({"category_id": e["before"]["category_id"]} if e["before"]["category_id"] else {}))
        for tid, e in first.items()]
    y.update(patches)
    handled = st.load("ai_txns", {})
    for tid in first:
        handled.pop(tid, None)
        st.audit(event="undone", txn_id=tid)
    st.save("ai_txns", handled)
    return len(patches)


def backtest(st: State, policy: Policy, llm, sample: int = 100) -> dict:
    """Hold out the last 30 days of cached history, replay rules + AI + policy with labels hidden."""
    hist, cats = st.load("history"), st.load("categories")
    if not hist:
        raise SystemExit("No state/history.json: run `approver distill` first.")
    txns = [Txn(**t) for t in hist]
    cutoff = str(date.today() - timedelta(days=30))
    train = [t for t in txns if t.date < cutoff]
    test = [t for t in txns if t.date >= cutoff and t.approved and not t.transfer and not t.subs
            and t.amount < 0 and t.category_id in cats][:sample]
    profiles = build_profiles(train, cats, date.fromisoformat(cutoff))
    labels = make_labels(cats)
    truth = {t.id: t.category_id for t in test}
    blind = [Txn(**{**asdict(t), "approved": False, "category_id": None}) for t in test]
    res = defaultdict(Counter)
    auto = 0
    for p in propose(blind, policy, profiles, cats, labels, llm, set()):
        action = decide(p, policy, labels.get(p.category_id, ""), auto)
        auto += action == "auto"
        r = res[labels.get(truth[p.txn.id], "?")]
        r["n"] += 1
        r[action] += 1
        r[f"{action}_ok"] += p.category_id == truth[p.txn.id]
    return dict(res)
