from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from approver import distill as D
from approver.classify import propose
from approver.config import Policy, Settings
from approver.policy import decide
from approver.run import run
from approver.state import State
from approver.ynab_client import RateBudgetExceeded, Txn, Ynab

G, R, M = (f"0000000{i}-0000-4000-8000-000000000000" for i in (1, 2, 3))
CATS = {G: {"name": "Groceries", "group": "Living", "deleted": False},
        R: {"name": "Restaurants", "group": "Fun", "deleted": False},
        M: {"name": "Medical", "group": "Health", "deleted": False}}
LAB = D.labels(CATS)


def tx(i, payee, cat, eur, days_ago=10, approved=True, payee_name=None, **kw):
    return Txn(id=str(i), date=str(date.today() - timedelta(days=days_ago)), amount=-int(eur * 1000),
               payee_id=payee, payee_name=payee_name or payee.title(), account="Joint", memo="", category_id=cat,
               approved=approved, flag="", transfer=False, deleted=False, **kw)


HIST = ([tx(i, "colruyt", G, 40 + i) for i in range(12)]
        + [tx(100 + i, "bistro", R, 30, days_ago=200) for i in range(4)]
        + [tx(200, "bistro", G, 30, days_ago=10), tx(300, "doc", M, 50), tx(301, "doc", M, 50)])


def test_distill_policy_and_rules():
    prof = D.build_profiles(HIST, CATS)
    draft = D.derive_policy(prof)
    assert draft["categories"]["Groceries"]["mode"] == "auto"
    assert draft["categories"]["Medical"]["mode"] == "off"  # n < 3 and sensitive
    rules = {r["payee"]: r["category"] for r in draft["rules"]}
    assert rules == {"colruyt": "Groceries"}  # bistro: recent txn went elsewhere
    assert "Groceries" in D.render_yaml(draft, prof)
    assert {c["min_confidence"] for c in draft["categories"].values()} == {0.85}


def test_payees_ignore_case_and_spacing():
    hist = [tx(i, "x", G, 40, payee_name=n) for i, n in
            enumerate(["COLRUYT  ST K", "Colruyt St K", "colruyt st k"])]
    hist = [Txn(**{**t.__dict__, "payee_id": f"id{i}"}) for i, t in enumerate(hist)]  # 3 different YNAB payees
    prof = D.build_profiles(hist, CATS)
    assert prof["payees"] == {"colruyt st k": {"cats": {G: 3}, "recent": [G]}}
    pol = Policy.model_validate({"rules": [{"payee": "Colruyt   ST K", "category": "Groceries"}]})
    new = Txn(**{**tx("n", "x", None, 30, approved=False).__dict__, "payee_name": "COLRUYT St K", "payee_id": "id9"})
    [p] = propose([new], pol, prof, CATS, LAB, lambda *a: pytest.fail("no LLM"), set())
    assert p.source == "rule" and p.category_id == G and p.anomalies == []


def test_policy_gate():
    pol = Policy.model_validate({"categories": {"Groceries": {"mode": "auto", "min_confidence": 0.85, "max_amount": 100}}})
    P = lambda conf, eur, an=(): SimpleNamespace(category_id=G, confidence=conf, txn=tx(1, "x", None, eur), anomalies=list(an))
    assert decide(P(0.9, 50), pol, "Groceries", 0) == "auto"
    assert decide(P(0.8, 50), pol, "Groceries", 0) == "suggest"
    assert decide(P(0.9, 120), pol, "Groceries", 0) == "auto"  # within the default 20% tolerance
    assert decide(P(0.9, 121), pol, "Groceries", 0) == "suggest"
    assert decide(P(0.9, 50, ["new_payee"]), pol, "Groceries", 0) == "suggest"
    assert decide(P(0.9, 50), pol, "Groceries", 30) == "defer"  # per-run cap: left for the next run
    assert decide(P(0.8, 50), pol, "Groceries", 30) == "suggest"
    assert decide(P(0.99, 5), pol, "Restaurants", 0) == "suggest"  # default mode
    assert decide(SimpleNamespace(category_id=None), pol, "", 0) == "skip"


