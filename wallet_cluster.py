"""
Wallet Cluster Analysis — Phase 4.

Builds a co-investment relationship graph: which wallets frequently enter the
same tokens within a tight time window?  A cluster of high-reputation wallets
co-buying together is a strong signal.

Key concepts:
  - Co-investment edge: wallet A and wallet B both entered token X within
    CO_INVEST_WINDOW_SECS of each other → share an edge with weight +1.
  - Cluster score: sum of reputation scores of all wallets in the cluster,
    normalised to 0-100, boosted by how many edges (co-investments) exist.
  - Heat boost: if 2+ tracked wallets co-invest on a live alert → +5/+10/+15
    modifier added to calculate_heat_score().

Persistence:
  - data/cluster_graph.json — edge list, updated as new entries are recorded
  - data/cluster_log.json   — last 200 cluster events for a token

TTL: graph edges older than EDGE_TTL_SECS are pruned on each write.
"""

import json
import os
import time

DATA_DIR           = os.path.join(os.path.dirname(__file__), "data")
GRAPH_FILE         = os.path.join(DATA_DIR, "cluster_graph.json")
CLUSTER_LOG_FILE   = os.path.join(DATA_DIR, "cluster_log.json")
os.makedirs(DATA_DIR, exist_ok=True)

CO_INVEST_WINDOW_SECS = 120   # two wallets buying within 2 minutes = co-invest
EDGE_TTL_SECS         = 604800  # prune edges older than 7 days
MAX_CLUSTER_LOG       = 200


# ─── Graph persistence ────────────────────────────────────────────────────────

def _load_graph() -> dict:
    """
    Graph structure:
    {
      "edges": {
        "walletA::walletB": {"count": 3, "last_ts": 1234567890, "tokens": [...]}
      },
      "tokens": {
        "mintXYZ": [{"wallet": "...", "ts": 123456789}, ...]
      }
    }
    """
    try:
        with open(GRAPH_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"edges": {}, "tokens": {}}


def _save_graph(g: dict):
    # Prune stale edges before saving
    cutoff = time.time() - EDGE_TTL_SECS
    g["edges"] = {k: v for k, v in g["edges"].items() if v.get("last_ts", 0) >= cutoff}
    with open(GRAPH_FILE, "w") as f:
        json.dump(g, f, indent=2)


