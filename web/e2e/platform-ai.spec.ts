import { expect, test } from "@playwright/test";

const organizationId = "org-internal-alpha";
const organizationName = "星河科技";
const secondPageOrganizationId = "org-internal-second-page";
const secondPageOrganizationName = "第二页公司";

function organizationSummary(organization_id: string, name: string) {
  return {
    organization_id,
    name,
    plan_id: null,
    plan_code: "basic",
    plan_name: "基础版",
    plan_status: "trial",
    trial_started_at: "2026-07-01T00:00:00Z",
    trial_ends_at: "2026-08-01T00:00:00Z",
    member_count: 1,
    active_member_count: 1,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
  };
}

function json(route: import("@playwright/test").Route, body: unknown) {
  return route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("平台 AI 运营按公司选择并保持内部工作区筛选", async ({ page }) => {
  const organizations = [
    organizationSummary(organizationId, organizationName),
    ...Array.from({ length: 99 }, (_, index) => organizationSummary(`org-internal-${index}`, `测试公司 ${index}`)),
    organizationSummary(secondPageOrganizationId, secondPageOrganizationName),
  ];
  const organizationOffsets: number[] = [];

  await page.route("**/v1/auth/session", (route) => json(route, {
    authenticated: true,
    login_required: false,
    is_platform_admin: true,
    email_verified: true,
    email_verification_required: false,
    user: {
      user_id: "platform-admin",
      display_name: "Platform Admin",
      email: "platform-admin@example.test",
    },
    organization: null,
    role: null,
    plan: null,
    trial: null,
  }));
  await page.route("**/v1/platform/organizations**", (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") ?? "100");
    const offset = Number(url.searchParams.get("offset") ?? "0");
    organizationOffsets.push(offset);
    return json(route, {
      items: organizations.slice(offset, offset + limit),
      total: organizations.length,
      limit,
      offset,
    });
  });
  await page.route("**/v1/platform/ai/usage/runs**", (route) => json(route, [
    {
      run_id: "run-alpha",
      organization_id: organizationId,
      feature: "ai_summary",
      service_kind: "model",
      status: "succeeded",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: "2026-07-23T00:00:01Z",
      total_cost_cny_micros: 0,
      cost_status: "known",
      invocation_count: 1,
      potentially_billed_invocation_count: 1,
      token_usage_invocation_count: 1,
      total_tokens: 100,
    },
  ]));
  await page.route("**/v1/platform/ai/usage/summary**", (route) => json(route, []));
  await page.route("**/v1/platform/ai/providers", (route) => json(route, []));
  await page.route("**/v1/platform/ai/models", (route) => json(route, []));
  await page.route("**/v1/platform/ai/routes", (route) => json(route, []));

  await page.goto("/platform/ai");

  const companySelect = page.getByLabel("公司", { exact: true });
  await expect(companySelect).toBeVisible();
  await expect.poll(() => organizationOffsets).toContain(100);
  await expect(companySelect.locator(`option[value="${organizationId}"]`)).toHaveText(organizationName);
  await expect(companySelect.locator(`option[value="${secondPageOrganizationId}"]`)).toHaveText(secondPageOrganizationName);
  await expect(page.getByRole("columnheader", { name: "公司", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: organizationName, exact: true })).toBeVisible();
  await expect(page.getByText(organizationId, { exact: false })).toHaveCount(0);

  const filteredRunsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return request.method() === "GET"
      && url.pathname === "/v1/platform/ai/usage/runs"
      && url.searchParams.get("organization_id") === organizationId;
  });
  await companySelect.selectOption(organizationId);
  await page.getByRole("button", { name: "筛选记录", exact: true }).click();
  await filteredRunsRequest;
});
