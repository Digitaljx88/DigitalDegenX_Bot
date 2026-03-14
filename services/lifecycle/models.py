from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TokenLifecycle:
    mint: str
    symbol: str | None = None
    name: str | None = None
    state: str = "launched"
    launch_ts: float | None = None
    last_trade_ts: float | None = None
    migration_ts: float | None = None
    raydium_pool: str | None = None
    dex_pair: str | None = None
    dev_wallet: str | None = None
    source_primary: str | None = None
    source_rank: int | None = None
    narrative: str | None = None
    archetype: str | None = None
    strategy_profile: str | None = None
    last_score: float | None = None
    last_effective_score: float | None = None
    last_confidence: float | None = None
    last_updated_ts: float = 0.0


@dataclass
class TokenTradeMetrics:
    mint: str
    buys_1m: int = 0
    sells_1m: int = 0
    buys_5m: int = 0
    sells_5m: int = 0
    volume_usd_1m: float = 0.0
    volume_usd_5m: float = 0.0
    buy_ratio_5m: float = 0.0
    unique_buyers_5m: int = 0
    holder_concentration: float = 0.0
    dev_activity_score: float = 0.0
    liquidity_usd: float = 0.0
    liquidity_delta_pct: float = 0.0
    bonding_curve_fill_pct: float = 0.0
    score_slope: float = 0.0
    score_acceleration: float = 0.0
    peak_score: float = 0.0
    time_since_peak_s: float = 0.0
    updated_ts: float = 0.0


@dataclass
class TokenEnrichment:
    mint: str
    rugcheck: dict[str, Any] = field(default_factory=dict)
    dex: dict[str, Any] = field(default_factory=dict)
    pump: dict[str, Any] = field(default_factory=dict)
    wallet: dict[str, Any] = field(default_factory=dict)
    updated_ts: float | None = None


@dataclass
class TokenSnapshot:
    mint: str
    lifecycle: TokenLifecycle
    metrics: TokenTradeMetrics
    enrichment: TokenEnrichment
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "lifecycle": asdict(self.lifecycle),
            "metrics": asdict(self.metrics),
            "enrichment": asdict(self.enrichment),
            "events": self.events,
        }
