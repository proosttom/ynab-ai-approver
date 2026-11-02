# Decisions — ynab-ai-approver

Lightweight decision log. Newest decisions are at the bottom. Status: **Accepted**, **Proposed** (awaiting the family's confirmation), or **Superseded**.

---

### D1 — YNAB stays the source of truth; no budgets, no own UI
**Status:** Accepted · 2026-10-31
**Context:** The family wants to keep YNAB and only needs spending stats, not budgeting.
**Decision:** YNAB holds all transactions and categories, and its built-in reports provide the stats. No targets or assigned amounts are used. We build no dashboard and send no notifications for now.
**Consequences:** The scope shrinks to one headless job. A dashboard can be added later on top of the same data.

### D2 — Unattended approver uses the official SDK, not an agent driving an MCP server
**Status:** Accepted · 2026-10-31 · *Supersedes the earlier "Claude Agent SDK + MCP server" proposal (plan v4)*
**Context:** We evaluated the YNAB MCP servers (oliverames, calebl, Maronato, cinnes, dizzlkheinz).
- **oliverames/ynab-mcp-server** has the best review tools, but it deliberately requires human consent for every write ("a token is not human consent"; writes need a preview token + `confirmed` + explicit approval). It isn't designed for unattended auto-approval, and working around that would break its safety model.
- An agent working through MCP tools makes many small calls (list, search, suggest, update…), which works against the 200 requests/hour limit.
- **calebl/ynab-mcp-server** allows unattended writes, but it has the same chattiness problem.

**Decision:** The scheduled approver is a thin Python runner. It uses the **official `ynab` SDK** for one delta read and one bulk write per run, and one batched Claude classification call (backend amended by D9). YNAB MCP servers stay an *optional, interactive* tool in Claude Desktop.
**Consequences:**
- About 1–3 YNAB requests per run, and deterministic, testable behaviour.
- Slightly more own code: about 10 small modules.
- MCP ideas (anomaly hints, triage prompt, undo journal) are reused as patterns.

### D3 — Policy is enforced in code, after the AI
**Status:** Accepted · 2026-10-31
**Context:** AI output can be wrong, and payee or memo text could try to manipulate the prompt.
**Decision:** Claude only *proposes* `{category, confidence, reason}`. A deterministic gate (`policy.yaml`) decides `auto`, `suggest` or `skip`, with confidence and amount thresholds, `never_auto_when` anomalies and a per-run cap.
**Consequences:** Approvals are predictable and explainable. Rules can be unit-tested.

### D4 — Rate-limit strategy: delta, early exit, batch, guard
**Status:** Accepted · 2026-10-31
**Context:** YNAB allows 200 requests per hour per token, shared with the YNAB apps and any MCP server.
**Decision:**
- Delta requests (`last_knowledge_of_server`).
- Early exit when nothing is unapproved.
- One bulk PATCH per run.
- No per-transaction YNAB lookups; history comes from the local profiles.
- Categories refreshed at most daily.
- Read the `X-Rate-Limit` header with a reserve (50), a per-run cap (10; 20 since D18), and 429/`Retry-After` backoff.
- Classification batched per run and grouped by category ("category batches").

**Consequences:** About 2–6 requests per hour at a 30-minute schedule.

### D5 — Secrets from environment variables, with `.env` as fallback
**Status:** Accepted · 2026-10-31
**Decision:** `pydantic-settings` reads `YNAB_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY` for the `api` backend, see D9) and the other settings from the process environment first, then from `.env` (pydantic-settings' built-in `.env` support; set variables always win). `.env` is git-ignored and `.env.example` is committed. Secrets are redacted in logs and errors. The variable name `YNAB_API_TOKEN` matches the common YNAB MCP servers, so one `.env` serves both.
**Consequences:** It works the same locally, under launchd/cron and in Docker (`--env-file`). Redaction in `state/approver.log` is implemented by D13.

### D6 — Distill the per-category policy from approved history
**Status:** Accepted · 2026-10-31
**Context:** Writing `policy.yaml` by hand for every category is the main setup cost.
**Decision:** `approver distill` analyses about 12 months of approved transactions (one request) and drafts:
- payee rules (≥ 3 transactions, ≥ 95% consistent)
- per-category `mode`, `min_confidence` and `max_amount` from volume, payee purity, amount spread and the sensitive-list
- optional AI-written category descriptions

It writes `policy.draft.yaml` with the statistics as comments. A person reviews and promotes it. `approver backtest` validates it on a hold-out month.
**Amended 2026-10-31:** payees are identified by normalized name (D10), `min_confidence` is a flat 0.85 (D11), and the family's policy was distilled from **24 months** (`--months 24`): 12 months covered too few categorized transactions (14 categories, 1 auto) versus 24 (32 categories, 7 auto).
**Consequences:** Setup becomes "review a draft" instead of "write a config". The derived profiles also serve as classifier context, which removes per-transaction history lookups (supports D4).

### D7 — Visible markers in YNAB instead of notifications
**Status:** Accepted · 2026-10-31
**Decision:** A purple flag plus the memo tag `[AI: reason]` marks auto-approved transactions. A yellow flag plus unapproved status marks suggestions that need review. The family reviews inside YNAB.
**Consequences:** No notification infrastructure. Note that the flag colours are reserved for the approver, so the family must not use purple or yellow for other purposes. Choose other colours if they already do.

### D8 — Python + local files for state
**Status:** Accepted · 2026-10-31
**Decision:** Python (the official SDK; standard-library statistics for distill, no pandas). State lives in small JSON/JSONL files under `state/` (sync knowledge, profiles, audit). No database.
**Consequences:** It's trivial to back up and inspect. A SQLite/DuckDB store can be introduced if a dashboard is added later.

### D9 — Use the Claude Pro subscription via the Claude Code CLI
**Status:** Accepted · 2026-10-31
**Context:** The family has a Claude Pro subscription (about €20/month) and doesn't want separate API billing. A Pro subscription doesn't include Claude API access: the `anthropic` SDK needs a Console API key with prepaid credits. Anthropic's terms allow subscription (OAuth) login for ordinary, personal use of Claude Code and its own apps. They don't allow third-party products to route requests through subscription credentials, and they direct developers building with the Agent SDK to use API keys.
**Decision:** The default LLM backend calls the official, unmodified Claude Code CLI in headless mode (`claude -p`). It's authenticated with the family's own subscription through `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`). Each run makes one call, with no tools, a single turn and JSON output, and only when there are unapproved transactions. A pay-per-use `api` backend (`ANTHROPIC_API_KEY`) stays available behind the same interface.
**Consequences:**
- No extra cost beyond the subscription. Usage counts against the plan's limits, which are shared with chat. When the limit is hit, the run is skipped and the next scheduled run picks up the transactions.
- It requires the Claude Code CLI on the machine that runs the approver, and the token must be renewed yearly.
- `ANTHROPIC_API_KEY` must be unset for this backend, because Claude Code prefers an API key over the subscription in `-p` mode. The runner enforces this at startup.
- This is personal use only. The token must not be shared or embedded in a service for others.

