import { IntelDashboard } from "@/components/intel-dashboard";

export default function DiscoveryIntelPage() {
  return (
    <IntelDashboard
      title="Wallet Discovery"
      subtitle="Auto-discovered wallets that entered winning launches early."
      endpoint="/intel/discovery"
      collectionKey="wallets"
    />
  );
}
