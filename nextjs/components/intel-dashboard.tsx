"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type IntelDashboardProps = {
  title: string;
  subtitle: string;
  endpoint: string;
  collectionKey: string;
  secondaryCollectionKey?: string;
  secondaryTitle?: string;
};

const intelLinks = [
  { href: "/intel/wallets", label: "Wallets" },
  { href: "/intel/narratives", label: "Narratives" },
  { href: "/intel/discovery", label: "Discovery" },
  { href: "/intel/cluster", label: "Clusters" },
  { href: "/intel/bundle", label: "Bundles" },
  { href: "/intel/playbook", label: "Playbook" },
];

function formatIntelValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "n/a";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.map((item) => (typeof item === "object" ? JSON.stringify(item) : String(item))).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function IntelCard({ item, compact = false }: { item: Record<string, unknown>; compact?: boolean }) {
  const entries = Object.entries(item);
  const mint = typeof item.mint === "string" ? item.mint : null;
  const address = typeof item.address === "string" ? item.address : null;
  const title =
    (typeof item.name === "string" && item.name) ||
    (typeof item.symbol === "string" && item.symbol) ||
    address || mint || "Record";

  return (
    <div style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 10, padding: 14 }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10, marginBottom: 10 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)" }}>{title}</div>
          {mint && <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2, fontFamily: "var(--font-mono, monospace)" }}>{mint}</div>}
          {!mint && address && <div style={{ fontSize: 10, color: "var(--t3)", marginTop: 2, fontFamily: "var(--font-mono, monospace)" }}>{address}</div>}
        </div>
        {mint && (
          <Link href={`/token/${mint}`}
            style={{ fontSize: 11, padding: "5px 10px", borderRadius: 7, background: "var(--bg3)", border: "1px solid var(--border)", color: "var(--t2)", textDecoration: "none", flexShrink: 0 }}
            className="hover:border-[var(--border2)] hover:text-white transition-colors"
          >
            View
          </Link>
        )}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
        {entries
          .filter(([key]) => !["mint", "address", "name", "symbol"].includes(key))
          .slice(0, compact ? 6 : 10)
          .map(([key, value]) => (
            <div key={key} style={{ background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 10px" }}>
              <div style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--t3)", marginBottom: 2 }}>{key.replace(/_/g, " ")}</div>
              <div style={{ fontSize: 11, color: "var(--foreground)", wordBreak: "break-all" }}>{formatIntelValue(value)}</div>
            </div>
          ))}
      </div>
    </div>
  );
}

export function IntelDashboard({
  title,
  subtitle,
  endpoint,
  collectionKey,
  secondaryCollectionKey,
  secondaryTitle = "Recent events",
}: IntelDashboardProps) {
  const pathname = usePathname();
  const [items, setItems] = useState<Record<string, unknown>[]>([]);
  const [secondaryItems, setSecondaryItems] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch<Record<string, unknown>>(endpoint);
        const rows = Array.isArray(response[collectionKey]) ? (response[collectionKey] as Record<string, unknown>[]) : [];
        const extraRows = secondaryCollectionKey && Array.isArray(response[secondaryCollectionKey]) ? (response[secondaryCollectionKey] as Record<string, unknown>[]) : [];
        setItems(rows);
        setSecondaryItems(extraRows);
        setError("");
      } catch (err) { setError(err instanceof Error ? err.message : "Failed to load intel"); }
    }
    void load();
  }, [collectionKey, endpoint, secondaryCollectionKey]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Sub-nav */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {intelLinks.map((item) => {
          const active = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              style={{
                fontSize: 12, fontWeight: 500, padding: "6px 14px", borderRadius: 8, textDecoration: "none",
                background: active ? "rgba(249,115,22,0.12)" : "var(--bg2)",
                color: active ? "var(--accent)" : "var(--t2)",
                border: `1px solid ${active ? "rgba(249,115,22,0.25)" : "var(--border)"}`,
              }}
              className="hover:border-[var(--border2)] transition-colors"
            >
              {item.label}
            </Link>
          );
        })}
      </div>

      {/* Panel */}
      <div style={{ background: "var(--bg1)", border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", background: "var(--bg2)", borderBottom: "1px solid var(--border)" }}>
          <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>{title}</h2>
          <p style={{ fontSize: 12, color: "var(--t3)", marginTop: 3 }}>{subtitle}</p>
        </div>

        <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
          {/* Stats */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))", gap: 10 }}>
            {[
              { label: "Rows", value: String(items.length) },
              { label: "Secondary", value: String(secondaryItems.length) },
              { label: "Endpoint", value: endpoint.replace("/intel/", "") },
            ].map((s) => (
              <div key={s.label} style={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px" }}>
                <div style={{ fontSize: 9, color: "var(--t3)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{s.label}</div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)", fontFamily: "var(--font-mono, monospace)", wordBreak: "break-all" }}>{s.value}</div>
              </div>
            ))}
          </div>

          {error && (
            <div style={{ padding: "10px 14px", background: "rgba(244,63,94,0.1)", border: "1px solid rgba(244,63,94,0.25)", borderRadius: 8, fontSize: 12, color: "var(--red)" }}>
              {error}
            </div>
          )}

          {items.length === 0 && !error && (
            <div style={{ padding: 16, textAlign: "center", fontSize: 13, color: "var(--t3)" }}>No data yet.</div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {items.map((item, index) => <IntelCard key={index} item={item} />)}
          </div>

          {secondaryItems.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--foreground)", paddingTop: 12, borderTop: "1px solid var(--border)" }}>{secondaryTitle}</div>
              {secondaryItems.map((item, index) => <IntelCard key={`s-${index}`} item={item} compact />)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
