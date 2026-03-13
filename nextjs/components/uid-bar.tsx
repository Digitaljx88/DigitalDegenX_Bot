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
    if (!value) {
      return;
    }
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
          setMessage("Dashboard UID cleared");
        })
        .catch((err) => {
          setMessage(err instanceof Error ? err.message : "Failed to clear UID");
        });
    });
  }

  return (
    <div className="flex flex-col gap-3 rounded-2xl border border-white/10 bg-black/15 p-4 md:flex-row md:items-end md:justify-between">
      <div>
        <div className="text-xs uppercase tracking-[0.24em] text-[var(--muted-foreground)]">Active Telegram User</div>
        <div className="mt-1 text-sm text-white">
          {currentUid ? `UID ${currentUid}` : "Set your Telegram user ID to unlock portfolio, trades, and controls."}
        </div>
        {message ? <div className="mt-2 text-xs text-[var(--muted-foreground)]">{message}</div> : null}
        {error ? <div className="mt-2 text-xs text-red-200">{error}</div> : null}
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          value={draftUid || currentUid}
          onChange={(event) => setDraftUid(event.target.value.replace(/[^\d]/g, ""))}
          placeholder="Telegram UID"
          className="rounded-full border border-white/10 bg-black/25 px-4 py-2 text-sm text-white outline-none placeholder:text-[var(--muted-foreground)]"
        />
        <button
          type="button"
          onClick={applyUid}
          disabled={isPending}
          className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)] disabled:opacity-60"
        >
          {isPending ? "Saving..." : "Use UID"}
        </button>
        <button
          type="button"
          onClick={handleClearUid}
          className="rounded-full border border-white/10 px-4 py-2 text-sm text-[var(--muted-foreground)]"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
