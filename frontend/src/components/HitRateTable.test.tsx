import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { HitRateTable } from './HitRateTable'
import type { HitRateResponse, StrategyStatsOut } from '../api'

function stats(overrides: Partial<StrategyStatsOut> = {}): StrategyStatsOut {
  return {
    strategy: 'bull_put_spread',
    trade_count: 19,
    hit_count: 14,
    miss_count: 5,
    hit_rate: 0.74,
    avg_win: 118,
    avg_loss: -196,
    expectancy: 35,
    total_pnl: 672,
    sufficient: true,
    ...overrides,
  }
}

function hitRateData(overrides: Partial<HitRateResponse> = {}): HitRateResponse {
  return {
    by_strategy: { bull_put_spread: stats() },
    overall: stats({ strategy: '_all' }),
    open_summary: { open_position_count: 0, realized_to_date: 0 },
    min_sample_size: 10,
    ...overrides,
  }
}

function row(strategyOrLabel: string) {
  return screen.getByText(strategyOrLabel).closest('tr') as HTMLElement
}

describe('HitRateTable', () => {
  it('renders per-strategy stats alongside a TOTAL row', () => {
    render(<HitRateTable hitRate={hitRateData()} />)
    expect(within(row('bull_put_spread')).getByText('74%')).toBeInTheDocument()
    expect(within(row('TOTAL')).getByText('74%')).toBeInTheDocument()
  })

  it('renders a dash and insufficient chip when a bucket is below min_sample_size', () => {
    render(
      <HitRateTable
        hitRate={hitRateData({
          by_strategy: {
            covered_call: stats({
              strategy: 'covered_call',
              trade_count: 2,
              hit_rate: null,
              avg_win: null,
              avg_loss: null,
              expectancy: null,
              sufficient: false,
            }),
          },
        })}
      />,
    )
    const r = row('covered_call')
    // hit_rate, avg_win, avg_loss, expectancy are all null → four dashes.
    expect(within(r).getAllByText('—')).toHaveLength(4)
    expect(within(r).getByText(/insufficient/)).toBeInTheDocument()
  })

  it('shows an empty state when there are no closed trades', () => {
    render(<HitRateTable hitRate={hitRateData({ by_strategy: {} })} />)
    expect(screen.getByText('no closed trades yet')).toBeInTheDocument()
  })

  it('shows the open-position footnote only when positions are open', () => {
    const { rerender } = render(<HitRateTable hitRate={hitRateData()} />)
    expect(screen.queryByText(/realized-to-date/)).not.toBeInTheDocument()

    rerender(
      <HitRateTable
        hitRate={hitRateData({
          open_summary: { open_position_count: 2, realized_to_date: 50 },
        })}
      />,
    )
    expect(screen.getByText(/realized-to-date/)).toBeInTheDocument()
  })
})
