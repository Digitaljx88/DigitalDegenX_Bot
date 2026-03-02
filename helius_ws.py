"""
Helius WebSocket watcher — real-time pump.fun launches + Raydium migrations.

Connects to Helius WSS, subscribes to on-chain program log events, then posts
to the configured feed channels (via feed.py) seconds after the on-chain event.
Dedup is handled by feed.py (86400s TTL), so double-posts are impossible even
when the polling scanner also catches the same token.
"""

import asyncio
import json
import logging
import time
import requests
import websockets

logger = logging.getLogger(__name__)

# ── Program addresses ─────────────────────────────────────────────────────────

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_AMM_V4  = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM    = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1D"
SOL_MINT        = "So11111111111111111111111111111111111111112"
USDC_MINT       = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Quote mints — filter these out when looking for the base (meme) token
QUOTE_MINTS = {SOL_MINT, USDC_MINT}

DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"


# ── Main class ────────────────────────────────────────────────────────────────

class HeliusWatcher:
    """
    Persistent WebSocket connection to Helius.

    - Subscribes to pump.fun program logs  → detects new token launches
    - Subscribes to Raydium AMM v4 + CPMM → detects migrations (pool creation)
    - Calls feed.maybe_post_launch / feed.maybe_post_migration on matches
    - Auto-reconnects on disconnect
    """

    def __init__(self, api_key: str, bot, feed_module):
        self.api_key = api_key
        self.wss_url = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.bot     = bot
        self.fd      = feed_module
        self._running = False
        self._task    = None
        self._req_id  = 0
        # Session-level dedup (feed.py handles 24h cross-session dedup)
        self._seen_sigs:  set[str] = set()
        self._seen_mints: set[str] = set()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _fetch_tx(self, sig: str) -> dict | None:
        """Synchronous RPC call — run via executor."""
        try:
            r = requests.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getTransaction",
                "params":  [sig, {"encoding": "json",
                                  "maxSupportedTransactionVersion": 0}],
            }, timeout=12)
            return r.json().get("result")
        except Exception:
            return None

    def _dex_lookup(self, mint: str, prefer_dex: str = "") -> dict | None:
        """Return best Solana pair for mint from DexScreener."""
        try:
            r = requests.get(DEXSCREENER_TOKEN + mint, timeout=8)
            pairs = r.json().get("pairs") or []
            sol = [p for p in pairs if p.get("chainId") == "solana"]
            if prefer_dex:
                filtered = [p for p in sol if prefer_dex in (p.get("dexId") or "").lower()]
                if filtered:
                    sol = filtered
            if not sol:
                return None
            sol.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0)), reverse=True)
            return sol[0]
        except Exception:
            return None

    @staticmethod
    def _pair_to_token(pair: dict, mint: str) -> dict:
        return {
            "mint":              mint,
            "name":              pair.get("baseToken", {}).get("name", ""),
            "symbol":            pair.get("baseToken", {}).get("symbol", ""),
            "mcap":              float(pair.get("marketCap") or pair.get("fdv") or 0),
            "price_usd":         float(pair.get("priceUsd") or 0),
            "volume_h1":         float((pair.get("volume") or {}).get("h1", 0)),
            "price_h1":          float((pair.get("priceChange") or {}).get("h1", 0)),
            "liquidity":         float((pair.get("liquidity") or {}).get("usd", 0)),
            "dex":               pair.get("dexId", ""),
            "pair_created":      pair.get("pairCreatedAt", 0),
            "total_holders":     0,
            "matched_narrative": "",
        }

    # ── Mint extraction ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_launch_mint(tx: dict) -> str:
        """
        Extract the new token mint from a pump.fun Create transaction.
        Primary: postTokenBalances (most reliable — bonding curve receives supply)
        Fallback: accountKeys[0] (pump.fun IDL: account[0] = mint)
        """
        balances = tx.get("meta", {}).get("postTokenBalances", [])
        for b in balances:
            m = b.get("mint", "")
            if m and m not in QUOTE_MINTS:
                return m

        keys = (tx.get("transaction", {})
                   .get("message", {})
                   .get("accountKeys", []))
        if keys:
            k0 = keys[0] if isinstance(keys[0], str) else keys[0].get("pubkey", "")
            if k0 and k0 not in QUOTE_MINTS:
                return k0
        return ""

    @staticmethod
    def _extract_migration_mint(tx: dict) -> str:
        """
        Extract the base (meme) token mint from a Raydium pool-init transaction.
        Uses postTokenBalances and filters out SOL/USDC quote mints.
        """
        balances = tx.get("meta", {}).get("postTokenBalances", [])
        for b in balances:
            m = b.get("mint", "")
            if m and m not in QUOTE_MINTS:
                return m
        return ""

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _handle_launch(self, sig: str, logs: list):
        """pump.fun Create → post to launch feed."""
        if sig in self._seen_sigs:
            return
        # Only process new token creations
        if not any("Instruction: Create" in l for l in logs):
            return
        self._seen_sigs.add(sig)

        loop = asyncio.get_running_loop()

        # Give RPC a moment to index the tx
        await asyncio.sleep(3)
        tx = await loop.run_in_executor(None, self._fetch_tx, sig)
        if not tx:
            return

        mint = self._extract_launch_mint(tx)
        if not mint or mint in self._seen_mints:
            return

        cfg = self.fd.load_feed_config()
        if not cfg.get("launch_enabled") or not cfg.get("launch_channel"):
            return

        # Wait for DexScreener to index the new pair
        await asyncio.sleep(15)
        pair = await loop.run_in_executor(None, self._dex_lookup, mint, "")
        if not pair:
            await asyncio.sleep(20)
            pair = await loop.run_in_executor(None, self._dex_lookup, mint, "")
        if not pair:
            return

        self._seen_mints.add(mint)
        token = self._pair_to_token(pair, mint)
        token["total_holders"]     = 0
        token["matched_narrative"] = ""
        await self.fd.maybe_post_launch(self.bot, token, 0, "⚡ LIVE")
        logger.info(f"[Helius] Launch: {token.get('name')} ({mint[:8]}…)")

    async def _handle_migration(self, sig: str, logs: list):
        """Raydium pool init → post to migration feed."""
        if sig in self._seen_sigs:
            return
        log_str = " ".join(logs).lower()
        # Filter to pool-creation instructions only
        if not any(kw in log_str for kw in ["initialize2", "initialize", "createpool"]):
            return
        self._seen_sigs.add(sig)

        loop = asyncio.get_running_loop()

        await asyncio.sleep(3)
        tx = await loop.run_in_executor(None, self._fetch_tx, sig)
        if not tx:
            return

        mint = self._extract_migration_mint(tx)
        if not mint or mint in self._seen_mints:
            return

        cfg = self.fd.load_feed_config()
        if not cfg.get("migrate_enabled") or not cfg.get("migrate_channel"):
            return

        # Brief wait for DexScreener to list the new Raydium pair
        await asyncio.sleep(10)
        pair = await loop.run_in_executor(None, self._dex_lookup, mint, "raydium")
        if not pair:
            await asyncio.sleep(20)
            pair = await loop.run_in_executor(None, self._dex_lookup, mint, "raydium")
        if not pair:
            return

        self._seen_mints.add(mint)
        token = self._pair_to_token(pair, mint)
        await self.fd.maybe_post_migration(self.bot, token)
        logger.info(f"[Helius] Migration: {token.get('name')} ({mint[:8]}…)")

    # ── Session-level dedup pruning ───────────────────────────────────────────

    def _prune_seen(self):
        if len(self._seen_sigs) > 10_000:
            self._seen_sigs  = set(list(self._seen_sigs)[-3_000:])
        if len(self._seen_mints) > 5_000:
            self._seen_mints = set(list(self._seen_mints)[-1_000:])

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self):
        while self._running:
            sub_map: dict[int, str] = {}   # subscription_id → "launch"|"migrate"
            try:
                async with websockets.connect(
                    self.wss_url,
                    ping_interval=20,
                    ping_timeout=40,
                    close_timeout=10,
                    max_size=2**23,   # 8 MB — Raydium txs can be large
                ) as ws:
                    logger.info("[Helius] WebSocket connected")

                    # Subscribe: pump.fun launches
                    launch_req = self._next_id()
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": launch_req,
                        "method":  "logsSubscribe",
                        "params":  [
                            {"mentions": [PUMPFUN_PROGRAM]},
                            {"commitment": "confirmed"},
                        ],
                    }))

                    # Subscribe: Raydium AMM v4 migrations
                    ray_req = self._next_id()
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": ray_req,
                        "method":  "logsSubscribe",
                        "params":  [
                            {"mentions": [RAYDIUM_AMM_V4]},
                            {"commitment": "confirmed"},
                        ],
                    }))

                    # Subscribe: Raydium CPMM migrations (newer pump.fun graduates)
                    cpmm_req = self._next_id()
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": cpmm_req,
                        "method":  "logsSubscribe",
                        "params":  [
                            {"mentions": [RAYDIUM_CPMM]},
                            {"commitment": "confirmed"},
                        ],
                    }))

                    async for raw in ws:
                        if not self._running:
                            break

                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        # Capture subscription IDs from subscription acks
                        if "result" in msg and isinstance(msg.get("result"), int):
                            rid = msg.get("id")
                            if rid == launch_req:
                                sub_map[msg["result"]] = "launch"
                                logger.info(f"[Helius] pump.fun sub #{msg['result']} OK")
                            elif rid in (ray_req, cpmm_req):
                                sub_map[msg["result"]] = "migrate"
                                logger.info(f"[Helius] Raydium sub #{msg['result']} OK")
                            continue

                        if msg.get("method") != "logsNotification":
                            continue

                        params = msg.get("params", {})
                        sub_id = params.get("subscription")
                        value  = (params.get("result") or {}).get("value", {})
                        sig    = value.get("signature", "")
                        logs   = value.get("logs") or []
                        err    = value.get("err")

                        if err or not sig or not logs:
                            continue

                        self._prune_seen()
                        etype = sub_map.get(sub_id)
                        if etype == "launch":
                            asyncio.create_task(self._handle_launch(sig, logs))
                        elif etype == "migrate":
                            asyncio.create_task(self._handle_migration(sig, logs))

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"[Helius] WS closed ({e}). Reconnecting in 5s…")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[Helius] WS error: {e}. Reconnecting in 10s…")
                await asyncio.sleep(10)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> asyncio.Task:
        """Start the watcher as a background asyncio task."""
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("[Helius] Watcher started")
        return self._task

    def stop(self):
        """Stop the watcher."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[Helius] Watcher stopped")
