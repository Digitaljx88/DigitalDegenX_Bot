"use client";

import { useEffect, useState } from "react";
import { Tooltip } from "@/components/tooltip";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

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

type TradesResponse = { uid: number; count: number; trades: TradeRow[] };

type TradeStatsResponse = {
  summary: {
    total_rows: number; closed_count: number; win_rate: number; realized_pnl_sol: number;
    paper_count: number; live_count: number; avg_giveback_pct: number; avg_peak_unrealized_pct: number;
    best_exit_reason: string; top_source: string; top_strategy: string; top_archetype: string; top_narrative: string;
  };
  cohorts: {
    by_exit_reason: CohortRow[]; by_source: CohortRow[]; by_strategy: CohortRow[];
    by_score_band: CohortRow[]; by_age_band: CohortRow[]; by_narrative: CohortRow[]; by_archetype: CohortRow[];
  };
};

type CohortRow = {
  label: string; count: number; win_rate: number; realized_pnl_sol: number;
  avg_giveback_pct: number | null; avg_peak_unrealized_pct: number | null;
};

type Cohorts = {
  by_exit_reason: CohortRow[]; by_source: CohortRow[]; by_strategy: CohortRow[];
  by_score_band: CohortRow[]; by_age_band: CohortRow[]; by_narrative: CohortRow[]; by_archetype: CohortRow[];
};

type WeeklyReportResponse = {
  window_days: number;
  summary: { window_days: number; closed_count: number; win_rate: number; realized_pnl_sol: number; avg_giveback_pct: number; avg_peak_unrealized_pct: number };
  leaders: { strategy: CohortRow | null; source: CohortRow | null; score_band: CohortRow | null; age_band: CohortRow | null; exit_reason: CohortRow | null; narrative: CohortRow | null; archetype: CohortRow | null };
  cohorts: Cohorts;
  insights: string[];
};

const PAGE_SIZE = 30;

