from __future__ import annotations

from api_server import AutoSellUpdate, _normalize_presets, merge_autosell_update
import db


def test_merge_autosell_update_preserves_existing_nested_fields():
    existing = {
        "enabled": True,
        "mult_targets": [
            {"mult": 2.0, "sell_pct": 50, "triggered": False, "label": "2x"},
        ],
        "stop_loss": {"enabled": False, "pct": 50, "sell_pct": 100, "triggered": False},
        "trailing_stop": {
            "enabled": True,
            "trail_pct": 25,
            "sell_pct": 100,
            "peak_price": 1.23,
            "triggered": False,
            "post_partial_trail_pct": 18,
            "tightened": False,
        },
    }

    update = AutoSellUpdate(
        enabled=False,
        stop_loss={"enabled": True, "pct": 35},
        mult_targets=[
            {"mult": 1.5, "sell_pct": 25},
            {"mult": 3.0, "sell_pct": 50, "label": "3x"},
        ],
    )

    merged = merge_autosell_update(existing, update)

    assert merged["enabled"] is False
    assert merged["stop_loss"]["enabled"] is True
    assert merged["stop_loss"]["pct"] == 35
    assert merged["stop_loss"]["sell_pct"] == 100
    assert merged["trailing_stop"]["trail_pct"] == 25
    assert len(merged["mult_targets"]) == 2
    assert merged["mult_targets"][0]["label"] == "1.5x"
    assert merged["mult_targets"][1]["label"] == "3x"


def test_normalize_presets_filters_invalid_rows():
    normalized = _normalize_presets(
        [
            {"mult": 2, "sell_pct": 50},
            {"multiplier": 3, "pct": 25},
            {"mult": 0, "sell_pct": 10},
            {"mult": 4, "sell_pct": 0},
        ]
    )

    assert normalized == [
        {"mult": 2.0, "sell_pct": 50},
        {"mult": 3.0, "sell_pct": 25},
    ]


def test_auto_buy_activity_summary_counts_categories():
    uid = 991122
    db.record_auto_buy_activity(uid, symbol="AAA", status="blocked", block_category="score", confidence=0.31, sol_amount=0.03)
    db.record_auto_buy_activity(uid, symbol="BBB", status="blocked", block_category="score", confidence=0.42, sol_amount=0.04)
    db.record_auto_buy_activity(uid, symbol="CCC", status="executed", confidence=0.88, sol_amount=0.06)

    summary = db.get_auto_buy_activity_summary(uid, window_hours=24)

    assert summary["total"] >= 3
    assert summary["status_counts"]["blocked"] >= 2
    assert summary["status_counts"]["executed"] >= 1
    assert summary["blocked_by_category"]["score"] >= 2
    assert summary["top_block_category"] == "score"
