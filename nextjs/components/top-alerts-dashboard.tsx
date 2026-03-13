"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type AlertItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  archetype?: string;
};

type TopAlertsResponse = {
  count: number;
  alerts: AlertItem[];
};

function formatMcap(value?: number) {
  if (!value) return "n/a";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

export function TopAlertsDashboard() {
  const [items, setItems] = useState<AlertItem[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch<TopAlertsResponse>("/scanner/top", { query: { limit: 20 } });
        setItems(response.alerts || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load top alerts");
      }
    }
    void load();
  }, []);

  return (
    <Panel title="Top Alerts" subtitle="Best-scoring scanner alerts from today, ranked by current scanner score.">
      {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
      <div className="space-y-3">
        {items.map((item, index) => (
          <div key={`${item.mint}-${index}`} className="rounded-2xl border border-white/8 bg-black/10 p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="font-medium text-white">{index + 1}. {item.symbol || item.name || item.mint.slice(0, 8)}</div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">{item.mint}</div>
              </div>
              <div className="rounded-full bg-emerald-500/20 px-3 py-1 text-xs text-emerald-200">{item.score ?? 0}</div>
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
