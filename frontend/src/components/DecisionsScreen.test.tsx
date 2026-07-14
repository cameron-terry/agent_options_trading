import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { DecisionsScreen } from './DecisionsScreen'
import { server } from '../test/msw/server'
import type { CycleListItem } from '../api'

const twoCyclesFixture: CycleListItem[] = [
  {
    cycle_id: 'cyc-2',
    timestamp: '2026-07-13T09:00:00Z',
    action_taken: 'OPENED',
    underlying: 'AAPL',
    strategy: 'bull_put_spread',
    conviction: 0.65,
  },
  {
    cycle_id: 'cyc-1',
    timestamp: '2026-07-12T14:05:00Z',
    action_taken: 'OPENED',
    underlying: 'SPY',
    strategy: 'iron_condor',
    conviction: 0.72,
  },
]

describe('DecisionsScreen', () => {
  it('loads the cycle list and auto-selects the newest cycle', async () => {
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId={null} onSelectCycle={onSelectCycle} />)

    // Default MSW handler returns one cycle (cyc-1 / SPY / OPENED).
    expect(await screen.findByText('SPY')).toBeInTheDocument()
    await waitFor(() => expect(onSelectCycle).toHaveBeenCalledWith('cyc-1'))
  })

  it('preserves an incoming selection on mount instead of jumping to newest', async () => {
    // Regression test: a citation deep-link from the Ask screen (WP-9.9)
    // sets selectedCycleId before DecisionsScreen ever mounts. The cycle
    // list here has cyc-2 as newest (index 0) — if the mount effect ignores
    // the incoming selection, onSelectCycle gets called with 'cyc-2' and
    // the citation silently fails to navigate to the cycle it named.
    server.use(http.get('/api/cycles', () => HttpResponse.json(twoCyclesFixture)))
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId="cyc-1" onSelectCycle={onSelectCycle} />)

    await screen.findByText('AAPL')
    expect(onSelectCycle).not.toHaveBeenCalled()
  })

  it('still jumps to newest on mount when the incoming selection is not in the fetched list', async () => {
    server.use(http.get('/api/cycles', () => HttpResponse.json(twoCyclesFixture)))
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId="cyc-stale" onSelectCycle={onSelectCycle} />)

    await screen.findByText('AAPL')
    await waitFor(() => expect(onSelectCycle).toHaveBeenCalledWith('cyc-2'))
  })

  it('still jumps to newest on a later, user-driven filter change (WP-9.3 behavior preserved)', async () => {
    server.use(http.get('/api/cycles', () => HttpResponse.json(twoCyclesFixture)))
    const user = userEvent.setup()
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId="cyc-1" onSelectCycle={onSelectCycle} />)

    // Mount preserves the incoming cyc-1 selection (still in the list).
    await screen.findByText('AAPL')
    expect(onSelectCycle).not.toHaveBeenCalled()

    // A real filter change afterward must still jump to newest, even though
    // cyc-1 is still a valid selection — this is the pre-existing WP-9.3
    // "never leave a stale selection in place after re-scoping" behavior.
    await user.type(screen.getByPlaceholderText(/symbol/), 'aapl')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(onSelectCycle).toHaveBeenCalledWith('cyc-2'))
  })

  it('clears the selection when the fetched list is empty', async () => {
    server.use(http.get('/api/cycles', () => HttpResponse.json([])))
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId={null} onSelectCycle={onSelectCycle} />)

    expect(await screen.findByText('no cycles in range')).toBeInTheDocument()
    await waitFor(() => expect(onSelectCycle).toHaveBeenCalledWith(null))
  })

  it('renders the trace for the selected cycle', async () => {
    render(<DecisionsScreen selectedCycleId="cyc-1" onSelectCycle={() => {}} />)

    // The cycle-detail fetch resolves and CycleTrace renders its header.
    expect(await screen.findByText('Cycle cyc-1')).toBeInTheDocument()
  })

  it('surfaces a load error', async () => {
    server.use(http.get('/api/cycles', () => new HttpResponse(null, { status: 500 })))
    render(<DecisionsScreen selectedCycleId={null} onSelectCycle={() => {}} />)

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
  })
})
