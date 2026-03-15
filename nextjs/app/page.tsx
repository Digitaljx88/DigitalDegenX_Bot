import { Suspense } from "react";
import { OverviewDashboard } from "@/components/overview-dashboard";

export default function Home() {
  return (
    <Suspense fallback={null}>
      <OverviewDashboard />
    </Suspense>
  );
}
