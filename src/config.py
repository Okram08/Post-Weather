"""Config loader: yaml + env vars (secrets)."""
import os
from dataclasses import dataclass, field
from typing import List

import yaml


@dataclass
class StrategyConfig:
    pairs: List[str]
    setup_a: dict


@dataclass
class BankrollConfig:
    initial_capital_usd: float
    risk_per_trade_pct: float
    max_concurrent_positions: int
    max_leverage: float
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    sizing_model: str = "fixed"
    kelly_fraction: float = 0.25


@dataclass
class FrictionsConfig:
    exchange: str
    maker_fee_bps: float
    taker_fee_bps: float


@dataclass
class TelegramConfig:
    enabled: bool
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class GistConfig:
    pat: str = ""
    state_gist_id: str = ""


@dataclass
class Config:
    strategy: StrategyConfig
    bankroll: BankrollConfig
    frictions: FrictionsConfig
    telegram: TelegramConfig
    gist: GistConfig


def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config(
        strategy=StrategyConfig(**raw["strategy"]),
        bankroll=BankrollConfig(**raw["bankroll"]),
        frictions=FrictionsConfig(**raw["frictions"]),
        telegram=TelegramConfig(
            enabled=raw["telegram"]["enabled"],
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        ),
        gist=GistConfig(
            pat=os.environ.get("GIST_PAT", ""),
            state_gist_id=os.environ.get("GIST_STATE_ID", ""),
        ),
    )
