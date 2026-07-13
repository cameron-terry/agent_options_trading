// Typed client for the WP-9.2 Overview API. Field names mirror
// options_agent/ui/overview.py's Pydantic models exactly (FastAPI serializes
// with no aliasing) — keep the two in sync by hand until a schema generator
// is wired up.

export type KillSwitchState = 'NONE' | 'HALT' | 'FLATTEN'

export interface EquityTile {
  value: number | null
  as_of: string | null
}

export interface RealizedPnlTile {
  total: number
  closed_count: number
  hit_count: number
}

export interface UnrealizedPnlTile {
  total: number
  open_position_count: number
}

export interface CyclesTodayTile {
  total: number
  by_action: Record<string, number>
}

export interface Tiles {
  account_equity: EquityTile
  realized_pnl: RealizedPnlTile
  unrealized_pnl: UnrealizedPnlTile
  cycles_today: CyclesTodayTile
}

export interface EquityCurvePoint {
  timestamp: string
  cumulative_realized_pnl: number
  equity: number | null
}

export interface ActivityItem {
  timestamp: string
  kind: 'journal' | 'outcome'
  action: string
  headline: string
  cycle_id: string | null
  position_id: string | null
}

export interface OverviewResponse {
  kill_switch: { state: KillSwitchState }
  tiles: Tiles
  equity_curve: EquityCurvePoint[]
  activity: ActivityItem[]
  mode: 'paper' | 'live'
}

export interface DistanceToTrigger {
  direction: 'stop' | 'target'
  pct: number
}

export interface PositionSummary {
  id: string
  underlying: string
  strategy: string
  strikes: string
  quantity: number
  entry_net_amount: number
  current_mark: number
  marked_at: string
  unrealized_pnl: number
  dte: number | null
  distance_to_trigger: DistanceToTrigger | null
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`${url} → ${res.status}`)
  }
  return (await res.json()) as T
}

export function fetchOverview(): Promise<OverviewResponse> {
  return getJSON<OverviewResponse>('/api/overview')
}

export function fetchPositions(): Promise<PositionSummary[]> {
  return getJSON<PositionSummary[]>('/api/positions')
}

// --- WP-9.3: Decision explorer ------------------------------------------
// Field names mirror options_agent/ui/cycles.py's Pydantic models.

export type ActionTaken =
  | 'OPENED'
  | 'CLOSED'
  | 'ROLLED'
  | 'NO_ACTION_GATED'
  | 'NO_ACTION_AGENT'
  | 'SIZED_TO_ZERO'
  | 'REJECTED'
  | 'EXECUTION_FAILED'

export interface CycleListItem {
  cycle_id: string
  timestamp: string
  action_taken: ActionTaken
  underlying: string | null
  strategy: string | null
  conviction: number | null
}

export interface CycleFilters {
  symbol?: string
  action_type?: ActionTaken
  date_from?: string
  date_to?: string
}

export interface Leg {
  right: 'call' | 'put'
  side: 'buy' | 'sell'
  strike: number
  expiration: string
  ratio: number
}

export interface ExitPlan {
  profit_target_pct: number
  stop_loss_max_loss_fraction: number
  time_stop_dte: number
}

export interface TradeProposal {
  action: 'OPEN' | 'CLOSE' | 'ROLL' | 'NO_ACTION'
  underlying: string
  strategy: string
  legs: Leg[]
  thesis: string
  iv_rationale: string
  catalyst_check: string
  conviction: number
  est_max_loss: number
  est_max_profit: number
  breakevens: number[]
  net_delta: number
  net_theta: number
  net_vega: number
  exit_plan: ExitPlan
  informed_by: string[]
}

export interface ToolCallRecord {
  tool_name: string
  tool_input: Record<string, unknown>
  result_json: string
}

export interface RejectionReason {
  rule_id: string
  severity: 'error' | 'warning'
  human_message: string
  field_affected: string | null
  observed: number | null
  limit: number | null
}

export interface ValidationResult {
  passed: boolean
  reasons: RejectionReason[]
}

export interface SizingResult {
  contracts: number
  sized_max_loss: number
  sized_max_profit: number
  risk_budget_used: number
  binding_constraint: string | null
  capped_to_zero: boolean
}

export interface LinkedPosition {
  id: string
  underlying: string
  strategy: string
  quantity: number
  entry_net_amount: number
  current_mark: number
  unrealized_pnl: number
  realized_pnl: number | null
  status: string
}

export interface LinkedOutcome {
  id: string
  event_type: string
  recorded_at: string
  realized_pnl: number
  fill_price: number | null
}

export interface PositionLink {
  id: string
  anomaly: boolean
  position: LinkedPosition | null
  outcomes: LinkedOutcome[]
}

