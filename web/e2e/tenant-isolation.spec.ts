import { expect, test } from "@playwright/test";

import { registerAndVerify, seedWorkspaceFixture } from "./helpers";

test("第二个工作区无法通过资源 ID 读取第一个工作区的简历", async ({ browser }) => {
  const firstContext = await browser.newContext();
  const firstPage = await firstContext.newPage();
  try {
    await registerAndVerify(firstPage, "tenant-a");
    const firstFixture = await seedWorkspaceFixture(firstPage);
    const protectedResumeId = firstFixture.resume_ids[0];
    if (!protectedResumeId) throw new Error("Expected an E2E fixture resume.");

    const secondContext = await browser.newContext();
    const secondPage = await secondContext.newPage();
    try {
      await registerAndVerify(secondPage, "tenant-b");
      const response = await secondPage.context().request.get(
        new URL(`/v1/resumes/${protectedResumeId}`, secondPage.url()).toString(),
      );
      expect(response.status()).toBe(404);
      await expect(response.json()).resolves.toMatchObject({ detail: "resume_not_found" });
    } finally {
      await secondContext.close();
    }
  } finally {
    await firstContext.close();
  }
});
