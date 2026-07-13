import type { AttributionResponse } from '../api'
import { formatSignedCurrency } from '../format'

export function AttributionPanel({ attribution }: { attribution: AttributionResponse }) {
  const byUnderlying = Object.values(attribution.by_underlying)
  const maxAbs = Math.max(1, ...byUnderlying.map((u) => Math.abs(u.net_pnl)))

  return (
    <div className="panel">
      <h2>
        P&L attribution by underlying <small>pnl_attribution() · realized, closed only</small>
      </h2>
      {byUnderlying.length === 0 ? (
        <div className="review-table--empty">no closed trades yet</div>
      ) : (
        <div className="attribution-bars">
          {byUnderlying.map((u) => {
            const pct = (Math.abs(u.net_pnl) / maxAbs) * 100
            return (
              <div className="attribution-bars__row" key={u.underlying}>
                <span className="attribution-bars__label">{u.underlying}</span>
                <div className="attribution-bars__neg">
                  {u.net_pnl < 0 && (
                    <i className="attribution-bars__fill--neg" style={{ width: `${pct}%` }} />
                  )}
                </div>
                <div className="attribution-bars__pos">
                  {u.net_pnl >= 0 && (
                    <i className="attribution-bars__fill--pos" style={{ width: `${pct}%` }} />
                  )}
                </div>
                <span
                  className={`attribution-bars__value ${u.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}
                >
                  {formatSignedCurrency(u.net_pnl)}
                </span>
              </div>
            )
          })}
        </div>
      )}
      {attribution.open_summary.open_position_count > 0 && (
        <p className="review-table__footnote">
          open positions ({formatSignedCurrency(attribution.open_summary.realized_to_date)}{' '}
          realized-to-date) shown separately, never blended in
        </p>
      )}
    </div>
  )
}

export function AttributionByStrategyPanel({
  attribution,
}: {
  attribution: AttributionResponse
}) {
  const byStrategy = Object.values(attribution.by_strategy)

  return (
    <div className="panel">
      <h2>
        P&L attribution by strategy <small>pnl_attribution()</small>
      </h2>
      {byStrategy.length === 0 ? (
        <div className="review-table--empty">no closed trades yet</div>
      ) : (
        <table className="review-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th className="num">Trades</th>
              <th className="num">Net P&L</th>
            </tr>
          </thead>
          <tbody>
            {byStrategy.map((s) => (
              <tr key={s.strategy}>
                <td>{s.strategy}</td>
                <td className="num">{s.trade_count}</td>
                <td className={`num ${s.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                  {formatSignedCurrency(s.net_pnl)}
                </td>
              </tr>
            ))}
            <tr className="review-table__total">
              <td>TOTAL</td>
              <td className="num"></td>
              <td
                className={`num ${attribution.total_realized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}
              >
                {formatSignedCurrency(attribution.total_realized_pnl)}
              </td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  )
}
