"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type ResearchRow = {
  date?: string;
  action?: string;
  symbol?: string;
  narrative?: string;
  entry_strategy?: string;
  pnl_pct?: number;
  giveback_pct?: number;
};

type ResearchResponse = {
  count: number;
  items: ResearchRow[];
  csv_filename?: string;
};

type HistoryRow = {
  symbol?: string;
  mint?: string;
  pnl_pct?: number;
  pnl_sol?: number;
  hold_s?: number;
  exit_reason?: string;
};

type HistoryResponse = {
  count: number;
  closed_trades: HistoryRow[];
};

export function ResearchDashboard() {
  const { uid } = useActiveUid();
  const [research, setResearch] = useState<ResearchResponse | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) return;
      try {
        const [researchRes, historyRes] = await Promise.all([
          apiFetch<ResearchResponse>("/research-log", { query: { limit: 25 } }),
          apiFetch<HistoryResponse>("/history", { query: { uid, limit: 25 } }),
        ]);
        setResearch(researchRes);
        setHistory(historyRes);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load research data");
      }
    }
    void load();
  }, [uid]);

  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <Panel title="History" subtitle="Recent closed trades with realized outcomes and exit reasons.">
        {!uid ? <div className="text-sm text-[var(--muted-foreground)]">Set your Telegram UID to load closed-trade history.</div> : null}
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        <div className="space-y-3">
          {(history?.closed_trades || []).map((row, index) => (
            <div key={`${row.mint}-${index}`} className="rounded-2xl border border-white/8 bg-black/10 p-4 text-sm">
              <div className="font-medium text-white">{row.symbol || row.mint?.slice(0, 8) || "Unknown"}</div>
              <div className="mt-2 grid gap-2 text-[var(--muted-foreground)] md:grid-cols-3">
                <div>PnL: {(row.pnl_pct ?? 0).toFixed(1)}%</div>
                <div>SOL: {(row.pnl_sol ?? 0).toFixed(4)}</div>
                <div>Exit: {row.exit_reason || "n/a"}</div>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Research Log" subtitle={`Recent research rows. CSV export file: ${research?.csv_filename || "n/a"}`}>
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        <div className="space-y-3">
          {(research?.items || []).map((row, index) => (
            <div key={`${row.date}-${row.symbol}-${index}`} className="rounded-2xl border border-white/8 bg-black/10 p-4 text-sm">
              <div className="font-medium text-white">{row.symbol || "Unknown"} · {row.action || "trade"}</div>
              <div className="mt-2 grid gap-2 text-[var(--muted-foreground)] md:grid-cols-3">
                <div>{row.date || "n/a"}</div>
                <div>{row.entry_strategy || "strategy n/a"}</div>
                <div>Give-back: {(row.giveback_pct ?? 0).toFixed(1)}%</div>
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
