# meme-trade-bot — Change Log & Feature Reference

All significant changes, fixes, and planned work are documented here.

---

## TODO List

| # | Feature | Status |
|---|---------|--------|
| 1 | Jito MEV protection on live trades | ✅ Complete |
| 2 | Priority fee presets UI in Settings | ✅ Complete |
| 3 | Token safety check before live buy | ✅ Complete |
| 4 | Auto-slippage retry on failed live trades | ✅ Complete |
| 5 | Wallet tracker — monitor wallets, alert on buys | ✅ Complete |
| 6 | Realized PnL trade history page (/history) | ✅ Complete |
| 7 | Quick buy amount presets per user | ✅ Complete |
| 8 | PnL share card on sell completion | ✅ Complete |

---

## Completed Changes

---

### [Session 3] — 2026-03-08

#### Bug Fix: Portfolio token value showing "?" on large portfolios
- **Root cause**: `fetch_sol_pair()` was called synchronously one-by-one inside an async loop. With many tokens, DexScreener rate-limits sequential requests → returns null → `price_sol = 0` → `"?"`.
- **Fix**: All tokens on the current page are now fetched concurrently via `asyncio.gather` + `run_in_executor`.
- **Files changed**: `bot.py`
  - Added `_portfolio_price_cache` dict (60-second TTL per mint)
  - Added `_get_cached_price(mint)`, `_set_cached_price(mint, pair, bc, coin)`
  - Added `async _fetch_portfolio_token_data(mint)` — concurrent fetch with cache
  - `_show_portfolio()` now uses `asyncio.gather(*[_fetch_portfolio_token_data(...)])` for all page tokens

#### Feature: Portfolio improvements
- **USD values** shown alongside SOL on every token line: `0.0234◎ ($3.51)`
- **MCap formatting** improved: shows `$1.23M` instead of `$1230.0K`
- **Holdings count** in header: `Holdings: 12 tokens • SOL: 4.2300 ($630)`
- **Portfolio total** always visible in header with USD equivalent
- **Overall P&L** shown in header: `P&L: +0.1◎ +41.2% 📈`
- **Positions sorted by value** (descending) — biggest holdings always show first
- **P&L badge on pump.fun tokens** — previously only DexScreener tokens showed it
- **Page value + global total** footer on multi-page portfolios
- **"?"** replaced with `~` for truly unlisted tokens

#### Bug Fix: Paper trading sell returning wrong SOL amount
- **Root cause**: All paper sells used `jupiter_quote()` which reflects real on-chain price impact. For thin-liquidity tokens, selling 90% of a position could have 40-50%+ price impact in Jupiter's routing engine, wiping out all profits. This caused e.g. 0.1 SOL in → 2x → 90% sell → 0.09 SOL out (should be ~0.18).
- **Fix**: Paper sells now execute at current **market price** with 1% simulated fee, not Jupiter quote.
- **Formula**: `sol_received = (price_sol × ui_tokens) × 0.99`
- **Files changed**: `bot.py` — fixed all 4 paper sell paths:
  1. `execute_auto_sell()` — paper path: uses `price_usd / sol_usd × ui × 0.99`
  2. `qp_callback()` paper sell — uses `pair.priceNative × ui × 0.99`
  3. `do_trade_flow()` paper sell — uses `pair.priceNative × ui × 0.99`
  4. `sell_all_exec` paper path — uses `pair.priceNative × ui × 0.99`

#### Feature: Slippage setting for live trades
- **Description**: Per-user adjustable slippage for all live trades (buys + sells). Default 1.5% (150 bps).
- **Storage**: `data/global_settings.json` → `user_trade_settings.{uid}.slippage_bps`
- **Range**: 10–5000 bps (0.1% – 50%)
- **UI**: Settings menu → `⚡ Slippage: 1.5%` button → preset grid + custom bps input
- **Presets**: 0.5%, 1%, 1.5%, 2%, 3%, 5%, 10%, 20%, 30%
- **Files changed**: `bot.py`
  - Added `get_user_slippage(uid)`, `set_user_slippage(uid, bps)`
  - `jupiter_quote()` signature updated to accept `slippage_bps: int = 150`
  - `settings_kb()` shows current slippage and links to slippage menu
  - Added `_slippage_kb()` for preset grid
  - `settings_callback` handles `slippage`, `slip_set:{bps}`, `slip_custom` actions
  - State handler `slippage_custom` added for text input
  - Applied `get_user_slippage(uid)` to all live `jupiter_quote()` calls:
    - `execute_auto_sell` live path
    - `execute_auto_buy` live path
    - `do_trade_flow` live buy + sell
    - `qp_callback` live buy + sell
    - `sell_all_exec` live path
    - Jito re-route on pump.fun graduation

