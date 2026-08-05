import { expect, test } from "@playwright/test";

import { registerAndVerify } from "./helpers";

const authPageTitles = [
  ["/login", "登录大卖智聘｜大卖数智"],
  ["/register", "免费试用大卖智聘｜大卖数智"],
  ["/forgot-password", "找回密码｜大卖智聘"],
  ["/reset-password", "设置新密码｜大卖智聘"],
  ["/verify-email", "验证邮箱｜大卖智聘"],
] as const;

test.describe("认证产品命名", () => {
  test("认证入口与登录后工作台使用统一产品名称", async ({ page }) => {
    for (const [path, title] of authPageTitles) {
      await page.goto(path);
      await expect(page).toHaveTitle(title);
    }

    await page.goto("/login");
    await expect(page.getByText("大卖智聘｜AI 招聘决策工作台", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "登录大卖智聘" })).toBeVisible();
    await expect(
      page.getByText(
        "进入只属于你所在团队的招聘工作区。候选人、岗位、评分和原始文件按工作区分别管理。",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      page.getByText("从简历筛选、AI 评分到 JD 匹配，在大卖智聘统一完成", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("GREATSELL AI · 招聘工具", { exact: true })).toHaveCount(0);
    await expect(page.getByText("登录招聘工作台", { exact: true })).toHaveCount(0);

    const brandLink = page.getByRole("link", { name: "大卖数智首页" });
    await expect(brandLink).toHaveAttribute("href", "/");
    await expect(brandLink.getByRole("img")).toHaveAttribute("alt", "大卖数智 GreatSell AI");

    const trialLink = page.getByRole("link", { name: "申请免费试用30天", exact: true });
    await expect(trialLink).toHaveAttribute("href", "/register");
    await expect(page.getByRole("link", { name: "免费试用 30 天", exact: true })).toHaveCount(0);

    await registerAndVerify(page, "auth-product-naming");
    await expect(page).toHaveTitle("大卖智聘｜AI 招聘决策工作台");
  });

  test("登录页在常用视口保持可读且没有横向溢出", async ({ page }) => {
    for (const viewport of [
      { width: 375, height: 900 },
      { width: 768, height: 900 },
      { width: 1440, height: 1000 },
    ]) {
      await page.setViewportSize(viewport);
      await page.goto("/login");

      await expect(page.getByRole("heading", { name: "登录大卖智聘" })).toBeVisible();
      await expect(page.getByRole("button", { name: "登录工作台" })).toBeVisible();
      await expect(page.getByRole("link", { name: "申请免费试用30天", exact: true })).toBeVisible();
      await expect(page).toHaveTitle("登录大卖智聘｜大卖数智");
      await expect
        .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
        .toBe(true);
    }
  });
});
