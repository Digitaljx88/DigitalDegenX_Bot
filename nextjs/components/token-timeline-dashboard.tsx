"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

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

function formatDate(ts?: number) {
  if (!ts) return "n/a";
  return new Date(ts * 1000).toLocaleString();
}

function metricValue(value: unknown) {
  if (typeof value === "number") {
    if (Math.abs(value) >= 1000) return value.toLocaleString();
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value ?? "n/a");
}

export function TokenTimelineDashboard({ mint }: { mint: string }) {
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const [snapshotData, timelineData] = await Promise.all([
          apiFetch<SnapshotResponse>(`/token/${mint}/snapshot`),
          apiFetch<TimelineResponse>(`/token/${mint}/timeline`),
        ]);
        setSnapshot(snapshotData);
        setTimeline(timelineData.events || []);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load token timeline");
      }
    }
    void load();
  }, [mint]);

  const highlights = useMemo(() => {
    if (!snapshot) return [];
    const lifecycle = snapshot.lifecycle || {};
    const metrics = snapshot.metrics || {};
    const analysis = snapshot.analysis || {};
    return [
      { label: "State", value: lifecycle.state },
      { label: "Source", value: lifecycle.source_primary },
      { label: "Narrative", value: lifecycle.narrative || "Other" },
      { label: "Archetype", value: lifecycle.archetype || "NONE" },
      { label: "Strategy", value: lifecycle.strategy_profile || "n/a" },
      { label: "Score", value: analysis.effective_score ?? lifecycle.last_effective_score ?? lifecycle.last_score ?? "n/a" },
      { label: "Buy Ratio 5m", value: metrics.buy_ratio_5m ?? "n/a" },
      { label: "Liquidity", value: metrics.liquidity_usd ?? "n/a" },
      { label: "Last Trade", value: formatDate(typeof lifecycle.last_trade_ts === "number" ? lifecycle.last_trade_ts : undefined) },
    ];
  }, [snapshot]);

  const qualitySummary = useMemo(() => {
    const analysis = snapshot?.analysis || {};
    const entryQuality = (analysis.entry_quality || {}) as Record<string, unknown>;
    const qualityFlags = (analysis.quality_flags || {}) as Record<string, unknown>;
    return {
      strategy: analysis.strategy_profile,
      confidence: analysis.strategy_confidence,
      exitPreset: analysis.strategy_exit_preset,
      risk: analysis.risk,
      alertBlocked: qualityFlags.alert_blocked,
      autobuyBlocked: qualityFlags.autobuy_blocked,
      forceScouted: qualityFlags.force_scouted,
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

  const breakdownRows = useMemo(() => {
    const breakdown = (snapshot?.analysis?.breakdown || {}) as Record<string, unknown>;
    return Object.entries(breakdown).map(([key, value]) => {
      const tuple = Array.isArray(value) ? value : [];
      return {
        factor: key,
        pts: tuple[0],
        reason: tuple[1],
      };
    });
  }, [snapshot]);

  const scoreEvents = useMemo(() => {
    return timeline
      .filter((event) => event.event_type === "score_update")
      .map((event) => {
        const payload = event.payload || {};
        return {
          ts: event.ts,
          score: typeof payload.last_score === "number" ? payload.last_score : null,
          effective: typeof payload.last_effective_score === "number" ? payload.last_effective_score : null,
          confidence: typeof payload.last_confidence === "number" ? payload.last_confidence : null,
          strategy: typeof payload.strategy_profile === "string" ? payload.strategy_profile : "n/a",
          narrative: typeof payload.narrative === "string" ? payload.narrative : "Other",
          archetype: typeof payload.archetype === "string" ? payload.archetype : "NONE",
        };
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
  }, [timeline]);

  const scoreSummary = useMemo(() => {
    if (!scoreEvents.length) {
      return {
        latest: null,
        peak: null,
        transitions: 0,
        confidencePeak: null,
      };
    }
    const latest = scoreEvents[0];
    const chronological = [...scoreEvents].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    let transitions = 0;
    for (let i = 1; i < chronological.length; i += 1) {
      if (chronological[i].strategy !== chronological[i - 1].strategy) {
        transitions += 1;
      }
    }
    const peak = scoreEvents.reduce((best, row) => {
      const score = row.effective ?? row.score ?? -Infinity;
      const bestScore = best ? (best.effective ?? best.score ?? -Infinity) : -Infinity;
      return score > bestScore ? row : best;
    }, null as (typeof scoreEvents)[number] | null);
    const confidencePeak = scoreEvents.reduce((best, row) => {
      const conf = row.confidence ?? -Infinity;
      const bestConf = best ? (best.confidence ?? -Infinity) : -Infinity;
      return conf > bestConf ? row : best;
    }, null as (typeof scoreEvents)[number] | null);
    return { latest, peak, transitions, confidencePeak };
  }, [scoreEvents]);

  return (
    <div className="space-y-6">
      <Panel title="Token Snapshot" subtitle={`Lifecycle state for ${mint}`}>
        {error ? <div className="rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        {!snapshot && !error ? <div className="text-sm text-[var(--muted-foreground)]">Loading token snapshot...</div> : null}
        {snapshot ? (
          <div className="space-y-5">
            <div className="grid gap-3 md:grid-cols-3">
              {highlights.map((item) => (
                <div key={item.label} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">{item.label}</div>
                  <div className="mt-2 text-sm font-medium text-white">{metricValue(item.value)}</div>
                </div>
              ))}
            </div>
            <div className="grid gap-5 lg:grid-cols-2">
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="mb-3 text-sm font-semibold text-white">Lifecycle</div>
                <div className="space-y-2 text-sm">
                  {Object.entries(snapshot.lifecycle || {}).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[160px_1fr] gap-3">
                      <div className="text-[var(--muted-foreground)]">{key}</div>
                      <div className="text-white">{metricValue(value)}</div>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="mb-3 text-sm font-semibold text-white">Trade Metrics</div>
                <div className="space-y-2 text-sm">
                  {Object.entries(snapshot.metrics || {}).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[160px_1fr] gap-3">
                      <div className="text-[var(--muted-foreground)]">{key}</div>
                      <div className="text-white">{metricValue(value)}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </Panel>

      <Panel title="Operator Analysis" subtitle="Lifecycle-backed scoring and quality gating, intended to replace token analysis in Telegram.">
        {snapshot?.analysis ? (
          <div className="space-y-5">
            {"error" in snapshot.analysis ? (
              <div className="rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">
                {String(snapshot.analysis.error)}
              </div>
            ) : (
              <>
                <div className="grid gap-3 md:grid-cols-4">
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Strategy</div>
                    <div className="mt-2 text-sm font-semibold text-white">{metricValue(qualitySummary.strategy || "n/a")}</div>
                    <div className="mt-1 text-xs text-[var(--muted-foreground)]">exit {metricValue(qualitySummary.exitPreset || "n/a")}</div>
                  </div>
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Confidence</div>
                    <div className="mt-2 text-sm font-semibold text-white">{metricValue(qualitySummary.confidence ?? "n/a")}</div>
                    <div className="mt-1 text-xs text-[var(--muted-foreground)]">risk {metricValue(qualitySummary.risk || "n/a")}</div>
                  </div>
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Quality State</div>
                    <div className="mt-2 text-sm font-semibold text-white">
                      {qualitySummary.alertBlocked ? "Alert Blocked" : qualitySummary.forceScouted ? "Scout Only" : "Alert Eligible"}
                    </div>
                    <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                      {qualitySummary.autobuyBlocked ? "Auto-buy blocked" : "Auto-buy eligible"}
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Snapshot Gate</div>
                    <div className="mt-2 text-sm font-semibold text-white">{metricValue(qualitySummary.ageBand || "n/a")}</div>
                    <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                      buy {metricValue(qualitySummary.buyRatio ?? "n/a")} · slope {metricValue(qualitySummary.scoreSlope ?? "n/a")}
                    </div>
                  </div>
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
                          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Auto-buy only blockers</div>
                          <ul className="space-y-2 text-white/80">
                            {qualitySummary.autobuyOnly.map((reason) => (
                              <li key={reason} className="rounded-xl border border-white/10 bg-white/5 px-3 py-2">{reason}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {!qualitySummary.reasons.length && !qualitySummary.forceReasons.length && !qualitySummary.autobuyOnly.length ? (
                        <div className="text-[var(--muted-foreground)]">No blocking reasons recorded. This token currently passes the quality gate.</div>
                      ) : null}
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="mb-3 text-sm font-semibold text-white">Factor Breakdown</div>
                    <div className="space-y-2 text-sm">
                      {breakdownRows.length ? breakdownRows.map((row) => (
                        <div key={row.factor} className="rounded-xl border border-white/8 bg-black/10 px-3 py-3">
                          <div className="flex items-center justify-between gap-3">
                            <div className="font-medium text-white">{row.factor.replaceAll("_", " ")}</div>
                            <div className="text-xs uppercase tracking-[0.14em] text-[var(--muted-foreground)]">{metricValue(row.pts)} pts</div>
                          </div>
                          <div className="mt-1 text-xs text-[var(--muted-foreground)]">{metricValue(row.reason)}</div>
                        </div>
                      )) : (
                        <div className="text-[var(--muted-foreground)]">No score breakdown available yet for this token.</div>
                      )}
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">No lifecycle-backed analysis available yet for this token.</div>
        )}
      </Panel>

      <Panel title="Score Transitions" subtitle="Score, confidence, and strategy changes recorded for this token over time.">
        {scoreEvents.length ? (
          <div className="space-y-5">
            <div className="grid gap-3 md:grid-cols-4">
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Latest Effective</div>
                <div className="mt-2 text-lg font-semibold text-white">
                  {metricValue(scoreSummary.latest?.effective ?? scoreSummary.latest?.score ?? "n/a")}
                </div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {scoreSummary.latest ? formatDate(scoreSummary.latest.ts) : "n/a"}
                </div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Peak Effective</div>
                <div className="mt-2 text-lg font-semibold text-white">
                  {metricValue(scoreSummary.peak?.effective ?? scoreSummary.peak?.score ?? "n/a")}
                </div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {scoreSummary.peak ? scoreSummary.peak.strategy : "n/a"}
                </div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Strategy Shifts</div>
                <div className="mt-2 text-lg font-semibold text-white">{scoreSummary.transitions}</div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">Across recorded score updates</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
                <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Confidence Peak</div>
                <div className="mt-2 text-lg font-semibold text-white">
                  {metricValue(scoreSummary.confidencePeak?.confidence ?? "n/a")}
                </div>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {scoreSummary.confidencePeak ? scoreSummary.confidencePeak.strategy : "n/a"}
                </div>
              </div>
            </div>
            <div className="space-y-3">
              {scoreEvents.map((event, index) => {
                const previous = scoreEvents[index + 1];
                const delta = previous
                  ? (event.effective ?? event.score ?? 0) - (previous.effective ?? previous.score ?? 0)
                  : null;
                return (
                  <div key={`${event.ts || index}-${event.strategy}-${index}`} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                    <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                      <div>
                        <div className="text-sm font-semibold text-white">{event.strategy}</div>
                        <div className="text-xs text-[var(--muted-foreground)]">
                          {event.narrative} · {event.archetype}
                        </div>
                      </div>
                      <div className="text-xs text-[var(--muted-foreground)]">{formatDate(event.ts)}</div>
                    </div>
                    <div className="mt-3 grid gap-3 md:grid-cols-4">
                      <div>
                        <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Effective</div>
                        <div className="mt-1 text-sm font-medium text-white">{metricValue(event.effective ?? event.score ?? "n/a")}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Raw</div>
                        <div className="mt-1 text-sm font-medium text-white">{metricValue(event.score ?? "n/a")}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Confidence</div>
                        <div className="mt-1 text-sm font-medium text-white">{metricValue(event.confidence ?? "n/a")}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground)]">Delta</div>
                        <div className="mt-1 text-sm font-medium text-white">
                          {delta === null ? "n/a" : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}`}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">No score transitions recorded yet for this mint.</div>
        )}
      </Panel>

      <Panel title="Lifecycle Timeline" subtitle="Launch, trade flow, migration, and scoring events for this mint.">
        <div className="space-y-3">
          {timeline.length ? timeline.map((event, index) => (
            <div key={`${event.id || event.ts || index}-${event.event_type || index}`} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="flex flex-col gap-1 md:flex-row md:items-center md:justify-between">
                <div className="text-sm font-semibold text-white">{event.event_type || "event"}</div>
                <div className="text-xs text-[var(--muted-foreground)]">{formatDate(event.ts)}</div>
              </div>
              <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-xs text-[var(--muted-foreground)]">
                {JSON.stringify(event.payload || {}, null, 2)}
              </pre>
            </div>
          )) : <div className="text-sm text-[var(--muted-foreground)]">No timeline events recorded yet for this mint.</div>}
        </div>
      </Panel>
    </div>
  );
}
