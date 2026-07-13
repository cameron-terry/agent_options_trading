import { useEffect, useState } from 'react'
import {
  fetchAttribution,
  fetchBias,
  fetchFunnel,
  fetchHitRate,
  type AttributionResponse,
  type BiasResponse,
  type FunnelResponse,
  type HitRateResponse,
  type ReviewFilters,
} from '../api'
import { AttributionByStrategyPanel, AttributionPanel } from './AttributionPanel'
import { BiasPanel } from './BiasPanel'
import { FunnelPanel, RejectionsByRulePanel } from './FunnelPanel'
import { HitRateTable } from './HitRateTable'
import { PerformanceFilters } from './PerformanceFilters'

export function PerformanceScreen() {
  const [filters, setFilters] = useState<ReviewFilters>({})
  const [funnel, setFunnel] = useState<FunnelResponse | null>(null)
  const [hitRate, setHitRate] = useState<HitRateResponse | null>(null)
  const [attribution, setAttribution] = useState<AttributionResponse | null>(null)
  const [bias, setBias] = useState<BiasResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetchFunnel(filters),
      fetchHitRate(filters),
      fetchAttribution(filters),
      fetchBias(filters),
    ])
      .then(([funnelRes, hitRateRes, attributionRes, biasRes]) => {
        if (cancelled) return
        setFunnel(funnelRes)
        setHitRate(hitRateRes)
        setAttribution(attributionRes)
        setBias(biasRes)
        setError(null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [filters])

  // "N cycles · M opened · K closed" summary, right-aligned in the filter
  // row per the design reference. "Closed" is hit_rate's overall.trade_count
  // — the same fully-closed-position definition hit rate and attribution
  // both use (see obs/review.py's _split_outcomes), not a raw outcome count.
  const summary =
    funnel && hitRate
      ? `${funnel.total} cycles · ${funnel.opened} opened · ${hitRate.overall.trade_count} closed`
      : undefined

  return (
    <div className="console-screen">
      {error && <div className="console-error">Failed to load: {error}</div>}
      <PerformanceFilters filters={filters} onChange={setFilters} summary={summary} />

      {funnel && (
        <div className="grid2eq">
          <FunnelPanel funnel={funnel} />
          <RejectionsByRulePanel funnel={funnel} />
        </div>
      )}

      {hitRate && <HitRateTable hitRate={hitRate} />}

      {attribution && (
        <div className="grid2eq">
          <AttributionPanel attribution={attribution} />
          <AttributionByStrategyPanel attribution={attribution} />
        </div>
      )}

      {bias && <BiasPanel bias={bias} />}
    </div>
  )
}