---

### [Session 4] — 2026-03-08

#### Feature: Jito MEV protection *(in progress)*
- **Description**: Route live swap transactions through Jito's block engine to prevent sandwich attacks.
- **Mechanism**: After building the Jupiter swap transaction, submit it as a Jito bundle with a tip instruction. Validators running Jito software prioritise bundled transactions.
- **Config additions** (`config.py`):
  - `JITO_ENABLED = True` — global on/off
  - `JITO_TIP_LAMPORTS = 100_000` — default tip (0.0001 SOL). Higher = faster inclusion.
  - `JITO_ENDPOINT` — Jito block engine URL
- **Per-user toggle**: Settings → `🛡️ Jito MEV` on/off button
- **Storage**: `global_settings.json` → `user_trade_settings.{uid}.jito_enabled`
- **Files changed**: `bot.py`, `config.py`

#### Feature: Priority fee presets UI *(in progress)*
- **Description**: Per-user priority fee setting with preset buttons instead of config.py only.
- **Presets**:
  - Low: 100,000 µlamports/CU
  - Medium: 500,000 µlamports/CU (current default)
  - High: 1,000,000 µlamports/CU
  - Turbo: 3,000,000 µlamports/CU
  - Custom: user-entered value
- **Storage**: `global_settings.json` → `user_trade_settings.{uid}.priority_fee`
- **UI**: Settings → `⚡ Priority Fee: Medium` button
- **Files changed**: `bot.py`, `config.py`

---

## Architecture Notes

### Data files
| File | Purpose |
|------|---------|
| `data/portfolios.json` | Paper portfolio balances per user |
| `data/auto_sell.json` | Per-token auto-sell configs (entry price, targets, stop loss) |
| `data/global_settings.json` | Global + per-user settings (heat score, slippage, Jito, priority fee) |
| `data/scanner_state.json` | Scanner state and token cache |
| `data/trade_log.json` | Full trade history for all users |
| `data/user_modes.json` | Paper vs live mode per user |

### Key functions
| Function | File | Purpose |
|----------|------|---------|
| `_show_portfolio()` | bot.py:2183 | Renders portfolio (paper + live), concurrent price fetch |
| `_fetch_portfolio_token_data(mint)` | bot.py:574 | Async price fetch with 60s TTL cache |
| `execute_auto_sell()` | bot.py:1066 | Sells pct% of position; paper uses market price |
| `execute_auto_buy()` | bot.py:~1700 | Auto-buy on scanner signal |
| `execute_swap_live()` | bot.py:831 | Signs + submits Jupiter swap tx to RPC |
| `jupiter_quote(in, out, amount, slippage_bps)` | bot.py:818 | Gets Jupiter quote; slippage now per-user |
| `get_user_slippage(uid)` | bot.py:192 | Returns user slippage in bps (default 150) |
| `set_user_slippage(uid, bps)` | bot.py:197 | Persists user slippage |
| `do_trade_flow()` | bot.py:1861 | Manual buy/sell flow from user input |
| `qp_callback()` | bot.py:7241 | Quick pct buy/sell buttons (25/50/90/100%) |

### Paper sell calculation (correct formula)
```
price_sol  = pair.priceNative  (SOL per UI token from DexScreener)
           OR bc.vsr / bc.vtr / 1e9 * 1e6  (pump.fun bonding curve spot)
ui_tokens  = raw_held / (10 ** decimals)
sol_out    = price_sol * ui_tokens * 0.99   # 1% simulated fee
```
Jupiter is NOT used for paper sells — it introduced real price impact.

### Slippage bps reference
| Setting | bps | % |
|---------|-----|---|
| Default | 150 | 1.5% |
| Thin token | 500 | 5% |
| New pump.fun launch | 1000–3000 | 10–30% |
| Max allowed | 5000 | 50% |
