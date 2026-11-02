"""LLM backends: Claude Code CLI on your subscription (default) or the pay-per-use API."""
from __future__ import annotations

import json
import os
import subprocess

from .config import Settings


class UsageLimit(Exception):
    """Subscription usage limit reached; skip this run, the next one will pick up."""


class ClaudeCli:
    def __init__(self, s: Settings):
        self.model = s.approver_model
        self.env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        if s.claude_code_oauth_token and s.claude_code_oauth_token.get_secret_value():
            self.env["CLAUDE_CODE_OAUTH_TOKEN"] = s.claude_code_oauth_token.get_secret_value()

    def __call__(self, system: str, user: str, schema: dict) -> dict:
        cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--model", self.model, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--system-prompt", system]
        try:
            p = subprocess.run(cmd, input=user, capture_output=True, text=True, env=self.env, timeout=600)
        except FileNotFoundError:
            raise SystemExit("Claude Code CLI (`claude`) not found on PATH.")
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude CLI failed ({p.returncode}): {(p.stderr or p.stdout)[-500:]}")
        text = str(out.get("result", ""))
        if out.get("is_error"):
            if "limit" in text.lower():
                raise UsageLimit(text)
            raise RuntimeError(f"claude CLI error: {text[:500]}")
        if isinstance(out.get("structured_output"), dict):
            return out["structured_output"]
        return json.loads(text.strip().removeprefix("```json").removesuffix("```"))


class Api:
    def __init__(self, s: Settings):
        import anthropic  # optional dependency: pip install .[api]

        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value())
        self.model = s.approver_model if s.approver_model != "haiku" else "claude-haiku-4-5"

    def __call__(self, system: str, user: str, schema: dict) -> dict:
        r = self.client.messages.create(
            model=self.model, max_tokens=8000, system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": "submit", "description": "Submit the answer.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "submit"},
        )
        return next(b.input for b in r.content if b.type == "tool_use")


def make_llm(s: Settings):
    return ClaudeCli(s) if s.approver_llm_backend == "claude-cli" else Api(s)
