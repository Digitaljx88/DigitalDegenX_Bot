"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

// ── Types ──────────────────────────────────────────────────────────────────────

type SniperConfig = {
  uid: number;
  effective_mode?: "paper" | "live";
  live_trading_enabled?: boolean;
  live_wallet_configured?: boolean;
  // Core
  enabled: boolean;
  sol_amount: number;
  max_concurrent: number;
  take_profit_pct: number;
  stop_loss_pct: number;
  max_age_secs: number;
  dev_buy_max_pct: number;
  // Intelligence filters
  require_narrative: boolean;
  min_predictor_confidence: number;
  use_lifecycle_filter: boolean;
  max_bundle_risk: number;
  // Adaptive sizing
  sol_multiplier_narrative: number;
  sol_multiplier_predictor: number;
  // Scheduling
  active_hours_utc: string;
  // Notifications
  telegram_notify: boolean;
};

type SniperStatus = {
  uid: number;
  enabled: boolean;
  effective_mode?: "paper" | "live";
  live_trading_enabled?: boolean;
  live_wallet_configured?: boolean;
  snipes_today: number;
  win_rate_pct: number;
  profit_sol_today: number;
  open_positions: number;
};

type SniperPosition = {
  mint: string;
  symbol: string;
  name: string;
  sol_spent: number;
  tokens_bought: number;
  buy_price_sol: number;
  current_price_sol: number;
  unrealized_sol: number;
  pnl_pct: number;
  buy_time: number;
  age_secs: number;
  mode?: string;
};

type SniperHistoryRow = {
  id: number;
  mint: string;
  symbol: string;
  name: string;
  sol_spent: number;
  sol_received: number;
  profit_sol: number;
  buy_time: number;
  sell_time: number;
  hold_secs: number;
  exit_reason: string;
  mode?: string;
};

type SniperBuyAttemptRow = {
  id: number;
  mint: string;
  symbol: string;
  name: string;
  mode: string;
  trade_sol: number;
  tx_sig: string;
  tx_status: string;
  tokens_received: number;
  sol_before: number;
  sol_after: number;
  sol_delta: number;
  outcome: string;
  note: string;
  attempted_at: number;
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtSol(v: number, dp = 4) {
  return v.toFixed(dp);
}

function fmtAge(secs: number) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

function fmtHold(secs: number) {
  if (secs < 60) return `${Math.round(secs)}s`;
  return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const exitColor: Record<string, string> = {
  TP: "var(--green)",
  SL: "var(--red)",
  TIME: "var(--yellow)",
  MANUAL: "var(--blue)",
  GRADUATED_STRANDED: "var(--text2)",
  NO_CURVE: "var(--text2)",
};

const attemptColor: Record<string, string> = {
  opened: "var(--green)",
  zero_tokens: "var(--red)",
  buy_unconfirmed: "var(--yellow)",
  buy_failed: "var(--text2)",
};

// ── Sub-components ────────────────────────────────────────────────────────────

function StatCard({ label, value, color, sub }: { label: string; value: string | number; color?: string; sub?: string }) {
  return (
    <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 18px", minWidth: 130 }}>
      <div style={{ fontSize: 10, color: "var(--text3)", textTransform: "uppercase", letterSpacing: "0.12em", fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "var(--font-mono)", letterSpacing: "-0.02em", color: color || "var(--foreground)" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: "var(--text3)", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      style={{
        width: 44, height: 24, borderRadius: 12, border: "none", cursor: "pointer", position: "relative",
        background: checked ? "var(--green)" : "var(--bg4)", transition: "background 200ms",
      }}
    >
      <span style={{
        position: "absolute", top: 3, left: checked ? 23 : 3, width: 18, height: 18,
        borderRadius: "50%", background: "#fff", transition: "left 200ms",
      }} />
    </button>
  );
}

function NumInput({ label, value, onChange, min, max, step, unit, hint }: {
  label: string; value: number; onChange: (v: number) => void;
  min?: number; max?: number; step?: number; unit?: string; hint?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <label style={{ fontSize: 11, color: "var(--text3)", fontWeight: 500 }}>{label}</label>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input
          type="number" value={value} min={min} max={max} step={step ?? 0.01}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{
            background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7,
            color: "var(--foreground)", fontSize: 13, padding: "7px 10px", width: 90, outline: "none",
            fontFamily: "var(--font-mono)",
          }}
        />
        {unit && <span style={{ fontSize: 11, color: "var(--text3)" }}>{unit}</span>}
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--text3)", marginTop: 1 }}>{hint}</div>}
    </div>
  );
}

function TextInput({ label, value, onChange, placeholder, hint }: {
  label: string; value: string; onChange: (v: string) => void;
  placeholder?: string; hint?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <label style={{ fontSize: 11, color: "var(--text3)", fontWeight: 500 }}>{label}</label>
      <input
        type="text" value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7,
          color: "var(--foreground)", fontSize: 13, padding: "7px 10px", width: 120, outline: "none",
          fontFamily: "var(--font-mono)",
        }}
      />
      {hint && <div style={{ fontSize: 10, color: "var(--text3)", marginTop: 1 }}>{hint}</div>}
    </div>
  );
}

