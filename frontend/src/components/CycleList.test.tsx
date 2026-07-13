import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CycleList } from './CycleList'
import type { CycleListItem } from '../api'

function cycle(overrides: Partial<CycleListItem> = {}): CycleListItem {
  return {
    cycle_id: 'cyc-1',
    timestamp: '2026-07-12T14:05:00Z',
    action_taken: 'OPENED',
    underlying: 'SPY',
    strategy: 'iron_condor',
    conviction: 0.72,
    ...overrides,
  }
}

describe('CycleList', () => {
  it('shows an empty state when there are no cycles', () => {
    render(<CycleList cycles={[]} selectedCycleId={null} onSelect={() => {}} />)
    expect(screen.getByText('no cycles in range')).toBeInTheDocument()
  })

  it('renders a button per cycle with its action and underlying', () => {
    render(
      <CycleList
        cycles={[cycle({ cycle_id: 'cyc-1' }), cycle({ cycle_id: 'cyc-2', underlying: 'QQQ' })]}
        selectedCycleId={null}
        onSelect={() => {}}
      />,
    )
    expect(screen.getAllByRole('button')).toHaveLength(2)
    expect(screen.getByText('QQQ')).toBeInTheDocument()
  })

  it('renders an em dash when the underlying is null', () => {
    render(
      <CycleList cycles={[cycle({ underlying: null })]} selectedCycleId={null} onSelect={() => {}} />,
    )
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('marks the selected cycle', () => {
    render(
      <CycleList
        cycles={[cycle({ cycle_id: 'cyc-1' })]}
        selectedCycleId="cyc-1"
        onSelect={() => {}}
      />,
    )
    expect(screen.getByRole('button')).toHaveClass('cycle-list__item--selected')
  })

  it('tones a rejected cycle as critical', () => {
    render(
      <CycleList
        cycles={[cycle({ action_taken: 'REJECTED' })]}
        selectedCycleId={null}
        onSelect={() => {}}
      />,
    )
    expect(screen.getByText('REJECTED')).toHaveClass('action-chip--critical')
  })

  it('calls onSelect with the cycle id when clicked', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <CycleList cycles={[cycle({ cycle_id: 'cyc-42' })]} selectedCycleId={null} onSelect={onSelect} />,
    )
    await user.click(screen.getByRole('button'))
    expect(onSelect).toHaveBeenCalledWith('cyc-42')
  })
})
