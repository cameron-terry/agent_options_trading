import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { BiasPanel } from './BiasPanel'
import type { BiasResponse, DirectionWinRateOut } from '../api'

function direction(overrides: Partial<DirectionWinRateOut> = {}): DirectionWinRateOut {
  return {
    direction: 'bullish',
    sample_size: 0,
    sufficient: false,
    hit_rate: null,
    avg_win: null,
    avg_loss: null,
    expectancy: null,
    total_pnl: 0,
    ...overrides,
  }
}

function biasData(overrides: Partial<BiasResponse> = {}): BiasResponse {
  return {
    min_sample_size: 10,
    window_start: null,
    delta_skew: {
      sample_size: 2,
      mean_net_delta: null,
      sufficient: false,
      direction: 'insufficient_data',
    },
    by_direction: {
      bullish: direction({ direction: 'bullish' }),
      bearish: direction({ direction: 'bearish' }),
    },
    event_proximity: {
      near_catalyst: direction({ direction: 'near_catalyst' }),
      baseline: direction({ direction: 'baseline' }),
    },
    ...overrides,
  }
}

describe('BiasPanel', () => {
  it('shows insufficient-data text for the skew meter when not sufficient', () => {
    render(<BiasPanel bias={biasData()} />)
    expect(screen.getByText('insufficient data (n=2)')).toBeInTheDocument()
  })

  it('keeps the band/zero markers inside .skew-meter__track when insufficient', () => {
    // Regression test: these two elements are absolutely positioned and rely
    // on .skew-meter__track (position: relative) as their containing block.
    // Rendering them as direct children of .skew-meter instead (no relative
    // ancestor) makes them escape to the page root — a real bug caught by
    // code review since jsdom doesn't compute layout, so only a structural
    // (not visual) assertion can catch a regression here.
    const { container } = render(<BiasPanel bias={biasData()} />)
    const track = container.querySelector('.skew-meter__track')
    expect(track).not.toBeNull()
    expect(track!.querySelector('.skew-meter__band')).not.toBeNull()
    expect(track!.querySelector('.skew-meter__zero')).not.toBeNull()
  })

  it('renders the pinned skew value when sufficient', () => {
    render(
      <BiasPanel
        bias={biasData({
          delta_skew: {
            sample_size: 29,
            mean_net_delta: 0.11,
            sufficient: true,
            direction: 'bullish',
          },
        })}
      />,
    )
    expect(screen.getByText('+0.11')).toBeInTheDocument()
  })

  it('renders insufficient chips for cohorts below the sample floor', () => {
    render(<BiasPanel bias={biasData()} />)
    const chips = screen.getAllByText('insufficient')
    // bullish + bearish + near_catalyst rows, all insufficient by default.
    expect(chips.length).toBeGreaterThanOrEqual(3)
  })

  it('renders hit rate and expectancy for a sufficient cohort', () => {
    render(
      <BiasPanel
        bias={biasData({
          by_direction: {
            bullish: direction({
              direction: 'bullish',
              sample_size: 29,
              sufficient: true,
              hit_rate: 0.72,
              expectancy: 31,
              total_pnl: 400,
            }),
            bearish: direction({ direction: 'bearish' }),
          },
        })}
      />,
    )
    expect(screen.getByText('72%')).toBeInTheDocument()
  })
})