def test_propose_rules_llm_and_validation():
    prof = D.build_profiles(HIST, CATS)
    pol = Policy.model_validate({"rules": [{"payee": "colruyt", "category": "Groceries"}]})
    calls = []

    def llm(system, user, schema):
        calls.append(user)
        return {"results": [{"id": "a", "category_id": R, "confidence": 0.9, "reason": "food"},
                            {"id": "b", "category_id": "nope", "confidence": 0.9, "reason": "?"}]}

    pending = [tx("c", "colruyt", None, 30, approved=False), tx("a", "newplace", None, 20, approved=False),
               tx("b", "bistro", None, 25, approved=False)]
    props = {p.txn.id: p for p in propose(pending, pol, prof, CATS, LAB, llm, set())}
    assert len(calls) == 1  # one batched call
    assert props["c"].source == "rule" and props["c"].category_id == G
    assert props["a"].category_id == R and "new_payee" in props["a"].anomalies
    assert props["b"].category_id is None  # invalid category rejected


class FakeYnab:
    used, remaining = 0, 150

    def __init__(self, txns):
        self.txns, self.patches = txns, []

    def categories(self, k=None):
        return CATS, 1

    def transactions(self, since=None, knowledge=None, type=None):
        return self.txns, 42

    def update(self, patches):
        self.patches += patches


def test_run_live_and_dry(tmp_path):
    st = State(tmp_path)
    st.save("profiles", D.build_profiles(HIST, CATS))
    pol = Policy.model_validate({"categories": {"Groceries": {"mode": "auto"}},
                                 "rules": [{"payee": "colruyt", "category": "Groceries"}]})
    y = FakeYnab([tx("n1", "colruyt", None, 45, approved=False), tx("n2", "colruyt", G, 45)])
    no_llm = lambda *a: pytest.fail("LLM should not be called")

    assert run(y, st, pol, no_llm, dry_run=True)["auto"] == 1 and not y.patches
    stats = run(y, st, pol, no_llm, dry_run=False)
    assert stats["auto"] == 1 and len(y.patches) == 1
    p = y.patches[0]
    assert p.approved and str(p.category_id) == G and p.flag_color.value == "purple" and "[AI:" in p.memo
    assert st.load("sync")["txn_knowledge"] == 42
    assert run(y, st, pol, no_llm, dry_run=False)["auto"] == 0  # already handled

    y.txns = [tx("n1", "colruyt", R, 45)]  # a human re-categorized it
    run(y, st, pol, no_llm, dry_run=False)
    assert "colruyt" in st.load("sync")["corrected_payees"]


