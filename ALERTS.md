## 🚨 Meme Token Alert System

### Alert Trigger
Only send alert when token Heat Score >= 70/100
Never spam — one alert per token per hour maximum

### Heat Score Scoring

SOL Volume (20pts):
- 3x volume spike in 5 mins = 20pts
- 2x volume spike = 10pts
- Normal volume = 0pts

Unique Wallets (15pts):
- 500+ unique wallets = 15pts
- 200-500 wallets = 10pts
- 100-200 wallets = 5pts
- Under 100 = 0pts

X/Twitter Activity (15pts):
- Trending on crypto Twitter = 15pts
- Multiple influencer mentions = 10pts
- Some mentions = 5pts
- None = 0pts

Narrative Match (15pts):
- Matches current hot narrative (AI, memes, politics) = 15pts
- Partial match = 8pts
- No narrative = 0pts

Raydium Migration (10pts):
- Migrated to Raydium in last 30 mins = 10pts
- Migrating soon (>95% bonding) = 5pts
- Still on pump.fun = 0pts

Dev Wallet (10pts):
- Dev sold 0% = 10pts
- Dev sold <10% = 5pts
- Dev sold >20% = 0pts
- Dev sold >50% = INSTANT DISQUALIFY

Top Holders (10pts):
- Top 10 hold <30% = 10pts
- Top 10 hold 30-50% = 5pts
- Top 10 hold >50% = 0pts
- One wallet >20% = INSTANT DISQUALIFY

Token Age (5pts):
- 30 mins - 4 hours old = 5pts (sweet spot)
- Under 30 mins = 2pts (too early)
- Over 24 hours = 0pts

### Alert Format
When Heat Score >= 70 send this to Telegram:

🚨 HOT TOKEN ALERT 🚨
━━━━━━━━━━━━━━━━━━━
🪙 Name: [token name] ($TICKER)
📍 Address: [contract address]
🌡️ Heat Score: [score]/100
⏰ Age: [age]
💰 Market Cap: $[mcap]
📊 Volume (1h): $[volume]
👛 Unique Wallets: [count]
━━━━━━━━━━━━━━━━━━━
📈 SIGNALS:
- Volume: [score]pts - [reason]
- Wallets: [score]pts - [reason]
- Twitter: [score]pts - [reason]
- Narrative: [score]pts - [reason]
- Migration: [score]pts - [reason]
- Dev Wallet: [score]pts - [reason]
- Holders: [score]pts - [reason]
- Age: [score]pts - [reason]
━━━━━━━━━━━━━━━━━━━
🟢 Risk Level: [LOW/MEDIUM/HIGH]
⚠️ Red Flags: [list any concerns]
🔗 Chart: https://dexscreener.com/solana/[address]
🔗 Pump: https://pump.fun/[address]
━━━━━━━━━━━━━━━━━━━
