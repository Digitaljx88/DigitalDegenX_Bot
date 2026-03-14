from __future__ import annotations

import time

import db

from .models import TokenEnrichment, TokenLifecycle, TokenSnapshot, TokenTradeMetrics


def _to_snapshot(raw: dict | None) -> TokenSnapshot | None:
    if not raw:
        return None
    lifecycle = TokenLifecycle(**raw.get("lifecycle", {}))
    metrics = TokenTradeMetrics(mint=raw["mint"], **{k: v for k, v in raw.get("metrics", {}).items() if k != "mint"})
    enrichment = TokenEnrichment(mint=raw["mint"], **{k: v for k, v in raw.get("enrichment", {}).items() if k != "mint"})
    return TokenSnapshot(
        mint=raw["mint"],
        lifecycle=lifecycle,
        metrics=metrics,
        enrichment=enrichment,
        events=raw.get("events", []),
    )


def record_launch_event(
    mint: str,
    *,
    symbol: str | None = None,
    name: str | None = None,
    launch_ts: float | None = None,
    dev_wallet: str | None = None,
    source_primary: str = "pump_launch",
    source_rank: int = 100,
    payload: dict | None = None,
) -> TokenSnapshot:
    ts = launch_ts or time.time()
    db.upsert_token_lifecycle(
        mint,
        symbol=symbol,
        name=name,
        state="launched",
        launch_ts=ts,
        dev_wallet=dev_wallet,
        source_primary=source_primary,
        source_rank=source_rank,
        last_updated_ts=ts,
    )
    db.append_token_event(mint, "launch", payload or {}, ts=ts)
    return get_token_snapshot(mint)


def update_lifecycle_fields(mint: str, **fields) -> TokenSnapshot:
    db.upsert_token_lifecycle(mint, **fields)
    return get_token_snapshot(mint)


def record_swap_metrics(mint: str, **metrics) -> TokenSnapshot:
    normalized_metrics = dict(metrics)
    now = normalized_metrics.pop("updated_ts", None) or time.time()
    db.upsert_token_trade_metrics(mint, **normalized_metrics, updated_ts=now)
    db.upsert_token_lifecycle(mint, state="pump_active", last_trade_ts=now, last_updated_ts=now)
    if metrics.get("payload"):
        db.append_token_event(mint, "swap", metrics["payload"], ts=now)
    return get_token_snapshot(mint)


def record_migration_detected(mint: str, *, migration_ts: float | None = None, payload: dict | None = None) -> TokenSnapshot:
    ts = migration_ts or time.time()
    db.upsert_token_lifecycle(mint, state="migration_pending", migration_ts=ts, last_updated_ts=ts)
    db.append_token_event(mint, "migration_detected", payload or {}, ts=ts)
    return get_token_snapshot(mint)


def attach_raydium_pool(mint: str, raydium_pool: str, *, payload: dict | None = None, ts: float | None = None) -> TokenSnapshot:
    event_ts = ts or time.time()
    db.upsert_token_lifecycle(
        mint,
        state="raydium_live",
        raydium_pool=raydium_pool,
        migration_ts=event_ts,
        last_updated_ts=event_ts,
    )
    db.append_token_event(mint, "raydium_pool", payload or {"raydium_pool": raydium_pool}, ts=event_ts)
    return get_token_snapshot(mint)


def attach_dex_pair(mint: str, dex_pair: str, *, payload: dict | None = None, ts: float | None = None) -> TokenSnapshot:
    event_ts = ts or time.time()
    db.upsert_token_lifecycle(mint, state="dex_indexed", dex_pair=dex_pair, last_updated_ts=event_ts)
    db.append_token_event(mint, "dex_seen", payload or {"dex_pair": dex_pair}, ts=event_ts)
    return get_token_snapshot(mint)


def update_score_state(
    mint: str,
    *,
    narrative: str | None = None,
    archetype: str | None = None,
    strategy_profile: str | None = None,
    last_score: float | None = None,
    last_effective_score: float | None = None,
    last_confidence: float | None = None,
    metrics: dict | None = None,
    payload: dict | None = None,
) -> TokenSnapshot:
    now = time.time()
    db.upsert_token_lifecycle(
        mint,
        narrative=narrative,
        archetype=archetype,
        strategy_profile=strategy_profile,
        last_score=last_score,
        last_effective_score=last_effective_score,
        last_confidence=last_confidence,
        last_updated_ts=now,
    )
    if metrics:
        db.upsert_token_trade_metrics(mint, **metrics, updated_ts=now)
    db.append_token_event(
        mint,
        "score_update",
        payload
        or {
            "narrative": narrative,
            "archetype": archetype,
            "strategy_profile": strategy_profile,
            "last_score": last_score,
            "last_effective_score": last_effective_score,
            "last_confidence": last_confidence,
        },
        ts=now,
    )
    return get_token_snapshot(mint)


def upsert_enrichment(mint: str, *, rugcheck=None, dex=None, pump=None, wallet=None, updated_ts: float | None = None) -> TokenSnapshot:
    db.upsert_token_enrichment(
        mint,
        rugcheck=rugcheck or {},
        dex=dex or {},
        pump=pump or {},
        wallet=wallet or {},
        updated_ts=updated_ts or time.time(),
    )
    return get_token_snapshot(mint)


def get_token_snapshot(mint: str) -> TokenSnapshot | None:
    return _to_snapshot(db.get_token_snapshot(mint))


def get_token_timeline(mint: str, limit: int = 100) -> list[dict]:
    return db.get_token_events(mint, limit=limit)


def list_recent_snapshots(limit: int = 50, states: list[str] | None = None) -> list[TokenSnapshot]:
    return [snapshot for raw in db.list_token_snapshots(limit=limit, states=states) if (snapshot := _to_snapshot(raw))]
