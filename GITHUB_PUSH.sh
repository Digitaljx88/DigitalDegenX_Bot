#!/bin/bash

# Script to push updated repository to GitHub

echo "🚀 Preparing to push to GitHub..."
echo ""

# Check git status
echo "📋 Current git status:"
git status
echo ""

# Add all changes
echo "📝 Staging all changes..."
git add -A
echo "✅ Changes staged"
echo ""

# Show what will be committed
echo "📦 Files to be committed:"
git diff --cached --name-only
echo ""

# Commit with detailed message
echo "💾 Creating commit..."
git commit -m "feat: Implement auto-sell fixes and deployment guide

FEATURES:
- Phase 1: Auto-sell cleanup on wallet reset
- Phase 2: Customizable auto-sell multiplier presets (2x→50%, 4x→50%, etc)
- Phase 3: Custom auto-sell configuration UI with Telegram buttons
- Phase 4: Fixed breakeven stop logic (prevent double-sells)
- Phase 5: Stale entry safeguards and automatic cleanup

IMPROVEMENTS:
- get_user_as_presets() - Retrieve user's custom multiplier presets
- set_user_as_presets() - Save custom multiplier presets to global settings
- format_as_presets() - Format presets for display
- as_preset_callback() - New Telegram callback handler for preset customization
- Enhanced reset_portfolio() to clean auto-sell entries
- Added portfolio existence check in check_auto_sell() to prevent stale entries
- Fixed breakeven stop to not reset triggered flag (prevent double-sell)

DOCS:
- Created TEST_GUIDE.md with 7 comprehensive test phases
- Created DEPLOYMENT.md with complete VPS deployment instructions
- Included health checks, monitoring, backup strategies, and troubleshooting

TECHNICAL:
- Auto-sell configs now use user presets instead of hardcoded 2x→50%, 4x→50%
- Preset multipliers customizable via Telegram UI buttons
- Stale auto-sell entries automatically cleaned when token is sold
- All features tested with paper trading first

BREAKING CHANGES: None
MIGRATION: Existing auto-sell configs will migrate to default presets on next trade
"

echo ""
echo "✅ Commit created successfully"
echo ""

# Push to GitHub
echo "🔄 Pushing to GitHub..."
git push origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "✨ SUCCESS! Repository pushed to GitHub"
    echo ""
    echo "📊 View your changes:"
    echo "   https://github.com/Digitaljx88/DigitalDegenX_Bot/commits/main"
else
    echo ""
    echo "❌ Push failed. Check your GitHub credentials and try again."
    exit 1
fi
