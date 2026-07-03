import type { ReactNode } from 'react'
import type { Tiles as TilesData } from '../api'
import { formatCurrency, formatSignedCurrency } from '../format'

function Tile({
  label,
  value,
  detail,
}: {
  label: string
  value: ReactNode
  detail: ReactNode
}) {
  return (
    <div className="tile">
      <div className="tile__label">{label}</div>
      <div className="tile__value">{value}</div>
      <div className="tile__detail">{detail}</div>
    </div>
  )
}

export function Tiles({ tiles }: { tiles: TilesData }) {
  const { account_equity, realized_pnl, unrealized_pnl, cycles_today } = tiles

  const actionSummary = Object.entries(cycles_today.by_action)
    .map(([action, count]) => `${count} ${action.toLowerCase()}`)
    .join(' · ')

  return (
    <div className="tiles">
      <Tile
        label="Account Equity"
        value={account_equity.value === null ? '—' : formatCurrency(account_equity.value)}
        detail={account_equity.as_of === null ? 'insufficient data' : 'as of last cycle'}
      />
      <Tile
        label="Realized P&L"
        value={
          <span className={realized_pnl.total >= 0 ? 'pnl-positive' : 'pnl-negative'}>
            {formatSignedCurrency(realized_pnl.total)}
          </span>
        }
        detail={`${realized_pnl.closed_count} closed · ${realized_pnl.hit_count} hits`}
      />
      <Tile
        label="Unrealized P&L"
        value={
          <span className={unrealized_pnl.total >= 0 ? 'pnl-positive' : 'pnl-negative'}>
            {formatSignedCurrency(unrealized_pnl.total)}
          </span>
        }
        detail={`${unrealized_pnl.open_position_count} open positions`}
      />
      <Tile
        label="Cycles Today"
        value={cycles_today.total}
        detail={actionSummary || 'no cycles yet'}
      />
    </div>
  )
}
