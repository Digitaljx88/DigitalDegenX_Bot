"""
tests/test_autobuy.py — Unit tests for autobuy gate functions.

Run with: pytest tests/test_autobuy.py -v
Each gate is tested independently with mocked db and network calls.
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import MagicMock, patch

# Import the module under test
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from autobuy import (
    gate_enabled,
    gate_score,
    gate_mcap,
    gate_already_bought,
    gate_daily_limit,
    gate_position_limit,
    gate_momentum,
    gate_freshness,
    BuyDecision,
    classify_block_reason,
    evaluate,
    evaluate_lifecycle_snapshot,
)
from services.lifecycle.models import TokenEnrichment, TokenLifecycle, TokenSnapshot, TokenTradeMetrics


# ── gate_enabled ──────────────────────────────────────────────────────────────

class TestGateEnabled:
    def test_passes_when_enabled(self):
        passed, reason = gate_enabled(1, {"enabled": True})
        assert passed is True
        assert reason == ""

    def test_blocks_when_disabled(self):
        passed, reason = gate_enabled(1, {"enabled": False})
        assert passed is False
        assert "not enabled" in reason

    def test_blocks_when_missing(self):
        passed, reason = gate_enabled(1, {})
        assert passed is False


# ── gate_score ────────────────────────────────────────────────────────────────

class TestGateScore:
    USER_CFG = {
        "alert_scouted_threshold": 35,
        "alert_warm_threshold": 55,
        "alert_hot_threshold": 70,
        "alert_ultra_hot_threshold": 85,
    }

    def test_passes_above_min_score(self):
        passed, reason = gate_score(60, {"min_score": 55}, self.USER_CFG)
        assert passed is True

    def test_blocks_below_min_score(self):
        passed, reason = gate_score(40, {"min_score": 55}, self.USER_CFG)
        assert passed is False
        assert "40" in reason

    def test_passes_equal_to_min_score(self):
        passed, reason = gate_score(55, {"min_score": 55}, self.USER_CFG)
        assert passed is True

    def test_tier_warm_uses_threshold(self):
        # buy_tier=warm → min = alert_warm_threshold (55)
        passed, reason = gate_score(50, {"buy_tier": "warm", "min_score": 40}, self.USER_CFG)
        assert passed is False

    def test_tier_hot_uses_threshold(self):
        passed, reason = gate_score(65, {"buy_tier": "hot", "min_score": 40}, self.USER_CFG)
        assert passed is False  # 65 < hot threshold 70

    def test_tier_hot_passes(self):
        passed, reason = gate_score(70, {"buy_tier": "hot"}, self.USER_CFG)
        assert passed is True

    def test_tier_scouted_lowest(self):
        passed, reason = gate_score(35, {"buy_tier": "scouted"}, self.USER_CFG)
        assert passed is True


# ── gate_mcap ─────────────────────────────────────────────────────────────────

class TestGateMcap:
    def test_passes_below_max(self):
        passed, reason = gate_mcap(100_000, {"max_mcap": 500_000})
        assert passed is True

    def test_blocks_above_max(self):
        passed, reason = gate_mcap(600_000, {"max_mcap": 500_000})
        assert passed is False
        assert "600,000" in reason

    def test_passes_zero_mcap(self):
        # mcap unknown — don't block
        passed, reason = gate_mcap(0, {"max_mcap": 500_000})
        assert passed is True

    def test_passes_equal_to_max(self):
        passed, reason = gate_mcap(500_000, {"max_mcap": 500_000})
        assert passed is True


# ── gate_already_bought ───────────────────────────────────────────────────────

class TestGateAlreadyBought:
    @patch("autobuy._db.has_bought", return_value=False)
    def test_passes_not_bought(self, mock_hb):
        passed, reason = gate_already_bought(1, "mint123")
        assert passed is True
        mock_hb.assert_called_once_with(1, "mint123")

    @patch("autobuy._db.has_bought", return_value=True)
    def test_blocks_already_bought(self, mock_hb):
        passed, reason = gate_already_bought(1, "mint123")
        assert passed is False
        assert "already bought" in reason


# ── gate_daily_limit ──────────────────────────────────────────────────────────

class TestGateDailyLimit:
    @patch("autobuy._db.get_spent_today", return_value=0.5)
    def test_passes_within_limit(self, mock_spent):
        passed, reason = gate_daily_limit(1, {"daily_limit_sol": 1.0}, 0.03)
        assert passed is True

    @patch("autobuy._db.get_spent_today", return_value=0.98)
    def test_blocks_over_limit(self, mock_spent):
        passed, reason = gate_daily_limit(1, {"daily_limit_sol": 1.0}, 0.03)
        assert passed is False
        assert "daily limit" in reason

    @patch("autobuy._db.get_spent_today", return_value=0.5)
    def test_passes_zero_limit_means_no_cap(self, mock_spent):
        passed, reason = gate_daily_limit(1, {"daily_limit_sol": 0}, 0.99)
        assert passed is True

    @patch("autobuy._db.get_spent_today", return_value=0.97)
    def test_passes_exactly_at_limit(self, mock_spent):
        # 0.97 + 0.03 == 1.0 — should pass (not strictly greater than)
        passed, reason = gate_daily_limit(1, {"daily_limit_sol": 1.0}, 0.03)
        assert passed is True


# ── gate_position_limit ───────────────────────────────────────────────────────

class TestGatePositionLimit:
    @patch("autobuy._db.get_open_position_count", return_value=3)
    def test_passes_below_max(self, mock_pos):
        passed, reason = gate_position_limit(1, {"max_positions": 5})
        assert passed is True

    @patch("autobuy._db.get_open_position_count", return_value=5)
    def test_blocks_at_max(self, mock_pos):
        passed, reason = gate_position_limit(1, {"max_positions": 5})
        assert passed is False
        assert "5" in reason

    @patch("autobuy._db.get_open_position_count", return_value=100)
    def test_passes_no_limit(self, mock_pos):
        passed, reason = gate_position_limit(1, {"max_positions": 0})
        assert passed is True


# ── gate_momentum ─────────────────────────────────────────────────────────────

class TestGateMomentum:
    def _result(self, price_h1=0, vol_m5=1000, vol_h1=1000):
        return {"price_h1": price_h1, "volume_m5": vol_m5, "volume_h1": vol_h1}

    def test_passes_positive_price(self):
        passed, reason = gate_momentum(self._result(price_h1=10))
        assert passed is True

    def test_passes_mild_dip_with_volume(self):
        # h1_price=-3 (>= -5) — passes on price condition alone
        passed, reason = gate_momentum(self._result(price_h1=-3))
        assert passed is True

    def test_passes_strong_m5_volume(self):
        # price is -10 but m5_pace >= h1*0.3
        result = {"price_h1": -10, "volume_m5": 500, "volume_h1": 1000}
        # m5_pace = 500*12=6000, h1*0.3=300 → passes
        passed, reason = gate_momentum(result)
        assert passed is True

    def test_blocks_dead_momentum(self):
        # price -20%, m5 barely ticking
        result = {"price_h1": -20, "volume_m5": 10, "volume_h1": 10_000}
        # m5_pace = 120, h1*0.3 = 3000 → fails
        passed, reason = gate_momentum(result)
        assert passed is False
        assert "momentum dead" in reason

    def test_blocks_post_peak(self):
        result = {"price_h1": -70, "volume_m5": 0, "volume_h1": 50_000}
        passed, reason = gate_momentum(result)
        assert passed is False


# ── gate_freshness ────────────────────────────────────────────────────────────

class TestGateFreshness:
    def _pair(self, vol_m5=200, price_h1=5, vol_h1=1000):
        return {
            "chainId": "solana",
            "volume": {"m5": vol_m5, "h1": vol_h1},
            "priceChange": {"h1": price_h1},
            "liquidity": {"usd": 10_000},
        }

    @patch("autobuy.requests.get")
    def test_passes_healthy_token(self, mock_get):
        mock_get.return_value.json.return_value = {"pairs": [self._pair()]}
        passed, reason, vol_m5, price_h1 = gate_freshness("mint123")
        assert passed is True
        assert vol_m5 == 200
        assert price_h1 == 5

    @patch("autobuy.requests.get")
    def test_blocks_zero_volume(self, mock_get):
        mock_get.return_value.json.return_value = {"pairs": [self._pair(vol_m5=10)]}
        passed, reason, vol_m5, price_h1 = gate_freshness("mint123")
        assert passed is False
        assert "zero activity" in reason

    @patch("autobuy.requests.get")
    def test_blocks_dead_momentum(self, mock_get):
        # vol_m5=100, vol_h1=100_000 → m5_pace=1200 << h1*0.3=30000; price_h1=-20
        mock_get.return_value.json.return_value = {
            "pairs": [self._pair(vol_m5=100, price_h1=-20, vol_h1=100_000)]
        }
        passed, reason, vol_m5, price_h1 = gate_freshness("mint123")
        assert passed is False
        assert "momentum dead" in reason

    @patch("autobuy.requests.get")
    def test_fails_open_on_no_pairs(self, mock_get):
        mock_get.return_value.json.return_value = {"pairs": []}
        passed, reason, vol_m5, price_h1 = gate_freshness("mint123")
        assert passed is True  # fail open — don't block on missing data

    @patch("autobuy.requests.get", side_effect=Exception("timeout"))
    def test_fails_open_on_network_error(self, mock_get):
        passed, reason, vol_m5, price_h1 = gate_freshness("mint123")
        assert passed is True  # fail open


# ── classify_block_reason ─────────────────────────────────────────────────────

class TestClassifyBlockReason:
    def test_classifies_score_blocks(self):
        assert classify_block_reason("score 52 < min 70") == "score"

    def test_classifies_freshness_blocks(self):
        assert classify_block_reason("fresh data: buy ratio fading (48%)") == "freshness"

    def test_classifies_daily_cap_blocks(self):
        assert classify_block_reason("daily limit reached") == "daily_cap"

    def test_classifies_exposure_blocks(self):
        assert classify_block_reason("narrative exposure cap reached for AI") == "exposure_cap"

    def test_classifies_quality_gate_blocks(self):
        assert classify_block_reason("entry quality blocked — buy ratio fading") == "quality_gate"


# ── evaluate() integration ────────────────────────────────────────────────────

class TestEvaluate:
    """Integration tests for evaluate() — mocks all db calls and HTTP."""

    RESULT = {
        "mint": "TestMint1111",
        "symbol": "TEST",
        "name": "Test Token",
        "total": 65,
        "mcap": 100_000,
        "price_h1": 5,
        "volume_m5": 500,
        "volume_h1": 1000,
        "price_usd": 0.00001,
    }

    def _mock_db(self, enabled=True, score=55, mcap=500_000, bought=False,
                 spent=0.0, positions=0):
        cfg = {
            "enabled": enabled,
            "min_score": score,
            "max_mcap": mcap,
            "sol_amount": 0.03,
            "max_sol_amount": 0.09,
            "min_confidence": 0.35,
            "confidence_scale_enabled": True,
            "daily_limit_sol": 1.0,
            "max_positions": 5,
            "max_narrative_exposure": 2,
            "max_archetype_exposure": 0,
            "buy_tier": "",
        }
        patches = {
            "autobuy._db.reset_day_if_needed": MagicMock(),
            "autobuy._db.get_auto_buy_config": MagicMock(return_value=cfg),
            "autobuy._db.has_bought": MagicMock(return_value=bought),
            "autobuy._db.get_spent_today": MagicMock(return_value=spent),
            "autobuy._db.get_open_position_count": MagicMock(return_value=positions),
            "autobuy._db.get_open_position_exposure": MagicMock(return_value={"narrative": {}, "archetype": {}}),
        }
        return patches

    @pytest.mark.asyncio
    async def test_all_gates_pass(self):
        patches = self._mock_db()
        fresh_response = {"pairs": [{
            "chainId": "solana",
            "volume": {"m5": 500, "h1": 1000},
            "priceChange": {"h1": 5},
            "liquidity": {"usd": 10_000},
        }]}
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=patches["autobuy._db.get_auto_buy_config"].return_value),
            patch("autobuy._db.has_bought", return_value=False),
            patch("autobuy._db.get_spent_today", return_value=0.0),
            patch("autobuy._db.get_open_position_count", return_value=0),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.requests.get") as mock_get,
            patch("autobuy.settings_manager", create=True) as mock_sm,
        ):
            mock_get.return_value.json.return_value = fresh_response
            mock_sm.get_user_settings.return_value = {}
            # Patch bot.get_mode
            with patch("autobuy.bot", create=True) as mock_bot:
                mock_bot.get_mode.return_value = "paper"
                decision = await evaluate(1, self.RESULT)

        assert decision.gate_passed is True
        assert decision.symbol == "TEST"
        assert decision.mode == "paper"

    @pytest.mark.asyncio
    async def test_blocks_when_disabled(self):
        cfg = {"enabled": False, "sol_amount": 0.03}
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=cfg),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.settings_manager", create=True),
        ):
            decision = await evaluate(1, self.RESULT)

        assert decision.gate_passed is False
        assert "not enabled" in decision.block_reason

    @pytest.mark.asyncio
    async def test_blocks_when_score_too_low(self):
        cfg = {"enabled": True, "min_score": 80, "sol_amount": 0.03}
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=cfg),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.settings_manager", create=True) as mock_sm,
        ):
            mock_sm.get_user_settings.return_value = {}
            decision = await evaluate(1, self.RESULT)  # score=65 < min=80

        assert decision.gate_passed is False
        assert "score" in decision.block_reason

    @pytest.mark.asyncio
    async def test_blocks_when_already_bought(self):
        cfg = {
            "enabled": True, "min_score": 55, "max_mcap": 500_000,
            "sol_amount": 0.03, "daily_limit_sol": 1.0,
            "max_positions": 5, "buy_tier": "",
        }
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=cfg),
            patch("autobuy._db.has_bought", return_value=True),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.settings_manager", create=True) as mock_sm,
        ):
            mock_sm.get_user_settings.return_value = {}
            decision = await evaluate(1, self.RESULT)

        assert decision.gate_passed is False
        assert "already bought" in decision.block_reason

    @pytest.mark.asyncio
    async def test_scales_sol_amount_for_high_confidence_setup(self):
        cfg = {
            "enabled": True,
            "min_score": 55,
            "max_mcap": 500_000,
            "sol_amount": 0.03,
            "max_sol_amount": 0.09,
            "min_confidence": 0.35,
            "confidence_scale_enabled": True,
            "daily_limit_sol": 1.0,
            "max_positions": 5,
            "max_narrative_exposure": 2,
            "max_archetype_exposure": 0,
            "buy_tier": "",
        }
        result = {
            **self.RESULT,
            "effective_score": 92,
            "_source_name": "pumpfun_newest",
            "_source_rank": 100,
            "age_mins": 4,
            "wallet_signal": 7,
            "liquidity_to_mcap_ratio": 0.12,
            "txns_per_10k_liq": 12,
            "buy_ratio_5m": 0.80,
            "score_slope": 4.5,
            "score_drop_from_peak": 0,
            "liquidity_drop_pct": 0,
            "holder_concentration_delta": 0,
            "narrative_cluster_count": 0,
            "matched_narrative": "AI",
            "archetype": "launch_snipe",
            "archetype_conf": 90,
        }
        fresh_response = {"pairs": [{
            "chainId": "solana",
            "volume": {"m5": 500, "h1": 1000},
            "priceChange": {"h1": 5},
            "liquidity": {"usd": 10_000},
        }]}
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=cfg),
            patch("autobuy._db.has_bought", return_value=False),
            patch("autobuy._db.get_spent_today", return_value=0.0),
            patch("autobuy._db.get_open_position_count", return_value=0),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.requests.get") as mock_get,
            patch("autobuy.settings_manager", create=True) as mock_sm,
        ):
            mock_get.return_value.json.return_value = fresh_response
            mock_sm.get_user_settings.return_value = {}
            decision = await evaluate(1, result)

        assert decision.gate_passed is True
        assert decision.sol_amount == 0.06
        assert decision.confidence >= 0.85

    @pytest.mark.asyncio
    async def test_evaluate_lifecycle_snapshot_uses_normalized_pipeline(self):
        cfg = {
            "enabled": True,
            "min_score": 35,
            "max_mcap": 500_000,
            "sol_amount": 0.03,
            "max_sol_amount": 0.09,
            "min_confidence": 0.2,
            "confidence_scale_enabled": True,
            "daily_limit_sol": 1.0,
            "max_positions": 5,
            "max_narrative_exposure": 2,
            "max_archetype_exposure": 0,
            "buy_tier": "",
        }
        snapshot = TokenSnapshot(
            mint="LifecycleMint111",
            lifecycle=TokenLifecycle(
                mint="LifecycleMint111",
                symbol="LIFE",
                name="Lifecycle Token",
                state="pump_active",
                launch_ts=time.time(),
                source_primary="pumpfun_newest",
                source_rank=100,
            ),
            metrics=TokenTradeMetrics(
                mint="LifecycleMint111",
                buys_5m=9,
                sells_5m=2,
                volume_usd_5m=2_000,
                liquidity_usd=15_000,
            ),
            enrichment=TokenEnrichment(
                mint="LifecycleMint111",
                dex={
                    "marketCap": 45_000,
                    "pairCreatedAt": int(time.time() * 1000),
                    "priceUsd": 0.00012,
                    "volume": {"m5": 2_000, "h1": 6_000},
                    "txns": {"m5": {"buys": 9, "sells": 2}},
                    "priceChange": {"h1": 6, "h24": 6},
                    "liquidity": {"usd": 15_000},
                    "dexId": "pumpfun",
                },
                rugcheck={},
                pump={"description": "fresh launch"},
                wallet={},
            ),
            events=[],
        )
        with (
            patch("autobuy._db.reset_day_if_needed"),
            patch("autobuy._db.get_auto_buy_config", return_value=cfg),
            patch("autobuy._db.has_bought", return_value=False),
            patch("autobuy._db.get_spent_today", return_value=0.0),
            patch("autobuy._db.get_open_position_count", return_value=0),
            patch("autobuy._db.get_open_position_exposure", return_value={"narrative": {}, "archetype": {}}),
            patch("autobuy.requests.get") as mock_get,
            patch("autobuy.settings_manager", create=True) as mock_sm,
        ):
            mock_get.return_value.json.return_value = {
                "pairs": [{
                    "chainId": "solana",
                    "volume": {"m5": 2_000, "h1": 6_000},
                    "priceChange": {"h1": 6},
                    "txns": {"m5": {"buys": 9, "sells": 2}},
                    "liquidity": {"usd": 15_000},
                }]
            }
            mock_sm.get_user_settings.return_value = {}
            decision = await evaluate_lifecycle_snapshot(1, snapshot)

        assert decision.gate_passed is True
        assert decision.symbol == "LIFE"
        assert decision.mcap == 45_000
