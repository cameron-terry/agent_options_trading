import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { CycleTrace } from './CycleTrace'
import type {
  CycleDetail,
  LinkedOrder,
  LinkedPosition,
  RejectionReason,
  ToolCallRecord,
  TradeProposal,
} from '../api'

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
    data_quality_flags: [],
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

function toolCall(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    tool_name: 'get_option_chain',
    tool_input: { symbol: 'SPY', dte: 30 },
    result_json: '{"iv_rank": 42}',
    ...overrides,
  }
}

function reason(overrides: Partial<RejectionReason> = {}): RejectionReason {
  return {
    rule_id: 'MAX_LOSS_CAP',
    severity: 'error',
    human_message: 'max loss exceeds the per-trade cap',
    field_affected: 'est_max_loss',
    observed: 500,
    limit: 300,
    ...overrides,
  }
}

function linkedOrder(overrides: Partial<LinkedOrder> = {}): LinkedOrder {
  return {
    id: 'ord-1',
    role: 'entry',
    status: 'FILLED',
    submitted_at: '2026-07-12T14:05:00Z',
    filled_at: '2026-07-12T14:06:00Z',
    net_fill_price: 1.85,
    filled_qty: 2,
    ...overrides,
  }
}

function linkedPosition(overrides: Partial<LinkedPosition> = {}): LinkedPosition {
  return {
    id: 'pos-1',
    underlying: 'SPY',
    strategy: 'iron_condor',
    quantity: 2,
    entry_net_amount: -180,
    current_mark: -140,
    unrealized_pnl: 80,
    realized_pnl: null,
    status: 'OPEN',
    ...overrides,
  }
}

// The sizing panel stitches together text across nested <span>/<b> nodes, so
// assert on the panel's flattened textContent rather than element-scoped text.
function sizingText(container: HTMLElement): string {
  return (container.querySelector('.sizing-panel') as HTMLElement).textContent ?? ''
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

  it('omits the data-quality flag row when no flags are set', () => {
    const { container } = render(<CycleTrace detail={detail()} />)
    expect(container.querySelector('.cycle-header__flags')).not.toBeInTheDocument()
  })

  it('renders a warn chip with a tooltip for a known data-quality flag', () => {
    render(<CycleTrace detail={detail({ data_quality_flags: ['phantom_net_delta'] })} />)
    const chip = screen.getByText('phantom_net_delta')
    expect(chip).toHaveClass('action-chip--warn')
    expect(chip).toHaveAttribute('title', expect.stringContaining('Greek-aggregation bug'))
  })

  it('falls back to the raw flag name as the tooltip for an unknown flag', () => {
    render(<CycleTrace detail={detail({ data_quality_flags: ['some_new_flag'] })} />)
    expect(screen.getByText('some_new_flag')).toHaveAttribute('title', 'some_new_flag')
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

describe('CycleTrace — failed validation', () => {
  it('counts failed rules in the heading and flags each failing rule chip', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          validation_result: {
            passed: false,
            reasons: [
              reason(),
              reason({
                rule_id: 'DTE_FLOOR',
                human_message: 'expiration is too close',
                observed: 3,
                limit: 7,
              }),
            ],
          },
        })}
      />,
    )

    expect(screen.getByText('2 rules failed')).toBeInTheDocument()

    const chips = container.querySelector('.validation-rules') as HTMLElement
    expect(within(chips).getByText('MAX_LOSS_CAP')).toHaveClass('validation-rules__rule--fail')
    expect(within(chips).getByText('DTE_FLOOR')).toHaveClass('validation-rules__rule--fail')
  })

  it('lists each rule reason with its observed-vs-limit numbers', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({ validation_result: { passed: false, reasons: [reason()] } })}
      />,
    )

    const items = container.querySelectorAll('.validation-rules__reasons li')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('max loss exceeds the per-trade cap')
    expect(items[0].textContent).toContain('(observed 500, limit 300)')
  })

  it('singularizes the heading for a single failed rule', () => {
    render(
      <CycleTrace
        detail={detail({ validation_result: { passed: false, reasons: [reason()] } })}
      />,
    )
    expect(screen.getByText('1 rule failed')).toBeInTheDocument()
  })

  it('omits the observed/limit suffix when either number is missing', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          validation_result: {
            passed: false,
            reasons: [reason({ observed: null, limit: null })],
          },
        })}
      />,
    )
    const item = container.querySelector('.validation-rules__reasons li') as HTMLElement
    expect(item.textContent).toContain('max loss exceeds the per-trade cap')
    expect(item.textContent).not.toContain('observed')
  })

  it('reads as passed and leaves warning-severity chips unflagged', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          validation_result: {
            passed: true,
            reasons: [reason({ rule_id: 'IV_LOW', severity: 'warning' })],
          },
        })}
      />,
    )

    expect(screen.getByText('passed')).toBeInTheDocument()
    const chips = container.querySelector('.validation-rules') as HTMLElement
    expect(within(chips).getByText('IV_LOW')).not.toHaveClass('validation-rules__rule--fail')
  })

  it('notes when validation ran but recorded no reasons', () => {
    render(<CycleTrace detail={detail({ validation_result: { passed: true, reasons: [] } })} />)
    expect(screen.getByText('no rule reasons recorded')).toBeInTheDocument()
  })
})

