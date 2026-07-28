import { useState } from "react";
import { Icon } from "../../icons";

export function LegacyWorkspaceAdoptionPanel({
  formatError,
  onAdopt,
}: {
  formatError: (error: unknown) => string;
  onAdopt: (legacyAdminPassword: string) => Promise<void>;
}) {
  const [legacyAdminPassword, setLegacyAdminPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setError(null);
    if (!legacyAdminPassword.trim()) {
      setError("请输入旧工作区管理口令。");
      return;
    }
    if (!window.confirm(
      "接管后，当前新建且为空的工作区会停用，历史简历、岗位、邮箱配置和 AI 任务将归入当前账号。该操作不能撤销，是否继续？",
    )) {
      return;
    }

    setSubmitting(true);
    try {
      await onAdopt(legacyAdminPassword);
    } catch (submissionError) {
      setError(formatError(submissionError));
      setSubmitting(false);
    }
  };

  return (
    <section className="panel legacy-workspace-adoption-panel" aria-labelledby="legacy-workspace-adoption-title">
      <div className="panel-heading">
        <div>
          <h2 id="legacy-workspace-adoption-title">接管旧工作区</h2>
          <p>
            将历史简历、岗位、评分、收件邮箱和招聘助手上下文交给当前账号管理。
          </p>
        </div>
        <span className="tiny-badge">一次性操作</span>
      </div>

      <form
        className="legacy-workspace-adoption-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="field-stack">
          <label className="field-label" htmlFor="legacy-workspace-management-password">
            旧工作区管理口令
          </label>
          <input
            aria-describedby={error ? "legacy-workspace-adoption-error" : "legacy-workspace-adoption-help"}
            aria-invalid={Boolean(error)}
            autoComplete="current-password"
            className="field"
            disabled={submitting}
            id="legacy-workspace-management-password"
            onChange={(event) => setLegacyAdminPassword(event.target.value)}
            required
            type="password"
            value={legacyAdminPassword}
          />
          <p className="field-help" id="legacy-workspace-adoption-help">
            当前新建工作区必须为空。接管完成后会自动进入历史工作区，旧登录入口将不能再使用。
          </p>
        </div>
        {error && <p className="library-error" id="legacy-workspace-adoption-error" role="alert">{error}</p>}
        <div className="legacy-workspace-adoption-actions">
          <button className="button button-primary" disabled={submitting || !legacyAdminPassword.trim()} type="submit">
            {submitting ? <><i className="spinner" />正在接管工作区</> : <><Icon name="arrow-right" size={16} />接管并进入旧工作区</>}
          </button>
        </div>
      </form>
    </section>
  );
}
