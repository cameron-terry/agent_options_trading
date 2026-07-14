import { describe, it, expect } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { PerformanceScreen } from './PerformanceScreen'
import { server } from '../test/msw/server'
import { attributionFixture, biasFixture, hitRateFixture } from '../test/msw/handlers'

describe('PerformanceScreen', () => {
  it('loads and renders all four review panels', async () => {
    render(<PerformanceScreen />)

    expect(await screen.findByText('Entry-cycle funnel')).toBeInTheDocument()
    expect(screen.getByText('Rejections by rule')).toBeInTheDocument()
    expect(screen.getByText('Hit rate by strategy')).toBeInTheDocument()
    expect(screen.getByText('P&L attribution by underlying')).toBeInTheDocument()
    expect(screen.getByText('Bias monitor')).toBeInTheDocument()

    // funnelFixture: total=12, opened=3; hitRateFixture.overall.trade_count=2.
    expect(screen.getByText('12 cycles · 3 opened · 2 closed')).toBeInTheDocument()
  })

  it('surfaces a load error', async () => {
    server.use(http.get('/api/review/funnel', () => new HttpResponse(null, { status: 500 })))
    render(<PerformanceScreen />)

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
  })

  it('switches to the compare layout without touching the funnel panel', async () => {
    render(<PerformanceScreen />)
    await screen.findByText('Entry-cycle funnel')

    // Single-version view: exactly one hit-rate table, no version columns yet.
    expect(screen.getAllByText('Hit rate by strategy')).toHaveLength(1)
    expect(screen.queryByText('Version A')).toBeNull()

    await userEvent.click(screen.getByRole('checkbox', { name: 'Compare prompt versions' }))

    // Funnel survives the mode switch (it's not filterable by prompt_version).
    expect(screen.getByText('Entry-cycle funnel')).toBeInTheDocument()
    expect(screen.getByText(/funnel is not filterable by prompt_version/)).toBeInTheDocument()

    // The single hit-rate table is replaced by one per compare column (A + B).
    await screen.findByText('Version A')
    expect(screen.getByText('Version B')).toBeInTheDocument()
    expect(await screen.findAllByText('Hit rate by strategy')).toHaveLength(2)
  })

  it('drops the stale top-level prompt_version filter once compare mode is on, and disables the control', async () => {
    const hitRateUrls: string[] = []
    server.use(
      http.get('/api/review/hit-rate', ({ request }) => {
        hitRateUrls.push(request.url)
        return HttpResponse.json(hitRateFixture)
      }),
    )
    const { container } = render(<PerformanceScreen />)
    await screen.findByText('Entry-cycle funnel')
    const topFilters = container.querySelector('.cycle-filters') as HTMLElement

    // Pick a specific version in the top filter before compare mode exists.
    await screen.findByRole('option', { name: 'v2.1.0' })
    await userEvent.selectOptions(within(topFilters).getByDisplayValue('prompt_version: all'), 'v2.1.0')
    await waitFor(() => {
      expect(hitRateUrls.some((u) => u.includes('prompt_version=v2.1.0'))).toBe(true)
    })

    const beforeToggleCount = hitRateUrls.length
    await userEvent.click(screen.getByRole('checkbox', { name: 'Compare prompt versions' }))

    // The (now-disabled) top select still displays v2.1.0, but it no longer
    // scopes anything: the summary/funnel fetch triggered by the toggle
    // carries no prompt_version, unlike the per-column compare fetches that
    // also fire around the same time. Scoped to .cycle-filters because both
    // compare columns' own pickers can independently show "v2.1.0" too.
    const promptVersionSelect = within(topFilters).getByDisplayValue('v2.1.0') as HTMLSelectElement
    expect(promptVersionSelect).toBeDisabled()
    await waitFor(() => {
      const newUrls = hitRateUrls.slice(beforeToggleCount)
      expect(newUrls.some((u) => !u.includes('prompt_version'))).toBe(true)
    })
  })

  it('stops fetching the single-version attribution/bias endpoints once comparing', async () => {
    // Track requests by whether they carry prompt_version: the two compare
    // columns always scope their own fetches to a version, so their calls
    // are expected and shouldn't be conflated with PerformanceScreen's own
    // (unscoped) single-version fetch, which should fire once at mount and
    // never again once compareMode is on.
    const attributionUrls: string[] = []
    const biasUrls: string[] = []
    server.use(
      http.get('/api/review/attribution', ({ request }) => {
        attributionUrls.push(request.url)
        return HttpResponse.json(attributionFixture)
      }),
      http.get('/api/review/bias', ({ request }) => {
        biasUrls.push(request.url)
        return HttpResponse.json(biasFixture)
      }),
    )
    render(<PerformanceScreen />)
    await screen.findByText('Entry-cycle funnel')
    const unscoped = (urls: string[]) => urls.filter((u) => !u.includes('prompt_version'))
    expect(unscoped(attributionUrls)).toHaveLength(1)
    expect(unscoped(biasUrls)).toHaveLength(1)

    await userEvent.click(screen.getByRole('checkbox', { name: 'Compare prompt versions' }))
    await screen.findByText('Version A')

    // Changing the shared date-range filter while comparing re-fires the two
    // compare columns' own scoped fetches, but must not re-trigger
    // PerformanceScreen's single-version (unscoped) fetch — those panels
    // aren't rendered in compare mode at all, so that would be pure waste.
    await userEvent.selectOptions(screen.getByDisplayValue('all time'), '7')
    await waitFor(() => {
      expect(screen.getByDisplayValue('last 7 days')).toBeInTheDocument()
    })

    expect(unscoped(attributionUrls)).toHaveLength(1)
    expect(unscoped(biasUrls)).toHaveLength(1)
  })
})
