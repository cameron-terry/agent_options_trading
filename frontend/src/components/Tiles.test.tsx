import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { Tiles } from './Tiles'
import type { Tiles as TilesData } from '../api'

function tilesData(overrides: Partial<TilesData> = {}): TilesData {
  return {
    account_equity: { value: 100000, as_of: '2026-07-12T14:00:00Z' },
    realized_pnl: { total: 1250, closed_count: 4, hit_count: 3 },
    unrealized_pnl: { total: -320, open_position_count: 2 },
    cycles_today: { total: 6, by_action: { OPENED: 1, NO_ACTION_AGENT: 5 } },
    ...overrides,
  }
}

function tile(label: string) {
  return screen.getByText(label).closest('.tile') as HTMLElement
}

describe('Tiles', () => {
  it('renders account equity formatted as currency', () => {
    render(<Tiles tiles={tilesData()} />)
    expect(within(tile('Account Equity')).getByText('$100,000')).toBeInTheDocument()
    expect(within(tile('Account Equity')).getByText('as of last cycle')).toBeInTheDocument()
  })

  it('shows an em dash and insufficient-data note when equity is null', () => {
    render(<Tiles tiles={tilesData({ account_equity: { value: null, as_of: null } })} />)
    expect(within(tile('Account Equity')).getByText('—')).toBeInTheDocument()
    expect(within(tile('Account Equity')).getByText('insufficient data')).toBeInTheDocument()
  })

  it('marks positive realized P&L positive and negative unrealized negative', () => {
    render(<Tiles tiles={tilesData()} />)
    const realized = within(tile('Realized P&L')).getByText('+$1,250')
    expect(realized).toHaveClass('pnl-positive')
    const unrealized = within(tile('Unrealized P&L')).getByText('-$320')
    expect(unrealized).toHaveClass('pnl-negative')
  })

  it('summarizes cycles by action, lowercased and joined', () => {
    render(<Tiles tiles={tilesData()} />)
    const cycles = tile('Cycles Today')
    expect(within(cycles).getByText('6')).toBeInTheDocument()
    expect(within(cycles).getByText('1 opened · 5 no_action_agent')).toBeInTheDocument()
  })

  it('falls back to "no cycles yet" when there are no actions', () => {
    render(<Tiles tiles={tilesData({ cycles_today: { total: 0, by_action: {} } })} />)
    expect(within(tile('Cycles Today')).getByText('no cycles yet')).toBeInTheDocument()
  })
})
