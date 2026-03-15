"use client";

import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";
import { ExternalLinks } from "@/components/external-links";

type SnapshotResponse = {
  mint: string;
  lifecycle: Record<string, unknown>;
  metrics: Record<string, unknown>;
  enrichment: Record<string, unknown>;
  trading_snapshot?: Record<string, unknown> | null;
  analysis?: Record<string, unknown> | null;
  autobuy_preview?: Record<string, unknown> | null;
  events: Array<Record<string, unknown>>;
};

type TimelineEvent = {
  id?: number;
  event_type?: string;
  ts?: number;
  payload?: Record<string, unknown>;
};

type TimelineResponse = { mint: string; events: TimelineEvent[] };

function fmtDate(ts?: number) {
  if (!ts) return "n/a";
  return new Date(ts * 1000).toLocaleString();
}

function fmtRelTime(ts?: number) {
  if (!ts) return "";
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${(diff / 3600).toFixed(1)}h ago`;
}

function mv(value: unknown): string {
  if (typeof value === "number") {
    if (Math.abs(value) >= 1000) return value.toLocaleString();
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value ?? "n/a");
}

type BottomTab = "snapshot" | "analysis" | "quality" | "history";

export function TokenTimelineDashboard({ mint }: { mint: string }) {
  const { uid, loading } = useActiveUid();
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [error, setError] = useState("");
  const [bottomTab, setBottomTab] = useState<BottomTab>("snapshot");
  const [buyAmount, setBuyAmount] = useState("0.1");
  const [tradeMode, setTradeMode] = useState<"paper" | "live">("paper");
  const [buyMsg, setBuyMsg] = useState("");
  const [buying, setBuying] = useState(false);

  useEffect(() => {
    if (loading) return;
    async function load() {
      try {
        const [snapshotData, timelineData] = await Promise.all([
          apiFetch<SnapshotResponse>(`/token/${mint}/snapshot`, { query: uid ? { uid } : {} }),
          apiFetch<TimelineResponse>(`/token/${mint}/timeline`),
        ]);
        setSnapshot(snapshotData);
        setTimeline(timelineData.events || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load token data");
      }
    }
    void load();
  }, [loading, mint, uid]);

  const analysis = useMemo(() => {
    const a = (snapshot?.analysis || {}) as Record<string, unknown>;
    const eq = (a.entry_quality || {}) as Record<string, unknown>;
    const qf = (a.quality_flags || {}) as Record<string, unknown>;
    return {
      strategy: a.strategy_profile as string | undefined,
      confidence: a.strategy_confidence as number | undefined,
      exitPreset: a.strategy_exit_preset as string | undefined,
      risk: a.risk as string | undefined,
      effectiveScore: a.effective_score as number | undefined,
      alertBlocked: qf.alert_blocked as boolean | undefined,
      autobuyBlocked: qf.autobuy_blocked as boolean | undefined,
      forceScouted: qf.force_scouted as boolean | undefined,
      reasons: (qf.quality_reasons || []) as string[],
      forceReasons: (qf.force_scouted_reasons || []) as string[],
      autobuyOnly: (qf.autobuy_only_reasons || []) as string[],
      ageBand: eq.age_band as string | undefined,
      buyRatio: eq.buy_ratio_5m as number | undefined,
      scoreSlope: eq.score_slope as number | undefined,
      breakdown: Object.entries((a.breakdown || {}) as Record<string, unknown>).map(([k, v]) => {
        const t = Array.isArray(v) ? v : [];
        return { factor: k, pts: t[0], reason: t[1] };
      }),
    };
  }, [snapshot]);

  const lifecycle = snapshot?.lifecycle || {};
  const metrics = snapshot?.metrics || {};
  const autobuyPreview = snapshot?.autobuy_preview;

  const scoreEvents = useMemo(() => {
    return timeline
      .filter((e) => e.event_type === "score_update")
      .map((e) => {
        const p = e.payload || {};
        return {
          ts: e.ts,
          score: typeof p.last_score === "number" ? p.last_score : null,
          effective: typeof p.last_effective_score === "number" ? p.last_effective_score : null,
          confidence: typeof p.last_confidence === "number" ? p.last_confidence : null,
          strategy: typeof p.strategy_profile === "string" ? p.strategy_profile : "n/a",
          narrative: typeof p.narrative === "string" ? p.narrative : "Other",
        };
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
  }, [timeline]);

  const recentFeedEvents = useMemo(() => {
    return [...timeline]
      .sort((a, b) => (b.ts || 0) - (a.ts || 0))
      .slice(0, 20);
  }, [timeline]);

  async function quickBuy() {
    if (!uid) { setBuyMsg("Set UID first."); return; }
    setBuying(true);
    try {
      await apiFetch("/buy", {
        method: "POST",
        body: JSON.stringify({ uid, mint, sol_amount: Number(buyAmount || 0), mode: tradeMode }),
      });
      setBuyMsg(`${tradeMode === "paper" ? "Paper" : "Live"} buy submitted.`);
    } catch (err) {
      setBuyMsg(err instanceof Error ? err.message : "Buy failed");
    } finally {
      setBuying(false);
    }
  }

  const BOTTOM_TABS: { key: BottomTab; label: string }[] = [
    { key: "snapshot", label: "Snapshot" },
    { key: "analysis", label: "Analysis" },
    { key: "quality", label: "Quality" },
    { key: "history", label: "Score History" },
  ];

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}
      {!loading && !uid && (
        <div className="rounded-xl border border-white/8 bg-black/20 px-4 py-3 text-sm text-white/40">
          Set UID for auto-buy preview and personalized analysis.
        </div>
      )}

      {/* Mobile timeline (shown below lg) */}
      {recentFeedEvents.length > 0 && (
        <details className="lg:hidden overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
          <summary className="cursor-pointer px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-white/30 select-none">
            Timeline Feed ({recentFeedEvents.length} events)
          </summary>
          <div>
            {recentFeedEvents.slice(0, 10).map((ev, i) => (
              <div key={`mob-${ev.id || ev.ts || i}`} className="border-b border-white/4 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-[10px] font-medium text-white/70">
                    {(ev.event_type || "event").replaceAll("_", " ")}
                  </div>
                  <div className="text-[9px] text-white/25">{fmtRelTime(ev.ts)}</div>
                </div>
              </div>
            ))}
          </div>
        </details>
      )}

      {/* 3-column layout */}
      <div className="grid gap-3 lg:grid-cols-[220px_1fr_260px]">

        {/* ── LEFT: event feed ── */}
        <div className="hidden overflow-hidden rounded-2xl border border-white/8 bg-[#080e14] lg:flex lg:flex-col">
          <div className="border-b border-white/6 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-white/30">
            Timeline Feed
          </div>
          <div className="flex-1 overflow-y-auto">
            {recentFeedEvents.length ? (
              recentFeedEvents.map((ev, i) => (
                <div
                  key={`${ev.id || ev.ts || i}`}
                  className="border-b border-white/4 px-3 py-2 hover:bg-white/[0.02]"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-[10px] font-medium text-white/70">
                      {(ev.event_type || "event").replaceAll("_", " ")}
                    </div>
                    <div className="text-[9px] text-white/25">
                      {fmtRelTime(ev.ts)}
                    </div>
                  </div>
                  {ev.event_type === "score_update" && ev.payload ? (
                    <div className="mt-0.5 text-[10px] text-white/40">
                      score {mv(ev.payload.last_effective_score ?? ev.payload.last_score)} ·{" "}
                      {mv(ev.payload.strategy_profile)}
                    </div>
                  ) : null}
                </div>
              ))
            ) : (
              <div className="px-3 py-4 text-[10px] text-white/25">
                No events recorded yet.
              </div>
            )}
          </div>
        </div>

        {/* ── CENTER: chart + tabs ── */}
        <div className="flex flex-col gap-3 overflow-hidden">
          {/* Token title bar */}
          <div className="flex items-center justify-between rounded-2xl border border-white/8 bg-[#080e14] px-4 py-3">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-lg font-bold text-white">
                  {mv(lifecycle.symbol || lifecycle.name || mint.slice(0, 8))}
                </span>
                <span className="text-sm text-white/40">/ SOL</span>
                {analysis.effectiveScore != null && (
                  <span className={`rounded px-1.5 py-0.5 font-mono text-xs font-bold ${
                    analysis.effectiveScore >= 75 ? "bg-emerald-500/20 text-emerald-300"
                    : analysis.effectiveScore >= 60 ? "bg-amber-500/20 text-amber-300"
                    : "bg-white/8 text-white/50"
                  }`}>
                    {analysis.effectiveScore}
                  </span>
                )}
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-white/25">{mint}</div>
              <ExternalLinks mint={mint} className="mt-1" />
              {lifecycle.mcap != null && (
                <div className="mt-0.5 text-[10px] text-white/40 font-mono">
                  MCap {mv(lifecycle.mcap) !== "n/a" ? (Number(lifecycle.mcap) >= 1_000_000 ? `$${(Number(lifecycle.mcap)/1_000_000).toFixed(2)}M` : `$${(Number(lifecycle.mcap)/1_000).toFixed(0)}K`) : "n/a"}
                </div>
              )}
            </div>
            <div className="flex items-center gap-3 text-xs text-white/50">
              {lifecycle.narrative ? (
                <span className="rounded bg-white/6 px-2 py-0.5 text-white/60">
                  {mv(lifecycle.narrative)}
                </span>
              ) : null}
              {lifecycle.strategy_profile ? (
                <span className="text-white/40">{mv(lifecycle.strategy_profile)}</span>
              ) : null}
            </div>
          </div>

          {/* DexScreener chart embed */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-black">
            <iframe
              src={`https://dexscreener.com/solana/${mint}?embed=1&theme=dark&info=0&trades=0`}
              className="h-[320px] w-full lg:h-[440px]"
              frameBorder="0"
              title="Price chart"
              allow="clipboard-write"
            />
          </div>

          {/* Bottom tab panel */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <div className="flex border-b border-white/6">
              {BOTTOM_TABS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setBottomTab(t.key)}
                  className={`px-4 py-2.5 text-xs font-medium transition-colors ${
                    bottomTab === t.key
                      ? "border-b-2 border-[var(--accent)] text-white"
                      : "text-white/35 hover:text-white/60"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>

            <div className="p-4">
              {bottomTab === "snapshot" && (
                <div className="space-y-3">
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {([
                      ["State",          lifecycle.state,            "Current lifecycle stage"],
                      ["Source",         lifecycle.source_primary,    "Where the scanner found this token"],
                      ["Narrative",      lifecycle.narrative,         "Matched meta/theme"],
                      ["Archetype",      lifecycle.archetype,         "Trading pattern archetype"],
                      ["Strategy",       lifecycle.strategy_profile,  "Assigned strategy profile"],
                      ["Last Score",     lifecycle.last_effective_score, "Most recent effective heat score"],
                    ] as [string, unknown, string][]).map(([label, val, _desc]) => val != null && (
                      <StatCell key={label} label={label} value={mv(val)} />
                    ))}
                  </div>
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {([
                      ["Launched",    lifecycle.launch_ts,     "Token launch timestamp"],
                      ["Migrated",    lifecycle.migration_ts,  "Migration to Raydium timestamp (if any)"],
                    ] as [string, unknown, string][]).map(([label, val, _desc]) => val != null && (
                      <StatCell key={label} label={label} value={typeof val === "number" ? fmtDate(val) : mv(val)} />
                    ))}
                  </div>
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {Object.entries(metrics).slice(0, 6).map(([k, v]) => (
                      <StatCell key={k} label={k.replaceAll("_", " ")} value={mv(v)} />
                    ))}
                  </div>
                </div>
              )}

              {bottomTab === "analysis" && (
                <div className="space-y-3">
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
                    <StatCell label="Strategy" value={analysis.strategy ?? "n/a"} />
                    <StatCell label="Exit Preset" value={analysis.exitPreset ?? "n/a"} />
                    <StatCell label="Confidence" value={analysis.confidence != null ? analysis.confidence.toFixed(2) : "n/a"} />
                    <StatCell label="Risk" value={analysis.risk ?? "n/a"} />
                  </div>
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
                    <StatCell label="Age Band" value={analysis.ageBand ?? "n/a"} />
                    <StatCell
                      label="Buy Ratio 5m"
                      value={analysis.buyRatio != null ? `${Math.round(Number(analysis.buyRatio) * 100)}%` : "n/a"}
                      positive={Number(analysis.buyRatio ?? 0) >= 0.6}
                    />
                    <StatCell
                      label="Score Slope"
                      value={analysis.scoreSlope != null ? Number(analysis.scoreSlope).toFixed(2) : "n/a"}
                    />
                    <StatCell
                      label="Buy Pressure"
                      value={metrics.buy_ratio_5m != null ? `${Math.round(Number(metrics.buy_ratio_5m) * 100)}%` : "n/a"}
                    />
                  </div>
                  {analysis.breakdown.length > 0 && (
                    <div className="space-y-1.5">
                      <div className="text-[10px] uppercase tracking-[0.18em] text-white/30">Score Factors</div>
                      {analysis.breakdown.map((row) => (
                        <div key={row.factor} className="flex items-start gap-3 rounded-lg border border-white/6 bg-black/20 px-3 py-2">
                          <div className="w-24 shrink-0 text-[10px] uppercase tracking-wider text-white/40">
                            {row.factor.replaceAll("_", " ")}
                          </div>
                          <div className="font-mono text-xs font-semibold text-white">
                            {mv(row.pts)}
                          </div>
                          <div className="text-xs text-white/50 leading-relaxed">
                            {mv(row.reason) !== "n/a" ? mv(row.reason) : "—"}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {bottomTab === "quality" && (
                <div className="space-y-3">
                  <div className="flex flex-wrap gap-2">
                    <StatusPill
                      label={analysis.alertBlocked ? "Alert blocked" : "Alert eligible"}
                      tone={analysis.alertBlocked ? "red" : "green"}
                    />
                    <StatusPill
                      label={analysis.autobuyBlocked ? "Auto-buy blocked" : "Auto-buy eligible"}
                      tone={analysis.autobuyBlocked ? "red" : "green"}
                    />
                    {analysis.forceScouted && <StatusPill label="Force scouted" tone="amber" />}
                  </div>
                  {analysis.reasons.length > 0 && (
                    <div className="space-y-1">
                      <div className="text-[10px] uppercase tracking-[0.18em] text-red-400/60">Hard blocks</div>
                      {analysis.reasons.map((r) => (
                        <div key={r} className="rounded-lg border border-red-400/15 bg-red-500/8 px-3 py-2 text-xs text-red-300">
                          {r}
                        </div>
                      ))}
                    </div>
                  )}
                  {analysis.forceReasons.length > 0 && (
                    <div className="space-y-1">
                      <div className="text-[10px] uppercase tracking-[0.18em] text-amber-400/60">Scout-only</div>
                      {analysis.forceReasons.map((r) => (
                        <div key={r} className="rounded-lg border border-amber-400/15 bg-amber-500/8 px-3 py-2 text-xs text-amber-200">
                          {r}
                        </div>
                      ))}
                    </div>
                  )}
                  {!analysis.reasons.length && !analysis.forceReasons.length && (
                    <div className="text-xs text-white/30">No blocking reasons — token passes the quality gate.</div>
                  )}
                </div>
              )}

              {bottomTab === "history" && (
                <div className="space-y-1.5">
                  {scoreEvents.length ? scoreEvents.map((ev, idx) => (
                    <div
                      key={`${ev.ts || idx}`}
                      className="flex items-center gap-4 rounded-lg border border-white/6 bg-black/20 px-3 py-2 text-xs"
                    >
                      <span className={`w-8 font-mono font-bold tabular-nums ${
                        (ev.effective ?? ev.score ?? 0) >= 75 ? "text-emerald-400"
                        : (ev.effective ?? ev.score ?? 0) >= 60 ? "text-amber-400"
                        : "text-white/50"
                      }`}>
                        {ev.effective ?? ev.score ?? "—"}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-white/40">{ev.strategy}</span>
                      <span className="w-12 text-white/30">{ev.narrative}</span>
                      <span className="ml-auto text-[10px] text-white/20">{fmtRelTime(ev.ts)}</span>
                    </div>
                  )) : (
                    <div className="text-xs text-white/30">No score history recorded yet.</div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* ── RIGHT: buy panel + stats ── */}
        <div className="flex flex-col gap-3">
          {/* Buy panel */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <div className="border-b border-white/6 px-4 py-2.5">
              <div className="flex gap-1">
                {(["paper", "live"] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setTradeMode(m)}
                    className={`flex-1 rounded py-1.5 text-xs font-semibold uppercase tracking-wider transition-colors ${
                      tradeMode === m
                        ? m === "live"
                          ? "bg-red-500/25 text-red-200"
                          : "bg-emerald-500/20 text-emerald-300"
                        : "text-white/25 hover:text-white/50"
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>
            <div className="p-4 space-y-3">
              <div className="grid grid-cols-2 gap-1.5">
                {["0.02", "0.05", "0.1", "0.25"].map((amt) => (
                  <button
                    key={amt}
                    type="button"
                    onClick={() => setBuyAmount(amt)}
                    className={`rounded py-1.5 text-xs font-mono ${
                      buyAmount === amt
                        ? "bg-[var(--accent)]/20 text-[var(--accent)]"
                        : "bg-white/5 text-white/40 hover:bg-white/10"
                    }`}
                  >
                    {amt} SOL
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2">
                <input
                  value={buyAmount}
                  onChange={(e) => setBuyAmount(e.target.value)}
                  className="flex-1 rounded-lg border border-white/10 bg-black/40 px-3 py-2 text-sm font-mono text-white outline-none"
                  placeholder="SOL amount"
                />
                <span className="text-xs text-white/30">SOL</span>
              </div>
              <button
                type="button"
                onClick={quickBuy}
                disabled={!uid || buying}
                className={`w-full rounded-lg py-2.5 text-sm font-semibold disabled:opacity-40 ${
                  tradeMode === "live"
                    ? "bg-red-500/25 text-red-200 hover:bg-red-500/35"
                    : "bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30"
                }`}
              >
                {buying ? "Submitting…" : uid ? `${tradeMode === "paper" ? "Paper" : "Live"} Buy` : "Set UID to buy"}
              </button>
              {buyMsg && (
                <div className="rounded-lg bg-white/5 px-3 py-2 text-xs text-white/60">
                  {buyMsg}
                </div>
              )}
            </div>
          </div>

          {/* Auto-Buy preview */}
          {autobuyPreview && (
            <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
              <div className="border-b border-white/6 px-4 py-2 text-[10px] uppercase tracking-[0.18em] text-white/30">
                Auto-Buy Preview
              </div>
              <div className="p-4 space-y-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-white/40">Status</span>
                  <span className={autobuyPreview.eligible ? "text-emerald-300" : "text-white/50"}>
                    {autobuyPreview.eligible ? "Would buy" : mv(autobuyPreview.status || "blocked")}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-white/40">Confidence</span>
                  <span className="font-mono text-white">{mv(autobuyPreview.confidence ?? "n/a")}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-white/40">Size</span>
                  <span className="font-mono text-white">{mv(autobuyPreview.sol_amount ?? "n/a")} SOL</span>
                </div>
                {autobuyPreview.block_reason ? (
                  <div className="mt-2 rounded-lg border border-red-400/15 bg-red-500/8 px-3 py-2 text-red-300 leading-relaxed">
                    {mv(autobuyPreview.block_reason)}
                  </div>
                ) : null}
              </div>
            </div>
          )}

          {/* Token stats */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <div className="border-b border-white/6 px-4 py-2 text-[10px] uppercase tracking-[0.18em] text-white/30">
              Metrics
            </div>
            <div className="p-4 space-y-1.5 text-xs">
              {[
                ["Score", mv(analysis.effectiveScore ?? lifecycle.last_effective_score ?? "n/a")],
                ["Strategy", mv(analysis.strategy ?? lifecycle.strategy_profile ?? "n/a")],
                ["Confidence", analysis.confidence != null ? analysis.confidence.toFixed(2) : "n/a"],
                ["Liquidity", metrics.liquidity_usd != null ? `$${Number(metrics.liquidity_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "n/a"],
                ["Buy Ratio 5m", metrics.buy_ratio_5m != null ? `${Math.round(Number(metrics.buy_ratio_5m) * 100)}%` : "n/a"],
                ["Score Slope", metrics.score_slope != null ? Number(metrics.score_slope).toFixed(2) : "n/a"],
                ["Peak Score", mv(metrics.peak_score ?? "n/a")],
                ["Holder Conc.", metrics.holder_concentration != null ? `${(Number(metrics.holder_concentration) * 100).toFixed(1)}%` : "n/a"],
              ].map(([label, val]) => {
              let valColor = "text-white/80";
              if (label === "Buy Ratio 5m" && metrics.buy_ratio_5m != null) {
                const r = Number(metrics.buy_ratio_5m);
                valColor = r >= 0.65 ? "text-emerald-400" : r >= 0.5 ? "text-amber-400" : "text-red-400/70";
              }
              return (
                <div key={label} className="flex items-center justify-between gap-2">
                  <span className="text-white/35">{label}</span>
                  <span className={`font-mono ${valColor}`}>{val}</span>
                </div>
              );
            })}
            </div>
          </div>

          {/* State + source */}
          <div className="overflow-hidden rounded-2xl border border-white/8 bg-[#080e14]">
            <div className="border-b border-white/6 px-4 py-2 text-[10px] uppercase tracking-[0.18em] text-white/30">
              Lifecycle
            </div>
            <div className="p-4 space-y-1.5 text-xs">
              {[
                ["State", mv(lifecycle.state ?? "n/a")],
                ["Source", mv(lifecycle.source_primary ?? "n/a")],
                ["Narrative", mv(lifecycle.narrative ?? "Other")],
                ["Archetype", mv(lifecycle.archetype ?? "NONE")],
              ].map(([label, val]) => (
                <div key={label} className="flex items-center justify-between gap-2">
                  <span className="text-white/35">{label}</span>
                  <span className="text-white/70">{val}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function StatCell({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return (
    <div className="rounded-lg border border-white/6 bg-black/20 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-white/30">{label}</div>
      <div className={`mt-1 text-sm font-medium ${positive ? "text-emerald-300" : "text-white"}`}>{value}</div>
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: "green" | "red" | "amber" }) {
  const cls =
    tone === "green" ? "border-emerald-400/20 bg-emerald-500/10 text-emerald-200"
    : tone === "red" ? "border-red-400/20 bg-red-500/10 text-red-200"
    : "border-amber-400/20 bg-amber-500/10 text-amber-100";
  return <div className={`rounded-full border px-3 py-1 text-xs ${cls}`}>{label}</div>;
}
