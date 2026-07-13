import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { CycleTrace } from './CycleTrace'
import type { CycleDetail, TradeProposal } from '../api'

function detail(overrides: Partial<CycleDetail> = {}): CycleDetail {
  return {
    cycle_id: 'cyc-1',
    timestamp: '2026-07-12T14:05:00Z',
    action_taken: 'OPENED',
    underlying: 'SPY',
    strategy: 'iron_condor',
    conviction: 0.72,
    model_id: 'claude-opus-4-8',
    prompt_version: 'v3',
    limits_version: 'v2',
    context_hash: 'abcdef0123456789',
    proposal: null,
    tool_calls_transcript: [],
    validation_result: null,
    rejection_rule_ids: [],
    sizing_result: null,
    positions: [],
    orders: [],
    ...overrides,
  }
}

function proposal(overrides: Partial<TradeProposal> = {}): TradeProposal {
  return {
    action: 'OPEN',
    underlying: 'SPY',
    strategy: 'iron_condor',
    legs: [{ right: 'put', side: 'sell', strike: 520, expiration: '2026-08-15', ratio: 1 }],
    thesis: 'range-bound into opex',
    iv_rationale: 'elevated IV rank',
    catalyst_check: 'no earnings',
    conviction: 0.72,
    est_max_loss: 320,
    est_max_profit: 180,
    breakevens: [517, 588],
    net_delta: 0.02,
    net_theta: 0.5,
    net_vega: -0.1,
    exit_plan: { profit_target_pct: 0.5, stop_loss_max_loss_fraction: 2, time_stop_dte: 7 },
    informed_by: [],
    ...overrides,
  }
}

describe('CycleTrace', () => {
  it('shows an empty state when no cycle is selected', () => {
    render(<CycleTrace detail={null} />)
    expect(screen.getByText('select a cycle to view its trace')).toBeInTheDocument()
  })

  it('renders header metadata with truncated hash, conviction, and tool-call count', () => {
    render(<CycleTrace detail={detail()} />)
    // context_hash truncated to 12 chars.
    expect(screen.getByText('abcdef012345')).toBeInTheDocument()
    // conviction to 2 decimals.
    expect(screen.getByText('0.72')).toBeInTheDocument()
    // tool-call count reflects the (empty) transcript.
    const toolCalls = screen.getByText('tool calls').closest('span') as HTMLElement
    expect(within(toolCalls).getByText('0')).toBeInTheDocument()
  })

  it('shows the empty-transcript note when no tools were called', () => {
    render(<CycleTrace detail={detail()} />)
    expect(screen.getByText(/no tool calls recorded/)).toBeInTheDocument()
  })

  it('omits the proposal panel when there is no proposal', () => {
    render(<CycleTrace detail={detail({ proposal: null })} />)
    expect(screen.queryByText('Proposal')).not.toBeInTheDocument()
  })

  it('renders the proposal panel with exit-plan percentages when a proposal exists', () => {
    render(<CycleTrace detail={detail({ proposal: proposal() })} />)
    expect(screen.getByText('Proposal')).toBeInTheDocument()
    expect(screen.getByText('range-bound into opex')).toBeInTheDocument()
    // exit_plan: 0.5 -> PT 50%, 2 -> SL 200% of max loss, 7 DTE.
    expect(screen.getByText(/PT 50%.*SL 200% of max loss.*time-stop 7 DTE/)).toBeInTheDocument()
  })

  it('reports no validation when none was recorded', () => {
    render(<CycleTrace detail={detail({ validation_result: null })} />)
    expect(screen.getByText('no validation recorded for this cycle')).toBeInTheDocument()
  })

  it('singularizes the contract count when sizing produced one contract', () => {
    render(
      <CycleTrace
        detail={detail({
          sizing_result: {
            contracts: 1,
            sized_max_loss: 320,
            sized_max_profit: 180,
            risk_budget_used: 0.03,
            binding_constraint: null,
            capped_to_zero: false,
          },
        })}
      />,
    )
    expect(screen.getByText(/1 contract$/)).toBeInTheDocument()
  })
})