### D10 — Payee identity is the normalized payee name, not the YNAB payee id
**Status:** Accepted · 2026-10-31
**Context:** The bank feed spells the same shop differently over time (`BAKERY JANSSENS     TOWN` vs `Bakery Janssens Town`), and YNAB gives each spelling its own payee id. With id-based matching, distilled rules and payee history rarely matched recent imports, so almost everything was flagged `new_payee` and could not be auto-approved (3 auto of 99 in the first dry run).
**Decision:** Payees are keyed by their name, casefolded and with whitespace collapsed (`payee_key`). This applies to profiles, payee rules (`rules: - payee: "…"`), the `new_payee`/`category_drift` anomalies, correction tracking and the payee text sent to Claude.
**Consequences:**
- Auto-approvals in the same dry run went from 3 to 19.
- Different YNAB payees that share a normalized name are merged. Example: the bank's generic loan-instalment payee covers both the property-investment loans and the mortgage, which lowers its purity; that category is therefore set to `suggest`.
- Spellings that differ by more than case/spacing (`Supermarket Town-Name` vs `Supermarket Twn Nm`) remain separate payees until history builds up.
- Older `policy.yaml` files with `payee_id` rules must be regenerated with `distill`.

### D11 — Flat 0.85 confidence threshold and a 20% amount tolerance
**Status:** Accepted · 2026-10-31
**Decision:** `distill` proposes `min_confidence: 0.85` for every category (default 0.85). Instead of inflating each `max_amount`, the gate allows `max_amount × (1 + defaults.amount_tolerance)`, with `amount_tolerance: 0.20`.
**Consequences:** `max_amount` keeps matching the p95 statistic shown in the draft comments, while one setting widens or narrows all limits. The `amount_outlier` anomaly (p95 × 1.5) is unchanged.

