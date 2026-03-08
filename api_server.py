"""
DigitalDegenX Bot — AI Control API
====================================
A FastAPI server that runs alongside bot.py and exposes authenticated REST
endpoints any AI model/agent can call to read state and drive trades.

Start standalone:
    uvicorn api_server:app --host 0.0.0.0 --port 8080

Or launched automatically from bot.py (see integration below).

Authentication:
    Pass your API key in every request header:
        X-API-Key: <your key from config.py API_KEY>

Available endpoints:
    GET  /status              — bot health + mode
    GET  /portfolio           — paper portfolio balances
    GET  /wallet              — live wallet SOL + token balances
    GET  /scanner/top         — top scanner alerts from today
    GET  /scanner/watchlist   — current watchlist (scored tokens)
    GET  /price/{mint}        — token price + mcap from DexScreener
    GET  /token/{mint}/safety — RugCheck safety summary
    POST /buy                 — paper or live buy
    POST /sell                — paper or live sell
    POST /alert/set           — set a price alert
    POST /scanner/threshold   — update watch/alert score thresholds
    GET  /trades              — recent trade history
    GET  /autosell/{mint}     — auto-sell config for a token
    POST /autosell/{mint}     — update auto-sell config
    POST /message             — send a Telegram message to a user
"""

import os
import time
import asyncio
import secrets
from typing import Optional

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.dirname(__file__)

def _load_api_key() -> str:
    try:
        import config as _cfg
        key = getattr(_cfg, "API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    # Fallback: auto-generate
    key = secrets.token_hex(32)
    print(f"[API] No API_KEY in config.py — using temporary key: {key}", flush=True)
    return key

API_KEY    = _load_api_key()
API_PORT   = 8080

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DigitalDegenX AI Control API",
    description="Control the trading bot from any AI model or agent.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key

# ─── Lazy imports from bot internals ─────────────────────────────────────────
# We import lazily so the API can start even before bot.py fully loads.

def _bot():
    import bot as b
    return b

def _sc():
    import scanner as s
    return s

# ─── Request / Response models ────────────────────────────────────────────────

class BuyRequest(BaseModel):
    uid: int                    # Telegram user ID (used to look up mode/portfolio)
    mint: str                   # Token mint address
    sol_amount: float           # SOL to spend
    mode: Optional[str] = None  # "paper" or "live" — overrides user's current mode

class SellRequest(BaseModel):
    uid: int
    mint: str
    pct: int = 100              # % of position to sell (1–100)
    mode: Optional[str] = None

class AlertRequest(BaseModel):
    uid: int
    mint: str
    symbol: str
    direction: str              # "above" or "below"
    price_usd: float

class ThresholdRequest(BaseModel):
    uid: int
    watch_score: Optional[int] = None   # 1–100
    alert_score: Optional[int] = None   # 1–100

class AutoSellUpdate(BaseModel):
    enabled: Optional[bool]    = None
    stop_loss_pct: Optional[int] = None
    targets: Optional[list]    = None   # list of {multiplier, pct}

class MessageRequest(BaseModel):
    uid: int
    text: str
    parse_mode: str = "Markdown"

# ─── Bot reference (set from bot.py after startup) ───────────────────────────
_app_ref = None   # telegram Application instance

def set_bot_app(application):
    """Called from bot.py post_init to give us a handle to the running bot."""
    global _app_ref
    _app_ref = application

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(verify_key)])
async def status():
    """Bot health check."""
    return {
        "status":    "online",
        "timestamp": time.time(),
        "bot":       "DigitalDegenX",
    }


@app.get("/portfolio", dependencies=[Depends(verify_key)])
async def get_portfolio(uid: int):
    """Return paper portfolio for a user (SOL + token balances)."""
    b = _bot()
    portfolio = b.get_portfolio(uid)
    return {"uid": uid, "portfolio": portfolio}


@app.get("/wallet", dependencies=[Depends(verify_key)])
async def get_wallet():
    """Return live wallet SOL balance and token accounts."""
    b      = _bot()
    pubkey = b.get_wallet_pubkey()
    if not pubkey:
        raise HTTPException(status_code=503, detail="No wallet configured")
    sol_bal = b.get_sol_balance(pubkey)
    tokens  = b.get_token_accounts(pubkey)
    return {"pubkey": pubkey, "sol": sol_bal, "tokens": tokens}


@app.get("/scanner/top", dependencies=[Depends(verify_key)])
async def scanner_top(limit: int = 10):
    """Return top scanner alerts from today, sorted by score."""
    sc      = _sc()
    alerts  = sc.get_todays_alerts() or []
    alerts  = sorted(alerts, key=lambda x: -x.get("score", 0))[:limit]
    return {"count": len(alerts), "alerts": alerts}


