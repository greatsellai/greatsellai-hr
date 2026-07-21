import { useEffect, useMemo, useRef, useState } from "react";
import type { ECharts, EChartsOption } from "echarts";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  AriaComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { Icon } from "../icons";
import type { AiUsageTrendBucket, AiUsageTrendGranularity } from "./admin-types";
import { numberFormat } from "./AdminComponents";

echarts.use([
  AriaComponent,
  GridComponent,
  LegendComponent,
  LineChart,
  TooltipComponent,
  CanvasRenderer,
]);

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

type TrendSeriesKey = "inputTokens" | "outputTokens" | "cachedReadTokens" | "cachedWriteTokens";

type TrendSeries = {
  key: TrendSeriesKey;
  label: string;
  color: string;
  lineType: "solid" | "dashed";
};

const DAY_MS = 24 * 60 * 60 * 1_000;

const trendSeries: TrendSeries[] = [
  { key: "inputTokens", label: "输入", color: "#2f6fed", lineType: "solid" },
  { key: "outputTokens", label: "输出及推理", color: "#7656c7", lineType: "solid" },
  { key: "cachedReadTokens", label: "缓存命中", color: "#16856b", lineType: "dashed" },
  { key: "cachedWriteTokens", label: "缓存写入", color: "#a06b19", lineType: "dashed" },
];

function safeNumber(value: number | null | undefined) {
  return Number.isFinite(value) ? Math.max(0, value ?? 0) : 0;
}

function formatTokenVolume(value: number) {
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;
  return numberFormat(value);
}

