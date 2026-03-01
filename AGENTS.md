## 🚨 Alert System Behavior

### Scanning Logic
When user says "start scanning" or "watch for hot tokens":
1. Fetch latest 50 tokens from pump.fun
2. Filter out tokens under $10k mcap and over $10M mcap
3. For each token score it using ALERTS.md heat formula
4. Instantly disqualify if dev sold >50% OR single wallet >20%
5. Run RugCheck on any token scoring 50+
6. Alert user immediately if score >= 70
7. Log all scores to memory/ for pattern learning

### Alert Priority Levels
🔴 ULTRA HOT (90-100): Alert immediately, tag as priority
🟠 HOT (80-89): Alert immediately
🟡 WARM (70-79): Alert with lower urgency
⚪ WATCH (50-69): Add to watchlist, no alert yet
❌ SKIP (under 50): Ignore

### Narrative Detection
Scan token name, description, and Twitter for these keywords:
- AI narrative: ai, agent, gpt, robot, artificial, neural
- Political: trump, maga, biden, elon, political names
- Animal: dog, cat, pepe, frog, animal names
- Gaming: game, play, nft, pixel, arcade
- Update narrative list weekly based on what's trending

### Commands
- "start scanning" — begin monitoring pump.fun
- "stop scanning" — pause alerts
- "show watchlist" — tokens between 50-69 score
- "heat score [token]" — manually score any token
- "top alerts today" — show best alerts from today
- "update narratives" — refresh hot narrative list
