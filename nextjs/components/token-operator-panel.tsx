"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type SnapshotResponse = {
  mint: string;
  lifecycle: Record<string, unknown>;
  metrics: Record<string, unknown>;
  enrichment: Record<string, unknown>;
  trading_snapshot?: Record<string, unknown> | null;
  analysis?: Record<string, unknown> | null;
  events: Array<Record<string, unknown>>;
};

type TimelineEvent = {
  id?: number;
  event_type?: string;
  ts?: number;
  payload?: Record<string, unknown>;
};

type TimelineResponse = {
  mint: string;
  events: TimelineEvent[];
};

function metricValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "n/a";
  if (typeof value === "number") {
    if (Math.abs(value) >= 1000) return value.toLocaleString();
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function formatDate(ts?: number) {
  if (!ts) return "n/a";
  return new Date(ts * 1000).toLocaleString();
}

function StatusPill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "good" | "warn" | "bad" | "neutral";
}) {
  const toneClass =
    tone === "good"
      ? "border-emerald-400/20 bg-emerald-500/10 text-emerald-100"
      : tone === "warn"
        ? "border-amber-400/20 bg-amber-500/10 text-amber-100"
        : tone === "bad"
          ? "border-red-400/20 bg-red-500/10 text-red-100"
          : "border-white/10 bg-white/5 text-white/80";
  return <span className={`rounded-full border px-3 py-1 text-[11px] ${toneClass}`}>{label}</span>;
}

function SummaryStat({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">{label}</div>
      <div className="mt-2 text-sm font-semibold text-white">{metricValue(value)}</div>
    </div>
  );
}

