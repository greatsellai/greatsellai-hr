import { useMemo } from "react";
import { Icon } from "../icons";
import type { AiUsageTrendBucket, AiUsageTrendGranularity } from "./admin-types";
import { numberFormat } from "./AdminComponents";

type TrendPoint = {
  bucketStartedAt: string;
  invocationCount: number;
  tokenUsageInvocationCount: number;
  inputTokens: number;
  cachedReadTokens: number;
  cachedWriteTokens: number;
  outputTokens: number;
  totalTokens: number;
};

type TrendSeries = {
  key: "inputTokens" | "outputTokens" | "cachedReadTokens" | "cachedWriteTokens";
  label: string;
};

const CHART_WIDTH = 960;
const CHART_HEIGHT = 272;
const CHART_MARGIN = { top: 18, right: 20, bottom: 38, left: 56 };
const DAY_MS = 24 * 60 * 60 * 1_000;

const trendSeries: TrendSeries[] = [
  { key: "inputTokens", label: "输入" },
  { key: "outputTokens", label: "输出及推理" },
  { key: "cachedReadTokens", label: "缓存命中" },
  { key: "cachedWriteTokens", label: "缓存写入" },
];

function safeNumber(value: number | null | undefined) {
  return Number.isFinite(value) ? Math.max(0, value ?? 0) : 0;
}

function formatTokenVolume(value: number) {
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;
  return numberFormat(value);
}

function formatBucketLabel(value: string, granularity: AiUsageTrendGranularity, includeYear = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", granularity === "hour"
    ? {
      year: includeYear ? "numeric" : undefined,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      hour12: false,
    }
    : {
      year: includeYear ? "numeric" : undefined,
      month: "2-digit",
      day: "2-digit",
    },
  ).format(date);
}

function buildTrendPoints(buckets: AiUsageTrendBucket[]) {
  const byBucket = new Map<string, TrendPoint>();
  for (const bucket of buckets) {
    const current = byBucket.get(bucket.bucket_started_at) ?? {
      bucketStartedAt: bucket.bucket_started_at,
      invocationCount: 0,
      tokenUsageInvocationCount: 0,
      inputTokens: 0,
      cachedReadTokens: 0,
      cachedWriteTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
    };
    current.invocationCount += safeNumber(bucket.invocation_count);
    current.tokenUsageInvocationCount += safeNumber(bucket.token_usage_invocation_count);
    current.inputTokens += safeNumber(bucket.input_tokens);
    current.cachedReadTokens += safeNumber(bucket.cached_read_input_tokens);
    current.cachedWriteTokens += safeNumber(bucket.cached_write_input_tokens);
    current.outputTokens += safeNumber(bucket.output_tokens) + safeNumber(bucket.reasoning_tokens);
    current.totalTokens += safeNumber(bucket.total_tokens);
    byBucket.set(bucket.bucket_started_at, current);
  }
  return [...byBucket.values()].sort((left, right) => (
    new Date(left.bucketStartedAt).getTime() - new Date(right.bucketStartedAt).getTime()
  ));
}

function localRangeBoundaries(start: string, end: string) {
  const startAt = new Date(`${start}T00:00:00`).getTime();
  const endAt = new Date(`${end}T23:59:59.999`).getTime();
  if (Number.isFinite(startAt) && Number.isFinite(endAt) && endAt >= startAt) {
    return { startAt, endAt };
  }
  const now = Date.now();
  return { startAt: now - DAY_MS, endAt: now };
}

function xForTimestamp(timestamp: number, startAt: number, endAt: number) {
  const plotWidth = CHART_WIDTH - CHART_MARGIN.left - CHART_MARGIN.right;
  if (endAt <= startAt) return CHART_MARGIN.left + plotWidth / 2;
  const ratio = Math.max(0, Math.min(1, (timestamp - startAt) / (endAt - startAt)));
  return CHART_MARGIN.left + ratio * plotWidth;
}

function pointX(point: TrendPoint, startAt: number, endAt: number) {
  return xForTimestamp(new Date(point.bucketStartedAt).getTime(), startAt, endAt);
}

