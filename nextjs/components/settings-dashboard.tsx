"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type AutoBuyConfig = {
  enabled?: boolean;
  sol_amount?: number;
  max_sol_amount?: number;
  min_confidence?: number;
  confidence_scale_enabled?: boolean;
  min_score?: number;
  max_mcap?: number;
  min_mcap_usd?: number;
  daily_limit_sol?: number;
  spent_today?: number;
  max_positions?: number;
  max_narrative_exposure?: number;
  max_archetype_exposure?: number;
  buy_tier?: string;
  min_liquidity_usd?: number;
  max_liquidity_usd?: number;
  min_age_mins?: number;
  max_age_mins?: number;
  min_txns_5m?: number;
};

type SettingsResponse = {
  uid: number;
  settings: Record<string, number | string | boolean>;
};

type ModeResponse = {
  uid: number;
  mode: "paper" | "live";
};

type PresetRow = { mult: number; sell_pct: number };

type TradeControls = {
  uid: number;
  presets_enabled: boolean;
  presets: PresetRow[];
  global_stop_loss: { enabled?: boolean; pct?: number; sell_pct?: number };
  global_trailing_stop: { enabled?: boolean; trail_pct?: number; sell_pct?: number };
  global_trailing_tp: { enabled?: boolean; activate_mult?: number; trail_pct?: number; sell_pct?: number };
  global_breakeven_stop: { enabled?: boolean; activate_mult?: number };
  global_time_exit: { enabled?: boolean; hours?: number; target_mult?: number; sell_pct?: number };
};

type AutoBuyActivityRow = {
  id: number; ts: number; mint?: string; symbol?: string; name?: string;
  score?: number; effective_score?: number; mcap?: number;
  strategy_profile?: string; confidence?: number; sol_amount?: number;
  size_multiplier?: number; mode?: string;
  status: "executed" | "blocked" | "failed";
  block_reason?: string; block_category?: string;
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

/* ── helpers ── */
function Divider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 py-1">
      <div className="h-px flex-1 bg-white/8" />
      <span className="text-[10px] uppercase tracking-[0.2em] text-white/25">{label}</span>
      <div className="h-px flex-1 bg-white/8" />
    </div>
  );
}

function FieldRow({
  label, help, children,
}: { label: string; help: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-white/70">{label}</div>
      <div>{children}</div>
      <div className="text-[11px] text-white/30 leading-relaxed">{help}</div>
    </div>
  );
}

