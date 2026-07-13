import type { BiasResponse, DirectionWinRateOut } from '../api'
import { formatPct, formatSignedCurrency } from '../format'

// Skew meter spans [-0.5, +0.5] net delta, clamped — matches the design
// reference's band exactly (delta values outside this range are rare given
// the validator's PORTFOLIO_DELTA_BAND rule, but clamp defensively anyway).
const SKEW_MIN = -0.5
const SKEW_MAX = 0.5

function skewPct(meanNetDelta: number): number {
  const clamped = Math.max(SKEW_MIN, Math.min(SKEW_MAX, meanNetDelta))
  return ((clamped - SKEW_MIN) / (SKEW_MAX - SKEW_MIN)) * 100
}

function DeltaSkewMeter({ skew }: { skew: BiasResponse['delta_skew'] }) {
  if (!skew.sufficient || skew.mean_net_delta === null) {
    return (
      <div className="skew-meter">
        <div className="skew-meter__band" />
        <div className="skew-meter__zero" />
        <p className="review-table__na">
          insufficient data (n={skew.sample_size})
        </p>
      </div>
    )
  }
  const pct = skewPct(skew.mean_net_delta)
  return (
    <div className="skew-meter">
      <div className="skew-meter__track">
        <div className="skew-meter__band" />
        <div className="skew-meter__zero" />
        <div className="skew-meter__pin" style={{ left: `${pct}%` }} />
        <div className="skew-meter__cap" style={{ left: `${pct}%` }}>
          {skew.mean_net_delta >= 0 ? '+' : ''}
          {skew.mean_net_delta.toFixed(2)}
        </div>
      </div>
      <div className="skew-meter__labels">
        <span>−0.5 bearish</span>
        <span>0 neutral</span>
        <span>+0.5 bullish</span>
      </div>
    </div>
  )
}

function cohortRow(label: string, stats: DirectionWinRateOut) {
  return (
    <tr key={label}>
      <td>{label}</td>
      <td className="num">{stats.sample_size}</td>
      {stats.sufficient ? (
        <>
          <td className="num">{formatPct(stats.hit_rate as number)}</td>
          <td className={`num ${(stats.expectancy as number) >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
            {formatSignedCurrency(stats.expectancy as number)}
          </td>
          <td></td>
        </>
      ) : (
        <>
          <td className="num review-table__na">—</td>
          <td className="num review-table__na">—</td>
          <td>
            <span className="action-chip action-chip--muted">insufficient</span>
          </td>
        </>
      )}
    </tr>
  )
}

export function BiasPanel({ bias }: { bias: BiasResponse }) {
  return (
    <div className="panel">
      <h2>
        Bias monitor <small>detect_bias() — evidence, never action</small>
      </h2>
      <DeltaSkewMeter skew={bias.delta_skew} />
      <table className="review-table" style={{ marginTop: 12 }}>
        <thead>
          <tr>
            <th>Cohort</th>
            <th className="num">n</th>
            <th className="num">Hit rate</th>
            <th className="num">Expectancy</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {bias.by_direction.bullish &&
            cohortRow('bullish (Δ>0 at open)', bias.by_direction.bullish)}
          {bias.by_direction.bearish &&
            cohortRow('bearish (Δ<0 at open)', bias.by_direction.bearish)}
          {cohortRow('near catalyst', bias.event_proximity.near_catalyst)}
        </tbody>
      </table>
    </div>
  )
}