def _load_cluster_log() -> list:
    try:
        with open(CLUSTER_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_cluster_log(entry: dict):
    log = _load_cluster_log()
    log.append(entry)
    if len(log) > MAX_CLUSTER_LOG:
        log = log[-MAX_CLUSTER_LOG:]
    with open(CLUSTER_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


# ─── Edge key helpers ─────────────────────────────────────────────────────────

def _edge_key(a: str, b: str) -> str:
    """Canonical (sorted) edge key so A::B == B::A."""
    return "::".join(sorted([a, b]))


# ─── Graph update ─────────────────────────────────────────────────────────────

def record_token_entries(token_mint: str, entries: list):
    """
    Record that a list of wallets entered `token_mint`.

    entries: list of {"wallet": address, "ts": unix_timestamp}

    For every pair of wallets that entered within CO_INVEST_WINDOW_SECS,
    increment their edge weight in the graph.
    """
    if not entries or len(entries) < 2:
        return

    g = _load_graph()

    # Store per-token entry list (keep last 50 entries per token)
    token_entries = g["tokens"].setdefault(token_mint, [])
    for e in entries:
        token_entries.append({"wallet": e["wallet"], "ts": e["ts"]})
    g["tokens"][token_mint] = token_entries[-50:]

    # Build co-investment edges for wallets within the time window
    now = time.time()
    valid = [e for e in entries if now - e.get("ts", 0) <= CO_INVEST_WINDOW_SECS * 10]

    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            a, b = valid[i], valid[j]
            dt = abs(a.get("ts", 0) - b.get("ts", 0))
            if dt <= CO_INVEST_WINDOW_SECS:
                key = _edge_key(a["wallet"], b["wallet"])
                edge = g["edges"].setdefault(key, {"count": 0, "last_ts": 0, "tokens": []})
                edge["count"] += 1
                edge["last_ts"] = now
                if token_mint not in edge["tokens"]:
                    edge["tokens"].append(token_mint)
                if len(edge["tokens"]) > 20:
                    edge["tokens"] = edge["tokens"][-20:]

    _save_graph(g)


# ─── Cluster analysis ─────────────────────────────────────────────────────────

def get_wallet_neighbors(wallet: str) -> list:
    """
    Return all wallets that have co-invested with `wallet`, sorted by edge weight.

    Returns list of {"wallet": addr, "co_investments": N, "last_ts": ts}
    """
    g   = _load_graph()
    out = []
    for key, edge in g["edges"].items():
        parts = key.split("::")
        if len(parts) != 2:
            continue
        a, b = parts
        if a == wallet:
            out.append({"wallet": b, "co_investments": edge["count"], "last_ts": edge["last_ts"]})
        elif b == wallet:
            out.append({"wallet": a, "co_investments": edge["count"], "last_ts": edge["last_ts"]})
    out.sort(key=lambda x: -x["co_investments"])
    return out


def score_cluster_strength(token_mint: str, wallet_entries: list) -> dict:
    """
    Given a list of wallet entries for a token, compute the cluster strength.

    wallet_entries: [{"wallet": addr, "ts": unix, "reputation": 0-100}, ...]

    Returns:
    {
      "cluster_score":  0-100,    # overall signal strength
      "boost":          int,      # heat score boost (+0/+5/+10/+15)
      "reason":         str,
      "co_invest_pairs": int,     # how many wallet pairs co-invested
      "cluster_wallets": [addr],  # wallets forming the strongest cluster
      "rep_weighted":   float,    # sum of reputations, normalised
    }
    """
    if not wallet_entries:
        return {"cluster_score": 0, "boost": 0, "reason": "No wallet entries", "co_invest_pairs": 0, "cluster_wallets": [], "rep_weighted": 0.0}

    try:
        import wallet_tracker
    except ImportError:
        wallet_tracker = None

    # Resolve reputations if not provided
    enriched = []
    for e in wallet_entries:
        rep = e.get("reputation", 0)
        if rep == 0 and wallet_tracker:
            perf = wallet_tracker.get_wallet_performance(e["wallet"])
            rep  = perf.get("reputation_score", 50)
        enriched.append({**e, "reputation": rep})

    g = _load_graph()

    # Count co-investment pairs (entries within CO_INVEST_WINDOW_SECS)
    pairs_found = []
    for i in range(len(enriched)):
        for j in range(i + 1, len(enriched)):
            a, b = enriched[i], enriched[j]
            dt = abs(a.get("ts", 0) - b.get("ts", 0))
            if dt <= CO_INVEST_WINDOW_SECS:
                key = _edge_key(a["wallet"], b["wallet"])
                historical_count = g["edges"].get(key, {}).get("count", 0)
                pairs_found.append({
                    "wallets": (a["wallet"], b["wallet"]),
                    "rep_sum": a["reputation"] + b["reputation"],
                    "historical_co": historical_count,
                })

    if not pairs_found:
        single_rep = max((e["reputation"] for e in enriched), default=0)
        single_reason = "Single tracked wallet, no co-investment signal"
        boost = 3 if single_rep >= 70 else 0
        return {
            "cluster_score":   round(single_rep * 0.4),
            "boost":           boost,
            "reason":          single_reason,
            "co_invest_pairs": 0,
            "cluster_wallets": [enriched[0]["wallet"]] if enriched else [],
            "rep_weighted":    single_rep,
        }

    # Reputation-weighted cluster score
    total_rep   = sum(e["reputation"] for e in enriched)
    avg_rep     = total_rep / len(enriched)
    pair_count  = len(pairs_found)
    best_pair   = max(pairs_found, key=lambda p: p["rep_sum"] + p["historical_co"] * 5)
    hist_bonus  = min(20, best_pair["historical_co"] * 4)  # Up to +20 for repeat co-investors

    # 0-100 cluster score
    raw_score  = min(100, (avg_rep * 0.5) + (pair_count * 10) + hist_bonus)
    cluster_score = round(raw_score)

    # Determine boost level for heat score
    if cluster_score >= 70 and pair_count >= 3:
        boost = 15
        reason = f"🔥 Strong cluster: {pair_count} wallet pairs, avg rep {avg_rep:.0f}, {hist_bonus}pt history"
    elif cluster_score >= 50 and pair_count >= 2:
        boost = 10
        reason = f"🟠 Cluster signal: {pair_count} co-invest pairs, avg rep {avg_rep:.0f}"
    elif cluster_score >= 30 or pair_count >= 1:
        boost = 5
        reason = f"🟡 Weak cluster: {pair_count} pair(s), avg rep {avg_rep:.0f}"
    else:
        boost = 0
        reason = "No cluster pattern"

    # Collect wallet addresses in the cluster
    cluster_wallets = list({w for p in pairs_found for w in p["wallets"]})

    # Log event
    _append_cluster_log({
        "ts":           time.time(),
        "mint":         token_mint,
        "cluster_score": cluster_score,
        "boost":        boost,
        "pair_count":   pair_count,
        "wallets":      cluster_wallets,
    })

    return {
        "cluster_score":   cluster_score,
        "boost":           boost,
        "reason":          reason,
        "co_invest_pairs": pair_count,
        "cluster_wallets": cluster_wallets,
        "rep_weighted":    round(avg_rep, 1),
    }


# ─── Token cluster map ────────────────────────────────────────────────────────

def get_token_cluster_map(token_mint: str) -> dict:
    """
    Return full cluster detail for a specific token.

    Reads recorded entries for token_mint from the graph, then:
      - Lists all wallet pairs that co-invested
      - Edge weight (total historical co-investment count)
      - Per-wallet edge degree (how many co-investors)

    Returns:
    {
      "mint": str,
      "wallets": [{"wallet": addr, "ts": ts, "degree": N}],
      "edges": [{"a": addr, "b": addr, "count": N, "historical": N}],
      "total_wallets": int,
      "total_edges": int,
      "cluster_score": int,
    }
    """
    g             = _load_graph()
    token_entries = g["tokens"].get(token_mint, [])

    if not token_entries:
        return {
            "mint":          token_mint,
            "wallets":       [],
            "edges":         [],
            "total_wallets": 0,
            "total_edges":   0,
            "cluster_score": 0,
        }

    # Build adjacency for this token
    wallet_set   = set(e["wallet"] for e in token_entries)
    edge_list    = []
    degree_count = {w: 0 for w in wallet_set}

    for i, ei in enumerate(token_entries):
        for j, ej in enumerate(token_entries):
            if j <= i:
                continue
            dt = abs(ei.get("ts", 0) - ej.get("ts", 0))
            if dt <= CO_INVEST_WINDOW_SECS:
                key  = _edge_key(ei["wallet"], ej["wallet"])
                hist = g["edges"].get(key, {}).get("count", 0)
                edge_list.append({
                    "a":          ei["wallet"],
                    "b":          ej["wallet"],
                    "dt_secs":    round(dt),
                    "historical": hist,
                })
                degree_count[ei["wallet"]] = degree_count.get(ei["wallet"], 0) + 1
                degree_count[ej["wallet"]] = degree_count.get(ej["wallet"], 0) + 1

    wallets_out = sorted(
        [{"wallet": w, "ts": next((e["ts"] for e in token_entries if e["wallet"] == w), 0),
          "degree": degree_count.get(w, 0)} for w in wallet_set],
        key=lambda x: -x["degree"]
    )

    # Quick cluster score for display
    cluster_entries = [{"wallet": e["wallet"], "ts": e["ts"], "reputation": 50} for e in token_entries]
    cs = score_cluster_strength(token_mint, cluster_entries)

    return {
        "mint":          token_mint,
        "wallets":       wallets_out,
        "edges":         edge_list,
        "total_wallets": len(wallet_set),
        "total_edges":   len(edge_list),
        "cluster_score": cs["cluster_score"],
        "boost":         cs["boost"],
        "reason":        cs["reason"],
    }


def get_cluster_log(limit: int = 20) -> list:
    """Return the last `limit` cluster events, newest first."""
    log = _load_cluster_log()
    return sorted(log, key=lambda x: -x.get("ts", 0))[:limit]


def get_global_top_clusters(limit: int = 10) -> list:
    """
    Return the top wallet pairs by co-investment count across all time.
    Useful for /clustertop command.
    """
    g   = _load_graph()
    out = []
    for key, edge in g["edges"].items():
        parts = key.split("::")
        if len(parts) == 2:
            out.append({
                "wallet_a":  parts[0],
                "wallet_b":  parts[1],
                "co_investments": edge["count"],
                "last_ts":   edge["last_ts"],
                "tokens":    edge.get("tokens", []),
            })
    out.sort(key=lambda x: -x["co_investments"])
    return out[:limit]
