import { expect, test } from "@playwright/test";

import { registerAndVerify, seedWorkspaceFixture } from "./helpers";

test("workspace select menus render on an opaque elevated surface", async ({ page }) => {
  await registerAndVerify(page, "dropdown-surface");
  await seedWorkspaceFixture(page);

  await page.locator(".rail-item").nth(4).click();

  const select = page.locator(".jd-switcher-select .semi-select");
  await expect(select).toBeVisible();
  await select.click();

  const popup = page.locator(".backoffice-ui-root .semi-popover-wrapper").last();
  await expect(popup).toBeVisible();

  const surface = await popup.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      boxShadow: style.boxShadow,
    };
  });

  expect(surface.backgroundColor).toBe("rgb(255, 255, 255)");
  expect(surface.boxShadow).not.toBe("none");
  expect(surface.boxShadow).not.toBe("");
});
