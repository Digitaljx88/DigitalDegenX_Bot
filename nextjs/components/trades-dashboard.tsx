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
    avg_giveback_pct: number;
    avg_peak_unrealized_pct: number;
    best_exit_reason: string;
    top_source: string;
    top_strategy: string;
    top_archetype: string;
    top_narrative: string;
  };
  cohorts: {
    by_exit_reason: CohortRow[];
    by_source: CohortRow[];
    by_strategy: CohortRow[];
    by_score_band: CohortRow[];
    by_age_band: CohortRow[];
    by_narrative: CohortRow[];
    by_archetype: CohortRow[];
  };
};

type CohortRow = {
  label: string;
  count: number;
  win_rate: number;
  realized_pnl_sol: number;
  avg_giveback_pct: number | null;
  avg_peak_unrealized_pct: number | null;
};

type Cohorts = {
  by_exit_reason: CohortRow[];
  by_source: CohortRow[];
  by_strategy: CohortRow[];
  by_score_band: CohortRow[];
  by_age_band: CohortRow[];
  by_narrative: CohortRow[];
  by_archetype: CohortRow[];
};

type WeeklyReportResponse = {
  window_days: number;
  summary: {
    window_days: number;
    closed_count: number;
    win_rate: number;
    realized_pnl_sol: number;
    avg_giveback_pct: number;
    avg_peak_unrealized_pct: number;
  };
  leaders: {
    strategy: CohortRow | null;
    source: CohortRow | null;
    score_band: CohortRow | null;
    age_band: CohortRow | null;
    exit_reason: CohortRow | null;
    narrative: CohortRow | null;
    archetype: CohortRow | null;
  };
  cohorts: Cohorts;
  insights: string[];
};

