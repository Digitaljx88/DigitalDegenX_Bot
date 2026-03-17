"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type AutoBuyActivityRow = {
  id: number; ts: number; mint?: string; symbol?: string; name?: string;
  score?: number; effective_score?: number; mcap?: number;
  strategy_profile?: string; confidence?: number; sol_amount?: number;
  size_multiplier?: number; mode?: string;
  status: "executed" | "blocked" | "failed";
  block_reason?: string; block_category?: string;
  source?: string; narrative?: string; archetype?: string;
};

type AutoBuyActivityResponse = {
  uid: number; count: number; latest: AutoBuyActivityRow | null;
  summary?: {
    window_hours: number; total: number;
    status_counts: Record<string, number>;
    blocked_by_category: Record<string, number>;
    top_block_category?: string;
    avg_confidence?: number; avg_size_sol?: number;
  };
  items: AutoBuyActivityRow[];
};

function fmtMcap(v?: number) {
  if (!v) return "n/a";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}
function prettify(v?: string) { return (v || "all").replaceAll("_", " "); }

const statusColor: Record<string, { bg: string; color: string; border: string }> = {
  executed: { bg: "rgba(34,211,160,0.12)", color: "var(--green)", border: "rgba(34,211,160,0.25)" },
  blocked:  { bg: "rgba(100,116,139,0.12)", color: "var(--t2)", border: "var(--border)" },
  failed:   { bg: "rgba(244,63,94,0.12)", color: "var(--red)", border: "rgba(244,63,94,0.25)" },
};

function StatCard({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)", letterSpacing: "-0.02em", color: color || "var(--foreground)" }}>{value}</div>
    </div>
  );
}

