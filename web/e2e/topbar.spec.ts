import { expect, test } from "@playwright/test";

import { registerAndVerify } from "./helpers";

test.describe("workspace topbar", () => {
  test("does not expose the deprecated global keyword search", async ({ page }) => {
    await registerAndVerify(page, "topbar-without-global-search");

    await expect(
      page.getByPlaceholder("输入技能或关键词，按 Enter 筛选"),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "条件筛选", exact: true }),
    ).toBeVisible();
  });
});
