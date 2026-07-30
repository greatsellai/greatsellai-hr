import { expect, test, type Page } from "@playwright/test";

import { e2eResumePdf, registerAndVerify } from "./helpers";

interface CandidateCreated {
  candidate_id: string;
}

interface ResumeUploaded {
  resume_id: string;
}

interface CandidateResumeVersions {
  items: Array<{ resume_id: string }>;
}

function apiUrl(page: Page, path: string): string {
  return new URL(path, page.url()).toString();
}

/**
 * Build two ordinary resume records under the same candidate through the real
 * candidate/upload APIs. The test does not need an AI provider: the API's
 * real version ordering remains the source of truth for the drawer.
 */
async function createCandidateWithTwoResumeVersions(
  page: Page,
  candidateName: string,
): Promise<{ candidateId: string; firstResumeId: string; secondResumeId: string }> {
  const createResponse = await page.context().request.post(
    apiUrl(page, "/v1/candidates"),
    { data: { display_name: candidateName } },
  );
  expect(createResponse.status()).toBe(200);
  const created = (await createResponse.json()) as CandidateCreated;
  expect(created.candidate_id).toBeTruthy();

  const uploadVersion = async (
    filename: string,
    marker: string,
  ): Promise<string> => {
    const uploadResponse = await page.context().request.post(
      apiUrl(page, `/v1/candidates/${created.candidate_id}/resumes`),
      {
        multipart: {
          file: {
            name: filename,
            mimeType: "application/pdf",
            // A PDF comment makes the two local fixture files distinct while
            // retaining a valid one-page source document.
            buffer: Buffer.concat([
              e2eResumePdf(),
              Buffer.from(`\n% ${marker}\n`, "ascii"),
            ]),
          },
        },
      },
    );
    expect(uploadResponse.status()).toBe(200);
    const uploaded = (await uploadResponse.json()) as ResumeUploaded;
    expect(uploaded.resume_id).toBeTruthy();

    return uploaded.resume_id;
  };

  const firstResumeId = await uploadVersion(
    "e2e-favorite-version-1.pdf",
    "favorite-version-one",
  );
  const secondResumeId = await uploadVersion(
    "e2e-favorite-version-2.pdf",
    "favorite-version-two",
  );

  return { candidateId: created.candidate_id, firstResumeId, secondResumeId };
}

test("个人收藏按候选人聚合，抽屉可切换所有简历版本", async ({ page }) => {
  await registerAndVerify(page, "candidate-favorites");
  const candidateName = "E2E Favorite Multi Version";
  const { candidateId, firstResumeId, secondResumeId } =
    await createCandidateWithTwoResumeVersions(page, candidateName);

  // Seed only the private association through the ordinary API. The two
  // uploaded versions deliberately remain in extraction state so this test
  // verifies that favorites never depend on copied AI facts or scores.
  const favoriteResponse = await page.context().request.put(
    apiUrl(page, `/v1/candidates/${candidateId}/favorite`),
  );
  expect(favoriteResponse.status()).toBe(200);

  // Never infer “current” from timestamps or UUID ordering in the test.
  // Read the same candidate-level version API that powers the drawer instead.
  const versionsResponse = await page.context().request.get(
    apiUrl(page, `/v1/candidates/${candidateId}/resume-versions`),
  );
  expect(versionsResponse.status()).toBe(200);
  const versions = (await versionsResponse.json()) as CandidateResumeVersions;
  expect(versions.items.map((item) => item.resume_id)).toEqual(
    expect.arrayContaining([firstResumeId, secondResumeId]),
  );
  expect(versions.items).toHaveLength(2);
  const initialResumeId = versions.items[0]?.resume_id;
  const alternateResumeId = versions.items.find(
    (item) => item.resume_id !== initialResumeId,
  )?.resume_id;
  if (!initialResumeId || !alternateResumeId) {
    throw new Error("Expected two distinct candidate resume versions.");
  }

  // The left rail opens the private worklist. The list has one candidate with
  // two source files, not two copied talent records.
  await page.getByRole("button", { name: "我的收藏", exact: true }).click();
  await expect(page).toHaveURL(/#favorites$/);
  await expect(
    page.getByRole("heading", { name: "我的收藏", exact: true }),
  ).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(
    page.getByRole("heading", { name: "招聘工作台", exact: true }),
  ).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(/#favorites$/);
  await expect(
    page.getByRole("heading", { name: "我的收藏", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/#favorites$/);
  await expect(
    page.getByRole("heading", { name: "我的收藏", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(candidateName, { exact: true })).toBeVisible();
  await expect(page.getByText("2 个版本", { exact: true })).toBeVisible();

  const openDetails = page.getByRole("button", {
    name: `查看 ${candidateName} 的简历详情`,
  });
  await openDetails.click();
  const drawer = page.getByRole("dialog", {
    name: `${candidateName} 的简历详情`,
  });
  await expect(drawer).toBeVisible();

  const versionSelect = drawer.locator("#candidate-drawer-resume-version");
  await expect(versionSelect).toHaveCount(1);
  await expect(versionSelect.locator("option")).toHaveCount(2);
  await expect(versionSelect).toHaveValue(initialResumeId);

  // Switching to an older source version only changes the displayed resume.
  // Candidate-level personal favorite state remains on the same candidate.
  const versionReview = page.waitForResponse((response) => {
    const requestUrl = new URL(response.url());
    return (
      response.request().method() === "GET" &&
      response.status() === 200 &&
      requestUrl.pathname === `/v1/resumes/${alternateResumeId}/review`
    );
  });
  await versionSelect.selectOption(alternateResumeId);
  await versionReview;
  await expect(versionSelect).toHaveValue(alternateResumeId);
  const favoriteInDrawer = drawer.getByRole("button", {
    name: `取消收藏候选人 ${candidateName}`,
  });
  await expect(favoriteInDrawer).toHaveAttribute("aria-pressed", "true");

  // Both drawer actions mutate only the current user's candidate association;
  // switching resume versions never changes that state.
  await favoriteInDrawer.click();
  const reFavoriteInDrawer = drawer.getByRole("button", {
    name: `收藏候选人 ${candidateName}`,
  });
  await expect(reFavoriteInDrawer).toHaveAttribute("aria-pressed", "false");
  await reFavoriteInDrawer.click();
  await expect(favoriteInDrawer).toHaveAttribute("aria-pressed", "true");
  await drawer.getByRole("button", { name: "关闭简历详情" }).click();
  await expect(drawer).toBeHidden();
  await expect(
    page.locator(".favorite-candidates-table").getByText(candidateName, {
      exact: true,
    }),
  ).toBeVisible();
});
