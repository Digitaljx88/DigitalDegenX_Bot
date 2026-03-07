"""
Research Logger — Dual CSV+JSON trading history for data analysis.
Logs every trade (buy/sell) with full metadata: CA, symbol, narrative, heat_score, prices, PnL.
Auto-rotates CSV files when they exceed 50k lines.
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

RESEARCH_LOG_CSV = os.path.join(DATA_DIR, "research_log.csv")
RESEARCH_LOG_JSON = os.path.join(DATA_DIR, "research_log.json")
CSV_MAX_LINES = 50_000


def _rotate_csv_if_needed():
    """Archive current CSV if it exceeds CSV_MAX_LINES, start fresh."""
    if not os.path.exists(RESEARCH_LOG_CSV):
        return
    
    with open(RESEARCH_LOG_CSV, "r") as f:
        line_count = sum(1 for _ in f.readlines())
    
    if line_count > CSV_MAX_LINES:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        archive_name = f"research_log_{timestamp}.csv"
        archive_path = os.path.join(DATA_DIR, archive_name)
        os.rename(RESEARCH_LOG_CSV, archive_path)
        print(f"[ResearchLogger] Archived CSV to {archive_name}")


def log_trade(
    timestamp: float,
    user_id: int,
    action: str,  # "buy" or "sell"
    mint: str,
    symbol: str,
    narrative: str,
    heat_score: int,
    buy_price_usd: float,
    sell_price_usd: float,
    sol_amount: float,
    token_amount: int,
    pnl_usd: float = None,
    pnl_pct: float = None,
    hold_seconds: int = None,
    mcap_at_entry: float = None,
    mcap_at_exit: float = None,
):
    """
    Log a trade to both CSV and JSON research logs.
    
    Args:
        timestamp: Unix timestamp of trade
        user_id: Telegram user ID
        action: "buy" or "sell"
        mint: Token contract address
        symbol: Token symbol (e.g., "BONK")
        narrative: Token narrative/category (e.g., "AI", "Animal", "Political")
        heat_score: Token heat score (0-100)
        buy_price_usd: Entry price in USD
        sell_price_usd: Current/exit price in USD (None for buys)
        sol_amount: SOL spent or received
        token_amount: Raw token amount transacted
        pnl_usd: Profit/loss in USD (for sells only)
        pnl_pct: Profit/loss percentage (for sells only)
        hold_seconds: Seconds held (for sells only)
        mcap_at_entry: Market cap at purchase time
        mcap_at_exit: Market cap at sale time
    """
    
    date_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    
    record = {
        "timestamp": timestamp,
        "date": date_str,
        "user_id": user_id,
        "action": action,
        "mint": mint,
        "symbol": symbol,
        "narrative": narrative,
        "heat_score": heat_score,
        "buy_price_usd": buy_price_usd,
        "sell_price_usd": sell_price_usd or 0.0,
        "sol_amount": sol_amount,
        "token_amount": token_amount,
        "pnl_usd": pnl_usd or 0.0,
        "pnl_pct": pnl_pct or 0.0,
        "hold_seconds": hold_seconds or 0,
        "mcap_at_entry": mcap_at_entry or 0.0,
        "mcap_at_exit": mcap_at_exit or 0.0,
    }
    
    # Write to CSV
    _write_csv(record)
    
    # Write to JSON
    _write_json(record)


def _write_csv(record: dict):
    """Append record to CSV, rotating if necessary."""
    _rotate_csv_if_needed()
    
    file_exists = os.path.exists(RESEARCH_LOG_CSV)
    
    try:
        with open(RESEARCH_LOG_CSV, "a", newline="") as f:
            fieldnames = [
                "timestamp", "date", "user_id", "action", "mint", "symbol",
                "narrative", "heat_score", "buy_price_usd", "sell_price_usd",
                "sol_amount", "token_amount", "pnl_usd", "pnl_pct",
                "hold_seconds", "mcap_at_entry", "mcap_at_exit"
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        print(f"[ResearchLogger] CSV write error: {e}")


def _write_json(record: dict):
    """Append record to JSON array."""
    records = []
    if os.path.exists(RESEARCH_LOG_JSON):
        try:
            with open(RESEARCH_LOG_JSON, "r") as f:
                records = json.load(f)
        except Exception as e:
            print(f"[ResearchLogger] JSON read error: {e}")
            records = []
    
    records.append(record)
    
    try:
        with open(RESEARCH_LOG_JSON, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        print(f"[ResearchLogger] JSON write error: {e}")


def load_research_log_json() -> list:
    """Load all research log records from JSON."""
    if not os.path.exists(RESEARCH_LOG_JSON):
        return []
    try:
        with open(RESEARCH_LOG_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return []


def export_csv_path() -> str:
    """Return full path to current research log CSV for download."""
    return RESEARCH_LOG_CSV
