from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src import mexc_futures_scraper as scanner


TIMESTAMP = 1789113090723
MINUTE = (TIMESTAMP // 1000 - 86400) // 60 * 60
CONTRACT = dict(symbol="ABC_USDT", quoteCoin="USDT", settleCoin="USDT",
                futureType=1, state=0, contractSize="0.1")
TICKER = dict(symbol="ABC_USDT", lastPrice="2.25", amount24="1000000",
              holdVol="600000", fundingRate="0", timestamp=TIMESTAMP)


@pytest.fixture
def candle_api(monkeypatch):
    monkeypatch.setattr(scanner.time, "sleep", lambda _: None)
    get = Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: dict(success=True, code=0,
                          data=dict(time=[MINUTE + 60, MINUTE], open=["9", "1.25"]))))
    monkeypatch.setattr(scanner.requests, "get", get)
    return get


def test_reference_uses_matching_minute_and_seconds(candle_api):
    assert scanner.fetch_reference_price("ABC_USDT", TIMESTAMP) == Decimal("1.25")
    assert candle_api.call_args.kwargs["params"] == dict(
        interval="Min1", start=MINUTE, end=MINUTE + 60)
    assert candle_api.call_args.args[0].endswith("/kline/ABC_USDT")


@pytest.mark.parametrize("fields", [{}, {"riseFallRate": "-0.9"},
                                    {"riseFallRate": "100", "riseFallRates": {"zone": "UTC+8"}}])
def test_rolling_gain_ignores_timezone_fields(candle_api, fields):
    coins, evaluated = scanner.select_gainers([TICKER | fields], [CONTRACT])
    assert coins[0]["gain"] == 80
    assert evaluated == {"ABC_USDT"}
    assert scanner.select_gainers([TICKER | fields | {"lastPrice": "1.99"}], [CONTRACT]) == (
        [], {"ABC_USDT"})


@pytest.mark.parametrize("data", [None, {}, {"time": [], "open": []},
    {"time": [MINUTE + 60], "open": ["1"]},
    {"time": [MINUTE - 60], "open": ["1"]},
    {"time": [MINUTE], "open": []},
    {"time": [MINUTE, MINUTE], "open": ["1", "1"]},
    *[{"time": [MINUTE], "open": [value]} for value in [0, -1, None, "NaN", "Infinity"]]])
def test_unusable_history_preserves_alert_state(candle_api, data):
    candle_api.return_value.json = lambda: dict(success=True, code=0, data=data)
    assert scanner.select_gainers([TICKER], [CONTRACT]) == ([], set())


def test_failed_history_preserves_alert_state(candle_api):
    candle_api.side_effect = requests.Timeout()
    assert scanner.select_gainers([TICKER], [CONTRACT]) == ([], set())


def test_liquidity_filter_avoids_history_requests(candle_api):
    assert scanner.select_gainers([TICKER | {"amount24": "999999"}], [CONTRACT]) == (
        [], {"ABC_USDT"})
    candle_api.assert_not_called()


@pytest.mark.parametrize("stamp", [None, "bad", 0, "NaN"])
def test_invalid_ticker_time_preserves_state(candle_api, stamp):
    assert scanner.select_gainers([TICKER | {"timestamp": stamp}], [CONTRACT]) == ([], set())
    candle_api.assert_not_called()