@app.get("/scanner/watchlist", dependencies=[Depends(verify_key)])
async def scanner_watchlist():
    """Return all tokens currently on the watchlist."""
    sc = _sc()
    wl = sc.get_watchlist() or {}
    items = sorted(wl.values(), key=lambda x: -x.get("score", 0))
    return {"count": len(items), "tokens": items}


@app.get("/price/{mint}", dependencies=[Depends(verify_key)])
async def token_price(mint: str):
    """Fetch live price and market cap for a token via DexScreener."""
    b    = _bot()
    loop = asyncio.get_event_loop()
    pair = await loop.run_in_executor(None, b.fetch_sol_pair, mint)
    if not pair:
        raise HTTPException(status_code=404, detail="Token not found on DexScreener")
    return {
        "mint":       pair["baseToken"]["address"],
        "symbol":     pair["baseToken"]["symbol"],
        "price_usd":  float(pair.get("priceUsd", 0) or 0),
        "price_sol":  float(pair.get("priceNative", 0) or 0),
        "mcap":       float(pair.get("marketCap", 0) or 0),
        "volume_h1":  float((pair.get("volume") or {}).get("h1", 0) or 0),
        "dex":        pair.get("dexId", ""),
    }


@app.get("/token/{mint}/safety", dependencies=[Depends(verify_key)])
async def token_safety(mint: str):
    """Run RugCheck safety analysis on a token."""
    b      = _bot()
    safety = await b.check_token_safety(mint)
    return safety


@app.post("/buy", dependencies=[Depends(verify_key)])
async def execute_buy(req: BuyRequest):
    """
    Execute a buy.
    - mode="paper" → simulated buy against paper portfolio
    - mode="live"  → real on-chain swap via Jupiter/pump.fun
    - mode omitted → uses the user's current mode setting
    """
    b    = _bot()
    loop = asyncio.get_event_loop()
    mode = req.mode or b.get_mode(req.uid)

    lamports = int(req.sol_amount * 1_000_000_000)

    if mode == "paper":
        # Paper buy
        quote = await loop.run_in_executor(
            None, lambda: b.jupiter_quote(b.SOL_MINT, req.mint, lamports, 150)
        )
        if not quote or "outAmount" not in quote:
            raise HTTPException(status_code=502, detail="Jupiter quote failed")
        out_raw   = int(quote["outAmount"])
        portfolio = b.get_portfolio(req.uid)
        sol_bal   = portfolio.get("SOL", 0)
        if sol_bal < req.sol_amount:
            raise HTTPException(status_code=400, detail=f"Insufficient paper SOL ({sol_bal:.4f})")
        portfolio["SOL"]      = sol_bal - req.sol_amount
        portfolio[req.mint]   = portfolio.get(req.mint, 0) + out_raw
        b.update_portfolio(req.uid, portfolio)
        b.log_trade(req.uid, "paper", "buy", req.mint, quote.get("outputMint", req.mint)[:8],
                    sol_amount=req.sol_amount, token_amount=out_raw)
        return {"mode": "paper", "sol_spent": req.sol_amount, "tokens_received": out_raw,
                "new_sol_balance": portfolio["SOL"]}

    else:
        # Safety check
        safety = await b.check_token_safety(req.mint)
        if not safety["safe"]:
            raise HTTPException(status_code=400, detail=f"Safety check failed: {safety['block_reason']}")

        slippage = b.get_user_slippage(req.uid)
        sig, quote, attempts, final_slip = await b._swap_with_retry(
            b.SOL_MINT, req.mint, lamports, req.uid, loop
        )
        if "ERROR" in str(sig) or "error" in str(sig).lower():
            raise HTTPException(status_code=502, detail=f"Swap failed: {sig}")
        out_raw = int((quote or {}).get("outAmount", 0))
        return {
            "mode":             "live",
            "tx_sig":           sig,
            "sol_spent":        req.sol_amount,
            "tokens_received":  out_raw,
            "slippage_bps":     final_slip,
            "attempts":         attempts,
        }


