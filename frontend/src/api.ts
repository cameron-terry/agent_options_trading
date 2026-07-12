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
