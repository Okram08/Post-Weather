"""Tests for setup detection logic — run with: python -m pytest tests/"""
import pandas as pd
import pytest

from src.setups import detect_setup_a


PARAMS = {
    "setup_atr_extension": 1.5,
    "rsi_threshold": 30.0,
    "adx_max": 25.0,
    "funding_threshold": -0.0002,
    "limit_extension_atr": 2.5,
    "stop_atr": 1.5,
    "target_pct": 0.01,
}


def make_row(extension_atr, rsi, adx, funding_rate, vwap=100.0, atr=2.0):
    s = pd.Series({
        "atr": atr, "vwap_24h": vwap, "rsi": rsi, "adx": adx,
        "funding_rate": funding_rate, "extension_atr": extension_atr,
    })
    s.name = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
    return s


def test_no_setup_when_in_trend():
    row = make_row(extension_atr=-2.0, rsi=25, adx=35, funding_rate=-0.0005)
    assert detect_setup_a(row, "BTC/USDT:USDT", 100.0, PARAMS) is None


def test_long_setup():
    row = make_row(extension_atr=-2.0, rsi=25, adx=20, funding_rate=-0.0005)
    sig = detect_setup_a(row, "BTC/USDT:USDT", 95.0, PARAMS)
    assert sig is not None
    assert sig.direction == "long"
    # Limit is below VWAP - 2.5 ATR = 100 - 5 = 95
    assert sig.limit_price == pytest.approx(95.0)
    # Stop is limit - 1.5 ATR = 95 - 3 = 92
    assert sig.stop_price == pytest.approx(92.0)
    # Target is limit * 1.01 = 95.95
    assert sig.target_price == pytest.approx(95.95)


def test_short_setup():
    row = make_row(extension_atr=2.0, rsi=75, adx=20, funding_rate=0.0005)
    sig = detect_setup_a(row, "ETH/USDT:USDT", 105.0, PARAMS)
    assert sig is not None
    assert sig.direction == "short"
    # Limit is above VWAP + 2.5 ATR = 100 + 5 = 105
    assert sig.limit_price == pytest.approx(105.0)
    assert sig.stop_price == pytest.approx(108.0)
    assert sig.target_price == pytest.approx(103.95)


def test_no_setup_extension_too_small():
    row = make_row(extension_atr=-1.0, rsi=25, adx=20, funding_rate=-0.0005)
    assert detect_setup_a(row, "BTC/USDT:USDT", 100.0, PARAMS) is None


def test_no_setup_rsi_not_extreme():
    row = make_row(extension_atr=-2.0, rsi=40, adx=20, funding_rate=-0.0005)
    assert detect_setup_a(row, "BTC/USDT:USDT", 100.0, PARAMS) is None


def test_no_setup_funding_not_extreme():
    row = make_row(extension_atr=-2.0, rsi=25, adx=20, funding_rate=0.0)
    assert detect_setup_a(row, "BTC/USDT:USDT", 100.0, PARAMS) is None


def test_signal_id_unique_per_pair_dir_time():
    row1 = make_row(-2.0, 25, 20, -0.0005)
    sig1 = detect_setup_a(row1, "BTC/USDT:USDT", 95.0, PARAMS)
    sig2 = detect_setup_a(row1, "BTC/USDT:USDT", 95.0, PARAMS)
    sig3 = detect_setup_a(row1, "ETH/USDT:USDT", 95.0, PARAMS)
    assert sig1.signal_id == sig2.signal_id  # same inputs
    assert sig1.signal_id != sig3.signal_id  # different pair


def test_risk_distance_pct():
    row = make_row(extension_atr=-2.0, rsi=25, adx=20, funding_rate=-0.0005)
    sig = detect_setup_a(row, "BTC/USDT:USDT", 95.0, PARAMS)
    # |95 - 92| / 95 = 3.16%
    assert sig.risk_distance_pct == pytest.approx(3.0 / 95.0, rel=1e-4)
