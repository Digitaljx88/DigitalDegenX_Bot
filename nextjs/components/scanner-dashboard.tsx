"use client";

import Link from "next/link";
import { useEffect, useEffectEvent, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type ScannerFeedItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  source_primary?: string;
  strategy_profile?: string;
  state?: string;
  confidence?: number;
  age_mins?: number;
  buy_ratio_5m?: number;
  alerted?: number;
  dq?: string;
  ts?: number;
  autobuy_preview?: {
    eligible?: boolean;
    status?: string;
    block_reason?: string;
    block_category?: string;
    sol_amount?: number;
    confidence?: number;
    strategy_profile?: string;
  };
};

type ScannerFeedResponse = {
  count: number;
  items: ScannerFeedItem[];
};

type ModeResponse = {
  uid: number;
  mode: "paper" | "live";
};

function formatMcap(value?: number) {
  if (!value) return "n/a";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function formatAgeMins(value?: number) {
  if (!value || value < 0) return "new";
  if (value < 1) return "<1m";
  if (value >= 60) return `${(value / 60).toFixed(1)}h`;
  return `${Math.round(value)}m`;
}

function formatPct(value?: number) {
  if (value === undefined || value === null) return "n/a";
  return `${Math.round(value * 100)}%`;
}

export function ScannerDashboard() {
  const { uid } = useActiveUid();
  const [feed, setFeed] = useState<ScannerFeedItem[]>([]);
  const [lastUpdated, setLastUpdated] = useState<string>("never");
  const [error, setError] = useState<string>("");
  const [message, setMessage] = useState<string>("");
  const [submittingMint, setSubmittingMint] = useState<string>("");
  const [buyAmount, setBuyAmount] = useState("0.1");
  const [tradeMode, setTradeMode] = useState<"paper" | "live">("paper");

  const loadFeed = useEffectEvent(async () => {
    try {
      const data = await apiFetch<ScannerFeedResponse>("/scanner/feed", { query: { limit: 40, uid: uid || undefined } });
      setFeed(data.items || []);
      setLastUpdated(new Date().toLocaleTimeString());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load scanner feed");
    }
  });

  useEffect(() => {
    loadFeed();
    const timer = window.setInterval(() => loadFeed(), 5000);
    return () => window.clearInterval(timer);
  }, [uid]);

  useEffect(() => {
    async function loadMode() {
      if (!uid) {
        setTradeMode("paper");
        return;
      }
      try {
        const data = await apiFetch<ModeResponse>("/mode", { query: { uid } });
        setTradeMode(data.mode || "paper");
      } catch {
        setTradeMode("paper");
      }
    }
    void loadMode();
  }, [uid]);

  const newest = useMemo(() => feed.slice(0, 12), [feed]);

  async function quickBuy(mint: string) {
    if (!uid) {
      setError("Set your Telegram UID first to use quick buy.");
      return;
    }
    setSubmittingMint(mint);
    try {
      await apiFetch("/buy", {
        method: "POST",
        body: JSON.stringify({
          uid,
          mint,
          sol_amount: Number(buyAmount || 0),
          mode: tradeMode,
        }),
      });
      setError("");
      setMessage(`${tradeMode === "paper" ? "Paper" : "Live"} buy submitted for ${mint.slice(0, 8)}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Buy failed");
      setMessage("");
    } finally {
      setSubmittingMint("");
    }
  }

  return (
    <div className="space-y-6">
      <Panel title="Scanner Feed" subtitle={`Newest scored launches. Auto-refreshing every 5s. Last update ${lastUpdated}.`}>
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="text-sm text-[var(--muted-foreground)]">
            The dashboard prefers the freshest qualifying tokens and lets you paper-buy directly from the feed.
          </div>
          <div className="flex items-center gap-2">
            <select
              value={tradeMode}
              onChange={(event) => setTradeMode(event.target.value as "paper" | "live")}
              className="rounded-full border border-white/10 bg-black/25 px-4 py-2 text-sm text-white outline-none"
            >
              <option value="paper">Paper mode</option>
              <option value="live">Live mode</option>
            </select>
            <input
              value={buyAmount}
              onChange={(event) => setBuyAmount(event.target.value)}
              className="w-24 rounded-full border border-white/10 bg-black/25 px-4 py-2 text-sm text-white outline-none"
            />
            <span className="text-sm text-[var(--muted-foreground)]">SOL quick buy</span>
          </div>
        </div>
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        {message ? <div className="mb-4 rounded-2xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{message}</div> : null}
        <div className="overflow-hidden rounded-2xl border border-white/8">
          <table className="min-w-full divide-y divide-white/8 text-left text-sm">
            <thead className="bg-white/4 text-[var(--muted-foreground)]">
              <tr>
                <th className="px-4 py-3">Token</th>
                <th className="px-4 py-3">Score</th>
                <th className="px-4 py-3">MCap</th>
                <th className="px-4 py-3">Setup</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Auto-Buy</th>
                <th className="px-4 py-3">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/6">
              {newest.map((item) => (
                <tr key={`${item.mint}-${item.ts}`} className="bg-black/10">
                  <td className="px-4 py-3">
                    <div className="font-medium text-white">
                      <Link href={`/token/${item.mint}`} className="hover:text-[var(--accent)]">
                        {item.symbol || item.name || item.mint.slice(0, 6)}
                      </Link>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-2 text-[10px] uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
                      <span>{item.source_primary || "scanner"}</span>
                      <span>{formatAgeMins(item.age_mins)}</span>
                      {item.state ? <span>{item.state.replaceAll("_", " ")}</span> : null}
                    </div>
                    <div className="text-xs text-[var(--muted-foreground)]">{item.mint}</div>
                  </td>
                  <td className="px-4 py-3 text-white">{item.score ?? 0}</td>
                  <td className="px-4 py-3 text-white">{formatMcap(item.mcap)}</td>
                  <td className="px-4 py-3 text-[var(--muted-foreground)]">
                    <div>{item.narrative || "Other"}</div>
                    <div className="text-xs text-white/60">{item.strategy_profile || "unprofiled"}</div>
                    <div className="text-xs text-white/40">
                      conf {item.confidence ? item.confidence.toFixed(2) : "0.00"} · buy {formatPct(item.buy_ratio_5m)}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-3 py-1 text-xs font-medium ${item.dq ? "bg-red-500/20 text-red-200" : item.alerted ? "bg-emerald-500/20 text-emerald-200" : "bg-amber-500/20 text-amber-100"}`}>
                      {item.dq ? "Disqualified" : item.alerted ? "Alerted" : "Tracked"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {item.autobuy_preview ? (
                      <div className="space-y-1">
                        <span
                          className={`rounded-full px-3 py-1 font-medium ${
                            item.autobuy_preview.eligible
                              ? "bg-emerald-500/20 text-emerald-200"
                              : "bg-amber-500/20 text-amber-100"
                          }`}
                        >
                          {item.autobuy_preview.eligible ? "Would buy" : "Blocked"}
                        </span>
                        <div className="text-[var(--muted-foreground)]">
                          {Number(item.autobuy_preview.confidence || item.confidence || 0).toFixed(2)} ·{" "}
                          {Number(item.autobuy_preview.sol_amount || 0).toFixed(3)} SOL
                        </div>
                        {item.autobuy_preview.block_category ? (
                          <div className="text-white/60">{item.autobuy_preview.block_category.replaceAll("_", " ")}</div>
                        ) : null}
                        {!item.autobuy_preview.eligible && item.autobuy_preview.block_reason ? (
                          <div className="max-w-xs text-red-100">{item.autobuy_preview.block_reason}</div>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-[var(--muted-foreground)]">Set UID to preview</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Link
                        href={`/token/${item.mint}`}
                        className="rounded-full border border-white/10 px-3 py-1.5 text-xs font-medium text-white/80 hover:bg-white/8"
                      >
                        View
                      </Link>
                      <button
                        type="button"
                        onClick={() => quickBuy(item.mint)}
                        disabled={!uid || !!item.dq || submittingMint === item.mint}
                        className="rounded-full bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-[var(--accent-foreground)] disabled:opacity-50"
                      >
                        {submittingMint === item.mint ? "Buying..." : tradeMode === "paper" ? "Paper Buy" : "Live Buy"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
