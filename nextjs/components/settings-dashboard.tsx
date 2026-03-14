"use client";

import { FormEvent, useEffect, useState } from "react";
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
  daily_limit_sol?: number;
  max_positions?: number;
  max_narrative_exposure?: number;
  max_archetype_exposure?: number;
  buy_tier?: string;
};

type SettingsResponse = {
  uid: number;
  settings: Record<string, number | string | boolean>;
};

type ModeResponse = {
  uid: number;
  mode: "paper" | "live";
};

type PresetRow = {
  mult: number;
  sell_pct: number;
};

type TradeControls = {
  uid: number;
  presets_enabled: boolean;
  presets: PresetRow[];
  global_stop_loss: {
    enabled?: boolean;
    pct?: number;
    sell_pct?: number;
  };
  global_trailing_stop: {
    enabled?: boolean;
    trail_pct?: number;
    sell_pct?: number;
  };
  global_trailing_tp: {
    enabled?: boolean;
    activate_mult?: number;
    trail_pct?: number;
    sell_pct?: number;
  };
  global_breakeven_stop: {
    enabled?: boolean;
    activate_mult?: number;
  };
  global_time_exit: {
    enabled?: boolean;
    hours?: number;
    target_mult?: number;
    sell_pct?: number;
  };
};

export function SettingsDashboard() {
  const { uid } = useActiveUid();
  const [autobuy, setAutobuy] = useState<AutoBuyConfig>({});
  const [settings, setSettings] = useState<Record<string, number | string | boolean>>({});
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [tradeControls, setTradeControls] = useState<TradeControls | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) {
        setAutobuy({});
        setSettings({});
        setTradeControls(null);
        return;
      }
      try {
        const [autobuyRes, settingsRes, modeRes, tradeControlsRes] = await Promise.all([
          apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
          apiFetch<SettingsResponse>(`/settings/${uid}`),
          apiFetch<ModeResponse>(`/mode`, { query: { uid } }),
          apiFetch<TradeControls>(`/trade-controls/${uid}`),
        ]);
        setAutobuy(autobuyRes);
        setSettings(settingsRes.settings || {});
        setMode(modeRes.mode || "paper");
        setTradeControls(tradeControlsRes);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load settings");
      }
    }
    load();
  }, [uid]);

  async function saveMode(nextMode: "paper" | "live") {
    try {
      const response = await apiFetch<ModeResponse>("/mode", {
        method: "POST",
        body: JSON.stringify({ uid, mode: nextMode }),
      });
      setMode(response.mode || nextMode);
      setMessage(`Trading mode set to ${response.mode || nextMode}.`);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update mode");
    }
  }

  async function saveAutobuy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await apiFetch(`/autobuy/${uid}`, {
        method: "POST",
        body: JSON.stringify(autobuy),
      });
      setMessage("Auto-buy settings saved.");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save auto-buy settings");
    }
  }

  async function saveThresholds(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = {
      alert_ultra_hot_threshold: Number(settings.alert_ultra_hot_threshold || 0),
      alert_hot_threshold: Number(settings.alert_hot_threshold || 0),
      alert_warm_threshold: Number(settings.alert_warm_threshold || 0),
      alert_scouted_threshold: Number(settings.alert_scouted_threshold || 0),
    };
    try {
      await apiFetch(`/settings/${uid}`, {
        method: "POST",
        body: JSON.stringify({ settings: payload }),
      });
      setMessage("Heat-score thresholds saved.");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save thresholds");
    }
  }

  async function saveTradeControls(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tradeControls) {
      return;
    }
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save trade controls");
    }
  }

  if (!uid) {
    return (
      <Panel title="Settings" subtitle="Set your Telegram UID to edit auto-buy controls and heat-score thresholds.">
        <div className="text-sm text-[var(--muted-foreground)]">
          Add your Telegram UID in the top bar to edit auto-buy controls and scanner alert thresholds from the browser.
        </div>
      </Panel>
    );
  }

  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <Panel title="Mode" subtitle="This controls what Telegram /portfolio shows and which trading path the bot treats as active.">
        <div className="space-y-4">
          <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-[var(--muted-foreground)]">
            Current mode: <span className="font-medium text-white">{mode === "paper" ? "Paper" : "Live"}</span>
          </div>
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => saveMode("paper")}
              className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]"
            >
              Use Paper
            </button>
            <button
              type="button"
              onClick={() => saveMode("live")}
              className="rounded-full border border-white/10 px-4 py-2 text-sm text-[var(--muted-foreground)]"
            >
              Use Live
            </button>
          </div>
        </div>
      </Panel>

      <Panel title="Auto-Buy" subtitle="Tune the key automation controls from the browser.">
        <form className="space-y-4" onSubmit={saveAutobuy}>
          <label className="flex items-center gap-3 text-sm text-white">
            <input
              type="checkbox"
              checked={Boolean(autobuy.enabled)}
              onChange={(event) => setAutobuy((current) => ({ ...current, enabled: event.target.checked }))}
            />
            Enabled
          </label>
          <label className="flex items-center gap-3 text-sm text-white">
            <input
              type="checkbox"
              checked={Boolean(autobuy.confidence_scale_enabled ?? true)}
              onChange={(event) =>
                setAutobuy((current) => ({ ...current, confidence_scale_enabled: event.target.checked }))
              }
            />
            Confidence-based sizing
          </label>
          <div className="grid gap-4 md:grid-cols-2">
            <input
              value={String(autobuy.sol_amount ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, sol_amount: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Base SOL amount"
            />
            <input
              value={String(autobuy.max_sol_amount ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, max_sol_amount: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Max SOL amount"
            />
            <input
              value={String(autobuy.min_confidence ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, min_confidence: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Min confidence (0-1)"
            />
            <input
              value={String(autobuy.min_score ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, min_score: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Min score"
            />
            <input
              value={String(autobuy.daily_limit_sol ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, daily_limit_sol: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Daily SOL limit"
            />
            <input
              value={String(autobuy.max_positions ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, max_positions: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Max positions"
            />
            <input
              value={String(autobuy.max_narrative_exposure ?? 0)}
              onChange={(event) =>
                setAutobuy((current) => ({ ...current, max_narrative_exposure: Number(event.target.value || 0) }))
              }
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Narrative cap (0 = off)"
            />
            <input
              value={String(autobuy.max_archetype_exposure ?? 0)}
              onChange={(event) =>
                setAutobuy((current) => ({ ...current, max_archetype_exposure: Number(event.target.value || 0) }))
              }
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="Archetype cap (0 = off)"
            />
          </div>
          <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-xs text-[var(--muted-foreground)]">
            Base size is your normal auto-buy size. Stronger setups can scale up to Max SOL when confidence sizing is on.
            Min confidence blocks weak trades before they hit daily limits. Exposure caps stop stacking too many open
            positions in the same narrative or archetype.
          </div>
          <select
            value={autobuy.buy_tier || "hot"}
            onChange={(event) => setAutobuy((current) => ({ ...current, buy_tier: event.target.value }))}
            className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
          >
            <option value="scouted">Scouted</option>
            <option value="ultra_hot">Ultra Hot</option>
            <option value="hot">Hot</option>
            <option value="warm">Warm</option>
          </select>
          <button type="submit" className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]">
            Save Auto-Buy
          </button>
        </form>
      </Panel>

      <Panel title="Heat Thresholds" subtitle="Adjust the four alert tiers used across scanner and scout alerts.">
        <form className="space-y-4" onSubmit={saveThresholds}>
          <div className="grid gap-4 md:grid-cols-2">
            {[
              ["alert_ultra_hot_threshold", "Ultra Hot"],
              ["alert_hot_threshold", "Hot"],
              ["alert_warm_threshold", "Warm"],
              ["alert_scouted_threshold", "Scouted"],
            ].map(([key, label]) => (
              <div key={key} className="space-y-2">
                <div className="text-sm text-[var(--muted-foreground)]">{label}</div>
                <input
                  value={String(settings[key] ?? 0)}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      [key]: Number(event.target.value || 0),
                    }))
                  }
                  className="w-full rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
                />
              </div>
            ))}
          </div>
          <button type="submit" className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]">
            Save Thresholds
          </button>
        </form>
        {message ? <div className="mt-4 rounded-2xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{message}</div> : null}
        {error ? <div className="mt-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
      </Panel>

      <Panel title="Trade Controls" subtitle="Manage default multiplier presets and global exit protections from the dashboard.">
        {tradeControls ? (
          <form className="space-y-5 xl:col-span-2" onSubmit={saveTradeControls}>
            <label className="flex items-center gap-3 text-sm text-white">
              <input
                type="checkbox"
                checked={Boolean(tradeControls.presets_enabled)}
                onChange={(event) =>
                  setTradeControls((current) =>
                    current ? { ...current, presets_enabled: event.target.checked } : current,
                  )
                }
              />
              Apply presets to new buys
            </label>

            <div className="space-y-3">
              <div className="text-sm font-medium text-white">Default multiplier presets</div>
              {(tradeControls.presets || []).map((preset, index) => (
                <div key={`preset-${index}`} className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
                  <input
                    value={String(preset.mult ?? 0)}
                    onChange={(event) =>
                      setTradeControls((current) =>
                        current
                          ? {
                              ...current,
                              presets: current.presets.map((row, rowIndex) =>
                                rowIndex === index ? { ...row, mult: Number(event.target.value || 0) } : row,
                              ),
                            }
                          : current,
                      )
                    }
                    className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
                    placeholder="Multiplier"
                  />
                  <input
                    value={String(preset.sell_pct ?? 0)}
                    onChange={(event) =>
                      setTradeControls((current) =>
                        current
                          ? {
                              ...current,
                              presets: current.presets.map((row, rowIndex) =>
                                rowIndex === index ? { ...row, sell_pct: Number(event.target.value || 0) } : row,
                              ),
                            }
                          : current,
                      )
                    }
                    className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
                    placeholder="Sell %"
                  />
                  <button
                    type="button"
                    onClick={() =>
                      setTradeControls((current) =>
                        current
                          ? { ...current, presets: current.presets.filter((_, rowIndex) => rowIndex !== index) }
                          : current,
                      )
                    }
                    className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)]"
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={() =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, presets: [...current.presets, { mult: 2, sell_pct: 50 }] }
                      : current,
                  )
                }
                className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)]"
              >
                Add preset
              </button>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <GlobalExitBlock
                title="Global Stop-Loss"
                enabled={Boolean(tradeControls.global_stop_loss?.enabled)}
                fields={[
                  { key: "pct", label: "Drop %", value: Number(tradeControls.global_stop_loss?.pct || 0) },
                  { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_stop_loss?.sell_pct || 100) },
                ]}
                onToggle={(enabled) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_stop_loss: { ...current.global_stop_loss, enabled } }
                      : current,
                  )
                }
                onFieldChange={(key, value) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_stop_loss: { ...current.global_stop_loss, [key]: value } }
                      : current,
                  )
                }
              />
              <GlobalExitBlock
                title="Global Trailing Stop"
                enabled={Boolean(tradeControls.global_trailing_stop?.enabled)}
                fields={[
                  { key: "trail_pct", label: "Trail %", value: Number(tradeControls.global_trailing_stop?.trail_pct || 0) },
                  { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_trailing_stop?.sell_pct || 100) },
                ]}
                onToggle={(enabled) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_trailing_stop: { ...current.global_trailing_stop, enabled } }
                      : current,
                  )
                }
                onFieldChange={(key, value) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_trailing_stop: { ...current.global_trailing_stop, [key]: value } }
                      : current,
                  )
                }
              />
              <GlobalExitBlock
                title="Global Trailing TP"
                enabled={Boolean(tradeControls.global_trailing_tp?.enabled)}
                fields={[
                  { key: "activate_mult", label: "Activate at x", value: Number(tradeControls.global_trailing_tp?.activate_mult || 0) },
                  { key: "trail_pct", label: "Trail %", value: Number(tradeControls.global_trailing_tp?.trail_pct || 0) },
                  { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_trailing_tp?.sell_pct || 0) },
                ]}
                onToggle={(enabled) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_trailing_tp: { ...current.global_trailing_tp, enabled } }
                      : current,
                  )
                }
                onFieldChange={(key, value) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_trailing_tp: { ...current.global_trailing_tp, [key]: value } }
                      : current,
                  )
                }
              />
              <GlobalExitBlock
                title="Global Breakeven"
                enabled={Boolean(tradeControls.global_breakeven_stop?.enabled)}
                fields={[
                  { key: "activate_mult", label: "Activate at x", value: Number(tradeControls.global_breakeven_stop?.activate_mult || 0) },
                ]}
                onToggle={(enabled) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_breakeven_stop: { ...current.global_breakeven_stop, enabled } }
                      : current,
                  )
                }
                onFieldChange={(key, value) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_breakeven_stop: { ...current.global_breakeven_stop, [key]: value } }
                      : current,
                  )
                }
              />
              <GlobalExitBlock
                title="Global Time Exit"
                enabled={Boolean(tradeControls.global_time_exit?.enabled)}
                fields={[
                  { key: "hours", label: "Hours", value: Number(tradeControls.global_time_exit?.hours || 0) },
                  { key: "target_mult", label: "Target x", value: Number(tradeControls.global_time_exit?.target_mult || 0) },
                  { key: "sell_pct", label: "Sell %", value: Number(tradeControls.global_time_exit?.sell_pct || 100) },
                ]}
                onToggle={(enabled) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_time_exit: { ...current.global_time_exit, enabled } }
                      : current,
                  )
                }
                onFieldChange={(key, value) =>
                  setTradeControls((current) =>
                    current
                      ? { ...current, global_time_exit: { ...current.global_time_exit, [key]: value } }
                      : current,
                  )
                }
              />
            </div>

            <button type="submit" className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]">
              Save Trade Controls
            </button>
          </form>
        ) : (
          <div className="text-sm text-[var(--muted-foreground)]">Loading trade controls...</div>
        )}
      </Panel>
    </div>
  );
}

function GlobalExitBlock({
  title,
  enabled,
  fields,
  onToggle,
  onFieldChange,
}: {
  title: string;
  enabled: boolean;
  fields: Array<{ key: string; label: string; value: number }>;
  onToggle: (enabled: boolean) => void;
  onFieldChange: (key: string, value: number) => void;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
      <label className="mb-3 flex items-center gap-3 text-sm text-white">
        <input type="checkbox" checked={enabled} onChange={(event) => onToggle(event.target.checked)} />
        {title}
      </label>
      <div className="grid gap-3 md:grid-cols-2">
        {fields.map((field) => (
          <div key={`${title}-${field.key}`} className="space-y-2">
            <div className="text-xs text-[var(--muted-foreground)]">{field.label}</div>
            <input
              value={String(field.value)}
              onChange={(event) => onFieldChange(field.key, Number(event.target.value || 0))}
              className="w-full rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
            />
          </div>
        ))}
      </div>
    </div>
  );
}
