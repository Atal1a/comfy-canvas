import { expect, test } from '@playwright/test';

const pixel = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVR4nGP4z8DAwMAAAAYAAQHLR3FvAAAAAElFTkSuQmCC',
  'base64',
);

function job(index) {
  const id = `job-${String(index).padStart(3, '0')}`;
  const image = { filename: `${id}.png`, subfolder: 'mobile/test', type: 'output', media_type: 'image/png' };
  return {
    id,
    workflow: 'qwen2511-modular-flux2',
    prompt: `Prompt ${index}`,
    source_prompt: '',
    title: `Image ${index}`,
    status: 'completed',
    submitted_at: `2026-08-30T00:${String(index % 60).padStart(2, '0')}:00+00:00`,
    completed_at: `2026-08-30T00:${String(index % 60).padStart(2, '0')}:30+00:00`,
    parameters: {},
    images: { final: [image], stage1: [] },
    items: [{ id: '0', final: image, upscales: [], details: [] }],
    favorite: false,
    collections: [],
  };
}

const jobs = Array.from({ length: 120 }, (_, index) => job(index));
const adminUsers = [
  { id: 1, username: '测试用户', role: 'admin', disabled: false, avatar_url: null, storage_bytes: 2_097_152, created_at: '2026-08-01T08:00:00+08:00' },
  { id: 2, username: '插画师', role: 'user', disabled: false, avatar_url: null, storage_bytes: 536_870_912, created_at: '2026-08-12T08:00:00+08:00' },
  { id: 3, username: '暂停账号', role: 'user', disabled: true, avatar_url: null, storage_bytes: 0, created_at: '2026-08-20T08:00:00+08:00' },
];
const adminInvites = [
  { invite_id: 8, created_at: '2026-08-29T08:00:00+08:00', expires_at: '2026-09-05T08:00:00+08:00', used_at: null, revoked_at: null },
  { invite_id: 7, created_at: '2026-08-20T08:00:00+08:00', expires_at: '2026-08-27T08:00:00+08:00', used_at: '2026-08-21T08:00:00+08:00', revoked_at: null },
];

async function mockApi(page) {
  page.apiCounts = new Map();
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    page.apiCounts.set(path, (page.apiCounts.get(path) || 0) + 1);
    let body = {};
    if (path === '/api/auth/me') body = { user: { id: 1, username: '测试用户', role: 'admin', avatar_url: null } };
    else if (path === '/api/workflows') body = [];
    else if (path === '/api/collections' || path === '/api/prompt-templates' || path === '/api/tips') body = [];
    else if (path === '/api/queue') body = { tasks: [], performance_mode: false, recovery_state: 'ready' };
    else if (path === '/api/chat/overview') body = { conversations: [], incoming: [], outgoing: [], unread: 0 };
    else if (path === '/api/admin/invites') body = adminInvites;
    else if (path === '/api/admin/users') body = adminUsers;
    else if (path === '/api/admin/activity') body = [];
    else if (path === '/api/admin/queue') body = { tasks: [], performance_mode: false, recovery_state: 'ready' };
    else if (path === '/api/admin/diagnostics') body = {
      generated_at: '2026-08-30T10:00:00+08:00',
      database: { quick_check: 'ok', rows: { users: 7, jobs: 669 } },
      queue: { completed: 12 },
      storage: {
        mobile_output: { bytes: 2_147_483_648, files: 600 },
        thumbnails: { bytes: 419_430_400, files: 1200 },
        chat_media: { bytes: 10_485_760, files: 10 },
        logs: { bytes: 1024, files: 1 },
      },
      frontend: { bytes: 900_000, files: 42 },
      comfyui: { reachable: true, status: 200 },
    };
    else if (path === '/api/gallery/context') {
      const anchor = jobs.findIndex(item => item.id === url.searchParams.get('record_id'));
      const radius = Number(url.searchParams.get('radius') || 24);
      const start = Math.max(0, anchor - radius);
      const end = Math.min(jobs.length, anchor + radius + 1);
      body = { jobs: jobs.slice(start, end), anchor_index: anchor - start, has_before: start > 0, has_after: end < jobs.length };
    } else if (path === '/api/gallery') {
      const limit = Number(url.searchParams.get('limit') || 24);
      const offset = Number(url.searchParams.get('offset') || 0);
      body = jobs.slice(offset, offset + limit);
    } else if (/^\/api\/jobs\/job-\d+$/.test(path)) {
      body = jobs.find(item => item.id === path.split('/').at(-1));
    } else if (path === '/api/account/storage') body = { used_bytes: 0, quota_bytes: 1_000_000 };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-08-30T10:00:00+08:00'));
  await mockApi(page);
});

