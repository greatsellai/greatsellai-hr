import { expect, test, type Page, type Route } from "@playwright/test";

import { accountMenuTrigger, registerAndVerify } from "./helpers";

const ICP_FILING_NUMBER = "粤ICP备2026106428号";
const ICP_FILING_URL = "https://beian.miit.gov.cn/";

function json(route: Route, body: unknown) {
  return route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function anonymousSession() {
  return {
    authenticated: false,
    login_required: true,
    is_platform_admin: false,
    email_verified: false,
    email_verification_required: false,
    user: null,
    organization: null,
    role: null,
    plan: null,
    trial: null,
  };
}

function platformAdminSession() {
  return {
    authenticated: true,
    login_required: false,
    is_platform_admin: true,
    email_verified: true,
    email_verification_required: false,
    user: {
      user_id: "icp-footer-platform-admin",
      display_name: "平台管理员",
      email: "platform-admin@example.test",
    },
    organization: null,
    role: null,
    plan: null,
    trial: null,
  };
}

async function expectIcpFilingLink(page: Page) {
  const link = page.getByRole("link", { name: ICP_FILING_NUMBER, exact: true });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", ICP_FILING_URL);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", /noreferrer/);
}

async function mockPlatformAiRequests(page: Page) {
  await page.route("**/v1/auth/session", (route) => json(route, platformAdminSession()));
  await page.route("**/v1/platform/organizations**", (route) => json(route, {
    items: [],
    total: 0,
    limit: 100,
    offset: 0,
  }));
  await page.route("**/v1/platform/ai/usage/runs**", (route) => json(route, []));
  await page.route("**/v1/platform/ai/usage/summary**", (route) => json(route, []));
  await page.route("**/v1/platform/ai/providers", (route) => json(route, []));
  await page.route("**/v1/platform/ai/models", (route) => json(route, []));
  await page.route("**/v1/platform/ai/routes", (route) => json(route, []));
}

test.describe("备案链接", () => {
  test("公开落地页展示备案号", async ({ page }) => {
    const webPort = process.env.E2E_WEB_PORT ?? "5176";
    await page.goto(`http://landing.localhost:${webPort}/`);

    await expect(page.locator(".landing-page")).toBeVisible();
    await expectIcpFilingLink(page);
  });

  test("登录页与兼容登录页展示备案号", async ({ page }) => {
    await page.route("**/v1/auth/session", (route) => json(route, anonymousSession()));
    await page.goto("/login");
    await expect(page.locator(".auth-page")).toBeVisible();
    await expectIcpFilingLink(page);

    await page.route("**/greatsellhr/v1/auth/session", (route) => json(route, anonymousSession()));
    await page.goto("/greatsellhr/login");
    await expect(page.locator(".auth-page")).toBeVisible();
    await expectIcpFilingLink(page);
  });

  test("登录后的招聘工作台展示备案号", async ({ page }) => {
    await registerAndVerify(page, "icp-footer-workspace");

    await expect(accountMenuTrigger(page)).toBeVisible();
    await expectIcpFilingLink(page);
  });

  test("平台管理展示备案号", async ({ page }) => {
    await mockPlatformAiRequests(page);
    await page.goto("/platform/ai");

    await expect(page.locator(".admin-shell")).toBeVisible();
    await expectIcpFilingLink(page);
  });
});
