# Implementation Plan — ynab-ai-approver

> Scope: the AI-approver only. Spending stats come from YNAB. No dashboard and no notifications (a dashboard may be added later).
> Background: [`README.md`](README.md) explains usage; [`decisions.md`](decisions.md) records the design decisions.

## 1. Goal

Categorize and approve imported YNAB transactions automatically with Claude, gated by a per-category policy. Setup should be fast: the policy is distilled from the family's existing approved transactions.

## 2. Architecture

```
┌──────────────┐  delta GET (1)   ┌──────────────────────────────┐  1 batched call  ┌────────┐
│     YNAB     │ ───────────────► │ runner (Python)              │ ───────────────► │ Claude │
│  plan/budget │                  │  rules → classify → gate     │ ◄─────────────── │  API   │
│              │ ◄─────────────── │  audit / state (local files) │  structured JSON └────────┘
└──────────────┘  bulk PATCH (1)  └──────────────────────────────┘
                                          ▲
                              policy.yaml + state/profiles.json
                              (drafted by `approver distill`)
```

**Reused components:**

- the official `ynab` Python SDK (API client, delta requests, bulk transaction update)
- the **Claude Code CLI** in headless mode (`claude -p`, your own subscription) as the default LLM backend, with `anthropic` as the optional pay-per-use backend (see D9 in `decisions.md`)
- `pydantic-settings` (configuration, including `.env`)
- `pyyaml` (policy)
- `typer` (CLI)

From the YNAB MCP servers we reuse **ideas**: the anomaly hints from oliverames (`new_payee`, `category_drift`), the triage prompt from Maronato, and the undo journal. A YNAB MCP server stays optional for interactive use (see D2 in `decisions.md`).

## 3. Components

| Module | Responsibility |
|---|---|
| `config.py` | `Settings` (pydantic-settings): env vars first, then `.env`; secrets as `SecretStr`. Refuses to start with `ANTHROPIC_API_KEY` set on the `claude-cli` backend (it would bill the API). `Policy` model + loader (a bare YAML `off` is accepted). `payee_key()`: case/whitespace-insensitive payee identity (D10). |
| `ynab_client.py` | Official SDK wrapper: delta reads, bulk PATCH (never blindly retried), request budget (per-run cap + `X-Rate-Limit` reserve), 429/`Retry-After` backoff for reads. Normalizes SDK objects to a small `Txn` (UUIDs → `str`, milliunits kept). |
| `state.py` | JSON/JSONL files under `state/`: `sync`, `categories`, `profiles`, `history`, `ai_txns`, `dry_seen`, `audit.jsonl`. |
| `log.py` | Human-readable `state/approver.log` (rotating 1 MB × 5), token redaction (D13). |
| `distill.py` | History → category/payee profiles → draft policy + payee rules → commented `policy.draft.yaml`; optional `--describe` (one Claude call). |
| `classify.py` | Payee rules first (matched on normalized payee name); policy `instructions` appended to the system prompt (D14); then one batched Claude call per chunk of 40 (sorted by candidate category, only those profiles attached); validates category ids; anomaly hints (`new_payee`, `category_drift`, `amount_outlier`, `split`, `corrected_payee` — the latter only for AI proposals, not payee rules). |
| `policy.py` | The gate: `auto` / `suggest` / `skip` / `defer` (auto-eligible past the per-run cap; retried next run) from mode, confidence, amount (`max_amount × (1 + amount_tolerance)`, D11), `never_auto_when` and the per-run cap. |
| `llm.py` | `ClaudeCli` (default): `claude -p --output-format json --json-schema … --tools "" --strict-mcp-config --no-session-persistence`, prompt on stdin, subscription token via env, usage-limit → skip run. `Api`: `anthropic` SDK with forced tool output. |
| `run.py` | `run` (delta → corrections → propose → decide → one PATCH → audit + log), `undo`, `backtest`. Dry runs and the first live run take all unapproved transactions (D12). |
| `cli.py` | `check`, `distill`, `backtest`, `run`, `undo`; each command is wrapped by `@logged`. |

