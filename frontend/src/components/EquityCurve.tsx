import { useState, type MouseEvent } from 'react'
import type { EquityCurvePoint } from '../api'
import { formatCurrency, formatTime } from '../format'

const WIDTH = 600
const HEIGHT = 160
const PAD_Y_TOP = 22
const PAD_Y_BOTTOM = 10
const TICK_COUNT = 4

export function EquityCurve({ points }: { points: EquityCurvePoint[] }) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)

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
    const y =
      HEIGHT -
      PAD_Y_BOTTOM -
      ((value - min) / range) * (HEIGHT - PAD_Y_TOP - PAD_Y_BOTTOM)
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

  const ticks = Array.from({ length: TICK_COUNT }, (_, i) => {
    const value = max - (i / (TICK_COUNT - 1)) * range
    const [, y] = toXY(value, 0)
    return { value, y }
  })

  const handleMove = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const relX = ((event.clientX - rect.left) / rect.width) * WIDTH
    const index = Math.round((relX / WIDTH) * (series.length - 1))
    setHoverIndex(Math.max(0, Math.min(series.length - 1, index)))
  }

  const hovered = hoverIndex !== null ? points[hoverIndex] : null
  const [hoverX, hoverY] = hoverIndex !== null ? toXY(series[hoverIndex], hoverIndex) : [0, 0]

  return (
    <div className="equity-curve">
      <div className="equity-curve__row">
        <div className="equity-curve__axis" style={{ height: HEIGHT }}>
          {ticks.map((t) => (
            <span key={t.value}>{formatCurrency(t.value)}</span>
          ))}
        </div>
        <div className="equity-curve__chart-wrap">
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            preserveAspectRatio="none"
            className="equity-curve__svg"
            onMouseMove={handleMove}
            onMouseLeave={() => setHoverIndex(null)}
          >
            <path d={areaPath} className="equity-curve__area" />
            {ticks.map((t) => (
              <line
                key={t.value}
                x1={0}
                y1={t.y}
                x2={WIDTH}
                y2={t.y}
                className="equity-curve__gridline"
              />
            ))}
            <path d={linePath} className="equity-curve__line" />
            <circle cx={lastX} cy={lastY} r={4} className="equity-curve__dot" />
            {hoverIndex !== null && (
              <circle cx={hoverX} cy={hoverY} r={4} className="equity-curve__dot" />
            )}
            <rect x={0} y={0} width={WIDTH} height={HEIGHT} className="equity-curve__hit" />
          </svg>
          {hoverIndex === null && (
            <div
              className="equity-curve__endlabel"
              style={{
                left: `${(lastX / WIDTH) * 100}%`,
                top: `${(lastY / HEIGHT) * 100}%`,
              }}
            >
              {formatCurrency(series[series.length - 1])}
            </div>
          )}
          {hovered && (
            <div
              className="equity-curve__tip"
              style={{
                left: `${(hoverX / WIDTH) * 100}%`,
                top: `${(hoverY / HEIGHT) * 100}%`,
              }}
            >
              {formatCurrency(hovered.equity ?? hovered.cumulative_realized_pnl)}
              <small>{formatTime(hovered.timestamp)}</small>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
