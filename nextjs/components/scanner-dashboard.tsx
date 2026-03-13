"use client";

import { useEffect, useEffectEvent, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type ScannerFeedItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  alerted?: number;
  dq?: string;
  ts?: number;
};

type ScannerFeedResponse = {
  count: number;
  items: ScannerFeedItem[];
};

function formatMcap(value?: number) {
  if (!value) return "n/a";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

export function ScannerDashboard() {
  const searchParams = useSearchParams();
  const uid = Number(searchParams.get("uid") || 0);
  const [feed, setFeed] = useState<ScannerFeedItem[]>([]);
  const [lastUpdated, setLastUpdated] = useState<string>("never");
  const [error, setError] = useState<string>("");
  const [submittingMint, setSubmittingMint] = useState<string>("");
  const [buyAmount, setBuyAmount] = useState("0.1");

  const loadFeed = useEffectEvent(async () => {
    try {
      const data = await apiFetch<ScannerFeedResponse>("/scanner/feed", { query: { limit: 40 } });
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
  }, []);

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
          mode: "paper",
        }),
      });
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Buy failed");
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
            <input
              value={buyAmount}
              onChange={(event) => setBuyAmount(event.target.value)}
              className="w-24 rounded-full border border-white/10 bg-black/25 px-4 py-2 text-sm text-white outline-none"
            />
            <span className="text-sm text-[var(--muted-foreground)]">SOL quick buy</span>
          </div>
        </div>
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        <div className="overflow-hidden rounded-2xl border border-white/8">
          <table className="min-w-full divide-y divide-white/8 text-left text-sm">
            <thead className="bg-white/4 text-[var(--muted-foreground)]">
              <tr>
                <th className="px-4 py-3">Token</th>
                <th className="px-4 py-3">Score</th>
                <th className="px-4 py-3">MCap</th>
                <th className="px-4 py-3">Narrative</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/6">
              {newest.map((item) => (
                <tr key={`${item.mint}-${item.ts}`} className="bg-black/10">
                  <td className="px-4 py-3">
                    <div className="font-medium text-white">{item.symbol || item.name || item.mint.slice(0, 6)}</div>
                    <div className="text-xs text-[var(--muted-foreground)]">{item.mint}</div>
                  </td>
                  <td className="px-4 py-3 text-white">{item.score ?? 0}</td>
                  <td className="px-4 py-3 text-white">{formatMcap(item.mcap)}</td>
                  <td className="px-4 py-3 text-[var(--muted-foreground)]">{item.narrative || "Other"}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-3 py-1 text-xs font-medium ${item.dq ? "bg-red-500/20 text-red-200" : item.alerted ? "bg-emerald-500/20 text-emerald-200" : "bg-amber-500/20 text-amber-100"}`}>
                      {item.dq ? "Disqualified" : item.alerted ? "Alerted" : "Tracked"}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => quickBuy(item.mint)}
                      disabled={!uid || !!item.dq || submittingMint === item.mint}
                      className="rounded-full bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-[var(--accent-foreground)] disabled:opacity-50"
                    >
                      {submittingMint === item.mint ? "Buying..." : "Paper Buy"}
                    </button>
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
