import { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  badge,
  children,
}: {
  title: string;
  subtitle?: string;
  badge?: string;
  children: ReactNode;
}) {
  return (
    <section
      style={{
        borderRadius: 14,
        border: "1px solid var(--border)",
        background: "var(--bg1)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "14px 20px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg2)",
        }}
      >
        <div className="flex items-center gap-2">
          <h2 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)" }}>{title}</h2>
          {badge && (
            <span
              style={{
                borderRadius: 20,
                background: "rgba(249,115,22,0.12)",
                padding: "2px 8px",
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                color: "var(--accent)",
                border: "1px solid rgba(249,115,22,0.2)",
              }}
            >
              {badge}
            </span>
          )}
        </div>
        {subtitle ? (
          <p style={{ fontSize: 12, color: "var(--text3)", marginTop: 3 }}>{subtitle}</p>
        ) : null}
      </div>
      <div style={{ padding: "20px" }}>{children}</div>
    </section>
  );
}
