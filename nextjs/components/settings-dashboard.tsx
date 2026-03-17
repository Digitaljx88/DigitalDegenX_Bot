"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";
import { FACTORS, type FactorKey } from "@/lib/score-labels";

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

type ProfileSection = Record<string, number | boolean | string>;
type ProfileData = { defaults: Record<string, ProfileSection>; overrides: Record<string, ProfileSection>; effective: Record<string, ProfileSection> };
type StrategyProfilesResponse = { uid: number; profiles: Record<string, ProfileData> };

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

type WalletInfoResponse = {
  pubkey: string;
  sol: number;
  has_backup: boolean;
  backup_type?: string | null;
  backup_created_ts?: number | null;
  backup_mode?: string | null;
  source?: string;
  derivation_path?: string | null;
  mnemonic_word_count?: number | null;
  has_bip39_passphrase?: boolean;
  live_trading_enabled?: boolean;
  wallet_management_enabled?: boolean;
  supports_watch_only?: boolean;
  supports_multiple_live_wallets?: boolean;
};

type WalletCapability = {
  key: string;
  label: string;
  status: "supported" | "partial" | "unsupported";
  detail: string;
};

type WalletCapabilitiesResponse = {
  uid: number;
  live_trading_enabled?: boolean;
  capabilities: WalletCapability[];
};

type WalletManageResponse = {
  wallet: {
    public_key: string;
    derivation_path?: string | null;
    mnemonic_word_count?: number | null;
    has_bip39_passphrase?: boolean;
    backup_saved?: boolean;
    backup_type?: string | null;
    backup_mode?: string | null;
  };
  secrets?: {
    mnemonic?: string;
    private_key_base58?: string;
    recovery_code?: string;
  };
  security_notice?: string;
};

/* ── helpers ── */
function SPanel({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
      <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)" }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>{title}</h2>
        {subtitle && <p style={{ fontSize: 12, color: "var(--t3)", marginTop: 3 }}>{subtitle}</p>}
      </div>
      <div style={{ padding: 20 }}>{children}</div>
    </div>
  );
}

function Divider({ label }: { label: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 0" }}>
      <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
      <span style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.18em", color: "var(--t3)", whiteSpace: "nowrap" }}>{label}</span>
      <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
    </div>
  );
}

function FieldRow({
  label, help, children,
}: { label: string; help: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ fontSize: 11, fontWeight: 500, color: "var(--t2)" }}>{label}</div>
      <div>{children}</div>
      <div style={{ fontSize: 10, color: "var(--t3)", lineHeight: 1.5 }}>{help}</div>
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
      style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none", fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)", boxSizing: "border-box" }}
      className="focus:border-[var(--accent)] placeholder:text-[var(--t3)]"
    />
  );
}

function SliderField({
  label, help, value, onChange, min, max, step, unit, accentColor,
}: {
  label: string; help: string; value: string;
  onChange: (v: string) => void;
  min: number; max: number; step: number;
  unit?: string; accentColor?: string;
}) {
  const accent = accentColor || "var(--accent)";
  const accentRgb = accentColor ? undefined : undefined; // use CSS var if no override
  const numVal = Number(value) || 0;
  const fillPct = Math.min(100, Math.max(0, ((numVal - min) / (max - min)) * 100));
  const trackRef = useRef<HTMLDivElement>(null);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 11, fontWeight: 500, color: "var(--t2)" }}>{label}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="number"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            min={min}
            max={max}
            step={step}
            style={{
              width: 64, background: "var(--bg3)", border: "1px solid var(--border2)",
              borderRadius: 6, color: "var(--foreground)", fontSize: 12, padding: "4px 7px",
              outline: "none", fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
              textAlign: "right", boxSizing: "border-box",
            }}
            className="focus:border-[var(--accent)]"
          />
          {unit && <span style={{ fontSize: 11, color: "var(--t3)", minWidth: 14 }}>{unit}</span>}
        </div>
      </div>
      {/* Custom slider track */}
      <div
        ref={trackRef}
        style={{ position: "relative", height: 18, display: "flex", alignItems: "center" }}
      >
        {/* Track background */}
        <div style={{ position: "absolute", left: 0, right: 0, height: 4, borderRadius: 2, background: "var(--bg4)" }} />
        {/* Fill */}
        <div
          style={{
            position: "absolute", left: 0, height: 4, borderRadius: 2,
            width: `${fillPct}%`, background: accent, transition: "width 0.1s",
          }}
        />
        {/* Native range (invisible, on top for interaction) */}
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={numVal}
          onChange={(e) => onChange(e.target.value)}
          style={{
            position: "absolute", left: 0, right: 0, width: "100%",
            opacity: 0, cursor: "pointer", height: 18, margin: 0,
          }}
        />
      </div>
      <div style={{ fontSize: 10, color: "var(--t3)", lineHeight: 1.5 }}>{help}</div>
    </div>
  );
}

