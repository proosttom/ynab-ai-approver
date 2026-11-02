"""Settings (env vars first, then .env) and policy.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def payee_key(name: str | None) -> str:
    """Case- and whitespace-insensitive payee identity: 'COLRUYT  St K' == 'colruyt st k'."""
    return " ".join((name or "").casefold().split())

Mode = Literal["auto", "suggest", "off"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ynab_api_token: SecretStr
    ynab_plan_id: str = "last-used"
    claude_code_oauth_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    approver_llm_backend: Literal["claude-cli", "api"] = "claude-cli"
    approver_model: str = "haiku"
    approver_max_requests_per_run: int = 20
    approver_rate_limit_reserve: int = 50
    approver_state_dir: Path = Path("state")
    approver_policy_file: Path = Path("policy.yaml")

    @model_validator(mode="after")
    def _check_backend(self):
        if self.approver_llm_backend == "claude-cli" and self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is set: Claude Code would bill the API instead of your "
                "subscription. Unset it, or set APPROVER_LLM_BACKEND=api."
            )
        if self.approver_llm_backend == "api" and not self.anthropic_api_key:
            raise ValueError("APPROVER_LLM_BACKEND=api requires ANTHROPIC_API_KEY.")
        return self


def _yaml_off(v):
    return "off" if v is False else v  # YAML 1.1 reads a bare `off` as false


class Defaults(BaseModel):
    mode: Mode = "suggest"
    min_confidence: float = 0.85
    max_amount: float = 150
    amount_tolerance: float = 0.20  # auto-approve up to max_amount * (1 + tolerance)
    max_auto_per_run: int = 30
    flag_auto: str = "purple"
    flag_suggest: str = "yellow"

    _off = field_validator("mode", mode="before")(_yaml_off)


class CategoryPolicy(BaseModel):
    mode: Mode | None = None
    min_confidence: float | None = None
    max_amount: float | None = None
    description: str = ""

    _off = field_validator("mode", mode="before")(_yaml_off)


class Rule(BaseModel):
    payee: str  # matched case- and whitespace-insensitively
    category: str
    note: str = ""

    @field_validator("payee")
    @classmethod
    def _norm(cls, v: str) -> str:
        return payee_key(v)


class Policy(BaseModel):
    defaults: Defaults = Defaults()
    categories: dict[str, CategoryPolicy] = {}
    rules: list[Rule] = []
    instructions: list[str] = []  # family rules for the classifier; they override payee history
    never_auto_when: list[str] = ["new_payee", "amount_outlier", "category_drift", "corrected_payee", "split"]

    def for_category(self, label: str) -> tuple[Mode, float, float]:
        c, d = self.categories.get(label, CategoryPolicy()), self.defaults
        return (
            c.mode or d.mode,
            c.min_confidence if c.min_confidence is not None else d.min_confidence,
            c.max_amount if c.max_amount is not None else d.max_amount,
        )

    @classmethod
    def load(cls, path: Path) -> "Policy":
        if not path.exists():
            raise SystemExit(f"{path} not found. Run `approver distill` and review policy.draft.yaml first.")
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
