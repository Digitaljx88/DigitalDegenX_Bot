## 🚨 Alert System Data Sources

### Primary Sources (check every scan)
- Pump.fun new tokens: https://frontend-api.pump.fun/coins?sortBy=last_trade_timestamp&limit=50
- DexScreener Solana trending: https://api.dexscreener.com/latest/dex/search?q=solana
- Raydium new pools: https://api.raydium.io/v2/main/pairs

### Volume & Wallet Data
- Birdeye token overview: https://public-api.birdeye.so/public/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=20&min_liquidity=1000
- Helius wallet tracker: use for dev wallet monitoring

### Social Signals
- Use web search for: "[token name] site:twitter.com"
- Search: "pump.fun [ticker] crypto twitter"
- LunarCrush API for social volume scores

### Rug Detection (always run)
- RugCheck: https://api.rugcheck.xyz/v1/tokens/{mint}/report
- Auto disqualify if: dev sold >50%, one wallet >20%, mint not renounced

### Current Hot Narratives (update weekly)
- AI agents on Solana
- Political memes
- Animal memes
- Gaming tokens
- Real world asset memes
