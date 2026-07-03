import type { PositionSummary } from '../api'
import { formatCurrency, formatPct, formatSignedCurrency } from '../format'

function DistanceBar({ position }: { position: PositionSummary }) {
  const dtt = position.distance_to_trigger
  if (dtt === null) {
    return <span className="distance-bar__na">—</span>
  }
  const clamped = Math.max(0, Math.min(1, dtt.pct))
  return (
    <div className="distance-bar">
      <div className="distance-bar__track">
        <div
          className={`distance-bar__fill distance-bar__fill--${dtt.direction}`}
          style={{ width: `${clamped * 100}%` }}
        />
      </div>
      <span className="distance-bar__label">
        {formatPct(dtt.pct)} → {dtt.direction}
      </span>
    </div>
  )
}

export function PositionsTable({ positions }: { positions: PositionSummary[] }) {
  if (positions.length === 0) {
    return <div className="positions-table positions-table--empty">no open positions</div>
  }

  return (
    <table className="positions-table">
      <thead>
        <tr>
          <th>Underlying</th>
          <th>Strategy</th>
          <th>Qty</th>
          <th>Entry Cr.</th>
          <th>Mark</th>
          <th>Unreal. P&L</th>
          <th>DTE</th>
          <th>Distance to Trigger</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((pos) => (
          <tr key={pos.id}>
            <td className="positions-table__underlying">{pos.underlying}</td>
            <td>{pos.strategy}</td>
            <td>{pos.quantity}</td>
            <td>{formatCurrency(Math.abs(pos.entry_net_amount))}</td>
            <td>{formatCurrency(Math.abs(pos.current_mark))}</td>
            <td className={pos.unrealized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
              {formatSignedCurrency(pos.unrealized_pnl)}
            </td>
            <td>{pos.dte ?? '—'}</td>
            <td>
              <DistanceBar position={pos} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
