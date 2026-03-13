"use client";

import { FormEvent, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type AutoBuyConfig = {
  enabled?: boolean;
  sol_amount?: number;
  min_score?: number;
  daily_limit_sol?: number;
  max_positions?: number;
  buy_tier?: string;
};

type SettingsResponse = {
  uid: number;
  settings: Record<string, number | string | boolean>;
};

export function SettingsDashboard() {
  const searchParams = useSearchParams();
  const uid = Number(searchParams.get("uid") || 0);
  const [autobuy, setAutobuy] = useState<AutoBuyConfig>({});
  const [settings, setSettings] = useState<Record<string, number | string | boolean>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      if (!uid) {
        setAutobuy({});
        setSettings({});
        return;
      }
      try {
        const [autobuyRes, settingsRes] = await Promise.all([
          apiFetch<AutoBuyConfig>(`/autobuy/${uid}`),
          apiFetch<SettingsResponse>(`/settings/${uid}`),
        ]);
        setAutobuy(autobuyRes);
        setSettings(settingsRes.settings || {});
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load settings");
      }
    }
    load();
  }, [uid]);

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
          <div className="grid gap-4 md:grid-cols-2">
            <input
              value={String(autobuy.sol_amount ?? 0)}
              onChange={(event) => setAutobuy((current) => ({ ...current, sol_amount: Number(event.target.value || 0) }))}
              className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
              placeholder="SOL amount"
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
          </div>
          <select
            value={autobuy.buy_tier || "hot"}
            onChange={(event) => setAutobuy((current) => ({ ...current, buy_tier: event.target.value }))}
            className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
          >
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
    </div>
  );
}
