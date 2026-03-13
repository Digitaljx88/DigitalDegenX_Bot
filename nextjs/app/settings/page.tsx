import { Suspense } from "react";
import { SettingsDashboard } from "@/components/settings-dashboard";

export default function SettingsPage() {
  return (
    <Suspense fallback={null}>
      <SettingsDashboard />
    </Suspense>
  );
}
