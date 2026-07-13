import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ActivityFeed } from './ActivityFeed'
import type { ActivityItem } from '../api'

function item(overrides: Partial<ActivityItem> = {}): ActivityItem {
  return {
    timestamp: '2026-07-12T14:05:00Z',
    kind: 'journal',
    action: 'OPEN',
    headline: 'OPENED SPY iron condor',
    cycle_id: 'cyc-1',
    position_id: null,
    ...overrides,
  }
}

describe('ActivityFeed', () => {
  it('shows an empty-state message when there are no items', () => {
    render(<ActivityFeed items={[]} />)
    expect(screen.getByText('no activity yet')).toBeInTheDocument()
  })

  it('renders the first headline word bolded and the remainder as body text', () => {
    render(<ActivityFeed items={[item()]} />)
    // First word is emphasized...
    expect(screen.getByText('OPENED').tagName).toBe('STRONG')
    // ...and the rest of the headline follows it.
    expect(screen.getByText('SPY iron condor')).toBeInTheDocument()
  })

  it('renders one row per item', () => {
    render(
      <ActivityFeed
        items={[
          item({ timestamp: '2026-07-12T14:05:00Z', cycle_id: 'cyc-1' }),
          item({ timestamp: '2026-07-12T14:06:00Z', cycle_id: 'cyc-2' }),
        ]}
      />,
    )
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })
})