### D12 — Process all unapproved transactions, not a recent window
**Status:** Accepted · 2026-10-31
**Context:** The first live run (no delta cursor yet) and every dry run used to look back only 60 days. The family has unapproved transactions going back to November 2025 (361 in total).
**Decision:** Dry runs and the first live run fetch **all** unapproved transactions (`type=unapproved`, no date limit); later live runs use the delta cursor. Because a capped run leaves the remainder as permanent suggestions, `max_auto_per_run` is raised to 100 for the first live run and set back to 30 afterwards.
**Consequences:** The first live run is large (about 65 auto, about 285 suggestions in the dry run) and uses several Claude calls (chunks of 40).
**Amended 2026-10-31 (after the first live run):** without `since_date`, YNAB returns only about the last 12 months, so the first run missed 285 unapproved transactions from Jun–Oct 2025. All-unapproved fetches now pass `since_date=2000-01-01`. `approver run --all` processes every unapproved transaction not yet handled (one extra request), which also recovers skipped transactions; anything with an `[AI:` memo is excluded so nothing is patched twice.

### D13 — Human-readable run log
**Status:** Accepted · 2026-10-31
**Decision:** Every command writes to `state/approver.log` (rotating, 1 MB × 5): start/end with options, console output, one line per decision for `run` (dry-run lines prefixed `[dry]`), and errors with traceback. Known tokens are replaced by `***` in the formatted output. `audit.jsonl` stays the machine-readable record used by `undo`.
**Consequences:** Manual runs are logged too, not only scheduled ones (launchd still captures stdout in `state/run.log`).

### D14 — Family rules as free-text policy instructions
**Status:** Accepted · 2026-11-01
**Context:** Some categorization depends on context that isn't in payee history, e.g. "everything spent abroad belongs to the trip", even at a supermarket.
**Decision:** `policy.yaml` gets an `instructions:` list. The runner appends it to Claude's system prompt as trusted family rules that override payee history and profiles (payee and memo text stays untrusted). First rule: every purchase made outside Belgium is `family trip/activities`; online shops and subscriptions billed from abroad are not. That category's description also mentions spending abroad.
**Consequences:**
- The AI only proposes; the gate still decides. `family trip/activities` is `suggest`, so abroad spending always gets a human look.
- Payee rules run before the AI, so a foreign branch whose normalized name matches a rule would bypass the instruction (none today: rule payees include the Belgian town).
- Already-processed transactions are not re-evaluated.
- Detection relies on the payee text (town, country, chain); a foreign payment whose bank text shows no location may be missed.

### D15 — Policy update after the first full review
**Status:** Accepted · 2026-11-01
**Context:** The family approved all 638 AI-handled transactions (95 auto, 543 suggestions). Comparing the AI's category with the approved one: auto-approvals 95/95 correct; suggestions 89–100% for most categories, but `❗️Stuff I forgot to budget for` 0/23, `goodwill` 0/8, `🏠 Mortgage` 0/4, `Asset` 2/5, `books` 5/10, `General home` 87/116.
**Decision:** Re-distilled 24 months (78 payee rules, up from 48; new categories 📈 Investments, Asset, 🍳️ Heating) and merged with the family's choices:
- Measured accuracy overrides the distill heuristic: Groceries, Dining out and Kids activities stay `auto` (100% correct) although the draft demoted them; `books` stays `suggest` (50%).
- Newly `auto` per the draft and confirmed by accuracy: clothes (12/12), 💻 Internet (10/10), 🎊 Celebrations (4/4).
- Kept: Property Investment `suggest` (D10), Esso → ⛽️ Gas, train, bus (history says Dining out), the abroad instruction (D14).
- New instructions: never propose `Stuff I forgot to budget for` / `goodwill`; P2P payments to persons → General home or Home maintenance & improvement; transfers to the family's own bank/savings accounts → 📈 Investments; Decathlon → clothes; school payments → Back to school.
**Consequences:** The recurring `DEBIT` €4.50 charge (approved inconsistently as Bank costs, Taxes and General home) is decided by the family as **Bank costs**: payee rule `debit` → Bank costs, and Bank costs is `auto` with `max_amount: 5`, so other DEBIT amounts still get a human look. Payee rules are exempt from the `corrected_payee` anomaly, because a rule is the family's explicit choice and is written after the correction. Previous policy backed up outside the repo before the merge.

