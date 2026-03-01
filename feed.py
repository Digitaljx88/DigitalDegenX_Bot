"""
Channel feed system.

Feed 1 — New pump.fun launches (customisable filters).
Feed 2 — Raydium migration alerts.

Both post to Telegram channels. The bot must be an admin of those channels.
Config is stored in data/feed_config.json.
Dedup state is stored in data/feed_state.json.
"""

import json
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

DATA_DIR         = os.path.join(os.path.dirname(__file__), "data")
FEED_CONFIG_FILE = os.path.join(DATA_DIR, "feed_config.json")
FEED_STATE_FILE  = os.path.join(DATA_DIR, "feed_state.json")
os.makedirs(DATA_DIR, exist_ok=True)

# How long to remember a token to avoid re-posting (24 h)
DEDUP_TTL = 86_400


# ── Config helpers ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "launch_channel":    "",      # Telegram channel ID (str) for new launches
    "migrate_channel":   "",      # Telegram channel ID for migrations
    "launch_enabled":    False,
    "migrate_enabled":   False,
    "min_mcap":          10_000,
    "max_mcap":          500_000,
    "min_heat_score":    0,       # 0 = post all; 50+ = filtered
    "min_wallets":       0,
    "narrative_filter":  "All",   # All / AI / Political / Animal / Gaming / RWA
}


def load_feed_config() -> dict:
    try:
        with open(FEED_CONFIG_FILE) as f:
            cfg = json.load(f)
        # Back-fill any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)


def save_feed_config(cfg: dict):
    with open(FEED_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def load_feed_state() -> dict:
    try:
        with open(FEED_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posted_launches": {}, "posted_migrations": {}}


def save_feed_state(s: dict):
    with open(FEED_STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def _already_posted(state_key: str, mint: str) -> bool:
    s = load_feed_state()
    ts = s.get(state_key, {}).get(mint, 0)
    return (time.time() - ts) < DEDUP_TTL


def _mark_posted(state_key: str, mint: str):
    s = load_feed_state()
    s.setdefault(state_key, {})[mint] = time.time()
    # Prune old entries
    cutoff = time.time() - DEDUP_TTL
    s[state_key] = {k: v for k, v in s[state_key].items() if v > cutoff}
    save_feed_state(s)


# ── Message formatters ────────────────────────────────────────────────────────

def _age_str(pair_created_ms: int) -> str:
    if not pair_created_ms:
        return "?"
    mins = (time.time() * 1000 - pair_created_ms) / 60_000
    return f"{mins:.0f}m" if mins < 60 else f"{mins/60:.1f}h"


def format_launch_post(token: dict, heat_score: int, priority_label: str) -> str:
    mint = token.get("mint", "")
    return (
        f"🆕 *NEW LAUNCH* — {priority_label}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token.get('name', '?')}* (${token.get('symbol', '?')})\n"
        f"📍 `{mint}`\n"
        f"🌡️ Heat Score: *{heat_score}/100*\n"
        f"⏰ Age: {_age_str(token.get('pair_created', 0))}\n"
        f"💰 MCap: ${token.get('mcap', 0):,.0f}\n"
        f"📊 Vol 1h: ${token.get('volume_h1', 0):,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Chart](https://dexscreener.com/solana/{mint})  "
        f"[Pump](https://pump.fun/{mint})"
    )


def format_migration_post(token: dict) -> str:
    mint = token.get("mint", "")
    return (
        f"🚀 *MIGRATED TO RAYDIUM*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token.get('name', '?')}* (${token.get('symbol', '?')})\n"
        f"📍 `{mint}`\n"
        f"💰 MCap: ${token.get('mcap', 0):,.0f}\n"
        f"📊 Vol 1h: ${token.get('volume_h1', 0):,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Now tradeable on Raydium via Jupiter\n"
        f"🔗 [Chart](https://dexscreener.com/solana/{mint})  "
        f"[Trade](https://jup.ag/swap/SOL-{mint})"
    )


def _launch_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
        InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
        InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}"),
    ]])


def _migrate_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Chart",  url=f"https://dexscreener.com/solana/{mint}"),
        InlineKeyboardButton("🔄 Trade",  url=f"https://jup.ag/swap/SOL-{mint}"),
    ]])


# ── Posting ───────────────────────────────────────────────────────────────────

