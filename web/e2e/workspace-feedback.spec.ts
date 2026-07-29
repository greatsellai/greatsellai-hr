import { expect, test } from "@playwright/test";

import { openAccountMenu, registerAndVerify } from "./helpers";

const feedbackScreenshot = Buffer.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
  0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89,
]);

test.describe("使用体验反馈", () => {
  test("从账户菜单提交完整意见和截图后进入审核队列", async ({ page }) => {
    await registerAndVerify(page, "workspace-feedback");
    await openAccountMenu(page);

    const accountMenu = page.getByRole("dialog", { name: "账户菜单" });
    await accountMenu.getByRole("button", { name: /提交宝贵意见/ }).click();

    await expect(page).toHaveURL(/#feedback$/);
    await expect(page.locator(".feedback-page")).toBeVisible();
    await expect(page.locator(".feedback-page")).toContainText("系统审核通过后");

    await page.getByLabel("联系电话").fill("138 0013 8000");

    const answers = page.locator(".feedback-question-list textarea");
    await expect(answers).toHaveCount(4);
    await answers.nth(0).fill("我在批量筛选候选人时使用 GreatSell AI。");
    await answers.nth(1).fill("我希望快速找到有项目经验的候选人。");
    await answers.nth(2).fill("技能命中和简历证据之间的对应关系还不够直观。");
    await answers.nth(3).fill("希望结果中直接展示技能对应的项目或经历证据。");
    await page.locator("#feedback-attachments").setInputFiles({
      name: "feedback.png",
      mimeType: "image/png",
      buffer: feedbackScreenshot,
    });
    await expect(page.getByText("feedback.png", { exact: true })).toBeVisible();

    await page.locator(".feedback-form .button-primary").click();

    await expect(page.locator(".feedback-history-list")).toContainText("系统审核中，审核通过后发放 500 次 AI 调用额度");
    await expect(page.locator(".feedback-form")).toContainText("下一次可提交意见的时间");
    await expect(page.locator(".feedback-form .button-primary")).toBeDisabled();
    await expect(page.locator(".feedback-history-list")).toContainText("查看填写内容");
  });
});