describe('CycleTrace — populated transcript', () => {
  it('renders one step per tool call with a 1-based index and formatted args', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall(), toolCall({ tool_name: 'get_quote' })],
        })}
      />,
    )

    const steps = container.querySelectorAll('details.tool-transcript__step')
    expect(steps).toHaveLength(2)

    expect(within(steps[0] as HTMLElement).getByText('1')).toBeInTheDocument()
    expect(within(steps[0] as HTMLElement).getByText('get_option_chain')).toBeInTheDocument()
    // tool_input is rendered as k=<json> pairs.
    expect(
      (steps[0].querySelector('.tool-transcript__args') as HTMLElement).textContent,
    ).toBe('(symbol="SPY", dte=30)')

    expect(within(steps[1] as HTMLElement).getByText('2')).toBeInTheDocument()
  })

  it('opens only the final step by default', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall(), toolCall(), toolCall({ tool_name: 'get_quote' })],
        })}
      />,
    )

    const steps = container.querySelectorAll('details.tool-transcript__step')
    expect((steps[0] as HTMLDetailsElement).open).toBe(false)
    expect((steps[1] as HTMLDetailsElement).open).toBe(false)
    expect((steps[2] as HTMLDetailsElement).open).toBe(true)
  })

  it('truncates a long result gist but keeps the full body', () => {
    const long = 'y'.repeat(100)
    const { container } = render(
      <CycleTrace detail={detail({ tool_calls_transcript: [toolCall({ result_json: long })] })} />,
    )

    // GIST_MAX_CHARS is 64, then an ellipsis.
    const gist = container.querySelector('.tool-transcript__gist') as HTMLElement
    expect(gist.textContent).toBe(`${'y'.repeat(64)}…`)

    // The raw blob is still available in the expanded body.
    const body = container.querySelector('.tool-transcript__body') as HTMLElement
    expect(body.textContent).toBe(long)
  })

  it('collapses whitespace in the result gist', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: '{\n  "iv_rank": 42\n}' })],
        })}
      />,
    )
    const gist = container.querySelector('.tool-transcript__gist') as HTMLElement
    expect(gist.textContent).toBe('{ "iv_rank": 42 }')
  })

  it('truncates a pretty-printed body over 10 lines behind a click-to-expand toggle', () => {
    // Pretty-printed, this 15-element array is 17 lines: "[", 15 items, "]".
    const items = Array.from({ length: 15 }, (_, i) => `item-${i}`)
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: JSON.stringify(items) })],
        })}
      />,
    )

    const body = container.querySelector('.tool-transcript__body') as HTMLElement
    // First 10 lines ("[" plus items 0-8) render outside the toggle.
    const preview = body.childNodes[0].textContent ?? ''
    expect(preview).toContain('"item-0"')
    expect(preview).toContain('"item-8"')
    expect(preview).not.toContain('"item-9"')

    const more = container.querySelector('.tool-transcript__body-more') as HTMLDetailsElement
    expect(more.open).toBe(false)
    expect(within(more).getByText('show 7 more lines')).toBeInTheDocument()
    expect(more.textContent).toContain('"item-9"')
    expect(more.textContent).toContain('"item-14"')
  })

  it('uses the singular "line" when exactly one line is hidden', () => {
    // Pretty-printed, a 9-element array is 11 lines: "[", 9 items, "]" —
    // one line past the 10-line threshold.
    const items = Array.from({ length: 9 }, (_, i) => `item-${i}`)
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: JSON.stringify(items) })],
        })}
      />,
    )
    const more = container.querySelector('.tool-transcript__body-more') as HTMLDetailsElement
    expect(within(more).getByText('show 1 more line')).toBeInTheDocument()
  })

  it('truncates a long result that pretty-prints to a single giant line', () => {
    // A long JSON string scalar has no newlines at all — the line-count
    // check alone would never catch this, only a character-count fallback.
    const longValue = 'x'.repeat(850)
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: JSON.stringify(longValue) })],
        })}
      />,
    )

    const body = container.querySelector('.tool-transcript__body') as HTMLElement
    const preview = body.childNodes[0].textContent ?? ''
    expect(preview.length).toBe(800)

    const more = container.querySelector('.tool-transcript__body-more') as HTMLDetailsElement
    expect(within(more).getByText('show more')).toBeInTheDocument()
    expect(more.textContent).toContain('x'.repeat(10))
  })

  it('relabels the toggle to "show less" once expanded', () => {
    const items = Array.from({ length: 15 }, (_, i) => `item-${i}`)
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: JSON.stringify(items) })],
        })}
      />,
    )

    // A real click queues the native "toggle" event asynchronously (per the
    // HTML spec), so drive it directly here rather than racing that timing.
    const more = container.querySelector('.tool-transcript__body-more') as HTMLDetailsElement
    more.open = true
    fireEvent(more, new Event('toggle'))
    expect(within(more).getByText('show less')).toBeInTheDocument()
    expect(within(more).queryByText(/show 7 more lines/)).not.toBeInTheDocument()

    more.open = false
    fireEvent(more, new Event('toggle'))
    expect(within(more).getByText(/show 7 more lines/)).toBeInTheDocument()
  })

  it('does not add a click-to-expand toggle for a body of 10 lines or fewer', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          tool_calls_transcript: [toolCall({ result_json: JSON.stringify({ iv_rank: 42 }) })],
        })}
      />,
    )
    expect(container.querySelector('.tool-transcript__body-more')).not.toBeInTheDocument()
  })
})

