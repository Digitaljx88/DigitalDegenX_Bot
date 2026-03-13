from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TradeQuery:
    view: str = "ledger"
    filter_spec: str = "all"
    page: int = 1


def normalize_filter_spec(value: str | None) -> str:
    if not value:
        return "all"
    aliases = {
        "win": "wins",
        "loss": "losses",
        "buy": "buys",
        "sell": "sells",
    }
    return aliases.get(value.lower(), value.lower())


def parse_trade_args(args: list[str]) -> TradeQuery:
    query = TradeQuery()
    parts = [str(a).strip() for a in args if str(a).strip()]
    i = 0
    while i < len(parts):
        part = parts[i]
        lower = part.lower()
        if lower in {"ledger", "closed", "stats"}:
            query.view = lower
        elif lower == "page" and i + 1 < len(parts):
            try:
                query.page = max(1, int(parts[i + 1]))
            except ValueError:
                pass
            i += 1
        elif lower in {"all", "wins", "losses", "win", "loss", "buys", "sells", "buy", "sell", "paper", "live"}:
            query.filter_spec = normalize_filter_spec(lower)
        elif ":" in part and len(part.split(":")[0]) == 10:
            query.filter_spec = f"date:{part}"
        else:
            query.filter_spec = f"search:{part}"
        i += 1
    return query


def filter_trades(trades: list[dict], filter_spec: str) -> list[dict]:
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
        try:
            date1, date2 = filter_spec.split(":", 1)[1].split(":", 1)
        except ValueError:
            return list(trades)
        return [t for t in trades if date1 <= str(t.get("date", "")) <= date2]
    if filter_spec.startswith("search:"):
        q = filter_spec.split(":", 1)[1].lower()
        return [
            t for t in trades
            if q in str(t.get("symbol", "")).lower()
            or q in str(t.get("mint", "")).lower()
            or q in str(t.get("name", "")).lower()
        ]
    return list(trades)


def build_closed_trades(trades: list[dict]) -> list[dict]:
    ordered = sorted(trades, key=lambda t: t.get("ts", 0))
    queues: dict[str, list[dict]] = {}
    closed: list[dict] = []
    for trade in ordered:
        mint = str(trade.get("mint", ""))
        action = str(trade.get("action", "")).lower()
        if action == "buy":
            queues.setdefault(mint, []).append(trade)
            continue
        if action != "sell" or not queues.get(mint):
            continue
        buy_trade = queues[mint].pop(0)
        sol_in = float(buy_trade.get("sol_amount") or 0)
        sol_out = float(trade.get("sol_received") or 0)
        pnl_sol = sol_out - sol_in
        pnl_pct = float(trade.get("pnl_pct") or 0)
        if not pnl_pct and sol_in > 0:
            pnl_pct = pnl_sol / sol_in * 100
        closed.append({
            "symbol": trade.get("symbol") or buy_trade.get("symbol") or "?",
            "name": trade.get("name") or buy_trade.get("name") or "",
            "mint": mint,
            "mode": trade.get("mode") or buy_trade.get("mode") or "?",
            "buy_ts": float(buy_trade.get("ts") or 0),
            "sell_ts": float(trade.get("ts") or 0),
            "sol_in": sol_in,
            "sol_out": sol_out,
            "pnl_sol": pnl_sol,
            "pnl_pct": pnl_pct,
            "hold_s": max(0.0, float(trade.get("ts") or 0) - float(buy_trade.get("ts") or 0)),
            "buy_price_usd": float(buy_trade.get("price_usd") or 0),
            "sell_price_usd": float(trade.get("price_usd") or 0),
            "tx_sig": trade.get("tx_sig") or "",
        })
    return sorted(closed, key=lambda row: row.get("sell_ts", 0), reverse=True)


def filter_closed_trades(closed: list[dict], filter_spec: str) -> list[dict]:
    filter_spec = normalize_filter_spec(filter_spec)
    if filter_spec == "all":
        return list(closed)
    if filter_spec == "wins":
        return [t for t in closed if float(t.get("pnl_sol") or 0) > 0]
    if filter_spec == "losses":
        return [t for t in closed if float(t.get("pnl_sol") or 0) <= 0]
    if filter_spec == "paper":
        return [t for t in closed if str(t.get("mode", "")).lower() == "paper"]
    if filter_spec == "live":
        return [t for t in closed if str(t.get("mode", "")).lower() == "live"]
    if filter_spec.startswith("date:"):
        try:
            date1, date2 = filter_spec.split(":", 1)[1].split(":", 1)
        except ValueError:
            return list(closed)
        return [
            t for t in closed
            if date1 <= datetime.fromtimestamp(float(t.get("sell_ts") or 0), tz=timezone.utc).strftime("%Y-%m-%d") <= date2
        ]
    if filter_spec.startswith("search:"):
        q = filter_spec.split(":", 1)[1].lower()
        return [
            t for t in closed
            if q in str(t.get("symbol", "")).lower()
            or q in str(t.get("mint", "")).lower()
            or q in str(t.get("name", "")).lower()
        ]
    return list(closed)


def summarize_trades(trades: list[dict], closed: list[dict]) -> dict:
    best = max(closed, key=lambda row: float(row.get("pnl_sol") or 0), default=None)
    worst = min(closed, key=lambda row: float(row.get("pnl_sol") or 0), default=None)
    narratives: dict[str, int] = {}
    for trade in trades:
        key = str(trade.get("narrative") or "Other")
        narratives[key] = narratives.get(key, 0) + 1
    avg_hold = sum(float(row.get("hold_s") or 0) for row in closed) / len(closed) if closed else 0.0
    return {
        "total_rows": len(trades),
        "buy_count": sum(1 for t in trades if str(t.get("action", "")).lower() == "buy"),
        "sell_count": sum(1 for t in trades if str(t.get("action", "")).lower() == "sell"),
        "paper_count": sum(1 for t in trades if str(t.get("mode", "")).lower() == "paper"),
        "live_count": sum(1 for t in trades if str(t.get("mode", "")).lower() == "live"),
        "closed_count": len(closed),
        "win_rate": (sum(1 for row in closed if float(row.get("pnl_sol") or 0) > 0) / len(closed) * 100) if closed else 0.0,
        "realized_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in closed),
        "avg_hold_s": avg_hold,
        "best_trade": best,
        "worst_trade": worst,
        "top_narrative": max(narratives, key=narratives.get) if narratives else "None",
    }
