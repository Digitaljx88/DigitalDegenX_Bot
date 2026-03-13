from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TradeCenterState:
    view: str = "ledger"   # ledger | closed | stats
    filter_spec: str = "all"
    page: int = 1


def parse_trade_command_args(args: list[str]) -> TradeCenterState:
    state = TradeCenterState()
    tokens = [a.strip() for a in args if str(a).strip()]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lower = token.lower()
        if lower == "page" and i + 1 < len(tokens):
            try:
                state.page = max(1, int(tokens[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        if lower in {"ledger", "closed", "stats"}:
            state.view = lower
        elif lower in {"all", "win", "wins", "loss", "losses", "buy", "buys", "sell", "sells", "paper", "live"}:
            state.filter_spec = normalize_filter_spec(lower)
        elif ":" in token and len(token.split(":")[0]) == 10:
            state.filter_spec = f"date:{token}"
        else:
            state.filter_spec = f"search:{token}"
        i += 1
    return state


def normalize_filter_spec(raw: str | None) -> str:
    if not raw:
        return "all"
    value = raw.lower()
    aliases = {
        "win": "wins",
        "loss": "losses",
        "buy": "buys",
        "sell": "sells",
    }
    return aliases.get(value, value)


def filter_trade_rows(trades: list[dict], filter_spec: str) -> list[dict]:
    filter_spec = normalize_filter_spec(filter_spec)
    if filter_spec == "all":
        return list(trades)
    if filter_spec == "wins":
        return [t for t in trades if str(t.get("action", "")).lower() == "sell" and float(t.get("pnl_pct") or 0) > 0]
    if filter_spec == "losses":
        return [t for t in trades if str(t.get("action", "")).lower() == "sell" and float(t.get("pnl_pct") or 0) <= 0]
    if filter_spec == "buys":
        return [t for t in trades if str(t.get("action", "")).lower() == "buy"]
    if filter_spec == "sells":
        return [t for t in trades if str(t.get("action", "")).lower() == "sell"]
    if filter_spec == "paper":
        return [t for t in trades if str(t.get("mode", "")).lower() == "paper"]
    if filter_spec == "live":
        return [t for t in trades if str(t.get("mode", "")).lower() == "live"]
    if filter_spec.startswith("date:"):
        date_range = filter_spec.split(":", 1)[1]
        try:
            date1, date2 = date_range.split(":", 1)
        except ValueError:
            return list(trades)
        return [t for t in trades if date1 <= str(t.get("date", "")) <= date2]
    if filter_spec.startswith("search:"):
        query = filter_spec.split(":", 1)[1].lower()
        return [
            t for t in trades
            if query in str(t.get("symbol", "")).lower()
            or query in str(t.get("mint", "")).lower()
            or query in str(t.get("name", "")).lower()
        ]
    return list(trades)


def build_closed_trades(trades: list[dict]) -> list[dict]:
    ordered = sorted(trades, key=lambda t: t.get("ts", 0))
    buy_queue: dict[str, list[dict]] = {}
    closed: list[dict] = []

    for trade in ordered:
        mint = trade.get("mint", "")
        action = str(trade.get("action", "")).lower()
        if action == "buy":
            buy_queue.setdefault(mint, []).append(trade)
            continue
        if action != "sell" or not buy_queue.get(mint):
            continue

        buy_trade = buy_queue[mint].pop(0)
        sol_in = float(buy_trade.get("sol_amount") or 0)
        sol_out = float(trade.get("sol_received") or 0)
        pnl_sol = sol_out - sol_in
        pnl_pct = float(trade.get("pnl_pct") or 0)
        if sol_in > 0 and not pnl_pct:
            pnl_pct = (pnl_sol / sol_in) * 100
        closed.append({
            "symbol": trade.get("symbol") or buy_trade.get("symbol") or "?",
            "name": trade.get("name") or buy_trade.get("name") or "",
            "mint": mint,
            "mode": trade.get("mode") or buy_trade.get("mode") or "?",
            "buy_ts": buy_trade.get("ts", 0),
            "sell_ts": trade.get("ts", 0),
            "buy_price_usd": float(buy_trade.get("price_usd") or 0),
            "sell_price_usd": float(trade.get("price_usd") or 0),
            "sol_in": sol_in,
            "sol_out": sol_out,
            "pnl_sol": pnl_sol,
            "pnl_pct": pnl_pct,
            "hold_s": max(0, float(trade.get("ts", 0) or 0) - float(buy_trade.get("ts", 0) or 0)),
            "tx_sig": trade.get("tx_sig") or "",
        })

    closed.sort(key=lambda row: row.get("sell_ts", 0), reverse=True)
    return closed


def filter_closed_trades(closed_trades: list[dict], filter_spec: str) -> list[dict]:
    filter_spec = normalize_filter_spec(filter_spec)
    if filter_spec == "all":
        return list(closed_trades)
    if filter_spec == "wins":
        return [t for t in closed_trades if float(t.get("pnl_sol") or 0) > 0]
    if filter_spec == "losses":
        return [t for t in closed_trades if float(t.get("pnl_sol") or 0) <= 0]
    if filter_spec == "paper":
        return [t for t in closed_trades if str(t.get("mode", "")).lower() == "paper"]
    if filter_spec == "live":
        return [t for t in closed_trades if str(t.get("mode", "")).lower() == "live"]
    if filter_spec.startswith("date:"):
        date_range = filter_spec.split(":", 1)[1]
        try:
            date1, date2 = date_range.split(":", 1)
        except ValueError:
            return list(closed_trades)
        return [
            t for t in closed_trades
            if date1 <= datetime.fromtimestamp(t.get("sell_ts", 0), tz=timezone.utc).strftime("%Y-%m-%d") <= date2
        ]
    if filter_spec.startswith("search:"):
        query = filter_spec.split(":", 1)[1].lower()
        return [
            t for t in closed_trades
            if query in str(t.get("symbol", "")).lower()
            or query in str(t.get("mint", "")).lower()
            or query in str(t.get("name", "")).lower()
        ]
    return list(closed_trades)


def summarize_trades(trades: list[dict], closed_trades: list[dict]) -> dict:
    buys = [t for t in trades if str(t.get("action", "")).lower() == "buy"]
    sells = [t for t in trades if str(t.get("action", "")).lower() == "sell"]
    live = [t for t in trades if str(t.get("mode", "")).lower() == "live"]
    paper = [t for t in trades if str(t.get("mode", "")).lower() == "paper"]
    realized_pnl = sum(float(t.get("pnl_sol") or 0) for t in closed_trades)
    wins = [t for t in closed_trades if float(t.get("pnl_sol") or 0) > 0]
    losses = [t for t in closed_trades if float(t.get("pnl_sol") or 0) <= 0]
    best = max(closed_trades, key=lambda t: float(t.get("pnl_sol") or 0), default=None)
    worst = min(closed_trades, key=lambda t: float(t.get("pnl_sol") or 0), default=None)
    avg_hold = sum(float(t.get("hold_s") or 0) for t in closed_trades) / len(closed_trades) if closed_trades else 0.0
    narratives: dict[str, int] = {}
    for trade in trades:
        narrative = str(trade.get("narrative") or "Other")
        narratives[narrative] = narratives.get(narrative, 0) + 1
    top_narrative = max(narratives.items(), key=lambda kv: kv[1])[0] if narratives else "None"
    return {
        "total_rows": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "closed_count": len(closed_trades),
        "live_count": len(live),
        "paper_count": len(paper),
        "realized_pnl_sol": realized_pnl,
        "win_rate": (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0,
        "best_trade": best,
        "worst_trade": worst,
        "avg_hold_s": avg_hold,
        "top_narrative": top_narrative,
        "winning_count": len(wins),
        "losing_count": len(losses),
    }
