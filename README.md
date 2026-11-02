# ynab-ai-approver

Automatically **categorizes and approves** imported YNAB transactions with Claude, following per-category rules the family controls. Anything the AI isn't sure about stays unapproved in YNAB for a person to review.

There is no dashboard, no notifications and no budgeting logic. Spending stats come from YNAB's own reports. See [`plan.md`](plan.md) for the implementation plan and [`decisions.md`](decisions.md) for the reasoning behind the design.

---

## How it works

```
 YNAB ──(1) delta GET transactions──► runner ──(2) one batched call──► Claude
  ▲                                     │      (all pending txns, grouped
  │                                     │       by candidate category)
  └──────(3) one bulk PATCH─────────────┘
                                        │
                              policy.yaml gate (code)
```

Each run:

1. **Fetch:** one *delta* request returns only the transactions that changed since the last run. If none of them are unapproved, the run stops there (1 API call in total, no AI cost). The very first run (and every `--dry-run`) takes **all** unapproved transactions, whatever their date.
2. **Rules:** payees with a known, stable category (from `policy.yaml`) are resolved without AI. Payees are matched by name, ignoring case and spacing, so `COLRUYT  St K` and `Colruyt St K` are the same payee.
3. **Classify:** the remaining transactions go to Claude in **one batched request**. They're grouped by candidate category, and the prompt includes the category profiles (description, typical payees, amount range). Claude returns `{category, confidence, reason}` per transaction as structured output.
4. **Gate:** `policy.yaml` decides per transaction:
   - `auto`: set the category, approve, add a **purple** flag and the memo tag `[AI: reason]`
   - `suggest`: set the category, leave the transaction unapproved, add a **yellow** flag
   - `off`, or an anomaly (new payee, unusual amount, split, transfer): untouched
5. **Write:** all changes go to YNAB in **bulk PATCHes of up to 100 transactions** (usually one). Each batch is recorded as soon as YNAB accepts it, so a failure halfway keeps the finished batches.
6. **Audit and learning:** every decision is appended to `state/audit.jsonl`. On the next run, approved suggestions update the payee's history, so the next transaction from that payee can be auto-approved. A correction blocks auto for that payee until a later suggestion is accepted unchanged. Run `approver distill --merge` weekly to also learn from transactions you categorized yourself and to add new payee rules to `policy.yaml` (earlier versions kept in `policy-history/`).
7. **Log:** every command writes a readable log to `state/approver.log` (start/end, one line per decision, errors with traceback; tokens redacted, rotated at 1 MB × 5).

**Review in YNAB:** filter on the yellow flag or the unapproved transactions, and spot-check the purple ones.

---

## Quick start

```bash
git clone <this repo> && cd ynab-ai-approver
python3 -m venv .venv && .venv/bin/pip install -e .   # add .[api] for the API backend
# install Claude Code (see claude.com/claude-code), then:
claude setup-token              # log in with your Claude subscription, copy the token
cp .env.example .env            # then fill in YNAB_API_TOKEN and CLAUDE_CODE_OAUTH_TOKEN
# policy.example.yaml shows the format; your own policy.yaml is git-ignored

approver check                  # validates tokens, shows plan & rate-limit headroom
approver distill --months 24    # drafts policy.draft.yaml from your history (more months = more rules)
#   review the draft → save it as policy.yaml
approver run --dry-run          # decisions are logged, nothing is written to YNAB
approver run                    # live
```

**First live run:** it processes every unapproved transaction you have, so it can be large. Transactions beyond `max_auto_per_run` become suggestions and aren't reconsidered for auto-approval, so raise the cap (e.g. to 100) for that first run, then set it back to 30.

Schedule it with launchd (macOS), cron or Docker. The `deploy/` launchd files run `approver run --all` daily at 07:00 and `approver distill --months 24 --merge` weekly on Sunday at 06:30 (output in `state/run.log` / `state/distill.log`, details in `state/approver.log`). Install:

```bash
for f in com.family.ynab-approver com.family.ynab-approver.distill; do
  sed -e "s#/PATH/TO/ynab-ai-approver#$PWD#g" -e "s#/Users/YOU#$HOME#g" deploy/$f.plist > ~/Library/LaunchAgents/$f.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$f.plist
done
```

Remove with `launchctl bootout gui/$(id -u)/<label>`. If the Mac is asleep at the scheduled time, the job runs at wake.

---

## Configuration

### Tokens and settings (environment or `.env`)

