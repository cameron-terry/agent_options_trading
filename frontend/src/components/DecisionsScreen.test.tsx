import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { DecisionsScreen } from './DecisionsScreen'
import { server } from '../test/msw/server'

describe('DecisionsScreen', () => {
  it('loads the cycle list and auto-selects the newest cycle', async () => {
    const onSelectCycle = vi.fn()
    render(<DecisionsScreen selectedCycleId={null} onSelectCycle={onSelectCycle} />)

    // Default MSW handler returns one cycle (cyc-1 / SPY / OPENED).
    expect(await screen.findByText('SPY')).toBeInTheDocument()
    await waitFor(() => expect(onSelectCycle).toHaveBeenCalledWith('cyc-1'))
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
