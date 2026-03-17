"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type ScannerItem = {
  mint: string; symbol?: string; name?: string;
  score?: number; mcap?: number; alerted?: number; dq?: string; age_mins?: number;
  narrative?: string; strategy_profile?: string; confidence?: number;
};
type PortfolioPos = { mint: string; symbol?: string; name?: string; value_sol?: number | null; pnl_pct?: number | null };
type TradeSummary = { total_rows: number; closed_count: number; win_rate: number; realized_pnl_sol: number; avg_giveback_pct: number; top_strategy: string };
type AutoBuyRow = { id: number; ts?: number; symbol?: string; name?: string; mint?: string; status: "executed" | "blocked" | "failed"; block_category?: string; sol_amount?: number };
type AutoBuyConfig = { enabled?: boolean; min_score?: number; sol_amount?: number };
type ModeRes = { mode: "paper" | "live" };

function fmtSol(v?: number | null) { return v != null ? `${Number(v).toFixed(3)} SOL` : "—"; }
function fmtRelTime(ts?: number) {
  if (!ts) return "";
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  return `${(d / 3600).toFixed(1)}h ago`;
}

function StatCard({ label, value, sub, change, changeUp, href, valueColor }: {
  label: string; value: string; sub?: string;
  change?: string; changeUp?: boolean; href?: string; valueColor?: string;
}) {
  const inner = (
    <div style={{
      background: "var(--bg2)", border: "1px solid var(--border)",
      borderRadius: 10, padding: "14px 16px",
    }}
    className="hover:border-[var(--border2)] transition-colors"
    >
      <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)", letterSpacing: "-0.02em", color: valueColor || "var(--foreground)" }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 3 }}>{sub}</div>}
      {change && (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 3,
          fontSize: 10, fontWeight: 600, marginTop: 4, padding: "2px 6px", borderRadius: 4,
          background: changeUp ? "rgba(34,211,160,0.1)" : "rgba(244,63,94,0.1)",
          color: changeUp ? "var(--green)" : "var(--red)",
        }}>
          {changeUp ? "↑" : "↓"} {change}
        </span>
      )}
    </div>
  );
  return href ? <Link href={href}>{inner}</Link> : inner;
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
      apiFetch<{ items: ScannerItem[] }>("/scanner/feed", { query: { limit: 40 } })
        .then((d) => { setScanFeed(d.items || []); setApiOnline(true); })
        .catch(() => setApiOnline(false));
    }
    loadScanner();
    const t = setInterval(loadScanner, 30_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!uid) return;
    Promise.allSettled([
      apiFetch<{ paper?: { sol_balance: number; positions: PortfolioPos[] } }>("/portfolio", { query: { uid } }),
      apiFetch<{ summary: TradeSummary }>("/trades/stats", { query: { uid, filter_spec: "paper" } }),
      apiFetch<{ items: AutoBuyRow[] }>(`/autobuy/activity/${uid}`, { query: { limit: 6 } }),
      apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
      apiFetch<ModeRes>("/mode", { query: { uid } }),
    ]).then(([port, trade, ab, cfg, modeRes]) => {
      if (port.status === "fulfilled") { setPaperSol(port.value.paper?.sol_balance ?? 0); setPaperPositions(port.value.paper?.positions?.slice(0, 5) ?? []); }
      if (trade.status === "fulfilled") setTradeSummary(trade.value.summary);
      if (ab.status === "fulfilled") setRecentAutobuy(ab.value.items || []);
      if (cfg.status === "fulfilled") setAutobuyConfig(cfg.value);
      if (modeRes.status === "fulfilled") setMode(modeRes.value.mode);
    });
  }, [uid]);

  const alerted = scanFeed.filter((i) => i.alerted && !i.dq);
  const topToken = [...scanFeed].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];
  const abExecuted = recentAutobuy.filter((r) => r.status === "executed").length;
  const avgScore = scanFeed.length ? (scanFeed.reduce((s, i) => s + (i.score ?? 0), 0) / scanFeed.length) : 0;

  // Score distribution buckets
  const dist = useMemo(() => {
    const b = { s0: 0, s40: 0, s50: 0, s60: 0, s70: 0 };
    scanFeed.forEach((i) => {
      const s = i.score ?? 0;
      if (s >= 70) b.s70++;
      else if (s >= 60) b.s60++;
      else if (s >= 50) b.s50++;
      else if (s >= 40) b.s40++;
      else b.s0++;
    });
    return b;
  }, [scanFeed]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* ── Stats grid ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
        <StatCard
          label="Realized P&L"
          value={tradeSummary ? `${tradeSummary.realized_pnl_sol >= 0 ? "+" : ""}${tradeSummary.realized_pnl_sol.toFixed(3)}` : "—"}
          sub="SOL"
          href="/trades"
          valueColor={tradeSummary ? (tradeSummary.realized_pnl_sol >= 0 ? "var(--green)" : "var(--red)") : undefined}
          change={tradeSummary ? `${tradeSummary.win_rate.toFixed(0)}% win rate` : undefined}
          changeUp={tradeSummary ? tradeSummary.win_rate >= 50 : undefined}
        />
        <StatCard label="Active Positions" value={uid ? String(paperPositions.length) : "—"} sub={uid ? fmtSol(paperSol) + " bal" : "set UID"} href="/portfolio" />
        <StatCard label="Tokens Scanned" value={String(scanFeed.length)} sub={`${alerted.length} alerted · ${scanFeed.length - alerted.length} tracked`} href="/scanner" />
        <StatCard
          label="Win Rate"
          value={tradeSummary ? `${tradeSummary.win_rate.toFixed(0)}%` : "—"}
          sub={tradeSummary ? `${tradeSummary.closed_count} closed` : "set UID"}
          href="/trades"
          valueColor={tradeSummary ? (tradeSummary.win_rate >= 50 ? "var(--yellow)" : "var(--red)") : undefined}
        />
        <StatCard label="Paper SOL" value={uid ? paperSol.toFixed(3) : "—"} sub="balance" href="/portfolio" valueColor="var(--blue)" />
        <StatCard
          label="Top Score Today"
          value={topToken ? String(topToken.score ?? 0) : "—"}
          sub={topToken ? (topToken.symbol || topToken.mint?.slice(0, 6)) : "none"}
          href={topToken ? `/token/${topToken.mint}` : "/scanner"}
          valueColor="var(--accent)"
          change={topToken ? undefined : undefined}
        />
        <StatCard
          label="Auto-Buy (6h)"
          value={recentAutobuy.length ? `${abExecuted}/${recentAutobuy.length}` : "—"}
          sub={uid ? (autobuyConfig?.enabled ? "enabled" : "disabled") : "set UID"}
          href="/autobuy"
          valueColor={abExecuted > 0 ? "var(--purple)" : undefined}
        />
        <StatCard
          label="Bot Status"
          value={apiOnline === null ? "…" : apiOnline ? "Running" : "Offline"}
          sub={mode !== null ? `${mode === "live" ? "Live" : "Paper"} mode · avg score ${avgScore.toFixed(1)}` : "DigitalDegenX"}
          valueColor={apiOnline === null ? undefined : apiOnline ? "var(--green)" : "var(--red)"}
        />
      </div>

      {/* ── Middle row ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>

        {/* Score distribution */}
        <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 7 }}>
            <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--yellow)", display: "inline-block" }} />
            <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--t2)" }}>Score Distribution</span>
          </div>
          <div style={{ padding: "14px 18px 18px" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8 }}>
              {[
                { label: "0–39",  count: dist.s0,  color: "var(--red)" },
                { label: "40–49", count: dist.s40, color: "var(--t2)" },
                { label: "50–59", count: dist.s50, color: "var(--yellow)" },
                { label: "60–69", count: dist.s60, color: "var(--accent)" },
                { label: "70+",   count: dist.s70, color: "var(--green)" },
              ].map(({ label, count, color }) => (
                <div key={label} style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 18, fontWeight: 700, fontFamily: "var(--font-mono, monospace)", color }}>{count}</div>
                  <div style={{ fontSize: 9, color: "var(--t3)", marginTop: 2 }}>{label}</div>
                  <div style={{ height: 3, background: color, opacity: 0.5, borderRadius: 2, marginTop: 6 }} />
                </div>
              ))}
            </div>
            {!uid && (
              <div style={{ marginTop: 14, fontSize: 11, color: "var(--t3)", textAlign: "center" }}>
                Set UID to unlock portfolio, trades &amp; auto-buy data
              </div>
            )}
          </div>
        </div>

        {/* Recent activity */}
        <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", display: "inline-block" }} />
              <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--t2)" }}>Recent Activity</span>
            </div>
            <Link href="/autobuy" style={{ fontSize: 10, color: "var(--t3)" }}>View all →</Link>
          </div>
          <div style={{ padding: "0 18px" }}>
            {recentAutobuy.length === 0 ? (
              <div style={{ padding: "20px 0", fontSize: 12, color: "var(--t3)", textAlign: "center" }}>
                {uid ? "No recent auto-buy activity" : "Set UID to see activity"}
              </div>
            ) : recentAutobuy.slice(0, 5).map((row) => (
              <div key={row.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
                <div style={{
                  width: 30, height: 30, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, flexShrink: 0,
                  background: row.status === "executed" ? "rgba(34,211,160,0.1)" : row.status === "failed" ? "rgba(244,63,94,0.1)" : "rgba(139,144,168,0.1)",
                }}>
                  {row.status === "executed" ? "🟢" : row.status === "failed" ? "🔴" : "⚪"}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>
                    {row.status === "executed" ? "Auto-buy: " : row.status === "failed" ? "Failed: " : "Blocked: "}
                    {row.symbol || row.name || row.mint?.slice(0, 8) || "Unknown"}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 1 }}>
                    {row.block_category?.replaceAll("_", " ") || (row.status === "executed" ? `${Number(row.sol_amount || 0).toFixed(3)} SOL` : "")}
                  </div>
                </div>
                <div style={{ fontSize: 10, color: "var(--t3)", whiteSpace: "nowrap" }}>{fmtRelTime(row.ts)}</div>
              </div>
            ))}
            {paperPositions.length > 0 && (
              <>
                {paperPositions.slice(0, 3).map((pos) => (
                  <div key={pos.mint} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
                    <div style={{ width: 30, height: 30, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, flexShrink: 0, background: "rgba(96,165,250,0.1)" }}>🔵</div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>Open: {pos.symbol || pos.name || pos.mint.slice(0, 8)}</div>
                      <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 1 }}>position</div>
                    </div>
                    {pos.pnl_pct != null && (
                      <div style={{ fontSize: 12, fontWeight: 600, fontFamily: "var(--font-mono, monospace)", color: Number(pos.pnl_pct) >= 0 ? "var(--green)" : "var(--red)" }}>
                        {Number(pos.pnl_pct) >= 0 ? "+" : ""}{Number(pos.pnl_pct).toFixed(1)}%
                      </div>
                    )}
                  </div>
                ))}
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── Trade stats row ── */}
      {tradeSummary && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          {[
            { label: "Best Strategy", value: tradeSummary.top_strategy || "None", sub: "by closed P&L" },
            { label: "Give-Back Avg", value: `${tradeSummary.avg_giveback_pct.toFixed(1)}%`, sub: "lower = better exits" },
            { label: "Paper Trades", value: String(tradeSummary.total_rows), sub: "total ledger rows" },
            { label: "Closed Trades", value: String(tradeSummary.closed_count), sub: `${tradeSummary.win_rate.toFixed(0)}% win rate` },
          ].map((s) => (
            <Link key={s.label} href="/trades">
              <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 16px" }}
                className="hover:border-[var(--border2)] transition-colors"
              >
                <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 6 }}>{s.label}</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: "var(--foreground)" }}>{s.value}</div>
                <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 3 }}>{s.sub}</div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
