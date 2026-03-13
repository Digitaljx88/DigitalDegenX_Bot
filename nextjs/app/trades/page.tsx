import { Suspense } from "react";
import { TradesDashboard } from "@/components/trades-dashboard";

export default function TradesPage() {
  return (
    <Suspense fallback={null}>
      <TradesDashboard />
    </Suspense>
  );
}
