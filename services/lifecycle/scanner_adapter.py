from __future__ import annotations

from services.trading import build_trading_snapshot


def snapshot_to_scanner_token(snapshot, *, max_age_hours: int) -> dict | None:
    return build_trading_snapshot(snapshot, max_age_hours=max_age_hours)
