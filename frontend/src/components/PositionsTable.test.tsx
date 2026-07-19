import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { PositionsTable } from './PositionsTable'
import type { PositionSummary } from '../api'

function position(overrides: Partial<PositionSummary> = {}): PositionSummary {
  return {
    id: 'pos-1',
    underlying: 'SPY',
    strategy: 'iron_condor',
    strikes: '520/525/580/585',
    quantity: 2,
    entry_net_amount: -180,
    current_mark: -140,
    marked_at: '2026-07-12T14:00:00Z',
    unrealized_pnl: 80,
    dte: 21,
    distance_to_trigger: { direction: 'target', pct: 0.4 },
    ...overrides,
  }
}

describe('PositionsTable', () => {
  it('shows a loading state when positions have not loaded yet', () => {
    render(<PositionsTable positions={null} />)
    expect(screen.getByText('loading positions…')).toBeInTheDocument()
    expect(screen.queryByText('no open positions')).not.toBeInTheDocument()
  })

  it('shows an empty state when there are no positions', () => {
    render(<PositionsTable positions={[]} />)
    expect(screen.getByText('no open positions')).toBeInTheDocument()
  })

  it('renders entry credit and mark as absolute currency values', () => {
    render(<PositionsTable positions={[position()]} />)
    const row = screen.getByRole('row', { name: /SPY/ })
    // entry_net_amount -180 and current_mark -140 shown as magnitudes.
    expect(within(row).getByText('$180')).toBeInTheDocument()
    expect(within(row).getByText('$140')).toBeInTheDocument()
  })

  it('signs and classes unrealized P&L', () => {
    render(<PositionsTable positions={[position({ unrealized_pnl: -55 })]} />)
    const cell = screen.getByText('-$55')
    expect(cell).toHaveClass('pnl-negative')
  })

  it('renders a distance bar with the trigger direction and clamped width', () => {
    const { container } = render(
      <PositionsTable positions={[position({ distance_to_trigger: { direction: 'stop', pct: 1.5 } })]} />,
    )
    // pct is clamped to [0,1] for the bar width even when the raw value exceeds 1.
    const fill = container.querySelector('.distance-bar__fill') as HTMLElement
    expect(fill).toHaveClass('distance-bar__fill--stop')
    expect(fill.style.width).toBe('100%')
    // Label still shows the raw percentage.
    expect(screen.getByText(/150%.*stop/)).toBeInTheDocument()
  })

  it('renders em dashes for missing DTE and distance-to-trigger', () => {
    render(<PositionsTable positions={[position({ dte: null, distance_to_trigger: null })]} />)
    // One dash for DTE cell, one for the distance column.
    expect(screen.getAllByText('—')).toHaveLength(2)
  })
})