export function SettingsDashboard() {
  const { uid } = useActiveUid();
  const [autobuy, setAutobuy] = useState<AutoBuyConfig>({});
  const [settings, setSettings] = useState<Record<string, number | string | boolean>>({});
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [tradeControls, setTradeControls] = useState<TradeControls | null>(null);
  const [strategyProfiles, setStrategyProfiles] = useState<StrategyProfilesResponse | null>(null);
  const [autobuyActivity, setAutobuyActivity] = useState<AutoBuyActivityResponse | null>(null);
  const [walletInfo, setWalletInfo] = useState<WalletInfoResponse | null>(null);
  const [walletCapabilities, setWalletCapabilities] = useState<WalletCapability[]>([]);
  const [trackedWallets, setTrackedWallets] = useState<{ wallet: string; address: string; label: string }[]>([]);
  const [walletTab, setWalletTab] = useState<"create" | "import">("create");
  const [walletWordCount, setWalletWordCount] = useState<12 | 24>(12);
  const [walletDerivationPath, setWalletDerivationPath] = useState("m/44'/501'/0'/0'");
  const [walletPassphrase, setWalletPassphrase] = useState("");
  const [importMode, setImportMode] = useState<"mnemonic" | "private_key">("mnemonic");
  const [importMnemonic, setImportMnemonic] = useState("");
  const [importPrivateKey, setImportPrivateKey] = useState("");
  const [walletResult, setWalletResult] = useState<WalletManageResponse | null>(null);
  const [newWalletAddr, setNewWalletAddr] = useState("");
  const [newWalletLabel, setNewWalletLabel] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [showAdvancedA, setShowAdvancedA] = useState(false);
  const [showAdvancedB, setShowAdvancedB] = useState(false);
  const [showWeights, setShowWeights] = useState(false);

  useEffect(() => {
    async function load() {
      if (!uid) { setAutobuy({}); setSettings({}); setTradeControls(null); return; }
      try {
        const [abRes, settingsRes, modeRes, tcRes, spRes] = await Promise.all([
          apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
          apiFetch<SettingsResponse>(`/settings/${uid}`),
          apiFetch<ModeResponse>(`/mode`, { query: { uid } }),
          apiFetch<TradeControls>(`/trade-controls/${uid}`),
          apiFetch<StrategyProfilesResponse>(`/strategy-profiles/${uid}`),
        ]);
        setAutobuy(abRes);
        setSettings(settingsRes.settings || {});
        setMode(modeRes.mode || "paper");
        setTradeControls(tcRes);
        setStrategyProfiles(spRes);
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

  useEffect(() => {
    async function loadWallet() {
      if (!uid) { setWalletInfo(null); setTrackedWallets([]); setWalletCapabilities([]); return; }
      try {
        const [info, tracked, capabilities] = await Promise.allSettled([
          apiFetch<WalletInfoResponse>("/wallet/info"),
          apiFetch<{ wallets: { wallet: string; address: string; label: string }[] }>("/wallet/tracked"),
          apiFetch<WalletCapabilitiesResponse>("/wallet/capabilities"),
        ]);
        if (info.status === "fulfilled") setWalletInfo(info.value);
        if (tracked.status === "fulfilled") setTrackedWallets(tracked.value.wallets || []);
        if (capabilities.status === "fulfilled") setWalletCapabilities(capabilities.value.capabilities || []);
      } catch { /* wallet data is optional */ }
    }
    loadWallet();
  }, [uid]);

  function ab(field: keyof AutoBuyConfig) { return String(autobuy[field] ?? ""); }
  function setAb(field: keyof AutoBuyConfig, value: string | boolean | number) {
    setAutobuy((c) => ({ ...c, [field]: value }));
  }
  function setAbNum(field: keyof AutoBuyConfig, value: string) {
    setAutobuy((c) => ({ ...c, [field]: value === "" ? 0 : Number(value) }));
  }

  async function reloadWalletData() {
    if (!uid) {
      setWalletInfo(null);
      setTrackedWallets([]);
      setWalletCapabilities([]);
      return;
    }
    try {
      const [info, tracked, capabilities] = await Promise.allSettled([
        apiFetch<WalletInfoResponse>("/wallet/info"),
        apiFetch<{ wallets: { wallet: string; address: string; label: string }[] }>("/wallet/tracked"),
        apiFetch<WalletCapabilitiesResponse>("/wallet/capabilities"),
      ]);
      if (info.status === "fulfilled") setWalletInfo(info.value);
      if (tracked.status === "fulfilled") setTrackedWallets(tracked.value.wallets || []);
      if (capabilities.status === "fulfilled") setWalletCapabilities(capabilities.value.capabilities || []);
    } catch {
      /* wallet data is optional */
    }
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

  async function addTrackedWallet() {
    if (!uid || !newWalletAddr.trim()) return;
    try {
      await apiFetch("/wallet/tracked", {
        method: "POST",
        body: JSON.stringify({ address: newWalletAddr.trim(), label: newWalletLabel.trim() }),
      });
      const r = await apiFetch<{ wallets: { wallet: string; address: string; label: string }[] }>("/wallet/tracked");
      setTrackedWallets(r.wallets || []);
      setNewWalletAddr("");
      setNewWalletLabel("");
      setMessage("Wallet added.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to add wallet"); }
  }

  async function removeTrackedWallet(address: string) {
    if (!uid) return;
    try {
      await apiFetch(`/wallet/tracked/${encodeURIComponent(address)}`, { method: "DELETE" });
      setTrackedWallets((prev) => prev.filter((w) => w.wallet !== address && w.address !== address));
      setMessage("Wallet removed.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to remove wallet"); }
  }

  async function createLiveWallet() {
    if (!uid) return;
    try {
      const result = await apiFetch<WalletManageResponse>("/wallet/create", {
        method: "POST",
        body: JSON.stringify({
          uid,
          word_count: walletWordCount,
          derivation_path: walletDerivationPath.trim(),
          bip39_passphrase: walletPassphrase,
        }),
      });
      setWalletResult(result);
      await reloadWalletData();
      setMessage("Live wallet created and saved to the bot configuration.");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create wallet");
    }
  }

  async function importLiveWallet() {
    if (!uid) return;
    try {
      const result = await apiFetch<WalletManageResponse>("/wallet/import", {
        method: "POST",
        body: JSON.stringify({
          uid,
          mnemonic: importMode === "mnemonic" ? importMnemonic.trim() : "",
          private_key: importMode === "private_key" ? importPrivateKey.trim() : "",
          derivation_path: walletDerivationPath.trim(),
          bip39_passphrase: walletPassphrase,
        }),
      });
      setWalletResult(result);
      await reloadWalletData();
      setMessage("Live wallet imported and saved to the bot configuration.");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to import wallet");
    }
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
    const mcapMin = Number(payload["scanner_mcap_min"] || 0);
    const mcapMax = Number(payload["scanner_mcap_max"] || 0);
    if (mcapMin < 0 || mcapMax < 0) {
      setError("MCap filter values must be positive.");
      return;
    }
    if (mcapMax > 0 && mcapMin >= mcapMax) {
      setError("MCap Min must be less than MCap Max.");
      return;
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

  async function saveStrategyProfiles(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!strategyProfiles) return;
    // Build overrides: only send values that differ from defaults
    const overrides: Record<string, Record<string, Record<string, number | boolean | string>>> = {};
    for (const [profileName, profileData] of Object.entries(strategyProfiles.profiles)) {
      overrides[profileName] = profileData.overrides as Record<string, Record<string, number | boolean | string>>;
    }
    try {
      const updated = await apiFetch<StrategyProfilesResponse>(`/strategy-profiles/${uid}`, {
        method: "POST",
        body: JSON.stringify({ overrides }),
      });
      setStrategyProfiles(updated);
      setMessage("Strategy profiles saved.");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save strategy profiles"); }
  }

  function setProfileOverride(profileName: string, section: string, key: string, value: number | boolean | string) {
    setStrategyProfiles((prev) => {
      if (!prev) return prev;
      const updated = { ...prev, profiles: { ...prev.profiles } };
      const profile = { ...updated.profiles[profileName] };
      const currentOverrides = { ...profile.overrides };
      const currentSection = { ...(currentOverrides[section] as Record<string, number | boolean | string> || {}) };
      currentSection[key] = value;
      currentOverrides[section] = currentSection;
      profile.overrides = currentOverrides;
      // Update effective too
      const currentEffective = { ...profile.effective };
      const effectiveSection = { ...(currentEffective[section] as Record<string, number | boolean | string> || {}) };
      effectiveSection[key] = value;
      currentEffective[section] = effectiveSection;
      profile.effective = currentEffective;
      updated.profiles[profileName] = profile;
      return updated;
    });
  }

  function resetProfileOverrides(profileName: string) {
    setStrategyProfiles((prev) => {
      if (!prev) return prev;
      const updated = { ...prev, profiles: { ...prev.profiles } };
      const profile = { ...updated.profiles[profileName] };
      profile.overrides = {};
      profile.effective = { ...profile.defaults };
      updated.profiles[profileName] = profile;
      return updated;
    });
  }

  if (!uid) {
    return (
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, padding: 40, textAlign: "center" }}>
        <div style={{ fontSize: 13, color: "var(--t3)" }}>Set your Telegram UID in the top bar to edit settings.</div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {message && <div style={{ padding: "10px 14px", background: "rgba(34,211,160,0.1)", border: "1px solid rgba(34,211,160,0.25)", borderRadius: 8, fontSize: 12, color: "var(--green)" }}>{message}</div>}
      {error && <div style={{ padding: "10px 14px", background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, fontSize: 12, color: "var(--red)" }}>{error}</div>}

      {/* ── Mode ── */}
      <SPanel title="Mode" subtitle="Controls which trading path the bot treats as active.">
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 12, color: "var(--t3)" }}>Current:</span>
            <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", padding: "3px 10px", borderRadius: 20, background: mode === "live" ? "rgba(244,63,94,0.12)" : "rgba(34,211,160,0.1)", color: mode === "live" ? "var(--red)" : "var(--green)", border: `1px solid ${mode === "live" ? "rgba(244,63,94,0.25)" : "rgba(34,211,160,0.2)"}` }}>
              {mode === "live" ? "Live" : "Paper"}
            </span>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" onClick={() => saveMode("paper")} style={{ fontSize: 12, fontWeight: 500, padding: "7px 16px", borderRadius: 8, cursor: "pointer", background: "rgba(34,211,160,0.1)", color: "var(--green)", border: "1px solid rgba(34,211,160,0.2)" }}>Use Paper</button>
            <button type="button" onClick={() => saveMode("live")} style={{ fontSize: 12, fontWeight: 500, padding: "7px 16px", borderRadius: 8, cursor: "pointer", background: "rgba(244,63,94,0.1)", color: "var(--red)", border: "1px solid rgba(244,63,94,0.2)" }}>Use Live</button>
            <button type="button" onClick={() => { if (window.confirm("Reset paper wallet? This clears all positions and restores the starting balance.")) resetPaperWallet(); }} style={{ fontSize: 12, padding: "7px 14px", borderRadius: 8, cursor: "pointer", background: "transparent", color: "var(--t3)", border: "1px solid var(--border)" }}>Reset Paper Wallet</button>
          </div>
        </div>
      </SPanel>

      {/* ── Wallet ── */}
      <SPanel title="Wallet" subtitle="Live wallet for on-chain trading and tracked wallets for copy-trade alerts.">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {walletInfo && walletInfo.live_trading_enabled === false ? (
            <div style={{ padding: "12px 14px", background: "rgba(250,204,21,0.08)", border: "1px solid rgba(250,204,21,0.18)", borderRadius: 10, fontSize: 12, color: "#facc15" }}>
              Live wallet management is disabled during security lockdown. Watch-only wallet intel remains available, and sniper/autobuy will stay on paper mode until live trading is re-enabled.
            </div>
          ) : null}
          <div>
            <Divider label="Live Wallet" />
            {walletInfo?.pubkey ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 11, color: "var(--t3)" }}>Address:</span>
                  <code style={{ fontSize: 11, color: "var(--foreground)", fontFamily: "var(--font-mono, monospace)", background: "var(--bg3)", padding: "3px 8px", borderRadius: 6, border: "1px solid var(--border)", wordBreak: "break-all" }}>{walletInfo.pubkey}</code>
                </div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <div style={{ padding: "8px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--bg3)", fontSize: 11 }}>
                    <div style={{ color: "var(--t3)", marginBottom: 4 }}>Balance</div>
                    <div style={{ color: "var(--foreground)", fontWeight: 600, fontFamily: "var(--font-mono, monospace)" }}>{walletInfo.sol.toFixed(4)} SOL</div>
                  </div>
                  <div style={{ padding: "8px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--bg3)", fontSize: 11 }}>
                    <div style={{ color: "var(--t3)", marginBottom: 4 }}>Source</div>
                    <div style={{ color: "var(--foreground)", fontWeight: 600 }}>{walletInfo.source || "env"}</div>
                  </div>
                  <div style={{ padding: "8px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--bg3)", fontSize: 11 }}>
                    <div style={{ color: "var(--t3)", marginBottom: 4 }}>Derivation</div>
                    <div style={{ color: "var(--foreground)", fontWeight: 600, fontFamily: "var(--font-mono, monospace)" }}>{walletInfo.derivation_path || "n/a"}</div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 10, padding: "4px 8px", borderRadius: 999, border: "1px solid rgba(34,211,160,0.2)", background: "rgba(34,211,160,0.1)", color: "var(--green)" }}>
                    {walletInfo.mnemonic_word_count ? `${walletInfo.mnemonic_word_count}-word seed` : "Private key import"}
                  </span>
                  {walletInfo.has_bip39_passphrase ? (
                    <span style={{ fontSize: 10, padding: "4px 8px", borderRadius: 999, border: "1px solid rgba(250,204,21,0.24)", background: "rgba(250,204,21,0.12)", color: "#facc15" }}>
                      BIP-39 passphrase enabled
                    </span>
                  ) : null}
                  {walletInfo.backup_mode ? (
                    <span style={{ fontSize: 10, padding: "4px 8px", borderRadius: 999, border: "1px solid rgba(34,211,160,0.2)", background: "rgba(34,211,160,0.1)", color: "var(--green)" }}>
                      Backup: manual offline
                    </span>
                  ) : (
                    <span style={{ fontSize: 10, padding: "4px 8px", borderRadius: 999, border: "1px solid rgba(244,63,94,0.2)", background: "rgba(244,63,94,0.08)", color: "var(--red)" }}>
                      No seed backup in app
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <a href={`https://solscan.io/account/${walletInfo.pubkey}`} target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: "var(--accent)", textDecoration: "none" }}>Solscan</a>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 12, color: "var(--t3)", marginTop: 8 }}>No live wallet configured. Create or import one below, or keep using `WALLET_PRIVATE_KEY` in `.env`.</div>
            )}
          </div>

          <div>
            <Divider label="Wallet Setup" />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8, marginBottom: 12, opacity: walletInfo?.wallet_management_enabled === false ? 0.6 : 1 }}>
              <button type="button" onClick={() => setWalletTab("create")} style={{ fontSize: 11, fontWeight: 600, padding: "7px 14px", borderRadius: 999, cursor: "pointer", background: walletTab === "create" ? "rgba(34,211,160,0.1)" : "var(--bg3)", color: walletTab === "create" ? "var(--green)" : "var(--t3)", border: `1px solid ${walletTab === "create" ? "rgba(34,211,160,0.2)" : "var(--border)"}` }}>Create</button>
              <button type="button" onClick={() => setWalletTab("import")} style={{ fontSize: 11, fontWeight: 600, padding: "7px 14px", borderRadius: 999, cursor: "pointer", background: walletTab === "import" ? "rgba(34,211,160,0.1)" : "var(--bg3)", color: walletTab === "import" ? "var(--green)" : "var(--t3)", border: `1px solid ${walletTab === "import" ? "rgba(34,211,160,0.2)" : "var(--border)"}` }}>Import</button>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10, marginBottom: 12, opacity: walletInfo?.wallet_management_enabled === false ? 0.6 : 1 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>Word count</span>
                <select value={String(walletWordCount)} onChange={(e) => setWalletWordCount(Number(e.target.value) === 24 ? 24 : 12)} style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none" }}>
                  <option value="12">12 words</option>
                  <option value="24">24 words</option>
                </select>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>Derivation path</span>
                <input type="text" value={walletDerivationPath} onChange={(e) => setWalletDerivationPath(e.target.value)} placeholder="m/44'/501'/0'/0'" style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)", boxSizing: "border-box" }} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>BIP-39 passphrase</span>
                <input type="password" value={walletPassphrase} onChange={(e) => setWalletPassphrase(e.target.value)} placeholder="Optional 25th word" style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none", boxSizing: "border-box" }} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, justifyContent: "flex-end" }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>Backup policy</span>
                <div style={{ minHeight: 38, display: "flex", alignItems: "center", padding: "8px 10px", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12 }}>
                  Manual offline backup only
                </div>
              </div>
            </div>

            {walletTab === "create" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ fontSize: 11, color: "var(--t3)", lineHeight: 1.6 }}>
                  This creates a new live bot wallet, derives it from the selected BIP-39 settings, and persists the signing key server-side for live trading. The seed phrase is shown once and must be stored offline by the operator.
                </div>
                <button type="button" disabled={walletInfo?.wallet_management_enabled === false} onClick={createLiveWallet} style={{ alignSelf: "flex-start", fontSize: 12, fontWeight: 600, padding: "8px 16px", borderRadius: 8, cursor: walletInfo?.wallet_management_enabled === false ? "not-allowed" : "pointer", background: walletInfo?.wallet_management_enabled === false ? "var(--bg3)" : "rgba(34,211,160,0.1)", color: walletInfo?.wallet_management_enabled === false ? "var(--t3)" : "var(--green)", border: `1px solid ${walletInfo?.wallet_management_enabled === false ? "var(--border)" : "rgba(34,211,160,0.2)"}` }}>Create live wallet</button>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button type="button" onClick={() => setImportMode("mnemonic")} style={{ fontSize: 11, fontWeight: 600, padding: "7px 12px", borderRadius: 999, cursor: "pointer", background: importMode === "mnemonic" ? "rgba(59,130,246,0.12)" : "var(--bg3)", color: importMode === "mnemonic" ? "#93c5fd" : "var(--t3)", border: `1px solid ${importMode === "mnemonic" ? "rgba(59,130,246,0.25)" : "var(--border)"}` }}>Seed phrase</button>
                  <button type="button" onClick={() => setImportMode("private_key")} style={{ fontSize: 11, fontWeight: 600, padding: "7px 12px", borderRadius: 999, cursor: "pointer", background: importMode === "private_key" ? "rgba(59,130,246,0.12)" : "var(--bg3)", color: importMode === "private_key" ? "#93c5fd" : "var(--t3)", border: `1px solid ${importMode === "private_key" ? "rgba(59,130,246,0.25)" : "var(--border)"}` }}>Private key</button>
                </div>
                {importMode === "mnemonic" ? (
                  <textarea value={importMnemonic} onChange={(e) => setImportMnemonic(e.target.value)} rows={4} placeholder="Paste 12 or 24 words" style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "10px 12px", outline: "none", boxSizing: "border-box", resize: "vertical" }} />
                ) : (
                  <textarea value={importPrivateKey} onChange={(e) => setImportPrivateKey(e.target.value)} rows={3} placeholder="Paste base58 private key" style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "10px 12px", outline: "none", boxSizing: "border-box", resize: "vertical", fontFamily: "var(--font-mono, monospace)" }} />
                )}
                <button type="button" onClick={importLiveWallet} disabled={walletInfo?.wallet_management_enabled === false || (importMode === "mnemonic" ? !importMnemonic.trim() : !importPrivateKey.trim())} style={{ alignSelf: "flex-start", fontSize: 12, fontWeight: 600, padding: "8px 16px", borderRadius: 8, cursor: walletInfo?.wallet_management_enabled === false ? "not-allowed" : ((importMode === "mnemonic" ? importMnemonic.trim() : importPrivateKey.trim()) ? "pointer" : "not-allowed"), background: walletInfo?.wallet_management_enabled === false ? "var(--bg3)" : ((importMode === "mnemonic" ? importMnemonic.trim() : importPrivateKey.trim()) ? "rgba(59,130,246,0.12)" : "var(--bg3)"), color: walletInfo?.wallet_management_enabled === false ? "var(--t3)" : ((importMode === "mnemonic" ? importMnemonic.trim() : importPrivateKey.trim()) ? "#93c5fd" : "var(--t3)"), border: `1px solid ${walletInfo?.wallet_management_enabled === false ? "var(--border)" : ((importMode === "mnemonic" ? importMnemonic.trim() : importPrivateKey.trim()) ? "rgba(59,130,246,0.25)" : "var(--border)")}` }}>Import live wallet</button>
              </div>
            )}

            {walletResult ? (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px", borderRadius: 10, background: "rgba(250,204,21,0.08)", border: "1px solid rgba(250,204,21,0.18)" }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#facc15", textTransform: "uppercase", letterSpacing: "0.08em" }}>Sensitive wallet material</div>
                <div style={{ fontSize: 11, color: "var(--foreground)" }}>Address: <code style={{ fontFamily: "var(--font-mono, monospace)" }}>{walletResult.wallet.public_key}</code></div>
                {walletResult.secrets?.mnemonic ? <div style={{ fontSize: 11, color: "var(--foreground)", lineHeight: 1.6 }}>Seed phrase: <code style={{ fontFamily: "var(--font-mono, monospace)", wordBreak: "break-word" }}>{walletResult.secrets.mnemonic}</code></div> : null}
                {walletResult.secrets?.private_key_base58 ? <div style={{ fontSize: 11, color: "var(--foreground)", lineHeight: 1.6 }}>Private key: <code style={{ fontFamily: "var(--font-mono, monospace)", wordBreak: "break-word" }}>{walletResult.secrets.private_key_base58}</code></div> : null}
                {walletResult.secrets?.recovery_code ? <div style={{ fontSize: 11, color: "var(--foreground)" }}>Recovery code: <code style={{ fontFamily: "var(--font-mono, monospace)" }}>{walletResult.secrets.recovery_code}</code></div> : null}
                <div style={{ fontSize: 10, color: "#facc15", lineHeight: 1.6 }}>Store this material offline before refreshing or leaving the page. The website does not retain a recoverable seed backup.</div>
                {walletResult.security_notice ? <div style={{ fontSize: 10, color: "var(--t3)", lineHeight: 1.6 }}>{walletResult.security_notice}</div> : null}
              </div>
            ) : null}
          </div>

          <div>
            <Divider label="Security Coverage" />
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
              {walletCapabilities.map((item) => (
                <div key={item.key} style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 12px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 8 }}>
                  <span style={{ flexShrink: 0, fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", padding: "4px 8px", borderRadius: 999, background: item.status === "supported" ? "rgba(34,211,160,0.1)" : item.status === "partial" ? "rgba(250,204,21,0.12)" : "rgba(244,63,94,0.08)", color: item.status === "supported" ? "var(--green)" : item.status === "partial" ? "#facc15" : "var(--red)", border: `1px solid ${item.status === "supported" ? "rgba(34,211,160,0.2)" : item.status === "partial" ? "rgba(250,204,21,0.24)" : "rgba(244,63,94,0.2)"}` }}>
                    {item.status}
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)" }}>{item.label}</div>
                    <div style={{ fontSize: 10, color: "var(--t3)", lineHeight: 1.5, marginTop: 2 }}>{item.detail}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Tracked wallets */}
          <div>
            <Divider label="Tracked Wallets" />
            <div style={{ fontSize: 11, color: "var(--t3)", marginTop: 4, marginBottom: 8 }}>Watch-only addresses for buy-activity alerts and wallet intel.</div>

            {trackedWallets.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 }}>
                {trackedWallets.map((w) => (
                  <div key={w.wallet || w.address} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 8 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)" }}>{w.label}</div>
                      <div style={{ fontSize: 10, color: "var(--t3)", fontFamily: "var(--font-mono, monospace)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.wallet || w.address}</div>
                    </div>
                    <a href={`https://solscan.io/account/${w.wallet || w.address}`} target="_blank" rel="noopener noreferrer" style={{ fontSize: 10, color: "var(--accent)", textDecoration: "none", flexShrink: 0 }}>View</a>
                    <button type="button" onClick={() => removeTrackedWallet(w.wallet || w.address)} style={{ fontSize: 10, padding: "4px 10px", borderRadius: 6, cursor: "pointer", background: "rgba(244,63,94,0.08)", color: "var(--red)", border: "1px solid rgba(244,63,94,0.2)", flexShrink: 0 }}>Remove</button>
                  </div>
                ))}
              </div>
            )}

            {/* Add wallet form */}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div style={{ flex: "1 1 280px", display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>Wallet address</span>
                <input
                  type="text"
                  value={newWalletAddr}
                  onChange={(e) => setNewWalletAddr(e.target.value)}
                  placeholder="Solana address..."
                  style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)", boxSizing: "border-box" }}
                />
              </div>
              <div style={{ flex: "0 1 140px", display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: 10, color: "var(--t3)" }}>Label (optional)</span>
                <input
                  type="text"
                  value={newWalletLabel}
                  onChange={(e) => setNewWalletLabel(e.target.value)}
                  placeholder="e.g. Smart Money"
                  style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none", boxSizing: "border-box" }}
                />
              </div>
              <button type="button" onClick={addTrackedWallet} disabled={!newWalletAddr.trim()} style={{ fontSize: 12, fontWeight: 500, padding: "8px 18px", borderRadius: 8, cursor: newWalletAddr.trim() ? "pointer" : "not-allowed", background: newWalletAddr.trim() ? "rgba(34,211,160,0.1)" : "var(--bg3)", color: newWalletAddr.trim() ? "var(--green)" : "var(--t3)", border: `1px solid ${newWalletAddr.trim() ? "rgba(34,211,160,0.2)" : "var(--border)"}`, flexShrink: 0 }}>Add</button>
            </div>
          </div>
        </div>
      </SPanel>

      {/* ── Auto-Buy — full settings ── */}
      <SPanel title="Auto-Buy" subtitle="All parameters that control when and how the bot executes automatic buys.">
        {autobuy.spent_today != null && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, padding: "8px 12px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 11 }}>
            <span style={{ color: "var(--t3)" }}>Spent today:</span>
            <span style={{ fontFamily: "var(--font-mono, monospace)", color: "var(--foreground)" }}>{Number(autobuy.spent_today).toFixed(4)} SOL</span>
            <span style={{ color: "var(--t3)" }}>/ {autobuy.daily_limit_sol ? `${autobuy.daily_limit_sol} SOL limit` : "no daily limit set"}</span>
          </div>
        )}
        <form style={{ display: "flex", flexDirection: "column", gap: 16 }} onSubmit={saveAutobuy}>

          {/* Toggles */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderRadius: 8, background: "var(--bg3)", border: "1px solid var(--border)", cursor: "pointer" }}>
              <input type="checkbox" checked={Boolean(autobuy.enabled)} onChange={(e) => setAb("enabled", e.target.checked)} />
              <div>
                <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>Enabled</div>
                <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2 }}>Master switch — bot will not auto-buy when off</div>
              </div>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderRadius: 8, background: "var(--bg3)", border: "1px solid var(--border)", cursor: "pointer" }}>
              <input type="checkbox" checked={Boolean(autobuy.confidence_scale_enabled ?? true)} onChange={(e) => setAb("confidence_scale_enabled", e.target.checked)} />
              <div>
                <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>Confidence Scaling</div>
                <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2 }}>Scales position up to max SOL based on confidence score</div>
              </div>
            </label>
          </div>

          <Divider label="Position Sizing" />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
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
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
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
                style={{ width: "100%", background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 8, color: "var(--foreground)", fontSize: 12, padding: "8px 10px", outline: "none" }}
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
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
            <FieldRow label="Min MCap (USD)" help="Skip tokens below this market cap. 0 = no minimum.">
              <NumInput value={ab("min_mcap_usd")} onChange={(v) => setAbNum("min_mcap_usd", v)} placeholder="0" step="100" />
            </FieldRow>
            <FieldRow label="Max MCap (USD)" help="Skip tokens above this market cap — avoids already-pumped tokens. (Default $500K)">
              <NumInput value={ab("max_mcap")} onChange={(v) => setAbNum("max_mcap", v)} placeholder="500000" step="1000" />
            </FieldRow>
          </div>

          <Divider label="Liquidity Filters" />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
            <FieldRow label="Min Liquidity (USD)" help="Skip tokens with less liquidity than this. 0 = off.">
              <NumInput value={ab("min_liquidity_usd")} onChange={(v) => setAbNum("min_liquidity_usd", v)} placeholder="0" step="100" />
            </FieldRow>
            <FieldRow label="Max Liquidity (USD)" help="Skip tokens with more liquidity than this (over-established). 0 = off.">
              <NumInput value={ab("max_liquidity_usd")} onChange={(v) => setAbNum("max_liquidity_usd", v)} placeholder="0" step="100" />
            </FieldRow>
          </div>

          <Divider label="Age Filters" />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
            <FieldRow label="Min Age (mins)" help="Skip tokens younger than this. 0 = no minimum.">
              <NumInput value={ab("min_age_mins")} onChange={(v) => setAbNum("min_age_mins", v)} placeholder="0" step="1" />
            </FieldRow>
            <FieldRow label="Max Age (mins)" help="Skip tokens older than this — keeps buys early. 0 = no limit.">
              <NumInput value={ab("max_age_mins")} onChange={(v) => setAbNum("max_age_mins", v)} placeholder="0" step="1" />
            </FieldRow>
          </div>

          <Divider label="Exposure Limits" />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
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
          <div style={{ marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>Recent Activity (24h)</div>
              <Link href="/autobuy" style={{ fontSize: 11, color: "var(--t3)", textDecoration: "none" }} className="hover:text-white">Full log →</Link>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8 }}>
              {[
                { label: "Executed", value: autobuyActivity.summary.status_counts.executed || 0, color: "var(--green)" },
                { label: "Blocked", value: autobuyActivity.summary.status_counts.blocked || 0, color: "var(--t2)" },
                { label: "Top Blocker", value: (autobuyActivity.summary.top_block_category || "none").replaceAll("_", " "), color: "var(--yellow)" },
                { label: "Avg Confidence", value: Number(autobuyActivity.summary.avg_confidence || 0).toFixed(2), color: "var(--foreground)" },
              ].map((s) => (
                <div key={s.label} style={{ background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px" }}>
                  <div style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 3 }}>{s.label}</div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: s.color, fontFamily: "var(--font-mono, monospace)" }}>{s.value}</div>
                </div>
              ))}
            </div>
            {autobuyActivity.latest && (
              <div style={{ background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px" }}>
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>{autobuyActivity.latest.symbol || autobuyActivity.latest.name || "Unknown"}</span>
                  <span style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", padding: "2px 7px", borderRadius: 20, background: autobuyActivity.latest.status === "executed" ? "rgba(34,211,160,0.12)" : autobuyActivity.latest.status === "failed" ? "rgba(244,63,94,0.12)" : "rgba(100,116,139,0.12)", color: autobuyActivity.latest.status === "executed" ? "var(--green)" : autobuyActivity.latest.status === "failed" ? "var(--red)" : "var(--t2)" }}>
                    {autobuyActivity.latest.status}
                  </span>
                  {autobuyActivity.latest.block_category && (
                    <span style={{ fontSize: 9, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", padding: "2px 6px", borderRadius: 4, background: "rgba(251,191,36,0.1)", color: "var(--yellow)", border: "1px solid rgba(251,191,36,0.2)" }}>
                      {autobuyActivity.latest.block_category.replaceAll("_", " ")}
                    </span>
                  )}
                </div>
                {autobuyActivity.latest.block_reason && (
                  <div style={{ marginTop: 6, fontSize: 11, color: "var(--red)" }}>{autobuyActivity.latest.block_reason}</div>
                )}
              </div>
            )}
          </div>
        ) : null}
      </SPanel>

      {/* ── Heat Score Settings ── */}
      <SPanel title="Heat Score Settings" subtitle="Controls when you get alerts, how conservative the filter is, and what the score values most.">
        <form style={{ display: "flex", flexDirection: "column", gap: 0 }} onSubmit={saveThresholds}>

          {/* ── Section A: Alert Sensitivity ── */}
          <div style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 1 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)" }}>Alert Sensitivity</span>
                <span style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>when do I get notified?</span>
              </div>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
              <SliderField label="Ultra Hot Alert" help="Score needed for the highest-tier Telegram alert. (Default 85)" value={String(settings["alert_ultra_hot_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, alert_ultra_hot_threshold: Number(v || 0) }))} min={0} max={100} step={1} accentColor="#f97316" />
              <SliderField label="Hot Alert" help="Score for HOT tier alerts. (Default 70)" value={String(settings["alert_hot_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, alert_hot_threshold: Number(v || 0) }))} min={0} max={100} step={1} accentColor="#fbbf24" />
              <SliderField label="Warm Alert" help="Score for WARM tier alerts. (Default 55)" value={String(settings["alert_warm_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, alert_warm_threshold: Number(v || 0) }))} min={0} max={100} step={1} accentColor="#a78bfa" />
              <SliderField label="Scout Threshold" help="Minimum score to add a token to the watchlist. (Default 35)" value={String(settings["alert_scouted_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, alert_scouted_threshold: Number(v || 0) }))} min={0} max={100} step={1} accentColor="#60a5fa" />
            </div>
            <button
              type="button"
              onClick={() => setShowAdvancedA((v) => !v)}
              style={{ marginTop: 10, fontSize: 11, color: "var(--t3)", background: "none", border: "none", cursor: "pointer", padding: 0 }}
              className="hover:text-[var(--foreground)] transition-colors"
            >
              {showAdvancedA ? "▲ Hide advanced" : "▼ Show advanced"}
            </button>
            {showAdvancedA && (
              <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
                <SliderField label="Brewing Tier" help="Minimum score for BREWING internal tier. (Default 35)" value={String(settings["scout_tier_brewing_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, scout_tier_brewing_threshold: Number(v || 0) }))} min={0} max={100} step={1} />
                <SliderField label="Warm Tier" help="Minimum score for WARM internal tier. (Default 50)" value={String(settings["scout_tier_warm_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, scout_tier_warm_threshold: Number(v || 0) }))} min={0} max={100} step={1} />
                <SliderField label="Hot Tier" help="Minimum score for HOT internal tier. (Default 70)" value={String(settings["scout_tier_hot_threshold"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, scout_tier_hot_threshold: Number(v || 0) }))} min={0} max={100} step={1} />
              </div>
            )}
          </div>

          {/* ── Section B: Risk Tolerance ── */}
          <div style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 1 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)" }}>Risk Tolerance</span>
                <span style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>how conservative is the filter?</span>
              </div>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
            </div>

            {/* Presets */}
            <div style={{ marginBottom: 14, display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ fontSize: 10, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)" }}>Quick Presets</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
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
                        "conservative": { alert_ultra_hot_threshold: 95, alert_hot_threshold: 90, alert_warm_threshold: 80, alert_scouted_threshold: 70, risk_dev_sell_threshold_pct: 30, risk_top_holder_threshold_pct: 10, liquidity_min_usd: 100000, scanner_mcap_min: 50000, scanner_mcap_max: 5_000_000 },
                        "balanced":     { alert_ultra_hot_threshold: 85, alert_hot_threshold: 70, alert_warm_threshold: 55, alert_scouted_threshold: 35, risk_dev_sell_threshold_pct: 50, risk_top_holder_threshold_pct: 20, liquidity_min_usd: 50000,  scanner_mcap_min: 15000, scanner_mcap_max: 10_000_000 },
                        "aggressive":   { alert_ultra_hot_threshold: 85, alert_hot_threshold: 70, alert_warm_threshold: 55, alert_scouted_threshold: 40, risk_dev_sell_threshold_pct: 70, risk_top_holder_threshold_pct: 30, liquidity_min_usd: 10000,  scanner_mcap_min: 10000, scanner_mcap_max: 15_000_000 },
                        "whale-mode":   { alert_ultra_hot_threshold: 80, alert_hot_threshold: 65, alert_warm_threshold: 50, alert_scouted_threshold: 30, risk_dev_sell_threshold_pct: 90, risk_top_holder_threshold_pct: 50, liquidity_min_usd: 2000,   scanner_mcap_min: 5000,  scanner_mcap_max: 50_000_000 },
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
                    style={{ padding: "8px 12px", borderRadius: 8, background: "var(--bg3)", border: "1px solid var(--border)", cursor: "pointer", textAlign: "left" }}
                    className="hover:border-[var(--border2)] transition-colors"
                  >
                    <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>{p.label}</div>
                    <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2 }}>{p.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
              <SliderField label="Dev Dump Limit %" help="Dev selling more than this % triggers disqualification. (Default 50)" value={String(settings["risk_dev_sell_threshold_pct"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, risk_dev_sell_threshold_pct: Number(v || 0) }))} min={0} max={100} step={1} unit="%" accentColor="#22d3a0" />
              <SliderField label="Whale Concentration Cap %" help="Single holder above this % triggers disqualification. (Default 20)" value={String(settings["risk_top_holder_threshold_pct"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, risk_top_holder_threshold_pct: Number(v || 0) }))} min={0} max={100} step={1} unit="%" accentColor="#22d3a0" />
              <SliderField label="Bundle Risk Penalty" help="How heavily bundled tokens are penalised in the score (0–100). (Default 50)" value={String(settings["risk_bundle_severity"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, risk_bundle_severity: Number(v || 0) }))} min={0} max={100} step={1} accentColor="#22d3a0" />
              <FieldRow label="Minimum Pool Size" help="USD threshold for full liquidity score. (Default $50K)">
                <NumInput value={String(settings["liquidity_min_usd"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, liquidity_min_usd: Number(v || 0) }))} step="500" />
              </FieldRow>
            </div>
            <button
              type="button"
              onClick={() => setShowAdvancedB((v) => !v)}
              style={{ marginTop: 10, fontSize: 11, color: "var(--t3)", background: "none", border: "none", cursor: "pointer", padding: 0 }}
              className="hover:text-[var(--foreground)] transition-colors"
            >
              {showAdvancedB ? "▲ Hide advanced" : "▼ Show advanced"}
            </button>
            {showAdvancedB && (
              <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
                <FieldRow label="Good Pool Size (USD)" help="USD threshold for a solid liquidity score. (Default $10K)">
                  <NumInput value={String(settings["liquidity_good_usd"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, liquidity_good_usd: Number(v || 0) }))} step="500" />
                </FieldRow>
                <FieldRow label="Fair Pool Size (USD)" help="USD threshold for a baseline liquidity score. (Default $2K)">
                  <NumInput value={String(settings["liquidity_fair_usd"] ?? 0)} onChange={(v) => setSettings((c) => ({ ...c, liquidity_fair_usd: Number(v || 0) }))} step="500" />
                </FieldRow>
              </div>
            )}
          </div>

          {/* ── Section C: Score Weights ── */}
          <div style={{ marginBottom: 20 }}>
            <button
              type="button"
              onClick={() => setShowWeights((v) => !v)}
              style={{
                width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "10px 14px", borderRadius: 8, background: "var(--bg3)",
                border: "1px solid var(--border)", cursor: "pointer", marginBottom: showWeights ? 14 : 0,
              }}
              className="hover:border-[var(--border2)] transition-colors"
            >
              <div style={{ textAlign: "left" }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>Score Weights</div>
                <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 1 }}>What matters most to me? — customize scoring weights</div>
              </div>
              <span style={{ fontSize: 11, color: "var(--t3)" }}>{showWeights ? "▲" : "▼"}</span>
            </button>
            {showWeights && (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {(
                  [
                    {
                      factorKey: "momentum" as FactorKey,
                      fields: [
                        ["momentum_weight_usd_vol",           "Volume Weight",    "Importance of USD trading volume vs token age (1–100). (Default 50)", 1, 100, 1],
                        ["momentum_weight_creation_momentum", "Newness Weight",   "Importance of token freshness/age in momentum score (1–100). (Default 50)", 1, 100, 1],
                        ["momentum_min_vol",                  "Min Volume (USD)", "USD volume needed for full momentum points. (Default 5000)", 0, 50000, 100],
                      ] as [string, string, string, number, number, number][],
                    },
                    {
                      factorKey: "social_narrative" as FactorKey,
                      fields: [
                        ["social_twitter_follower_min",      "Twitter Min Followers",  "Followers needed for full social points. (Default 1000)", 0, 10000, 100],
                        ["social_narrative_trending_boost",  "Narrative Boost Weight", "How much a trending narrative boosts the score (0–100). (Default 50)", 0, 100, 1],
                      ] as [string, string, string, number, number, number][],
                    },
                    {
                      factorKey: "wallets" as FactorKey,
                      fields: [
                        ["wallet_cluster_boost_pts",    "Cluster Match Bonus",    "Points added when known wallet clusters are matched (0–15). (Default 5)", 0, 15, 1],
                        ["wallet_known_seed_boost_pts", "Known Seed Wallet Bonus","Points for entry from a known seed wallet (0–15). (Default 8)", 0, 15, 1],
                      ] as [string, string, string, number, number, number][],
                    },
                    {
                      factorKey: "migration" as FactorKey,
                      fields: [
                        ["migration_new_boost_pts",        "New Token Bonus",  "Points for tokens under 1 hour old (0–10). (Default 8)", 0, 10, 1],
                        ["migration_grad_boost_pts",       "Graduation Bonus", "Points for pump.fun graduates (0–10). (Default 6)", 0, 10, 1],
                        ["migration_migrated_penalty_pts", "Migration Penalty","Points deducted for already-migrated tokens (0–10). (Default 2)", 0, 10, 1],
                      ] as [string, string, string, number, number, number][],
                    },
                    {
                      factorKey: "directional_bias" as FactorKey,
                      fields: [
                        ["bias_buy_threshold_pct",      "Buy Bias Max %",  "Buy % needed for the maximum buy-pressure score (0–100). (Default 70)", 0, 100, 1],
                        ["bias_buy_good_threshold_pct", "Buy Bias Good %", "Buy % for a strong (but not max) buy-pressure score (0–100). (Default 60)", 0, 100, 1],
                      ] as [string, string, string, number, number, number][],
                    },
                    {
                      factorKey: "volume_trend" as FactorKey,
                      fields: [
                        ["trend_explosive_threshold", "Explosive Trend Pts", "Max points awarded for explosive volume growth (0–5). (Default 5)", 0, 5, 1],
                        ["trend_strong_threshold",    "Strong Trend Pts",    "Points for strong (but not explosive) volume growth (0–5). (Default 3)", 0, 5, 1],
                      ] as [string, string, string, number, number, number][],
                    },
                  ]
                ).map(({ factorKey, fields }) => {
                  const meta = FACTORS[factorKey];
                  return (
                    <div
                      key={factorKey}
                      style={{
                        borderRadius: 10, border: `1px solid rgba(${meta.colorRgb}, 0.18)`,
                        background: `rgba(${meta.colorRgb}, 0.05)`, padding: "12px 14px",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 12 }}>
                        <span style={{ fontSize: 16 }}>{meta.icon}</span>
                        <div>
                          <span style={{ fontSize: 12, fontWeight: 600, color: meta.color }}>{meta.label}</span>
                          <span style={{ fontSize: 10, color: "var(--t3)", marginLeft: 6 }}>up to {meta.maxPts} pts</span>
                        </div>
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
                        {fields.map(([key, label, help, min, max, step]) => (
                          <SliderField
                            key={key}
                            label={label}
                            help={help}
                            value={String(settings[key] ?? 0)}
                            onChange={(v) => setSettings((c) => ({ ...c, [key]: Number(v || 0) }))}
                            min={min}
                            max={max}
                            step={step}
                            accentColor={meta.color}
                          />
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* ── Section D: Scanner MCap Filters ── */}
          <div style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 1 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: "var(--foreground)" }}>Scanner MCap Filter</span>
                <span style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>which tokens reach your alerts?</span>
              </div>
              <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
            </div>
            <p style={{ fontSize: 11, color: "var(--t3)", marginBottom: 12, lineHeight: 1.6 }}>
              Tokens outside this market cap window are silently filtered <em>before</em> they score — they will never appear in your alerts or watchlist regardless of heat score.
              Defaults: min <code>$15K</code> · max <code>$10M</code>.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14 }}>
              <FieldRow label="Min MCap (USD)" help="Tokens below this market cap are ignored. Raise to filter out micro-dust. (Default $15,000)">
                <NumInput
                  value={String(settings["scanner_mcap_min"] ?? "")}
                  onChange={(v) => setSettings((c) => ({ ...c, scanner_mcap_min: v === "" ? 15000 : Number(v) }))}
                  placeholder="15000"
                  step="1000"
                />
              </FieldRow>
              <FieldRow label="Max MCap (USD)" help="Tokens above this market cap are ignored. Lower to focus on earlier-stage gems. (Default $10,000,000)">
                <NumInput
                  value={String(settings["scanner_mcap_max"] ?? "")}
                  onChange={(v) => setSettings((c) => ({ ...c, scanner_mcap_max: v === "" ? 10000000 : Number(v) }))}
                  placeholder="10000000"
                  step="100000"
                />
              </FieldRow>
            </div>
            <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
              {[
                { label: "Ultra-early ($5K–$100K)",  min: 5000,   max: 100_000 },
                { label: "Early ($10K–$500K)",        min: 10000,  max: 500_000 },
                { label: "Default ($15K–$10M)",       min: 15000,  max: 10_000_000 },
                { label: "Mid-cap ($50K–$50M)",       min: 50000,  max: 50_000_000 },
              ].map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => setSettings((c) => ({ ...c, scanner_mcap_min: p.min, scanner_mcap_max: p.max }))}
                  style={{ fontSize: 11, padding: "5px 10px", borderRadius: 7, background: "var(--bg3)", border: "1px solid var(--border)", cursor: "pointer", color: "var(--t2)" }}
                  className="hover:border-[var(--border2)] hover:text-white transition-colors"
                >{p.label}</button>
              ))}
            </div>
          </div>

          <button type="submit" className="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-[var(--accent-foreground)] hover:opacity-90">
            Save Heat Score Settings
          </button>
        </form>
      </SPanel>

      {/* ── Trade Controls ── */}
      <SPanel title="Trade Controls" subtitle="Default multiplier presets and global exit protections applied to new buys.">
        {tradeControls ? (
          <form style={{ display: "flex", flexDirection: "column", gap: 16 }} onSubmit={saveTradeControls}>
            <label style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderRadius: 8, background: "var(--bg3)", border: "1px solid var(--border)", cursor: "pointer" }}>
              <input type="checkbox" checked={Boolean(tradeControls.presets_enabled)} onChange={(e) => setTradeControls((c) => c ? { ...c, presets_enabled: e.target.checked } : c)} />
              <div>
                <div style={{ fontSize: 12, fontWeight: 500, color: "var(--foreground)" }}>Apply presets to new buys</div>
                <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2 }}>Automatically assign multiplier targets when the bot buys</div>
              </div>
            </label>

            <Divider label="Multiplier Presets" />
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {(tradeControls.presets || []).map((preset, i) => (
                <div key={`preset-${i}`} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 8 }}>
                  <input value={String(preset.mult ?? 0)}
                    onChange={(e) => setTradeControls((c) => c ? { ...c, presets: c.presets.map((r, ri) => ri === i ? { ...r, mult: Number(e.target.value || 0) } : r) } : c)}
                    style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }}
                    placeholder="Multiplier (e.g. 2)" />
                  <input value={String(preset.sell_pct ?? 0)}
                    onChange={(e) => setTradeControls((c) => c ? { ...c, presets: c.presets.map((r, ri) => ri === i ? { ...r, sell_pct: Number(e.target.value || 0) } : r) } : c)}
                    style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }}
                    placeholder="Sell %" />
                  <button type="button"
                    onClick={() => setTradeControls((c) => c ? { ...c, presets: c.presets.filter((_, ri) => ri !== i) } : c)}
                    style={{ fontSize: 11, padding: "6px 10px", borderRadius: 6, background: "transparent", color: "var(--t3)", border: "1px solid var(--border)", cursor: "pointer" }}
                    className="hover:text-white hover:border-[var(--border2)]">Remove</button>
                </div>
              ))}
              <button type="button"
                onClick={() => setTradeControls((c) => c ? { ...c, presets: [...c.presets, { mult: 2, sell_pct: 50 }] } : c)}
                style={{ fontSize: 11, padding: "6px 12px", borderRadius: 6, background: "transparent", color: "var(--t3)", border: "1px solid var(--border)", cursor: "pointer", marginTop: 4 }}
                className="hover:text-white hover:border-[var(--border2)]">+ Add preset</button>
            </div>

            <Divider label="Global Exit Protections" />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
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
          <div style={{ fontSize: 12, color: "var(--t3)" }}>Loading trade controls…</div>
        )}
      </SPanel>

      {/* ── Strategy Profile Exit Defaults ── */}
      <SPanel title="Strategy Profile Defaults" subtitle="Customise trailing stop, first risk-off, time exit, and trailing TP per entry strategy. Applied to every new auto-buy for that profile.">
        {strategyProfiles ? (
          <form style={{ display: "flex", flexDirection: "column", gap: 20 }} onSubmit={saveStrategyProfiles}>
            {(["launch_snipe", "migration_continuation", "wallet_follow", "narrative_breakout"] as const).map((profileName) => {
              const pData = strategyProfiles.profiles[profileName];
              if (!pData) return null;
              const eff = pData.effective as Record<string, Record<string, number | boolean | string>>;
              const defs = pData.defaults as Record<string, Record<string, number | boolean | string>>;
              const labels: Record<string, string> = {
                launch_snipe: "🚀 Launch Snipe",
                migration_continuation: "🔄 Migration Continuation",
                wallet_follow: "👛 Wallet Follow",
                narrative_breakout: "📖 Narrative Breakout",
              };
              const descriptions: Record<string, string> = {
                launch_snipe: "Ultra-fresh pump.fun tokens (≤35 min, ≤$350K mcap)",
                migration_continuation: "Graduated Raydium momentum plays",
                wallet_follow: "Wallet-follow continuation trades",
                narrative_breakout: "Narrative-driven breakout tokens",
              };
              const ts  = (eff.trailing_stop  || {}) as Record<string, number | boolean>;
              const fro = (eff.first_risk_off || {}) as Record<string, number | boolean>;
              const te  = (eff.time_exit      || {}) as Record<string, number | boolean>;
              const ttp = (eff.trailing_tp    || {}) as Record<string, number | boolean>;
              const vro = (eff.velocity_rollover || {}) as Record<string, boolean>;
              const ent = (eff.entry || {}) as Record<string, number | boolean | string>;
              const dts  = (defs.trailing_stop  || {}) as Record<string, number | boolean>;
              const dfro = (defs.first_risk_off || {}) as Record<string, number | boolean>;
              const dte  = (defs.time_exit      || {}) as Record<string, number | boolean>;
              const dttp = (defs.trailing_tp    || {}) as Record<string, number | boolean>;
              const dent = (defs.entry         || {}) as Record<string, number | boolean | string>;
              function pf(section: string, key: string, v: string | boolean) {
                setProfileOverride(profileName, section, key, typeof v === "boolean" ? v : (v === "" ? 0 : Number(v)));
              }
              function pfStr(section: string, key: string, v: string) {
                setProfileOverride(profileName, section, key, v);
              }
              return (
                <div key={profileName} style={{ border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
                  <div style={{ padding: "12px 16px", background: "var(--bg2)", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)" }}>{labels[profileName]}</div>
                      <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2 }}>{descriptions[profileName]}</div>
                    </div>
                    <button type="button" onClick={() => resetProfileOverrides(profileName)}
                      style={{ fontSize: 10, padding: "4px 10px", borderRadius: 6, background: "transparent", color: "var(--t3)", border: "1px solid var(--border)", cursor: "pointer" }}
                      className="hover:text-white hover:border-[var(--border2)]">Reset to defaults</button>
                  </div>
                  <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>

                    {/* Entry Filters */}
                    <div>
                      <div style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 8 }}>Entry Filters</div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Soft age limit (min) <span style={{ fontSize: 9 }}>(default: {dent.soft_max_age_mins})</span></div>
                          <input type="number" value={String(ent.soft_max_age_mins ?? dent.soft_max_age_mins ?? 0)} min={1} max={1440} step={1}
                            onChange={(e) => pf("entry", "soft_max_age_mins", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Hard age limit (min) <span style={{ fontSize: 9 }}>(default: {dent.hard_max_age_mins})</span></div>
                          <input type="number" value={String(ent.hard_max_age_mins ?? dent.hard_max_age_mins ?? 0)} min={1} max={2880} step={1}
                            onChange={(e) => pf("entry", "hard_max_age_mins", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Min liquidity ($) <span style={{ fontSize: 9 }}>(default: {dent.min_liquidity_usd})</span></div>
                          <input type="number" value={String(ent.min_liquidity_usd ?? dent.min_liquidity_usd ?? 0)} min={0} max={500000} step={100}
                            onChange={(e) => pf("entry", "min_liquidity_usd", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Min txns (5m) <span style={{ fontSize: 9 }}>(default: {dent.min_txns_5m})</span></div>
                          <input type="number" value={String(ent.min_txns_5m ?? dent.min_txns_5m ?? 0)} min={0} max={200} step={1}
                            onChange={(e) => pf("entry", "min_txns_5m", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Min buy ratio (0–1) <span style={{ fontSize: 9 }}>(default: {dent.min_buy_ratio_5m})</span></div>
                          <input type="number" value={String(ent.min_buy_ratio_5m ?? dent.min_buy_ratio_5m ?? 0)} min={0} max={1} step={0.01}
                            onChange={(e) => pf("entry", "min_buy_ratio_5m", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Size bias <span style={{ fontSize: 9 }}>(default: {dent.size_bias})</span></div>
                          <input type="number" value={String(ent.size_bias ?? dent.size_bias ?? 1)} min={0.1} max={3} step={0.05}
                            onChange={(e) => pf("entry", "size_bias", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        {profileName === "launch_snipe" && (
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Max mcap ($) <span style={{ fontSize: 9 }}>(default: {dent.max_mcap_usd})</span></div>
                            <input type="number" value={String(ent.max_mcap_usd ?? dent.max_mcap_usd ?? 0)} min={0} max={10000000} step={10000}
                              onChange={(e) => pf("entry", "max_mcap_usd", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                        )}
                        {profileName === "wallet_follow" && (
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Min wallet signal <span style={{ fontSize: 9 }}>(default: {dent.min_wallet_signal})</span></div>
                            <input type="number" value={String(ent.min_wallet_signal ?? dent.min_wallet_signal ?? 5)} min={1} max={20} step={1}
                              onChange={(e) => pf("entry", "min_wallet_signal", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                        )}
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Exit preset <span style={{ fontSize: 9 }}>(default: {dent.exit_preset ?? "—"})</span></div>
                          <select value={String(ent.exit_preset ?? dent.exit_preset ?? "standard")}
                            onChange={(e) => pfStr("entry", "exit_preset", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none" }}>
                            <option value="scalp">scalp</option>
                            <option value="standard">standard</option>
                            <option value="diamond">diamond</option>
                            <option value="moon">moon</option>
                          </select>
                        </div>
                      </div>
                    </div>

                    {/* Trailing Stop */}
                    <div>
                      <div style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 8 }}>Trailing Stop</div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Trail % <span style={{ color: "var(--t3)", fontSize: 9 }}>(default: {dts.trail_pct})</span></div>
                          <input type="number" value={String(ts.trail_pct ?? 0)} min={5} max={60} step={1}
                            onChange={(e) => pf("trailing_stop", "trail_pct", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Post-partial trail % <span style={{ color: "var(--t3)", fontSize: 9 }}>(default: {dts.post_partial_trail_pct})</span></div>
                          <input type="number" value={String(ts.post_partial_trail_pct ?? 0)} min={3} max={50} step={1}
                            onChange={(e) => pf("trailing_stop", "post_partial_trail_pct", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                      </div>
                    </div>

                    {/* First Risk-Off */}
                    <div>
                      <div style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 8 }}>First Risk-Off</div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                            <input type="checkbox" checked={Boolean(fro.enabled ?? true)}
                              onChange={(e) => pf("first_risk_off", "enabled", e.target.checked)} />
                            <span style={{ fontSize: 10, color: "var(--t3)" }}>Enabled</span>
                          </label>
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Activate at x <span style={{ fontSize: 9 }}>(default: {dfro.activate_mult})</span></div>
                          <input type="number" value={String(fro.activate_mult ?? 0)} min={1.1} max={5} step={0.05}
                            onChange={(e) => pf("first_risk_off", "activate_mult", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                          <div style={{ fontSize: 10, color: "var(--t3)" }}>Sell % <span style={{ fontSize: 9 }}>(default: {dfro.sell_pct})</span></div>
                          <input type="number" value={String(fro.sell_pct ?? 0)} min={1} max={100} step={1}
                            onChange={(e) => pf("first_risk_off", "sell_pct", e.target.value)}
                            style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                        </div>
                      </div>
                    </div>

                    {/* Time Exit (not wallet_follow) */}
                    {profileName !== "wallet_follow" && (
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 8 }}>Time Exit</div>
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                              <input type="checkbox" checked={Boolean(te.enabled)}
                                onChange={(e) => pf("time_exit", "enabled", e.target.checked)} />
                              <span style={{ fontSize: 10, color: "var(--t3)" }}>Enabled <span style={{ fontSize: 9 }}>(default: {dte.enabled ? "on" : "off"})</span></span>
                            </label>
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Hours <span style={{ fontSize: 9 }}>(default: {dte.hours})</span></div>
                            <input type="number" value={String(te.hours ?? 0)} min={1} max={168} step={1}
                              onChange={(e) => pf("time_exit", "hours", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Target x <span style={{ fontSize: 9 }}>(default: {dte.target_mult})</span></div>
                            <input type="number" value={String(te.target_mult ?? 0)} min={1.0} max={10} step={0.05}
                              onChange={(e) => pf("time_exit", "target_mult", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                        </div>
                      </div>
                    )}

                    {/* Trailing TP (wallet_follow only) */}
                    {profileName === "wallet_follow" && (
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 8 }}>Trailing Take-Profit</div>
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                              <input type="checkbox" checked={Boolean(ttp.enabled ?? true)}
                                onChange={(e) => pf("trailing_tp", "enabled", e.target.checked)} />
                              <span style={{ fontSize: 10, color: "var(--t3)" }}>Enabled <span style={{ fontSize: 9 }}>(default: {dttp.enabled ? "on" : "off"})</span></span>
                            </label>
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Activate at x <span style={{ fontSize: 9 }}>(default: {dttp.activate_mult})</span></div>
                            <input type="number" value={String(ttp.activate_mult ?? 0)} min={1.5} max={10} step={0.05}
                              onChange={(e) => pf("trailing_tp", "activate_mult", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Trail % <span style={{ fontSize: 9 }}>(default: {dttp.trail_pct})</span></div>
                            <input type="number" value={String(ttp.trail_pct ?? 0)} min={5} max={50} step={1}
                              onChange={(e) => pf("trailing_tp", "trail_pct", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                            <div style={{ fontSize: 10, color: "var(--t3)" }}>Sell % <span style={{ fontSize: 9 }}>(default: {dttp.sell_pct})</span></div>
                            <input type="number" value={String(ttp.sell_pct ?? 0)} min={1} max={100} step={1}
                              onChange={(e) => pf("trailing_tp", "sell_pct", e.target.value)}
                              style={{ background: "var(--bg3)", border: "1px solid var(--border2)", borderRadius: 7, color: "var(--foreground)", fontSize: 12, padding: "7px 10px", outline: "none", fontFamily: "var(--font-mono, monospace)" }} />
                          </div>
                        </div>
                      </div>
                    )}

                    {/* Velocity Rollover toggle */}
                    <div>
                      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                        <input type="checkbox" checked={Boolean(vro.enabled ?? true)}
                          onChange={(e) => pf("velocity_rollover", "enabled", e.target.checked)} />
                        <div>
                          <div style={{ fontSize: 11, fontWeight: 500, color: "var(--foreground)" }}>Velocity Rollover</div>
                          <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 1 }}>Sell when heat score collapses while price is above entry (default: on)</div>
                        </div>
                      </label>
                    </div>

                  </div>
                </div>
              );
            })}
            <button type="submit" className="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-[var(--accent-foreground)] hover:opacity-90">
              Save Strategy Profiles
            </button>
          </form>
        ) : (
          <div style={{ fontSize: 12, color: "var(--t3)" }}>Loading strategy profiles…</div>
        )}
      </SPanel>
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
    <div style={{ background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 10, padding: 14 }}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, cursor: "pointer" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => onToggle(e.target.checked)} />
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)" }}>{title}</span>
      </label>
      <p style={{ fontSize: 10, color: "var(--t3)", lineHeight: 1.5, marginBottom: 10 }}>{description}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(80px, 1fr))", gap: 8 }}>
        {fields.map((f) => (
          <div key={`${title}-${f.key}`} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <div style={{ fontSize: 10, color: "var(--t3)" }}>{f.label}</div>
            <input value={String(f.value)}
              onChange={(e) => onFieldChange(f.key, Number(e.target.value || 0))}
              style={{ background: "var(--bg2)", border: "1px solid var(--border2)", borderRadius: 6, color: "var(--foreground)", fontSize: 12, padding: "6px 8px", outline: "none", fontFamily: "var(--font-mono, monospace)" }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
