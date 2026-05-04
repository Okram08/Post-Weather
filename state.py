"""Gist-based state persistence — keeps state outside of the repo.

Gist files used:
  - bankroll.json       : current BankrollState
  - emitted_signals.json: idempotency set (last 500 signal IDs)
  - signals_log.json    : audit log of every emitted/rejected signal
  - audit_log.json      : audit log of state-changing events (halts, resets)
"""
import json
from datetime import datetime, timezone
from typing import Any

import requests

from src.bankroll import BankrollState

GIST_API = "https://api.github.com/gists"
TIMEOUT = 15


class GistState:
    def __init__(self, pat: str, gist_id: str):
        if not pat or not gist_id:
            raise ValueError("GIST_PAT and GIST_STATE_ID must be set")
        self.pat = pat
        self.gist_id = gist_id
        self.headers = {
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_files(self) -> dict:
        r = requests.get(f"{GIST_API}/{self.gist_id}",
                         headers=self.headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("files", {})

    def read(self, filename: str) -> Any:
        files = self._get_files()
        if filename not in files:
            return {}
        content = files[filename].get("content", "")
        if not content:
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}

    def write(self, filename: str, data: Any) -> None:
        body = {"files": {filename: {"content": json.dumps(data, indent=2, default=str)}}}
        r = requests.patch(f"{GIST_API}/{self.gist_id}", json=body,
                           headers=self.headers, timeout=TIMEOUT)
        r.raise_for_status()

    def append_log(self, filename: str, entry: dict, max_entries: int = 1000) -> None:
        """Append an entry to a JSON list in the gist (capped at max_entries)."""
        data = self.read(filename)
        if not isinstance(data, list):
            data = []
        entry = dict(entry)
        entry["_logged_at"] = datetime.now(timezone.utc).isoformat()
        data.append(entry)
        data = data[-max_entries:]
        self.write(filename, data)


# ---- Bankroll helpers ----

def load_bankroll(state: GistState, defaults: dict) -> BankrollState:
    raw = state.read("bankroll.json")
    if not raw or not isinstance(raw, dict):
        now = datetime.now(timezone.utc).isoformat()
        return BankrollState(
            equity_usd=defaults["initial_capital_usd"],
            peak_equity_usd=defaults["initial_capital_usd"],
            daily_pnl_usd=0.0,
            daily_reset_at=now,
            open_positions=[],
            halted=False,
            halt_reason="",
            last_updated=now,
        )
    # Backward-compat: ensure all keys exist
    return BankrollState(
        equity_usd=raw.get("equity_usd", defaults["initial_capital_usd"]),
        peak_equity_usd=raw.get("peak_equity_usd", defaults["initial_capital_usd"]),
        daily_pnl_usd=raw.get("daily_pnl_usd", 0.0),
        daily_reset_at=raw.get("daily_reset_at",
                               datetime.now(timezone.utc).isoformat()),
        open_positions=raw.get("open_positions", []),
        halted=raw.get("halted", False),
        halt_reason=raw.get("halt_reason", ""),
        last_updated=raw.get("last_updated", ""),
    )


def save_bankroll(state: GistState, bankroll: BankrollState) -> None:
    state.write("bankroll.json", bankroll.to_dict())