def test_rate_limit_guard(monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    y = Ynab(Settings(_env_file=None, approver_max_requests_per_run=2))
    ok = lambda: SimpleNamespace(headers={"x-rate-limit": "160/200"}, data=SimpleNamespace(data="d"))
    assert y._call(ok) == "d" and y.remaining == 40
    with pytest.raises(RateBudgetExceeded):  # 40 left <= reserve of 50
        y._call(ok)


def test_api_key_blocks_subscription_backend(monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    with pytest.raises(ValueError, match="subscription"):
        Settings(_env_file=None)


def test_sdk_mapping_and_draft_roundtrip():
    import yaml
    import ynab
    t = Txn.from_sdk(ynab.TransactionDetail.from_dict({
        "id": "t1", "date": "2026-10-01", "amount": -12340, "cleared": "cleared", "approved": False,
        "account_id": G, "account_name": "Joint", "payee_id": R, "payee_name": "Aldi", "category_id": None,
        "flag_color": None, "deleted": False, "subtransactions": []}))
    assert (t.payee_id, t.category_id, t.eur, t.flag) == (R, None, 12.34, "")
    prof = D.build_profiles(HIST, CATS)
    pol = Policy.model_validate(yaml.safe_load(D.render_yaml(D.derive_policy(prof), prof)))
    assert pol.for_category("Groceries")[0] == "auto" and pol.rules[0].payee == "colruyt"


def test_run_log_lines_and_redaction(tmp_path):
    from approver.log import log, setup
    path = setup(tmp_path, ["sekret-token"])
    try:
        st = State(tmp_path)
        st.save("profiles", D.build_profiles(HIST, CATS))
        pol = Policy.model_validate({"categories": {"Groceries": {"mode": "auto"}},
                                     "rules": [{"payee": "colruyt", "category": "Groceries"}]})
        run(FakeYnab([tx("n1", "colruyt", None, 45, approved=False)]), st, pol, None, dry_run=True)
        try:
            raise RuntimeError("401 for sekret-token")
        except RuntimeError:
            log.exception("failed")
        text = path.read_text()
        assert "[dry] auto" in text and "Colruyt" in text and "Groceries (rule, 1.00)" in text
        assert "sekret-token" not in text and "401 for ***" in text
    finally:
        for h in list(log.handlers):
            log.removeHandler(h)
            h.close()


def test_run_all_picks_up_unhandled_but_not_ai_touched(tmp_path):
    st = State(tmp_path)
    st.save("profiles", D.build_profiles(HIST, CATS))
    st.save("sync", {"txn_knowledge": 41})
    pol = Policy.model_validate({"categories": {"Groceries": {"mode": "auto"}},
                                 "rules": [{"payee": "colruyt", "category": "Groceries"}]})
    old = tx("old", "colruyt", None, 45, days_ago=400, approved=False)
    touched = Txn(**{**tx("t", "colruyt", G, 45, approved=False).__dict__, "memo": "[AI: payee rule]"})
    y = FakeYnab([old, touched])
    no_llm = lambda *a: pytest.fail("LLM should not be called")
    assert run(y, st, pol, no_llm, dry_run=False, all_unapproved=True)["auto"] == 1
    assert [p.id for p in y.patches] == ["old"]


def test_policy_instructions_reach_the_system_prompt():
    from approver.classify import SYSTEM
    seen = []
    pol = Policy.model_validate({"instructions": ["Spending abroad is Restaurants."]})
    llm = lambda system, user, schema: seen.append(system) or {"results": []}
    propose([tx("a", "newplace", None, 20, approved=False)], pol, D.build_profiles(HIST, CATS), CATS, LAB, llm, set())
    assert seen[0].startswith(SYSTEM) and "- Spending abroad is Restaurants." in seen[0]
    assert "instructions: []" in D.render_yaml(D.derive_policy(D.build_profiles(HIST, CATS)), D.build_profiles(HIST, CATS))


def test_corrected_payee_blocks_ai_but_not_rules():
    prof = D.build_profiles(HIST, CATS)
    pol = Policy.model_validate({"rules": [{"payee": "colruyt", "category": "Groceries"}]})
    llm = lambda *a: {"results": [{"id": "b", "category_id": R, "confidence": 0.9, "reason": "x"}]}
    pending = [tx("c", "colruyt", None, 30, approved=False), tx("b", "bistro", None, 25, approved=False)]
    props = {p.txn.id: p for p in propose(pending, pol, prof, CATS, LAB, llm, {"colruyt", "bistro"})}
    assert "corrected_payee" not in props["c"].anomalies and "corrected_payee" in props["b"].anomalies


def test_learning_lifts_corrections(tmp_path):
    from approver.run import absorb_reviews
    st = State(tmp_path)
    st.save("profiles", D.build_profiles(HIST, CATS))
    pol = Policy.model_validate({"categories": {"Restaurants": {"mode": "auto"}}})
    llm = lambda s, u, schema: {"results": [{"id": t, "category_id": R, "confidence": 0.95, "reason": "x"}
                                            for t in ("b1", "b2", "b3")]}
    y = FakeYnab([tx("b1", "bistro", None, 30, approved=False)])
    run(y, st, pol, llm, dry_run=False)
    y.txns = [tx("b1", "bistro", G, 30)]  # human corrects it
    run(y, st, pol, llm, dry_run=False)
    assert st.load("sync")["corrected_payees"] == ["bistro"]

    y.txns = [tx("b2", "bistro", None, 30, approved=False)]  # blocked while corrected: suggestion
    assert run(y, st, pol, llm, dry_run=False)["suggest"] == 1
    y.txns = [tx("b2", "bistro", R, 30)]  # human accepts the suggestion unchanged -> lifted
    run(y, st, pol, llm, dry_run=False)
    assert st.load("sync")["corrected_payees"] == []

    st.save("sync", {**st.load("sync"), "corrected_payees": ["bistro"]})
    st.save("ai_txns", {"b3": {"category_id": R, "action": "suggest", "ts": "2999"}})
    assert absorb_reviews([tx("b3", "bistro", G, 30)], st) == {"corrected": 1}
    assert st.load("sync")["corrected_payees"] == [] and st.load("ai_txns") == {}


def test_merge_is_additive_and_render_round_trips():
    import yaml
    prof = D.build_profiles(HIST, CATS)
    draft = D.derive_policy(prof)  # Groceries auto, Restaurants, Medical; rule colruyt -> Groceries
    pol = Policy.model_validate({
        "defaults": {"max_auto_per_run": 7},
        "categories": {"Groceries": {"mode": "suggest", "max_amount": 10, "description": "mine"}},
        "rules": [{"payee": "Bistro", "category": "Restaurants", "note": "family"}],
        "instructions": ["Spending abroad is a trip, even groceries: always."]})
    merged, changes = D.merge_policy(pol, draft)
    assert merged.categories["Groceries"] == pol.categories["Groceries"]  # existing untouched
    assert merged.categories["Restaurants"].mode == "suggest"
    assert [r.payee for r in merged.rules] == ["bistro", "colruyt"] and merged.rules[0].note == "family"
    assert any("new rule 'colruyt'" in c for c in changes)
    again, changes2 = D.merge_policy(merged, draft)
    assert again == merged and not [c for c in changes2 if not c.startswith("kept")]  # idempotent
    assert Policy.model_validate(yaml.safe_load(D.render_policy(merged, prof))) == merged


def test_writes_in_batches_and_keeps_progress_on_failure(tmp_path, monkeypatch):
    import approver.run as R
    monkeypatch.setattr(R, "WRITE_BATCH", 2)
    st = State(tmp_path)
    st.save("profiles", D.build_profiles(HIST, CATS))
    pol = Policy.model_validate({"categories": {"Groceries": {"mode": "auto"}},
                                 "rules": [{"payee": "colruyt", "category": "Groceries"}]})
    y = FakeYnab([tx(f"n{i}", "colruyt", None, 45, approved=False) for i in range(5)])
    calls = []

    def flaky(patches):
        calls.append([p.id for p in patches])
        if len(calls) == 2:
            raise ConnectionResetError("peer reset")
    y.update = flaky
    with pytest.raises(ConnectionResetError):
        run(y, st, pol, None, dry_run=False)
    assert calls == [["n0", "n1"], ["n2", "n3"]]
    assert set(st.load("ai_txns")) == {"n0", "n1"}  # first batch recorded, cursor not advanced
    assert "txn_knowledge" not in st.load("sync")
    y.update = lambda patches: calls.append([p.id for p in patches])
    assert run(y, st, pol, None, dry_run=False)["auto"] == 3  # the rest on the next run


def test_cap_defers_instead_of_suggesting(tmp_path):
    st = State(tmp_path)
    st.save("profiles", D.build_profiles(HIST, CATS))
    pol = Policy.model_validate({"defaults": {"max_auto_per_run": 1}, "categories": {"Groceries": {"mode": "auto"}},
                                 "rules": [{"payee": "colruyt", "category": "Groceries"}]})
    y = FakeYnab([tx("n1", "colruyt", None, 45, approved=False), tx("n2", "colruyt", None, 45, approved=False)])
    stats = run(y, st, pol, None, dry_run=False, all_unapproved=True)
    assert (stats["auto"], stats["defer"]) == (1, 1) and [p.id for p in y.patches] == ["n1"]
    assert set(st.load("ai_txns")) == {"n1"}  # n2 untouched in YNAB and not tracked
    y.txns, y.patches = [y.txns[1]], []
    assert run(y, st, pol, None, dry_run=False, all_unapproved=True)["auto"] == 1  # next run approves it