function ToggleRow({ label, checked, onChange, hint }: {
  label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Toggle checked={checked} onChange={onChange} />
        <span style={{ fontSize: 12, color: "var(--foreground)", fontWeight: 500 }}>{label}</span>
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--text3)", marginLeft: 54 }}>{hint}</div>}
    </div>
  );
}

// ── Main dashboard ────────────────────────────────────────────────────────────

export function SniperDashboard() {
  const { uid } = useActiveUid();

  const [status, setStatus] = useState<SniperStatus | null>(null);
  const [config, setConfig] = useState<SniperConfig | null>(null);
  const [positions, setPositions] = useState<SniperPosition[]>([]);
  const [history, setHistory] = useState<SniperHistoryRow[]>([]);
  const [attempts, setAttempts] = useState<SniperBuyAttemptRow[]>([]);

  const [draft, setDraft] = useState<Partial<SniperConfig>>({});
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [closingMint, setClosingMint] = useState<string | null>(null);
  const [error, setError] = useState("");

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const effectiveMode = status?.effective_mode || config?.effective_mode || "paper";

  // ── Data fetching ──────────────────────────────────────────────────────────

  const loadAll = useCallback(async () => {
    if (!uid) return;
    try {
      const [st, cfg, pos, hist, att] = await Promise.all([
        apiFetch<SniperStatus>("/api/sniper/status"),
        apiFetch<SniperConfig>("/api/sniper/config"),
        apiFetch<{ positions: SniperPosition[] }>("/api/sniper/positions"),
        apiFetch<{ history: SniperHistoryRow[] }>("/api/sniper/history?limit=50"),
        apiFetch<{ attempts: SniperBuyAttemptRow[] }>("/api/sniper/attempts?limit=50"),
      ]);
      setStatus(st);
      setConfig(cfg);
      setDraft({});   // clear unsaved draft on fresh load
      setPositions(pos.positions);
      setHistory(hist.history);
      setAttempts(att.attempts);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, [uid]);

  const refreshPositions = useCallback(async () => {
    if (!uid) return;
    try {
      const [st, pos] = await Promise.all([
        apiFetch<SniperStatus>("/api/sniper/status"),
        apiFetch<{ positions: SniperPosition[] }>("/api/sniper/positions"),
      ]);
      setStatus(st);
      setPositions(pos.positions);
    } catch { /* silent */ }
  }, [uid]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Auto-refresh positions every 10s
  useEffect(() => {
    pollRef.current = setInterval(refreshPositions, 10_000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [refreshPositions]);

  // ── Config save ────────────────────────────────────────────────────────────

  async function saveConfig(patch: Partial<SniperConfig>) {
    setSaving(true);
    setSaveMsg("");
    try {
      const updated = await apiFetch<SniperConfig>("/api/sniper/config", {
        method: "POST",
        body: JSON.stringify(patch),
      });
      setConfig(updated);
      setDraft({});
      setSaveMsg(
        updated.enabled
          ? `Sniper enabled in ${String(updated.effective_mode || "paper").toUpperCase()} mode`
          : "Sniper disabled"
      );
      setTimeout(() => setSaveMsg(""), 2500);
      // Refresh status so the enabled indicator updates
      const st = await apiFetch<SniperStatus>("/api/sniper/status");
      setStatus(st);
    } catch (e) {
      setSaveMsg(`Error: ${e}`);
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled() {
    if (!config) return;
    await saveConfig({ enabled: !config.enabled });
  }

  async function submitDraft(e: React.FormEvent) {
    e.preventDefault();
    if (Object.keys(draft).length === 0) return;
    await saveConfig(draft);
  }

  // ── Manual close ──────────────────────────────────────────────────────────

  async function closePosition(mint: string) {
    setClosingMint(mint);
    try {
      await apiFetch(`/api/sniper/close/${mint}`, { method: "POST", body: JSON.stringify({}) });
      await Promise.all([
        refreshPositions(),
        apiFetch<{ history: SniperHistoryRow[] }>("/api/sniper/history?limit=50").then(r => setHistory(r.history)),
        apiFetch<{ attempts: SniperBuyAttemptRow[] }>("/api/sniper/attempts?limit=50").then(r => setAttempts(r.attempts)),
      ]);
    } catch (e) {
      alert(`Close failed: ${e}`);
    } finally {
      setClosingMint(null);
    }
  }

  // ── Merged config (config + unsaved draft) ─────────────────────────────────

  // ── Render ─────────────────────────────────────────────────────────────────

  if (!uid) {
    return (
      <div style={{ color: "var(--text3)", padding: "40px 0", textAlign: "center", fontSize: 14 }}>
        Select a user to view sniper.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>

      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
        <div>
          <h2 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Launch Sniper</h2>
          <p style={{ fontSize: 12, color: "var(--text3)", margin: "4px 0 0" }}>
            Auto-buys new pump.fun tokens on launch, exits on TP / SL / time.
          </p>
        </div>
        {config && (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 12, color: status?.enabled ? "var(--green)" : "var(--text3)" }}>
              {status?.enabled ? "Active" : "Inactive"}
            </span>
            <Toggle checked={!!status?.enabled} onChange={toggleEnabled} />
          </div>
        )}
      </div>

      {error && (
        <div style={{ background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "var(--red)" }}>
          {error}
        </div>
      )}

      {status?.live_trading_enabled === false ? (
        <div style={{ background: "rgba(250,204,21,0.08)", border: "1px solid rgba(250,204,21,0.18)", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "#facc15" }}>
          Live trading is disabled during security lockdown. Sniper remains available, but effective execution mode is paper until live trading is re-enabled.
        </div>
      ) : null}

      {status?.live_trading_enabled !== false && status?.live_wallet_configured === false ? (
        <div style={{ background: "rgba(59,130,246,0.1)", border: "1px solid rgba(59,130,246,0.2)", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "#93c5fd" }}>
          No live wallet is configured. Sniper settings can still be edited, but live execution needs a configured wallet.
        </div>
      ) : null}

      {status?.enabled && effectiveMode === "paper" ? (
        <div style={{ background: "rgba(34,211,160,0.1)", border: "1px solid rgba(34,211,160,0.22)", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "var(--green)" }}>
          Sniper is enabled and armed in paper mode. New launch entries will be simulated, not sent on-chain.
        </div>
      ) : null}

      {/* ── Stats bar ── */}
      {status && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <StatCard label="Execution Mode" value={String(effectiveMode).toUpperCase()} color={effectiveMode === "live" ? "var(--red)" : "var(--green)"} />
          <StatCard label="Snipes Today" value={status.snipes_today} />
          <StatCard
            label="Win Rate"
            value={`${status.win_rate_pct}%`}
            color={status.win_rate_pct >= 50 ? "var(--green)" : "var(--red)"}
          />
          <StatCard
            label="P&L Today"
            value={`${status.profit_sol_today >= 0 ? "+" : ""}${fmtSol(status.profit_sol_today)} SOL`}
            color={status.profit_sol_today >= 0 ? "var(--green)" : "var(--red)"}
          />
          <StatCard label="Open Positions" value={status.open_positions} color="var(--blue)" />
        </div>
      )}

      {/* ── Config panel ── */}
      {config && (
        <form onSubmit={submitDraft}>
          <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 12, padding: "20px 22px", display: "flex", flexDirection: "column", gap: 22 }}>

            {/* Section: Core */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text3)", marginBottom: 14, textTransform: "uppercase", letterSpacing: "0.1em" }}>
                Core Settings
              </div>
              <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
                <NumInput
                  label="SOL Per Trade"
                  value={draft.sol_amount ?? config.sol_amount}
                  onChange={(v) => setDraft(d => ({ ...d, sol_amount: v }))}
                  min={0.001} max={10} step={0.01} unit="SOL"
                  hint="Base size before multipliers"
                />
                <NumInput
                  label="Max Concurrent"
                  value={draft.max_concurrent ?? config.max_concurrent}
                  onChange={(v) => setDraft(d => ({ ...d, max_concurrent: Math.round(v) }))}
                  min={1} max={20} step={1} unit="positions"
                />
                <NumInput
                  label="Take Profit"
                  value={draft.take_profit_pct ?? config.take_profit_pct}
                  onChange={(v) => setDraft(d => ({ ...d, take_profit_pct: v }))}
                  min={1} max={10000} step={5} unit="%"
                />
                <NumInput
                  label="Stop Loss"
                  value={draft.stop_loss_pct ?? config.stop_loss_pct}
                  onChange={(v) => setDraft(d => ({ ...d, stop_loss_pct: v }))}
                  min={1} max={99} step={1} unit="%"
                />
                <NumInput
                  label="Max Age"
                  value={draft.max_age_secs ?? config.max_age_secs}
                  onChange={(v) => setDraft(d => ({ ...d, max_age_secs: Math.round(v) }))}
                  min={30} max={3600} step={30} unit="secs"
                />
                <NumInput
                  label="Dev Buy Max"
                  value={draft.dev_buy_max_pct ?? config.dev_buy_max_pct}
                  onChange={(v) => setDraft(d => ({ ...d, dev_buy_max_pct: v }))}
                  min={0} max={100} step={1} unit="%"
                  hint="Skip if dev holds > X% supply"
                />
              </div>
            </div>

            <div style={{ borderTop: "1px solid var(--border)" }} />

            {/* Section: Intelligence Filters */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text3)", marginBottom: 14, textTransform: "uppercase", letterSpacing: "0.1em" }}>
                Intelligence Filters
              </div>
              <div style={{ display: "flex", gap: 28, flexWrap: "wrap", alignItems: "flex-start" }}>
                <ToggleRow
                  label="Require Narrative"
                  checked={draft.require_narrative ?? config.require_narrative}
                  onChange={(v) => setDraft(d => ({ ...d, require_narrative: v }))}
                  hint="Skip if name/symbol has no known narrative (AI, meme, political…)"
                />
                <ToggleRow
                  label="Lifecycle Filter"
                  checked={draft.use_lifecycle_filter ?? config.use_lifecycle_filter}
                  onChange={(v) => setDraft(d => ({ ...d, use_lifecycle_filter: v }))}
                  hint="Skip if scanner already rated this token < 50; 2× size if ≥ 70"
                />
                <NumInput
                  label="Min Predictor Confidence"
                  value={draft.min_predictor_confidence ?? config.min_predictor_confidence}
                  onChange={(v) => setDraft(d => ({ ...d, min_predictor_confidence: v }))}
                  min={0} max={100} step={5} unit="%"
                  hint="0 = disabled. Skip if archetype confidence < X"
                />
                <NumInput
                  label="Max Bundle Risk"
                  value={draft.max_bundle_risk ?? config.max_bundle_risk}
                  onChange={(v) => setDraft(d => ({ ...d, max_bundle_risk: Math.round(v) }))}
                  min={0} max={10} step={1} unit="/10"
                  hint="10 = disabled. Skip if cached bundle risk > X"
                />
              </div>
            </div>

            <div style={{ borderTop: "1px solid var(--border)" }} />

            {/* Section: Adaptive Sizing */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text3)", marginBottom: 14, textTransform: "uppercase", letterSpacing: "0.1em" }}>
                Adaptive Position Sizing
              </div>
              <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
                <NumInput
                  label="Narrative Multiplier"
                  value={draft.sol_multiplier_narrative ?? config.sol_multiplier_narrative}
                  onChange={(v) => setDraft(d => ({ ...d, sol_multiplier_narrative: v }))}
                  min={0.1} max={10} step={0.1} unit="×"
                  hint="Multiply SOL when narrative matched"
                />
                <NumInput
                  label="Predictor Multiplier"
                  value={draft.sol_multiplier_predictor ?? config.sol_multiplier_predictor}
                  onChange={(v) => setDraft(d => ({ ...d, sol_multiplier_predictor: v }))}
                  min={0.1} max={10} step={0.1} unit="×"
                  hint="Multiply SOL when archetype confidence ≥ 70%"
                />
              </div>
              <div style={{ marginTop: 10, fontSize: 11, color: "var(--text3)", fontStyle: "italic" }}>
                Example: 0.05 SOL base × 1.5 narrative × 2.0 predictor = 0.15 SOL trade
              </div>
            </div>

            <div style={{ borderTop: "1px solid var(--border)" }} />

            {/* Section: Scheduling & Notifications */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text3)", marginBottom: 14, textTransform: "uppercase", letterSpacing: "0.1em" }}>
                Scheduling & Notifications
              </div>
              <div style={{ display: "flex", gap: 28, flexWrap: "wrap", alignItems: "flex-start" }}>
                <TextInput
                  label="Active Hours (UTC)"
                  value={draft.active_hours_utc ?? config.active_hours_utc}
                  onChange={(v) => setDraft(d => ({ ...d, active_hours_utc: v }))}
                  placeholder="e.g. 12-23"
                  hint={'Format: "HH-HH". Empty = all hours'}
                />
                <ToggleRow
                  label="Telegram Notifications"
                  checked={draft.telegram_notify ?? config.telegram_notify}
                  onChange={(v) => setDraft(d => ({ ...d, telegram_notify: v }))}
                  hint="Send buy/sell alerts to Telegram"
                />
              </div>
            </div>

            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
              <button
                type="submit"
                disabled={saving || Object.keys(draft).length === 0}
                style={{
                  background: Object.keys(draft).length > 0 ? "var(--accent)" : "var(--bg3)",
                  color: Object.keys(draft).length > 0 ? "#fff" : "var(--text3)",
                  border: "none", borderRadius: 8, padding: "8px 20px",
                  fontSize: 12, fontWeight: 600, cursor: Object.keys(draft).length > 0 ? "pointer" : "default",
                }}
              >
                {saving ? "Saving…" : "Save Changes"}
              </button>
              {saveMsg && (
                <span style={{ fontSize: 12, color: saveMsg.startsWith("Error") ? "var(--red)" : "var(--green)" }}>
                  {saveMsg}
                </span>
              )}
              {Object.keys(draft).length > 0 && (
                <button
                  type="button"
                  onClick={() => setDraft({})}
                  style={{ background: "none", border: "none", color: "var(--text3)", fontSize: 12, cursor: "pointer" }}
                >
                  Discard
                </button>
              )}
            </div>
          </div>
        </form>
      )}

      {/* ── Open positions ── */}
      <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text2)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Open Positions
          </span>
          <span style={{ fontSize: 11, color: "var(--text3)" }}>auto-refreshes every 10s</span>
        </div>
        {positions.length === 0 ? (
          <div style={{ padding: "28px 18px", textAlign: "center", fontSize: 13, color: "var(--text3)" }}>
            No open positions
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["Token", "Entry", "Current", "Unrealized", "P&L %", "Age", ""].map(h => (
                    <th key={h} style={{ padding: "10px 14px", textAlign: "left", color: "var(--text3)", fontWeight: 500, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map(pos => (
                  <tr key={pos.mint} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 14px" }}>
                      <div style={{ fontWeight: 600 }}>
                        {pos.symbol}
                        {pos.mode && (
                          <span style={{
                            marginLeft: 6, fontSize: 9, padding: "1px 5px", borderRadius: 4,
                            background: pos.mode === "paper" ? "rgba(234,179,8,0.15)" : "rgba(34,197,94,0.15)",
                            color: pos.mode === "paper" ? "#eab308" : "#22c55e",
                            fontWeight: 700, textTransform: "uppercase",
                          }}>{pos.mode}</span>
                        )}
                      </div>
                      <div style={{ fontSize: 10, color: "var(--text3)" }}>
                        <Link
                          href={`https://dexscreener.com/solana/${pos.mint}`}
                          target="_blank"
                          style={{ color: "var(--blue)", textDecoration: "none" }}
                        >
                          {pos.mint.slice(0, 8)}…
                        </Link>
                      </div>
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)" }}>
                      {fmtSol(pos.sol_spent)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)", color: "var(--text2)" }}>
                      {pos.current_price_sol > 0 ? `${fmtSol(pos.current_price_sol * pos.tokens_bought, 5)} SOL` : "—"}
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)", color: pos.unrealized_sol >= 0 ? "var(--green)" : "var(--red)" }}>
                      {pos.unrealized_sol >= 0 ? "+" : ""}{fmtSol(pos.unrealized_sol, 5)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)", color: pos.pnl_pct >= 0 ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
                      {pos.pnl_pct >= 0 ? "+" : ""}{pos.pnl_pct.toFixed(1)}%
                    </td>
                    <td style={{ padding: "10px 14px", color: "var(--text3)" }}>
                      {fmtAge(pos.age_secs)}
                    </td>
                    <td style={{ padding: "10px 14px" }}>
                      <button
                        onClick={() => closePosition(pos.mint)}
                        disabled={closingMint === pos.mint}
                        style={{
                          background: "rgba(244,63,94,0.12)", border: "1px solid rgba(244,63,94,0.25)",
                          color: "var(--red)", borderRadius: 6, padding: "5px 12px",
                          fontSize: 11, fontWeight: 600, cursor: "pointer",
                        }}
                      >
                        {closingMint === pos.mint ? "Closing…" : "Close"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── History ── */}
      <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)" }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text2)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            History
          </span>
        </div>
        {history.length === 0 ? (
          <div style={{ padding: "28px 18px", textAlign: "center", fontSize: 13, color: "var(--text3)" }}>
            No completed snipes yet
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["Time", "Token", "In", "Out", "Profit", "Hold", "Exit"].map(h => (
                    <th key={h} style={{ padding: "10px 14px", textAlign: "left", color: "var(--text3)", fontWeight: 500, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.map(row => (
                  <tr key={row.id} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 14px", color: "var(--text3)" }}>
                      {fmtTime(row.sell_time)}
                    </td>
                    <td style={{ padding: "10px 14px" }}>
                      <div style={{ fontWeight: 600 }}>
                        {row.symbol}
                        {row.mode && (
                          <span style={{
                            marginLeft: 6, fontSize: 9, padding: "1px 5px", borderRadius: 4,
                            background: row.mode === "paper" ? "rgba(234,179,8,0.15)" : "rgba(34,197,94,0.15)",
                            color: row.mode === "paper" ? "#eab308" : "#22c55e",
                            fontWeight: 700, textTransform: "uppercase",
                          }}>{row.mode}</span>
                        )}
                      </div>
                      <div style={{ fontSize: 10, color: "var(--text3)" }}>
                        <Link
                          href={`https://dexscreener.com/solana/${row.mint}`}
                          target="_blank"
                          style={{ color: "var(--blue)", textDecoration: "none" }}
                        >
                          {row.mint.slice(0, 8)}…
                        </Link>
                      </div>
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)" }}>
                      {fmtSol(row.sol_spent)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)" }}>
                      {fmtSol(row.sol_received)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)", color: row.profit_sol >= 0 ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
                      {row.profit_sol >= 0 ? "+" : ""}{fmtSol(row.profit_sol, 5)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", color: "var(--text3)" }}>
                      {fmtHold(row.hold_secs)}
                    </td>
                    <td style={{ padding: "10px 14px" }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, padding: "3px 8px", borderRadius: 5,
                        background: `${exitColor[row.exit_reason] || "var(--text3)"}22`,
                        color: exitColor[row.exit_reason] || "var(--text3)",
                        border: `1px solid ${exitColor[row.exit_reason] || "var(--border)"}44`,
                      }}>
                        {row.exit_reason}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Buy attempts ── */}
      <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)" }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text2)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Buy Attempts
          </span>
        </div>
        {attempts.length === 0 ? (
          <div style={{ padding: "28px 18px", textAlign: "center", fontSize: 13, color: "var(--text3)" }}>
            No buy attempts yet
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["Time", "Token", "Trade", "SOL Δ", "Tokens", "Outcome", "Tx"].map(h => (
                    <th key={h} style={{ padding: "10px 14px", textAlign: "left", color: "var(--text3)", fontWeight: 500, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {attempts.map(row => (
                  <tr key={row.id} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 14px", color: "var(--text3)" }}>
                      {fmtTime(row.attempted_at)}
                    </td>
                    <td style={{ padding: "10px 14px" }}>
                      <div style={{ fontWeight: 600 }}>{row.symbol || row.mint.slice(0, 8)}</div>
                      <div style={{ fontSize: 10, color: "var(--text3)" }}>
                        <Link
                          href={`https://dexscreener.com/solana/${row.mint}`}
                          target="_blank"
                          style={{ color: "var(--blue)", textDecoration: "none" }}
                        >
                          {row.mint.slice(0, 8)}…
                        </Link>
                      </div>
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)" }}>
                      {fmtSol(row.trade_sol)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)", color: row.sol_delta > 0 ? "var(--red)" : "var(--text2)" }}>
                      {fmtSol(row.sol_delta, 5)} SOL
                    </td>
                    <td style={{ padding: "10px 14px", fontFamily: "var(--font-mono)" }}>
                      {row.tokens_received > 0 ? row.tokens_received.toFixed(0) : "0"}
                    </td>
                    <td style={{ padding: "10px 14px" }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, padding: "3px 8px", borderRadius: 5,
                        background: `${attemptColor[row.outcome] || "var(--text3)"}22`,
                        color: attemptColor[row.outcome] || "var(--text3)",
                        border: `1px solid ${attemptColor[row.outcome] || "var(--border)"}44`,
                      }}>
                        {row.outcome}
                      </span>
                      {row.note && (
                        <div style={{ marginTop: 4, fontSize: 10, color: "var(--text3)", maxWidth: 260 }}>
                          {row.note}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "10px 14px", fontSize: 10 }}>
                      {row.tx_sig ? (
                        <Link
                          href={`https://solscan.io/tx/${row.tx_sig}`}
                          target="_blank"
                          style={{ color: "var(--blue)", textDecoration: "none" }}
                        >
                          {row.tx_sig.slice(0, 12)}…
                        </Link>
                      ) : (
                        <span style={{ color: "var(--text3)" }}>{row.tx_status || "—"}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

    </div>
  );
}
