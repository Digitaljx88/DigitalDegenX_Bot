# Testing Guide: Auto-Sell Fixes

## Quick Start

### Test Environment Setup
```bash
# Make sure bot is running
/opt/homebrew/bin/python3 bot.py

# In another terminal, watch logs
tail -f bot.log 2>/dev/null || tail -f /tmp/bot_debug.log
```

---

## Test 1: Auto-Sell Cleanup on Reset ✅

**Purpose**: Verify that `/reset` command clears both portfolio AND auto-sell configs

### Steps:
1. **Start a paper trade**:
   - Send `/buy BONK 0.1` to bot
   - Confirm the trade (click button)
   - Expected: Auto-sell config created with default 2x→50%, 4x→50%

2. **Verify auto-sell config exists**:
   - Check `data/auto_sell.json`
   - Look for your user ID → should have BONK mint entry

3. **Reset portfolio**:
   - Send `/reset` to bot
   - Expected: "🗑️ Paper portfolio reset to `10 SOL`."

4. **Verify cleanup**:
   - Check `data/auto_sell.json` again
   - Your user ID entry should be completely removed OR empty
   - ✅ **PASS** if auto-sell config is gone

### Debug Commands:
```bash
# Check portfolio after reset
cat data/portfolios.json | grep -A 2 "YOUR_USER_ID"

# Check auto-sell after reset
cat data/auto_sell.json | grep -A 5 "YOUR_USER_ID"
```

---

## Test 2: Customizable Auto-Sell Multipliers ✅

**Purpose**: Verify user presets are stored and applied to trades

### Steps:
1. **Customize presets before trading**:
   - Send `/autosell` or look for preset menu
   - Click "🔧 Customize Presets" (if visible in settings)
   - Edit presets:
     - Change 2x→50% to **1.5x→75%**
     - Change 4x→50% to **3x→50%**
   - Click save

2. **Execute a trade**:
   - Send `/buy BONK 0.1`
   - Check the auto-sell confirmation message
   - Expected: "🤖 Auto-sell configured: `1.5x→75%, 3x→50%`" (your custom values)

3. **Verify config storage**:
   - Check `data/global_settings.json`
   - Look for key `as_presets_YOUR_USER_ID`
   - Should contain: `[{"mult": 1.5, "sell_pct": 75}, {"mult": 3.0, "sell_pct": 50}]`

4. **Verify next trade uses presets**:
   - Send `/buy BONK 0.05` (different amount)
   - Check confirmation message
   - Expected: Same custom presets (1.5x→75%, 3x→50%)

### Debug Check:
```bash
# View your user's preset settings
cat data/global_settings.json | python3 -m json.tool | grep -A 10 "as_presets"
```

---

## Test 3: Custom Auto-Sell Config UI ✅

**Purpose**: Verify users can customize presets via Telegram UI

### Steps:
1. **Access preset customization menu**:
   - Send `/settings` or `/menu`
   - Look for button that leads to auto-sell settings
   - Click "🔧 Customize Presets" or similar

2. **Test edit target flow**:
   - Select a target to edit (e.g., "Edit 2x")
   - Try these button actions:
     - Click `🔺` next to multiplier → should increase to 2.5x
     - Click `🔻` next to multiplier → should decrease back to 2x
     - Click `🔺` next to percentage → should increase by 5%
     - Click `🔻` next to percentage → should decrease by 5%

3. **Verify updates persist**:
   - Adjust a target to `1.8x` with `65%`
   - Go back to main menu
   - Return to customize presets
   - Expected: Your changes are still there

4. **Test add new target**:
   - Click "➕ Add Target"
   - Select a preset (e.g., "5x (moon)")
   - Expected: New target added to your preset list

5. **Test delete target**:
   - Edit a target and click "🗑️ Delete"
   - Expected: Target removed from list

### Expected Button Responses:
- Multiplier +/- buttons: Adjust by 0.5x increments
- Percentage +/- buttons: Adjust by 5% increments
- Min values: 0.5x mult, 5% sell
- Max values: 10x mult, 100% sell

