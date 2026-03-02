## 🚨 Alert System Behavior

### Scanning Logic
When user says "start scanning" or "watch for hot tokens":
1. Fetch tokens from pump.fun sorted by created_timestamp DESC (newest first)
2. Filter out any token older than 4 hours — skip immediately, do not score
3. Filter out tokens under $10k mcap and over $10M mcap
4. Skip any token address already seen this session (cache seen addresses)
5. For each remaining token score it using ALERTS.md heat formula
6. Instantly disqualify if dev sold >50% OR single wallet >20%
7. Run RugCheck on any token scoring 50+
8. Alert user immediately if score >= 70
9. Log all scores to memory/ for pattern learning

### Token Freshness Rules
- Always sort fetched tokens by created_timestamp DESC before processing
- Before scoring any token: check age — if over 4 hours, skip it
- Maintain a seen_tokens cache (set of mint addresses) per session
- Never process the same mint address twice in the same session
- Reset seen_tokens cache only when scanner is fully stopped and restarted

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