## 4. Rate-limit design (200 requests/hour per token)

| Step | Requests | Notes |
|---|---|---|
| Fetch changed transactions | 1 | `last_knowledge_of_server` delta. Contains new imports **and** human edits, which are used for correction detection. The first run (no cursor yet) fetches all unapproved transactions instead. |
| Early exit | 0 | Stop if nothing in the delta is unapproved. |
| Refresh categories | 0–1 | Delta request at most once a day, or on an unknown category id. |
| Classify | 0 | Claude only. Payee history comes from `profiles.json`, with no per-transaction YNAB lookups. |
| Write | 0–1 | Bulk `PATCH …/transactions`, up to 100 transactions per request (more requests only for large imports). |
| **Typical run** | **1–3** | About 2–6 per hour at a 30-min schedule. |

**Guards:**

- A hard per-run cap.
- Stop when the `X-Rate-Limit` header shows fewer than the reserve remaining.
- On 429, honour `Retry-After` with exponential backoff and jitter. Reads are retried; writes are not blindly retried. Instead the runner re-reads state first, so a retry can't double-apply changes.
- `distill` uses 2–3 requests: transactions `since_date`, categories, and payees for names.

**Batching on the Claude side ("category batches"):**

- All pending transactions from a run go into one request. Above about 50 transactions, they're chunked by candidate category so that each chunk carries only the profiles it needs.
- Each category profile is sent once per batch, not once per transaction. This keeps prompts small and cheap.

## 5. Policy distillation (`approver distill`)

**Input:** approved, categorized, non-transfer transactions from the last N months (default 12; the family uses 24, see D6). It takes one request, and split transactions are expanded into their subtransactions.

**Statistics per category:**

- `n`, distinct payees, top payees with their share
- payees are identified by normalized name: casefolded, whitespace collapsed (D10)
- **payee purity:** for each payee, the share of its transactions in this category, averaged over the category's transactions
- amounts: median, p95, max, coefficient of variation

**Derivation (defaults, all configurable):**

| Output | Rule |
|---|---|
| Payee rule | Payee (normalized name) has ≥ 3 transactions and ≥ 95% of them in one category, and no transaction in the last 90 days went elsewhere |
| `mode: auto` | `n ≥ 10`, purity ≥ 0.85, CV of amount ≤ 1.0, not on the sensitive list |
| `mode: suggest` | Everything else with `n ≥ 3` |
| `mode: off` | `n < 3`, or on the sensitive list (default: Medical, Taxes, Gifts; editable) |
| `max_amount` | p95 rounded up to a sensible step (5/10/50); the gate adds `amount_tolerance` (20%) on top |
| `min_confidence` | 0.85 for every category (D11) |
| `description` | With `--describe`: one batched Claude call over all categories' top payees |

**Output:**

- `policy.draft.yaml`, with per-category stats as comments
- `state/profiles.json`, which holds the classifier context: top payees, amount range and description. After that the runner keeps the profiles up to date incrementally from the delta feed, at no extra requests.

**Validation:** `approver backtest` holds out the most recent month, runs rules, classifier and policy on it with the labels hidden, and reports per category: auto-approve rate, accuracy of the auto-approved transactions, and accuracy of the suggestions. Use it to tune thresholds before going live.

## 6. Safety

- Only transactions with `approved = false` that aren't transfers are candidates. Amount, date and payee are never written.
- The policy gate runs in code, after the AI. The AI output is validated against a schema, and unknown category ids are rejected.
- `max_auto_per_run` acts as a circuit breaker. Auto-eligible transactions past the cap are deferred to the next run (D18), so raising it is not needed for large imports.
- `--dry-run` writes the full decision report (`audit.jsonl` and `state/approver.log`) and makes no PATCH.
- The audit log stores the before-state of every change, and `undo --since` restores it with one bulk PATCH.
- **Correction detection:** if a later delta shows a person changed an AI-handled transaction, it's logged as `corrected`. The payee's profile is updated, and repeated corrections demote that payee or category to `suggest`.

## 7. Phases