---

## Test 4: Auto-Sell Trigger Timing ✅

**Purpose**: Verify auto-sell triggers at correct prices and percentages

### Steps:
1. **Create a position with known buy price**:
   - Send `/buy BONK 0.1`
   - Note the buy price from bot's response
   - Example: Buy price = $0.000001

2. **Set up a 2x target**:
   - Auto-sell should have 2x→50% target
   - This means: At price ≥ (buy_price × 2), sell 50% of holdings

3. **Monitor price movement** (use real or mock data):
   - Watch price with `/price BONK` or DexScreener
   - When price hits exactly 2x:
     - Expected: Auto-sell triggers within 15 seconds
     - Expected: Bot sends "🤖 *Auto-Sell Triggered — 2x*"
     - Expected: You see "Sold: `50%` of position"

4. **Check second target**:
   - If you have 4x→50%, wait for price to hit 4x
   - Expected: Another auto-sell message for 50% of remaining

5. **Verify portfolio updated**:
   - Send `/portfolio`
   - Should show reduced BONK balance after sells
   - SOL balance should increase

### Debug Log Output:
```bash
# Look for auto-sell trigger messages
grep "Auto-Sell Triggered" /tmp/bot_debug.log
grep "Sold:" /tmp/bot_debug.log
```

### Test with Stop-Loss:
1. Buy BONK at price $0.000001
2. Set stop-loss to 50% drop
3. If price drops to $0.0000005 (50% drop):
   - Expected: Auto-sell triggers
   - Message: "🤖 *Auto-Sell Triggered — Stop-Loss -50%*"

---

## Test 5: Stale Entry Safeguards ✅

**Purpose**: Verify orphaned auto-sell configs are cleaned up

### Steps:
1. **Create multiple positions**:
   - `/buy BONK 0.1`
   - `/buy SHIB 0.05`
   - `/buy PEPE 0.02`
   - All three should have auto-sell configs

2. **Sell one position manually**:
   - Send `/sell BONK 1000000` (sell entire BONK position)
   - Expected: Portfolio updated, BONK removed

3. **Wait for cleanup job**:
   - Wait 15-20 seconds (job runs every 15s)
   - Check logs for cleanup message:
     ```
     [AUTO-SELL] Cleaned up stale entry: uid=YOUR_ID, mint=BONK...
     ```

4. **Verify cleanup**:
   - Check `data/auto_sell.json`
   - BONK entry should be removed
   - SHIB and PEPE should still exist

5. **Test with portfolio reset**:
   - `/reset` to clear all positions
   - Check `data/auto_sell.json`
   - All entries for your user ID should be gone

### Debug Verification:
```bash
# Before selling BONK
cat data/auto_sell.json | python3 -c "import sys, json; d=json.load(sys.stdin); print(list(d.get('YOUR_USER_ID', {}).keys()))"
# Expected: ['BONK_MINT', 'SHIB_MINT', 'PEPE_MINT']

# After selling BONK and waiting 15s
cat data/auto_sell.json | python3 -c "import sys, json; d=json.load(sys.stdin); print(list(d.get('YOUR_USER_ID', {}).keys()))"
# Expected: ['SHIB_MINT', 'PEPE_MINT']
```

---

## Test 6: Breakeven Stop Logic Fix ✅

**Purpose**: Verify breakeven stop doesn't cause double-sells

### Steps:
1. **Set up position with breakeven stop**:
   - Buy BONK at $0.000001
   - Configure: Breakeven stop at 2x, Hard stop-loss at 50% drop

2. **Wait for price to hit 2x**:
   - When price reaches $0.000002 (2x):
   - Expected: "🛡️ *Breakeven Stop Activated*" message
   - Expected: Stop-loss moved to entry price

3. **Drop price slightly below 2x**:
   - Price drops to $0.0000019
   - Expected: No additional sell triggered (not hitting SL yet)

4. **Drop price to entry or below**:
   - Price drops to $0.000001 or less
   - Expected: "🤖 *Auto-Sell Triggered — Stop-Loss*" (breakeven SL)
   - Expected: Only 1 sell message (not double-sell)

