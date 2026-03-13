TELEGRAM_TOKEN     = "YOUR_BOT_TOKEN_HERE"           # from @BotFather
SOLANA_RPC         = "https://api.mainnet-beta.solana.com"
WALLET_PRIVATE_KEY = ""   # base58 private key — required for live trading
OPENCLAW_CONTAINER = "openclaw-jgoz-openclaw-1"

# Admin Telegram user IDs (can access admin commands)
ADMIN_IDS = []   # e.g. [123456789]

PAPER_START_SOL    = 10.0
ALERT_CHECK_SECS   = 60     # how often to poll prices for alerts
HELIUS_API_KEY     = ""     # Helius API key for WebSocket feeds (optional)

# Priority fee / Jito defaults
PRIORITY_FEE_MICRO_LAMPORTS = 500_000
PRIORITY_FEE_COMPUTE_UNITS  = 200_000
JITO_ENABLED                = True
JITO_TIP_LAMPORTS           = 50_000
JITO_ENDPOINT               = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
ANTI_SANDWICH_JITO_TIP_SCALE = True
