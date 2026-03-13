"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type TradeRow = {
  ts?: number;
  action?: string;
  mode?: string;
  symbol?: string;
  mint?: string;
  sol_amount?: number;
  sol_received?: number;
  token_amount?: number;
  price_usd?: number;
  pnl_pct?: number;
};

type TradesResponse = {
  uid: number;
  count: number;
  trades: TradeRow[];
};

type TradeStatsResponse = {
  summary: {
    total_rows: number;
    closed_count: number;
    win_rate: number;
    realized_pnl_sol: number;
    paper_count: number;
    live_count: number;
  };
};

export function TradesDashboard() {
  const searchParams = useSearchParams();
  const uid = Number(searchParams.get("uid") || 0);
  const [filter, setFilter] = useState("all");
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [stats, setStats] = useState<TradeStatsResponse["summary"] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) {
        setTrades([]);
        setStats(null);
        return;
      }
      try {
        const [tradeData, statData] = await Promise.all([
          apiFetch<TradesResponse>("/trades", { query: { uid, limit: 30, filter_spec: filter } }),
          apiFetch<TradeStatsResponse>("/trades/stats", { query: { uid, filter_spec: filter } }),
        ]);
        setTrades(tradeData.trades || []);
        setStats(statData.summary);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load trades");
      }
    }
    load();
  }, [filter, uid]);

  if (!uid) {
    return (
      <Panel title="Trade Center" subtitle="Set your Telegram UID to load your ledger and closed-trade stats.">
        <div className="text-sm text-[var(--muted-foreground)]">
          Add your Telegram UID in the top bar to unlock your trade history, filters, and performance stats.
        </div>
      </Panel>
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-4">
        <Panel title="Trades" subtitle="Ledger rows">
          <div className="text-3xl font-semibold text-white">{stats?.total_rows ?? 0}</div>
        </Panel>
        <Panel title="Closed" subtitle="Realized exits">
          <div className="text-3xl font-semibold text-white">{stats?.closed_count ?? 0}</div>
        </Panel>
        <Panel title="Win Rate" subtitle="Closed trade ratio">
          <div className="text-3xl font-semibold text-white">{stats ? `${stats.win_rate.toFixed(0)}%` : "0%"}</div>
        </Panel>
        <Panel title="Realized P&L" subtitle="SOL">
          <div className="text-3xl font-semibold text-white">{stats?.realized_pnl_sol?.toFixed(4) ?? "0.0000"}</div>
        </Panel>
      </div>

      <Panel title="Trade Ledger" subtitle="Filter and review recent trade rows.">
        <div className="mb-4 flex flex-wrap gap-2">
          {["all", "wins", "losses", "buys", "sells", "paper", "live"].map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setFilter(option)}
              className={`rounded-full px-3 py-1.5 text-xs font-medium ${filter === option ? "bg-[var(--accent)] text-[var(--accent-foreground)]" : "border border-white/10 text-[var(--muted-foreground)]"}`}
            >
              {option}
            </button>
          ))}
        </div>
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        <div className="space-y-3">
          {trades.map((trade, idx) => (
            <div key={`${trade.mint}-${trade.ts}-${idx}`} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="flex items-center justify-between">
                <div className="font-medium text-white">
                  {trade.symbol || trade.mint?.slice(0, 6) || "Unknown"} · {String(trade.action || "").toUpperCase()}
                </div>
                <div className="text-xs text-[var(--muted-foreground)]">{trade.mode || "paper"}</div>
              </div>
              <div className="mt-2 grid gap-2 text-sm text-[var(--muted-foreground)] md:grid-cols-4">
                <div>Price: ${Number(trade.price_usd || 0).toFixed(8)}</div>
                <div>Tokens: {Number(trade.token_amount || 0).toLocaleString()}</div>
                <div>SOL: {Number(trade.sol_amount || trade.sol_received || 0).toFixed(4)}</div>
                <div>PnL: {trade.pnl_pct !== undefined && trade.pnl_pct !== null ? `${Number(trade.pnl_pct).toFixed(1)}%` : "n/a"}</div>
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