Values come from **process environment variables first**, then from a `.env` file in the working directory (read by `pydantic-settings`; variables already set always win). `.env` is git-ignored; `.env.example` is committed.

| Variable | Required | Description |
|---|---|---|
| `YNAB_API_TOKEN` | yes | YNAB Personal Access Token (YNAB → Account Settings → Developer Settings). This is the same name the common YNAB MCP servers use, so one `.env` works for both. |
| `CLAUDE_CODE_OAUTH_TOKEN` | yes (default backend) | Long-lived token for your **Claude Pro/Max subscription**, created with `claude setup-token`. Used by the `claude-cli` backend. |
| `APPROVER_LLM_BACKEND` | no | `claude-cli` (default: uses your subscription via the Claude Code CLI) or `api` (pay-per-use Claude API). |
| `ANTHROPIC_API_KEY` | only for `api` | Claude Console API key. **Leave it unset for `claude-cli`**: if it's set, Claude Code uses it instead of your subscription and bills the API. |
| `YNAB_PLAN_ID` | no | Plan (budget) id. Defaults to `last-used`. |
| `APPROVER_MODEL` | no | Model alias or id, e.g. `haiku` or `sonnet`. A small, fast model is recommended. |
| `APPROVER_MAX_REQUESTS_PER_RUN` | no | Hard cap on YNAB requests per run (default `20`). |
| `APPROVER_RATE_LIMIT_RESERVE` | no | Stop when fewer than this many requests remain in the hour (default `50`). |
| `APPROVER_STATE_DIR` | no | Where state and the audit log live (default `./state`). |
| `APPROVER_POLICY_FILE` | no | Policy path (default `./policy.yaml`). |

```dotenv
# .env.example
YNAB_API_TOKEN=
CLAUDE_CODE_OAUTH_TOKEN=         # from: claude setup-token
APPROVER_LLM_BACKEND=claude-cli
# ANTHROPIC_API_KEY=             # only for APPROVER_LLM_BACKEND=api
YNAB_PLAN_ID=last-used
APPROVER_MODEL=haiku
```

The tokens are never logged: `state/approver.log` replaces them with `***`.

### `policy.yaml`

```yaml
defaults:
  mode: suggest            # auto | suggest | off
  min_confidence: 0.85
  max_amount: 150          # in your plan's currency; above this (plus tolerance), always a human
  amount_tolerance: 0.20   # auto-approve up to max_amount * 1.2, for every category
  max_auto_per_run: 30     # circuit breaker

categories:
  "Groceries":
    mode: auto
    min_confidence: 0.85
    max_amount: 250
    description: "Supermarket and daily food shopping"
    # distilled: 412 txns, 9 payees, 97% payee purity, p95 €182
  "Medical":
    mode: off

rules:                     # payee → category, no AI needed
  - payee: "colruyt"   # case and spacing are ignored
    category: "Groceries"

instructions:              # family rules for Claude; they override payee history
  - Every purchase made outside Belgium is "family trip/activities", even groceries or fuel.
    Online shops billed from abroad are not trips.

never_auto_when: [new_payee, amount_outlier, split, transfer]
```

`instructions` are added to Claude's system prompt. They steer the AI's *proposal* only: the gate (modes, thresholds, `never_auto_when`) still decides, and payee rules are applied before the AI.

You don't have to write this by hand: `approver distill` generates it (see below).

---

## Commands

