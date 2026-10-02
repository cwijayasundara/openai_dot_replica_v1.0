import { expect, test } from "@playwright/test";

import { E2E_API } from "../playwright.config";

test("one thread across Slack and the web: message, job, approval, audit", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByText("You have no dots yet.")).toBeVisible();
  await page.getByRole("button", { name: "Create dot" }).click();
  await expect(page).toHaveURL(/\/dots\/dot-/);

  // A DM through the Slack adapter's inbound router lands in the same thread.
  const slack = await request.post(`${E2E_API}/__e2e/slack`, { data: { text: "Morning from Slack" } });
  expect(slack.status()).toBe(202);
  const fromSlack = page.locator('[data-testid="thread-message"][data-source="slack"]');
  await expect(fromSlack).toContainText("Morning from Slack");
  await expect(fromSlack).toContainText("Slack");
  await expect(page.getByText("Got your Slack message.")).toBeVisible();

  const composer = page.getByLabel("Message your dot");
  await composer.fill("Research open dot runtimes and email Sam");
  await composer.press("Enter");
  await expect(page.locator('[data-testid="thread-message"][data-source="web"]')).toContainText(
    "Research open dot runtimes",
  );
  await expect(page.getByText("Started a research job.")).toBeVisible();

  const job = page.getByTestId("job").filter({ hasText: "researcher" });
  await expect(job).toBeVisible();
  await expect(job).toContainText("Done");

  const card = page.getByRole("article", { name: "Approval for send_email" });
  await expect(card).toContainText("Needs your decision");
  await expect(card).toContainText("sam@example.com");
  await expect(page.getByTestId("dot-state")).toHaveText("Waiting on you");

  await card.getByRole("button", { name: "Edit" }).click();
  await expect(card).toContainText("Current: Two runtimes found.");
  await card.getByLabel("body").fill("Two runtimes found. Brief attached.");
  await card.getByRole("button", { name: "Approve with edits" }).click();

  await expect(page.getByText("Email sent to Sam.")).toBeVisible();
  await expect(page.getByText("Nothing needs your decision.")).toBeVisible();
  const decided = page.getByRole("article", { name: "Approval for send_email" });
  await expect(decided).toContainText("Approved with edits");
  await expect(decided).toContainText("Two runtimes found. Brief attached.");
  await expect(decided).toContainText("Decided by ada");
  const sent = await (await request.get(`${E2E_API}/__e2e/sent`)).json();
  expect(sent.to).toEqual(["sam@example.com"]);

  // A correction targets one of the dot's messages and is stored for reflection.
  const reply = page
    .locator('[data-testid="thread-message"][data-role="ai"]')
    .filter({ hasText: "Email sent to Sam." });
  await reply.getByRole("button", { name: "Correct this" }).click();
  await reply.getByLabel("What should the dot do differently?").fill("Keep emails to Sam under 50 words");
  await reply.getByRole("button", { name: "Save correction" }).click();
  await expect(reply).toContainText("Correction noted.");

  // An edit the replay gate held for review: accept it, then roll it back.
  const dotId = /\/dots\/(dot-[^/]+)/.exec(page.url())![1];
  expect((await request.post(`${E2E_API}/__e2e/memory/${dotId}`)).status()).toBe(201);
  await page.getByRole("link", { name: "Memory" }).click();
  const held = page.getByTestId("memory-version");
  await expect(held).toContainText("Held for your review");
  await expect(held).toContainText("Replay: no cited episode can be replayed");
  await held.getByRole("button", { name: "Accept" }).click();
  await expect(held).toContainText("Accepted");
  await held.getByRole("button", { name: "Roll back" }).click();
  await expect(held).toContainText("Rolled back");
  await expect(held.getByRole("button")).toHaveCount(0);

  await page.getByRole("link", { name: "Audit trail" }).click();
  const rows = page.getByTestId("audit-row");
  // The Guardian reviews the proposal, then the edited arguments again.
  await expect(rows.filter({ hasText: "Guardian" }).filter({ hasText: "send_email" })).toHaveCount(2);
  const approvals = rows.and(page.locator('[data-kind="approval"]'));
  await expect(approvals.filter({ hasText: "ada" })).toContainText("edit");
  await expect(approvals.filter({ hasText: "pending" })).toHaveCount(1);
});
