import { http, HttpResponse } from 'msw'
import type {
  CycleDetail,
  CycleListItem,
  OverviewResponse,
  PositionSummary,
} from '../../api'

// Minimal but well-formed fixtures mirroring the FastAPI response shapes.
// Individual tests override these with server.use(...) when they need to
// assert on query params, error codes, or specific payloads.

export const overviewFixture: OverviewResponse = {
  kill_switch: { state: 'NONE' },
  tiles: {
    account_equity: { value: 100000, as_of: '2026-07-12T14:00:00Z' },
    realized_pnl: { total: 1250, closed_count: 4, hit_count: 3 },
    unrealized_pnl: { total: -320, open_position_count: 2 },
    cycles_today: { total: 6, by_action: { OPENED: 1, NO_ACTION_AGENT: 5 } },
  },
  equity_curve: [
    { timestamp: '2026-07-11T20:00:00Z', cumulative_realized_pnl: 0, equity: 98750 },
    { timestamp: '2026-07-12T20:00:00Z', cumulative_realized_pnl: 1250, equity: 100000 },
  ],
  activity: [
    {
      timestamp: '2026-07-12T14:05:00Z',
      kind: 'journal',
      action: 'OPEN',
      headline: 'OPENED SPY iron condor',
      cycle_id: 'cyc-1',
      position_id: null,
    },
  ],
  mode: 'paper',
}

export const positionsFixture: PositionSummary[] = [
  {
    id: 'pos-1',
    underlying: 'SPY',
    strategy: 'iron_condor',
    strikes: '520/525/580/585',
    quantity: 2,
    entry_net_amount: -180,
    current_mark: -140,
    marked_at: '2026-07-12T14:00:00Z',
    unrealized_pnl: 80,
    dte: 21,
    distance_to_trigger: { direction: 'target', pct: 0.4 },
  },
]

export const cyclesFixture: CycleListItem[] = [
  {
    cycle_id: 'cyc-1',
    timestamp: '2026-07-12T14:05:00Z',
    action_taken: 'OPENED',
    underlying: 'SPY',
    strategy: 'iron_condor',
    conviction: 0.72,
  },
]

export const cycleDetailFixture: CycleDetail = {
  cycle_id: 'cyc-1',
  timestamp: '2026-07-12T14:05:00Z',
  action_taken: 'OPENED',
  underlying: 'SPY',
  strategy: 'iron_condor',
  conviction: 0.72,
  model_id: 'claude-opus-4-8',
  prompt_version: 'v3',
  limits_version: 'v2',
  context_hash: 'abc123',
  proposal: null,
  tool_calls_transcript: [],
  validation_result: null,
  rejection_rule_ids: [],
  sizing_result: null,
  positions: [],
  orders: [],
}

export const handlers = [
  http.get('/api/overview', () => HttpResponse.json(overviewFixture)),
  http.get('/api/positions', () => HttpResponse.json(positionsFixture)),
  http.get('/api/cycles', () => HttpResponse.json(cyclesFixture)),
  http.get('/api/cycles/:cycleId', ({ params }) =>
    HttpResponse.json({ ...cycleDetailFixture, cycle_id: String(params.cycleId) }),
  ),
]
