import { expect, type Page } from "@playwright/test";

interface E2EDelivery {
  recipient: string;
  verification_url: string;
  expires_minutes: number;
}

interface E2EDeliveriesResponse {
  deliveries: E2EDelivery[];
}

export interface E2EWorkspaceFixture {
  resume_ids: string[];
  job_version_id: string;
}

let registrationSequence = 0;

function uniqueTestEmail(label: string): string {
  registrationSequence += 1;
  return `playwright-${label}-${Date.now()}-${registrationSequence}@example.test`;
}

export async function e2eControl<T>(
  page: Page,
  path: string,
  options: { method?: string; body?: unknown; headers?: Record<string, string> } = {},
): Promise<T> {
  // BrowserContext.request shares the active browser context's cookies.  Using
  // it instead of page.evaluate keeps this test-only control channel stable
  // while React redirects between registration and verification screens.
  const requestUrl = new URL(path, page.url());
  const response = await page.context().request.fetch(requestUrl.toString(), {
    method: options.method ?? "GET",
    data: options.body,
    headers: options.headers,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok()) {
    // Do not render captured action links or test recipient addresses in a
    // Playwright failure log.
    throw new Error(`E2E control request failed (${response.status()}): ${requestUrl.pathname}`);
  }
  return payload as T;
}

export async function registerAndVerify(page: Page, label: string): Promise<string> {
  const email = uniqueTestEmail(label);
  await page.goto("/register");
  await page.locator("#register-organization").fill(`E2E 工作区 ${label}`);
  await page.locator("#register-name").fill("E2E 管理员");
  await page.locator("#register-email").fill(email);
  await page.locator("#register-password").fill("E2E-password-2026");
  await page.locator("#register-password-confirmation").fill("E2E-password-2026");
  await page.getByRole("button", { name: /免费开启/ }).click();

  await expect(
    page.getByRole("heading", { name: "请查收验证邮件" }),
  ).toBeVisible();
  await expect
    .poll(async () => {
      const response = await e2eControl<E2EDeliveriesResponse>(page, "/__e2e__/deliveries");
      return response.deliveries.length;
    })
    .toBeGreaterThan(0);

  const deliveries = await e2eControl<E2EDeliveriesResponse>(page, "/__e2e__/deliveries");
  const delivery = deliveries.deliveries.at(-1);
  if (!delivery) throw new Error("Expected a local verification delivery.");
  const verificationUrl = new URL(delivery.verification_url);
  await page.goto(`${verificationUrl.pathname}${verificationUrl.search}`);
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  return email;
}

export async function login(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.locator("#login-email").fill(email);
  await page.locator("#login-password").fill("E2E-password-2026");
  await page.getByRole("button", { name: "登录工作台" }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
}

export async function logout(page: Page): Promise<void> {
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByRole("button", { name: "登录工作台" })).toBeVisible();
}

export async function seedWorkspaceFixture(page: Page): Promise<E2EWorkspaceFixture> {
  return e2eControl<E2EWorkspaceFixture>(page, "/__e2e__/fixture/seed", {
    method: "POST",
  });
}

/** A real one-page PDF so the upload page exercises multipart FastAPI input. */
export function e2eResumePdf(): Buffer {
  const content = "BT /F1 18 Tf 72 720 Td (E2E Resume Candidate) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${Buffer.byteLength(content, "ascii")} >>\nstream\n${content}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(pdf, "ascii"));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xrefOffset = Buffer.byteLength(pdf, "ascii");
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) {
    pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
  }
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  return Buffer.from(pdf, "ascii");
}
