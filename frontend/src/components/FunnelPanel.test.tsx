import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { FunnelPanel, RejectionsByRulePanel } from './FunnelPanel'
import type { FunnelResponse } from '../api'

function funnelData(overrides: Partial<FunnelResponse> = {}): FunnelResponse {
  return {
    total: 12,
    gated: 2,
    reasoned: 10,
    no_action_agent: 6,
    proposed: 4,
    rejected: 1,
    sized_to_zero: 0,
    execution_failed: 0,
    opened: 3,
    rejections_by_rule: [{ rule_id: 'EVENT_BLACKOUT', count: 1 }],
    ...overrides,
  }
}

describe('FunnelPanel', () => {
  it('renders each funnel stage with its count', () => {
    render(<FunnelPanel funnel={funnelData()} />)
    expect(screen.getByText('Cycles run')).toBeInTheDocument()
    const openedRow = screen.getByText('Opened').closest('.funnel__row') as HTMLElement
    expect(within(openedRow).getByText('3')).toBeInTheDocument()
  })

  it('shades each row a distinct color, lightest first', () => {
    render(<FunnelPanel funnel={funnelData()} />)
    const bars = document.querySelectorAll('.funnel__bar')
    expect(bars).toHaveLength(5)
    const colors = Array.from(bars).map((el) => (el as HTMLElement).style.background)
    expect(colors).toEqual([
      'var(--f1)',
      'var(--f2)',
      'var(--f3)',
      'var(--f4)',
      'var(--f5)',
    ])
  })

  it('renders the drop-off summary line', () => {
    render(<FunnelPanel funnel={funnelData()} />)
    expect(screen.getByText(/gated 2/)).toBeInTheDocument()
    expect(screen.getByText(/rejected 1/)).toBeInTheDocument()
  })

  it('shows an empty state when there are no cycles', () => {
    render(
      <FunnelPanel
        funnel={funnelData({
          total: 0,
          gated: 0,
          reasoned: 0,
          no_action_agent: 0,
          proposed: 0,
          rejected: 0,
          opened: 0,
        })}
      />,
    )
    expect(screen.getByText('no cycles in range')).toBeInTheDocument()
  })
})

describe('RejectionsByRulePanel', () => {
  it('renders one row per rejection rule with its fire count and a reading', () => {
    render(<RejectionsByRulePanel funnel={funnelData()} />)
    const row = screen.getByText('EVENT_BLACKOUT').closest('tr') as HTMLElement
    expect(within(row).getByText('1')).toBeInTheDocument()
    expect(within(row).getByText(/earnings\/event blackout window/)).toBeInTheDocument()
  })

  it('falls back to a generic reading for an unmapped rule id', () => {
    render(
      <RejectionsByRulePanel
        funnel={funnelData({ rejections_by_rule: [{ rule_id: 'SOME_NEW_RULE', count: 2 }] })}
      />,
    )
    expect(screen.getByText('no reading available')).toBeInTheDocument()
  })

  it('shows an empty state when there are no rejections', () => {
    render(<RejectionsByRulePanel funnel={funnelData({ rejections_by_rule: [] })} />)
    expect(screen.getByText('no rejections in range')).toBeInTheDocument()
  })
})
