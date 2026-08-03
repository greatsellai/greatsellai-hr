import { useEffect, useState } from "react";
import { api } from "../../api";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { Icon } from "../../icons";
import type { CandidateSearchItem } from "../../types";
import type { CandidateDrawerTab } from "../candidate-drawer/candidate-drawer-types";
import {
  degreeLabels,
  formatDuration,
  institutionClassificationLabel,
  sortInstitutionClassifications,
} from "./filter-model";
import {
  candidatePendingReasons,
  candidateScoreConfidencePresentation,
} from "./candidate-workbench-model";

function candidateSubtitle(item: CandidateSearchItem): string {
  return [
    item.latest_experience_title,
    item.latest_experience_organization,
  ]
    .filter(Boolean)
    .join(" · ") || "经历待核验";
}

export function CandidateInspector({
  candidate,
  onFavoriteChanged,
  onOpenCandidate,
}: {
  candidate: CandidateSearchItem | null;
  onFavoriteChanged?: () => void;
  onOpenCandidate: (candidate: CandidateSearchItem, tab?: CandidateDrawerTab) => void;
}) {
  const [favoriteOverride, setFavoriteOverride] = useState<boolean | null>(null);
  const [favoriteUpdating, setFavoriteUpdating] = useState(false);
  const [favoriteError, setFavoriteError] = useState<string | null>(null);

  useEffect(() => {
    setFavoriteOverride(null);
    setFavoriteError(null);
    setFavoriteUpdating(false);
  }, [candidate?.candidate_id]);

  if (!candidate) {
    return (
      <aside aria-label="候选人档案" className="candidate-inspector is-empty">
        <div className="candidate-inspector-empty-icon" aria-hidden="true">
          <Icon name="user" size={20} />
        </div>
        <h2>选择一位候选人</h2>
        <p>在结果表中选择候选人，即可查看评分、事实与待确认项。</p>
      </aside>
    );
  }

  const isFavorited = favoriteOverride ?? candidate.is_favorited;
  const pending = candidatePendingReasons(candidate);
  const confidence = candidateScoreConfidencePresentation(candidate.score_confidence);
  const schoolTags = sortInstitutionClassifications(
    candidate.institution_classifications,
  );
  const totalTenureMonths = Math.max(
    candidate.employment_or_internship_months ?? 0,
    candidate.employment_months ?? 0,
  );

  const toggleFavorite = async () => {
    if (favoriteUpdating) return;
    setFavoriteUpdating(true);
    setFavoriteError(null);
    try {
      const nextState = isFavorited
        ? (await api.unfavoriteCandidate(candidate.candidate_id), false)
        : (await api.favoriteCandidate(candidate.candidate_id)).is_favorited;
      setFavoriteOverride(nextState);
      onFavoriteChanged?.();
    } catch {
      setFavoriteError("更新收藏失败，请稍后重试。");
    } finally {
      setFavoriteUpdating(false);
    }
  };

  return (
    <aside aria-label="候选人档案" className="candidate-inspector">
      <header className="candidate-inspector-header">
        <span>候选人档案</span>
        <h2>{candidate.display_name?.trim() || "未命名候选人"}</h2>
        <p>{candidateSubtitle(candidate)}</p>
      </header>

      <BackofficeButton
        className="candidate-inspector-full-profile"
        icon={<Icon name="document" size={16} />}
        onClick={() => onOpenCandidate(candidate)}
      >
        查看完整档案
      </BackofficeButton>

      <section className="candidate-inspector-score" aria-label="综合评分">
        <div className="candidate-inspector-score-heading">
          <span>综合评分</span>
          <span className={`candidate-inspector-confidence${confidence.tone !== "grounded" ? " is-review" : ""}`}>
            {confidence.label}
          </span>
        </div>
        {candidate.score_total !== null ? (
          <button
            aria-label={`查看 ${candidate.display_name?.trim() || "候选人"} 的评分详情`}
            className="candidate-inspector-score-value"
            onClick={() => onOpenCandidate(candidate, "score")}
            type="button"
          >
            <strong>{candidate.score_total.toFixed(1)}</strong>
            <span>/ 100</span>
          </button>
        ) : (
          <button
            className="candidate-inspector-unscored"
            onClick={() => onOpenCandidate(candidate, "score")}
            type="button"
          >
            尚未评分
          </button>
        )}
        {candidate.score_template_name && (
          <p>{candidate.score_template_name}</p>
        )}
      </section>

      <section className="candidate-inspector-section" aria-label="初筛命中">
        <h3>初筛命中</h3>
        <div className="candidate-inspector-tags">
          {candidate.highest_degree && (
            <span>{degreeLabels[candidate.highest_degree]}</span>
          )}
          {schoolTags.map((classification) => (
            <span key={classification}>
              {institutionClassificationLabel(classification)}
            </span>
          ))}
          {totalTenureMonths > 0 && <span>{formatDuration(totalTenureMonths)}</span>}
          {candidate.skill_highlights.slice(0, 2).map((skill) => (
            <span key={skill}>{skill}</span>
          ))}
          {!candidate.highest_degree && !schoolTags.length && !totalTenureMonths && !candidate.skill_highlights.length && (
            <span className="is-muted">待补充</span>
          )}
        </div>
      </section>

      <section className="candidate-inspector-section candidate-inspector-pending" aria-label="待确认项">
        <h3>待确认项</h3>
        {pending.length ? (
          <ul>
            {pending.slice(0, 3).map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        ) : (
          <p>当前筛选结果未发现需要额外核验的项。</p>
        )}
      </section>

      {favoriteError && <p className="candidate-inspector-error" role="alert">{favoriteError}</p>}

      <div className="candidate-inspector-actions">
        <BackofficeButton
          icon={<Icon name="briefcase" size={16} />}
          onClick={() => onOpenCandidate(candidate, "applications")}
        >
          加入岗位
        </BackofficeButton>
        <BackofficeButton
          ariaLabel={isFavorited ? "取消收藏候选人" : "收藏候选人"}
          icon={<Icon name="bookmark" size={16} />}
          loading={favoriteUpdating}
          onClick={() => void toggleFavorite()}
        >
          {isFavorited ? "已收藏" : "收藏候选人"}
        </BackofficeButton>
      </div>
    </aside>
  );
}
