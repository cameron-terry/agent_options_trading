import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EquityCurve } from './EquityCurve'
import type { EquityCurvePoint } from '../api'

function point(overrides: Partial<EquityCurvePoint> = {}): EquityCurvePoint {
  return {
    timestamp: '2026-07-12T20:00:00Z',
    cumulative_realized_pnl: 0,
    equity: 100000,
    ...overrides,
  }
}

describe('EquityCurve', () => {
  it('shows an empty state with fewer than two points', () => {
    render(<EquityCurve points={[point()]} />)
    expect(screen.getByText(/insufficient data/)).toBeInTheDocument()
  })

  it('draws the line and area paths for a valid series', () => {
    const { container } = render(
      <EquityCurve
        points={[
          point({ equity: 98000, cumulative_realized_pnl: 0 }),
          point({ equity: 100000, cumulative_realized_pnl: 2000 }),
        ]}
      />,
    )
    expect(container.querySelector('.equity-curve__line')).toBeInTheDocument()
    expect(container.querySelector('.equity-curve__area')).toBeInTheDocument()
  })

  it('labels the last point with its equity value', () => {
    const { container } = render(
      <EquityCurve points={[point({ equity: 98000 }), point({ equity: 100000 })]} />,
    )
    const endlabel = container.querySelector('.equity-curve__endlabel') as HTMLElement
    expect(endlabel.textContent).toBe('$100,000')
  })

  it('falls back to cumulative_realized_pnl when equity is null', () => {
    const { container } = render(
      <EquityCurve
        points={[
          point({ equity: null, cumulative_realized_pnl: 0 }),
          point({ equity: null, cumulative_realized_pnl: 500 }),
        ]}
      />,
    )
    const endlabel = container.querySelector('.equity-curve__endlabel') as HTMLElement
    expect(endlabel.textContent).toBe('$500')
  })
})