test('LAN HTTP supports chat IDs and copying without secure-context APIs', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined });
    Object.defineProperty(crypto, 'randomUUID', { value: undefined });
  });
  await page.goto('/');
  const result = await page.evaluate(async () => {
    const ids = Array.from({ length: 20 }, () => createChatNonce());
    let copied = '';
    const original = document.execCommand;
    document.execCommand = command => {
      if (command !== 'copy') return false;
      copied = document.activeElement.value;
      return true;
    };
    try {
      await copyCanvasText('局域网复制');
    } finally {
      document.execCommand = original;
    }
    return { ids, copied };
  });
  expect(new Set(result.ids).size).toBe(20);
  expect(result.ids.every(id => /^[a-f0-9]{32}$/.test(id))).toBe(true);
  expect(result.copied).toBe('局域网复制');
});

test('prebuilt frontend boots with greeting and icon hydration', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#home-greeting-name')).toHaveText('测试用户');
  await expect(page.locator('.primary-nav svg').first()).toBeVisible();
  expect(page.apiCounts.get('/api/queue')).toBe(1);
  expect(errors).toEqual([]);
});

test('retired prompt assistant has no entry point or background requests', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#home-greeting-name')).toHaveText('测试用户');
  await expect(page.locator('#prompt-assistant-dialog, #prompt-enhance, #prompt-interrogate')).toHaveCount(0);
  expect(await page.evaluate(() => typeof openPromptAssistant)).toBe('undefined');
  expect([...page.apiCounts.keys()].some(path => path.startsWith('/api/prompt-tools'))).toBe(false);
});

test('account avatar editor starts from a movable fixed crop frame', async ({ page }) => {
  await page.goto('/settings/account');
  await expect(page.locator('#account-profile-form [data-icon-before="check"] svg')).toBeVisible();
  await page.locator('#account-avatar-input').setInputFiles({ name: 'avatar.png', mimeType: 'image/png', buffer: pixel });
  await expect(page.locator('#crop-editor-dialog')).toBeVisible();
  await expect(page.locator('#crop-editor-dialog')).toHaveClass(/avatar-crop-mode/);
  await expect(page.locator('#crop-editor-ratio')).toBeDisabled();
  const selection = await page.evaluate(() => ({ ...state.cropDrag.selection }));
  expect(selection.width).toBeLessThan(1);
  expect(selection.width).toBeCloseTo(selection.height, 5);
});

test('admin diagnostics renders byte sizes without a runtime error', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/settings/admin');
  await expect(page.locator('#admin-diagnostics')).toContainText('完整性正常');
  await expect(page.locator('#admin-diagnostics')).toContainText('2.00 GB');
  await expect(page.locator('#admin-diagnostics')).toContainText('连接正常');
  expect(errors).toEqual([]);
});

test('admin access workspace summarizes, filters, and manages users without crowded row actions', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings/admin');

  await expect(page.locator('#admin-user-count')).toHaveText('3');
  await expect(page.locator('#admin-active-user-count')).toHaveText('2');
  await expect(page.locator('#admin-active-invite-count')).toHaveText('1');
  await expect(page.locator('#admin-invites .admin-invite-card')).toHaveCount(1);
  await expect(page.locator('#admin-users .admin-user-card')).toHaveCount(3);

  await page.locator('#admin-user-search').fill('插画');
  await expect(page.locator('#admin-users .admin-user-card')).toHaveCount(1);
  await page.getByRole('button', { name: '管理' }).click();
  await expect(page.locator('#admin-user-dialog')).toBeVisible();
  await expect(page.locator('#admin-user-dialog-name')).toHaveText('插画师');
  await expect(page.locator('#admin-user-password')).toBeVisible();
  await expect(page.locator('#admin-user-dialog [data-icon-before="key-round"] svg')).toBeVisible();
  expect(errors).toEqual([]);
});

test('photo viewer extends context beyond the initial thumbnail window', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/jobs/job-050');
  await expect(page.locator('#photo-detail-experience')).toBeVisible();
  await expect.poll(() => page.evaluate(() => photoDetailCurrentEntry().sequence.length)).toBeGreaterThan(24);

  for (let index = 0; index < 55; index += 1) {
    const result = await page.evaluate(() => {
      const before = photoDetailCurrentEntry();
      return { moved: photoDetailCommitPage(1), id: state.detailJob?.id, flatIndex: before.flatIndex, length: before.sequence.length };
    });
    expect(result.moved, `viewer should advance at step ${index + 1}: ${JSON.stringify(result)}`).toBe(true);
    if (index === 24 || index === 48) await page.waitForTimeout(250);
  }

  await expect.poll(async () => page.evaluate(() => state.detailJob?.id)).toBe('job-105');
  await expect.poll(() => page.evaluate(() => photoDetailCurrentEntry().sequence.length)).toBeGreaterThan(49);
  await expect(page.locator('#photo-detail-thumbs button').first()).toBeVisible();
});

test('route changes cancel stale page requests without showing an error', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.route('**/api/gallery?*', async route => {
    await new Promise(resolve => setTimeout(resolve, 450));
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }).catch(() => {});
  });

  await page.evaluate(() => { navigate('/history'); });
  await page.waitForTimeout(40);
  await page.evaluate(() => navigate('/settings'));

  await expect(page.locator('#view-settings')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.locator('#view-settings')).toBeVisible();
  expect(errors).toEqual([]);
});