### D16 — Learning loop: reviews turn into auto-approvals
**Status:** Accepted · 2026-11-01
**Context:** The family wants reviewed transactions to be auto-approved next time. Previously a corrected payee was blocked from auto forever, and payees approved outside the approver stayed `new_payee` until the next distill.
**Decision:**
- When a person approves an AI suggestion, the payee's history is updated (as before). When the suggestion is accepted unchanged, any `corrected_payee` block on that payee is lifted.
- `distill` absorbs reviews: approved AI-handled transactions are logged as `accepted`/`corrected`, no longer tracked, and the correction marks are cleared, since the rebuilt profiles already contain the human categories. `category_drift` still blocks an AI proposal that contradicts a payee's history.
- Payee rules are exempt from `corrected_payee` (D15).
**Consequences:**
- Loop for a payee in an `auto` category: first transaction → yellow suggestion; once approved → the next one can be auto-approved (confidence ≥ 0.85, no other anomaly).
- Payees a person categorizes without the AI (skips, manual entries) are learned at the next `distill`; running it weekly keeps profiles fresh. It never touches `policy.yaml`; new payee rules still need a human merge from the draft.
- First absorb (2026-11-01): 544 suggestions accepted, 94 corrected.
- `backtest` under-estimates auto rates for recently learned payees, because it rebuilds history from transactions older than 30 days.

### D17 — Weekly additive auto-merge of the policy
**Status:** Accepted · 2026-11-01 · *Amends D6 (human promotes the draft) and D16*
**Decision:** `distill --merge` (weekly via launchd) merges the fresh draft into `policy.yaml`, additively:
- new payee rules are added (with `note: auto-merged <date>`); a payee that already has a rule keeps it, even if history now differs (e.g. Esso → fuel), and that is logged;
- new categories are added with at most `suggest` mode, so a category never becomes auto-approved without a person;
- existing categories, defaults, instructions and `never_auto_when` are never changed, and nothing is removed.
Before writing, the current file is copied to `policy-history/policy.<timestamp>.yaml`; the file is only rewritten when something was added. Changes are printed and logged.
**Consequences:** New rules can auto-approve straight away when their category is `auto` (a distilled rule needs ≥ 3 transactions, ≥ 95% in one category, and no recent exception). The policy file is now regenerated by code: YAML comments other than the statistics are not preserved, so remarks go in a rule's `note`. To roll back: copy a file from `policy-history/` over `policy.yaml`.

### D18 — Chunked writes, recorded per batch
**Status:** Accepted · 2026-11-01 · *Amends D4 (one bulk PATCH per run)*
**Context:** After importing Mar–Sep 2026 (692 unapproved transactions), the single bulk PATCH failed with "connection reset by peer" after 14 minutes of classification; nothing was applied, and all Claude work was lost.
**Decision:** Bulk PATCHes carry at most 100 transactions. After each accepted batch, its decisions are written to the audit log and `ai_txns`, so finished batches survive a later failure and remain undoable; the delta cursor only advances when the whole run succeeds. The per-run YNAB request cap goes from 10 to 20.
**Per-run cap:** an auto-eligible transaction beyond `max_auto_per_run` is now *deferred* (left untouched, not tracked) instead of becoming a suggestion, so the next `run --all` auto-approves it. The 76 transactions that the 2026-11-01 import run had turned into suggestions this way were approved afterwards (audit event `promoted`; `undo` still restores their original state).
**Consequences:** Normal runs still use one write. A large import costs one request per 100 transactions. If a batch fails without a clear answer, YNAB may have applied it unrecorded; the `[AI:` memo filter prevents processing those twice.

---

## Open decisions (Proposed)

### P1 — Where does it run?
Options: launchd on the MacBook (simplest, but only runs while the Mac is awake), Docker on a NAS or Raspberry Pi (always on), or a GitHub Actions cron (needs the state files persisted, e.g. as a cache or artifact).
**Recommendation:** start on the Mac, and move to the NAS once it's stable.
**Current (2026-11-01):** launchd on the Mac. `run --all` daily at 07:00 (daily instead of every 30 min: transactions import in batches and review happens in YNAB anyway; `--all` retries earlier skips), `distill --months 24` weekly on Sunday 06:30 (D16). A missed time runs at wake.

### P2 — Which categories start on `auto`?
**Recommendation:** use what `distill` + `backtest` shows has high purity and accuracy, typically Groceries, Fuel and Subscriptions. Everything else starts on `suggest`.
**Current choice (2026-10-31, from the 24-month draft):** `auto` for 🛒 Groceries, 🍽️ Dining out, ⛽️ Gas/train/bus, ⭐ Kids activities, ⚡️ Electricity and Insurance. Property Investment is `suggest` (shared loan payee, see D10). The Esso payee rule maps to ⛽️ Gas, train, bus. Revisit after a few weeks of audit data and a `backtest`.

### P3 — Sensitive categories that are never auto-approved
Default list: Medical, Taxes, Gifts. The family to confirm or extend.
Note: matching is by substring of the category name (`medical`, `health`, `tax`, `gift`, `doctor`, `pharmacy`), so the family's `medicine` category is *not* caught; it ended up `suggest` on its own merits.

### P4 — Flag colours
Default: purple = AI-approved, yellow = AI suggestion. Change these if the family already uses those colours.
