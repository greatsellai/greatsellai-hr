export const ICP_FILING_NUMBER = "粤ICP备2026106428号";
export const ICP_FILING_URL = "https://beian.miit.gov.cn/";

export function IcpFilingLink({ className }: { className?: string }) {
  const linkClassName = ["icp-filing-link", className].filter(Boolean).join(" ");

  return (
    <a
      className={linkClassName}
      href={ICP_FILING_URL}
      rel="noreferrer"
      target="_blank"
    >
      {ICP_FILING_NUMBER}
    </a>
  );
}
