import { Suspense } from "react";
import { PortfolioDashboard } from "@/components/portfolio-dashboard";

export default function PortfolioPage() {
  return (
    <Suspense fallback={null}>
      <PortfolioDashboard />
    </Suspense>
  );
}