@app.post("/sell", dependencies=[Depends(verify_key)])
async def execute_sell(req: SellRequest):
    """
    Execute a sell (pct% of current position).
    """
    b    = _bot()
    loop = asyncio.get_event_loop()
    mode = req.mode or b.get_mode(req.uid)
    pct  = max(1, min(100, req.pct))

    if mode == "paper":
        portfolio = b.get_portfolio(req.uid)
        raw_held  = portfolio.get(req.mint, 0)
        if raw_held <= 0:
            raise HTTPException(status_code=400, detail="No position to sell")
        pair = await loop.run_in_executor(None, b.fetch_sol_pair, req.mint)
        if not pair:
            raise HTTPException(status_code=404, detail="Token not found")
        price_sol = float(pair.get("priceNative", 0) or 0)
        sell_raw  = max(1, int(raw_held * pct / 100))
        dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6)
        ui        = sell_raw / (10 ** dec)
        sol_recv  = price_sol * ui * 0.99
        portfolio[req.mint] = raw_held - sell_raw
        portfolio["SOL"]    = portfolio.get("SOL", 0) + sol_recv
        if portfolio[req.mint] <= 0:
            portfolio.pop(req.mint, None)
        b.update_portfolio(req.uid, portfolio)
        b.log_trade(req.uid, "paper", "sell", req.mint, pair["baseToken"]["symbol"],
                    sol_received=sol_recv, token_amount=sell_raw)
        return {"mode": "paper", "pct": pct, "tokens_sold": sell_raw,
                "sol_received": sol_recv, "new_sol_balance": portfolio["SOL"]}

    else:
        pubkey   = b.get_wallet_pubkey()
        accounts = b.get_token_accounts(pubkey) if pubkey else []
        held     = next((a for a in accounts if a["mint"] == req.mint), None)
        if not held:
            raise HTTPException(status_code=400, detail="Token not in live wallet")
        raw_held  = held["amount"]
        sell_raw  = max(1, int(raw_held * pct / 100))
        slippage  = b.get_user_slippage(req.uid)
        sig, quote, attempts, final_slip = await b._swap_with_retry(
            req.mint, b.SOL_MINT, sell_raw, req.uid, loop
        )
        if "ERROR" in str(sig) or "error" in str(sig).lower():
            raise HTTPException(status_code=502, detail=f"Swap failed: {sig}")
        sol_recv = int((quote or {}).get("outAmount", 0)) / 1e9
        return {
            "mode":         "live",
            "tx_sig":       sig,
            "pct":          pct,
            "tokens_sold":  sell_raw,
            "sol_received": sol_recv,
            "slippage_bps": final_slip,
            "attempts":     attempts,
        }


@app.post("/scanner/threshold", dependencies=[Depends(verify_key)])
async def set_threshold(req: ThresholdRequest):
    """Update watch and/or alert score thresholds for a user."""
    import scanner_manager as sm_mod
    sm = sm_mod.ScannerManager()
    user_settings = sm.get_user_settings(req.uid)
    changed = {}
    if req.watch_score is not None:
        user_settings["alert_scouted_threshold"] = req.watch_score
        changed["watch_score"] = req.watch_score
    if req.alert_score is not None:
        user_settings["alert_warm_threshold"]      = req.alert_score
        user_settings["alert_hot_threshold"]       = req.alert_score
        user_settings["alert_ultra_hot_threshold"] = req.alert_score
        changed["alert_score"] = req.alert_score
    sm.save_user_settings(req.uid, user_settings)
    return {"uid": req.uid, "updated": changed}


@app.get("/trades", dependencies=[Depends(verify_key)])
async def get_trades(uid: int, limit: int = 20, action: Optional[str] = None):
    """Return recent trades for a user, optionally filtered by action (buy/sell)."""
    b      = _bot()
    trades = [t for t in b.load_trade_log() if t.get("uid") == uid]
    if action:
        trades = [t for t in trades if t.get("action") == action]
    trades = sorted(trades, key=lambda t: t.get("ts", 0), reverse=True)[:limit]
    return {"uid": uid, "count": len(trades), "trades": trades}


@app.get("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def get_autosell(mint: str, uid: int):
    """Return auto-sell config for a token."""
    b   = _bot()
    cfg = b.load_auto_sell().get(str(uid), {}).get(mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    return cfg


@app.post("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def update_autosell(mint: str, uid: int, update: AutoSellUpdate):
    """Update auto-sell config for a token (partial update)."""
    b    = _bot()
    data = b.load_auto_sell()
    cfg  = data.get(str(uid), {}).get(mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    if update.enabled is not None:
        cfg["enabled"] = update.enabled
    if update.stop_loss_pct is not None:
        cfg["stop_loss_pct"] = update.stop_loss_pct
    if update.targets is not None:
        cfg["targets"] = update.targets
    data.setdefault(str(uid), {})[mint] = cfg
    b.save_auto_sell(data)
    return {"mint": mint, "uid": uid, "updated": cfg}


@app.post("/message", dependencies=[Depends(verify_key)])
async def send_message(req: MessageRequest):
    """Send a Telegram message to a user via the bot."""
    if _app_ref is None:
        raise HTTPException(status_code=503, detail="Bot not connected yet")
    try:
        await _app_ref.bot.send_message(req.uid, req.text, parse_mode=req.parse_mode)
        return {"sent": True, "uid": req.uid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/score/{mint}", dependencies=[Depends(verify_key)])
async def score_token(mint: str):
    """Run a full heat score analysis on a token."""
    sc     = _sc()
    loop   = asyncio.get_event_loop()
    result = await sc.score_single_token(mint)
    if not result:
        raise HTTPException(status_code=404, detail="Token not found or could not be scored")
    return result
