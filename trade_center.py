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


def _score_band(score: float | int | None) -> str:
    value = float(score or 0)
    if value >= 90:
        return "90-100"
    if value >= 80:
        return "80-89"
    if value >= 70:
        return "70-79"
    if value >= 55:
        return "55-69"
    return "<55"


def _age_band(age_mins: float | int | None) -> str:
    value = float(age_mins or 0)
    if value < 5:
        return "0-5m"
    if value < 15:
        return "5-15m"
    if value < 30:
        return "15-30m"
    if value < 60:
        return "30-60m"
    return "60m+"


def _avg_metric(rows: list[dict], field: str) -> float:
    values = [float(row.get(field) or 0) for row in rows if row.get(field) is not None]
    return (sum(values) / len(values)) if values else 0.0


def _top_row(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None


def summarize_closed_cohorts(closed: list[dict]) -> dict:
    def _group(rows: list[dict], field: str, default: str = "Unknown") -> list[dict]:
        buckets: dict[str, dict] = {}
        for row in rows:
            key = str(row.get(field) or default)
            bucket = buckets.setdefault(
                key,
                {
                    "label": key,
                    "count": 0,
                    "wins": 0,
                    "realized_pnl_sol": 0.0,
                    "giveback_sum": 0.0,
                    "giveback_count": 0,
                    "peak_sum": 0.0,
                    "peak_count": 0,
                },
            )
            pnl_sol = float(row.get("pnl_sol") or 0)
            bucket["count"] += 1
            bucket["realized_pnl_sol"] += pnl_sol
            if pnl_sol > 0:
                bucket["wins"] += 1
            giveback = row.get("giveback_pct")
            if giveback is not None:
                bucket["giveback_sum"] += float(giveback or 0)
                bucket["giveback_count"] += 1
            peak = row.get("max_unrealized_pnl_pct")
            if peak is not None:
                bucket["peak_sum"] += float(peak or 0)
                bucket["peak_count"] += 1
        result = []
        for bucket in buckets.values():
            count = bucket["count"] or 1
            result.append({
                "label": bucket["label"],
                "count": bucket["count"],
                "win_rate": bucket["wins"] / count * 100.0,
                "realized_pnl_sol": bucket["realized_pnl_sol"],
                "avg_giveback_pct": (
                    bucket["giveback_sum"] / bucket["giveback_count"]
                    if bucket["giveback_count"]
                    else None
                ),
                "avg_peak_unrealized_pct": (
                    bucket["peak_sum"] / bucket["peak_count"]
                    if bucket["peak_count"]
                    else None
                ),
            })
        return sorted(result, key=lambda row: (row["realized_pnl_sol"], row["count"]), reverse=True)

    source_rows = _group(closed, "entry_source")
    narrative_rows = _group(closed, "narrative", "Other")
    archetype_rows = _group(closed, "entry_archetype", "NONE")
    strategy_rows = _group(closed, "entry_strategy", "none")
    exit_reason_rows = _group(closed, "exit_reason", "manual")

    score_rows = _group(
        [{**row, "_score_band": _score_band(row.get("entry_score_effective"))} for row in closed],
        "_score_band",
    )
    age_rows = _group(
        [{**row, "_age_band": _age_band(row.get("entry_age_mins"))} for row in closed],
        "_age_band",
    )

    return {
        "by_source": source_rows,
        "by_narrative": narrative_rows,
        "by_archetype": archetype_rows,
        "by_strategy": strategy_rows,
        "by_exit_reason": exit_reason_rows,
        "by_score_band": score_rows,
        "by_age_band": age_rows,
    }


def filter_recent_closed_trades(
    closed: list[dict],
    window_days: int = 7,
    now_ts: float | None = None,
) -> list[dict]:
    days = max(1, int(window_days or 7))
    current = float(now_ts or datetime.now(tz=timezone.utc).timestamp())
    cutoff = current - (days * 86400)
    return [row for row in closed if float(row.get("sell_ts") or 0) >= cutoff]


def build_optimization_report(
    closed: list[dict],
    window_days: int = 7,
    now_ts: float | None = None,
) -> dict:
    recent = filter_recent_closed_trades(closed, window_days=window_days, now_ts=now_ts)
    cohorts = summarize_closed_cohorts(recent)
    win_count = sum(1 for row in recent if float(row.get("pnl_sol") or 0) > 0)
    summary = {
        "window_days": max(1, int(window_days or 7)),
        "closed_count": len(recent),
        "win_rate": (win_count / len(recent) * 100.0) if recent else 0.0,
        "realized_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in recent),
        "avg_giveback_pct": _avg_metric(recent, "giveback_pct"),
        "avg_peak_unrealized_pct": _avg_metric(recent, "max_unrealized_pnl_pct"),
    }
    leaders = {
        "strategy": _top_row(cohorts["by_strategy"]),
        "source": _top_row(cohorts["by_source"]),
        "score_band": _top_row(cohorts["by_score_band"]),
        "age_band": _top_row(cohorts["by_age_band"]),
        "exit_reason": _top_row(cohorts["by_exit_reason"]),
        "narrative": _top_row(cohorts["by_narrative"]),
        "archetype": _top_row(cohorts["by_archetype"]),
    }

    insights: list[str] = []
    insight_specs = (
        ("strategy", "Best strategy this window"),
        ("source", "Best discovery source"),
        ("score_band", "Best entry score band"),
        ("age_band", "Best freshness band"),
        ("exit_reason", "Best exit behavior"),
    )
    for key, prefix in insight_specs:
        row = leaders.get(key)
        if not row:
            continue
        insights.append(
            f"{prefix}: {row['label']} ({row['realized_pnl_sol']:.4f} SOL, {row['count']} trades, {row['win_rate']:.0f}% win rate)"
        )

    return {
        "generated_at": float(now_ts or datetime.now(tz=timezone.utc).timestamp()),
        "summary": summary,
        "leaders": leaders,
        "cohorts": cohorts,
        "insights": insights,
    }


def filter_closed_trades(closed: list[dict], filter_spec: str) -> list[dict]:
    filter_spec = normalize_filter_spec(filter_spec)
    if filter_spec == "all":
        return list(closed)
    if filter_spec in {"buys", "sells"}:
        return []
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
    source_counts: dict[str, int] = {}
    archetype_counts: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    for row in closed:
        src = str(row.get("entry_source") or "Unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
        arch = str(row.get("entry_archetype") or "NONE")
        archetype_counts[arch] = archetype_counts.get(arch, 0) + 1
        strategy = str(row.get("entry_strategy") or "none")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
    cohort_summary = summarize_closed_cohorts(closed) if closed else {}
    exit_reason_rows = cohort_summary.get("by_exit_reason", [])
    strategy_rows = cohort_summary.get("by_strategy", [])
    source_rows = cohort_summary.get("by_source", [])
    narrative_rows = cohort_summary.get("by_narrative", [])
    archetype_rows = cohort_summary.get("by_archetype", [])
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
        "avg_giveback_pct": _avg_metric(closed, "giveback_pct"),
        "avg_peak_unrealized_pct": _avg_metric(closed, "max_unrealized_pnl_pct"),
        "best_trade": best,
        "worst_trade": worst,
        "top_narrative": narrative_rows[0]["label"] if narrative_rows else (max(narratives, key=narratives.get) if narratives else "None"),
        "top_source": source_rows[0]["label"] if source_rows else (max(source_counts, key=source_counts.get) if source_counts else "None"),
        "top_archetype": archetype_rows[0]["label"] if archetype_rows else (max(archetype_counts, key=archetype_counts.get) if archetype_counts else "None"),
        "top_strategy": strategy_rows[0]["label"] if strategy_rows else (max(strategy_counts, key=strategy_counts.get) if strategy_counts else "None"),
        "top_exit_reason": exit_reason_rows[0]["label"] if exit_reason_rows else "None",
        "best_exit_reason": exit_reason_rows[0]["label"] if exit_reason_rows else "None",
    }
