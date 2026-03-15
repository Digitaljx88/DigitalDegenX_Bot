"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";
import { ExternalLinks } from "@/components/external-links";

/* ── types ── */
type ScannerItem = {
  mint: string; symbol?: string; name?: string;
  score?: number; mcap?: number; narrative?: string;
  strategy_profile?: string; confidence?: number;
  buy_ratio_5m?: number; alerted?: number; dq?: string; age_mins?: number;
};
type PortfolioPos = {
  mint: string; symbol?: string; name?: string;
  value_sol?: number | null; pnl_pct?: number | null; mcap?: number | null;
};
type TradeSummary = {
  total_rows: number; closed_count: number; win_rate: number;
  realized_pnl_sol: number; avg_giveback_pct: number; top_strategy: string;
};
type AutoBuyRow = {
  id: number; ts?: number; symbol?: string; name?: string; mint?: string;
  status: "executed" | "blocked" | "failed";
  block_category?: string; confidence?: number; sol_amount?: number;
};
type AutoBuyConfig = { enabled?: boolean; min_score?: number; sol_amount?: number };
type ModeRes = { mode: "paper" | "live" };

/* ── helpers ── */
function fmtMcap(v?: number | null) {
  if (!v) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}
function fmtAge(v?: number) {
  if (!v || v < 0) return "new";
  if (v < 1) return "<1m";
  if (v >= 60) return `${(v / 60).toFixed(1)}h`;
  return `${Math.round(v)}m`;
}
function fmtRelTime(ts?: number) {
  if (!ts) return "";
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  return `${(d / 3600).toFixed(1)}h ago`;
}
function scoreColor(score: number, dq?: string) {
  if (dq) return "text-red-400";
  if (score >= 75) return "text-emerald-400";
  if (score >= 60) return "text-amber-400";
  return "text-white/40";
}
function pnlColor(v?: number | null) {
  if (v == null) return "text-white/50";
  return v >= 0 ? "text-emerald-400" : "text-red-400";
}

