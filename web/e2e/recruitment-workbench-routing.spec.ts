import { expect, test } from "@playwright/test";

import { e2eControl, registerAndVerify, seedWorkspaceFixture } from "./helpers";

interface ConfirmedJobVersionFixture {
  job_id: string;
  job_version_id: string;
  version: number;
}

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
  await expect(page.getByRole("heading", { level: 1, name: "招聘工作台", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#workbench$/);
  await expect(page.getByRole("button", { name: "招聘流程", exact: true })).toHaveCount(0);

  await page.goto("/#recruiting");
  await expect(page.getByRole("heading", { level: 1, name: "招聘工作台", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#workbench$/);
});

test("职位管理将同一岗位的 JD 版本归为一个岗位", async ({ page }) => {
  await registerAndVerify(page, "job-management-versions");
  const fixture = await seedWorkspaceFixture(page);
  const versions = await e2eControl<ConfirmedJobVersionFixture[]>(
    page,
    "/v1/jobs/confirmed-versions",
  );
  const original = versions.find(
    (item) => item.job_version_id === fixture.job_version_id,
  );
  if (!original) throw new Error("Expected the seeded JD version.");

  const revised = await e2eControl<ConfirmedJobVersionFixture>(
    page,
    `/v1/jobs/${original.job_id}/publish-original-version`,
    {
      method: "POST",
      body: {
        title: "E2E 后端工程师",
        jd_text: "必须掌握 Python\n具备后端经验\n负责服务稳定性。",
      },
    },
  );
  expect(revised.job_id).toBe(original.job_id);
  expect(revised.version).toBe(original.version + 1);

  await page.goto("/#jobs");
  const jobSelector = page.locator(".semi-select#saved-job-selector");
  await expect(jobSelector).toBeVisible();
  await expect(jobSelector).toContainText(`最新 v${revised.version}`);
  await jobSelector.click();
  await expect(
    page.getByRole("option", {
      name: new RegExp(`E2E 后端工程师.*最新 v${revised.version}.*2 个版本`),
    }),
  ).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(
    page.getByText("当前岗位保留 2 个已发布版本。", { exact: true }),
  ).toBeVisible();
  const versionSelector = page.locator(".semi-select#saved-job-version-selector");
  await expect(versionSelector).toBeVisible();
  await expect(versionSelector).toContainText(`v${revised.version}`);
  await versionSelector.click();
  await page.getByRole("option", { name: /v1$/ }).click();
  await expect(page.getByLabel("当前已启用岗位的 JD 原文")).toHaveValue(
    "必须掌握 Python\n具备后端经验",
  );

  await page.getByRole("button", { name: "基于此新建版本" }).click();
  await expect(jobSelector).toContainText(`最新 v${revised.version}`);
  await expect(page.getByText(/正在基于当前岗位创建新版本。/)).toBeVisible();
});
