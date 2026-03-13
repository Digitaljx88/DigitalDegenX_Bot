from __future__ import annotations

import pytest

import trade_center as tc


def _trade(ts: float, action: str, symbol: str = "TEST", **overrides) -> dict:
    row = {
        "ts": ts,
        "date": "2026-03-12",
        "action": action,
        "symbol": symbol,
        "name": symbol,
        "mint": f"{symbol}_MINT",
        "mode": "paper",
        "sol_amount": 0.5 if action == "buy" else None,
        "sol_received": 0.7 if action == "sell" else None,
        "price_usd": 0.01,
        "buy_price_usd": 0.01 if action == "sell" else None,
        "pnl_pct": 40.0 if action == "sell" else None,
        "narrative": "AI",
        "token_amount": 1000,
    }
    row.update(overrides)
    return row


def test_parse_trade_command_args_supports_view_filter_and_page():
    state = tc.parse_trade_command_args(["closed", "wins", "page", "3"])
    assert state.view == "closed"
    assert state.filter_spec == "wins"
    assert state.page == 3


def test_parse_trade_command_args_supports_search_and_date():
    search_state = tc.parse_trade_command_args(["BONK"])
    date_state = tc.parse_trade_command_args(["2026-03-01:2026-03-10"])
    assert search_state.filter_spec == "search:BONK"
    assert date_state.filter_spec == "date:2026-03-01:2026-03-10"


def test_filter_trade_rows_supports_mode_and_symbol_search():
    trades = [
        _trade(1, "buy", symbol="BONK", mode="paper"),
        _trade(2, "sell", symbol="WIF", mode="live", pnl_pct=-10),
    ]
    assert len(tc.filter_trade_rows(trades, "paper")) == 1
    assert tc.filter_trade_rows(trades, "search:bonk")[0]["symbol"] == "BONK"


def test_build_closed_trades_pairs_buys_and_sells():
    trades = [
        _trade(1, "buy", symbol="BONK", sol_amount=0.5, mint="M1"),
        _trade(2, "sell", symbol="BONK", sol_received=0.8, pnl_pct=60.0, mint="M1"),
    ]
    closed = tc.build_closed_trades(trades)
    assert len(closed) == 1
    assert closed[0]["symbol"] == "BONK"
    assert closed[0]["pnl_sol"] == pytest.approx(0.3)


def test_summarize_trades_reports_realized_pnl_and_counts():
    trades = [
        _trade(1, "buy", symbol="BONK", mint="M1"),
        _trade(2, "sell", symbol="BONK", mint="M1", sol_received=0.8, pnl_pct=60.0),
        _trade(3, "buy", symbol="WIF", mint="M2", mode="live"),
    ]
    closed = tc.build_closed_trades(trades)
    summary = tc.summarize_trades(trades, closed)
    assert summary["total_rows"] == 3
    assert summary["closed_count"] == 1
    assert summary["realized_pnl_sol"] > 0
    assert summary["top_narrative"] == "AI"
