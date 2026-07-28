import { expect, test } from "@playwright/test";

import {
  accountMenuTrigger,
  e2eResumePdf,
  e2eControl,
  logout,
  openAccountMenu,
  registerAndAwaitEmailVerification,
  registerAndVerify,
  seedWorkspaceFixture,
} from "./helpers";

interface PasswordResetDelivery {
  recipient: string;
  reset_url: string;
  expires_minutes: number;
}

interface PasswordResetDeliveriesResponse {
  deliveries: PasswordResetDelivery[];
}

test.describe("招聘工作台关键路径", () => {
  test("注册验证后可退出并重新登录", async ({ page }) => {
    const email = await registerAndVerify(page, "registration-login");
    await logout(page);
    await expect(page.getByRole("button", { name: "登录工作台" })).toBeVisible();
    await page.locator("#login-email").fill(email);
    await page.locator("#login-password").fill("E2E-password-2026");
    await page.getByRole("button", { name: "登录工作台" }).click();
    await expect(accountMenuTrigger(page)).toBeVisible();
  });

  test("试用额度只在账户菜单内展示，并支持 Escape 关闭", async ({ page }) => {
    const email = await registerAndVerify(page, "account-menu");
    const trigger = accountMenuTrigger(page);
    const menu = page.getByRole("dialog", { name: "账户菜单" });

    await expect(trigger).toHaveAccessibleName(/账户菜单：E2E 管理员/);
    await expect(trigger).not.toHaveAccessibleName(/AI 剩余/);
    await expect(page.getByText("AI 剩余 1,000 次", { exact: true })).toHaveCount(0);
    await expect(page.locator(".trial-banner")).toHaveCount(0);

    // A direct click must open and pin the menu even though moving the mouse
    // onto the trigger also opens it through hover.
    await openAccountMenu(page);
    await trigger.click();
    await expect(menu).toBeHidden();

    // Hover opens temporarily; the first click pins it and the second closes it.
    await page.locator("#main-content").hover();
    await trigger.hover();
    await expect(menu).toBeVisible();
    await menu.getByRole("button", { name: "工作区设置" }).hover();
    await page.waitForTimeout(240);
    await expect(menu).toBeVisible();
    await trigger.click();
    await expect(menu).toBeVisible();
    await trigger.click();
    await expect(menu).toBeHidden();

    await openAccountMenu(page);
    await expect(menu.getByText("E2E 管理员", { exact: true })).toBeVisible();
    await expect(menu.getByText(email, { exact: true })).toBeVisible();
    await expect(menu.locator(".account-menu-allowance")).toContainText("AI 调用已用 0 / 1,000，剩余 1,000 次");
    await expect(menu.getByRole("button", { name: "工作区设置" })).toBeVisible();

    await trigger.focus();
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("验证链接页和原注册页都会自动进入工作台", async ({ page, browser }) => {
    const { verificationPath } = await registerAndAwaitEmailVerification(
      page,
      "verification-return",
    );
    await expect(
      page.getByText("验证完成后，本页面会自动进入工作台。"),
    ).toBeVisible();

    // Use a separate browser context to prove the registration page discovers
    // the completed verification from its own session rather than sharing a
    // tab, cookie, or navigation with the page that opened the email link.
    const verificationContext = await browser.newContext();
    try {
      const verificationPage = await verificationContext.newPage();
      await verificationPage.goto(new URL(verificationPath, page.url()).toString());

      await expect(verificationPage).toHaveURL(/\/$/);
      await expect(accountMenuTrigger(verificationPage)).toBeVisible();

      await expect(page).toHaveURL(/\/$/);
      await expect(accountMenuTrigger(page)).toBeVisible();
    } finally {
      await verificationContext.close();
    }

    // A used link must not be mistaken for an already-authenticated email tab.
    // A fresh browser remains on the validation page and receives no session.
    const replayContext = await browser.newContext();
    try {
      const replayPage = await replayContext.newPage();
      await replayPage.goto(new URL(verificationPath, page.url()).toString());

      await expect(replayPage).toHaveURL(/\/verify-email\?token=/);
      await expect(
        replayPage.getByRole("heading", { name: "邮箱验证未完成" }),
      ).toBeVisible();
      await expect(accountMenuTrigger(replayPage)).toHaveCount(0);
    } finally {
      await replayContext.close();
    }
  });

  test("已验证用户可通过真实浏览器路径找回并使用新密码", async ({ page }) => {
    const email = await registerAndVerify(page, "password-reset");
    const newPassword = "E2E-password-reset-2026";

    await logout(page);
    await page.goto("/forgot-password");
    await page.locator("#reset-email").fill(email);
    await page.getByRole("button", { name: "获取重置指引" }).click();
    await expect(page.getByRole("heading", { name: "请查看邮箱" })).toBeVisible();

    const deliveryPath = `/__e2e__/password-reset-deliveries?recipient=${encodeURIComponent(email)}`;
    const controlHeaders = {
      "X-E2E-Control-Token": process.env.E2E_CONTROL_TOKEN ?? "local-playwright-control",
    };
    await expect
      .poll(async () => {
        const response = await e2eControl<PasswordResetDeliveriesResponse>(page, deliveryPath, {
          headers: controlHeaders,
        });
        return response.deliveries.length;
      })
      .toBeGreaterThan(0);
    const deliveries = await e2eControl<PasswordResetDeliveriesResponse>(page, deliveryPath, {
      headers: controlHeaders,
    });
    const delivery = deliveries.deliveries.at(-1);
    if (!delivery) throw new Error("Expected a local password reset delivery.");
    const resetUrl = new URL(delivery.reset_url);

    await page.goto(`${resetUrl.pathname}${resetUrl.search}`);
    await page.locator("#reset-password").fill(newPassword);
    await page.locator("#reset-password-confirmation").fill(newPassword);
    await page.getByRole("button", { name: "保存新密码" }).click();
    await expect(page.getByRole("heading", { name: "新密码已设置" })).toBeVisible();

    await page.getByRole("link", { name: "前往登录" }).click();
    await expect(page.getByRole("button", { name: "登录工作台" })).toBeVisible();
    await page.locator("#login-email").fill(email);
    await page.locator("#login-password").fill("E2E-password-2026");
    await page.getByRole("button", { name: "登录工作台" }).click();
    await expect(page.getByRole("alert")).toHaveText("邮箱或密码不正确，请重试。");

    await page.locator("#login-password").fill(newPassword);
    await page.getByRole("button", { name: "登录工作台" }).click();
    await expect(accountMenuTrigger(page)).toBeVisible();
  });

  test("上传页面通过真实 multipart 请求保存 PDF 并进入 AI 队列", async ({ page }) => {
    await registerAndVerify(page, "upload");
    await page.getByRole("button", { name: "上传简历", exact: true }).first().click();
    await expect(page.getByRole("heading", { name: "批量上传简历" })).toBeVisible();
    await expect(
      page.getByText(
        "上传后自动入库并进入 AI 处理，完成后可在简历库查看、筛选、评分和匹配岗位。",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(page.getByText("批量处理路径", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "上传后会发生什么" })).toHaveCount(0);
    const uploadGridTracks = await page
      .locator(".upload-workspace .page-layout")
      .evaluate((element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).length);
    expect(uploadGridTracks).toBe(1);

    await page.locator('input[type="file"]').setInputFiles({
      name: "e2e-resume.pdf",
      mimeType: "application/pdf",
      buffer: e2eResumePdf(),
    });
    await expect(page.getByText("e2e-resume.pdf")).toBeVisible();
    await page.getByRole("button", { name: /上传 1 份并自动提取/ }).click();
    await expect(page.getByText("简历已保存，AI 正在提取候选人姓名和结构化事实。")).toBeVisible();
    await expect(page.getByText("原件已保存，AI 正在排队提取候选人姓名和结构化事实")).toBeVisible();
    await page.getByRole("button", { name: "查看状态" }).click();
    await expect(page.getByRole("dialog", { name: /简历详情/ })).toBeVisible();

    const autoPreviewGrant = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.status() === 200 &&
        /\/v1\/resumes\/[^/]+\/file-access$/.test(new URL(response.url()).pathname),
    );
    await page.getByRole("tab", { name: "原始文件" }).click();
    await autoPreviewGrant;
    const originalPreview = page.getByTitle("e2e-resume.pdf 原始文件");
    await expect(originalPreview).toBeVisible();
    await expect(originalPreview).toHaveAttribute("src", /^blob:/);
    await expect(page.getByRole("button", { name: "重新加载预览" })).toBeVisible();
  });

  test("简历库可浏览已入库候选人并通过键盘打开详情", async ({ page }) => {
    await registerAndVerify(page, "resume-library");
    await seedWorkspaceFixture(page);
    await page.reload();

    await page.getByRole("button", { name: "简历库", exact: true }).click();
    await expect(page.getByRole("heading", { name: "简历库", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "AI 总结", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "AI 评分", exact: true })).toBeVisible();

    const detailsButton = page.getByRole("button", {
      name: "查看 E2E 推荐候选人 的简历详情",
    });
    await expect(detailsButton).toBeVisible();
    await detailsButton.focus();
    await page.keyboard.press("Enter");
    const drawer = page.getByRole("dialog", { name: "E2E 推荐候选人 的简历详情" });
    await expect(drawer).toBeVisible();

    const summaryTab = drawer.getByRole("tab", { name: "AI 总结" });
    await summaryTab.click();
    await expect(summaryTab).toHaveAttribute("aria-selected", "true");
    await expect(summaryTab).toHaveAttribute(
      "aria-controls",
      "candidate-drawer-panel-summary",
    );
    const summaryPanel = drawer.getByRole("tabpanel");
    await expect(summaryPanel).toHaveAttribute(
      "aria-labelledby",
      "candidate-drawer-tab-summary",
    );
    await expect(
      summaryPanel.getByRole("heading", { name: "还没有 AI 总结" }),
    ).toBeVisible();

    const evidenceTab = drawer.getByRole("tab", { name: "提取依据" });
    await evidenceTab.click();
    await expect(evidenceTab).toHaveAttribute("aria-selected", "true");
    const evidencePanel = drawer.getByRole("tabpanel");
    await expect(
      evidencePanel.getByRole("heading", { name: "已提取的简历事实" }),
    ).toBeVisible();
    await expect(
      evidencePanel.getByRole("heading", { name: "原文证据块" }),
    ).toBeVisible();
    await expect(evidencePanel.getByText("Python · 原文依据：page-001")).toBeVisible();
  });

  test("简历库窄屏保留查看入口且表格只在自身区域横向滚动", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await registerAndVerify(page, "resume-library-narrow");
    await seedWorkspaceFixture(page);
    await page.reload();

    await page.getByRole("button", { name: "简历库", exact: true }).click();
    const tableScroll = page.locator(".resume-library-page .table-scroll");
    await expect(tableScroll).toBeVisible();
    await expect.poll(() => page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    )).toBe(true);

    const detailsButton = page.getByRole("button", {
      name: "查看 E2E 推荐候选人 的简历详情",
    });
    await detailsButton.click();
    await expect(page.getByRole("dialog", { name: "E2E 推荐候选人 的简历详情" })).toBeVisible();
  });

  test("岗位原样发布只提交原始输入，不调用 AI", async ({ page }) => {
    await registerAndVerify(page, "publish-original-job");
    await page.getByRole("button", { name: "招聘详情", exact: true }).click();
    await expect(page.getByRole("heading", { name: "招聘详情", exact: true })).toBeVisible();

    const title = "E2E 原样发布岗位";
    const originalJd = "负责招聘工作台内测。\n\n任职要求：能独立完成招聘流程。";
    await page.locator("#job-title").fill(title);
    await page.locator("#job-brief").fill(originalJd);

    const originalPublishButton = page.getByRole("button", { name: "原样发布 JD" });
    await expect(originalPublishButton).toBeVisible();
    await expect(page.getByRole("button", { name: "AI 生成 JD" })).toBeVisible();

    let generateRequests = 0;
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/v1/jobs/generate-jd"
      ) {
        generateRequests += 1;
      }
    });
    const publishRequest = page.waitForRequest((request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/v1/jobs/publish-original",
    );

    await originalPublishButton.click();
    const request = await publishRequest;
    expect(request.postDataJSON()).toEqual({
      jd_text: originalJd,
      title,
    });
    await expect.poll(() => generateRequests).toBe(0);
    await expect(page.getByText("原版已发布", { exact: true })).toBeVisible();
  });

  test("初筛支持院校、学历与经历范围，评分与 JD 仍按全量批处理", async ({ page }) => {
    await registerAndVerify(page, "screen-score-match");
    const fixture = await seedWorkspaceFixture(page);
    await page.reload();

    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();
    await expect(
      page.getByText("E2E 评分规则 · v1", { exact: true }),
    ).toBeVisible();
    const basicFilters = page.getByRole("complementary", { name: "初筛条件" });
    const institutionGroup = basicFilters.getByRole("group", { name: "院校等级条件" });
    const degreeGroup = basicFilters.getByRole("group", { name: "最高学历条件" });
    const experienceGroup = basicFilters.getByRole("group", { name: "经历类型条件" });
    const formalExperienceRange = basicFilters.locator("#min-experience");
    const workInternshipRange = basicFilters.locator("#min-work-internship");
    await expect(basicFilters).toBeVisible();
    await expect(page.locator("details.filter-match-rules")).toHaveCount(0);
    await expect(page.locator("#saved-filter")).toHaveCount(0);
    await expect(page.locator("#school-name")).toHaveCount(0);
    await expect(page.locator("#filter-rule-language")).toHaveCount(0);
    await expect(basicFilters.getByRole("heading", { name: "英语能力", exact: true })).toHaveCount(0);
    await expect(basicFilters.getByRole("heading", { name: "技能", exact: true })).toHaveCount(0);
    await expect(basicFilters.getByRole("heading", { name: "关键词", exact: true })).toHaveCount(0);
    await expect(basicFilters.locator("select")).toHaveCount(0);
    await expect(institutionGroup.getByRole("checkbox")).toHaveCount(6);
    for (const label of ["985", "211", "本科", "大专", "中专", "海外院校"]) {
      await expect(institutionGroup.getByRole("checkbox", { name: label })).toBeVisible();
    }
    await expect(degreeGroup.getByRole("checkbox")).toHaveCount(6);
    await expect(degreeGroup.getByRole("checkbox", { name: "本科" })).toBeVisible();
    await expect(experienceGroup.getByRole("checkbox")).toHaveCount(2);
    await expect(experienceGroup.getByRole("checkbox", { name: "正式工作" })).toBeVisible();
    await expect(experienceGroup.getByRole("checkbox", { name: "实习" })).toBeVisible();
    for (const range of [formalExperienceRange, workInternshipRange]) {
      await expect(range).toHaveAttribute("type", "range");
      await expect(range).toHaveAttribute("min", "0");
      await expect(range).toHaveAttribute("max", "240");
      await expect(range).toHaveAttribute("step", "12");
    }

    const fullInitialFilterRequest = (response: import("@playwright/test").Response) => {
      if (
        response.request().method() !== "POST"
        || new URL(response.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      const request = response.request().postDataJSON() as {
        education_any_of?: Array<{
          institution_classifications_any_of?: string[];
        }>;
        highest_degree_in?: string[];
        min_employment_months?: number;
        min_employment_or_internship_months?: number;
        experience_any_of?: Array<{ experience_types?: string[] }>;
      };
      const experienceTypes = request.experience_any_of?.[0]?.experience_types ?? [];
      return Boolean(
        request.education_any_of?.[0]?.institution_classifications_any_of?.includes("985")
        && request.highest_degree_in?.includes("bachelor")
        && request.min_employment_months === 36
        && request.min_employment_or_internship_months === 48
        && experienceTypes.includes("employment")
        && experienceTypes.includes("internship"),
      );
    };
    const increaseRange = async (
      range: typeof formalExperienceRange,
      steps: number,
    ) => {
      await range.focus();
      for (let index = 0; index < steps; index += 1) {
        await range.press("ArrowRight");
      }
    };

    const institution985 = institutionGroup.getByRole("checkbox", { name: "985" });
    await institution985.check();
    await degreeGroup.getByRole("checkbox", { name: "本科" }).check();
    const formalRangeSearch = page.waitForResponse((response) => {
      if (response.request().method() !== "POST") return false;
      const request = response.request().postDataJSON() as {
        min_employment_months?: number;
      };
      return new URL(response.url()).pathname === "/v1/candidates/search"
        && request.min_employment_months === 36;
    });
    await increaseRange(formalExperienceRange, 3);
    await formalRangeSearch;
    const combinedRangeSearch = page.waitForResponse((response) => {
      if (response.request().method() !== "POST") return false;
      const request = response.request().postDataJSON() as {
        min_employment_months?: number;
        min_employment_or_internship_months?: number;
      };
      return new URL(response.url()).pathname === "/v1/candidates/search"
        && request.min_employment_months === 36
        && request.min_employment_or_internship_months === 48;
    });
    await increaseRange(workInternshipRange, 4);
    await combinedRangeSearch;
    await experienceGroup.getByRole("checkbox", { name: "正式工作" }).check();
    const completeInitialSearch = page.waitForResponse(fullInitialFilterRequest);
    await experienceGroup.getByRole("checkbox", { name: "实习" }).check();
    await completeInitialSearch;
    await expect(institution985).toBeChecked();
    const appliedFilterBar = page.getByLabel("已应用的筛选条件");
    await expect(appliedFilterBar).toContainText("院校：985");
    await expect(appliedFilterBar).toContainText("最高学历：本科");
    await expect(appliedFilterBar).toContainText("正式工作：至少 3 年");
    await expect(appliedFilterBar).toContainText("工作 + 实习：至少 4 年");

    const resetSearch = page.waitForResponse((response) => {
      if (response.request().method() !== "POST") return false;
      const request = response.request().postDataJSON() as Record<string, unknown>;
      return new URL(response.url()).pathname === "/v1/candidates/search"
        && !request.education_any_of
        && !request.highest_degree_in
        && !request.min_employment_months
        && !request.min_employment_or_internship_months
        && !request.experience_any_of;
    });
    await basicFilters.getByRole("button", { name: "清空", exact: true }).click();
    await resetSearch;
    await expect(appliedFilterBar).toHaveCount(0);
    await expect(page.getByText("E2E 推荐候选人")).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "学历 / 院校", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "经历", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "核心技能", exact: true })).toBeVisible();
    await expect(page.getByText("e2e-fixture-1.pdf", { exact: true })).toHaveCount(0);
    await expect(page.getByText("待核实", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("未设门槛")).toHaveCount(0);
    await expect(page.getByText("当前已加载", { exact: false })).toHaveCount(0);
    await expect(page.getByText("评分口径", { exact: true })).toHaveCount(0);

    const candidateTableFillsResultsPane = await page
      .locator(".filter-workspace .candidate-table")
      .evaluate((table) => {
        const scrollRegion = table.parentElement;
        return Boolean(
          scrollRegion &&
            table.getBoundingClientRect().width >= scrollRegion.clientWidth - 1,
        );
      });
    expect(candidateTableFillsResultsPane).toBeTruthy();

    await page.getByRole("button", { name: "查看 E2E 推荐候选人 的评分详情" }).click();
    const drawer = page.getByRole("dialog", { name: "E2E 推荐候选人 的简历详情" });
    await expect(drawer.getByRole("tab", { name: "评分详情" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(drawer.getByRole("heading", { name: "E2E 评分规则", exact: true })).toBeVisible();
    await expect(drawer.getByText("AI 判断", { exact: true }).first()).toBeVisible();
    await expect(drawer.getByText("简历事实", { exact: true }).first()).toBeVisible();
    await expect(drawer.getByText("待确认项", { exact: true })).toBeVisible();
    await expect(
      drawer.locator(".drawer-title-wrap").getByText("e2e-fixture-1.pdf", { exact: true }),
    ).toHaveCount(0);
    await drawer.getByRole("button", { name: "关闭简历详情" }).click();
    await expect(drawer).toBeHidden();

    await page.getByRole("button", { name: "评分模板", exact: true }).click();
    await expect(page.getByRole("heading", { name: "通用评分模板", exact: true })).toBeVisible();
    await expect(page.getByText("在这里维护维度和权重", { exact: false })).toHaveCount(0);
    await expect(page.locator("#main-content").getByText(/当前简历：|尚未选择简历/)).toHaveCount(0);
    await expect(page.locator("#main-content").getByRole("button", { name: /生成当前候选人评分/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /通用候选人初筛/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /技术岗位初筛/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /销售与业务岗位初筛/ })).toBeVisible();
    await expect(page.locator('input[id^="dimension-key-"]')).toHaveCount(0);
    await expect(page.getByLabel("评分维度", { exact: true })).toHaveCount(3);
    await expect(page.getByLabel("权重（%）", { exact: true })).toHaveCount(3);
    await expect(page.getByLabel("AI 评分说明（可选）", { exact: true })).toHaveCount(3);
    await page.locator("#template-name").fill("E2E 批量评分规则");
    const createTemplateRequest = page.waitForRequest((request) => {
      const { pathname } = new URL(request.url());
      return request.method() === "POST" && pathname === "/v1/score-templates";
    });
    await page.getByRole("button", { name: "创建评分模板" }).click();
    const createdTemplateRequest = await createTemplateRequest;
    const createPayload = createdTemplateRequest.postDataJSON() as {
      dimensions: Array<Record<string, unknown>>;
    };
    expect(createPayload.dimensions).toHaveLength(3);
    expect(createPayload.dimensions.every((dimension) => !("key" in dimension))).toBeTruthy();
    await expect(page.getByText("评分模板“E2E 批量评分规则”已创建。")).toBeVisible();
    const genericScoreRequest = page.waitForRequest((request) => {
      const { pathname } = new URL(request.url());
      return request.method() === "POST" && /^\/v1\/score-templates\/[^/]+\/score-all$/.test(pathname);
    });
    await page.getByRole("button", { name: "生成全部简历的通用评分" }).click();
    await genericScoreRequest;
    await expect(page.getByRole("heading", { name: "批量评分任务" })).toBeVisible();

    await page.getByRole("button", { name: "招聘详情", exact: true }).click();
    await expect(page.getByRole("heading", { name: "招聘详情", exact: true })).toBeVisible();
    await expect(page.locator("#main-content").getByText("当前候选人", { exact: true })).toHaveCount(0);
    await expect(page.locator("#main-content").getByRole("button", { name: "运行岗位匹配" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "候选人评估结果" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "推荐候选人" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "待核实候选人" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "明确不匹配" })).toBeVisible();
    await expect(page.getByText("E2E 推荐候选人", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("E2E 待核实候选人", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("E2E 不匹配候选人", { exact: true }).first()).toBeVisible();

    const forbiddenCandidateRequests: string[] = [];
    const observeCandidateRequests = (request: import("@playwright/test").Request) => {
      const { pathname } = new URL(request.url());
      if (
        request.method() === "POST" &&
        /^\/v1\/resumes\/[^/]+\/(scores|job-matches)$/.test(pathname)
      ) {
        forbiddenCandidateRequests.push(pathname);
      }
    };
    page.on("request", observeCandidateRequests);
    try {
      const jobBatchResponse = page.waitForResponse((response) => {
        const { pathname } = new URL(response.url());
        return response.request().method() === "POST" &&
          pathname === `/v1/job-versions/${fixture.job_version_id}/match-all`;
      });
      await page.getByRole("button", { name: "开始岗位评分（全部可匹配简历）" }).click();
      const response = await jobBatchResponse;
      expect(response.ok()).toBeTruthy();
      const batch = await response.json() as { job_version_id: string };
      expect(batch.job_version_id).toBe(fixture.job_version_id);
      expect(forbiddenCandidateRequests).toEqual([]);
    } finally {
      page.off("request", observeCandidateRequests);
    }
  });

  test("初筛结果按总数交给 Agent，且浏览器不会提交候选人或覆盖冻结范围", async ({ page }) => {
    await registerAndVerify(page, "first-pass-agent-scope");
    await seedWorkspaceFixture(page);
    await page.reload();
    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();

    const isCompleteFirstPass = (request: import("@playwright/test").Request) => {
      if (
        request.method() !== "POST"
        || new URL(request.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      const body = request.postDataJSON() as {
        education_any_of?: Array<{
          institution_classifications_any_of?: string[];
        }>;
        highest_degree_in?: string[];
        min_employment_months?: number;
        min_employment_or_internship_months?: number;
        experience_any_of?: Array<{ experience_types?: string[] }>;
      };
      const experienceTypes = body.experience_any_of?.[0]?.experience_types ?? [];
      return Boolean(
        body.min_employment_months === 36
        && body.min_employment_or_internship_months === 48
        && body.education_any_of?.some((condition) =>
          condition.institution_classifications_any_of?.includes("985"),
        )
        && body.highest_degree_in?.includes("bachelor")
        && experienceTypes.includes("employment")
        && experienceTypes.includes("internship"),
      );
    };

    let replacedResultPage = false;
    await page.route("**/v1/candidates/search", async (route) => {
      if (!isCompleteFirstPass(route.request()) || replacedResultPage) {
        await route.continue();
        return;
      }
      replacedResultPage = true;
      // A page can display one or zero rows while the current filter matches
      // many more records. The action must use the response total_count.
      await route.fulfill({
        json: {
          items: [],
          next_cursor: null,
          needs_review_count: 0,
          total_count: 17,
        },
      });
    });

    const basicFilters = page.getByRole("complementary", { name: "初筛条件" });
    const institutionGroup = basicFilters.getByRole("group", { name: "院校等级条件" });
    const degreeGroup = basicFilters.getByRole("group", { name: "最高学历条件" });
    const experienceGroup = basicFilters.getByRole("group", { name: "经历类型条件" });
    const formalExperienceRange = basicFilters.locator("#min-experience");
    const workInternshipRange = basicFilters.locator("#min-work-internship");
    const searchFor985 = page.waitForResponse((response) => {
      const request = response.request();
      if (
        request.method() !== "POST"
        || new URL(request.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      const body = request.postDataJSON() as {
        education_any_of?: Array<{
          institution_classifications_any_of?: string[];
        }>;
      };
      return body.education_any_of?.some((condition) =>
        condition.institution_classifications_any_of?.includes("985"),
      ) ?? false;
    });
    await institutionGroup.getByRole("checkbox", { name: "985" }).check();
    await searchFor985;
    await degreeGroup.getByRole("checkbox", { name: "本科" }).check();

    const searchForFormalWork = page.waitForResponse((response) => {
      const request = response.request();
      if (
        request.method() !== "POST"
        || new URL(request.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      return (request.postDataJSON() as { min_employment_months?: number })
        .min_employment_months === 36;
    });
    await formalExperienceRange.focus();
    await formalExperienceRange.press("ArrowRight");
    await formalExperienceRange.press("ArrowRight");
    await formalExperienceRange.press("ArrowRight");
    await searchForFormalWork;

    const searchForCombinedTenure = page.waitForResponse((response) => {
      const request = response.request();
      if (
        request.method() !== "POST"
        || new URL(request.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      const body = request.postDataJSON() as {
        min_employment_months?: number;
        min_employment_or_internship_months?: number;
      };
      return body.min_employment_months === 36
        && body.min_employment_or_internship_months === 48;
    });
    await workInternshipRange.focus();
    await workInternshipRange.press("ArrowRight");
    await workInternshipRange.press("ArrowRight");
    await workInternshipRange.press("ArrowRight");
    await workInternshipRange.press("ArrowRight");
    await searchForCombinedTenure;
    await experienceGroup.getByRole("checkbox", { name: "正式工作" }).check();
    const firstPassResponse = page.waitForResponse((response) => isCompleteFirstPass(response.request()));
    await experienceGroup.getByRole("checkbox", { name: "实习" }).check();
    await firstPassResponse;

    const filterScopeRequests: Array<Record<string, unknown>> = [];
    let agentTurnRequestCount = 0;
    const observeAgentTurns = (request: import("@playwright/test").Request) => {
      if (
        request.method() === "POST"
        && new URL(request.url()).pathname === "/v1/recruiting-agent/turns"
      ) {
        agentTurnRequestCount += 1;
      }
    };
    page.on("request", observeAgentTurns);
    await page.route("**/v1/recruiting-agent/conversations/filter-scope", async (route) => {
      filterScopeRequests.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        json: {
          conversation_id: "e2e-first-pass-scope",
          context_version: 1,
          active_context: {
            candidate_set_source: "candidate_filter",
            candidate_count: 17,
            active_job_version_id: null,
            active_job_title: null,
            active_talent_profile: null,
            expires_at: "2026-07-28T10:00:00Z",
          },
        },
      });
    });

    try {
      const refineAction = page.getByRole("button", { name: /交给 Agent 精筛当前 17/ });
      await expect(refineAction).toContainText("交给 Agent 精筛当前 17 人");
      await refineAction.click();

      const dialog = page.getByRole("dialog", { name: "招聘助手" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByText("初筛结果 · 17 位候选人", { exact: true })).toBeVisible();
      expect(filterScopeRequests).toHaveLength(1);
      const scopePayload = filterScopeRequests[0];
      expect(scopePayload).toMatchObject({
        filter: {
          schema_version: "candidate_filter.v2",
          education_any_of: [{ institution_classifications_any_of: ["985"] }],
          highest_degree_in: ["bachelor"],
          min_employment_months: 36,
          min_employment_or_internship_months: 48,
          experience_any_of: [{ experience_types: ["employment", "internship"] }],
        },
      });
      const scopeFilter = scopePayload.filter as Record<string, unknown>;
      expect(Object.keys(scopeFilter).sort()).toEqual([
        "education_any_of",
        "experience_any_of",
        "highest_degree_in",
        "min_employment_months",
        "min_employment_or_internship_months",
        "schema_version",
      ]);
      expect(JSON.stringify(scopePayload)).not.toMatch(
        /candidate_id|candidate_ids|resume_id|resume_ids|cursor|limit|score_template_id/,
      );
      expect(agentTurnRequestCount).toBe(0);

      // Altering the form after handoff must not replace the existing
      // server-side scope. It stays usable when the drawer is reopened.
      await dialog.getByRole("button", { name: "关闭招聘助手" }).click();
      const changedFilterResponse = page.waitForResponse((response) => {
        const request = response.request();
        return request.method() === "POST"
          && new URL(request.url()).pathname === "/v1/candidates/search"
          && (request.postDataJSON() as { min_employment_months?: number })
            .min_employment_months === 60;
      });
      await formalExperienceRange.focus();
      await formalExperienceRange.press("ArrowRight");
      await formalExperienceRange.press("ArrowRight");
      await changedFilterResponse;
      await page.getByRole("button", { name: "招聘助手", exact: true }).click();
      await expect(dialog.getByText("初筛结果 · 17 位候选人", { exact: true })).toBeVisible();
      expect(filterScopeRequests).toHaveLength(1);
      expect(agentTurnRequestCount).toBe(0);
    } finally {
      page.off("request", observeAgentTurns);
    }
  });

  test("筛选结果完整显示评分可信度的三档状态", async ({ page }) => {
    await page.route("**/v1/candidates/search", async (route) => {
      await route.fulfill({
        json: {
          items: [
            {
              candidate_id: "e2e-confidence-grounded-candidate",
              display_name: "E2E 高可信度候选人",
              resume_id: "e2e-confidence-grounded-resume",
              original_filename: "e2e-confidence-grounded.pdf",
              is_985_211: false,
              institution_classifications: [],
              highest_degree: "bachelor",
              employment_months: 24,
              employment_or_internship_months: 24,
              education_school: "E2E 大学",
              education_major: "软件工程",
              latest_experience_title: "后端工程师",
              latest_experience_organization: "E2E 公司",
              latest_experience_type: "employment",
              skill_highlights: ["Python"],
              summary_preview: null,
              score_id: "e2e-confidence-grounded-score",
              score_template_id: null,
              score_total: 88,
              score_status: "succeeded",
              score_template_name: null,
              score_confidence: 80,
              display_fields: [],
              matched_filters: [],
              matched_evidence: [],
            },
            {
              candidate_id: "e2e-confidence-partial-candidate",
              display_name: "E2E 部分可信度候选人",
              resume_id: "e2e-confidence-partial-resume",
              original_filename: "e2e-confidence-partial.pdf",
              is_985_211: false,
              institution_classifications: [],
              highest_degree: "bachelor",
              employment_months: 12,
              employment_or_internship_months: 12,
              education_school: "E2E 大学",
              education_major: "计算机科学",
              latest_experience_title: "开发工程师",
              latest_experience_organization: "E2E 公司",
              latest_experience_type: "employment",
              skill_highlights: ["TypeScript"],
              summary_preview: null,
              score_id: "e2e-confidence-partial-score",
              score_template_id: null,
              score_total: 72,
              score_status: "succeeded",
              score_template_name: null,
              score_confidence: 79,
              display_fields: [],
              matched_filters: [],
              matched_evidence: [],
            },
            {
              candidate_id: "e2e-confidence-unknown-candidate",
              display_name: "E2E 待核实候选人",
              resume_id: "e2e-confidence-unknown-resume",
              original_filename: "e2e-confidence-unknown.pdf",
              is_985_211: false,
              institution_classifications: [],
              highest_degree: "bachelor",
              employment_months: 6,
              employment_or_internship_months: 6,
              education_school: "E2E 大学",
              education_major: "信息管理",
              latest_experience_title: "实习生",
              latest_experience_organization: "E2E 公司",
              latest_experience_type: "internship",
              skill_highlights: ["SQL"],
              summary_preview: null,
              score_id: "e2e-confidence-unknown-score",
              score_template_id: null,
              score_total: 64,
              score_status: "succeeded",
              score_template_name: null,
              score_confidence: null,
              display_fields: [],
              matched_filters: [],
              matched_evidence: [],
            },
          ],
          next_cursor: null,
          needs_review_count: 1,
          total_count: 3,
        },
      });
    });

    await registerAndVerify(page, "score-confidence");
    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();

    const grounded = page.locator("tr", { hasText: "E2E 高可信度候选人" });
    await expect(grounded.locator(".score-confidence")).toHaveText("可信度 80%");
    await expect(grounded.locator(".score-confidence")).toHaveClass(/is-grounded/);

    const partial = page.locator("tr", { hasText: "E2E 部分可信度候选人" });
    await expect(partial.locator(".score-confidence")).toHaveText("可信度 79%");
    await expect(partial.locator(".score-confidence")).toHaveClass(/is-partial/);

    const unknown = page.locator("tr", { hasText: "E2E 待核实候选人" });
    await expect(unknown.locator(".score-confidence")).toHaveText("待核实");
    await expect(unknown.locator(".score-confidence")).toHaveClass(/is-unknown/);
  });

  test("联系方式只在受保护的简历详情中展示并可复制", async ({ page, context }) => {
    await registerAndVerify(page, "contact-details");
    await seedWorkspaceFixture(page);
    await page.reload();

    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();
    await expect(page.getByText("e2e-contact@example.test", { exact: true })).toHaveCount(0);
    await page
      .getByRole("complementary", { name: "初筛条件" })
      .getByRole("checkbox", { name: "985" })
      .check();
    await expect(
      page.getByRole("button", { name: "查看 E2E 推荐候选人 的评分详情" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "查看 E2E 推荐候选人 的评分详情" })
      .click();

    const drawer = page.getByRole("dialog", { name: "E2E 推荐候选人 的简历详情" });
    await expect(drawer.getByRole("heading", { name: "联系方式", exact: true })).toBeVisible();
    await expect(drawer.getByText("13800000000", { exact: true })).toBeVisible();
    await expect(drawer.getByText("e2e-contact@example.test", { exact: true })).toBeVisible();
    await expect(
      drawer.getByText("仅从简历原文提取，不参与筛选、评分、JD 匹配或招聘助手。", {
        exact: true,
      }),
    ).toBeVisible();

    await context.grantPermissions(["clipboard-read", "clipboard-write"], {
      origin: new URL(page.url()).origin,
    });
    await drawer.getByRole("button", { name: "复制电话" }).click();
    await expect(page.getByText("电话已复制。", { exact: true })).toBeVisible();
  });

  test("简历详情只保留直接删除当前简历的入口", async ({ page }) => {
    await registerAndVerify(page, "simple-resume-delete");
    await seedWorkspaceFixture(page);

    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();
    await page
      .getByRole("complementary", { name: "初筛条件" })
      .getByRole("checkbox", { name: "985" })
      .check();
    await expect(page.getByRole("button", { name: "查看 E2E 推荐候选人 的评分详情" })).toBeVisible();
    await page.getByRole("button", { name: "查看 E2E 推荐候选人 的评分详情" }).click();

    const drawer = page.getByRole("dialog", { name: "E2E 推荐候选人 的简历详情" });
    const deleteButton = drawer.getByRole("button", { name: "删除简历", exact: true });
    await expect(deleteButton).toBeVisible();
    await expect(drawer.getByText("候选人数据管理", { exact: true })).toHaveCount(0);
    await expect(drawer.getByText("导出候选人资料", { exact: true })).toHaveCount(0);

    const deleteRequest = page.waitForRequest((request) => {
      const { pathname } = new URL(request.url());
      return request.method() === "DELETE" && /^\/v1\/resumes\/[^/]+$/.test(pathname);
    });
    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("删除当前简历");
      await dialog.accept();
    });
    await deleteButton.click();

    const request = await deleteRequest;
    expect(request.postDataJSON()).toEqual({
      reason: "other",
      other_note: "simple_resume_delete",
    });
    await expect(page.getByText(/当前简历版本已移出工作台/)).toBeVisible();
    await expect(drawer).toBeHidden();
  });

  test("窄屏仍可展开筛选条件并自动应用", async ({ page }) => {
    await registerAndVerify(page, "mobile-filter");
    await page.setViewportSize({ width: 390, height: 844 });

    await page.getByRole("button", { name: "筛选工作台", exact: true }).click();
    const filters = page.getByRole("complementary", { name: "初筛条件" });
    const toggle = page.getByRole("button", { name: "展开", exact: true });
    await expect(toggle).toBeVisible();
    await expect(filters.locator("#min-experience")).not.toBeVisible();

    await toggle.click();
    await expect(filters.locator("#min-experience")).toBeVisible();
    const searchFor985 = page.waitForResponse((response) => {
      if (
        response.request().method() !== "POST"
        || new URL(response.url()).pathname !== "/v1/candidates/search"
      ) {
        return false;
      }
      const request = response.request().postDataJSON() as {
        education_any_of?: Array<{
          institution_classifications_any_of?: string[];
        }>;
      };
      return request.education_any_of?.some((condition) =>
        condition.institution_classifications_any_of?.includes("985"),
      ) ?? false;
    });
    await filters.getByRole("checkbox", { name: "985" }).check();
    await searchFor985;
    await expect(page.getByRole("button", { name: "应用筛选条件" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "收起", exact: true })).toBeVisible();
  });

  test("招聘助手打开后聚焦关闭键，关闭后返回触发按钮", async ({ page }) => {
    await registerAndVerify(page, "agent-focus");

    const trigger = page.getByRole("button", { name: "招聘助手", exact: true });
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveClass(/semi-button-primary/);
    await expect(trigger).toHaveCSS("background-color", "rgb(215, 22, 24)");
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    const closeButton = dialog.getByRole("button", { name: "关闭招聘助手" });
    await expect(dialog).toBeVisible();
    await expect(closeButton).toBeFocused();
    await expect(dialog.getByText(/当前候选人：|未选择候选人/)).toHaveCount(0);
    await expect(dialog.getByLabel("常用提问")).toHaveCount(0);
    await expect(dialog.getByLabel("向招聘助手提问")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
  });

  test("窄屏保留招聘助手入口", async ({ page }) => {
    await registerAndVerify(page, "agent-mobile-entry");
    await page.setViewportSize({ width: 390, height: 844 });

    const trigger = page.getByRole("button", { name: "招聘助手", exact: true });
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveClass(/semi-button-primary/);
    await expect(trigger).toHaveCSS("background-color", "rgb(215, 22, 24)");
    await trigger.click();
    await expect(page.getByRole("dialog", { name: "招聘助手" })).toBeVisible();
  });

  test("招聘助手输入框使用 Enter 发送并保留 Shift 加 Enter 换行", async ({ page }) => {
    await registerAndVerify(page, "agent-composer-keyboard");
    const turnRequests: Array<Record<string, unknown>> = [];
    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      turnRequests.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        json: {
          conversation_id: "e2e-agent-composer-conversation",
          context_version: 1,
          active_context: {
            candidate_set_source: null,
            candidate_count: 0,
            active_job_version_id: null,
            active_job_title: null,
            active_talent_profile: null,
            expires_at: "2026-07-28T10:00:00Z",
          },
          message: "已收到多行请求。",
          intent: "help",
          job_version_id: null,
          candidates: [],
          actions: [],
          tool_trace: [],
          search_summary: null,
          batch_id: null,
        },
      });
    });

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    const composer = dialog.getByLabel("向招聘助手提问");
    await composer.fill("第一行");
    await composer.dispatchEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
      isComposing: true,
    });
    await expect(composer).toHaveValue("第一行");
    expect(turnRequests).toHaveLength(0);

    // Some Windows IMEs keep the legacy keyCode at 229 for a normal Enter
    // after composition has already ended. The browser's isComposing flag is
    // the authoritative signal, so this must still send.
    await composer.dispatchEvent("keydown", {
      key: "Enter",
      keyCode: 229,
      bubbles: true,
      cancelable: true,
      isComposing: false,
    });
    await expect.poll(() => turnRequests.length).toBe(1);
    expect(turnRequests[0]).toEqual({
      message: "第一行",
      job_version_id: null,
    });
    await expect(composer).toHaveValue("");

    await composer.fill("第一行");
    await composer.press("Shift+Enter");
    await composer.type("第二行");
    await expect(composer).toHaveValue("第一行\n第二行");
    expect(turnRequests).toHaveLength(1);

    await composer.press("Enter");
    await expect.poll(() => turnRequests.length).toBe(2);
    expect(turnRequests[1]).toMatchObject({
      message: "第一行\n第二行",
      job_version_id: null,
    });
    await expect(composer).toHaveValue("");
    await expect(dialog.getByText("已收到多行请求。")).toHaveCount(2);
  });

  test("招聘助手错误说明 AI 服务，并在重发时不重复用户消息", async ({ page }) => {
    await registerAndVerify(page, "agent-retry");
    let attempts = 0;
    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({
          contentType: "application/json",
          status: 503,
          body: JSON.stringify({ detail: "agent_model_unavailable" }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          conversation_id: "e2e-agent-retry-conversation",
          context_version: 1,
          active_context: {
            candidate_set_source: null,
            candidate_count: 0,
            active_job_version_id: null,
            active_job_title: null,
            expires_at: "2026-07-27T10:00:00Z",
          },
          message: "已重新连接 AI 服务，并完成本次检索。",
          intent: "help",
          job_version_id: null,
          candidates: [],
          actions: [],
          tool_trace: [],
          search_summary: null,
          batch_id: null,
        }),
      });
    });

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    const question = "谁最适合这个岗位？";
    await dialog.getByLabel("向招聘助手提问").fill(question);
    await dialog.getByRole("button", { name: "发送提问" }).click();

    await expect(
      dialog.getByText("招聘助手所用 AI 服务暂时不可用，请稍后重试。"),
    ).toBeVisible();
    await expect(dialog.locator(".agent-message.is-user")).toHaveCount(1);
    await expect(dialog.locator(".agent-message.is-error")).toHaveCount(1);

    await dialog.getByRole("button", { name: "重新发送" }).click();

    await expect(dialog.getByText("已重新连接 AI 服务，并完成本次检索。")).toBeVisible();
    await expect(dialog.locator(".agent-message.is-user")).toHaveCount(1);
    await expect(dialog.locator(".agent-message.is-error")).toHaveCount(0);
    expect(attempts).toBe(2);
  });

  test("招聘助手不会为确定性请求错误提供无效重发", async ({ page }) => {
    await registerAndVerify(page, "agent-request-rejected");
    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        status: 503,
        body: JSON.stringify({ detail: "agent_model_request_rejected" }),
      });
    });

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    await dialog.getByLabel("向招聘助手提问").fill("谁最适合这个岗位？");
    await dialog.getByRole("button", { name: "发送提问" }).click();

    await expect(
      dialog.getByText("招聘助手当前配置暂时无法处理这类请求，请联系工作区管理员。"),
    ).toBeVisible();
    await expect(dialog.getByRole("button", { name: "重新发送" })).toHaveCount(0);
  });

  test("招聘助手在同一对话中生成画像，并解释零结果的严格召回条件", async ({ page }) => {
    await registerAndVerify(page, "agent-profile-recall");

    const hardFilters = {
      institution_classifications_any_of: [],
      education_degree_in: ["bachelor"],
      highest_degree_in: [],
      graduation_status: "any",
      fresh_graduate_start_month: null,
      fresh_graduate_end_month: null,
      min_employment_months: null,
      min_employment_or_internship_months: null,
      experience_types_all_of: [],
      skills_all_of: ["Python"],
      language_credentials_all_of: [],
    };
    const draftRevision = {
      revision_id: "e2e-profile-revision-1",
      revision_number: 1,
      source: "ai_generated",
      status: "draft",
      title: "LLM 应用工程师",
      summary: "先保留本科毕业候选人，再核验 LangChain 项目实践。",
      hard_filters: hardFilters,
      verification_requirements: [
        {
          key: "langchain_project",
          label: "具备 LangChain 的项目、实习或工作实践",
          evidence_hint: "核验项目职责、实现和结果。",
        },
      ],
      preferred_requirements: [],
      aliases: ["LLM 应用工程师"],
      clarifying_questions: [],
      created_at: "2026-07-24T10:00:00Z",
      confirmed_at: null,
    };
    const draftProfile = {
      profile_id: "e2e-profile",
      source_type: "freeform",
      source_job_version_id: null,
      original_request: "寻找有 LangChain 项目经验的本科毕业工程师",
      status: "draft",
      current_revision: draftRevision,
      created_at: "2026-07-24T10:00:00Z",
      updated_at: "2026-07-24T10:00:00Z",
    };
    const confirmedProfile = {
      ...draftProfile,
      status: "confirmed",
      current_revision: {
        ...draftRevision,
        status: "confirmed",
        confirmed_at: "2026-07-24T10:01:00Z",
      },
      updated_at: "2026-07-24T10:01:00Z",
    };
    const refinedHardFilters = {
      ...hardFilters,
      institution_classifications_any_of: ["985"],
      min_employment_months: 60,
    };
    const refinedProfile = {
      ...draftProfile,
      current_revision: {
        ...draftRevision,
        revision_id: "e2e-profile-revision-2",
        revision_number: 2,
        source: "ai_refined",
        hard_filters: refinedHardFilters,
      },
      updated_at: "2026-07-24T10:00:30Z",
    };
    const condensedProfile = {
      ...refinedProfile,
      status: "draft",
      current_revision: {
        ...refinedProfile.current_revision,
        revision_id: "e2e-profile-revision-3",
        revision_number: 3,
        summary: "保留本科、Python 与真实 Agent 项目交付要求，重点核验项目职责。",
        verification_requirements: [
          {
            ...refinedProfile.current_revision.verification_requirements[0],
            label: "具备真实的 Agent 项目交付经历",
            evidence_hint: "核验项目中的职责、实现内容和交付结果。",
          },
        ],
      },
      updated_at: "2026-07-24T10:03:00Z",
    };
    const activeProfileContext = (profile: typeof draftProfile) => ({
      profile_id: profile.profile_id,
      revision_id: profile.current_revision.revision_id,
      revision_number: profile.current_revision.revision_number,
      title: profile.current_revision.title,
      status: profile.status,
    });
    let agentTurnCount = 0;

    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      agentTurnCount += 1;
      const body = route.request().postDataJSON();
      if (agentTurnCount === 1) {
        expect(body).toEqual({
          message: "寻找有 LangChain 项目经验的本科毕业工程师",
          job_version_id: null,
        });
        await route.fulfill({
          json: {
            conversation_id: "e2e-profile-agent-context",
            context_version: 2,
            active_context: {
              candidate_set_source: null,
              candidate_count: 0,
              active_job_version_id: null,
              active_job_title: null,
              active_talent_profile: activeProfileContext(draftProfile),
              expires_at: "2026-07-25T10:00:00Z",
            },
            message: "这是我整理的找人条件。确认前不会筛选、评分或匹配候选人。",
            intent: "draft_talent_search_profile",
            job_version_id: null,
            candidates: [],
            actions: [],
            tool_trace: [{ tool: "人才画像草案", summary: "已整理人才画像草案，尚未执行候选人筛选或评分" }],
            search_summary: null,
            batch_id: null,
            talent_profile: draftProfile,
          },
        });
        return;
      }
      expect(agentTurnCount).toBe(2);
      expect(body).toEqual({
        message: "再加 985，正式工作年限改成 5 年",
        job_version_id: null,
        conversation_id: "e2e-profile-agent-context",
        context_version: 2,
      });
      expect(JSON.stringify(body)).not.toContain("profile_id");
      await route.fulfill({
        json: {
          conversation_id: "e2e-profile-agent-context",
          context_version: 3,
          active_context: {
            candidate_set_source: null,
            candidate_count: 0,
            active_job_version_id: null,
            active_job_title: null,
            active_talent_profile: activeProfileContext(refinedProfile),
            expires_at: "2026-07-25T10:00:00Z",
          },
          message: "这是更新后的找人条件。确认前不会筛选、评分或匹配候选人。",
          intent: "refine_active_talent_search_profile",
          job_version_id: null,
          candidates: [],
          actions: [],
          tool_trace: [{ tool: "人才画像草案", summary: "已根据补充条件更新人才画像草案，尚未执行候选人筛选或评分" }],
          search_summary: null,
          batch_id: null,
          talent_profile: refinedProfile,
        },
      });
    });

    await page.route(/\/v1\/talent-search-profiles\/e2e-profile\/confirm$/, async (route) => {
      expect(route.request().postDataJSON()).toEqual({ revision_id: "e2e-profile-revision-2" });
      await route.fulfill({
        json: {
          ...confirmedProfile,
          current_revision: {
            ...refinedProfile.current_revision,
            status: "confirmed",
            confirmed_at: "2026-07-24T10:01:00Z",
          },
        },
      });
    });
    await page.route(/\/v1\/talent-search-profiles\/e2e-profile\/refine$/, async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        revision_id: "e2e-profile-revision-2",
        message: "请精简当前人才画像：保留原始招聘目标和已明确硬条件；合并重复内容，删除模糊或非必要的要求；不要新增或放宽任何条件；将摘要和核验重点写得更短。",
      });
      await route.fulfill({ json: condensedProfile });
    });
    await page.route(/\/v1\/talent-search-profiles\/e2e-profile\/runs$/, async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({
        revision_id: "e2e-profile-revision-2",
      });
      await route.fulfill({
        json: {
          run_id: "e2e-profile-run",
          profile_id: "e2e-profile",
          revision_id: "e2e-profile-revision-1",
          status: "completed",
          result_mode: "hard_filter_recall",
          total_recalled_count: 0,
          job_match_batch_id: null,
          match_total_count: 0,
          match_completed_count: 0,
          match_failed_count: 0,
          match_results: [],
          created_at: "2026-07-24T10:02:00Z",
          updated_at: "2026-07-24T10:02:00Z",
          applied_hard_filters: hardFilters,
          recall_diagnostics: {
            eligible_resume_count: 4,
            needs_review_count: 1,
            strict_match_count: 0,
            steps: [
              {
                key: "education_degree_in",
                label: "教育经历：含本科（任一）",
                remaining_count: 3,
                removed_count: 1,
              },
              {
                key: "skills_all_of",
                label: "精确技能：Python（全部）",
                remaining_count: 0,
                removed_count: 3,
              },
            ],
          },
          candidate_recall: {
            items: [],
            next_cursor: null,
            needs_review_count: 1,
            total_count: 0,
          },
        },
      });
    });
    let profileContextBindCount = 0;
    await page.route("**/v1/recruiting-agent/conversations/context", async (route) => {
      const body = route.request().postDataJSON();
      const isProfileBind = body.context_ref.kind === "talent_search_profile";
      if (isProfileBind) profileContextBindCount += 1;
      const expectedProfileRevisionId = profileContextBindCount === 1
        ? "e2e-profile-revision-2"
        : "e2e-profile-revision-3";
      const activeProfile = profileContextBindCount === 1
        ? { ...refinedProfile, status: "confirmed" }
        : condensedProfile;
      expect(body).toMatchObject(
        isProfileBind
          ? {
            context_ref: {
              kind: "talent_search_profile",
              profile_id: "e2e-profile",
              revision_id: expectedProfileRevisionId,
            },
            conversation_id: "e2e-profile-agent-context",
            context_version: profileContextBindCount === 1 ? 3 : 5,
          }
          : {
            context_ref: { kind: "talent_search_run", run_id: "e2e-profile-run" },
            conversation_id: "e2e-profile-agent-context",
            context_version: 4,
          },
      );
      await route.fulfill({
        json: {
          conversation_id: "e2e-profile-agent-context",
          context_version: isProfileBind
            ? (profileContextBindCount === 1 ? 4 : 6)
            : 5,
          active_context: {
            candidate_set_source: isProfileBind ? null : "talent_search_run",
            candidate_count: 0,
            active_job_version_id: null,
            active_job_title: null,
            active_talent_profile: activeProfileContext(activeProfile),
            expires_at: "2026-07-25T10:00:00Z",
          },
        },
      });
    });
    await page.route(
      /\/v1\/recruiting-agent\/conversations\/e2e-profile-agent-context$/,
      async (route) => {
        await route.fulfill({
          json: {
            conversation_id: "e2e-profile-agent-context",
            context_version: 2,
            active_context: {
              candidate_set_source: null,
              candidate_count: 0,
              active_job_version_id: null,
              active_job_title: null,
              active_talent_profile: activeProfileContext(draftProfile),
              expires_at: "2026-07-25T10:00:00Z",
            },
          },
        });
      },
    );
    await page.route(/\/v1\/talent-search-profiles\/e2e-profile$/, async (route) => {
      await route.fulfill({ json: draftProfile });
    });

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    let dialog = page.getByRole("dialog", { name: "招聘助手" });
    await expect(dialog.getByRole("button", { name: "新建人才画像" })).toHaveCount(0);
    await dialog.getByLabel("向招聘助手提问").fill("寻找有 LangChain 项目经验的本科毕业工程师");
    await dialog.getByRole("button", { name: "发送提问" }).click();

    await expect(dialog.getByText("教育经历：含本科（任一）")).toBeVisible();
    await expect(dialog.getByText("具备 LangChain 的项目、实习或工作实践")).toBeVisible();
    // Only the opaque conversation ID survives a reload. The drawer must
    // recover the safe active-profile reference and then re-fetch the card
    // under the ordinary workspace-scoped profile endpoint.
    await page.reload();
    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "招聘助手" });
    await expect(dialog.getByText("教育经历：含本科（任一）")).toBeVisible();
    await expect(dialog.getByText("具备 LangChain 的项目、实习或工作实践")).toBeVisible();
    await dialog.getByLabel("向招聘助手提问").fill("再加 985，正式工作年限改成 5 年");
    await dialog.getByRole("button", { name: "发送提问" }).click();
    await expect(dialog.getByText("院校类型：985（任一）")).toBeVisible();
    await expect(dialog.getByText("正式工作不少于 5 年")).toBeVisible();

    await dialog.getByRole("button", { name: "确认画像" }).last().click();
    await dialog.getByRole("button", { name: "开始找人" }).last().click();

    await expect(dialog.getByText("没有候选人同时满足本次严格条件").last()).toBeVisible();
    await expect(dialog.getByText("筛掉 3，剩余 0").last()).toBeVisible();
    await expect(dialog.getByRole("button", { name: "调整条件" })).toBeVisible();
    await expect(dialog.getByText("人才画像找人结果 · 0 位候选人")).toBeVisible();
    await dialog.getByRole("button", { name: "精简画像" }).last().click();
    await expect(
      dialog.getByText("已精简人才画像，已生成待确认的第 3 版。"),
    ).toBeVisible();
    await expect(dialog.getByText("版本 3", { exact: true })).toBeVisible();
    await expect(dialog.getByText("具备真实的 Agent 项目交付经历")).toBeVisible();
    await expect(
      dialog.getByText("核验项目中的职责、实现内容和交付结果。"),
    ).toBeVisible();
  });

  test("招聘助手将简历依据和未确认状态以招聘语言展示", async ({ page }) => {
    await registerAndVerify(page, "agent-evidence");
    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          conversation_id: "e2e-agent-evidence-conversation",
          context_version: 2,
          active_context: {
            candidate_set_source: "agent_search",
            candidate_count: 1,
            active_job_version_id: null,
            active_job_title: null,
            expires_at: "2026-07-25T10:00:00Z",
          },
          message: "找到 1 位简历明确提到英语四级的候选人。",
          intent: "search_candidates",
          job_version_id: null,
          candidates: [
            {
              candidate_id: "candidate-e2e-evidence",
              resume_id: "resume-e2e-evidence",
              display_name: "候选人甲",
              detail: "本科 · 工作经历 3 年 0 个月",
              score: null,
              verification_status: "confirmed",
              verification_evidence: [
                {
                  label: "大学英语四级（CET-4）",
                  source: "resume_text",
                },
              ],
            },
          ],
          actions: [],
          tool_trace: [
            {
              tool: "简历筛选",
              summary: "已完成大学英语四级（CET-4）检索：已确认 1 人，未确认 4 份",
            },
          ],
          search_summary: {
            confirmed_count: 1,
            displayed_count: 1,
            unconfirmed_count: 4,
            confirmation_basis: "已确认表示简历明确提及；未确认不代表未通过。",
          },
          batch_id: null,
        }),
      });
    });

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    await dialog.getByLabel("向招聘助手提问").fill("给我找过了英语四级的人");
    await dialog.getByRole("button", { name: "发送提问" }).click();

    await expect(dialog.getByText("检索结果")).toBeVisible();
    await expect(dialog.getByText("已确认", { exact: true }).first()).toBeVisible();
    await expect(dialog.getByText("未确认", { exact: true })).toBeVisible();
    await expect(dialog.getByText("简历原文", { exact: true })).toBeVisible();
    await expect(dialog.getByText("大学英语四级（CET-4）")).toBeVisible();
    await expect(dialog.locator(".agent-tool-trace")).toHaveCount(0);
    await expect(dialog.getByText("language_credentials_any_of", { exact: false })).toHaveCount(0);
    await expect(
      dialog.getByRole("button", { name: "查看候选人甲详情" }),
    ).toBeVisible();
  });

  test("招聘助手恢复安全工作范围，并在后续提问携带最新会话版本", async ({ page }) => {
    await registerAndVerify(page, "agent-context");
    const turnRequests: Array<Record<string, unknown>> = [];
    const context = {
      candidate_set_source: "agent_search",
      candidate_count: 2,
      active_job_version_id: null,
      active_job_title: null,
      expires_at: "2026-07-25T10:00:00Z",
    };
    const chatHistory = [
      {
        context_version: 2,
        user_message: "先筛选符合条件的人",
        assistant_message: "已保存当前筛选范围。",
        created_at: "2026-07-24T10:00:00Z",
      },
      {
        context_version: 3,
        user_message: "在刚才这些人中继续比较",
        assistant_message: "已在当前范围内继续处理。",
        created_at: "2026-07-24T10:01:00Z",
      },
    ];
    await page.route("**/v1/recruiting-agent/turns", async (route) => {
      turnRequests.push(route.request().postDataJSON() as Record<string, unknown>);
      const contextVersion = turnRequests.length + 1;
      await route.fulfill({
        json: {
          conversation_id: "e2e-agent-context-conversation",
          context_version: contextVersion,
          active_context: context,
          chat_history: chatHistory.slice(0, turnRequests.length),
          message: turnRequests.length === 1
            ? "已保存当前筛选范围。"
            : "已在当前范围内继续处理。",
          intent: "search_candidates",
          job_version_id: null,
          candidates: [],
          actions: [],
          tool_trace: [],
          search_summary: null,
          batch_id: null,
        },
      });
    });
    await page.route(
      "**/v1/recruiting-agent/conversations/e2e-agent-context-conversation",
      async (route) => {
        if (route.request().method() === "DELETE") {
          await route.fulfill({ status: 204 });
          return;
        }
        await route.fulfill({
          json: {
            conversation_id: "e2e-agent-context-conversation",
            context_version: 4,
            active_context: context,
            chat_history: chatHistory,
          },
        });
      },
    );

    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "招聘助手" });
    await dialog.getByLabel("向招聘助手提问").fill("先筛选符合条件的人");
    await dialog.getByRole("button", { name: "发送提问" }).click();
    await expect(dialog.getByText("助手筛选结果 · 2 位候选人")).toBeVisible();

    await dialog.getByLabel("向招聘助手提问").fill("在刚才这些人中继续比较");
    await dialog.getByRole("button", { name: "发送提问" }).click();
    await expect.poll(() => turnRequests.length).toBe(2);
    expect(turnRequests[0]).not.toHaveProperty("conversation_id");
    expect(turnRequests[0]).not.toHaveProperty("chat_history");
    expect(turnRequests[1]).toMatchObject({
      conversation_id: "e2e-agent-context-conversation",
      context_version: 2,
    });
    expect(turnRequests[1]).not.toHaveProperty("chat_history");

    await page.reload();
    await page.getByRole("button", { name: "招聘助手", exact: true }).click();
    const reloadedDialog = page.getByRole("dialog", { name: "招聘助手" });
    await expect(reloadedDialog.getByText("助手筛选结果 · 2 位候选人")).toBeVisible();
    await expect(reloadedDialog.getByText("先筛选符合条件的人")).toBeVisible();
    await expect(reloadedDialog.getByText("已保存当前筛选范围。")).toBeVisible();
    await expect(reloadedDialog.getByText("在刚才这些人中继续比较")).toBeVisible();
    await expect(reloadedDialog.getByText("已在当前范围内继续处理。")).toBeVisible();

    await reloadedDialog.getByRole("button", { name: "清除范围" }).click();
    await expect(
      reloadedDialog.getByText(
        "直接描述你想找的人，我会先整理条件，确认后才开始找人。",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(reloadedDialog.getByRole("button", { name: "清除范围" })).toHaveCount(0);
  });

  test("邮箱通道保存后同步请求只进入后台队列", async ({ page }) => {
    const gridTrackCount = (selector: string) => page.locator(selector).evaluate((element) => {
      const template = getComputedStyle(element).gridTemplateColumns.trim();
      return template ? template.split(/\s+/).length : 0;
    });

    await registerAndVerify(page, "mailbox");
    await expect(page.getByRole("button", { name: "邮箱入库", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
    await expect(page.getByRole("button", { name: "设置", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("tab", { name: "收件邮箱", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "收件邮箱", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "绑定招聘收件邮箱" })).toBeVisible();
    expect(await gridTrackCount(".settings-layout")).toBe(1);
    expect(await gridTrackCount(".mailbox-setup-shell")).toBe(1);
    await expect(page.getByRole("heading", { name: "收件通道" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "附件入库记录" })).toHaveCount(0);
    await expect(page.getByText("系统固定使用经过审核的加密连接，不开放自定义服务器地址或端口。"))
      .toBeVisible();
    await expect(page.locator("#imap-host")).toHaveCount(0);
    await expect(page.locator("#imap-port")).toHaveCount(0);
    await expect(page.getByRole("radio", { name: /Gmail \/ Google Workspace/ })).toBeDisabled();
    await page.getByRole("radio", { name: /飞书邮箱/ }).click();
    await page.locator("#mailbox-display-name").fill("E2E 收件通道");
    await page.locator("#imap-address").fill("e2e-inbox@example.test");
    await page.locator("#imap-password").fill("e2e-local-imap-authorization-code");
    const createMailboxRequest = page.waitForRequest((request) => {
      const { pathname } = new URL(request.url());
      return request.method() === "POST" && pathname === "/v1/mailboxes";
    });
    await page.getByRole("button", { name: /^(保存收件通道|创建并开始接收)$/ }).click();
    const createPayload = createMailboxRequest.then((request) => request.postDataJSON() as Record<string, unknown>);
    await expect(createPayload).resolves.toMatchObject({
      provider_key: "feishu_app_password",
      initial_sync_lookback_days: 0,
    });
    await expect(createPayload).resolves.not.toHaveProperty("imap_host");
    await expect(createPayload).resolves.not.toHaveProperty("imap_port");
    await expect(page.getByText("收件通道已创建，不导入历史邮件，后续只接收新邮件。")).toBeVisible();
    await expect(page.getByRole("heading", { name: "E2E 收件通道" })).toBeVisible();
    await expect(page.getByLabel("来源", { exact: true })).toContainText("E2E 收件通道");
    await expect(page.getByText("首次范围", { exact: true })).toBeVisible();
    await expect(page.getByText("从现在开始", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "归档通道" })).toBeVisible();
    expect(await gridTrackCount(".mailbox-workspace")).toBe(1);
    await page.getByRole("button", { name: "编辑连接" }).click();
    await expect(page.getByRole("button", { name: "返回概览" })).toBeVisible();
    await expect(page.locator("#initial-sync-lookback-days")).toHaveCount(0);
    expect(await gridTrackCount(".mailbox-detail-grid")).toBe(1);
    await page.locator("#mailbox-display-name").fill("E2E 未保存收件通道");
    page.once("dialog", (dialog) => dialog.dismiss());
    await page.getByRole("button", { name: "返回概览" }).click();
    await expect(page.getByRole("button", { name: "返回概览" })).toBeVisible();
    await page.locator("#mailbox-display-name").fill("E2E 收件通道");
    await page.getByRole("button", { name: "返回概览" }).click();
    await page.getByRole("button", { name: "新建收件通道" }).first().click();
    await expect(page.getByRole("heading", { name: "新建收件通道" })).toBeVisible();
    await expect(page.getByRole("button", { name: "取消新建" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "收件通道", exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "运行状态", exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "内容保留", exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "附件入库记录", exact: true })).toHaveCount(0);
    await expect(page.locator("#mailbox-display-name")).toHaveValue("");
    await expect(page.locator("#imap-address")).toHaveValue("");
    const newFeishuProvider = page.getByRole("radio", { name: /飞书邮箱/ });
    await expect(newFeishuProvider).toHaveAttribute("aria-checked", "false");
    await expect(page.locator(".mailbox-provider-option.is-selected")).toHaveCount(0);
    await newFeishuProvider.click();
    await expect(newFeishuProvider).toHaveAttribute("aria-checked", "true");
    await expect(newFeishuProvider).toHaveClass(/is-selected/);
    await expect(newFeishuProvider.getByText("已选择", { exact: true })).toBeVisible();
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "取消新建" }).click();
    await expect(page.getByRole("heading", { name: "E2E 收件通道" })).toBeVisible();
    await page.getByRole("button", { name: "同步此通道" }).click();
    await expect(page.getByText("已加入后台同步队列。")).toBeVisible();
    await page.getByRole("tab", { name: "候选人数据与保留", exact: true }).click();
    await expect(page.getByRole("heading", { name: "候选人数据与保留", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "候选人资料保留策略" })).toBeVisible();
    await expect(page.getByRole("button", { name: "刷新记录" })).toBeVisible();
    expect(await gridTrackCount(".settings-layout")).toBe(1);
  });

  test("旧邮箱入口会转入设置中的收件邮箱", async ({ page }) => {
    await registerAndVerify(page, "mailbox-hash");
    await page.goto("/#inbox");

    await expect(page).toHaveURL(/#settings\/mailbox$/);
    await expect(page.getByRole("button", { name: "设置", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("tab", { name: "收件邮箱", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "收件邮箱", exact: true })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("tab", { name: "收件邮箱", exact: true })).toHaveAttribute("aria-selected", "true");
  });

  test("OAuth 返回失败会保留收件设置定位并清理回调参数", async ({ page }) => {
    await registerAndVerify(page, "mailbox-oauth-return");
    await page.goto("/?mailbox_oauth=failed&mailbox_provider=gmail_oauth#settings/mailbox");

    await expect(page.getByRole("heading", { name: "收件邮箱", exact: true })).toBeVisible();
    await expect(page.getByText("邮箱授权没有完成。你可以检查服务商设置后重新发起授权。")).toBeVisible();
    await expect(page).toHaveURL(/\/#settings\/mailbox$/);
  });

  test("OAuth 服务商不显示授权码并通过整页跳转开始授权", async ({ page }) => {
    const oauthLandingPath = "/__e2e__/mailbox-oauth-provider";
    await page.route("**/v1/mailbox-providers", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              provider_key: "gmail_oauth",
              display_name: "Gmail / Google Workspace",
              authentication_mode: "oauth2",
              available: true,
              imap_host: "imap.gmail.com",
              imap_port: 993,
              default_mailbox: "INBOX",
              credential_label: "Google 授权",
              help_text: "通过 Google 登录授权，不收集或保存 Google 登录密码。",
            },
          ],
        }),
      });
    });
    await page.route("**/v1/mailbox-oauth/start", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ authorization_url: `${new URL(oauthLandingPath, page.url()).toString()}` }),
      });
    });
    await page.route(`**${oauthLandingPath}`, async (route) => {
      await route.fulfill({ contentType: "text/html", body: "<main>Mock OAuth provider</main>" });
    });

    await registerAndVerify(page, "mailbox-oauth");
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("radio", { name: /Gmail \/ Google Workspace/ }).click();
    await page.locator("#mailbox-display-name").fill("E2E Google 收件通道");
    await page.locator("#imap-address").fill("e2e-google@example.test");
    // BackofficeSelect initially renders a native fallback while its shared
    // Semi Select chunk is loading. Wait for the interactive component so the
    // test drives the same listbox that users receive after the chunk loads,
    // rather than trying to inspect the browser-owned native popup.
    await expect(page.locator(".semi-select#initial-sync-lookback-days")).toBeVisible();
    await page.getByLabel("导入历史邮件", { exact: true }).click();
    // Semi Select includes its tick icon in the accessible option name.
    await page.getByRole("option", { name: /最近 7 天$/ }).click();
    await expect(page.getByRole("combobox", { name: "导入历史邮件", exact: true }))
      .toContainText("最近 7 天");
    await expect(page.locator("#imap-password")).toHaveCount(0);
    await expect(page.getByText("Gmail / Google Workspace 网页授权")).toBeVisible();

    const oauthRequest = page.waitForRequest((request) => {
      const { pathname } = new URL(request.url());
      return request.method() === "POST" && pathname === "/v1/mailbox-oauth/start";
    });
    await page.getByRole("button", { name: "前往 Gmail / Google Workspace 授权" }).click();
    const oauthPayload = oauthRequest.then((request) => request.postDataJSON() as Record<string, unknown>);
    await expect(oauthPayload).resolves.toMatchObject({
      provider_key: "gmail_oauth",
      display_name: "E2E Google 收件通道",
      email_address: "e2e-google@example.test",
      mailbox: "INBOX",
      initial_sync_lookback_days: 7,
    });
    await expect(oauthPayload).resolves.not.toHaveProperty("password");
    await expect(page).toHaveURL(new RegExp(`${oauthLandingPath}$`));
  });
});