| Command | What it does | YNAB requests |
|---|---|---|
| `approver check` | Validates tokens and config, shows rate-limit headroom | 1 |
| `approver distill [--months N] [--describe] [--merge]` | Drafts `policy.draft.yaml` and `state/profiles.json` from approved history. `--merge` adds new payee rules and new categories (at most `suggest`) to `policy.yaml`, never changes existing entries, and saves the previous version in `policy-history/` | 2–3 |
| `approver backtest [--months N]` | Replays the policy on recent approved transactions (labels hidden) and reports per-category accuracy and auto-approve rate | 0 (uses the distill cache) |
| `approver run [--dry-run] [--all]` | One approval pass. `--all` also takes every unapproved transaction the approver hasn't handled yet (older ones, earlier skips) | 1–3 (+1 with `--all`) |
| `approver undo --since <date>` | Reverts AI changes from the audit log: unapproves, restores flag, memo and the original category (YNAB can't clear a category, so an originally uncategorized transaction keeps the AI category but is unapproved again) | 1–2 |

### `approver distill`: policy from your history

This reads your already-approved transactions (one request) and computes, per category:

- the number of transactions, the distinct payees (matched by name, ignoring case and spacing, so `COLRUYT  St K` and `Colruyt St K` are one payee), and **payee purity** (how consistently each payee maps to this category)
- the amount distribution (median, p95, max, coefficient of variation)

From that it proposes:

- **Payee rules:** payees with at least 3 transactions that land in one category at least 95% of the time.
- **Mode:** `auto` for high-volume, high-purity categories with stable amounts; `suggest` for mixed ones; `off` for rare or sensitive ones (configurable list, e.g. Medical).
- **Thresholds:** `max_amount` is p95 rounded up (the gate adds `amount_tolerance`, 20% by default), and `min_confidence` is 0.85.
- **Descriptions** (with `--describe`): one batched Claude call writes a short description for each category from its top payees.

The output is `policy.draft.yaml`, with the statistics as comments so you can see *why* each value was chosen. Review it, adjust it and save it as `policy.yaml`. Then run `approver backtest` to see what it would have done.

---

## YNAB API rate limit

YNAB allows **200 requests per hour per token**. The approver is designed to use only a small fraction of that:

- **Delta requests:** transactions and categories are fetched with `last_knowledge_of_server`, so each run downloads only what changed.
- **Early exit:** if nothing is unapproved, the run ends after one request.
- **Batching:** changes go out in bulk `PATCH …/transactions` requests of up to 100 transactions (one per run in normal use; a large import of ~700 needs 7). Nothing is updated one transaction at a time.
- **No per-transaction lookups:** payee history comes from the local profile built by `distill` and kept current from the delta feed, not from extra API calls.
- **Categories:** refreshed with a delta request at most once a day, or when an unknown category id appears.
- **Guards:** the runner reads YNAB's `X-Rate-Limit` header (e.g. `36/200`) and stops when the reserve is reached. There's a hard cap of `APPROVER_MAX_REQUESTS_PER_RUN`, and on HTTP 429 it waits for `Retry-After` with exponential backoff.

A typical run costs **1–3 requests**. Running every 30 minutes uses about 2–6 of the 200 per hour, which leaves plenty for the YNAB apps and any MCP server you use interactively.

---

## Safety

- It never touches transactions a person already approved, and never changes amount, date or payee.
- The policy is enforced **in code**, after the AI responds. Payee or memo text can't talk the AI into approving something the policy forbids.
- Use `--dry-run` for the first 1–2 weeks.
- There's an audit trail (`state/audit.jsonl`), a readable log (`state/approver.log`) and `undo`.
- When Claude leaves a transaction out of its answer (`no valid AI answer` in the log), it is skipped. Normal runs don't retry it; `approver run --all` does. Transactions carrying an `[AI:` memo are never processed twice.

## Claude subscription vs API

By default the approver uses your **Claude Pro/Max subscription**. It calls the official, unmodified **Claude Code CLI** in headless mode (`claude -p`) once per run, with no tools enabled, a single turn and JSON output that the runner validates. The CLI logs in with your own subscription through `CLAUDE_CODE_OAUTH_TOKEN`. Usage counts against your plan's normal limits, which are shared with your chats. One small batched call per run, and only when there's something to approve, is light usage.

| | `claude-cli` (default) | `api` |
|---|---|---|
| Billing | Included in your Pro/Max subscription | Pay-per-use, Claude Console credits |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, valid for 1 year) | `ANTHROPIC_API_KEY` |
| Extra install | Claude Code CLI | none (`anthropic` Python SDK) |
| Limits | Your plan's usage limits; a run is skipped and retried later when the limit is reached | API rate limits |

Note that the subscription login is meant for your *own, personal* use of Claude Code. Don't share the token or build this into a service for other people. If you ever do, switch to the `api` backend.

## Interactive use (optional)

For ad hoc questions or manual triage in Claude Desktop, connect a YNAB MCP server such as [oliverames/ynab-mcp-server](https://github.com/oliverames/ynab-mcp-server) with the same `YNAB_API_TOKEN`. It has a built-in rate limiter, so it shares the hourly budget safely.

## Built on

[`ynab`](https://pypi.org/project/ynab/) (official SDK) · [Claude Code CLI](https://code.claude.com/docs) (headless, subscription) · [`anthropic`](https://pypi.org/project/anthropic/) (optional `api` backend) · [`pydantic`](https://pypi.org/project/pydantic/) / `pydantic-settings` · `pyyaml` · `typer` (CLI). Statistics and backoff use the standard library.
