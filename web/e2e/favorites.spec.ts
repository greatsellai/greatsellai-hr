import { expect, test } from "@playwright/test";

import { e2eControl, registerAndVerify, seedWorkspaceFixture } from "./helpers";

test("收藏候选人会出现在左侧收藏库，并可在详情中移除", async ({ page }) => {
  await registerAndVerify(page, "candidate-favorites");
  const fixture = await seedWorkspaceFixture(page);
  const resumeId = fixture.resume_ids[0];
  if (!resumeId) throw new Error("Expected an E2E fixture resume.");

  const resume = await e2eControl<{ candidate_id: string }>(
    page,
    `/v1/resumes/${resumeId}`,
  );
  await page.reload();
  const detailsButton = page.getByRole("button", {
    name: "查看 E2E 推荐候选人 的简历详情",
  });
  await detailsButton.click();

  const drawer = page.getByRole("dialog", {
    name: "E2E 推荐候选人 的简历详情",
  });
  const favoriteButton = drawer.getByRole("button", {
    name: "收藏候选人",
  });
  await expect(favoriteButton).toBeVisible();
  await expect(favoriteButton).toHaveAttribute("aria-pressed", "false");

  const addResponse = page.waitForResponse((response) => (
    response.request().method() === "PUT"
    && new URL(response.url()).pathname === `/v1/candidates/${resume.candidate_id}/favorite`
  ));
  await favoriteButton.click();
  await addResponse;
  await expect(
    drawer.getByRole("button", { name: "从收藏库移除候选人" }),
  ).toHaveAttribute("aria-pressed", "true");

  await drawer.getByRole("button", { name: "关闭简历详情" }).click();
  await page.getByRole("button", { name: "收藏库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "收藏库", exact: true })).toBeVisible();
  const favoriteDetailsButton = page.getByRole("button", {
    name: "查看 E2E 推荐候选人 的简历详情",
  });
  await expect(favoriteDetailsButton).toBeVisible();
  await favoriteDetailsButton.click();

  const removeResponse = page.waitForResponse((response) => (
    response.request().method() === "DELETE"
    && new URL(response.url()).pathname === `/v1/candidates/${resume.candidate_id}/favorite`
  ));
  await drawer.getByRole("button", { name: "从收藏库移除候选人" }).click();
  await removeResponse;
  await expect(
    drawer.getByRole("button", { name: "收藏候选人" }),
  ).toHaveAttribute("aria-pressed", "false");
  await drawer.getByRole("button", { name: "关闭简历详情" }).click();
  await expect(page.getByRole("heading", { name: "还没有收藏候选人" })).toBeVisible();
});
