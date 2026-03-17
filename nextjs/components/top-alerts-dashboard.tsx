"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { TokenOperatorPanel } from "@/components/token-operator-panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type AlertItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  archetype?: string;
  state?: string;
  source_primary?: string;
  strategy_profile?: string;
  confidence?: number;
  age_mins?: number;
  buy_ratio_5m?: number;
};

type TopAlertsResponse = { count: number; alerts: AlertItem[] };

const AVATAR_COLORS = [
  { bg: "rgba(249,115,22,0.12)", color: "#f97316", border: "rgba(249,115,22,0.22)" },
  { bg: "rgba(96,165,250,0.10)", color: "#60a5fa", border: "rgba(96,165,250,0.20)" },
  { bg: "rgba(34,211,160,0.10)", color: "#22d3a0", border: "rgba(34,211,160,0.20)" },
  { bg: "rgba(167,139,250,0.10)", color: "#a78bfa", border: "rgba(167,139,250,0.20)" },
  { bg: "rgba(251,191,36,0.10)", color: "#fbbf24", border: "rgba(251,191,36,0.20)" },
  { bg: "rgba(244,63,94,0.10)", color: "#f43f5e", border: "rgba(244,63,94,0.20)" },
];
function avatarColor(mint: string) {
  let h = 0;
  for (let i = 0; i < mint.length; i++) h = (h * 31 + mint.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function scoreColor(s: number) { return s >= 65 ? "var(--accent)" : s >= 50 ? "var(--yellow)" : "var(--t2)"; }
function scoreBg(s: number) { return s >= 65 ? "rgba(249,115,22,0.15)" : s >= 50 ? "rgba(251,191,36,0.12)" : "rgba(100,116,139,0.12)"; }
function fmtMcap(v?: number) {
  if (!v) return "";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}
function fmtAge(mins?: number) {
  if (!mins) return "";
  if (mins < 60) return `${Math.round(mins)}m`;
  return `${(mins / 60).toFixed(1)}h`;
}

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 10, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)", letterSpacing: "-0.02em", color: color || "var(--foreground)" }}>{value}</div>
    </div>
  );
}