function SCard({ label, value, sub, color, tip }: { label: string; value: string; sub?: string; color?: string; tip?: string }) {
  return (
    <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)", letterSpacing: "-0.02em", color: color || "var(--foreground)" }}>{value}</div>
        {tip && <Tooltip text={tip} />}
      </div>
      {sub && <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

function CohortPanel({ title, rows }: { title: string; rows: CohortRow[] }) {
  if (!rows.length) return null;
  return (
    <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
      <div style={{ padding: "12px 18px", background: "var(--bg2)", borderBottom: "1px solid var(--border)" }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>{title}</span>
      </div>
      <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 8 }}>
        {rows.slice(0, 4).map((row, idx) => (
          <div key={row.label} style={{ padding: "10px 12px", borderRadius: 8, background: idx === 0 ? "rgba(249,115,22,0.06)" : "var(--bg2)", border: `1px solid ${idx === 0 ? "rgba(249,115,22,0.2)" : "var(--border)"}` }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>{row.label}</span>
                {idx === 0 && <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", padding: "1px 6px", borderRadius: 4, background: "rgba(249,115,22,0.15)", color: "var(--accent)" }}>Best</span>}
              </div>
              <span style={{ fontSize: 10, color: "var(--t3)" }}>{row.count} trades</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 4 }}>
              {[
                { label: "Win Rate", value: `${row.win_rate.toFixed(0)}%`, color: row.win_rate >= 60 ? "var(--green)" : row.win_rate >= 45 ? "var(--yellow)" : "var(--red)" },
                { label: "Realized", value: `${row.realized_pnl_sol.toFixed(4)} SOL`, color: "var(--foreground)" },
                { label: "Give-Back", value: row.avg_giveback_pct != null ? `${row.avg_giveback_pct.toFixed(1)}%` : "n/a", color: row.avg_giveback_pct != null ? (row.avg_giveback_pct <= 20 ? "var(--green)" : row.avg_giveback_pct <= 40 ? "var(--yellow)" : "var(--red)") : "var(--t3)" },
              ].map((s) => (
                <div key={s.label}>
                  <div style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{s.label}</div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: s.color, fontFamily: "var(--font-mono, monospace)", marginTop: 1 }}>{s.value}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TradesDashboard() {
  const { uid } = useActiveUid();
  const [filter, setFilter] = useState<string | null>(null);
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [stats, setStats] = useState<TradeStatsResponse["summary"] | null>(null);
  const [weekly, setWeekly] = useState<WeeklyReportResponse | null>(null);
  const [cohorts, setCohorts] = useState<Cohorts>({ by_exit_reason: [], by_source: [], by_strategy: [], by_score_band: [], by_age_band: [], by_narrative: [], by_archetype: [] });
  const [error, setError] = useState("");

  useEffect(() => {
    if (!uid) { setFilter("paper"); return; }
    apiFetch<{ mode: string }>("/mode", { query: { uid } })
      .then((r) => setFilter(r.mode === "live" ? "live" : "paper"))
      .catch(() => setFilter("paper"));
  }, [uid]);

  useEffect(() => {
    if (filter === null) return;
    async function load() {
      if (!uid) { setTrades([]); setOffset(0); setHasMore(false); setStats(null); setWeekly(null); setCohorts({ by_exit_reason: [], by_source: [], by_strategy: [], by_score_band: [], by_age_band: [], by_narrative: [], by_archetype: [] }); return; }
      try {
        const fs = filter ?? undefined;
        const [tradeData, statData, weeklyData] = await Promise.all([
          apiFetch<TradesResponse>("/trades", { query: { uid, limit: PAGE_SIZE, offset: 0, filter_spec: fs } }),
          apiFetch<TradeStatsResponse>("/trades/stats", { query: { uid, filter_spec: fs } }),
          apiFetch<WeeklyReportResponse>("/trades/weekly-report", { query: { uid, filter_spec: fs, days: 7 } }),
        ]);
        setTrades(tradeData.trades || []);
        setOffset(PAGE_SIZE);
        setHasMore((tradeData.trades || []).length === PAGE_SIZE);
        setStats(statData.summary);
        setWeekly(weeklyData);
        setCohorts({ by_exit_reason: statData.cohorts?.by_exit_reason || [], by_source: statData.cohorts?.by_source || [], by_strategy: statData.cohorts?.by_strategy || [], by_score_band: statData.cohorts?.by_score_band || [], by_age_band: statData.cohorts?.by_age_band || [], by_narrative: statData.cohorts?.by_narrative || [], by_archetype: statData.cohorts?.by_archetype || [] });
        setError("");
      } catch (err) { setError(err instanceof Error ? err.message : "Failed to load trades"); }
    }
    load();
  }, [filter, uid]);

  async function loadMore() {
    if (!uid || loadingMore) return;
    setLoadingMore(true);
    try {
      const tradeData = await apiFetch<TradesResponse>("/trades", { query: { uid, limit: PAGE_SIZE, offset, filter_spec: filter ?? undefined } });
      const newRows = tradeData.trades || [];
      setTrades((prev) => [...prev, ...newRows]);
      setOffset((prev) => prev + PAGE_SIZE);
      setHasMore(newRows.length === PAGE_SIZE);
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to load more"); }
    finally { setLoadingMore(false); }
  }

  if (!uid) {
    return (
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, padding: 40, textAlign: "center" }}>
        <div style={{ fontSize: 13, color: "var(--t3)" }}>Set your Telegram UID in the top bar to view trades.</div>
      </div>
    );
  }

  const filterOptions = ["all", "wins", "losses", "buys", "sells", "paper", "live"];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
        <SCard label="Trades" value={String(stats?.total_rows ?? 0)} sub="ledger rows" />
        <SCard label="Closed" value={String(stats?.closed_count ?? 0)} sub="realized exits" />
        <SCard label="Win Rate" value={stats ? `${stats.win_rate.toFixed(0)}%` : "—"} color={stats ? (stats.win_rate >= 50 ? "var(--green)" : "var(--red)") : undefined} sub={stats ? `${stats.closed_count} closed` : undefined} />
        <SCard label="Realized P&L" value={stats?.realized_pnl_sol != null ? `${stats.realized_pnl_sol >= 0 ? "+" : ""}${stats.realized_pnl_sol.toFixed(4)}` : "—"} sub="SOL" color={stats ? (stats.realized_pnl_sol >= 0 ? "var(--green)" : "var(--red)") : undefined} />
        <SCard label="Avg Give-Back" value={stats ? `${stats.avg_giveback_pct.toFixed(1)}%` : "—"} sub="peak to exit" tip="The percentage of peak unrealized gain surrendered before closing. Lower = better exits." />
        <SCard label="Peak Unrealized" value={stats ? `${stats.avg_peak_unrealized_pct.toFixed(1)}%` : "—"} sub="before exit" tip="Highest unrealized gain reached before closing. High peak + low give-back = excellent timing." />
      </div>

      {/* Top performers */}
      {stats && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
          {[
            { label: "Top Strategy", value: stats.top_strategy || "None" },
            { label: "Top Source", value: stats.top_source || "None" },
            { label: "Top Archetype", value: stats.top_archetype || "None" },
            { label: "Top Narrative", value: stats.top_narrative || "None" },
          ].map((s) => (
            <div key={s.label} style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "12px 14px" }}>
              <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 5 }}>{s.label}</div>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)" }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Weekly report */}
      {weekly?.summary.closed_count ? (
        <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
          <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)" }}>
            <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>7-Day Optimization Report</h2>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 10 }}>
              {[
                { label: "Closed", value: String(weekly.summary.closed_count) },
                { label: "Win Rate", value: `${weekly.summary.win_rate.toFixed(0)}%` },
                { label: "Realized", value: `${weekly.summary.realized_pnl_sol.toFixed(4)} SOL` },
                { label: "Give-Back", value: `${weekly.summary.avg_giveback_pct.toFixed(1)}%` },
                { label: "Best Strategy", value: weekly.leaders.strategy?.label || "None" },
                { label: "Best Source", value: weekly.leaders.source?.label || "None" },
              ].map((s) => (
                <div key={s.label} style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px" }}>
                  <div style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{s.label}</div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "var(--foreground)", fontFamily: "var(--font-mono, monospace)" }}>{s.value}</div>
                </div>
              ))}
            </div>
            {weekly.insights.length > 0 && (
              <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 8, padding: "12px 14px" }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)", marginBottom: 8 }}>What worked this week</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {weekly.insights.map((insight) => (
                    <div key={insight} style={{ fontSize: 11, color: "var(--t2)" }}>• {insight}</div>
                  ))}
                </div>
              </div>
            )}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <CohortPanel title="Weekly Strategy Leaders" rows={weekly.cohorts.by_strategy} />
              <CohortPanel title="Weekly Source Leaders" rows={weekly.cohorts.by_source} />
              <CohortPanel title="Weekly Score Bands" rows={weekly.cohorts.by_score_band} />
              <CohortPanel title="Weekly Age Bands" rows={weekly.cohorts.by_age_band} />
            </div>
          </div>
        </div>
      ) : null}

      {/* Exit performance */}
      {cohorts.by_exit_reason.length > 0 && (
        <CohortPanel title={`Exit Performance · Best: ${stats?.best_exit_reason || "None"}`} rows={cohorts.by_exit_reason} />
      )}

      {/* Cohort grids */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <CohortPanel title="Strategy Cohorts" rows={cohorts.by_strategy} />
        <CohortPanel title="Source Cohorts" rows={cohorts.by_source} />
        <CohortPanel title="Score Bands" rows={cohorts.by_score_band} />
        <CohortPanel title="Age Bands" rows={cohorts.by_age_band} />
      </div>

      {/* Trade Ledger */}
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>Trade Ledger</h2>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {filterOptions.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setFilter(option)}
                style={{
                  fontSize: 11, fontWeight: 500, padding: "4px 10px", borderRadius: 6, cursor: "pointer",
                  background: filter === option ? "var(--accent)" : "transparent",
                  color: filter === option ? "#fff" : "var(--t3)",
                  border: `1px solid ${filter === option ? "var(--accent)" : "var(--border)"}`,
                }}
              >
                {option}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div style={{ margin: 16, padding: "10px 14px", background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, fontSize: 12, color: "var(--red)" }}>
            {error}
          </div>
        )}

        <div>
          {trades.map((trade, idx) => {
            const isBuy = String(trade.action).toLowerCase() === "buy";
            const pnl = trade.pnl_pct != null ? Number(trade.pnl_pct) : null;
            return (
              <div key={`${trade.mint}-${trade.ts}-${idx}`}
                style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", alignItems: "center", gap: 12, padding: "10px 20px", borderBottom: "1px solid var(--border)" }}
                className="hover:bg-white/[0.02] transition-colors"
              >
                {/* Action badge */}
                <div style={{ padding: "4px 8px", borderRadius: 6, fontSize: 10, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", background: isBuy ? "rgba(34,211,160,0.12)" : "rgba(244,63,94,0.12)", color: isBuy ? "var(--green)" : "var(--red)" }}>
                  {trade.action || "—"}
                </div>

                {/* Token + details */}
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>
                    {trade.symbol || trade.mint?.slice(0, 6) || "Unknown"}
                    <span style={{ marginLeft: 6, fontSize: 10, color: "var(--t3)", fontWeight: 400 }}>{trade.mode || "paper"}</span>
                  </div>
                  <div style={{ display: "flex", gap: 10, marginTop: 2, fontSize: 11, color: "var(--t3)", fontFamily: "var(--font-mono, monospace)" }}>
                    <span>${Number(trade.price_usd || 0).toFixed(8)}</span>
                    <span>{Number(trade.token_amount || 0).toLocaleString()} tokens</span>
                    <span>{Number(trade.sol_amount || trade.sol_received || 0).toFixed(4)} SOL</span>
                  </div>
                </div>

                {/* PnL */}
                {pnl != null ? (
                  <div style={{ fontSize: 12, fontWeight: 700, fontFamily: "var(--font-mono, monospace)", color: pnl >= 0 ? "var(--green)" : "var(--red)" }}>
                    {pnl >= 0 ? "+" : ""}{pnl.toFixed(1)}%
                  </div>
                ) : <div />}
              </div>
            );
          })}
        </div>

        {hasMore && (
          <div style={{ padding: 16, textAlign: "center" }}>
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              style={{ fontSize: 12, padding: "8px 20px", borderRadius: 8, background: "var(--bg2)", border: "1px solid var(--border)", color: "var(--t2)", cursor: "pointer" }}
              className="hover:border-[var(--border2)] disabled:opacity-50"
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
