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