function CohortList({
  title,
  subtitle,
  rows,
}: {
  title: string;
  subtitle: string;
  rows: CohortRow[];
}) {
  return (
    <Panel title={title} subtitle={subtitle}>
      {rows.length ? (
        <div className="space-y-3">
          {rows.slice(0, 4).map((row) => (
            <div key={row.label} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="flex items-center justify-between">
                <div className="font-medium text-white">{row.label}</div>
                <div className="text-xs text-[var(--muted-foreground)]">{row.count} trades</div>
              </div>
              <div className="mt-2 grid gap-2 text-sm text-[var(--muted-foreground)] md:grid-cols-3">
                <div>Win Rate: {row.win_rate.toFixed(0)}%</div>
                <div>Realized: {row.realized_pnl_sol.toFixed(4)} SOL</div>
                <div>
                  Give-Back: {row.avg_giveback_pct !== null ? `${row.avg_giveback_pct.toFixed(1)}%` : "n/a"}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-sm text-[var(--muted-foreground)]">No closed-trade cohorts yet.</div>
      )}
    </Panel>
  );
}

export function TradesDashboard() {
  const searchParams = useSearchParams();
  const uid = Number(searchParams.get("uid") || 0);
  const [filter, setFilter] = useState("all");
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [stats, setStats] = useState<TradeStatsResponse["summary"] | null>(null);
  const [weekly, setWeekly] = useState<WeeklyReportResponse | null>(null);
  const [cohorts, setCohorts] = useState<Cohorts>({
    by_exit_reason: [],
    by_source: [],
    by_strategy: [],
    by_score_band: [],
    by_age_band: [],
    by_narrative: [],
    by_archetype: [],
  });
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) {
        setTrades([]);
        setStats(null);
        setWeekly(null);
        setCohorts({
          by_exit_reason: [],
          by_source: [],
          by_strategy: [],
          by_score_band: [],
          by_age_band: [],
          by_narrative: [],
          by_archetype: [],
        });
        return;
      }
      try {
        const [tradeData, statData, weeklyData] = await Promise.all([
          apiFetch<TradesResponse>("/trades", { query: { uid, limit: 30, filter_spec: filter } }),
          apiFetch<TradeStatsResponse>("/trades/stats", { query: { uid, filter_spec: filter } }),
          apiFetch<WeeklyReportResponse>("/trades/weekly-report", { query: { uid, filter_spec: filter, days: 7 } }),
        ]);
        setTrades(tradeData.trades || []);
        setStats(statData.summary);
        setWeekly(weeklyData);
        setCohorts({
          by_exit_reason: statData.cohorts?.by_exit_reason || [],
          by_source: statData.cohorts?.by_source || [],
          by_strategy: statData.cohorts?.by_strategy || [],
          by_score_band: statData.cohorts?.by_score_band || [],
          by_age_band: statData.cohorts?.by_age_band || [],
          by_narrative: statData.cohorts?.by_narrative || [],
          by_archetype: statData.cohorts?.by_archetype || [],
        });
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
      <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
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
        <Panel title="Avg Give-Back" subtitle="Peak to exit">
          <div className="text-3xl font-semibold text-white">
            {stats ? `${stats.avg_giveback_pct.toFixed(1)}%` : "0.0%"}
          </div>
        </Panel>
        <Panel title="Peak Unrealized" subtitle="Before exit">
          <div className="text-3xl font-semibold text-white">
            {stats ? `${stats.avg_peak_unrealized_pct.toFixed(1)}%` : "0.0%"}
          </div>
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-4">
        <Panel title="Top Strategy" subtitle="Best closed cohort">
          <div className="text-xl font-semibold text-white">{stats?.top_strategy || "None"}</div>
        </Panel>
        <Panel title="Top Source" subtitle="Best discovery path">
          <div className="text-xl font-semibold text-white">{stats?.top_source || "None"}</div>
        </Panel>
        <Panel title="Top Archetype" subtitle="Most productive pattern">
          <div className="text-xl font-semibold text-white">{stats?.top_archetype || "None"}</div>
        </Panel>
        <Panel title="Top Narrative" subtitle="Most active theme">
          <div className="text-xl font-semibold text-white">{stats?.top_narrative || "None"}</div>
        </Panel>
      </div>

      <Panel title="Weekly Optimization Report" subtitle="Realized performance over the last 7 days.">
        {weekly?.summary.closed_count ? (
          <div className="space-y-5">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Closed</div>
                <div className="mt-2 text-2xl font-semibold text-white">{weekly.summary.closed_count}</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Win Rate</div>
                <div className="mt-2 text-2xl font-semibold text-white">{weekly.summary.win_rate.toFixed(0)}%</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Realized</div>
                <div className="mt-2 text-2xl font-semibold text-white">{weekly.summary.realized_pnl_sol.toFixed(4)} SOL</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Give-Back</div>
                <div className="mt-2 text-2xl font-semibold text-white">{weekly.summary.avg_giveback_pct.toFixed(1)}%</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Best Strategy</div>
                <div className="mt-2 text-lg font-semibold text-white">{weekly.leaders.strategy?.label || "None"}</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Best Source</div>
                <div className="mt-2 text-lg font-semibold text-white">{weekly.leaders.source?.label || "None"}</div>
              </div>
            </div>

            {weekly.insights.length ? (
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="mb-3 text-sm font-medium text-white">What worked this week</div>
                <div className="space-y-2 text-sm text-[var(--muted-foreground)]">
                  {weekly.insights.map((insight) => (
                    <div key={insight}>{insight}</div>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="grid gap-4 xl:grid-cols-2">
              <CohortList
                title="Weekly Strategy Leaders"
                subtitle="Best playbooks over the current window."
                rows={weekly.cohorts.by_strategy}
              />
              <CohortList
                title="Weekly Source Leaders"
                subtitle="Discovery sources with real closed P&L this week."
                rows={weekly.cohorts.by_source}
              />
              <CohortList
                title="Weekly Score Bands"
                subtitle="Which entry score ranges are paying right now."
                rows={weekly.cohorts.by_score_band}
              />
              <CohortList
                title="Weekly Age Bands"
                subtitle="Which freshness windows are actually converting."
                rows={weekly.cohorts.by_age_band}
              />
            </div>
          </div>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">
            No closed trades in the last 7 days yet, so the optimization report has nothing to rank.
          </div>
        )}
      </Panel>

      <Panel title="Exit Performance" subtitle="Which exits are protecting profit best.">
        {cohorts.by_exit_reason.length ? (
          <div className="space-y-3">
            <div className="text-sm text-[var(--muted-foreground)]">
              Best exit right now: <span className="font-medium text-white">{stats?.best_exit_reason || "None"}</span>
            </div>
            {cohorts.by_exit_reason.slice(0, 4).map((row) => (
              <div key={row.label} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="flex items-center justify-between">
                  <div className="font-medium text-white">{row.label}</div>
                  <div className="text-xs text-[var(--muted-foreground)]">{row.count} exits</div>
                </div>
                <div className="mt-2 grid gap-2 text-sm text-[var(--muted-foreground)] md:grid-cols-4">
                  <div>Win Rate: {row.win_rate.toFixed(0)}%</div>
                  <div>Realized: {row.realized_pnl_sol.toFixed(4)} SOL</div>
                  <div>Give-Back: {row.avg_giveback_pct !== null ? `${row.avg_giveback_pct.toFixed(1)}%` : "n/a"}</div>
                  <div>Peak: {row.avg_peak_unrealized_pct !== null ? `${row.avg_peak_unrealized_pct.toFixed(1)}%` : "n/a"}</div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">
            Exit analytics will appear after your first closed trades are attributed.
          </div>
        )}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-2">
        <CohortList
          title="Strategy Cohorts"
          subtitle="How each playbook is performing."
          rows={cohorts.by_strategy}
        />
        <CohortList
          title="Source Cohorts"
          subtitle="Which discovery sources are actually paying."
          rows={cohorts.by_source}
        />
        <CohortList
          title="Score Bands"
          subtitle="Expected value by entry score."
          rows={cohorts.by_score_band}
        />
        <CohortList
          title="Age Bands"
          subtitle="Performance by token freshness."
          rows={cohorts.by_age_band}
        />
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
