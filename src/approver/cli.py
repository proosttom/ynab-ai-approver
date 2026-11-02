"""approver check | distill | backtest | run | undo"""
from __future__ import annotations

import functools
import json
import shutil
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import typer

from . import distill as D
from .config import Policy, Settings
from .log import log, setup as setup_log
from .llm import UsageLimit, make_llm
from .run import absorb_reviews, backtest as _backtest, load_categories, run as _run, undo as _undo
from .state import State
from .ynab_client import RateBudgetExceeded, Ynab

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _ctx():
    s = Settings()
    return s, Ynab(s), State(s.approver_state_dir)


def _say(msg: str) -> None:
    typer.echo(msg)
    log.info(msg)


def logged(fn):
    """Write start, end and any error of a command to <state dir>/approver.log."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        s = Settings()
        setup_log(s.approver_state_dir, [t.get_secret_value() for t in
                                         (s.ynab_api_token, s.claude_code_oauth_token, s.anthropic_api_key) if t])
        log.info("start %s %s", fn.__name__, " ".join(f"{k}={v}" for k, v in kwargs.items()))
        try:
            result = fn(*args, **kwargs)
        except typer.Exit:
            log.info("end %s", fn.__name__)
            raise
        except BaseException:
            log.exception("failed %s", fn.__name__)
            raise
        log.info("end %s", fn.__name__)
        return result
    return wrapper


@app.command()
@logged
def check(llm: bool = typer.Option(True, help="Also send one tiny test prompt to Claude.")):
    """Validate config and tokens; show rate-limit headroom."""
    s, y, st = _ctx()
    cats = load_categories(y, st, force=True)
    _say(f"YNAB ok: plan '{s.ynab_plan_id}', {len(cats)} categories, "
               f"{y.remaining if y.remaining is not None else '?'} requests left this hour")
    if llm:
        r = make_llm(s)("Reply with JSON only.", 'Return {"ok": true}.',
                        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
        _say(f"Claude ok ({s.approver_llm_backend}, model {s.approver_model}): {r}")


@app.command()
@logged
def distill(months: int = 12, describe: bool = typer.Option(False, help="Let Claude write category descriptions."),
            out: Path = Path("policy.draft.yaml"),
            merge: bool = typer.Option(False, "--merge", help="Add new payee rules/categories to the policy "
                                       "(existing entries untouched; old version kept in policy-history/).")):
    """Draft policy + category profiles from approved history (2 YNAB requests)."""
    s, y, st = _ctx()
    cats = load_categories(y, st, force=True)
    txns, _ = y.transactions(since=date.today() - timedelta(days=30 * months))
    st.save("history", [asdict(t) for t in txns])
    reviewed = absorb_reviews(txns, st)
    profiles = D.build_profiles(txns, cats)
    if describe:
        D.describe(profiles, make_llm(s))
    st.save("profiles", profiles)
    draft = D.derive_policy(profiles)
    out.write_text(D.render_yaml(draft, profiles))
    modes = [c["mode"] for c in draft["categories"].values()]
    _say(f"{len(txns)} transactions ({reviewed['accepted']} AI suggestions accepted, {reviewed['corrected']} corrected) → {len(modes)} categories "
               f"(auto {modes.count('auto')}, suggest {modes.count('suggest')}, off {modes.count('off')}), "
               f"{len(draft['rules'])} payee rules → {out}.")
    if merge:
        path = s.approver_policy_file
        merged, changes = D.merge_policy(Policy.load(path), draft)
        for c in changes:
            _say(f"  merge: {c}")
        if any(not c.startswith("kept") for c in changes):
            hist = path.parent / "policy-history"
            hist.mkdir(exist_ok=True)
            backup = hist / f"{path.stem}.{datetime.now():%Y-%m-%d_%H%M%S}{path.suffix}"
            shutil.copy2(path, backup)
            path.write_text(D.render_policy(merged, profiles))
            _say(f"Merged into {path}; previous version saved as {backup}.")
        else:
            _say(f"Nothing new to merge into {path}.")


@app.command()
@logged
def backtest(sample: int = 100):
    """Replay the policy on the last 30 days of history (labels hidden). Uses Claude, no YNAB requests."""
    s = Settings()
    res = _backtest(State(s.approver_state_dir), Policy.load(s.approver_policy_file), make_llm(s), sample)
    _say(f"{'category':30} {'n':>4} {'auto':>5} {'auto ok':>8} {'sugg':>5} {'sugg ok':>8} {'skip':>5}")
    for label, r in sorted(res.items(), key=lambda kv: -kv[1]["n"]):
        _say(f"{label[:30]:30} {r['n']:>4} {r['auto']:>5} {r['auto_ok']:>8} "
                   f"{r['suggest']:>5} {r['suggest_ok']:>8} {r['skip']:>5}")


@app.command()
@logged
def run(dry_run: bool = typer.Option(False, "--dry-run", help="Log decisions, write nothing to YNAB."),
        all_: bool = typer.Option(False, "--all", help="Also process every unapproved transaction not handled yet "
                                  "(older ones, earlier skips). One extra YNAB request.")):
    """One approval pass (1–3 YNAB requests)."""
    s, y, st = _ctx()
    try:
        stats = _run(y, st, Policy.load(s.approver_policy_file), make_llm(s), dry_run, all_)
    except (UsageLimit, RateBudgetExceeded) as e:
        _say(f"Skipped this run: {e}")
        raise typer.Exit(0)
    _say(json.dumps(dict(stats)))


@app.command()
@logged
def undo(since: str = typer.Option(..., help="ISO date, e.g. 2026-11-01")):
    """Revert AI changes made since a date (one bulk PATCH)."""
    _, y, st = _ctx()
    _say(f"Reverted {_undo(y, st, since)} transactions.")


if __name__ == "__main__":
    app()
