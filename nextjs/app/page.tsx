import { Panel } from "@/components/panel";

export default function Home() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-3">
        <Panel title="Backend" subtitle="FastAPI bot control layer">
          <div className="text-3xl font-semibold text-white">Online via local API</div>
        </Panel>
        <Panel title="Bot" subtitle="Connected service">
          <div className="text-3xl font-semibold text-white">DigitalDegenX</div>
        </Panel>
        <Panel title="Domain Ready" subtitle="Frontend can sit on your public domain while the bot API stays private.">
          <div className="text-3xl font-semibold text-white">digitaldegenx.online</div>
        </Panel>
      </div>

      <Panel title="Operating Model" subtitle="What this dashboard is optimized for right now.">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {[
            ["Scanner", "Live feed of the newest scored tokens with quick paper buys."],
            ["Trades", "Ledger, closed-trade stats, and filterable history by UID."],
            ["Portfolio", "Current balances with quick paper sells from the browser."],
            ["Settings", "Auto-buy and alert-threshold editing without Telegram menu friction."],
          ].map(([title, body]) => (
            <div key={title} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-xl font-semibold text-white">{title}</div>
              <div className="mt-3 text-sm text-[var(--muted-foreground)]">{body}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