export interface LinkedOrder {
  id: string
  role: string
  status: string
  submitted_at: string
  filled_at: string | null
  net_fill_price: number | null
  filled_qty: number
}

export interface OrderLink {
  id: string
  anomaly: boolean
  order: LinkedOrder | null
}

export interface CycleDetail {
  cycle_id: string
  timestamp: string
  action_taken: ActionTaken
  underlying: string | null
  strategy: string | null
  conviction: number | null
  model_id: string
  prompt_version: string
  limits_version: string
  context_hash: string
  proposal: TradeProposal | null
  tool_calls_transcript: ToolCallRecord[]
  validation_result: ValidationResult | null
  rejection_rule_ids: string[]
  sizing_result: SizingResult | null
  positions: PositionLink[]
  orders: OrderLink[]
}

export function fetchCycles(filters: CycleFilters = {}): Promise<CycleListItem[]> {
  const params = new URLSearchParams()
  if (filters.symbol) params.set('symbol', filters.symbol)
  if (filters.action_type) params.set('action_type', filters.action_type)
  if (filters.date_from) params.set('date_from', filters.date_from)
  if (filters.date_to) params.set('date_to', filters.date_to)
  const qs = params.toString()
  return getJSON<CycleListItem[]>(`/api/cycles${qs ? `?${qs}` : ''}`)
}

export function fetchCycleDetail(cycleId: string): Promise<CycleDetail> {
  return getJSON<CycleDetail>(`/api/cycles/${encodeURIComponent(cycleId)}`)
}

// --- WP-9.5: Performance & bias -----------------------------------------
// Field names mirror options_agent/ui/review.py's Pydantic models. Fields
// that can be NaN in the underlying obs/review.py dataclasses (hit_rate,
// avg_win, avg_loss, expectancy, mean_net_delta) are nulled server-side —
// null here always means "not computed / insufficient", never zero.

export interface ReviewFilters {
  since?: string
  prompt_version?: string
}

function reviewParams(filters: ReviewFilters): string {
  const params = new URLSearchParams()
  if (filters.since) params.set('since', filters.since)
  if (filters.prompt_version) params.set('prompt_version', filters.prompt_version)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export interface RejectionRuleCount {
  rule_id: string
  count: number
}

export interface FunnelResponse {
  total: number
  gated: number
  reasoned: number
  no_action_agent: number
  proposed: number
  rejected: number
  sized_to_zero: number
  execution_failed: number
  opened: number
  rejections_by_rule: RejectionRuleCount[]
}

export interface StrategyStatsOut {
  strategy: string
  trade_count: number
  hit_count: number
  miss_count: number
  hit_rate: number | null
  avg_win: number | null
  avg_loss: number | null
  expectancy: number | null
  total_pnl: number
  sufficient: boolean
}

export interface OpenSummaryOut {
  open_position_count: number
  realized_to_date: number
}

export interface HitRateResponse {
  by_strategy: Record<string, StrategyStatsOut>
  overall: StrategyStatsOut
  open_summary: OpenSummaryOut
  min_sample_size: number
}

export interface UnderlyingPnLOut {
  underlying: string
  net_pnl: number
  trade_count: number
}

export interface StrategyPnLOut {
  strategy: string
  net_pnl: number
  trade_count: number
}

export interface AttributionResponse {
  by_underlying: Record<string, UnderlyingPnLOut>
  by_strategy: Record<string, StrategyPnLOut>
  total_realized_pnl: number
  open_summary: OpenSummaryOut
}

export interface DeltaSkewOut {
  sample_size: number
  mean_net_delta: number | null
  sufficient: boolean
  direction: 'bullish' | 'bearish' | 'neutral' | 'insufficient_data'
}

export interface DirectionWinRateOut {
  direction: string
  sample_size: number
  sufficient: boolean
  hit_rate: number | null
  avg_win: number | null
  avg_loss: number | null
  expectancy: number | null
  total_pnl: number
}

export interface EventProximityOut {
  near_catalyst: DirectionWinRateOut
  baseline: DirectionWinRateOut
}

export interface BiasResponse {
  min_sample_size: number
  window_start: string | null
  delta_skew: DeltaSkewOut
  by_direction: Record<string, DirectionWinRateOut>
  event_proximity: EventProximityOut
}

export function fetchFunnel(filters: ReviewFilters = {}): Promise<FunnelResponse> {
  return getJSON<FunnelResponse>(`/api/review/funnel${reviewParams(filters)}`)
}

export function fetchHitRate(filters: ReviewFilters = {}): Promise<HitRateResponse> {
  return getJSON<HitRateResponse>(`/api/review/hit-rate${reviewParams(filters)}`)
}

export function fetchAttribution(filters: ReviewFilters = {}): Promise<AttributionResponse> {
  return getJSON<AttributionResponse>(`/api/review/attribution${reviewParams(filters)}`)
}

export function fetchBias(filters: ReviewFilters = {}): Promise<BiasResponse> {
  return getJSON<BiasResponse>(`/api/review/bias${reviewParams(filters)}`)
}

export function fetchPromptVersions(): Promise<string[]> {
  return getJSON<string[]>('/api/review/prompt-versions')
}

// --- WP-9.7: Kill-switch console + alert-delivery health -----------------
// Field names mirror options_agent/ui/killswitch.py's Pydantic models.

export type KillSwitchActionType = 'HALT' | 'FLATTEN' | 'RESUME'

export interface KillSwitchHistoryEntry {
  id: string
  state: KillSwitchState
  set_by: string
  reason: string
  created_at: string
}

export interface AlertFailureItem {
  id: string
  event_type: string
  severity: string
  detail: string
  attempted_at: string
  attempts: number
  last_error: string
}

export interface KillSwitchStatusResponse {
  state: KillSwitchState
  history: KillSwitchHistoryEntry[]
  alert_failures: AlertFailureItem[]
}

export interface KillSwitchActionRequest {
  action: KillSwitchActionType
  reason: string
  confirmation?: string
}

export function fetchKillSwitchStatus(): Promise<KillSwitchStatusResponse> {
  return getJSON<KillSwitchStatusResponse>('/api/killswitch')
}

function extractErrorDetail(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string }
      if (typeof first.msg === 'string') return first.msg
    }
  }
  return `POST /api/killswitch → ${status}`
}

