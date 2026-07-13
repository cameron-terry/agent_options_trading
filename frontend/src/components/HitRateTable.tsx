import type { HitRateResponse, StrategyStatsOut } from '../api'
import { formatPct, formatSignedCurrency } from '../format'

function pnlCell(value: number | null) {
  if (value === null) return <td className="num review-table__na">—</td>
  return (
    <td className={`num ${value >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
      {formatSignedCurrency(value)}
    </td>
  )
}

function StatsRow({
  stats,
  label,
  minSampleSize,
}: {
  stats: StrategyStatsOut
  label?: string
  minSampleSize: number
}) {
  return (
    <tr className={label ? 'review-table__total' : undefined}>
      <td>{label ?? stats.strategy}</td>
      <td className="num">{stats.trade_count}</td>
      <td className="num">
        {stats.hit_rate === null ? (
          <span className="review-table__na">—</span>
        ) : (
          formatPct(stats.hit_rate)
        )}
      </td>
      {pnlCell(stats.avg_win)}
      {pnlCell(stats.avg_loss)}
      {pnlCell(stats.expectancy)}
      <td className={`num ${stats.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
        {formatSignedCurrency(stats.total_pnl)}
      </td>
      <td>
        {!stats.sufficient && (
          <span className="action-chip action-chip--muted">
            n &lt; {minSampleSize} · insufficient
          </span>
        )}
      </td>
    </tr>
  )
}

export function HitRateTable({ hitRate }: { hitRate: HitRateResponse }) {
  const strategies = Object.values(hitRate.by_strategy)

  return (
    <div className="panel">
      <h2>
        Hit rate by strategy{' '}
        <small>
          hit_rate_by_strategy() — hit = realized_pnl &gt; 0, never shown without
          expectancy
        </small>
      </h2>
      {strategies.length === 0 ? (
        <div className="review-table--empty">no closed trades yet</div>
      ) : (
        <table className="review-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th className="num">Closed</th>
              <th className="num">Hit rate</th>
              <th className="num">Avg win</th>
              <th className="num">Avg loss</th>
              <th className="num">Expectancy</th>
              <th className="num">Total P&L</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {strategies.map((stats) => (
              <StatsRow
                key={stats.strategy}
                stats={stats}
                minSampleSize={hitRate.min_sample_size}
              />
            ))}
            <StatsRow
              stats={hitRate.overall}
              label="TOTAL"
              minSampleSize={hitRate.min_sample_size}
            />
          </tbody>
        </table>
      )}
      {hitRate.open_summary.open_position_count > 0 && (
        <p className="review-table__footnote">
          open: {hitRate.open_summary.open_position_count} position(s), realized-to-date{' '}
          {formatSignedCurrency(hitRate.open_summary.realized_to_date)}
        </p>
      )}
    </div>
  )
}
