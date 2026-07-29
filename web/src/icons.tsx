import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "activity"
  | "arrow-left"
  | "arrow-right"
  | "briefcase"
  | "check"
  | "chevron-down"
  | "chevron-right"
  | "close"
  | "document"
  | "download"
  | "filter"
  | "folder"
  | "gear"
  | "history"
  | "inbox"
  | "layers"
  | "match"
  | "more"
  | "plus"
  | "refresh"
  | "search"
  | "spark"
  | "star"
  | "upload"
  | "user";

type IconProps = SVGProps<SVGSVGElement> & {
  name: IconName;
  size?: number;
};

const paths: Record<IconName, ReactNode> = {
  activity: <><path d="M3 12h3l2.2-6 4 12 2.3-6H21" /></>,
  "arrow-left": <><path d="m15 18-6-6 6-6" /><path d="M9 12h12" /></>,
  "arrow-right": <><path d="m9 18 6-6-6-6" /><path d="M3 12h12" /></>,
  briefcase: <><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v2h4v-2" /></>,
  check: <><path d="m5 12 4 4L19 6" /></>,
  "chevron-down": <><path d="m6 9 6 6 6-6" /></>,
  "chevron-right": <><path d="m9 18 6-6-6-6" /></>,
  close: <><path d="m6 6 12 12M18 6 6 18" /></>,
  document: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M8 13h8M8 17h6" /></>,
  download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>,
  filter: <><path d="M4 5h16M7 12h10M10 19h4" /></>,
  folder: <><path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></>,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.15 2.15-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.55v.09h-3.04v-.09a1.7 1.7 0 0 0-1.03-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06-2.15-2.15.06-.06A1.7 1.7 0 0 0 6.9 15a1.7 1.7 0 0 0-1.55-1.03h-.09v-3.04h.09A1.7 1.7 0 0 0 6.9 9.9a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.15-2.15.06.06a1.7 1.7 0 0 0 1.88.34 1.7 1.7 0 0 0 1.03-1.55v-.09h3.04v.09a1.7 1.7 0 0 0 1.03 1.55 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.15 2.15-.06.06a1.7 1.7 0 0 0-.34 1.88 1.7 1.7 0 0 0 1.55 1.03h.09v3.04h-.09A1.7 1.7 0 0 0 19.4 15Z" /></>,
  history: <><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5M12 7v5l3 2" /></>,
  inbox: <><path d="M4 4h16v12a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" /><path d="M4 14h4l2 3h4l2-3h4" /></>,
  layers: <><path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
  match: <><circle cx="10" cy="10" r="6" /><path d="m15 15 5 5M8 10l1.5 1.5L12.5 8.5" /></>,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" /></>,
  plus: <><path d="M12 5v14M5 12h14" /></>,
  refresh: <><path d="M20 11a8 8 0 1 0 2 5.4" /><path d="M20 4v7h-7" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></>,
  spark: <><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6zM19 16l.7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7z" /></>,
  star: <><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z" /></>,
  upload: <><path d="M12 16V4M7 9l5-5 5 5M5 20h14" /></>,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>
};

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.75"
      viewBox="0 0 24 24"
      width={size}
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
