import { http, HttpResponse } from 'msw'
import type {
  AttributionResponse,
  BiasResponse,
  CycleDetail,
  CycleListItem,
  FunnelResponse,
  HitRateResponse,
  KillSwitchStatusResponse,
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

export const funnelFixture: FunnelResponse = {
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
}

export const hitRateFixture: HitRateResponse = {
  by_strategy: {
    bull_put_spread: {
      strategy: 'bull_put_spread',
      trade_count: 2,
      hit_count: 1,
      miss_count: 1,
      hit_rate: null,
      avg_win: null,
      avg_loss: null,
      expectancy: null,
      total_pnl: 70,
      sufficient: false,
    },
  },
  overall: {
    strategy: '_all',
    trade_count: 2,
    hit_count: 1,
    miss_count: 1,
    hit_rate: null,
    avg_win: null,
    avg_loss: null,
    expectancy: null,
    total_pnl: 70,
    sufficient: false,
  },
  open_summary: { open_position_count: 0, realized_to_date: 0 },
  min_sample_size: 10,
}

export const attributionFixture: AttributionResponse = {
  by_underlying: {
    SPY: { underlying: 'SPY', net_pnl: 150, trade_count: 1 },
    QQQ: { underlying: 'QQQ', net_pnl: -80, trade_count: 1 },
  },
  by_strategy: {
    bull_put_spread: { strategy: 'bull_put_spread', net_pnl: 150, trade_count: 1 },
    iron_condor: { strategy: 'iron_condor', net_pnl: -80, trade_count: 1 },
  },
  total_realized_pnl: 70,
  open_summary: { open_position_count: 0, realized_to_date: 0 },
}

export const biasFixture: BiasResponse = {
  min_sample_size: 10,
  window_start: null,
  delta_skew: {
    sample_size: 2,
    mean_net_delta: null,
    sufficient: false,
    direction: 'insufficient_data',
  },
  by_direction: {
    bullish: {
      direction: 'bullish',
      sample_size: 2,
      sufficient: false,
      hit_rate: null,
      avg_win: null,
      avg_loss: null,
      expectancy: null,
      total_pnl: 70,
    },
    bearish: {
      direction: 'bearish',
      sample_size: 0,
      sufficient: false,
      hit_rate: null,
      avg_win: null,
      avg_loss: null,
      expectancy: null,
      total_pnl: 0,
    },
  },
  event_proximity: {
    near_catalyst: {
      direction: 'near_catalyst',
      sample_size: 1,
      sufficient: false,
      hit_rate: null,
      avg_win: null,
      avg_loss: null,
      expectancy: null,
      total_pnl: -80,
    },
    baseline: {
      direction: 'baseline',
      sample_size: 1,
      sufficient: false,
      hit_rate: null,
      avg_win: null,
      avg_loss: null,
      expectancy: null,
      total_pnl: 150,
    },
  },
}

export const promptVersionsFixture: string[] = ['v1.0.0', 'v2.0.0', 'v2.1.0']

export const killSwitchStatusFixture: KillSwitchStatusResponse = {
  state: 'NONE',
  history: [
    {
      id: 'ks-2',
      state: 'NONE',
      set_by: 'console',
      reason: 'issue resolved',
      created_at: '2026-07-12T15:00:00Z',
    },
    {
      id: 'ks-1',
      state: 'HALT',
      set_by: 'console',
      reason: 'reconcile mismatch',
      created_at: '2026-07-12T14:00:00Z',
    },
  ],
  alert_failures: [
    {
      id: 'af-1',
      event_type: 'AlertEventType.FILL',
      severity: 'AlertSeverity.WARN',
      detail: 'Discord webhook timed out',
      attempted_at: '2026-07-12T13:00:00Z',
      attempts: 2,
      last_error: 'HTTPError 503',
    },
  ],
}

// --- WP-9.9: Ask the journal — SSE response builder -----------------------
// POST /api/ask streams `event: X\ndata: Y\n\n` frames; MSW v2 supports a
// ReadableStream response body, so this builds one from a plain list of
// (event, data) pairs — the same shape options_agent/ui/ask.py's
// ask_event_stream() emits.

export type SseFrame = [event: string, data: unknown]

export function sseResponse(frames: SseFrame[]): Response {
  const body = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder()
      for (const [event, data] of frames) {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
      }
      controller.close()
    },
  })
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

export const askAnswerFixture: SseFrame[] = [
  ['query_started', { sql: "SELECT cycle_id FROM journal_records WHERE strategy='bull_put_spread'" }],
  [
    'query_result',
    {
      sql: "SELECT cycle_id FROM journal_records WHERE strategy='bull_put_spread'",
      columns: ['cycle_id'],
      rows: [{ cycle_id: 'cyc-1' }, { cycle_id: 'cyc-2' }],
      truncated: false,
      row_cap: 500,
    },
  ],
  [
    'answer',
    {
      answer_text: '2 bull put spreads opened this window.',
      executed_sql: ["SELECT cycle_id FROM journal_records WHERE strategy='bull_put_spread'"],
      cited_cycle_ids: ['cyc-1', 'cyc-2'],
      tables_touched: ['journal_records'],
    },
  ],
]

export const handlers = [
  http.get('/api/overview', () => HttpResponse.json(overviewFixture)),
  http.get('/api/positions', () => HttpResponse.json(positionsFixture)),
  http.get('/api/cycles', () => HttpResponse.json(cyclesFixture)),
  http.get('/api/cycles/:cycleId', ({ params }) =>
    HttpResponse.json({ ...cycleDetailFixture, cycle_id: String(params.cycleId) }),
  ),
  http.get('/api/review/funnel', () => HttpResponse.json(funnelFixture)),
  http.get('/api/review/hit-rate', () => HttpResponse.json(hitRateFixture)),
  http.get('/api/review/attribution', () => HttpResponse.json(attributionFixture)),
  http.get('/api/review/bias', () => HttpResponse.json(biasFixture)),
  http.get('/api/review/prompt-versions', () => HttpResponse.json(promptVersionsFixture)),
  http.get('/api/killswitch', () => HttpResponse.json(killSwitchStatusFixture)),
  http.post('/api/killswitch', async ({ request }) => {
    const body = (await request.json()) as {
      action: 'HALT' | 'FLATTEN' | 'RESUME'
      reason: string
      confirmation?: string
    }
    const newState = body.action === 'RESUME' ? 'NONE' : body.action
    return HttpResponse.json({
      id: 'ks-new',
      state: newState,
      set_by: 'console',
      reason: body.reason,
      created_at: '2026-07-13T10:00:00Z',
    })
  }),
  http.post('/api/ask', () => sseResponse(askAnswerFixture)),
]