/* ── stat card ── */
function Stat({
  label, value, sub, href, accent,
}: { label: string; value: string; sub?: string; href?: string; accent?: string }) {
  const inner = (
    <div className="rounded-xl border border-white/8 bg-[#080e14] px-4 py-3 hover:border-white/16 transition-colors">
      <div className="text-[10px] uppercase tracking-[0.18em] text-white/30">{label}</div>
      <div className={`mt-1 text-2xl font-bold tabular-nums ${accent ?? "text-white"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-white/30">{sub}</div>}
    </div>
  );
  return href ? <Link href={href}>{inner}</Link> : inner;
}

/* ── section header ── */
function SectionHead({ title, href }: { title: string; href: string }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/30">{title}</span>
      <Link href={href} className="text-[10px] text-white/25 hover:text-white/60">View all →</Link>
    </div>
  );
}

export function OverviewDashboard() {
  const { uid } = useActiveUid();

  const [scanFeed, setScanFeed] = useState<ScannerItem[]>([]);
  const [paperPositions, setPaperPositions] = useState<PortfolioPos[]>([]);
  const [paperSol, setPaperSol] = useState<number>(0);
  const [tradeSummary, setTradeSummary] = useState<TradeSummary | null>(null);
  const [recentAutobuy, setRecentAutobuy] = useState<AutoBuyRow[]>([]);
  const [autobuyConfig, setAutobuyConfig] = useState<AutoBuyConfig | null>(null);
  const [mode, setMode] = useState<"paper" | "live" | null>(null);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);

  useEffect(() => {
    function loadScanner() {
      apiFetch<{ items: ScannerItem[] }>("/scanner/feed", { query: { limit: 20 } })
        .then((d) => { setScanFeed(d.items || []); setApiOnline(true); })
        .catch(() => setApiOnline(false));
    }
    loadScanner();
    const t = setInterval(loadScanner, 30_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!uid) {
      setPaperPositions([]); setTradeSummary(null);
      setRecentAutobuy([]); setAutobuyConfig(null); setMode(null);
      return;
    }
    Promise.allSettled([
      apiFetch<{ paper?: { sol_balance: number; positions: PortfolioPos[] } }>("/portfolio", { query: { uid } }),
      apiFetch<{ summary: TradeSummary }>("/trades/stats", { query: { uid, filter_spec: "paper" } }),
      apiFetch<{ items: AutoBuyRow[] }>(`/autobuy/activity/${uid}`, { query: { limit: 6 } }),
      apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
      apiFetch<ModeRes>("/mode", { query: { uid } }),
    ]).then(([port, trade, ab, cfg, modeRes]) => {
      if (port.status === "fulfilled") {
        setPaperSol(port.value.paper?.sol_balance ?? 0);
        setPaperPositions(port.value.paper?.positions?.slice(0, 5) ?? []);
      }
      if (trade.status === "fulfilled") setTradeSummary(trade.value.summary);
      if (ab.status === "fulfilled") setRecentAutobuy(ab.value.items || []);
      if (cfg.status === "fulfilled") setAutobuyConfig(cfg.value);
      if (modeRes.status === "fulfilled") setMode(modeRes.value.mode);
    });
  }, [uid]);

  // Derived scanner stats
  const alerted = scanFeed.filter((i) => i.alerted && !i.dq);
  const tracked = scanFeed.filter((i) => !i.alerted && !i.dq);
  const topTokens = [...scanFeed].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 8);

  // Auto-buy activity counts
  const abExecuted = recentAutobuy.filter((r) => r.status === "executed").length;
  const abBlocked = recentAutobuy.filter((r) => r.status === "blocked").length;

  return (
    <div className="space-y-5">

      {/* ── Status strip ── */}
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-white/8 bg-[#080e14] px-4 py-3 text-xs">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${apiOnline === null ? "bg-white/20" : apiOnline ? "live-dot bg-emerald-400" : "bg-red-400"}`} />
          <span className="text-white/50">API</span>
          <span className={apiOnline === null ? "text-white/25" : apiOnline ? "text-emerald-300" : "text-red-300"}>
            {apiOnline === null ? "checking…" : apiOnline ? "Online" : "Offline"}
          </span>
        </div>
        <span className="text-white/10">│</span>
        {mode !== null ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-white/50">Mode</span>
              <span className={mode === "live" ? "font-semibold text-red-300" : "text-emerald-300"}>
                {mode === "live" ? "LIVE" : "Paper"}
              </span>
            </div>
            <span className="text-white/10">│</span>
          </>
        ) : null}
        {autobuyConfig !== null ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-white/50">Auto-Buy</span>
              <span className={autobuyConfig.enabled ? "text-emerald-300" : "text-white/30"}>
                {autobuyConfig.enabled ? "Enabled" : "Disabled"}
              </span>
              {autobuyConfig.min_score ? (
                <span className="text-white/25">min score {autobuyConfig.min_score}</span>
              ) : null}
            </div>
            <span className="text-white/10">│</span>
          </>
        ) : null}
        <div className="flex items-center gap-2">
          <span className="live-dot h-1.5 w-1.5 rounded-full bg-emerald-400" />
          <span className="text-white/50">Scanner</span>
          <span className="text-white/70">{scanFeed.length} tokens</span>
        </div>
        {!uid && (
          <>
            <span className="text-white/10">│</span>
            <span className="text-amber-300/60">Set UID in the top bar for portfolio & trade data</span>
          </>
        )}
      </div>

      {/* ── Top stat cards ── */}
      <div className="grid gap-3 grid-cols-2 md:grid-cols-3 xl:grid-cols-6">
        <Stat
          label="Tokens in Feed"
          value={String(scanFeed.length)}
          sub={`${alerted.length} alerted`}
          href="/scanner"
        />
        <Stat
          label="Top Score"
          value={topTokens[0] ? String(topTokens[0].score ?? 0) : "—"}
          sub={topTokens[0]?.symbol || topTokens[0]?.mint?.slice(0, 6) || "none"}
          href={topTokens[0] ? `/token/${topTokens[0].mint}` : "/scanner"}
          accent={topTokens[0] ? scoreColor(topTokens[0].score ?? 0, topTokens[0].dq) : undefined}
        />
        <Stat
          label="Open Positions"
          value={uid ? String(paperPositions.length) : "—"}
          sub={uid ? `${paperSol.toFixed(2)} SOL bal` : "set UID"}
          href="/portfolio"
        />
        <Stat
          label="Win Rate"
          value={tradeSummary ? `${tradeSummary.win_rate.toFixed(0)}%` : "—"}
          sub={tradeSummary ? `${tradeSummary.closed_count} closed` : "set UID"}
          href="/trades"
          accent={tradeSummary ? (tradeSummary.win_rate >= 50 ? "text-emerald-400" : "text-red-400") : undefined}
        />
        <Stat
          label="Realized P&L"
          value={tradeSummary ? `${tradeSummary.realized_pnl_sol >= 0 ? "+" : ""}${tradeSummary.realized_pnl_sol.toFixed(3)}` : "—"}
          sub="SOL"
          href="/trades"
          accent={tradeSummary ? pnlColor(tradeSummary.realized_pnl_sol) : undefined}
        />
        <Stat
          label="Auto-Buy (6h)"
          value={recentAutobuy.length ? `${abExecuted}/${recentAutobuy.length}` : "—"}
          sub={abBlocked ? `${abBlocked} blocked` : "no blocks"}
          href="/autobuy"
          accent={abExecuted > 0 ? "text-emerald-400" : undefined}
        />
      </div>

      {/* ── Middle: scanner + autobuy ── */}
      <div className="grid gap-4 xl:grid-cols-[1fr_320px]">

        {/* Top tokens */}
        <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
          <SectionHead title="Top Scoring Tokens" href="/scanner" />
          <table className="min-w-full text-xs">
            <thead>
              <tr className="border-b border-white/6 text-left text-[10px] uppercase tracking-[0.15em] text-white/25">
                <th className="px-4 py-2">Token</th>
                <th className="px-3 py-2">Score</th>
                <th className="px-3 py-2">MCap</th>
                <th className="px-3 py-2">Age</th>
                <th className="px-3 py-2">Setup</th>
                <th className="px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/4">
              {topTokens.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-white/25">
                    Scanner loading…
                  </td>
                </tr>
              ) : topTokens.map((item) => {
                const score = item.score ?? 0;
                return (
                  <tr key={item.mint} className="hover:bg-white/[0.025]">
                    <td className="px-4 py-2">
                      <Link href={`/token/${item.mint}`} className="font-semibold text-white hover:text-[var(--accent)]">
                        {item.symbol || item.name || item.mint.slice(0, 6)}
                      </Link>
                      <div className="text-[10px] text-white/25">{item.mint.slice(0, 10)}…</div>
                      <ExternalLinks mint={item.mint} className="mt-0.5" />
                    </td>
                    <td className="px-3 py-2">
                      <span className={`font-mono font-bold ${scoreColor(score, item.dq)}`}>{score}</span>
                    </td>
                    <td className="px-3 py-2 font-mono text-white/70">{fmtMcap(item.mcap)}</td>
                    <td className="px-3 py-2 text-white/40">{fmtAge(item.age_mins)}</td>
                    <td className="px-3 py-2">
                      <div className="text-white/70">{item.narrative || "Other"}</div>
                      <div className="text-[10px] text-white/30">{item.strategy_profile || "unprofiled"}</div>
                    </td>
                    <td className="px-3 py-2">
                      {item.dq ? (
                        <span className="text-red-400">DQ</span>
                      ) : item.alerted ? (
                        <span className="text-emerald-400">Alerted</span>
                      ) : (
                        <span className="text-amber-400/60">Tracked</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Right column: autobuy feed + portfolio */}
        <div className="flex flex-col gap-4">

          {/* Recent auto-buy */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <SectionHead title="Recent Auto-Buy" href="/autobuy" />
            {recentAutobuy.length === 0 ? (
              <div className="px-4 py-4 text-xs text-white/25">
                {uid ? "No recent auto-buy activity." : "Set UID to see auto-buy history."}
              </div>
            ) : (
              <div className="divide-y divide-white/4">
                {recentAutobuy.map((row) => (
                  <div key={row.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
                    <div>
                      <div className="text-xs font-medium text-white">
                        {row.symbol || row.name || row.mint?.slice(0, 8) || "Unknown"}
                      </div>
                      <div className="text-[10px] text-white/30">
                        {row.block_category?.replaceAll("_", " ") || (row.status === "executed" ? `${Number(row.sol_amount || 0).toFixed(3)} SOL` : "")}
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-0.5">
                      <span className={`text-xs font-medium ${row.status === "executed" ? "text-emerald-300" : row.status === "failed" ? "text-red-300" : "text-white/30"}`}>
                        {row.status}
                      </span>
                      <span className="text-[10px] text-white/20">{fmtRelTime(row.ts)}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Open positions */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <SectionHead title="Open Positions" href="/portfolio" />
            {paperPositions.length === 0 ? (
              <div className="px-4 py-4 text-xs text-white/25">
                {uid ? "No open positions." : "Set UID to see portfolio."}
              </div>
            ) : (
              <div className="divide-y divide-white/4">
                {paperPositions.map((pos) => (
                  <div key={pos.mint} className="flex items-center justify-between gap-3 px-4 py-2.5">
                    <div>
                      <Link href={`/token/${pos.mint}`} className="text-xs font-medium text-white hover:text-[var(--accent)]">
                        {pos.symbol || pos.name || pos.mint.slice(0, 8)}
                      </Link>
                      <div className="text-[10px] text-white/30">{fmtMcap(pos.mcap)}</div>
                    </div>
                    <div className="flex flex-col items-end gap-0.5">
                      {pos.value_sol != null && (
                        <span className="font-mono text-xs text-white/70">
                          {Number(pos.value_sol).toFixed(3)} SOL
                        </span>
                      )}
                      {pos.pnl_pct != null && (
                        <span className={`font-mono text-[10px] ${pnlColor(pos.pnl_pct)}`}>
                          {pos.pnl_pct >= 0 ? "+" : ""}{pos.pnl_pct.toFixed(1)}%
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Bottom: trade summary + narrative breakdown ── */}
      {tradeSummary && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {[
            { label: "Best Strategy", value: tradeSummary.top_strategy || "None", sub: "by closed P&L" },
            { label: "Give-Back Avg", value: `${tradeSummary.avg_giveback_pct.toFixed(1)}%`, sub: "lower = better exits" },
            { label: "Paper Trades", value: String(tradeSummary.total_rows), sub: "total ledger rows" },
            { label: "Closed Trades", value: String(tradeSummary.closed_count), sub: `${tradeSummary.win_rate.toFixed(0)}% win rate` },
          ].map((s) => (
            <Link key={s.label} href="/trades">
              <div className="rounded-xl border border-white/8 bg-[#080e14] px-4 py-3 hover:border-white/16">
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/30">{s.label}</div>
                <div className="mt-1 text-lg font-bold text-white">{s.value}</div>
                <div className="mt-0.5 text-[10px] text-white/25">{s.sub}</div>
              </div>
            </Link>
          ))}
        </div>
      )}
      {uid && !tradeSummary && (
        <div className="rounded-xl border border-white/6 bg-[#080e14] px-4 py-5 text-center text-xs text-white/25">
          No closed trades yet — stats appear once your first paper or live trade closes.
        </div>
      )}

    </div>
  );
}
