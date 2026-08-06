import { lazy, Suspense } from "react";
import type { ProgressProps } from "@douyinfe/semi-ui-19/lib/es/progress";

const SemiProgress = lazy(() => import("@douyinfe/semi-ui-19/lib/es/progress"));

/**
 * Keeps the authenticated workspace on Semi's native progress control without
 * adding its code to the public landing page's initial bundle.
 */
export function BackofficeProgress(props: ProgressProps) {
  return (
    <Suspense fallback={null}>
      <SemiProgress {...props} />
    </Suspense>
  );
}
