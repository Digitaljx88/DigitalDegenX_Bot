"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type WatchToken = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  archetype?: string;
};

type WatchlistResponse = {
  count: number;
  tokens: WatchToken[];
};

function formatMcap(value?: number) {
  if (!value) return "n/a";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

export function WatchlistDashboard() {
  const [items, setItems] = useState<WatchToken[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch<WatchlistResponse>("/scanner/watchlist");
        setItems(response.tokens || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load watchlist");
      }
    }
    void load();
  }, []);

  return (
    <Panel title="Watchlist" subtitle="Scouted tokens that are worth watching but not strong enough for full alerts yet.">
      {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
      <div className="grid gap-3 lg:grid-cols-2">
        {items.map((item) => (
          <div key={item.mint} className="rounded-2xl border border-white/8 bg-black/10 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-medium text-white">{item.symbol || item.name || item.mint.slice(0, 8)}</div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">{item.mint}</div>
              </div>
              <div className="rounded-full bg-amber-500/20 px-3 py-1 text-xs text-amber-100">{item.score ?? 0}</div>
            </div>
            <div className="mt-4 grid gap-2 text-sm text-[var(--muted-foreground)] md:grid-cols-3">
              <div>Narrative: {item.narrative || "Other"}</div>
              <div>MCap: {formatMcap(item.mcap)}</div>
              <div>Archetype: {item.archetype || "None"}</div>
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}
