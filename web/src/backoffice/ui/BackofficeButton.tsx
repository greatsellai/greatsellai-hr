import { lazy, Suspense, type MouseEventHandler, type ReactNode } from "react";
import type { ButtonProps as SemiButtonProps } from "@douyinfe/semi-ui-19/lib/es/button";

const SemiButton = lazy(() => import("@douyinfe/semi-ui-19/lib/es/button"));

type BackofficeButtonTone = "default" | "danger" | "primary";

interface BackofficeButtonProps {
  ariaLabel?: string;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  icon?: ReactNode;
  loading?: boolean;
  onClick?: MouseEventHandler<HTMLButtonElement>;
  tone?: BackofficeButtonTone;
}

function semiButtonProps(tone: BackofficeButtonTone): Pick<SemiButtonProps, "theme" | "type"> {
  if (tone === "primary") return { theme: "solid", type: "primary" };
  if (tone === "danger") return { theme: "outline", type: "danger" };
  return { theme: "outline", type: "tertiary" };
}

/**
 * A deliberately narrow adapter for the first migrated toolbar actions.
 * The Semi component itself is lazy so the public landing page never imports
 * button code merely because it shares the application entry point.
 */
export function BackofficeButton({
  ariaLabel,
  children,
  className,
  disabled = false,
  icon,
  loading = false,
  onClick,
  tone = "default",
}: BackofficeButtonProps) {
  const buttonClassName = ["backoffice-action-button", className].filter(Boolean).join(" ");
  const fallback = (
    <button
      aria-label={ariaLabel}
      className={`button${tone === "primary" ? " button-primary" : tone === "danger" ? " button-danger-ghost" : ""} ${className ?? ""}`.trim()}
      disabled={disabled || loading}
      onClick={onClick}
      type="button"
    >
      {loading ? <i className="spinner" /> : icon}
      {children}
    </button>
  );

  return (
    <Suspense fallback={fallback}>
      <SemiButton
        aria-label={ariaLabel}
        className={buttonClassName}
        disabled={disabled}
        htmlType="button"
        icon={icon}
        loading={loading}
        onClick={onClick}
        {...semiButtonProps(tone)}
      >
        {children}
      </SemiButton>
    </Suspense>
  );
}
