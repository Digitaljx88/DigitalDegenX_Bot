#!/bin/bash

# Script to push updated repository to GitHub

set -e

echo "🚀 Preparing to push to GitHub..."
echo ""

echo "📋 Current git status:"
git status
echo ""

echo "📝 Staging all changes..."
git add -A

CHANGED=$(git diff --cached --name-only)
if [ -z "$CHANGED" ]; then
    echo "⚠️  No changes to commit."
else
    echo "📦 Files to be committed:"
    echo "$CHANGED"
    echo ""

    read -p "💬 Commit message: " COMMIT_MSG
    if [ -z "$COMMIT_MSG" ]; then
        COMMIT_MSG="update: latest changes $(date +%Y-%m-%d)"
    fi

    echo "💾 Creating commit..."
    git commit -m "$COMMIT_MSG"
    echo "✅ Commit created"
fi
echo ""

echo "🔄 Pushing to GitHub..."
git push origin main

echo ""
echo "✨ SUCCESS! Repository pushed to GitHub"
echo "📊 https://github.com/Digitaljx88/DigitalDegenX_Bot/commits/main"
