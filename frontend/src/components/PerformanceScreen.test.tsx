import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { PerformanceScreen } from './PerformanceScreen'
import { server } from '../test/msw/server'

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
})
