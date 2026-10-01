import { expect, test, type Page } from "@playwright/test";

import { registerAndVerify } from "./helpers";

const candidateAlpha = "candidate-alpha";
const candidateBeta = "candidate-beta";

function pendingDetail(id: string, observationCandidateId: string) {
  return {
    id,
    title: id === "pending-alpha" ? "候选人分析草稿 A" : "候选人分析草稿 B",
    version: 1,
    candidates: [
      {
        candidate_id: candidateAlpha,
        candidate_code: "CAND-ALPHA",
        resume_id: "resume-alpha-v2",
        fact_snapshot_id: "facts-alpha-v4",
        facts_version: 4,
      },
      {
        candidate_id: candidateBeta,
        candidate_code: "CAND-BETA",
        resume_id: "resume-beta-v3",
        fact_snapshot_id: "facts-beta-v6",
        facts_version: 6,
      },
    ],
    job: { job_id: "job-recruiter", job_version_id: "job-version-recruiter-v5" },
    job_title: "招聘专员",
    source_connection_name: "E2E MCP",
    payload_sha256: "a".repeat(64),
    source_status: "current",
    referenced_facts: [
      {
        candidate_id: candidateAlpha,
        candidate_code: "CAND-ALPHA",
        resume_id: "resume-alpha-v2",
        fact_snapshot_id: "facts-alpha-v4",
        facts_version: 4,
        evidence_source_block_ids: ["evidence-alpha-01"],
        omitted_fields: [],
        facts: {
          highest_degree: "bachelor",
          employment_months: 36,
          employment_or_internship_months: 42,
          education: [],
          experiences: [],
          skills: [{ skill: "招聘", evidence_source_block_ids: ["evidence-alpha-01"] }],
          language_credentials: [],
          scholarships: [],
        },
      },
      {
        candidate_id: candidateBeta,
        candidate_code: "CAND-BETA",
        resume_id: "resume-beta-v3",
        fact_snapshot_id: "facts-beta-v6",
        facts_version: 6,
        evidence_source_block_ids: ["evidence-beta-02"],
        omitted_fields: [],
        facts: {
          highest_degree: "master",
          employment_months: 48,
          employment_or_internship_months: 48,
          education: [],
          experiences: [],
          skills: [{ skill: "组织发展", evidence_source_block_ids: ["evidence-beta-02"] }],
          language_credentials: [],
          scholarships: [],
        },
      },
    ],
    inferences: [{ candidate_id: observationCandidateId, text: "同一条推断文本" }],
    questions_to_verify: [{ candidate_id: observationCandidateId, text: "同一条待核验文本" }],
    created_at: "2026-10-01T08:00:00Z",
    expires_at: "2026-10-01T08:15:00Z",
  };
}

async function mockIntegrationSettings(
  page: Page,
  identity: { userId: string; workspaceId: string },
): Promise<void> {
  const pending = [
    pendingDetail("pending-alpha", candidateAlpha),
    pendingDetail("pending-beta", candidateBeta),
  ];
  await page.route(/\/v1\/integration-settings(?:\/.*)?$/, async (route) => {
    const { pathname } = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });

    if (pathname === "/v1/integration-settings") {
      return json({
        user: { id: identity.userId, display_name: "E2E 管理员", email: "e2e@example.test" },
        workspace: {
          organization_id: identity.workspaceId,
          name: "E2E 工作区",
          enabled: true,
          allowed_scopes: ["candidates:read", "jobs:read", "assessments:read", "analyses:read", "analyses:write", "evidence:read"],
        },
        features: { api: false, mcp: false, analyses: true, oauth: false },
        endpoints: { api_base_url: "https://example.test/v1/integrations", mcp_url: "https://example.test/v1/mcp" },
        csrf_token: "e2e-csrf-token",
        permissions: { can_create: false, can_admin: false, can_revoke_own: true },
        grants: [],
      });
    }
    if (pathname === "/v1/integration-settings/activity") return json({ items: [], next_cursor: null });
    if (pathname === "/v1/integration-settings/analysis-reports") return json({ items: [], next_cursor: null });
    if (pathname === "/v1/integration-settings/analysis-reports/pending") {
      return json({
        items: pending.map(({ referenced_facts, inferences, payload_sha256, questions_to_verify, source_status, ...summary }) => summary),
      });
    }
    const detail = pending.find((item) => pathname === `/v1/integration-settings/analysis-reports/pending/${item.id}`);
    if (detail) return json(detail);
    return route.continue();
  });
}

test("确认前会显示每条外部 AI 内容的候选人归属、版本与证据", async ({ page }) => {
  await registerAndVerify(page, "integration-analysis-review");
  const sessionResponse = await page.context().request.get(new URL("/v1/auth/session", page.url()).toString());
  expect(sessionResponse.ok()).toBe(true);
  const session = await sessionResponse.json() as {
    user: { user_id: string };
    organization: { organization_id: string };
  };
  await mockIntegrationSettings(page, {
    userId: session.user.user_id,
    workspaceId: session.organization.organization_id,
  });
  await page.goto("/#settings/integrations");

  await expect(page.getByRole("heading", { name: "API 与 AI 工具连接" })).toBeVisible();
  await page.getByRole("tab", { name: "我的分析记录", exact: true }).click();
  await expect(page.getByText("待你确认的外部 AI 草稿", { exact: true })).toBeVisible();

  const firstRow = page.getByRole("row").filter({ hasText: "候选人分析草稿 A" });
  const firstDetailResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith("/analysis-reports/pending/pending-alpha")
  ));
  await firstRow.getByRole("button", { name: "核对内容" }).click();
  expect((await firstDetailResponse).status()).toBe(200);
  await expect(page.getByText("岗位版本：job-version-recruiter-v5", { exact: true })).toBeVisible();
  await expect(page.getByText("简历版本引用：resume-alpha-v2；事实版本：v4；事实快照：facts-alpha-v4", { exact: true })).toBeVisible();
  await expect(page.getByText("引用证据：evidence-alpha-01", { exact: true })).toBeVisible();
  await expect(page.locator('[data-candidate-code="CAND-ALPHA"]').getByText("同一条推断文本", { exact: true })).toBeVisible();
  await expect(page.locator('[data-candidate-code="CAND-ALPHA"]').getByText("同一条待核验文本", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "暂不处理" }).click();
  const secondRow = page.getByRole("row").filter({ hasText: "候选人分析草稿 B" });
  await secondRow.getByRole("button", { name: "核对内容" }).click();
  await expect(page.locator('[data-candidate-code="CAND-BETA"]').getByText("同一条推断文本", { exact: true })).toBeVisible();
  await expect(page.locator('[data-candidate-code="CAND-BETA"]').getByText("同一条待核验文本", { exact: true })).toBeVisible();
});
