import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AttributionByStrategyPanel, AttributionPanel } from './AttributionPanel'
import type { AttributionResponse } from '../api'

function attributionData(
  overrides: Partial<AttributionResponse> = {},
): AttributionResponse {
  return {
    by_underlying: {
      SPY: { underlying: 'SPY', net_pnl: 612, trade_count: 3 },
      QQQ: { underlying: 'QQQ', net_pnl: -84, trade_count: 1 },
    },
    by_strategy: {
      bull_put_spread: { strategy: 'bull_put_spread', net_pnl: 612, trade_count: 3 },
      iron_condor: { strategy: 'iron_condor', net_pnl: -84, trade_count: 1 },
    },
    total_realized_pnl: 528,
    open_summary: { open_position_count: 0, realized_to_date: 0 },
    ...overrides,
  }
}

describe('AttributionPanel', () => {
  it('renders one bar row per underlying with signed P&L', () => {
    render(<AttributionPanel attribution={attributionData()} />)
    expect(screen.getByText('SPY')).toBeInTheDocument()
    expect(screen.getByText('+$612')).toBeInTheDocument()
    expect(screen.getByText('QQQ')).toBeInTheDocument()
    expect(screen.getByText('-$84')).toBeInTheDocument()
  })

  it('shows an empty state with no closed trades', () => {
    render(<AttributionPanel attribution={attributionData({ by_underlying: {} })} />)
    expect(screen.getByText('no closed trades yet')).toBeInTheDocument()
  })

  it('shows the open-position footnote only when positions are open', () => {
    const { rerender } = render(<AttributionPanel attribution={attributionData()} />)
    expect(screen.queryByText(/shown separately/)).not.toBeInTheDocument()

    rerender(
      <AttributionPanel
        attribution={attributionData({
          open_summary: { open_position_count: 1, realized_to_date: 20 },
        })}
      />,
    )
    expect(screen.getByText(/shown separately/)).toBeInTheDocument()
  })
})

describe('AttributionByStrategyPanel', () => {
  it('renders a row per strategy plus a TOTAL row', () => {
    render(<AttributionByStrategyPanel attribution={attributionData()} />)
    expect(screen.getByText('bull_put_spread')).toBeInTheDocument()
    expect(screen.getByText('+$528')).toBeInTheDocument() // TOTAL
  })
})
