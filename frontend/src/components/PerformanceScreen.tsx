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
import { PerformanceCompare } from './PerformanceCompare'
import { PerformanceFilters } from './PerformanceFilters'

export function PerformanceScreen() {
  const [filters, setFilters] = useState<ReviewFilters>({})
  const [compareMode, setCompareMode] = useState(false)
  const [funnel, setFunnel] = useState<FunnelResponse | null>(null)
  const [hitRate, setHitRate] = useState<HitRateResponse | null>(null)
  const [attribution, setAttribution] = useState<AttributionResponse | null>(null)
  const [bias, setBias] = useState<BiasResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // While comparing, each column picks its own prompt_version — the top
    // filter's leftover selection has no coordinated meaning here, so the
    // funnel panel and the "N cycles" summary always reflect all versions
    // regardless of what it was last set to (rather than silently scoping
    // the summary to whatever single version happened to be selected before
    // Compare was toggled on). Attribution/bias aren't rendered in compare
    // mode at all, so skip fetching them entirely rather than wasting two
    // requests on every filter change.
    const summaryFilters = compareMode ? { since: filters.since } : filters
    Promise.all([
      fetchFunnel(summaryFilters),
      fetchHitRate(summaryFilters),
      compareMode ? Promise.resolve(null) : fetchAttribution(filters),
      compareMode ? Promise.resolve(null) : fetchBias(filters),
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
  }, [filters, compareMode])

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
      <PerformanceFilters
        filters={filters}
        onChange={setFilters}
        summary={summary}
        disablePromptVersion={compareMode}
      />

      <label className="compare-toggle">
        <input
          type="checkbox"
          checked={compareMode}
          onChange={(e) => setCompareMode(e.target.checked)}
        />
        Compare prompt versions
      </label>

      {funnel && (
        <div className="grid2eq">
          <FunnelPanel funnel={funnel} />
          <RejectionsByRulePanel funnel={funnel} />
        </div>
      )}
      {compareMode && (
        <p className="review-table__footnote">
          funnel is not filterable by prompt_version — cycle_funnel() has no such filter, so
          the panel above reflects all versions regardless of the pickers below.
        </p>
      )}

      {compareMode ? (
        <PerformanceCompare since={filters.since} />
      ) : (
        <>
          {hitRate && <HitRateTable hitRate={hitRate} />}

          {attribution && (
            <div className="grid2eq">
              <AttributionPanel attribution={attribution} />
              <AttributionByStrategyPanel attribution={attribution} />
            </div>
          )}

          {bias && <BiasPanel bias={bias} />}
        </>
      )}
    </div>
  )
}
