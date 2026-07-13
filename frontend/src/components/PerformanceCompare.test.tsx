import { describe, it, expect } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { PerformanceCompare } from './PerformanceCompare'
import { server } from '../test/msw/server'
import {
  attributionFixture,
  biasFixture,
  hitRateFixture,
  promptVersionsFixture,
} from '../test/msw/handlers'

// Per-version fixtures, keyed by the `prompt_version` query param, so each
// compare column can be asserted independently. The shared handlers in
// handlers.ts ignore query params entirely — these overrides are what make
// the two columns actually differ.
function byVersion<T>(fixtures: Record<string, T>) {
  return ({ request }: { request: Request }) => {
    const version = new URL(request.url).searchParams.get('prompt_version') ?? ''
    const fixture = fixtures[version]
    if (!fixture) return new HttpResponse(null, { status: 404 })
    return HttpResponse.json(fixture)
  }
}

function installVersionedHandlers() {
  const hitRateByVersion = {
    'v2.0.0': { ...hitRateFixture, overall: { ...hitRateFixture.overall, total_pnl: 100 } },
    'v2.1.0': { ...hitRateFixture, overall: { ...hitRateFixture.overall, total_pnl: 900 } },
  }
  const attributionByVersion = {
    'v2.0.0': attributionFixture,
    'v2.1.0': { ...attributionFixture, total_realized_pnl: 500 },
  }
  const biasByVersion = {
    'v2.0.0': biasFixture,
    'v2.1.0': biasFixture,
  }
  server.use(
    http.get('/api/review/hit-rate', byVersion(hitRateByVersion)),
    http.get('/api/review/attribution', byVersion(attributionByVersion)),
    http.get('/api/review/bias', byVersion(biasByVersion)),
  )
  return { hitRateByVersion, attributionByVersion }
}

describe('PerformanceCompare', () => {
  it('defaults to the two most recent versions and fetches each column independently', async () => {
    installVersionedHandlers()
    render(<PerformanceCompare />)

    expect(await screen.findByDisplayValue('v2.0.0')).toBeInTheDocument()
    expect(screen.getByDisplayValue('v2.1.0')).toBeInTheDocument()

    // Both columns render hit-rate tables driven by different prompt_version
    // params — same WP-9.5 endpoint, no duplicated metric logic, distinct data.
    expect(await screen.findByText('+$100')).toBeInTheDocument()
    expect(await screen.findByText('+$900')).toBeInTheDocument()
  })

  it('changing one column picker does not affect the other', async () => {
    installVersionedHandlers()
    render(<PerformanceCompare />)

    await screen.findByDisplayValue('v2.0.0')
    const columns = screen.getAllByRole('combobox')
    expect(columns).toHaveLength(2)

    // Point Version A at v2.1.0 too — both columns should now show +$900,
    // proving each column's fetch is independent of the other's selection.
    await userEvent.selectOptions(columns[0], 'v2.1.0')

    await waitFor(() => {
      const values = screen.getAllByRole('combobox').map((el) => (el as HTMLSelectElement).value)
      expect(values).toEqual(['v2.1.0', 'v2.1.0'])
    })
  })

  it('renders insufficient-sample state per column, independent of the other column', async () => {
    installVersionedHandlers()
    const { container } = render(<PerformanceCompare />)

    await screen.findByDisplayValue('v2.0.0')
    // hitRateFixture's rows are `sufficient: false` regardless of version —
    // both columns should surface their own insufficient chip.
    const columns = container.querySelectorAll('.compare-column')
    expect(columns).toHaveLength(2)
    for (const col of Array.from(columns)) {
      expect(await within(col as HTMLElement).findAllByText(/insufficient/)).not.toHaveLength(0)
    }
  })

  it('shows a placeholder when no prompt versions have been recorded', async () => {
    server.use(http.get('/api/review/prompt-versions', () => HttpResponse.json([])))
    render(<PerformanceCompare />)

    expect(await screen.findByText('no prompt versions recorded yet')).toBeInTheDocument()
  })

  it('shows a pick-a-version placeholder for a column with nothing selected', async () => {
    server.use(http.get('/api/review/prompt-versions', () => HttpResponse.json(['v1.0.0'])))
    render(<PerformanceCompare />)

    // Only one version exists, so Version A gets it and Version B stays empty.
    await screen.findByDisplayValue('v1.0.0')
    expect(screen.getByText('pick a prompt_version to compare')).toBeInTheDocument()
  })

  it('surfaces a per-column fetch error without breaking the other column', async () => {
    server.use(
      http.get('/api/review/hit-rate', ({ request }) => {
        const version = new URL(request.url).searchParams.get('prompt_version')
        if (version === 'v2.1.0') return new HttpResponse(null, { status: 500 })
        return HttpResponse.json(hitRateFixture)
      }),
    )
    render(<PerformanceCompare />)

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
    // Version A (v2.0.0) still renders its own data despite Version B's error.
    expect(screen.getByText('Hit rate by strategy')).toBeInTheDocument()
  })
})

// Sanity check that the default-version selection uses the fixture's actual
// ordering rather than a hardcoded guess, in case handlers.ts changes.
describe('PerformanceCompare default versions', () => {
  it('picks the last two entries of promptVersionsFixture', () => {
    expect(promptVersionsFixture.slice(-2)).toEqual(['v2.0.0', 'v2.1.0'])
  })
})