describe('CycleTrace — order and position links', () => {
  it('renders a filled order as a net debit', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({ orders: [{ id: 'ord-1', anomaly: false, order: linkedOrder() }] })}
      />,
    )
    const text = sizingText(container)
    expect(text).toContain('Order ord-1 filled')
    expect(text).toContain('at net debit')
    expect(text).toContain('1.85')
  })

  it('renders a negative fill price as a net credit, using its magnitude', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          orders: [{ id: 'ord-1', anomaly: false, order: linkedOrder({ net_fill_price: -2.5 }) }],
        })}
      />,
    )
    const text = sizingText(container)
    expect(text).toContain('at net credit')
    expect(text).toContain('2.50')
    expect(text).not.toContain('-2.50')
  })

  it('flags an anomalous order link as broken history', () => {
    const { container } = render(
      <CycleTrace detail={detail({ orders: [{ id: 'ord-9', anomaly: true, order: null }] })} />,
    )
    expect(sizingText(container)).toContain('order ord-9 — not found (broken history)')
    expect(container.querySelector('.sizing-panel__anomaly')).toBeInTheDocument()
  })

  it('treats a null order as broken history even when not flagged anomalous', () => {
    const { container } = render(
      <CycleTrace detail={detail({ orders: [{ id: 'ord-8', anomaly: false, order: null }] })} />,
    )
    expect(sizingText(container)).toContain('order ord-8 — not found (broken history)')
  })

  it('renders a position with signed unrealized P&L and its recorded outcomes', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          positions: [
            {
              id: 'pos-1',
              anomaly: false,
              position: linkedPosition(),
              outcomes: [
                {
                  id: 'out-1',
                  event_type: 'PROFIT_TARGET',
                  recorded_at: '2026-07-12T15:00:00Z',
                  realized_pnl: 120,
                  fill_price: 1.2,
                },
              ],
            },
          ],
        })}
      />,
    )
    const text = sizingText(container)
    expect(text).toContain('Position pos-1 — open, +$80 unrealized.')
    expect(text).toContain('PROFIT_TARGET realized +$120.')
  })

  it('flags an anomalous position link as broken history', () => {
    const { container } = render(
      <CycleTrace
        detail={detail({
          positions: [{ id: 'pos-9', anomaly: true, position: null, outcomes: [] }],
        })}
      />,
    )
    expect(sizingText(container)).toContain('position pos-9 — not found (broken history)')
  })

  it('reports when nothing is linked to the cycle', () => {
    render(<CycleTrace detail={detail()} />)
    expect(screen.getByText('no order or position linked to this cycle')).toBeInTheDocument()
  })
})