| Phase | Scope | Exit criterion |
|---|---|---|
| **1. Foundation** | Repo, `pyproject`, `Settings` (env + `.env`), `.env.example`, SDK wrapper with request budget + backoff, LLM backends, `approver check` | `check` reports the plan name, rate-limit headroom, and that the Claude CLI responds on your subscription |
| **2. Distill** | History fetch, statistics, `policy.draft.yaml`, `profiles.json`, `--describe` | The family reviews the draft and saves `policy.yaml` |
| **3. Classify + gate** | Rules, anomalies, batched classifier, policy gate, `run --dry-run`, audit | A dry-run report looks right on real data |
| **4. Backtest** | Hold-out replay + per-category report | Thresholds tuned; auto accuracy acceptable to the family |
| **5. Write-back** | Bulk PATCH, flags/memo, undo, correction detection | Live on 2–3 categories |
| **6. Unattended** | launchd plist / cron / Dockerfile, log rotation, README finalized | Runs on schedule for 2 weeks without intervention |
| **Later** | Dashboard; widen auto categories based on audit stats | — |

## 8. Repo layout

```
ynab-ai-approver/
  README.md  plan.md  decisions.md
  pyproject.toml  .env.example  .gitignore
  src/approver/
    config.py  ynab_client.py  state.py  log.py  llm.py
    distill.py  classify.py  policy.py  run.py  cli.py
    prompts/classify.md
  tests/test_core.py
  deploy/com.family.ynab-approver.plist  deploy/Dockerfile
  policy.yaml             # family-owned, created from policy.draft.yaml
  state/                  # git-ignored
```

## 9. Testing

- **Unit:** policy gate (incl. amount tolerance), distill derivations, rule matching (incl. case/spacing-insensitive payees), run log lines and token redaction, rate-limit guard (with a fake header sequence), and 429 handling.
- **Contract:** the classifier's JSON schema validation against recorded responses.
- **Integration:** the SDK wrapper against recorded YNAB responses (no live writes in CI).
- **Acceptance:** `backtest` on real history, then 1–2 weeks of `--dry-run`.

## 10. Status and open points

**Implemented (v0.1):** phases 1–6 in code, with unit tests for the policy gate, distill, the proposal pipeline, a full live and dry run against a fake YNAB, correction detection, payee normalization, the run log, the rate-limit guard and the API-key guard.

**First live run (2026-10-31):** 361 processed → 65 auto (purple), 243 suggest (yellow), 53 skip; verified in YNAB. It missed 285 older unapproved transactions (YNAB's 12-month default, see D12) — recovered with `run --all`.

**Before that, against the live plan:** `check` (YNAB and Claude CLI ok), `distill --months 24` (1,654 transactions → 32 categories, 48 payee rules), and dry runs over all 361 unapproved transactions (about 65 auto, 285 suggest, 11–30 skip). `policy.yaml` is reviewed and saved. 
Resolved:
- The SDK (`ynab` 4.2) bulk-updates category, approved, flag and memo in one payload and exposes response headers, so `X-Rate-Limit` is read directly. The SDK uses "plans".
- The Claude CLI supports `--json-schema` in `-p` mode. The runner reads `structured_output` and falls back to parsing `result`.

- The CLI's JSON field names work on the installed version (`approver check` and the dry runs use them).

Still open:
- **`no valid AI answer` skips** (49 of 361 in the first live run, mostly one chunk): normal runs don't retry them; `run --all` does. Still to do: retry missing answers once within the run and log whether the id was missing or the category invalid.
- Scheduled 2026-11-01 via launchd (daily run, weekly distill, P1). Check `state/run.log` after the first scheduled run.
- Weekly `distill --merge` adds new rules/categories to `policy.yaml` (D17); review `policy-history/` diffs occasionally.
- Measure how much of the Pro allowance one run uses during dry-run. If it's noticeable, run hourly.
- ~~Bulk PATCH batch size~~ resolved 2026-11-01: a 692-transaction PATCH made YNAB reset the connection; writes are now chunked at 100 and recorded per batch, per-run request cap raised to 20 (D18).
