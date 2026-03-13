import { Suspense } from "react";
import { ScannerDashboard } from "@/components/scanner-dashboard";

export default function ScannerPage() {
  return (
    <Suspense fallback={null}>
      <ScannerDashboard />
    </Suspense>
  );
}
