import { expect, test } from "@playwright/test";

import { registerAndVerify, seedWorkspaceFixture } from "./helpers";

/**
 * Resource routes are deliberately hash based so both supported public entry
 * paths can keep serving the same SPA.  Default selections must not rewrite
 * the browser history; explicit JD and new-JD routes must survive a refresh.
 */
test("招聘工作台深链接可刷新，默认入口不会吞掉浏览器返回", async ({ page }) => {
  await registerAndVerify(page, "recruiting-workbench-routing");
  const fixture = await seedWorkspaceFixture(page);

  await page.goto("/#jobs");
  await expect(page.getByRole("heading", { name: "职位管理", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#jobs$/);

  await page.goto(`/#jobs?jobVersion=${fixture.job_version_id}`);
  await expect(page.getByRole("button", { name: "职位管理", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByLabel("当前已启用岗位的 JD 原文")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("当前已启用岗位的 JD 原文")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`#jobs\\?jobVersion=${fixture.job_version_id}$`));

  await page.goBack();
  await expect(page.getByRole("heading", { name: "职位管理", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#jobs$/);

  await page.goto("/#jobs?new=1");
  await expect(page.getByLabel("岗位名称", { exact: true })).toBeVisible();
  await expect(page.getByLabel("岗位需求或完整 JD", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("岗位名称", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#jobs\?new=1$/);

  await page.goto("/#matching?jobVersion=missing-job-version");
  await expect(page.getByRole("heading", { name: "智能匹配", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#matching$/);

  await page.goto("/#workflow?job=missing-job");
  await expect(page.getByRole("heading", { level: 1, name: "招聘流程", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#workflow$/);
});
