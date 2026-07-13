import { test, expect } from '@playwright/test'

// The Cycles Today tile: the value cell inside the tile whose label is
// "Cycles Today". The mock backend maps this number to its SSE counter.
function cyclesTodayValue(page: import('@playwright/test').Page) {
  return page.locator('.tile', { hasText: 'Cycles Today' }).locator('.tile__value')
}

test('overview loads with header, kill-switch and tiles', async ({ page }) => {
  await page.goto('/')

  // Brand + mode come from the /api/overview payload once it loads.
  await expect(page.getByText('OPTIONS AGENT')).toBeVisible()
  await expect(page.getByText('/ paper')).toBeVisible()

  // Kill-switch chip reflects the NONE state.
  await expect(page.getByText('TRADING · NONE')).toBeVisible()

  // Tiles rendered from the overview payload.
  await expect(page.getByText('Account Equity')).toBeVisible()
  await expect(cyclesTodayValue(page)).toBeVisible()
})

test('switching to the Decisions tab renders the cycle explorer', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByText('Account Equity')).toBeVisible()

  await page.getByText('Decisions', { exact: true }).click()

  // Overview tiles gone, decision explorer's Cycles panel present.
  await expect(page.getByText('Account Equity')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: /Cycles/ })).toBeVisible()
})

test('a live SSE update event refreshes the overview', async ({ page }) => {
  await page.goto('/')

  // Initial load renders the pre-update value the mock resets to on connect.
  await expect(cyclesTodayValue(page)).toHaveText('0')

  // The mock pushes an `update` event ~700ms after the EventSource connects;
  // the client re-fetches /api/overview and the tile advances to 1 — proving
  // the SSE-driven refresh path end to end in a real browser.
  await expect(cyclesTodayValue(page)).toHaveText('1')
})
