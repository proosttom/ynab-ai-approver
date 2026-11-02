"""Thin wrapper around the official SDK: request budget, 429 backoff, normalized transactions."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import ynab

from .config import Settings, payee_key


class RateBudgetExceeded(Exception):
    pass


@dataclass
class Txn:
    id: str
    date: str
    amount: int  # milliunits, negative = outflow
    payee_id: str | None
    payee_name: str
    account: str
    memo: str
    category_id: str | None
    approved: bool
    flag: str
    transfer: bool
    deleted: bool
    subs: list[dict] = field(default_factory=list)  # [{category_id, amount, payee_id}]

    @property
    def eur(self) -> float:
        return abs(self.amount) / 1000

    @property
    def payee(self) -> str:
        return payee_key(self.payee_name)

    @classmethod
    def from_sdk(cls, t) -> "Txn":
        flag = t.flag_color.value if hasattr(t.flag_color, "value") else (t.flag_color or "")
        return cls(
            id=_s(t.id), date=str(t.var_date), amount=t.amount, payee_id=_s(t.payee_id),
            payee_name=t.payee_name or t.import_payee_name or "", account=t.account_name or "",
            memo=t.memo or "", category_id=_s(t.category_id), approved=bool(t.approved), flag=flag or "",
            transfer=bool(t.transfer_account_id), deleted=bool(t.deleted),
            subs=[{"category_id": _s(s.category_id), "amount": s.amount, "payee_id": _s(s.payee_id or t.payee_id)}
                  for s in (t.subtransactions or []) if not s.deleted and not s.transfer_account_id],
        )


def _s(v) -> str | None:
    return None if v is None else str(v)


def _header(headers, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


class Ynab:
    def __init__(self, s: Settings):
        self.plan = s.ynab_plan_id
        self.cap, self.reserve = s.approver_max_requests_per_run, s.approver_rate_limit_reserve
        self.used, self.remaining = 0, None
        client = ynab.ApiClient(ynab.Configuration(access_token=s.ynab_api_token.get_secret_value()))
        self._tx, self._cat = ynab.TransactionsApi(client), ynab.CategoriesApi(client)

    def _track(self, headers):
        v = _header(headers, "X-Rate-Limit")  # e.g. "36/200"
        if v and "/" in v:
            used, limit = (int(x) for x in v.split("/", 1))
            self.remaining = limit - used

    def _call(self, fn, *args, retry: bool = True, **kw):
        for attempt in range(3):
            if self.used >= self.cap:
                raise RateBudgetExceeded(f"per-run cap of {self.cap} YNAB requests reached")
            if self.remaining is not None and self.remaining <= self.reserve:
                raise RateBudgetExceeded(f"only {self.remaining} YNAB requests left this hour (reserve {self.reserve})")
            self.used += 1
            try:
                r = fn(*args, **kw)
            except ynab.ApiException as e:
                self._track(e.headers)
                if e.status == 429 and retry and attempt < 2:
                    time.sleep(min(int(_header(e.headers, "Retry-After") or 30), 120) * (attempt + 1))
                    continue
                raise
            self._track(r.headers)
            return r.data.data
        raise RateBudgetExceeded("YNAB kept answering 429")

    def transactions(self, since: date | None = None, knowledge: int | None = None, type: str | None = None):
        d = self._call(self._tx.get_transactions_with_http_info, self.plan, since_date=since,
                       last_knowledge_of_server=knowledge, type=type)
        return [Txn.from_sdk(t) for t in d.transactions], d.server_knowledge

    def categories(self, knowledge: int | None = None):
        """Returns ({id: {name, group, deleted}}, server_knowledge). Internal/credit-card groups excluded."""
        d = self._call(self._cat.get_categories_with_http_info, self.plan, last_knowledge_of_server=knowledge)
        out = {}
        for g in d.category_groups:
            if g.internal or g.name == "Credit Card Payments":
                continue
            for c in g.categories:
                out[str(c.id)] = {"name": c.name, "group": g.name, "deleted": bool(c.deleted or c.hidden or g.deleted)}
        return out, d.server_knowledge

    def update(self, patches: list[ynab.SaveTransactionWithIdOrImportId]) -> None:
        """One bulk PATCH. Never auto-retried: a blind retry could double-apply."""
        if patches:
            self._call(self._tx.update_transactions_with_http_info, self.plan,
                       ynab.PatchTransactionsWrapper(transactions=patches), retry=False)