function formatBucketLabel(value: string | number, granularity: AiUsageTrendGranularity, includeYear = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
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

function emptyTrendPoint(bucketStartedAt: number): TrendPoint {
  return {
    bucketStartedAt: new Date(bucketStartedAt).toISOString(),
    invocationCount: 0,
    tokenUsageInvocationCount: 0,
    inputTokens: 0,
    cachedReadTokens: 0,
    cachedWriteTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
  };
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

function bucketStartAt(timestamp: number, granularity: AiUsageTrendGranularity) {
  const bucket = new Date(timestamp);
  if (granularity === "hour") {
    bucket.setMinutes(0, 0, 0);
  } else {
    bucket.setHours(0, 0, 0, 0);
  }
  return bucket.getTime();
}

function nextBucketStart(timestamp: number, granularity: AiUsageTrendGranularity) {
  const next = new Date(timestamp);
  if (granularity === "hour") {
    next.setHours(next.getHours() + 1, 0, 0, 0);
  } else {
    next.setDate(next.getDate() + 1);
    next.setHours(0, 0, 0, 0);
  }
  return next.getTime();
}

function completeTrendPoints(
  points: TrendPoint[],
  rangeStart: string,
  rangeEnd: string,
  granularity: AiUsageTrendGranularity,
  nowAt = Date.now(),
) {
  const { startAt, endAt } = localRangeBoundaries(rangeStart, rangeEnd);
  const latestBucketAt = bucketStartAt(Math.min(endAt, nowAt), granularity);
  const firstBucketAt = bucketStartAt(startAt, granularity);
  if (!Number.isFinite(latestBucketAt) || latestBucketAt < firstBucketAt) return [];

  const byBucketStart = new Map<number, TrendPoint>();
  for (const point of points) {
    const timestamp = new Date(point.bucketStartedAt).getTime();
    if (!Number.isFinite(timestamp)) continue;
    const bucketStartedAt = bucketStartAt(timestamp, granularity);
    const current = byBucketStart.get(bucketStartedAt) ?? emptyTrendPoint(bucketStartedAt);
    current.invocationCount += point.invocationCount;
    current.tokenUsageInvocationCount += point.tokenUsageInvocationCount;
    current.inputTokens += point.inputTokens;
    current.cachedReadTokens += point.cachedReadTokens;
    current.cachedWriteTokens += point.cachedWriteTokens;
    current.outputTokens += point.outputTokens;
    current.totalTokens += point.totalTokens;
    byBucketStart.set(bucketStartedAt, current);
  }

  const completed: TrendPoint[] = [];
  for (let bucketStartedAt = firstBucketAt; bucketStartedAt <= latestBucketAt;) {
    completed.push(byBucketStart.get(bucketStartedAt) ?? emptyTrendPoint(bucketStartedAt));
    const next = nextBucketStart(bucketStartedAt, granularity);
    if (!Number.isFinite(next) || next <= bucketStartedAt) break;
    bucketStartedAt = next;
  }
  return completed;
}

function chartSeriesData(points: TrendPoint[], key: TrendSeriesKey) {
  return points.flatMap((point) => {
    const timestamp = new Date(point.bucketStartedAt).getTime();
    return Number.isFinite(timestamp) ? [[timestamp, point[key]] as [number, number]] : [];
  });
}

function Metric({ label, value, description }: { label: string; value: string; description: string }) {
  return <div className="admin-token-trend-metric"><dt>{label}</dt><dd>{value}</dd><small>{description}</small></div>;
}

function EChartsTrendCanvas({ description, option }: { description: string; option: EChartsOption }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const [renderFailed, setRenderFailed] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    let chart: ECharts | null = null;
    let observer: ResizeObserver | null = null;
    const resize = () => {
      try {
        chart?.resize();
      } catch {
        // The page and trend-detail table remain usable if a browser rejects a resize.
      }
    };

    try {
      chart = echarts.init(container, undefined, { renderer: "canvas" });
      chart.setOption(option, { lazyUpdate: true, notMerge: true });
      chartRef.current = chart;
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(resize);
        observer.observe(container);
      } else {
        window.addEventListener("resize", resize);
      }
    } catch {
      chart?.dispose();
      chartRef.current = null;
      setRenderFailed(true);
    }

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      chart?.dispose();
      if (chartRef.current === chart) chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    try {
      chart.setOption(option, { lazyUpdate: true, notMerge: true });
      setRenderFailed(false);
    } catch {
      setRenderFailed(true);
    }
  }, [option]);

  if (renderFailed) {
    return <div className="admin-token-chart-fallback" role="status"><Icon name="activity" size={18} /><span>图表暂时无法渲染，请查看下方趋势明细。</span></div>;
  }

  return <div aria-label={description} className="admin-token-chart-canvas" ref={containerRef} role="img" />;
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
  const points = useMemo(() => completeTrendPoints(
    buildTrendPoints(buckets),
    rangeStart,
    rangeEnd,
    granularity,
  ), [buckets, granularity, rangeEnd, rangeStart]);
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
  const { startAt, endAt: requestedEndAt } = useMemo(
    () => localRangeBoundaries(rangeStart, rangeEnd),
    [rangeEnd, rangeStart],
  );
  // Future hours have no observed usage yet, so the chart ends at the current
  // instant instead of presenting them as an artificial empty tail.
  const endAt = Math.max(startAt, Math.min(requestedEndAt, Date.now()));
  const hasReportedUsage = summary.tokenUsageInvocationCount > 0;
  const includeYearInAxis = new Date(rangeStart).getFullYear() !== new Date(rangeEnd).getFullYear();
  const chartDescription = hasReportedUsage
    ? `${scopeLabel}，${coverageLabel}，${rangeLabel}。共 ${numberFormat(summary.totalTokens)} Token。`
    : `${scopeLabel}，${coverageLabel}，${rangeLabel}。没有模型返回的 Token 用量。`;

  const chartOption = useMemo<EChartsOption>(() => ({
    animation: false,
    aria: {
      enabled: true,
      description: chartDescription,
    },
    color: trendSeries.map((series) => series.color),
    grid: {
      top: 46,
      right: 20,
      bottom: 34,
      left: 72,
    },
    legend: {
      top: 6,
      right: 12,
      selectedMode: false,
      icon: "roundRect",
      itemWidth: 15,
      itemHeight: 4,
      itemGap: 14,
      textStyle: {
        color: "#74706d",
        fontSize: 11,
        fontFamily: "inherit",
      },
    },
    textStyle: {
      fontFamily: "inherit",
    },
    tooltip: {
      trigger: "axis",
      appendToBody: true,
      backgroundColor: "#ffffff",
      borderColor: "#dedad5",
      borderWidth: 1,
      confine: true,
      padding: [9, 11],
      textStyle: {
        color: "#292522",
        fontSize: 12,
      },
      axisPointer: {
        type: "line",
        lineStyle: {
          color: "#b6b0aa",
          type: "dashed",
        },
      },
      valueFormatter: (value) => `${numberFormat(Number(value))} Token`,
    },
    xAxis: {
      type: "time",
      min: startAt,
      max: endAt,
      axisLine: {
        lineStyle: { color: "#dcd7d1" },
      },
      axisTick: { show: false },
      axisLabel: {
        color: "#74706d",
        fontSize: 11,
        hideOverlap: true,
        margin: 12,
        formatter: (value: string | number) => formatBucketLabel(value, granularity, includeYearInAxis),
      },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: (value: { max: number }) => Math.max(value.max, 1),
      splitNumber: 4,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: "#74706d",
        fontSize: 11,
        margin: 12,
        formatter: (value: number) => formatTokenVolume(value),
      },
      splitLine: {
        lineStyle: {
          color: "#ebe7e2",
          type: "dashed",
        },
      },
    },
    series: trendSeries.map((series, index) => ({
      name: series.label,
      type: "line" as const,
      data: chartSeriesData(points, series.key),
      showSymbol: false,
      smooth: 0.22,
      connectNulls: true,
      lineStyle: {
        width: series.lineType === "solid" ? 2.5 : 2,
        type: series.lineType,
      },
      itemStyle: { color: series.color },
      emphasis: {
        focus: "series",
        scale: true,
        lineStyle: { width: 3.25 },
      },
      z: trendSeries.length - index,
    })),
  }), [chartDescription, endAt, granularity, includeYearInAxis, points, startAt]);

  return (
    <section aria-labelledby="admin-token-trend-title" className="admin-token-trend-panel">
      <header className="admin-token-trend-header">
        <div>
          <div className="admin-token-trend-title"><Icon name="activity" size={18} /><h2 id="admin-token-trend-title">Token 使用趋势</h2></div>
          <p>{scopeLabel} · {coverageLabel} · {rangeLabel}</p>
          <small className="admin-token-trend-scope-note">{isCrossModelAggregate ? `按 ${timeZone} 分桶；空闲时段按 0 Token 连续展示，当前为跨模型汇总，不用于模型间比较。` : `按 ${timeZone} 分桶；空闲时段按 0 Token 连续展示，所有 Token 均归属于所选 Provider / 模型。`}</small>
        </div>
        <dl className="admin-token-trend-metrics">
          <Metric label="输入" value={`${formatTokenVolume(summary.inputTokens)} Token`} description={numberFormat(summary.inputTokens)} />
          <Metric label="输出及推理" value={`${formatTokenVolume(summary.outputTokens)} Token`} description={numberFormat(summary.outputTokens)} />
          <Metric label="缓存命中" value={`${formatTokenVolume(summary.cachedReadTokens)} Token`} description={numberFormat(summary.cachedReadTokens)} />
          <Metric label="缓存写入" value={`${formatTokenVolume(summary.cachedWriteTokens)} Token`} description={numberFormat(summary.cachedWriteTokens)} />
        </dl>
      </header>

      {points.length ? (
        <div aria-label="Token 使用趋势图" className="admin-token-chart-frame">
          <EChartsTrendCanvas description={chartDescription} option={chartOption} />
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