export function TopAlertsDashboard() {
  const { uid, loading } = useActiveUid();
  const [items, setItems] = useState<AlertItem[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!uid) return;
    async function load() {
      try {
        const r = await apiFetch<TopAlertsResponse>("/scanner/top", { query: { limit: 20, uid: uid || undefined } });
        setItems(r.alerts || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load top alerts");
      }
    }
    void load();
  }, [uid]);

  if (loading) {
    return (
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, padding: 32, textAlign: "center", color: "var(--t3)", fontSize: 13 }}>
        Checking session…
      </div>
    );
  }

  if (!uid) {
    return (
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, padding: 40, textAlign: "center" }}>
        <div style={{ fontSize: 13, color: "var(--t3)" }}>Set your Telegram UID in the top bar to view top alerts.</div>
      </div>
    );
  }

  const avgScore = items.length ? (items.reduce((s, i) => s + (i.score ?? 0), 0) / items.length) : 0;
  const fresh = items.filter((i) => Number(i.age_mins || 0) <= 10).length;
  const highConf = items.filter((i) => Number(i.confidence || 0) >= 0.7).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <StatCard label="Alerts" value={String(items.length)} color="var(--accent)" />
        <StatCard label="Avg Score" value={avgScore.toFixed(1)} />
        <StatCard label="Fresh ≤10m" value={String(fresh)} color="var(--green)" />
        <StatCard label="High Confidence" value={String(highConf)} color="var(--blue)" />
      </div>

      {/* Panel */}
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>Top Alerts</h2>
            <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", padding: "2px 8px", borderRadius: 20, background: "rgba(249,115,22,0.12)", color: "var(--accent)", border: "1px solid rgba(249,115,22,0.2)" }}>
              Live
            </span>
          </div>
          <span style={{ fontSize: 11, color: "var(--t3)" }}>Today&apos;s best-scoring tokens</span>
        </div>

        {error && (
          <div style={{ margin: 16, padding: "10px 14px", background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, fontSize: 12, color: "var(--red)" }}>
            {error}
          </div>
        )}

        {items.length === 0 && !error && (
          <div style={{ padding: 32, textAlign: "center", fontSize: 13, color: "var(--t3)" }}>
            No alerts yet today.
          </div>
        )}

        {items.map((item, index) => {
          const score = item.score ?? 0;
          const av = avatarColor(item.mint);
          const sym = item.symbol || item.name || item.mint.slice(0, 4);
          const initials = sym.slice(0, 3).toUpperCase();
          const mcap = fmtMcap(item.mcap);
          const age = fmtAge(item.age_mins);

          return (
            <div key={`${item.mint}-${index}`} style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 20px", borderBottom: "1px solid var(--border)" }}
              className="hover:bg-white/[0.02] transition-colors"
            >
              {/* Rank */}
              <div style={{ width: 24, textAlign: "right", fontSize: 11, fontWeight: 600, color: index < 3 ? "var(--accent)" : "var(--t3)", fontFamily: "var(--font-mono, monospace)", flexShrink: 0 }}>
                #{index + 1}
              </div>

              {/* Avatar */}
              <div style={{ width: 36, height: 36, borderRadius: 8, background: av.bg, border: `1px solid ${av.border}`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: av.color, fontFamily: "var(--font-mono, monospace)" }}>{initials}</span>
              </div>

              {/* Token info */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)" }}>{sym}</span>
                  {item.narrative && (
                    <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", padding: "2px 6px", borderRadius: 4, background: "rgba(96,165,250,0.1)", color: "var(--blue)", border: "1px solid rgba(96,165,250,0.2)" }}>
                      {item.narrative}
                    </span>
                  )}
                  {item.archetype && (
                    <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", padding: "2px 6px", borderRadius: 4, background: "rgba(167,139,250,0.1)", color: "var(--purple)", border: "1px solid rgba(167,139,250,0.2)" }}>
                      {item.archetype}
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 3, flexWrap: "wrap" }}>
                  {mcap && <span style={{ fontSize: 11, color: "var(--t3)", fontFamily: "var(--font-mono, monospace)" }}>{mcap}</span>}
                  {age && <span style={{ fontSize: 11, color: "var(--t3)" }}>{age}</span>}
                  {item.strategy_profile && <span style={{ fontSize: 11, color: "var(--t3)" }}>{item.strategy_profile.replaceAll("_", " ")}</span>}
                  {item.buy_ratio_5m != null && Number(item.buy_ratio_5m) > 0 && (
                    <span style={{ fontSize: 10, color: Number(item.buy_ratio_5m) >= 0.6 ? "var(--green)" : "var(--t3)" }}>
                      {(Number(item.buy_ratio_5m) * 100).toFixed(0)}% buys
                    </span>
                  )}
                  {item.confidence != null && (
                    <span style={{ fontSize: 10, color: Number(item.confidence) >= 0.7 ? "var(--green)" : "var(--t3)" }}>
                      {(Number(item.confidence) * 100).toFixed(0)}% conf
                    </span>
                  )}
                </div>
              </div>

              {/* Score badge */}
              <div style={{ width: 40, height: 40, borderRadius: 8, background: scoreBg(score), display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <span style={{ fontSize: 14, fontWeight: 700, color: scoreColor(score), fontFamily: "var(--font-mono, monospace)", lineHeight: 1 }}>{score}</span>
              </div>

              {/* Actions */}
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                <TokenOperatorPanel mint={item.mint} label="Inspect" />
                <Link
                  href={`/token/${item.mint}`}
                  style={{ fontSize: 11, fontWeight: 500, padding: "6px 12px", borderRadius: 7, background: "var(--bg3)", border: "1px solid var(--border)", color: "var(--t2)", textDecoration: "none" }}
                  className="hover:border-[var(--border2)] hover:text-white transition-colors"
                >
                  View
                </Link>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