5. **Verify portfolio**:
   - Send `/portfolio`
   - Position should be sold once (not twice)
   - ✅ **PASS** if only one auto-sell execution occurred

### Common Bug Sign (FAIL):
- Receiving TWO auto-sell messages for breakeven stop
- Position sold twice despite only one trigger event

---

## Test 7: Integration Test (Full Workflow) ✅

**Purpose**: Test all features together in realistic scenario

### Scenario:
1. **Start fresh**:
   - `/reset` to clear portfolio

2. **Customize presets**:
   - Set custom presets: 1.5x→40%, 2x→35%, 3x→25%
   - Verify in settings menu

3. **Execute trade**:
   - `/buy BONK 0.1`
   - Confirm your custom presets show in response

4. **Monitor auto-sell**:
   - Check `/portfolio` shows BONK position
   - Watch price movement or mock it
   - When price hits 1.5x: Auto-sell 40% should trigger
   - When price hits 2x: Auto-sell 35% should trigger
   - When price hits 3x: Auto-sell 25% should trigger

5. **Partial sell manually**:
   - `/sell BONK 500000` (manual partial sell)
   - Remaining position should still track auto-sell

6. **Full reset**:
   - `/reset`
   - Verify:
     - Portfolio cleared
     - Auto-sell configs removed
     - Global presets preserved

---

## Automated Testing (Python)

Create file `test_autosell.py`:

```python
#!/usr/bin/env python3
import json
import os
from pathlib import Path

DATA_DIR = Path("data")
AUTO_SELL_FILE = DATA_DIR / "auto_sell.json"
GLOBAL_SETTINGS_FILE = DATA_DIR / "global_settings.json"
PORTFOLIOS_FILE = DATA_DIR / "portfolios.json"

def load_json(filepath):
    if not filepath.exists():
        return {}
    with open(filepath) as f:
        return json.load(f)

def test_reset_cleanup():
    """Test 1: Verify reset clears auto-sell"""
    auto_sell = load_json(AUTO_SELL_FILE)
    portfolios = load_json(PORTFOLIOS_FILE)
    
    test_uid = "123456"  # Replace with your Telegram ID
    
    # Check that if portfolio is reset to 10 SOL, auto_sell is empty
    if test_uid in portfolios and portfolios[test_uid] == {"SOL": 10}:
        assert test_uid not in auto_sell, "FAIL: Auto-sell not cleaned on reset"
        print("✅ PASS: Reset cleanup works")
    else:
        print("⏭️  SKIP: Need to run /reset first")

def test_custom_presets():
    """Test 2: Verify custom presets are stored"""
    settings = load_json(GLOBAL_SETTINGS_FILE)
    test_uid = "123456"  # Replace with your Telegram ID
    
    preset_key = f"as_presets_{test_uid}"
    if preset_key in settings:
        presets = settings[preset_key]
        assert isinstance(presets, list), "Presets should be array"
        assert all("mult" in p and "sell_pct" in p for p in presets), "Each preset needs mult and sell_pct"
        print(f"✅ PASS: Custom presets stored: {presets}")
    else:
        print("⏭️  SKIP: No custom presets set yet")

def test_autosell_configs():
    """Test 3: Verify auto-sell configs exist for positions"""
    auto_sell = load_json(AUTO_SELL_FILE)
    portfolios = load_json(PORTFOLIOS_FILE)
    test_uid = "123456"  # Replace with your Telegram ID
    
    if test_uid not in portfolios:
        print("⏭️  SKIP: No positions in portfolio")
        return
    
    portfolio = portfolios[test_uid]
    positions = [k for k in portfolio.keys() if k != "SOL"]
    
    if not positions:
        print("⏭️  SKIP: No token positions in portfolio")
        return
    
    as_configs = auto_sell.get(test_uid, {})
    
    for mint in positions:
        if mint in as_configs:
            cfg = as_configs[mint]
            assert "mult_targets" in cfg, f"Config missing mult_targets"
            assert "buy_price_usd" in cfg, f"Config missing buy_price_usd"
            print(f"✅ PASS: Auto-sell config exists for {mint[:8]}")
        else:
            print(f"⚠️  WARNING: No auto-sell config for {mint[:8]}")

if __name__ == "__main__":
    print("Running Auto-Sell Tests...\n")
    test_reset_cleanup()
    test_custom_presets()
    test_autosell_configs()
    print("\nDone!")
```