export async function postKillSwitchAction(
  body: KillSwitchActionRequest,
): Promise<KillSwitchHistoryEntry> {
  const res = await fetch('/api/killswitch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const errBody = await res.json().catch(() => null)
    throw new Error(extractErrorDetail(errBody, res.status))
  }
  return (await res.json()) as KillSwitchHistoryEntry
}

// --- WP-9.9: Ask the journal ---------------------------------------------
// POST /api/ask streams Server-Sent Events; field names mirror
// options_agent/ui/ask.py's event payloads exactly.

export interface AskHistoryTurn {
  question: string
  answer_text: string
}

export interface AskQueryResultPayload {
  sql: string
  columns: string[]
  rows: Record<string, unknown>[]
  truncated: boolean
  row_cap: number
}

export interface AskQueryErrorPayload {
  sql: string
  error: string
}

export interface AskAnswerPayload {
  answer_text: string
  executed_sql: string[]
  cited_cycle_ids: string[]
  tables_touched: string[]
}

export interface AskStreamHandlers {
  onQueryStarted?: (sql: string) => void
  onQueryResult?: (payload: AskQueryResultPayload) => void
  onQueryError?: (payload: AskQueryErrorPayload) => void
  onAnswer?: (payload: AskAnswerPayload) => void
  onError?: (message: string) => void
}

function dispatchSseFrame(frame: string, handlers: AskStreamHandlers): void {
  let event = 'message'
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7)
    else if (line.startsWith('data: ')) data = line.slice(6)
  }
  if (!data) return

  const payload = JSON.parse(data)
  switch (event) {
    case 'query_started':
      handlers.onQueryStarted?.(payload.sql)
      break
    case 'query_result':
      handlers.onQueryResult?.(payload as AskQueryResultPayload)
      break
    case 'query_error':
      handlers.onQueryError?.(payload as AskQueryErrorPayload)
      break
    case 'answer':
      handlers.onAnswer?.(payload as AskAnswerPayload)
      break
    case 'error':
      handlers.onError?.(payload.message)
      break
  }
}

// Manual SSE parsing over fetch()'s ReadableStream rather than EventSource
// (WP-9.9 decision): EventSource is GET-only with no body, and a question
// is an arbitrary-length string that needs to go in a POST body, not a
// query param.
export async function streamAsk(
  question: string,
  history: AskHistoryTurn[],
  handlers: AskStreamHandlers,
): Promise<void> {
  let res: Response
  try {
    res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history }),
    })
  } catch {
    handlers.onError?.('Network error contacting /api/ask')
    return
  }
  if (!res.ok || !res.body) {
    handlers.onError?.(`/api/ask → ${res.status}`)
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let sepIndex = buffer.indexOf('\n\n')
    while (sepIndex !== -1) {
      const frame = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      dispatchSseFrame(frame, handlers)
      sepIndex = buffer.indexOf('\n\n')
    }
  }
}
