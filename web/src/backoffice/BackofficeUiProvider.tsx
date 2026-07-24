import { useEffect, useState, type ReactNode } from "react";
import "./backoffice-ui.css";

type SemiConfigProvider = typeof import("@douyinfe/semi-ui-19/lib/es/configProvider").default;

/**
 * Semi is deliberately loaded only after a user enters the authenticated
 * workspace. The public landing page shares the Vite application entry, so a
 * static import here would make its design-system payload part of the public
 * site's initial bundle even though the landing page does not use it. Individual
 * backoffice adapters lazy-load their own component stylesheet; importing the
 * library's full global stylesheet here would be both wasteful and unsafe for
 * a public landing page that intentionally has its own visual system.
 */
function useBackofficeConfigProvider() {
  const [ConfigProvider, setConfigProvider] = useState<SemiConfigProvider | null>(null);

  useEffect(() => {
    let active = true;

    void import("@douyinfe/semi-ui-19/lib/es/configProvider").then((configProviderModule) => {
      if (!active) return;
      setConfigProvider(() => configProviderModule.default);
    });

    return () => {
      active = false;
    };
  }, []);

  return ConfigProvider;
}

export function BackofficeUiProvider({ children }: { children: ReactNode }) {
  const ConfigProvider = useBackofficeConfigProvider();

  if (!ConfigProvider) {
    return (
      <main className="backoffice-ui-boot" aria-busy="true" aria-live="polite">
        <i className="spinner" /> 正在打开招聘工作台…
      </main>
    );
  }

  return (
    <ConfigProvider
      getPopupContainer={() =>
        document.querySelector<HTMLElement>(".backoffice-ui-root") ?? document.body
      }
    >
      <div className="backoffice-ui-root">{children}</div>
    </ConfigProvider>
  );
}
