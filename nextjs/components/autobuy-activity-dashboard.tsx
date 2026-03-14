"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type AutoBuyActivityRow = {
  id: number;
  ts: number;
  mint?: string;
  symbol?: string;
  name?: string;
  score?: number;
  effective_score?: number;
  mcap?: number;
  strategy_profile?: string;
  confidence?: number;
  sol_amount?: number;
  size_multiplier?: number;
  mode?: string;
  status: "executed" | "blocked" | "failed";
  block_reason?: string;
  block_category?: string;
  source?: string;
  narrative?: string;
  archetype?: string;
};

type AutoBuyActivityResponse = {
  uid: number;
  count: number;
  latest: AutoBuyActivityRow | null;
  summary?: {
    window_hours: number;
    total: number;
    status_counts: Record<string, number>;
    blocked_by_category: Record<string, number>;
    top_block_category?: string;
    avg_confidence?: number;
    avg_size_sol?: number;
  };
  items: AutoBuyActivityRow[];
};

function formatMcap(value?: number) {
  if (!value) return "n/a";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function prettify(value?: string) {
  return (value || "all").replaceAll("_", " ");
}

export function AutoBuyActivityDashboard() {
  const { uid } = useActiveUid();
  const [data, setData] = useState<AutoBuyActivityResponse | null>(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [blockerFilter, setBlockerFilter] = useState("all");
  const [strategyFilter, setStrategyFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) {
        setData(null);
        return;
      }
      try {
        const response = await apiFetch<AutoBuyActivityResponse>(`/autobuy/activity/${uid}`, {
          query: { limit: 200 },
        });
        setData(response);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load auto-buy activity");
      }
    }
    void load();
  }, [uid]);

  const filteredItems = useMemo(() => {
    const items = data?.items || [];
    return items.filter((item) => {
      if (statusFilter !== "all" && item.status !== statusFilter) return false;
      if (blockerFilter !== "all" && (item.block_category || "none") !== blockerFilter) return false;
      if (strategyFilter !== "all" && (item.strategy_profile || "none") !== strategyFilter) return false;
      if (sourceFilter !== "all" && (item.source || "none") !== sourceFilter) return false;
      return true;
    });
  }, [data, statusFilter, blockerFilter, strategyFilter, sourceFilter]);

  const blockerOptions = useMemo(() => {
    return Array.from(new Set((data?.items || []).map((item) => item.block_category || "none"))).sort();
  }, [data]);

  const strategyOptions = useMemo(() => {
    return Array.from(new Set((data?.items || []).map((item) => item.strategy_profile || "none"))).sort();
  }, [data]);

  const sourceOptions = useMemo(() => {
    return Array.from(new Set((data?.items || []).map((item) => item.source || "none"))).sort();
  }, [data]);

  if (!uid) {
    return (
      <Panel title="Auto-Buy Activity" subtitle="Bind your Telegram UID to inspect auto-buy execution and blocker trends.">
        <div className="text-sm text-[var(--muted-foreground)]">
          Set your Telegram UID in the top bar to inspect auto-buy attempts, blocker categories, and execution history.
        </div>
      </Panel>
    );
  }

  return (
    <div className="space-y-6">
      <Panel title="Auto-Buy Activity" subtitle="Recent execution history plus blocker trends from the real scanner-driven auto-buy path.">
        {error ? <div className="rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        {!data && !error ? <div className="text-sm text-[var(--muted-foreground)]">Loading auto-buy activity...</div> : null}
        {data ? (
          <div className="space-y-5">
            <div className="grid gap-3 md:grid-cols-4">
              <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-xs text-[var(--muted-foreground)]">
                Executed
                <div className="mt-1 text-lg font-semibold text-white">{data.summary?.status_counts.executed || 0}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-xs text-[var(--muted-foreground)]">
                Blocked
                <div className="mt-1 text-lg font-semibold text-white">{data.summary?.status_counts.blocked || 0}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-xs text-[var(--muted-foreground)]">
                Top blocker
                <div className="mt-1 text-sm font-semibold text-white">{prettify(data.summary?.top_block_category)}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-xs text-[var(--muted-foreground)]">
                Avg size
                <div className="mt-1 text-lg font-semibold text-white">{Number(data.summary?.avg_size_sol || 0).toFixed(3)} SOL</div>
              </div>
            </div>

            <div className="grid gap-3 md:grid-cols-4">
              <select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              >
                <option value="all">All statuses</option>
                <option value="executed">Executed</option>
                <option value="blocked">Blocked</option>
                <option value="failed">Failed</option>
              </select>
              <select
                value={blockerFilter}
                onChange={(event) => setBlockerFilter(event.target.value)}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              >
                <option value="all">All blockers</option>
                {blockerOptions.map((value) => (
                  <option key={value} value={value}>
                    {prettify(value)}
                  </option>
                ))}
              </select>
              <select
                value={strategyFilter}
                onChange={(event) => setStrategyFilter(event.target.value)}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              >
                <option value="all">All strategies</option>
                {strategyOptions.map((value) => (
                  <option key={value} value={value}>
                    {prettify(value)}
                  </option>
                ))}
              </select>
              <select
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value)}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              >
                <option value="all">All sources</option>
                {sourceOptions.map((value) => (
                  <option key={value} value={value}>
                    {prettify(value)}
                  </option>
                ))}
              </select>
            </div>

            <div className="space-y-3">
              {filteredItems.length ? (
                filteredItems.map((item) => (
                  <div key={item.id} className="rounded-2xl border border-white/10 bg-black/20 px-4 py-4 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <div className="font-medium text-white">
                          <Link href={`/token/${item.mint || ""}`} className="hover:text-[var(--accent)]">
                            {item.symbol || item.name || item.mint || "Unknown token"}
                          </Link>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-2 text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">
                          <span>{item.strategy_profile || "none"}</span>
                          <span>{item.source || "none"}</span>
                          <span>{item.mode || "paper"}</span>
                          {item.block_category ? <span>{prettify(item.block_category)}</span> : null}
                        </div>
                      </div>
                      <div className="text-xs text-[var(--muted-foreground)]">{new Date((item.ts || 0) * 1000).toLocaleString()}</div>
                    </div>
                    <div className="mt-3 grid gap-3 md:grid-cols-5 text-xs text-[var(--muted-foreground)]">
                      <div>Status: <span className="text-white">{item.status}</span></div>
                      <div>Score: <span className="text-white">{item.effective_score ?? item.score ?? 0}</span></div>
                      <div>Confidence: <span className="text-white">{Number(item.confidence || 0).toFixed(2)}</span></div>
                      <div>Size: <span className="text-white">{Number(item.sol_amount || 0).toFixed(3)} SOL</span></div>
                      <div>MCap: <span className="text-white">{formatMcap(item.mcap)}</span></div>
                    </div>
                    {item.block_reason ? (
                      <div className="mt-3 rounded-2xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-100">
                        {item.block_reason}
                      </div>
                    ) : null}
                  </div>
                ))
              ) : (
                <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-[var(--muted-foreground)]">
                  No auto-buy activity matches the current filters.
                </div>
              )}
            </div>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}
