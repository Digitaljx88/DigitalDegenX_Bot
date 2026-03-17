"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";
import { ExternalLinks } from "@/components/external-links";
import { topFactors, type BreakdownMap } from "@/lib/score-labels";

type ScannerFeedItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  source_primary?: string;
  strategy_profile?: string;
  state?: string;
  confidence?: number;
  age_mins?: number;
  buy_ratio_5m?: number;
  alerted?: number;
  dq?: string;
  ts?: number;
  breakdown?: BreakdownMap;
  autobuy_preview?: {
    eligible?: boolean;
    status?: string;
    block_reason?: string;
    block_category?: string;
    sol_amount?: number;
    confidence?: number;
    strategy_profile?: string;
  };
};

type ScannerFeedResponse = { count: number; items: ScannerFeedItem[] };
type ModeResponse = { uid: number; mode: "paper" | "live" };
type FilterTab = "all" | "alerted" | "tracked" | "dq";

// ── Formatters ───────────────────────────────────────────────────
function fmtMcap(v?: number) {
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

// ── Token avatar color from mint hash ────────────────────────────
const AVATAR_COLORS = [
  { bg: "rgba(249,115,22,0.12)",  color: "#f97316", border: "rgba(249,115,22,0.22)" },
  { bg: "rgba(96,165,250,0.10)",  color: "#60a5fa", border: "rgba(96,165,250,0.20)" },
  { bg: "rgba(34,211,160,0.10)",  color: "#22d3a0", border: "rgba(34,211,160,0.20)" },
  { bg: "rgba(167,139,250,0.10)", color: "#a78bfa", border: "rgba(167,139,250,0.20)" },
  { bg: "rgba(251,191,36,0.10)",  color: "#fbbf24", border: "rgba(251,191,36,0.20)" },
  { bg: "rgba(244,63,94,0.10)",   color: "#f43f5e", border: "rgba(244,63,94,0.20)" },
];

function avatarColor(mint: string) {
  let h = 0;
  for (let i = 0; i < mint.length; i++) h = (h * 31 + mint.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function initials(name?: string, symbol?: string) {
  const s = symbol || name || "??";
  return s.slice(0, 2).toUpperCase();
}

// ── Score helpers ─────────────────────────────────────────────────
function scoreBadgeStyle(score: number, dq?: string) {
  if (dq)       return { bg: "rgba(244,63,94,0.12)",  color: "#f43f5e", border: "rgba(244,63,94,0.25)",  bar: "#f43f5e" };
  if (score >= 65) return { bg: "rgba(249,115,22,0.12)",  color: "#f97316", border: "rgba(249,115,22,0.25)", bar: "#f97316" };
  if (score >= 50) return { bg: "rgba(251,191,36,0.10)",  color: "#fbbf24", border: "rgba(251,191,36,0.20)", bar: "#fbbf24" };
  return            { bg: "rgba(139,144,168,0.10)", color: "#8b90a8", border: "rgba(139,144,168,0.18)", bar: "#555c78" };
}

export function ScannerDashboard() {
  const { uid } = useActiveUid();
  const [feed, setFeed] = useState<ScannerFeedItem[]>([]);
  const [clock, setClock] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [submittingMint, setSubmittingMint] = useState("");
  const [buyAmount, setBuyAmount] = useState("0.1");
  const [tradeMode, setTradeMode] = useState<"paper" | "live">("paper");
  const [tab, setTab] = useState<FilterTab>("all");

  // Live clock
  useEffect(() => {
    function tick() {
      setClock(new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    }
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);

  const loadFeed = useCallback(async () => {
    try {
      const data = await apiFetch<ScannerFeedResponse>("/scanner/feed", {
        query: { limit: 40, uid: uid || undefined },
      });
      setFeed(data.items || []);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Feed error");
    }
  }, [uid]);

  useEffect(() => {
    loadFeed();
    const t = window.setInterval(loadFeed, 5000);
    return () => window.clearInterval(t);
  }, [loadFeed]);

  useEffect(() => {
    async function loadMode() {
      if (!uid) { setTradeMode("paper"); return; }
      try {
        const d = await apiFetch<ModeResponse>("/mode", { query: { uid } });
        setTradeMode(d.mode || "paper");
      } catch { setTradeMode("paper"); }
    }
    void loadMode();
  }, [uid]);

  const counts = useMemo(() => ({
    all:     feed.length,
    alerted: feed.filter((i) => i.alerted && !i.dq).length,
    tracked: feed.filter((i) => !i.alerted && !i.dq).length,
    dq:      feed.filter((i) => !!i.dq).length,
  }), [feed]);

  const filtered = useMemo(() => {
    const base = feed.slice(0, 40);
    if (tab === "alerted") return base.filter((i) => !!i.alerted && !i.dq);
    if (tab === "tracked") return base.filter((i) => !i.alerted && !i.dq);
    if (tab === "dq")      return base.filter((i) => !!i.dq);
    return base;
  }, [feed, tab]);

  async function quickBuy(mint: string) {
    if (!uid) { setError("Set your Telegram UID first."); return; }
    setSubmittingMint(mint);
    try {
      await apiFetch("/buy", {
        method: "POST",
        body: JSON.stringify({ uid, mint, sol_amount: Number(buyAmount || 0), mode: tradeMode }),
      });
      setError("");
      setMessage(`${tradeMode === "paper" ? "Paper" : "Live"} buy submitted.`);
      setTimeout(() => setMessage(""), 4000);
      void loadFeed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Buy failed");
      setTimeout(() => setError(""), 6000);
    } finally {
      setSubmittingMint("");
    }
  }

  const TABS: { key: FilterTab; label: string }[] = [
    { key: "all",     label: "All" },
    { key: "alerted", label: "Alerted" },
    { key: "tracked", label: "Tracked" },
    { key: "dq",      label: "DQ'd" },
  ];

  return (
    <div
      style={{
        background: "var(--bg1)",
        border: "1px solid var(--border)",
        borderRadius: 14,
        overflow: "hidden",
      }}
    >
      {/* ── Toolbar ── */}
      <div
        className="flex items-center justify-between flex-wrap gap-3"
        style={{
          padding: "14px 20px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg2)",
        }}
      >
        {/* Left: title + filter tabs */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <span
              className="live-dot rounded-full"
              style={{ width: 6, height: 6, background: "var(--green)", display: "inline-block" }}
            />
            <span
              style={{
                fontSize: 12,
                fontWeight: 600,
                letterSpacing: "0.12em",
                textTransform: "uppercase",
                color: "var(--text2)",
              }}
            >
              Scanner
            </span>
            <span
              style={{
                fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                fontSize: 11,
                color: "var(--text3)",
              }}
            >
              {clock}
            </span>
          </div>

          {/* Filter tabs */}
          <div className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className="transition-all"
                style={{
                  padding: "4px 12px",
                  borderRadius: 6,
                  fontSize: 12,
                  fontWeight: 500,
                  cursor: "pointer",
                  fontFamily: "var(--font-sans, 'Space Grotesk', sans-serif)",
                  border: tab === t.key ? "1px solid var(--accent)" : "1px solid var(--border)",
                  background: tab === t.key ? "var(--accent)" : "transparent",
                  color: tab === t.key ? "#fff" : "var(--text3)",
                }}
              >
                {t.label}
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    background: "rgba(255,255,255,0.15)",
                    borderRadius: 4,
                    fontSize: 10,
                    padding: "1px 5px",
                    marginLeft: 4,
                    fontWeight: 600,
                  }}
                >
                  {counts[t.key]}
                </span>
              </button>
            ))}
          </div>
        </div>

        {/* Right: size + mode */}
        <div className="flex items-center gap-2">
          <span style={{ fontSize: 11, color: "var(--text3)", marginRight: 2 }}>Size</span>
          {["0.05", "0.1", "0.25", "0.5"].map((amt) => (
            <button
              key={amt}
              type="button"
              onClick={() => setBuyAmount(amt)}
              style={{
                padding: "4px 10px",
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                cursor: "pointer",
                border: buyAmount === amt ? "1px solid rgba(249,115,22,0.3)" : "1px solid var(--border)",
                background: buyAmount === amt ? "var(--bg4)" : "transparent",
                color: buyAmount === amt ? "var(--accent)" : "var(--text3)",
              }}
            >
              {amt}
            </button>
          ))}

          <div style={{ width: 1, height: 20, background: "var(--border)", margin: "0 2px" }} />

          <button
            type="button"
            onClick={() => setTradeMode(tradeMode === "paper" ? "live" : "paper")}
            style={{
              padding: "4px 12px",
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              fontFamily: "var(--font-sans, 'Space Grotesk', sans-serif)",
              border: tradeMode === "live"
                ? "1px solid rgba(244,63,94,0.25)"
                : "1px solid var(--border)",
              background: tradeMode === "live"
                ? "rgba(244,63,94,0.10)"
                : "transparent",
              color: tradeMode === "live" ? "var(--red)" : "var(--text3)",
            }}
          >
            {tradeMode === "paper" ? "Paper" : "Live"}
          </button>
        </div>
      </div>

      {/* ── Alerts ── */}
      {error && (
        <div style={{
          borderBottom: "1px solid rgba(244,63,94,0.2)",
          background: "rgba(244,63,94,0.08)",
          padding: "8px 20px",
          fontSize: 12,
          color: "var(--red)",
        }}>
          {error}
        </div>
      )}
      {message && (
        <div style={{
          borderBottom: "1px solid rgba(34,211,160,0.2)",
          background: "rgba(34,211,160,0.08)",
          padding: "8px 20px",
          fontSize: 12,
          color: "var(--green)",
        }}>
          {message}
        </div>
      )}

      {/* ── Table ── */}
      <div className="overflow-x-auto">
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "var(--bg2)", borderBottom: "1px solid var(--border)" }}>
              {["Token", "Score", "MCap", "Age", "Setup", "Status", uid ? "Auto-Buy" : null, "Action"]
                .filter(Boolean)
                .map((h) => (
                  <th
                    key={h as string}
                    style={{
                      padding: "10px 16px",
                      textAlign: "left",
                      fontSize: 10,
                      fontWeight: 600,
                      letterSpacing: "0.12em",
                      textTransform: "uppercase",
                      color: "var(--text3)",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {h}
                  </th>
                ))}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td
                  colSpan={uid ? 8 : 7}
                  style={{ padding: "40px", textAlign: "center", color: "var(--text3)", fontSize: 13 }}
                >
                  {feed.length === 0
                    ? "Waiting for scanner feed — refreshing every 5s…"
                    : "No tokens match this filter."}
                </td>
              </tr>
            ) : null}

            {filtered.map((item) => {
              const score = item.score ?? 0;
              const isAlerted = !!item.alerted && !item.dq;
              const isDq = !!item.dq;
              const av = avatarColor(item.mint);
              const sb = scoreBadgeStyle(score, item.dq);

              return (
                <tr
                  key={`${item.mint}-${item.ts}`}
                  style={{ borderBottom: "1px solid var(--border)", cursor: "pointer" }}
                  className="hover:bg-white/[0.02] transition-colors"
                >
                  {/* ── Token ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <div className="flex items-center gap-3">
                      <div
                        style={{
                          width: 36, height: 36, borderRadius: 10,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 13, fontWeight: 700, flexShrink: 0,
                          background: av.bg, color: av.color, border: `1px solid ${av.border}`,
                        }}
                      >
                        {initials(item.name, item.symbol)}
                      </div>
                      <div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>
                          <Link
                            href={`/token/${item.mint}`}
                            className="hover:text-[var(--accent)] transition-colors"
                          >
                            {item.symbol || item.name || item.mint.slice(0, 8)}
                          </Link>
                        </div>
                        <div
                          style={{
                            fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                            fontSize: 10,
                            color: "var(--text3)",
                            marginTop: 2,
                          }}
                        >
                          {item.mint.slice(0, 14)}…
                        </div>
                        <div className="mt-1 flex gap-1">
                          <span
                            style={{
                              fontSize: 9, fontWeight: 600, letterSpacing: "0.08em",
                              textTransform: "uppercase", padding: "2px 6px", borderRadius: 4,
                              background: "rgba(249,115,22,0.10)", color: "var(--accent)",
                              border: "1px solid rgba(249,115,22,0.18)",
                            }}
                          >
                            {item.source_primary || "scan"}
                          </span>
                          {item.state ? (
                            <span
                              style={{
                                fontSize: 9, fontWeight: 600, letterSpacing: "0.08em",
                                textTransform: "uppercase", padding: "2px 6px", borderRadius: 4,
                                background: "rgba(96,165,250,0.08)", color: "var(--blue)",
                                border: "1px solid rgba(96,165,250,0.15)",
                              }}
                            >
                              {item.state.replaceAll("_", " ")}
                            </span>
                          ) : null}
                        </div>
                        <ExternalLinks mint={item.mint} className="mt-1" />
                      </div>
                    </div>
                  </td>

                  {/* ── Score ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <div className="flex items-center gap-2.5">
                      <div
                        style={{
                          width: 42, height: 42, borderRadius: 10,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 16, fontWeight: 700,
                          fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                          flexShrink: 0,
                          background: sb.bg, color: sb.color, border: `1px solid ${sb.border}`,
                        }}
                      >
                        {score}
                      </div>
                      <div>
                        <div
                          style={{
                            width: 40, height: 3,
                            background: "var(--bg4)", borderRadius: 2, overflow: "hidden",
                            marginBottom: 5,
                          }}
                        >
                          <div
                            style={{
                              height: "100%", borderRadius: 2,
                              width: `${Math.min(100, score)}%`,
                              background: sb.bar,
                              transition: "width 0.3s",
                            }}
                          />
                        </div>
                        {item.breakdown && (() => {
                          const chips = topFactors(item.breakdown, 3);
                          if (chips.length === 0) return null;
                          return (
                            <div style={{ display: "flex", gap: 3, flexWrap: "nowrap" }}>
                              {chips.map(({ key, pts, reason, meta }) => (
                                <span
                                  key={key}
                                  title={`${meta.label}: ${meta.description}${reason ? `\n${reason}` : ""}`}
                                  style={{
                                    display: "inline-flex", alignItems: "center", gap: 2,
                                    padding: "1px 5px", borderRadius: 4, fontSize: 10, fontWeight: 600,
                                    lineHeight: "18px", whiteSpace: "nowrap",
                                    background: `rgba(${meta.colorRgb}, 0.12)`,
                                    color: meta.color,
                                    border: `1px solid rgba(${meta.colorRgb}, 0.22)`,
                                  }}
                                >
                                  {meta.icon}+{pts}
                                </span>
                              ))}
                            </div>
                          );
                        })()}
                      </div>
                    </div>
                  </td>

                  {/* ── MCap ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <div
                      style={{
                        fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                        fontSize: 13, fontWeight: 500, color: "var(--foreground)",
                      }}
                    >
                      {fmtMcap(item.mcap)}
                    </div>
                    {item.buy_ratio_5m != null ? (
                      <div style={{ fontSize: 10, color: "var(--text3)", marginTop: 2 }}>
                        {Math.round(item.buy_ratio_5m * 100)}% buy
                      </div>
                    ) : null}
                  </td>

                  {/* ── Age ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <span
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 4,
                        background: "rgba(167,139,250,0.08)", color: "var(--purple)",
                        border: "1px solid rgba(167,139,250,0.15)",
                        borderRadius: 6, padding: "3px 8px",
                        fontSize: 11, fontWeight: 500,
                        fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                      }}
                    >
                      ⬤ {fmtAge(item.age_mins)}
                    </span>
                  </td>

                  {/* ── Setup ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)", marginBottom: 3 }}>
                      {item.narrative || "Other"}
                    </div>
                    <div style={{ fontSize: 10, color: "var(--text3)" }}>
                      {item.strategy_profile || "unprofiled"} · conf {(item.confidence ?? 0).toFixed(2)}
                    </div>
                  </td>

                  {/* ── Status ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    {isDq ? (
                      <div>
                        <span
                          style={{
                            display: "inline-flex", alignItems: "center", gap: 5,
                            padding: "4px 10px", borderRadius: 20,
                            fontSize: 11, fontWeight: 600,
                            background: "rgba(244,63,94,0.08)", color: "var(--red)",
                            border: "1px solid rgba(244,63,94,0.15)",
                          }}
                        >
                          <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--red)", display: "inline-block" }} />
                          DQ&apos;d
                        </span>
                        {item.dq && (
                          <div style={{ fontSize: 10, color: "rgba(244,63,94,0.6)", marginTop: 3, maxWidth: 120 }}>
                            {item.dq.replaceAll("_", " ")}
                          </div>
                        )}
                      </div>
                    ) : isAlerted ? (
                      <span
                        style={{
                          display: "inline-flex", alignItems: "center", gap: 5,
                          padding: "4px 10px", borderRadius: 20,
                          fontSize: 11, fontWeight: 600,
                          background: "rgba(34,211,160,0.08)", color: "var(--green)",
                          border: "1px solid rgba(34,211,160,0.15)",
                        }}
                      >
                        <span className="live-dot" style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--green)", display: "inline-block" }} />
                        Alerted
                      </span>
                    ) : (
                      <span
                        style={{
                          display: "inline-flex", alignItems: "center", gap: 5,
                          padding: "4px 10px", borderRadius: 20,
                          fontSize: 11, fontWeight: 600,
                          background: "rgba(139,144,168,0.08)", color: "var(--text2)",
                        }}
                      >
                        <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--text3)", display: "inline-block" }} />
                        Tracked
                      </span>
                    )}
                  </td>

                  {/* ── Auto-Buy ── */}
                  {uid ? (
                    <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                      {item.autobuy_preview?.eligible ? (
                        <span
                          style={{
                            fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                            fontSize: 12, color: "var(--green)", fontWeight: 500,
                          }}
                        >
                          {Number(item.autobuy_preview.sol_amount || 0).toFixed(3)} SOL
                        </span>
                      ) : item.autobuy_preview ? (
                        <div>
                          <span style={{ fontSize: 12, color: "var(--text3)" }}>—</span>
                          {item.autobuy_preview.block_category && (
                            <div style={{ fontSize: 10, color: "var(--text3)", marginTop: 2 }}>
                              {item.autobuy_preview.block_category.replaceAll("_", " ")}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span style={{ fontSize: 12, color: "var(--text3)" }}>—</span>
                      )}
                    </td>
                  ) : null}

                  {/* ── Action ── */}
                  <td style={{ padding: "14px 16px", verticalAlign: "middle" }}>
                    <div className="flex items-center gap-1.5">
                      <Link
                        href={`/token/${item.mint}`}
                        style={{
                          padding: "5px 12px", borderRadius: 6, fontSize: 11, fontWeight: 600,
                          border: "1px solid var(--border2)", background: "var(--bg3)",
                          color: "var(--text2)", cursor: "pointer",
                        }}
                        className="hover:bg-[var(--bg4)] hover:text-white transition-colors"
                      >
                        View
                      </Link>
                      <button
                        type="button"
                        onClick={() => quickBuy(item.mint)}
                        disabled={!uid || !!item.dq || submittingMint === item.mint}
                        style={{
                          padding: "5px 12px", borderRadius: 6, fontSize: 11, fontWeight: 600,
                          cursor: "pointer",
                          fontFamily: "var(--font-sans, 'Space Grotesk', sans-serif)",
                          border: tradeMode === "live"
                            ? "1px solid rgba(244,63,94,0.25)"
                            : "1px solid rgba(167,139,250,0.25)",
                          background: tradeMode === "live"
                            ? "rgba(244,63,94,0.08)"
                            : "rgba(167,139,250,0.08)",
                          color: tradeMode === "live" ? "var(--red)" : "var(--purple)",
                          opacity: (!uid || !!item.dq) ? 0.35 : 1,
                        }}
                        className="hover:opacity-80 transition-opacity"
                      >
                        {submittingMint === item.mint
                          ? "…"
                          : tradeMode === "paper"
                            ? "Paper"
                            : "Buy"}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