function NumInput({
  value, onChange, placeholder, step,
}: { value: string; onChange: (v: string) => void; placeholder?: string; step?: string }) {
  return (
    <input
      type="number"
      step={step ?? "any"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2.5 text-sm text-white outline-none focus:border-white/25"
    />
  );
}

export function SettingsDashboard() {
  const { uid } = useActiveUid();
  const [autobuy, setAutobuy] = useState<AutoBuyConfig>({});
  const [settings, setSettings] = useState<Record<string, number | string | boolean>>({});
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [tradeControls, setTradeControls] = useState<TradeControls | null>(null);
  const [autobuyActivity, setAutobuyActivity] = useState<AutoBuyActivityResponse | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) { setAutobuy({}); setSettings({}); setTradeControls(null); return; }
      try {
        const [abRes, settingsRes, modeRes, tcRes] = await Promise.all([
          apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
          apiFetch<SettingsResponse>(`/settings/${uid}`),
          apiFetch<ModeResponse>(`/mode`, { query: { uid } }),
          apiFetch<TradeControls>(`/trade-controls/${uid}`),
        ]);
        setAutobuy(abRes);
        setSettings(settingsRes.settings || {});
        setMode(modeRes.mode || "paper");
        setTradeControls(tcRes);
        setError("");
      } catch (err) { setError(err instanceof Error ? err.message : "Failed to load settings"); }
    }
    load();
  }, [uid]);

  useEffect(() => {
    async function loadActivity() {
      if (!uid) { setAutobuyActivity(null); return; }
      try {
        const r = await apiFetch<AutoBuyActivityResponse>(`/autobuy/activity/${uid}`, { query: { limit: 8 } });
        setAutobuyActivity(r);
      } catch { setAutobuyActivity(null); }
    }
    loadActivity();
  }, [uid, autobuy, mode]);

  function ab(field: keyof AutoBuyConfig) { return String(autobuy[field] ?? ""); }
  function setAb(field: keyof AutoBuyConfig, value: string | boolean | number) {
    setAutobuy((c) => ({ ...c, [field]: value }));
  }
  function setAbNum(field: keyof AutoBuyConfig, value: string) {
    setAutobuy((c) => ({ ...c, [field]: value === "" ? 0 : Number(value) }));
  }

  async function saveMode(nextMode: "paper" | "live") {
    try {
      const r = await apiFetch<ModeResponse>("/mode", { method: "POST", body: JSON.stringify({ uid, mode: nextMode }) });
      setMode(r.mode || nextMode);
      setMessage(`Mode set to ${r.mode || nextMode}.`);
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to update mode"); }
  }

  async function resetPaperWallet() {
    if (!uid) return;
    try {
      await apiFetch(`/portfolio/reset`, { method: "POST", body: JSON.stringify({ uid }) });
      setMessage("Paper wallet reset.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to reset"); }
  }

  async function saveAutobuy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await apiFetch(`/autobuy/${uid}`, { method: "POST", body: JSON.stringify(autobuy) });
      setMessage("Auto-buy settings saved.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save auto-buy"); }
  }

  async function saveThresholds(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Convert all settings values to numbers
    const payload = Object.fromEntries(
      Object.entries(settings).map(([k, v]) => [k, typeof v === "boolean" ? v : Number(v || 0)])
    );
    const thresholdKeys = ["alert_ultra_hot_threshold","alert_hot_threshold","alert_warm_threshold","alert_scouted_threshold"];
    for (const k of thresholdKeys) {
      const v = Number(payload[k] || 0);
      if (v < 0 || v > 100) {
        setError(`${k.replace("alert_","").replace("_threshold","")} threshold must be between 0 and 100.`);
        return;
      }
    }
    try {
      await apiFetch(`/settings/${uid}`, { method: "POST", body: JSON.stringify({ settings: payload }) });
      setMessage("Heat score settings saved.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save heat score settings"); }
  }

  async function saveTradeControls(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tradeControls) return;
    try {
      await apiFetch(`/trade-controls/${uid}`, {
        method: "POST",
        body: JSON.stringify({
          presets_enabled: tradeControls.presets_enabled,
          presets: tradeControls.presets,
          global_stop_loss: tradeControls.global_stop_loss,
          global_trailing_stop: tradeControls.global_trailing_stop,
          global_trailing_tp: tradeControls.global_trailing_tp,
          global_breakeven_stop: tradeControls.global_breakeven_stop,
          global_time_exit: tradeControls.global_time_exit,
        }),
      });
      setMessage("Trade controls saved.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save trade controls"); }
  }

  if (!uid) {
    return (
      <Panel title="Settings" subtitle="Set your Telegram UID to edit auto-buy controls and thresholds.">
        <div className="text-sm text-[var(--muted-foreground)]">
          Add your Telegram UID in the top bar to edit settings from the browser.
        </div>
      </Panel>
    );
  }

  return (
    <div className="space-y-6">
      {message && <div className="rounded-xl border border-emerald-400/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">{message}</div>}
      {error && <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-4 py-3 text-sm text-red-200">{error}</div>}

      {/* ── Mode ── */}
      <Panel title="Mode" subtitle="Controls which trading path the bot treats as active.">
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-sm text-white/50">Current:</span>
            <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wider ${mode === "live" ? "bg-red-500/20 text-red-200" : "bg-emerald-500/15 text-emerald-300"}`}>
              {mode === "live" ? "Live" : "Paper"}
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => saveMode("paper")} className="rounded-xl bg-emerald-500/15 px-4 py-2 text-sm font-medium text-emerald-300 hover:bg-emerald-500/25">Use Paper</button>
            <button type="button" onClick={() => saveMode("live")} className="rounded-xl border border-red-400/20 bg-red-500/10 px-4 py-2 text-sm text-red-200 hover:bg-red-500/20">Use Live</button>
            <button type="button" onClick={() => { if (window.confirm("Reset paper wallet? This clears all positions and restores the starting balance.")) resetPaperWallet(); }} className="rounded-xl border border-white/10 px-4 py-2 text-sm text-white/40 hover:text-white/70">Reset Paper Wallet</button>
          </div>
        </div>
      </Panel>

      {/* ── Auto-Buy — full settings ── */}
      <Panel title="Auto-Buy" subtitle="All parameters that control when and how the bot executes automatic buys.">
        {autobuy.spent_today != null && (
          <div className="mb-4 flex items-center gap-2 rounded-xl border border-white/8 bg-black/20 px-3 py-2 text-xs">
            <span className="text-white/40">Spent today:</span>
            <span className="font-mono text-white/70">{Number(autobuy.spent_today).toFixed(4)} SOL</span>
            <span className="text-white/20">/ {autobuy.daily_limit_sol ? `${autobuy.daily_limit_sol} SOL limit` : "no daily limit set"}</span>
          </div>
        )}
        <form className="space-y-5" onSubmit={saveAutobuy}>

          {/* Toggles */}
          <div className="flex flex-wrap gap-4">
            <label className="flex cursor-pointer items-center gap-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-white hover:bg-black/30">
              <input type="checkbox" checked={Boolean(autobuy.enabled)}
                onChange={(e) => setAb("enabled", e.target.checked)} />
              <div>
                <div className="font-medium">Enabled</div>
                <div className="text-[11px] text-white/30">Master switch — bot will not auto-buy when off</div>
              </div>
            </label>
            <label className="flex cursor-pointer items-center gap-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-white hover:bg-black/30">
              <input type="checkbox" checked={Boolean(autobuy.confidence_scale_enabled ?? true)}
                onChange={(e) => setAb("confidence_scale_enabled", e.target.checked)} />
              <div>
                <div className="font-medium">Confidence Scaling</div>
                <div className="text-[11px] text-white/30">Scales position up to max SOL based on confidence score</div>
              </div>
            </label>
          </div>

          <Divider label="Position Sizing" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Base SOL Amount" help="Fixed SOL per buy when confidence scaling is off.">
              <NumInput value={ab("sol_amount")} onChange={(v) => setAbNum("sol_amount", v)} placeholder="0.03" step="0.001" />
            </FieldRow>
            <FieldRow label="Max SOL Amount" help="Upper cap when confidence scaling is on — strong setups scale up to this.">
              <NumInput value={ab("max_sol_amount")} onChange={(v) => setAbNum("max_sol_amount", v)} placeholder="0.10" step="0.001" />
            </FieldRow>
            <FieldRow label="Daily SOL Limit" help="Max total SOL the bot can spend on auto-buys in a 24h window.">
              <NumInput value={ab("daily_limit_sol")} onChange={(v) => setAbNum("daily_limit_sol", v)} placeholder="1.0" step="0.01" />
            </FieldRow>
          </div>
          <Divider label="Quality Thresholds" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Min Heat Score" help="Token must score at least this before auto-buy is considered. (Default 55)">
              <NumInput value={ab("min_score")} onChange={(v) => setAbNum("min_score", v)} placeholder="55" step="1" />
            </FieldRow>
            <FieldRow label="Min Confidence" help="Strategy confidence (0–1) required to execute. Weaker setups are skipped. (Default 0.35)">
              <NumInput value={ab("min_confidence")} onChange={(v) => setAbNum("min_confidence", v)} placeholder="0.35" step="0.01" />
            </FieldRow>
            <FieldRow label="Buy Tier" help="Minimum alert tier required — scouted, warm, hot, or ultra_hot.">
              <select
                value={autobuy.buy_tier || "warm"}
                onChange={(e) => setAb("buy_tier", e.target.value)}
                className="w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2.5 text-sm text-white outline-none focus:border-white/25"
              >
                <option value="scouted">Scouted (lowest)</option>
                <option value="warm">Warm</option>
                <option value="hot">Hot</option>
                <option value="ultra_hot">Ultra Hot (highest)</option>
              </select>
            </FieldRow>
            <FieldRow label="Min Transactions 5m" help="Minimum number of on-chain transactions in the last 5 minutes. 0 = off.">
              <NumInput value={ab("min_txns_5m")} onChange={(v) => setAbNum("min_txns_5m", v)} placeholder="0" step="1" />
            </FieldRow>
          </div>

          <Divider label="Market Cap Filters" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Min MCap (USD)" help="Skip tokens below this market cap. 0 = no minimum.">
              <NumInput value={ab("min_mcap_usd")} onChange={(v) => setAbNum("min_mcap_usd", v)} placeholder="0" step="100" />
            </FieldRow>
            <FieldRow label="Max MCap (USD)" help="Skip tokens above this market cap — avoids already-pumped tokens. (Default $500K)">
              <NumInput value={ab("max_mcap")} onChange={(v) => setAbNum("max_mcap", v)} placeholder="500000" step="1000" />
            </FieldRow>
          </div>

          <Divider label="Liquidity Filters" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Min Liquidity (USD)" help="Skip tokens with less liquidity than this. 0 = off.">
              <NumInput value={ab("min_liquidity_usd")} onChange={(v) => setAbNum("min_liquidity_usd", v)} placeholder="0" step="100" />
            </FieldRow>
            <FieldRow label="Max Liquidity (USD)" help="Skip tokens with more liquidity than this (over-established). 0 = off.">
              <NumInput value={ab("max_liquidity_usd")} onChange={(v) => setAbNum("max_liquidity_usd", v)} placeholder="0" step="100" />
            </FieldRow>
          </div>

          <Divider label="Age Filters" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Min Age (mins)" help="Skip tokens younger than this. 0 = no minimum.">
              <NumInput value={ab("min_age_mins")} onChange={(v) => setAbNum("min_age_mins", v)} placeholder="0" step="1" />
            </FieldRow>
            <FieldRow label="Max Age (mins)" help="Skip tokens older than this — keeps buys early. 0 = no limit.">
              <NumInput value={ab("max_age_mins")} onChange={(v) => setAbNum("max_age_mins", v)} placeholder="0" step="1" />
            </FieldRow>
          </div>

          <Divider label="Exposure Limits" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <FieldRow label="Max Open Positions" help="Bot won't open more than this many auto-buy positions at once.">
              <NumInput value={ab("max_positions")} onChange={(v) => setAbNum("max_positions", v)} placeholder="5" step="1" />
            </FieldRow>
            <FieldRow label="Max Narrative Exposure" help="Max positions in the same narrative theme (e.g. AI, memes). 0 = off.">
              <NumInput value={ab("max_narrative_exposure")} onChange={(v) => setAbNum("max_narrative_exposure", v)} placeholder="2" step="1" />
            </FieldRow>
            <FieldRow label="Max Archetype Exposure" help="Max positions in the same archetype (e.g. momentum, breakout). 0 = off.">
              <NumInput value={ab("max_archetype_exposure")} onChange={(v) => setAbNum("max_archetype_exposure", v)} placeholder="0" step="1" />
            </FieldRow>
          </div>

          <button type="submit" className="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-[var(--accent-foreground)] hover:opacity-90">
            Save Auto-Buy Settings
          </button>
        </form>

        {/* Recent activity summary */}
        {autobuyActivity?.summary?.total ? (
          <div className="mt-6 space-y-3 border-t border-white/8 pt-5">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium text-white">Recent Activity (24h)</div>
              <Link href="/autobuy" className="text-xs text-white/30 hover:text-white/60">Full log →</Link>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              {[
                { label: "Executed", value: autobuyActivity.summary.status_counts.executed || 0, color: "text-emerald-300" },
                { label: "Blocked", value: autobuyActivity.summary.status_counts.blocked || 0, color: "text-white/50" },
                { label: "Top Blocker", value: (autobuyActivity.summary.top_block_category || "none").replaceAll("_", " "), color: "text-amber-300" },
                { label: "Avg Confidence", value: Number(autobuyActivity.summary.avg_confidence || 0).toFixed(2), color: "text-white" },
              ].map((s) => (
                <div key={s.label} className="rounded-xl border border-white/8 bg-black/20 px-3 py-3">
                  <div className="text-[10px] uppercase tracking-wider text-white/30">{s.label}</div>
                  <div className={`mt-1 text-lg font-bold ${s.color}`}>{s.value}</div>
                </div>
              ))}
            </div>
            {autobuyActivity.latest && (
              <div className="rounded-xl border border-white/8 bg-black/20 px-4 py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-white">{autobuyActivity.latest.symbol || autobuyActivity.latest.name || "Unknown"}</span>
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${autobuyActivity.latest.status === "executed" ? "bg-emerald-500/15 text-emerald-300" : autobuyActivity.latest.status === "failed" ? "bg-red-500/15 text-red-300" : "bg-white/8 text-white/40"}`}>
                    {autobuyActivity.latest.status}
                  </span>
                  {autobuyActivity.latest.block_category && (
                    <span className="rounded-full border border-amber-400/20 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200">
                      {autobuyActivity.latest.block_category.replaceAll("_", " ")}
                    </span>
                  )}
                </div>
                {autobuyActivity.latest.block_reason && (
                  <div className="mt-2 text-xs text-red-300/80">{autobuyActivity.latest.block_reason}</div>
                )}
              </div>
            )}
          </div>
        ) : null}
      </Panel>

      {/* ── Heat Score Settings ── */}
      <Panel title="Heat Score Settings" subtitle="All parameters that control how the scanner scores tokens. Changes apply to your next scan cycle.">
        {/* Preset quick-apply */}
        <div className="mb-5 space-y-2">
          <div className="text-xs font-medium text-white/50 uppercase tracking-wider">Quick Presets</div>
          <div className="flex flex-wrap gap-2">
            {[
              { key: "conservative", label: "🛡️ Conservative", desc: "High thresholds, low noise" },
              { key: "balanced",     label: "⚖️ Balanced",     desc: "Standard defaults" },
              { key: "aggressive",   label: "🚀 Aggressive",   desc: "More signals, lower bar" },
              { key: "whale-mode",   label: "🐋 Whale Mode",   desc: "Maximum signals" },
            ].map((p) => (
              <button
                key={p.key}
                type="button"
                title={p.desc}
                onClick={async () => {
                  const presetOverrides: Record<string, Record<string, number>> = {
                    "conservative": { alert_ultra_hot_threshold: 95, alert_hot_threshold: 90, alert_warm_threshold: 80, alert_scouted_threshold: 70, risk_dev_sell_threshold_pct: 30, risk_top_holder_threshold_pct: 10, liquidity_min_usd: 100000 },
                    "balanced":     { alert_ultra_hot_threshold: 85, alert_hot_threshold: 70, alert_warm_threshold: 55, alert_scouted_threshold: 35, risk_dev_sell_threshold_pct: 50, risk_top_holder_threshold_pct: 20, liquidity_min_usd: 50000 },
                    "aggressive":   { alert_ultra_hot_threshold: 85, alert_hot_threshold: 70, alert_warm_threshold: 55, alert_scouted_threshold: 40, risk_dev_sell_threshold_pct: 70, risk_top_holder_threshold_pct: 30, liquidity_min_usd: 10000 },
                    "whale-mode":   { alert_ultra_hot_threshold: 80, alert_hot_threshold: 65, alert_warm_threshold: 50, alert_scouted_threshold: 30, risk_dev_sell_threshold_pct: 90, risk_top_holder_threshold_pct: 50, liquidity_min_usd: 2000 },
                  };
                  const overrides = presetOverrides[p.key];
                  if (!overrides) return;
                  setSettings((c) => ({ ...c, ...overrides }));
                  try {
                    await apiFetch(`/settings/${uid}`, { method: "POST", body: JSON.stringify({ settings: overrides }) });
                    setMessage(`Applied ${p.label} preset.`);
                    setError("");
                  } catch (err) { setError(err instanceof Error ? err.message : "Failed to apply preset"); }
                }}
                className="rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-xs hover:bg-black/40"
              >
                <div className="font-medium text-white">{p.label}</div>
                <div className="text-white/35">{p.desc}</div>
              </button>
            ))}
          </div>
        </div>

        <form className="space-y-5" onSubmit={saveThresholds}>
          <Divider label="Alert Thresholds" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {([
              ["alert_ultra_hot_threshold", "Ultra Hot", "Score needed for the highest-tier Telegram alert. (Default 85)"],
              ["alert_hot_threshold",       "Hot",       "Score for HOT tier alerts. (Default 70)"],
              ["alert_warm_threshold",      "Warm",      "Score for WARM tier alerts. (Default 55)"],
              ["alert_scouted_threshold",   "Scouted",   "Minimum score to add a token to the watchlist. (Default 35)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Scout Tier Thresholds" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["scout_tier_brewing_threshold", "Brewing Tier",  "Minimum score for BREWING internal tier. (Default 35)"],
              ["scout_tier_warm_threshold",    "Warm Tier",     "Minimum score for WARM internal tier. (Default 50)"],
              ["scout_tier_hot_threshold",     "Hot Tier",      "Minimum score for HOT internal tier. (Default 70)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Momentum Factor (0–20 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["momentum_weight_usd_vol",           "Volume Weight",    "Importance of USD trading volume vs token age (1–100). (Default 50)"],
              ["momentum_weight_creation_momentum", "Newness Weight",   "Importance of token freshness/age in momentum score (1–100). (Default 50)"],
              ["momentum_min_vol",                  "Min Volume (USD)", "USD volume needed for full momentum points. (Default 5000)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step={key === "momentum_min_vol" ? "100" : "1"} />
              </FieldRow>
            ))}
          </div>

          <Divider label="Liquidity Factor (0–20 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["liquidity_min_usd",  "Min Liquidity (USD)",  "USD threshold for full liquidity score. (Default $50K)"],
              ["liquidity_good_usd", "Good Liquidity (USD)", "USD threshold for a solid liquidity score. (Default $10K)"],
              ["liquidity_fair_usd", "Fair Liquidity (USD)", "USD threshold for a baseline liquidity score. (Default $2K)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="500" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Risk / Safety Factor (0–25 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["risk_dev_sell_threshold_pct",    "Dev Dump Threshold %", "Dev selling more than this % triggers disqualification. (Default 50)"],
              ["risk_top_holder_threshold_pct",  "Top Holder Cap %",     "Single holder above this % triggers disqualification. (Default 20)"],
              ["risk_bundle_severity",           "Bundle Penalty",       "How heavily bundled tokens are penalised in the score (0–100). (Default 50)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Social / Narrative Factor (0–15 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["social_twitter_follower_min",      "Twitter Min Followers",  "Followers needed for full social points. (Default 1000)"],
              ["social_narrative_trending_boost",  "Narrative Boost Weight", "How much a trending narrative boosts the score (0–100). (Default 50)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Wallet Behavior Factor (0–15 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["wallet_cluster_boost_pts",    "Cluster Match Bonus",   "Points added when known wallet clusters are matched (0–15). (Default 5)"],
              ["wallet_known_seed_boost_pts", "Known Seed Wallet Bonus", "Points for entry from a known seed wallet (0–15). (Default 8)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Migration Factor (0–10 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["migration_new_boost_pts",         "New Token Bonus",       "Points for tokens under 1 hour old (0–10). (Default 8)"],
              ["migration_grad_boost_pts",        "Graduation Bonus",      "Points for pump.fun graduates (0–10). (Default 6)"],
              ["migration_migrated_penalty_pts",  "Migration Penalty",     "Points deducted for already-migrated tokens (0–10). (Default 2)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Buy Pressure Factor (0–10 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["bias_buy_threshold_pct",      "Buy Bias Max %",  "Buy % needed for the maximum buy-pressure score (0–100). (Default 70)"],
              ["bias_buy_good_threshold_pct", "Buy Bias Good %", "Buy % for a strong (but not max) buy-pressure score (0–100). (Default 60)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <Divider label="Volume Trend Factor (0–5 pts)" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {([
              ["trend_explosive_threshold", "Explosive Trend Pts", "Max points awarded for explosive volume growth (0–5). (Default 5)"],
              ["trend_strong_threshold",    "Strong Trend Pts",    "Points for strong (but not explosive) volume growth (0–5). (Default 3)"],
            ] as [string, string, string][]).map(([key, label, help]) => (
              <FieldRow key={key} label={label} help={help}>
                <NumInput value={String(settings[key] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))} step="1" />
              </FieldRow>
            ))}
          </div>

          <button type="submit" className="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-[var(--accent-foreground)] hover:opacity-90">
            Save Heat Score Settings
          </button>
        </form>
      </Panel>

      {/* ── Trade Controls ── */}
      <Panel title="Trade Controls" subtitle="Default multiplier presets and global exit protections applied to new buys.">
        {tradeControls ? (
          <form className="space-y-5" onSubmit={saveTradeControls}>
            <label className="flex cursor-pointer items-center gap-3 text-sm text-white">
              <input type="checkbox" checked={Boolean(tradeControls.presets_enabled)}
                onChange={(e) => setTradeControls((c) => c ? { ...c, presets_enabled: e.target.checked } : c)} />
              <div>
                <div className="font-medium">Apply presets to new buys</div>
                <div className="text-[11px] text-white/30">Automatically assign multiplier targets when the bot buys</div>
              </div>
            </label>

            <Divider label="Multiplier Presets" />
            <div className="space-y-2">
              {(tradeControls.presets || []).map((preset, i) => (
                <div key={`preset-${i}`} className="grid gap-2 md:grid-cols-[1fr_1fr_auto]">
                  <input value={String(preset.mult ?? 0)}
                    onChange={(e) => setTradeControls((c) => c ? { ...c, presets: c.presets.map((r, ri) => ri === i ? { ...r, mult: Number(e.target.value || 0) } : r) } : c)}
                    className="rounded-xl border border-white/10 bg-black/30 px-3 py-2.5 text-sm text-white outline-none"
                    placeholder="Multiplier (e.g. 2)" />
                  <input value={String(preset.sell_pct ?? 0)}
                    onChange={(e) => setTradeControls((c) => c ? { ...c, presets: c.presets.map((r, ri) => ri === i ? { ...r, sell_pct: Number(e.target.value || 0) } : r) } : c)}
                    className="rounded-xl border border-white/10 bg-black/30 px-3 py-2.5 text-sm text-white outline-none"
                    placeholder="Sell %" />
                  <button type="button"
                    onClick={() => setTradeControls((c) => c ? { ...c, presets: c.presets.filter((_, ri) => ri !== i) } : c)}
                    className="rounded-xl border border-white/10 px-4 py-2 text-xs text-white/40 hover:text-white/70">Remove</button>
                </div>
              ))}
              <button type="button"
                onClick={() => setTradeControls((c) => c ? { ...c, presets: [...c.presets, { mult: 2, sell_pct: 50 }] } : c)}
                className="rounded-xl border border-white/10 px-4 py-2 text-xs text-white/50 hover:text-white/80">+ Add preset</button>
            </div>

            <Divider label="Global Exit Protections" />
            <div className="grid gap-4 lg:grid-cols-2">
              <GlobalExitBlock title="Global Stop-Loss" description="Sells when price drops X% from entry."
                enabled={Boolean(tradeControls.global_stop_loss?.enabled)}
                fields={[{ key: "pct", label: "Drop %", value: Number(tradeControls.global_stop_loss?.pct || 0) }, { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_stop_loss?.sell_pct || 100) }]}
                onToggle={(v) => setTradeControls((c) => c ? { ...c, global_stop_loss: { ...c.global_stop_loss, enabled: v } } : c)}
                onFieldChange={(k, v) => setTradeControls((c) => c ? { ...c, global_stop_loss: { ...c.global_stop_loss, [k]: v } } : c)} />
              <GlobalExitBlock title="Global Trailing Stop" description="Sells when price drops X% from its peak."
                enabled={Boolean(tradeControls.global_trailing_stop?.enabled)}
                fields={[{ key: "trail_pct", label: "Trail %", value: Number(tradeControls.global_trailing_stop?.trail_pct || 0) }, { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_trailing_stop?.sell_pct || 100) }]}
                onToggle={(v) => setTradeControls((c) => c ? { ...c, global_trailing_stop: { ...c.global_trailing_stop, enabled: v } } : c)}
                onFieldChange={(k, v) => setTradeControls((c) => c ? { ...c, global_trailing_stop: { ...c.global_trailing_stop, [k]: v } } : c)} />
              <GlobalExitBlock title="Global Trailing TP" description="Activates trailing exit after target multiplier is reached."
                enabled={Boolean(tradeControls.global_trailing_tp?.enabled)}
                fields={[{ key: "activate_mult", label: "Activate at x", value: Number(tradeControls.global_trailing_tp?.activate_mult || 0) }, { key: "trail_pct", label: "Trail %", value: Number(tradeControls.global_trailing_tp?.trail_pct || 0) }, { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_trailing_tp?.sell_pct || 0) }]}
                onToggle={(v) => setTradeControls((c) => c ? { ...c, global_trailing_tp: { ...c.global_trailing_tp, enabled: v } } : c)}
                onFieldChange={(k, v) => setTradeControls((c) => c ? { ...c, global_trailing_tp: { ...c.global_trailing_tp, [k]: v } } : c)} />
              <GlobalExitBlock title="Global Breakeven" description="Moves stop to entry price once multiplier is hit."
                enabled={Boolean(tradeControls.global_breakeven_stop?.enabled)}
                fields={[{ key: "activate_mult", label: "Activate at x", value: Number(tradeControls.global_breakeven_stop?.activate_mult || 0) }]}
                onToggle={(v) => setTradeControls((c) => c ? { ...c, global_breakeven_stop: { ...c.global_breakeven_stop, enabled: v } } : c)}
                onFieldChange={(k, v) => setTradeControls((c) => c ? { ...c, global_breakeven_stop: { ...c.global_breakeven_stop, [k]: v } } : c)} />
              <GlobalExitBlock title="Global Time Exit" description="Force-sells if target multiplier not reached within N hours."
                enabled={Boolean(tradeControls.global_time_exit?.enabled)}
                fields={[{ key: "hours", label: "Hours", value: Number(tradeControls.global_time_exit?.hours || 0) }, { key: "target_mult", label: "Target x", value: Number(tradeControls.global_time_exit?.target_mult || 0) }, { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_time_exit?.sell_pct || 100) }]}
                onToggle={(v) => setTradeControls((c) => c ? { ...c, global_time_exit: { ...c.global_time_exit, enabled: v } } : c)}
                onFieldChange={(k, v) => setTradeControls((c) => c ? { ...c, global_time_exit: { ...c.global_time_exit, [k]: v } } : c)} />
            </div>

            <button type="submit" className="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-[var(--accent-foreground)] hover:opacity-90">
              Save Trade Controls
            </button>
          </form>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">Loading trade controls…</div>
        )}
      </Panel>
    </div>
  );
}

function GlobalExitBlock({ title, description, enabled, fields, onToggle, onFieldChange }: {
  title: string; description: string; enabled: boolean;
  fields: Array<{ key: string; label: string; value: number }>;
  onToggle: (v: boolean) => void;
  onFieldChange: (key: string, value: number) => void;
}) {
  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
      <label className="mb-1 flex cursor-pointer items-center gap-3 text-sm font-medium text-white">
        <input type="checkbox" checked={enabled} onChange={(e) => onToggle(e.target.checked)} />
        {title}
      </label>
      <p className="mb-3 text-[11px] text-white/30 leading-relaxed">{description}</p>
      <div className="grid gap-3 md:grid-cols-2">
        {fields.map((f) => (
          <div key={`${title}-${f.key}`} className="space-y-1">
            <div className="text-xs text-white/40">{f.label}</div>
            <input value={String(f.value)}
              onChange={(e) => onFieldChange(f.key, Number(e.target.value || 0))}
              className="w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2.5 text-sm text-white outline-none" />
          </div>
        ))}
      </div>
    </div>
  );
}
