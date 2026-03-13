import { IntelDashboard } from "@/components/intel-dashboard";

export default function BundleIntelPage() {
  return <IntelDashboard title="Bundle Intelligence" subtitle="Recent coordinated-buyer and bundle-risk events." endpoint="/intel/bundles" collectionKey="events" />;
}
