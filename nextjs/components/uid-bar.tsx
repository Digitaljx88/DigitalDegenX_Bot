"use client";

import { useState, useTransition } from "react";
import { useActiveUid } from "@/lib/active-uid";

export function UidBar() {
  const [isPending, startTransition] = useTransition();
  const { uid, error, setUid, clearUid } = useActiveUid();
  const currentUid = uid ? String(uid) : "";
  const [draftUid, setDraftUid] = useState("");
  const [message, setMessage] = useState("");

  function applyUid() {
    const value = draftUid.trim();
    if (!value) return;
    startTransition(() => {
      void setUid(value)
        .then((nextUid) => {
          setDraftUid(nextUid ? String(nextUid) : "");
          setMessage(nextUid ? `UID ${nextUid} active` : "UID saved");
        })
        .catch((err) => {
          setMessage(err instanceof Error ? err.message : "Failed to save UID");
        });
    });
  }

  function handleClearUid() {
    startTransition(() => {
      void clearUid()
        .then(() => {
          setDraftUid("");
          setMessage("UID cleared");
        })
        .catch((err) => {
          setMessage(err instanceof Error ? err.message : "Failed to clear UID");
        });
    });
  }

  return (
    <div
      className="flex items-center gap-2.5 flex-shrink-0 flex-wrap"
      style={{
        background: "var(--bg2)",
        border: "1px solid var(--border)",
        borderRadius: 10,
        padding: "10px 14px",
      }}
    >
      <span
        style={{
          fontSize: 10,
          fontWeight: 500,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          color: "var(--text3)",
          whiteSpace: "nowrap",
        }}
      >
        Active UID
      </span>

      {currentUid ? (
        <span
          style={{
            fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
            fontSize: 12,
            color: "var(--blue)",
            background: "rgba(96,165,250,0.07)",
            padding: "4px 10px",
            borderRadius: 6,
            border: "1px solid rgba(96,165,250,0.15)",
          }}
        >
          {currentUid}
        </span>
      ) : null}

      <input
        value={draftUid}
        onChange={(e) => setDraftUid(e.target.value.replace(/[^\d]/g, ""))}
        placeholder="Enter UID…"
        onKeyDown={(e) => e.key === "Enter" && applyUid()}
        style={{
          background: "var(--bg3)",
          border: "1px solid var(--border2)",
          borderRadius: 7,
          color: "var(--foreground)",
          fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
          fontSize: 12,
          padding: "6px 10px",
          outline: "none",
          width: 160,
        }}
        className="focus:border-[var(--accent)] placeholder:text-[var(--text3)]"
      />

      <button
        type="button"
        onClick={applyUid}
        disabled={isPending}
        style={{
          background: "var(--accent)",
          color: "#fff",
          border: "none",
          borderRadius: 7,
          fontFamily: "var(--font-sans, 'Space Grotesk', sans-serif)",
          fontSize: 12,
          fontWeight: 600,
          padding: "7px 14px",
          cursor: "pointer",
        }}
        className="hover:bg-[var(--accent2)] disabled:opacity-60 transition-colors"
      >
        {isPending ? "Saving…" : "Use UID"}
      </button>

      <button
        type="button"
        onClick={handleClearUid}
        style={{
          background: "transparent",
          color: "var(--text3)",
          border: "1px solid var(--border)",
          borderRadius: 7,
          fontFamily: "var(--font-sans, 'Space Grotesk', sans-serif)",
          fontSize: 12,
          padding: "7px 12px",
          cursor: "pointer",
        }}
        className="hover:text-white hover:border-[var(--border2)] transition-colors"
      >
        Clear
      </button>

      {(message || error) ? (
        <span style={{ fontSize: 10, color: error ? "var(--red)" : "var(--text3)" }}>
          {error || message}
        </span>
      ) : null}
    </div>
  );
}