Run tests:
```bash
/opt/homebrew/bin/python3 test_autosell.py
```

---

## Telegram Testing Checklist

Create this checklist in your notes and check off as you test:

```
PHASE 1: Reset & Cleanup
☐ Create paper trade
☐ Verify auto-sell config created
☐ Run /reset command
☐ Verify auto-sell config removed

PHASE 2: Custom Multipliers
☐ Customize presets to 1.5x→75%, 3x→50%
☐ Buy a token
☐ Verify custom values show in confirmation
☐ Buy another token
☐ Verify same custom values apply

PHASE 3: UI Customization
☐ Access preset menu
☐ Edit a target (change 2x to 1.8x)
☐ Adjust percentage +/- buttons
☐ Add new target (5x)
☐ Delete a target
☐ Go back and verify changes persisted

PHASE 4: Trigger Timing
☐ Buy token at known price
☐ Monitor price increase
☐ When price hits 2x, verify auto-sell triggers
☐ Verify 50% sold (not 100%)
☐ Verify portfolio updated

PHASE 5: Stale Entry Cleanup
☐ Buy 3 different tokens
☐ Sell one token completely
☐ Wait 15 seconds
☐ Verify sold token's auto-sell removed
☐ Verify other tokens' auto-sell still exist

PHASE 6: Breakeven Stop
☐ Set breakeven stop at 2x
☐ Wait for price to hit 2x
☐ Verify "Breakeven Stop Activated" message
☐ Drop price to entry level
☐ Verify single auto-sell (not double)

PHASE 7: Full Workflow
☐ Reset portfolio
☐ Customize presets
☐ Buy → execute all auto-sells → reset
☐ Verify all configs cleared properly
```

---

## Common Issues & Troubleshooting

### Issue: Auto-sell config not created
```
Solution: 
1. Check bot.py Line 1354 - setup_auto_sell() is called after buy
2. Check data/auto_sell.json exists
3. Verify your user ID is in the file
```

### Issue: Custom presets not applying
```
Solution:
1. Check data/global_settings.json has as_presets_YOUR_UID
2. Verify get_user_as_presets() function at Line 180
3. Check next trade uses setup_auto_sell() at Line 1354
```

### Issue: Auto-sell not triggering
```
Solution:
1. Check price is actually hitting the target (verify with /price)
2. Check check_auto_sell() job runs every 15 seconds
3. Look for price fetch errors in logs
4. Verify position still exists in portfolio
```

### Issue: Stale entries not cleaning up
```
Solution:
1. Try manually selling token: /sell MINT 1000000
2. Wait 20 seconds for next check_auto_sell() job
3. Check logs for "[AUTO-SELL] Cleaned up stale entry" message
4. Verify auto_sell.json no longer has that mint
```

---

## Log Monitoring Commands

```bash
# Watch for auto-sell events
tail -f /tmp/bot_debug.log | grep -i "auto-sell\|triggered\|cleaned"

# Find all auto-sell messages in last hour
grep "Auto-Sell" /tmp/bot_debug.log | tail -20

# Check for errors
grep -i "error\|exception" /tmp/bot_debug.log | tail -10

# Monitor data file changes
watch -n 1 'ls -lah data/*.json'
```

---

## Success Criteria

✅ **All tests pass when:**

1. Reset clears auto-sell with portfolio
2. Custom presets persist across trades
3. UI buttons adjust values correctly
4. Auto-sell triggers at multiplier thresholds
5. Stale entries cleaned after position is sold
6. Breakeven stop doesn't cause double-sells
7. Full workflow completes without errors

**Total estimated test time: 30-45 minutes**

