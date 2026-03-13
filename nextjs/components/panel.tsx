import { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[28px] border border-white/10 bg-[var(--panel)] p-6 shadow-[0_24px_80px_rgba(0,0,0,0.22)]">
      <div className="mb-5 flex flex-col gap-1">
        <h2 className="text-lg font-semibold text-white">{title}</h2>
        {subtitle ? <p className="text-sm text-[var(--muted-foreground)]">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}