function FilterSelect({ value, onChange, children }: { value: string; onChange: (v: string) => void; children: React.ReactNode }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 8,
        color: "var(--foreground)", fontSize: 11, padding: "7px 12px", outline: "none", cursor: "pointer",
      }}
    >
      {children}
    </select>
  );
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
      if (!uid) { setData(null); return; }
      try {
        const r = await apiFetch<AutoBuyActivityResponse>(`/autobuy/activity/${uid}`, { query: { limit: 200 } });
        setData(r);
        setError("");
      } catch (err) { setError(err instanceof Error ? err.message : "Failed to load auto-buy activity"); }
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

  const blockerOptions = useMemo(() => Array.from(new Set((data?.items || []).map((i) => i.block_category || "none"))).sort(), [data]);
  const strategyOptions = useMemo(() => Array.from(new Set((data?.items || []).map((i) => i.strategy_profile || "none"))).sort(), [data]);
  const sourceOptions = useMemo(() => Array.from(new Set((data?.items || []).map((i) => i.source || "none"))).sort(), [data]);

  if (!uid) {
    return (
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, padding: 40, textAlign: "center" }}>
        <div style={{ fontSize: 13, color: "var(--t3)" }}>Set your Telegram UID to inspect auto-buy execution history.</div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Stats */}
      {data?.summary && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
          <StatCard label="Executed" value={data.summary.status_counts.executed || 0} color="var(--green)" />
          <StatCard label="Blocked" value={data.summary.status_counts.blocked || 0} />
          <StatCard label="Failed" value={data.summary.status_counts.failed || 0} color="var(--red)" />
          <StatCard label="Top Blocker" value={prettify(data.summary.top_block_category)} color="var(--yellow)" />
          <StatCard label="Avg Confidence" value={Number(data.summary.avg_confidence || 0).toFixed(2)} />
          <StatCard label="Avg Size" value={`${Number(data.summary.avg_size_sol || 0).toFixed(3)} SOL`} color="var(--blue)" />
        </div>
      )}

      {/* Panel */}
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>Auto-Buy Activity</h2>
          <span style={{ fontSize: 11, color: "var(--t3)" }}>{filteredItems.length} of {data?.items.length || 0} entries</span>
        </div>

        {/* Filters */}
        <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--border)", display: "flex", gap: 8, flexWrap: "wrap" }}>
          <FilterSelect value={statusFilter} onChange={setStatusFilter}>
            <option value="all">All statuses</option>
            <option value="executed">Executed</option>
            <option value="blocked">Blocked</option>
            <option value="failed">Failed</option>
          </FilterSelect>
          <FilterSelect value={blockerFilter} onChange={setBlockerFilter}>
            <option value="all">All blockers</option>
            {blockerOptions.map((v) => <option key={v} value={v}>{prettify(v)}</option>)}
          </FilterSelect>
          <FilterSelect value={strategyFilter} onChange={setStrategyFilter}>
            <option value="all">All strategies</option>
            {strategyOptions.map((v) => <option key={v} value={v}>{prettify(v)}</option>)}
          </FilterSelect>
          <FilterSelect value={sourceFilter} onChange={setSourceFilter}>
            <option value="all">All sources</option>
            {sourceOptions.map((v) => <option key={v} value={v}>{prettify(v)}</option>)}
          </FilterSelect>
        </div>

        {error && (
          <div style={{ margin: 16, padding: "10px 14px", background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, fontSize: 12, color: "var(--red)" }}>
            {error}
          </div>
        )}

        {!data && !error && (
          <div style={{ padding: 24, textAlign: "center", fontSize: 13, color: "var(--t3)" }}>Loading…</div>
        )}

        {filteredItems.length === 0 && data && (
          <div style={{ padding: 24, textAlign: "center", fontSize: 13, color: "var(--t3)" }}>No activity matches the current filters.</div>
        )}

        {filteredItems.map((item) => {
          const sc = statusColor[item.status] || statusColor.blocked;
          return (
            <div key={item.id} style={{ padding: "12px 20px", borderBottom: "1px solid var(--border)" }}
              className="hover:bg-white/[0.02] transition-colors"
            >
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <Link href={`/token/${item.mint || ""}`} style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)", textDecoration: "none" }}
                      className="hover:text-[var(--accent)]">
                      {item.symbol || item.name || item.mint || "Unknown"}
                    </Link>
                    <span style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", padding: "2px 7px", borderRadius: 20, background: sc.bg, color: sc.color, border: `1px solid ${sc.border}` }}>
                      {item.status}
                    </span>
                    {item.block_category && (
                      <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", padding: "2px 6px", borderRadius: 4, background: "rgba(251,191,36,0.1)", color: "var(--yellow)", border: "1px solid rgba(251,191,36,0.2)" }}>
                        {prettify(item.block_category)}
                      </span>
                    )}
                  </div>
                  <div style={{ display: "flex", gap: 10, marginTop: 4, fontSize: 10, color: "var(--t3)", flexWrap: "wrap" }}>
                    {item.strategy_profile && <span>{prettify(item.strategy_profile)}</span>}
                    {item.source && <span>{item.source}</span>}
                    <span>{item.mode || "paper"}</span>
                  </div>
                </div>
                <div style={{ fontSize: 10, color: "var(--t3)", whiteSpace: "nowrap", flexShrink: 0 }}>
                  {new Date((item.ts || 0) * 1000).toLocaleString()}
                </div>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8, marginTop: 10 }}>
                {[
                  { label: "Score", value: String(item.effective_score ?? item.score ?? 0) },
                  { label: "Confidence", value: Number(item.confidence || 0).toFixed(2) },
                  { label: "Size", value: `${Number(item.sol_amount || 0).toFixed(3)} SOL` },
                  { label: "MCap", value: fmtMcap(item.mcap) },
                  { label: "Mult", value: item.size_multiplier ? `${item.size_multiplier}x` : "1x" },
                ].map((s) => (
                  <div key={s.label}>
                    <div style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{s.label}</div>
                    <div style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)", fontFamily: "var(--font-mono, monospace)", marginTop: 1 }}>{s.value}</div>
                  </div>
                ))}
              </div>

              {item.block_reason && (
                <div style={{ marginTop: 8, padding: "6px 10px", background: "rgba(244,63,94,0.08)", border: "1px solid rgba(244,63,94,0.2)", borderRadius: 6, fontSize: 11, color: "var(--red)" }}>
                  {item.block_reason}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