function pathFor(
  points: TrendPoint[],
  key: TrendSeries["key"],
  maxValue: number,
  startAt: number,
  endAt: number,
  granularity: AiUsageTrendGranularity,
) {
  const plotHeight = CHART_HEIGHT - CHART_MARGIN.top - CHART_MARGIN.bottom;
  const expectedBucketMs = granularity === "hour" ? 60 * 60 * 1_000 : DAY_MS;
  return points.map((point, index) => {
    const x = pointX(point, startAt, endAt);
    const y = CHART_MARGIN.top + plotHeight - ((point[key] / maxValue) * plotHeight);
    const previous = points[index - 1];
    const hasGap = previous && (
      new Date(point.bucketStartedAt).getTime() - new Date(previous.bucketStartedAt).getTime()
      > expectedBucketMs * 1.5
    );
    return `${index && !hasGap ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

function pointY(value: number, maxValue: number) {
  const plotHeight = CHART_HEIGHT - CHART_MARGIN.top - CHART_MARGIN.bottom;
  return CHART_MARGIN.top + plotHeight - ((value / maxValue) * plotHeight);
}

function yAxisTicks(maxValue: number) {
  if (maxValue <= 4) return Array.from({ length: Math.ceil(maxValue) + 1 }, (_, index) => index);
  const rawStep = maxValue / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalized = rawStep / magnitude;
  const niceFraction = [1, 2, 2.5, 5, 10].find((candidate) => candidate >= normalized) ?? 10;
  const step = niceFraction * magnitude;
  const axisMax = Math.ceil(maxValue / step) * step;
  return Array.from({ length: Math.round(axisMax / step) + 1 }, (_, index) => index * step);
}

function timeAxisTicks(startAt: number, endAt: number) {
  const tickCount = 5;
  if (endAt <= startAt) return [startAt];
  return Array.from({ length: tickCount }, (_, index) => (
    startAt + ((endAt - startAt) * index) / (tickCount - 1)
  ));
}

function pointDescription(point: TrendPoint, granularity: AiUsageTrendGranularity) {
  return [
    formatBucketLabel(point.bucketStartedAt, granularity, true),
    `输入 ${numberFormat(point.inputTokens)} Token`,
    `输出及推理 ${numberFormat(point.outputTokens)} Token`,
    `缓存命中 ${numberFormat(point.cachedReadTokens)} Token`,
    `缓存写入 ${numberFormat(point.cachedWriteTokens)} Token`,
    `总计 ${numberFormat(point.totalTokens)} Token`,
  ].join("；");
}

function Metric({ label, value, description }: { label: string; value: string; description: string }) {
  return <div className="admin-token-trend-metric"><dt>{label}</dt><dd>{value}</dd><small>{description}</small></div>;
}

export function TokenUsageTrendChart({
  buckets,
  granularity,
  rangeEnd,
  rangeLabel,
  rangeStart,
  scopeLabel,
  coverageLabel,
  isCrossModelAggregate,
  timeZone,
}: {
  buckets: AiUsageTrendBucket[];
  granularity: AiUsageTrendGranularity;
  rangeEnd: string;
  rangeLabel: string;
  rangeStart: string;
  scopeLabel: string;
  coverageLabel: string;
  isCrossModelAggregate: boolean;
  timeZone: string;
}) {
  const points = useMemo(() => buildTrendPoints(buckets), [buckets]);
  const summary = useMemo(() => points.reduce((current, point) => ({
    invocationCount: current.invocationCount + point.invocationCount,
    tokenUsageInvocationCount: current.tokenUsageInvocationCount + point.tokenUsageInvocationCount,
    inputTokens: current.inputTokens + point.inputTokens,
    cachedReadTokens: current.cachedReadTokens + point.cachedReadTokens,
    cachedWriteTokens: current.cachedWriteTokens + point.cachedWriteTokens,
    outputTokens: current.outputTokens + point.outputTokens,
    totalTokens: current.totalTokens + point.totalTokens,
  }), {
    invocationCount: 0,
    tokenUsageInvocationCount: 0,
    inputTokens: 0,
    cachedReadTokens: 0,
    cachedWriteTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
  }), [points]);
  const { startAt, endAt } = useMemo(() => localRangeBoundaries(rangeStart, rangeEnd), [rangeEnd, rangeStart]);
  const maxValue = Math.max(
    1,
    ...points.flatMap((point) => [
      point.inputTokens,
      point.outputTokens,
      point.cachedReadTokens,
      point.cachedWriteTokens,
    ]),
  );
  const yTicks = yAxisTicks(maxValue);
  const yAxisMax = yTicks.at(-1) ?? 1;
  const xTicks = timeAxisTicks(startAt, endAt);
  const plotBottom = CHART_HEIGHT - CHART_MARGIN.bottom;
  const plotRight = CHART_WIDTH - CHART_MARGIN.right;
  const hasReportedUsage = summary.tokenUsageInvocationCount > 0;
  const description = hasReportedUsage
    ? `${scopeLabel}，${coverageLabel}，${rangeLabel}，共 ${numberFormat(summary.totalTokens)} Token。`
    : `${scopeLabel}，${coverageLabel}，${rangeLabel}，没有模型返回的 Token 用量。`;

  return (
    <section aria-labelledby="admin-token-trend-title" className="admin-token-trend-panel">
      <header className="admin-token-trend-header">
        <div>
          <div className="admin-token-trend-title"><Icon name="activity" size={18} /><h2 id="admin-token-trend-title">Token 使用趋势</h2></div>
          <p>{scopeLabel} · {coverageLabel} · {rangeLabel}</p>
          <small className="admin-token-trend-scope-note">{isCrossModelAggregate ? `按 ${timeZone} 分桶；当前为跨模型汇总，不用于模型间比较。` : `按 ${timeZone} 分桶；所有 Token 均归属于所选 Provider / 模型。`}</small>
        </div>
        <dl className="admin-token-trend-metrics">
          <Metric label="输入" value={`${formatTokenVolume(summary.inputTokens)} Token`} description={numberFormat(summary.inputTokens)} />
          <Metric label="输出及推理" value={`${formatTokenVolume(summary.outputTokens)} Token`} description={numberFormat(summary.outputTokens)} />
          <Metric label="缓存命中" value={`${formatTokenVolume(summary.cachedReadTokens)} Token`} description={numberFormat(summary.cachedReadTokens)} />
          <Metric label="缓存写入" value={`${formatTokenVolume(summary.cachedWriteTokens)} Token`} description={numberFormat(summary.cachedWriteTokens)} />
        </dl>
      </header>

      <div className="admin-token-trend-legend" aria-label="趋势图图例">
        {trendSeries.map((series) => <span key={series.key}><i aria-hidden="true" className={`is-${series.key}`} />{series.label}</span>)}
      </div>

      {hasReportedUsage ? (
        <div className="admin-token-chart-scroll">
          <svg
            aria-describedby="admin-token-trend-description"
            aria-label="Token 使用趋势图"
            className="admin-token-chart"
            preserveAspectRatio="none"
            role="img"
            viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
          >
            <desc id="admin-token-trend-description">{description}</desc>
            <g aria-hidden="true">
              {yTicks.map((tick) => {
                const y = pointY(tick, yAxisMax);
                return <g key={tick}>
                  <line x1={CHART_MARGIN.left} x2={plotRight} y1={y} y2={y} />
                  <text textAnchor="end" x={CHART_MARGIN.left - 10} y={y + 4}>{formatTokenVolume(tick)}</text>
                </g>;
              })}
              {xTicks.map((tick) => (
                <text key={tick} textAnchor="middle" x={xForTimestamp(tick, startAt, endAt)} y={plotBottom + 24}>{formatBucketLabel(new Date(tick).toISOString(), granularity)}</text>
              ))}
            </g>
            {trendSeries.map((series) => <path className={`admin-token-chart-series is-${series.key}`} d={pathFor(points, series.key, yAxisMax, startAt, endAt, granularity)} key={series.key} />)}
            {points.map((point) => <g aria-hidden="true" key={point.bucketStartedAt}>
              <title>{pointDescription(point, granularity)}</title>
              {trendSeries.map((series) => <circle className={`admin-token-chart-point is-${series.key}`} cx={pointX(point, startAt, endAt)} cy={pointY(point[series.key], yAxisMax)} key={series.key} r="2.75" />)}
            </g>)}
          </svg>
        </div>
      ) : <div className="admin-token-trend-empty"><Icon name="activity" size={18} /><span>{summary.invocationCount ? `当前范围内有 ${numberFormat(summary.invocationCount)} 次调用，但模型未返回 Token usage。` : "当前范围内没有模型调用。调整日期或模型后再查看趋势。"}</span></div>}

      {points.length > 0 && <details className="admin-token-trend-details">
        <summary>查看趋势明细（{points.length} 个{granularity === "hour" ? "小时" : "日期"}桶）</summary>
        <div className="admin-data-table-scroll">
          <table className="admin-data-table admin-token-trend-table">
            <thead><tr><th>{granularity === "hour" ? "开始时间" : "日期"}</th><th>输入</th><th>输出及推理</th><th>缓存命中</th><th>缓存写入</th><th>总 Token</th><th>已返回用量 / 调用</th></tr></thead>
            <tbody>{points.map((point) => <tr key={point.bucketStartedAt}>
              <td>{formatBucketLabel(point.bucketStartedAt, granularity, true)}</td>
              <td>{numberFormat(point.inputTokens)}</td>
              <td>{numberFormat(point.outputTokens)}</td>
              <td>{numberFormat(point.cachedReadTokens)}</td>
              <td>{numberFormat(point.cachedWriteTokens)}</td>
              <td><strong>{numberFormat(point.totalTokens)}</strong></td>
              <td>{numberFormat(point.tokenUsageInvocationCount)} / {numberFormat(point.invocationCount)}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </details>}
    </section>
  );
}
