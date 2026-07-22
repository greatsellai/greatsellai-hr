import type { AiModelProfile, AiProviderProfile, AiRoutePolicy } from "./admin-types";

const LEGACY_RUNTIME_PROVIDER_SLUG = "legacy-runtime-openai-compatible";
const LEGACY_RUNTIME_MODEL_SLUG = "legacy-runtime-default";

const knownModelNames: Record<string, string> = {
  "deepseek-v4-flash": "DeepSeek V4 Flash",
  "deepseek-chat": "DeepSeek Chat",
  "deepseek-reasoner": "DeepSeek Reasoner",
};

const featureCopy: Record<string, { name: string; description: string }> = {
  extraction: { name: "简历提取", description: "从简历中整理候选人信息。" },
  resume_extract_rich: { name: "简历深度提取", description: "提取完整的候选人结构化信息。" },
  resume_extract_core: { name: "简历核心信息提取", description: "提取筛选所需的核心字段。" },
  candidate_name_backfill: { name: "候选人姓名补全", description: "基于简历原文补全可核验的姓名。" },
  summary: { name: "简历总结", description: "生成候选人经历与亮点摘要。" },
  resume_summary: { name: "简历总结", description: "生成候选人经历与亮点摘要。" },
  scoring: { name: "简历评分", description: "根据岗位要求生成候选人评分。" },
  resume_score: { name: "简历评分", description: "根据岗位要求生成候选人评分。" },
  matching: { name: "JD 匹配", description: "分析候选人与岗位的匹配情况。" },
  match: { name: "JD 匹配", description: "分析候选人与岗位的匹配情况。" },
  jd_match: { name: "JD 匹配", description: "分析候选人与岗位的匹配情况。" },
  jd_generation: { name: "JD 生成", description: "根据岗位需求生成职位描述。" },
  jd_generate: { name: "JD 生成", description: "根据岗位需求生成职位描述。" },
  jd_requirements_extract: { name: "JD 要求提取", description: "将职位描述整理为评估要求。" },
  requirements_extract: { name: "JD 要求提取", description: "将职位描述整理为评估要求。" },
  recruiting_agent_turn: { name: "招聘助手对话", description: "为招聘助手生成下一轮回复。" },
  agent: { name: "招聘助手对话", description: "为招聘助手生成下一轮回复。" },
  resume_ocr_page: { name: "简历 OCR 识别", description: "识别扫描件或图片简历页面。" },
};

export const aiFeatureFilterOptions = [
  "resume_extract_rich",
  "resume_extract_core",
  "candidate_name_backfill",
  "resume_score",
  "resume_summary",
  "jd_generate",
  "jd_requirements_extract",
  "jd_match",
  "recruiting_agent_turn",
  "resume_ocr_page",
].map((value) => ({ value, label: featureCopy[value]?.name ?? value }));

export function featureDisplayName(feature: string) {
  return featureCopy[feature]?.name ?? feature;
}

export function featureDescription(feature: string) {
  return featureCopy[feature]?.description ?? "当前 AI 功能。";
}

export function providerDisplayName(provider: Pick<AiProviderProfile, "slug" | "display_name">) {
  return provider.slug === LEGACY_RUNTIME_PROVIDER_SLUG ? "DeepSeek" : provider.display_name;
}

export function modelDisplayName(model: Pick<AiModelProfile, "slug" | "display_name" | "provider_model_id">) {
  if (model.slug === LEGACY_RUNTIME_MODEL_SLUG) {
    const providerModelId = model.provider_model_id.trim();
    return knownModelNames[providerModelId.toLowerCase()] || providerModelId || "DeepSeek 默认模型";
  }
  return model.display_name;
}

export function routeDisplayName(route: Pick<AiRoutePolicy, "feature" | "display_name">) {
  return route.display_name === route.feature ? featureDisplayName(route.feature) : route.display_name;
}

export function routeDisplayDescription(route: Pick<AiRoutePolicy, "feature" | "description">) {
  if (route.description && route.description !== "Compatibility route created during AI gateway migration.") {
    return route.description;
  }
  return featureDescription(route.feature);
}

export function serviceKindDisplayName(serviceKind: string) {
  const names: Record<string, string> = {
    llm: "大模型",
    ocr: "文字识别",
  };
  return names[serviceKind] ?? serviceKind;
}
