from __future__ import annotations

import db


def main():
    db.init()
    removed = db.cleanup_invalid_wallet_alerts()
    print(f"Removed {removed} invalid wallet alert rows.")


if __name__ == "__main__":
    main()