async def maybe_post_launch(bot, token: dict, heat_score: int, priority_label: str):
    """
    Post to launch feed channel if token passes filters and hasn't been posted.
    Called by scanner's run_scan() for every token it scores.
    """
    cfg  = load_feed_config()
    mint = token.get("mint", "")

    if not cfg["launch_enabled"] or not cfg["launch_channel"] or not mint:
        return

    # Dedup
    if _already_posted("posted_launches", mint):
        return

    # Apply filters
    mcap    = token.get("mcap", 0)
    wallets = token.get("total_holders", 0)
    narr    = token.get("matched_narrative", "")

    if not (cfg["min_mcap"] <= mcap <= cfg["max_mcap"]):
        return
    if heat_score < cfg["min_heat_score"]:
        return
    if wallets < cfg["min_wallets"]:
        return
    if cfg["narrative_filter"] != "All" and cfg["narrative_filter"] not in narr:
        return

    _mark_posted("posted_launches", mint)
    msg = format_launch_post(token, heat_score, priority_label)
    try:
        await bot.send_message(
            chat_id=cfg["launch_channel"],
            text=msg,
            parse_mode="Markdown",
            reply_markup=_launch_kb(mint),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def maybe_post_migration(bot, token: dict):
    """
    Post to migration feed when a pump.fun token graduates to Raydium.
    Called by scanner when dex_id switches to 'raydium'.
    """
    cfg  = load_feed_config()
    mint = token.get("mint", "")

    if not cfg["migrate_enabled"] or not cfg["migrate_channel"] or not mint:
        return

    if _already_posted("posted_migrations", mint):
        return

    _mark_posted("posted_migrations", mint)
    msg = format_migration_post(token)
    try:
        await bot.send_message(
            chat_id=cfg["migrate_channel"],
            text=msg,
            parse_mode="Markdown",
            reply_markup=_migrate_kb(mint),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ── Feed status summary ───────────────────────────────────────────────────────

def feed_status_text() -> str:
    cfg = load_feed_config()

    launch_ch  = cfg["launch_channel"]  or "not set"
    migrate_ch = cfg["migrate_channel"] or "not set"
    launch_on  = "✅ ON" if cfg["launch_enabled"]  else "⏸️ OFF"
    migrate_on = "✅ ON" if cfg["migrate_enabled"] else "⏸️ OFF"

    return (
        f"*📡 Channel Feeds*\n\n"
        f"*New Launches:* {launch_on}\n"
        f"Channel: `{launch_ch}`\n"
        f"MCap: ${cfg['min_mcap']:,} – ${cfg['max_mcap']:,}\n"
        f"Min Heat Score: {cfg['min_heat_score']}\n"
        f"Min Wallets: {cfg['min_wallets']}\n"
        f"Narrative: {cfg['narrative_filter']}\n\n"
        f"*Raydium Migrations:* {migrate_on}\n"
        f"Channel: `{migrate_ch}`"
    )


def feed_settings_kb() -> InlineKeyboardMarkup:
    cfg = load_feed_config()
    launch_toggle  = "⏸️ Pause Launches"  if cfg["launch_enabled"]  else "▶️ Enable Launches"
    migrate_toggle = "⏸️ Pause Migrations" if cfg["migrate_enabled"] else "▶️ Enable Migrations"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Set Launch Channel",    callback_data="feed:set_launch_ch"),
         InlineKeyboardButton("🚀 Set Migration Channel", callback_data="feed:set_migrate_ch")],
        [InlineKeyboardButton("💰 MCap Range",            callback_data="feed:set_mcap"),
         InlineKeyboardButton("🌡️ Min Heat Score",        callback_data="feed:set_heat")],
        [InlineKeyboardButton("👛 Min Wallets",           callback_data="feed:set_wallets"),
         InlineKeyboardButton("🏷️ Narrative Filter",      callback_data="feed:set_narrative")],
        [InlineKeyboardButton(launch_toggle,              callback_data="feed:toggle_launch"),
         InlineKeyboardButton(migrate_toggle,             callback_data="feed:toggle_migrate")],
        [InlineKeyboardButton("⬅️ Main Menu",             callback_data="menu:main")],
    ])


def narrative_kb() -> InlineKeyboardMarkup:
    options = ["All", "AI", "Political", "Animal", "Gaming", "RWA"]
    rows    = []
    row     = []
    for i, n in enumerate(options):
        row.append(InlineKeyboardButton(n, callback_data=f"feed:narr:{n}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="feed:back")])
    return InlineKeyboardMarkup(rows)