export function TokenOperatorPanel({
  mint,
  label = "Inspect",
}: {
  mint: string;
  label?: string;
}) {
  const { uid } = useActiveUid();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);

  useEffect(() => {
    if (!open || snapshot) return;
    if (!uid) {
      setError("Set your Telegram UID to load token analysis.");
      return;
    }
    async function load() {
      try {
        setLoading(true);
        const [snapshotData, timelineData] = await Promise.all([
          apiFetch<SnapshotResponse>(`/token/${mint}/snapshot`, { query: { uid: uid || undefined } }),
          apiFetch<TimelineResponse>(`/token/${mint}/timeline`),
        ]);
        setSnapshot(snapshotData);
        setTimeline(timelineData.events || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load token detail");
      } finally {
        setLoading(false);
      }
    }
    void load();
  }, [mint, open, snapshot, uid]);

  const qualitySummary = useMemo(() => {
    const analysis = snapshot?.analysis || {};
    const entryQuality = (analysis.entry_quality || {}) as Record<string, unknown>;
    const qualityFlags = (analysis.quality_flags || {}) as Record<string, unknown>;
    return {
      strategy: analysis.strategy_profile,
      confidence: analysis.strategy_confidence,
      exitPreset: analysis.strategy_exit_preset,
      risk: analysis.risk,
      alertBlocked: Boolean(qualityFlags.alert_blocked),
      autobuyBlocked: Boolean(qualityFlags.autobuy_blocked),
      forceScouted: Boolean(qualityFlags.force_scouted),
      reasons: (qualityFlags.quality_reasons || []) as string[],
      forceReasons: (qualityFlags.force_scouted_reasons || []) as string[],
      autobuyOnly: (qualityFlags.autobuy_only_reasons || []) as string[],
      ageBand: entryQuality.age_band,
      buyRatio: entryQuality.buy_ratio_5m,
      liqDrop: entryQuality.liquidity_drop_pct,
      scoreSlope: entryQuality.score_slope,
      holderPct: entryQuality.holder_concentration_pct,
    };
  }, [snapshot]);

  const scoreEvents = useMemo(() => {
    return timeline
      .filter((event) => event.event_type === "score_update")
      .map((event) => {
        const payload = event.payload || {};
        return {
          ts: event.ts,
          effective: typeof payload.last_effective_score === "number" ? payload.last_effective_score : null,
          confidence: typeof payload.last_confidence === "number" ? payload.last_confidence : null,
          strategy: typeof payload.strategy_profile === "string" ? payload.strategy_profile : "n/a",
        };
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0))
      .slice(0, 6);
  }, [timeline]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)] transition hover:border-white/20 hover:text-white"
      >
        {label}
      </button>

      {open ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
          <div className="max-h-[90vh] w-full max-w-5xl overflow-y-auto rounded-[28px] border border-white/10 bg-[#08111a] p-6 shadow-[0_24px_80px_rgba(0,0,0,0.45)]">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground)]">Operator Detail</div>
                <div className="mt-2 text-2xl font-semibold text-white">{mint}</div>
              </div>
              <div className="flex items-center gap-3">
                <Link
                  href={`/token/${mint}`}
                  className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)] transition hover:border-white/20 hover:text-white"
                >
                  Full token page
                </Link>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)] transition hover:border-white/20 hover:text-white"
                >
                  Close
                </button>
              </div>
            </div>

            {loading ? <div className="text-sm text-[var(--muted-foreground)]">Loading lifecycle detail...</div> : null}
            {error ? <div className="rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}

            {snapshot ? (
              <div className="space-y-6">
                <div className="grid gap-3 md:grid-cols-4">
                  <SummaryStat label="State" value={snapshot.lifecycle?.state} />
                  <SummaryStat label="Source" value={snapshot.lifecycle?.source_primary} />
                  <SummaryStat label="Strategy" value={qualitySummary.strategy || snapshot.lifecycle?.strategy_profile} />
                  <SummaryStat label="Confidence" value={qualitySummary.confidence ?? snapshot.lifecycle?.last_confidence} />
                  <SummaryStat label="Effective score" value={snapshot.analysis?.effective_score ?? snapshot.lifecycle?.last_effective_score ?? snapshot.lifecycle?.last_score} />
                  <SummaryStat label="Buy ratio 5m" value={snapshot.metrics?.buy_ratio_5m ?? qualitySummary.buyRatio} />
                  <SummaryStat label="Liquidity delta" value={snapshot.metrics?.liquidity_delta_pct ?? qualitySummary.liqDrop} />
                  <SummaryStat label="Last trade" value={formatDate(typeof snapshot.lifecycle?.last_trade_ts === "number" ? (snapshot.lifecycle.last_trade_ts as number) : undefined)} />
                </div>

                <div className="flex flex-wrap gap-2">
                  <StatusPill label={qualitySummary.alertBlocked ? "Alert blocked" : "Alert eligible"} tone={qualitySummary.alertBlocked ? "bad" : "good"} />
                  <StatusPill label={qualitySummary.autobuyBlocked ? "Auto-buy blocked" : "Auto-buy eligible"} tone={qualitySummary.autobuyBlocked ? "bad" : "good"} />
                  {qualitySummary.forceScouted ? <StatusPill label="Scout only" tone="warn" /> : null}
                  <StatusPill label={`Risk ${metricValue(qualitySummary.risk || "n/a")}`} tone="neutral" />
                  <StatusPill label={`Exit ${metricValue(qualitySummary.exitPreset || "n/a")}`} tone="neutral" />
                </div>

                <div className="grid gap-5 lg:grid-cols-2">
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="mb-3 text-sm font-semibold text-white">Quality Reasons</div>
                    <div className="space-y-3 text-sm">
                      {qualitySummary.reasons.length ? (
                        <div>
                          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Hard blocks</div>
                          <ul className="space-y-2 text-red-100">
                            {qualitySummary.reasons.map((reason) => (
                              <li key={reason} className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2">{reason}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {qualitySummary.forceReasons.length ? (
                        <div>
                          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Scout-only reasons</div>
                          <ul className="space-y-2 text-amber-100">
                            {qualitySummary.forceReasons.map((reason) => (
                              <li key={reason} className="rounded-xl border border-amber-400/20 bg-amber-500/10 px-3 py-2">{reason}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {qualitySummary.autobuyOnly.length ? (
                        <div>
                          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Auto-buy blockers</div>
                          <ul className="space-y-2 text-white/80">
                            {qualitySummary.autobuyOnly.map((reason) => (
                              <li key={reason} className="rounded-xl border border-white/10 bg-white/5 px-3 py-2">{reason}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {!qualitySummary.reasons.length && !qualitySummary.forceReasons.length && !qualitySummary.autobuyOnly.length ? (
                        <div className="text-[var(--muted-foreground)]">No quality blockers recorded.</div>
                      ) : null}
                    </div>
                  </div>

                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="mb-3 text-sm font-semibold text-white">Recent Score Transitions</div>
                    <div className="space-y-2 text-sm">
                      {scoreEvents.length ? scoreEvents.map((event, index) => (
                        <div key={`${event.ts ?? index}-${index}`} className="rounded-xl border border-white/8 bg-black/10 px-3 py-3">
                          <div className="flex items-center justify-between gap-3">
                            <div className="font-medium text-white">{metricValue(event.strategy)}</div>
                            <div className="text-xs text-[var(--muted-foreground)]">{formatDate(event.ts)}</div>
                          </div>
                          <div className="mt-2 grid gap-2 text-xs text-[var(--muted-foreground)] md:grid-cols-2">
                            <div>Effective score: {metricValue(event.effective)}</div>
                            <div>Confidence: {metricValue(event.confidence)}</div>
                          </div>
                        </div>
                      )) : (
                        <div className="text-[var(--muted-foreground)]">No score transition history recorded yet.</div>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
