import type { EquityCurvePoint } from '../api'
import { formatCurrency } from '../format'

const WIDTH = 600
const HEIGHT = 180
const PAD_Y = 12

export function EquityCurve({ points }: { points: EquityCurvePoint[] }) {
  // Prefer the dollar-anchored series; falls back to the always-available
  // cumulative_realized_pnl when no account_equity reading exists yet.
  const series = points.map((p) => p.equity ?? p.cumulative_realized_pnl)

  if (series.length < 2) {
    return (
      <div className="equity-curve equity-curve--empty">
        insufficient data — need at least two closed positions
      </div>
    )
  }

  const min = Math.min(...series)
  const max = Math.max(...series)
  const range = max - min || 1

  const toXY = (value: number, i: number): [number, number] => {
    const x = (i / (series.length - 1)) * WIDTH
    const y = HEIGHT - PAD_Y - ((value - min) / range) * (HEIGHT - 2 * PAD_Y)
    return [x, y]
  }

  const linePath = series
    .map((v, i) => {
      const [x, y] = toXY(v, i)
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  const [lastX, lastY] = toXY(series[series.length - 1], series.length - 1)
  const areaPath = `${linePath} L${lastX.toFixed(1)},${HEIGHT} L0,${HEIGHT} Z`

  return (
    <div className="equity-curve">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" className="equity-curve__svg">
        <path d={areaPath} className="equity-curve__area" />
        <path d={linePath} className="equity-curve__line" />
        <circle cx={lastX} cy={lastY} r={4} className="equity-curve__dot" />
      </svg>
      <div className="equity-curve__labels">
        <span>{formatCurrency(min)}</span>
        <span className="equity-curve__latest">{formatCurrency(series[series.length - 1])}</span>
        <span>{formatCurrency(max)}</span>
      </div>
    </div>
  )
}
