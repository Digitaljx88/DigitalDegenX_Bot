# Lifecycle Migration Todo

This backlog turns the product into a dashboard-first operator console backed by
one lifecycle spine:

`pump.fun launch -> swap.pump.fun trades -> migration detection -> Raydium pools -> DexScreener enrichment`

## Phase 0: Stabilize production

- [ ] Rotate the exposed bot API key and dashboard basic-auth password.
- [ ] Keep the bot API bound to `127.0.0.1:8080` only.
- [ ] Standardize one VPS deploy script for:
  - [ ] `git fetch/reset`
  - [ ] `npm install && npm run build`
  - [ ] dashboard container restart
  - [ ] bot restart
  - [ ] smoke tests for `/scanner`, `/trades`, `/portfolio`, `/settings`
- [ ] Add rollback instructions for the VPS deployment path.

## Phase 1: Lifecycle foundation

- [x] Add lifecycle tables in SQLite:
  - [x] `token_lifecycle`
  - [x] `token_trade_metrics`
  - [x] `token_events`
  - [x] `token_enrichment`
- [x] Add DB helpers for lifecycle persistence and snapshot reads.
- [x] Add shared lifecycle dataclasses and store module.
- [x] Add initial lifecycle API reads:
  - [x] `GET /token/{mint}/snapshot`
  - [x] `GET /token/{mint}/timeline`
  - [x] lifecycle-aware `GET /scanner/feed` fallback
- [ ] Backfill lifecycle rows from existing scanner log where possible.

## Phase 2: Ingestion adapters

- [x] Wire [helius_ws.py](/Users/rosalindjames/DigitalDegenX_Bot/helius_ws.py) launch detection into lifecycle store.
- [x] Wire [helius_ws.py](/Users/rosalindjames/DigitalDegenX_Bot/helius_ws.py) migration detection into lifecycle store.
- [x] Wire [pumpfeed.py](/Users/rosalindjames/DigitalDegenX_Bot/pumpfeed.py) live pump/Raydium events into lifecycle metrics.
- [ ] Persist Raydium pool metadata when migration is detected.
- [ ] Persist DexScreener enrichment into lifecycle enrichment records instead of only transient scanner payloads.

## Phase 3: Snapshot-driven scanner and auto-buy

- [x] Add a normalized token snapshot adapter used by scanner and auto-buy.
- [x] Add a shared trading snapshot builder so scanner and auto-buy consume the same normalized lifecycle object.
- [x] Make [scanner.py](/Users/rosalindjames/DigitalDegenX_Bot/scanner.py) score normalized lifecycle snapshots first, with legacy fetch fallback during migration.
- [x] Make [autobuy.py](/Users/rosalindjames/DigitalDegenX_Bot/autobuy.py) gate and size entries from lifecycle snapshots.
- [x] Make scanner quality gating prefer snapshot-native age/source/buy-ratio/liquidity/score fields before transient fetch fallbacks.
- [ ] Keep DexScreener as enrichment only, not primary token discovery truth.
- [ ] Ensure `pumpfun_newest` stays primary for earliest launch edge.

## Phase 4: Dashboard parity for daily ops

- [ ] Scanner feed:
  - [x] dedupe by mint
  - [x] token detail drawer/page
  - [x] timeline view per mint
  - [ ] watchlist page
  - [ ] top alerts page
- [ ] Trade Center:
  - [x] ledger
  - [x] closed P&L
  - [x] cohorts
  - [x] weekly report
  - [ ] CSV export button
- [ ] Portfolio:
  - [x] paper/live split
  - [x] current mode visible
  - [ ] position detail drawers
  - [ ] auto-sell summary per position
- [ ] Settings:
  - [x] mode switch
  - [x] auto-buy sizing/exposure controls
  - [ ] alert routing controls
  - [ ] scanner pause/enable controls

## Phase 5: Advanced trade management

- [ ] Full auto-sell editor in dashboard.
- [ ] Auto-sell preset editor in dashboard.
- [ ] Global SL / trailing / breakeven settings in dashboard.
- [ ] Manual buy/sell confirmations in dashboard.
- [ ] Research/history export flows.

## Phase 6: Intelligence and research parity

- [ ] `/intel/wallets`
- [ ] `/intel/narratives`
- [ ] `/intel/cluster`
- [ ] `/intel/bundle`
- [ ] `/intel/playbook`
- [ ] normalized intelligence API response shapes

## Phase 7: Telegram reduction

- [ ] Keep Telegram focused on alerts and settings only.
- [ ] Replace manual trade commands with dashboard links.
- [ ] Replace research/intel commands with dashboard links.
- [ ] Simplify Telegram keyboards and callbacks.
- [ ] Prune dead text-state handlers after shadow period.

## Phase 8: Validation and operator polish

- [ ] Add lifecycle store tests.
- [ ] Add migration detector tests.
- [ ] Add lifecycle-backed scanner feed tests.
- [ ] Add smoke tests for dashboard buy/sell parity.
- [ ] Improve empty/error states in dashboard panels.
- [ ] Add one-click refresh/retry controls where requests are long-running.
