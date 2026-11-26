import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

test('owner console navigation, chat cancellation, documents and responsive layout', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.getByLabel('Owner password').fill(readFileSync(process.env.WARDEN_PASSWORD_FILE || resolve('../.local-state/owner-password.txt'), 'utf8').trim());
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Assistant', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'What needs your attention?' })).toBeVisible();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `test-results/${info.project.name}-assistant.png`, fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  expect(overflow).toBe(false);
  await page.getByRole('textbox', { name: 'Message Warden' }).fill('Explain what Warden monitors.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop investigation' })).toBeVisible();
  await page.getByRole('button', { name: 'Stop investigation' }).click();
  await expect(page.getByText('Investigation stopped', { exact: true })).toBeVisible();
  for (const [path, title] of [['/knowledge', 'Knowledge'], ['/server', 'Server'], ['/activity', 'Activity'], ['/settings', 'Settings']]) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    if (title === 'Server') await expect(page.getByRole('heading', { name: 'Services', exact: true })).toBeVisible();
    await page.screenshot({ path: `test-results/${info.project.name}-${title.toLowerCase()}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  }
  await page.goto('/knowledge');
  await page.locator('input[type=file]').setInputFiles({ name: `browser-check-${info.project.name}.md`, mimeType: 'text/markdown', buffer: Buffer.from('# Test document\nWarden is a local server assistant.') });
  await expect(page.getByText(`browser-check-${info.project.name}.md`, { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: `Remove source browser-check-${info.project.name}.md` }).click();
  await expect(page.getByRole('button', { name: `Remove source browser-check-${info.project.name}.md` })).toHaveCount(0);
  if (info.project.name === 'mobile') {
    await page.getByRole('button', { name: 'Open navigation' }).click();
    await expect(page.getByRole('link', { name: 'Knowledge', exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Knowledge', exact: true }).click();
  }
  expect(errors).toEqual([]);
});

test('approval controls and cited document previews', async ({ page }, info) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill(readFileSync(process.env.WARDEN_PASSWORD_FILE || resolve('../.local-state/owner-password.txt'), 'utf8').trim());
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Assistant', exact: true })).toBeVisible();
  const citation = { id: 1, document_id: 'test-doc', page: 2, content: 'The sentinel runs every five minutes.', name: 'Runbook.md', hash: 'test-hash', source_id: 'test-source', collection: 'Server' };
  const action = { id: 999999, tool: 'container_restart', args_json: '{"name":"test-container"}', status: 'pending', description: 'Restart the test container', run_id: 'test-run', created_at: new Date().toISOString() };
  await page.route('**/api/approvals', route => route.fulfill({ json: [action] }));
  await page.route('**/api/approvals/999999/decision', async route => {
    expect(route.request().postDataJSON()).toEqual({ approved: false });
    action.status = 'denied';
    await route.fulfill({ json: action });
  });
  await page.goto('/activity');
  await expect(page.getByText('Restart the test container', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reject', exact: true }).click();
  await expect(page.getByText('denied', { exact: true })).toBeVisible();
  await page.route('**/api/conversations/test-thread/messages', route => route.fulfill({ json: {
    messages: [{ id: 'test-message', role: 'assistant', content: 'The sentinel runs every five minutes. [1]', citations_json: JSON.stringify([citation]), created_at: new Date().toISOString() }], runs: [],
  } }));
  await page.route('**/api/documents/test-doc', route => route.fulfill({ json: { document: { name: citation.name, hash: citation.hash }, chunks: [{ id: 1, page: 2, content: citation.content }] } }));
  await page.goto('/chat/test-thread');
  await page.getByRole('button', { name: /Runbook.md/ }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByText('CITED PASSAGE', { exact: false })).toBeVisible();
  await page.screenshot({ path: `test-results/${info.project.name}-citation.png`, fullPage: true });
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('attachments wait for indexing and evidence toggles', async ({ page }, info) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill(readFileSync(resolve('../.local-state/owner-password.txt'), 'utf8').trim());
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Assistant', exact: true })).toBeVisible();
  const evidence = page.locator('aside.evidence');
  if (info.project.name === 'desktop') {
    await expect(evidence).toBeVisible();
    await page.getByRole('button', { name: 'Toggle evidence' }).click();
    await expect(evidence).toBeHidden();
  } else {
    await expect(evidence).toBeHidden();
    await page.getByRole('button', { name: 'Toggle evidence' }).click();
    await expect(evidence).toBeVisible();
    await page.getByRole('button', { name: 'Close evidence' }).click();
  }
  const source = { id: 'attachment-test', name: 'Attached.md', kind: 'upload', collection: 'General', status: 'queued' };
  await page.route('**/api/sources/upload', route => route.fulfill({ json: source }));
  await page.route('**/api/sources', route => route.fulfill({ json: [source] }));
  await page.locator('input[type=file]').setInputFiles({ name: source.name, mimeType: 'text/markdown', buffer: Buffer.from('A sample document') });
  await expect(page.getByText(source.name, { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeDisabled();
  source.status = 'ready';
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Detach document' }).click();
  await expect(page.getByText(source.name, { exact: true })).toHaveCount(0);
});
