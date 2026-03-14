from __future__ import annotations

import time


def _ts_to_ms(ts: float | int | None) -> int:
    if not ts:
        return 0
    ts = float(ts)
    return int(ts if ts > 1e12 else ts * 1000)


def _first_non_empty(*values):
    for value in values:
        if value not in (None, "", 0, 0.0, False, [], {}):
            return value
    return None


def build_trading_snapshot(snapshot, *, max_age_hours: int | None = None) -> dict | None:
    """
    Normalize a lifecycle snapshot into the token shape used by scoring, scanner
    alerts, and auto-buy decisions.
    """
    lifecycle = snapshot.lifecycle
    metrics = snapshot.metrics
    enrichment = snapshot.enrichment
    dex = enrichment.dex or {}
    pump = enrichment.pump or {}
    wallet = enrichment.wallet or {}

    pair_created = _first_non_empty(
        dex.get("pairCreatedAt"),
        dex.get("pair_created"),
        pump.get("created_timestamp"),
        lifecycle.launch_ts,
    )
    pair_created_ms = _ts_to_ms(pair_created)
    if max_age_hours is not None:
        cutoff_ms = (time.time() - max_age_hours * 3600) * 1000
        if pair_created_ms and pair_created_ms < cutoff_ms:
            return None

    name = _first_non_empty(
        lifecycle.name,
        pump.get("name"),
        ((dex.get("baseToken") or {}) or {}).get("name"),
        dex.get("name"),
    )
    symbol = _first_non_empty(
        lifecycle.symbol,
        pump.get("symbol"),
        ((dex.get("baseToken") or {}) or {}).get("symbol"),
        dex.get("symbol"),
    )
    if not name and not symbol:
        return None

    volume = dex.get("volume") or {}
    txns = dex.get("txns") or {}
    txns_m5 = txns.get("m5") or {}
    price_change = dex.get("priceChange") or {}
    liquidity = dex.get("liquidity") or {}

    tx_buys = int(_first_non_empty(
        txns_m5.get("buys") if isinstance(txns_m5, dict) else None,
        metrics.buys_5m,
    ) or 0)
    tx_sells = int(_first_non_empty(
        txns_m5.get("sells") if isinstance(txns_m5, dict) else None,
        metrics.sells_5m,
    ) or 0)
    total_txns = tx_buys + tx_sells
    buy_ratio_5m = float(_first_non_empty(
        metrics.buy_ratio_5m,
        (tx_buys / total_txns) if total_txns else 0.0,
    ) or 0.0)

    pump_mcap = _first_non_empty(
        pump.get("usd_market_cap"),
        pump.get("market_cap"),
        pump.get("marketCap"),
        pump.get("mcap"),
    )
    if not pump_mcap:
        pump_mcap_sol = _first_non_empty(
            pump.get("marketCapSol"),
            pump.get("market_cap_sol"),
        )
        pump_sol_usd = _first_non_empty(
            pump.get("sol_price_usd"),
            dex.get("sol_price_usd"),
        )
        if pump_mcap_sol and pump_sol_usd:
            try:
                pump_mcap = float(pump_mcap_sol) * float(pump_sol_usd)
            except Exception:
                pump_mcap = None

    mcap = float(_first_non_empty(
        dex.get("marketCap"),
        dex.get("fdv"),
        dex.get("mcap"),
        dex.get("usd_market_cap"),
        pump_mcap,
    ) or 0)
    price_usd = float(_first_non_empty(
        dex.get("priceUsd"),
        dex.get("price_usd"),
        pump.get("priceUsd"),
        pump.get("price_usd"),
    ) or 0)
    liquidity_usd = float(_first_non_empty(
        liquidity.get("usd") if isinstance(liquidity, dict) else None,
        dex.get("liquidity_usd"),
        metrics.liquidity_usd,
        pump.get("liquidity"),
    ) or 0)

    dex_id = _first_non_empty(
        dex.get("dexId"),
        dex.get("dex"),
        "raydium" if lifecycle.state in {"raydium_live", "dex_indexed"} else None,
        "pumpfun" if lifecycle.state in {"launched", "pump_active", "migration_pending"} else None,
    ) or ""

    launch_ts = lifecycle.launch_ts or (pair_created_ms / 1000.0 if pair_created_ms else None)
    age_mins = ((time.time() - float(launch_ts)) / 60.0) if launch_ts else 0.0

    token = {
        "mint": lifecycle.mint,
        "name": name or symbol or lifecycle.mint[:8],
        "symbol": symbol or (name[:10] if name else lifecycle.mint[:8]),
        "mcap": mcap,
        "price_usd": price_usd,
        "volume_h1": float(_first_non_empty(
            volume.get("h1") if isinstance(volume, dict) else None,
            dex.get("volume_h1"),
            pump.get("volume_h1"),
            pump.get("volume_1h_usd"),
        ) or 0),
        "volume_h6": float(_first_non_empty(
            volume.get("h6") if isinstance(volume, dict) else None,
            dex.get("volume_h6"),
            pump.get("volume_h6"),
        ) or 0),
        "volume_h24": float(_first_non_empty(
            volume.get("h24") if isinstance(volume, dict) else None,
            dex.get("volume_h24"),
            pump.get("volume_h24"),
            pump.get("volume_24h_usd"),
        ) or 0),
        "volume_m5": float(_first_non_empty(
            volume.get("m5") if isinstance(volume, dict) else None,
            dex.get("volume_m5"),
            pump.get("volume_m5"),
            pump.get("volume_5m_usd"),
            metrics.volume_usd_5m,
        ) or 0),
        "txns_m5_buys": tx_buys,
        "txns_m5_sells": tx_sells,
        "buy_ratio_5m": buy_ratio_5m,
        "price_h24": float(_first_non_empty(
            price_change.get("h24") if isinstance(price_change, dict) else None,
            dex.get("price_h24"),
        ) or 0),
        "price_h1": float(_first_non_empty(
            price_change.get("h1") if isinstance(price_change, dict) else None,
            dex.get("price_h1"),
            pump.get("price_h1"),
        ) or 0),
        "liquidity": liquidity_usd,
        "dex": dex_id,
        "pair_address": _first_non_empty(
            dex.get("pairAddress"),
            dex.get("pair_address"),
            lifecycle.dex_pair,
            lifecycle.raydium_pool,
        ) or "",
        "pair_created": pair_created_ms,
        "launch_ts": launch_ts,
        "age_mins": age_mins,
        "description": _first_non_empty(
            pump.get("description"),
            dex.get("description"),
        ) or "",
        "twitter_url": _first_non_empty(
            pump.get("twitter"),
            dex.get("twitter"),
            dex.get("twitter_url"),
        ),
        "_source_name": lifecycle.source_primary or "lifecycle",
        "_source_rank": lifecycle.source_rank or 0,
        "source_primary": lifecycle.source_primary or "lifecycle",
        "source_rank": lifecycle.source_rank or 0,
        "lifecycle_state": lifecycle.state,
        "wallet_signal": float(_first_non_empty(
            wallet.get("wallet_signal"),
            wallet.get("wallet_boost"),
            lifecycle.last_confidence,
        ) or 0),
        "lifecycle_narrative": lifecycle.narrative or "",
        "lifecycle_archetype": lifecycle.archetype or "",
        "lifecycle_strategy_profile": lifecycle.strategy_profile or "",
        "strategy_profile": lifecycle.strategy_profile or "",
        "snapshot_score_raw": float(lifecycle.last_score or 0.0),
        "snapshot_score_effective": float(lifecycle.last_effective_score or 0.0),
        "snapshot_confidence": float(lifecycle.last_confidence or 0.0),
        "unique_buyers_5m": int(metrics.unique_buyers_5m or 0),
        "holder_concentration": float(metrics.holder_concentration or 0.0),
        "dev_activity_score": float(metrics.dev_activity_score or 0.0),
        "liquidity_delta_pct": float(metrics.liquidity_delta_pct or 0.0),
        "bonding_curve_fill_pct": float(metrics.bonding_curve_fill_pct or 0.0),
        "score_slope": float(metrics.score_slope or 0.0),
        "score_acceleration": float(metrics.score_acceleration or 0.0),
        "peak_score": float(metrics.peak_score or 0.0),
        "time_since_peak_s": float(metrics.time_since_peak_s or 0.0),
        "raydium_pool": lifecycle.raydium_pool or "",
        "dev_wallet": lifecycle.dev_wallet or "",
        "snapshot_events": snapshot.events,
    }
    return token
