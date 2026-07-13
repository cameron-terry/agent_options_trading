import { useEffect, useState } from 'react'
import {
  fetchAttribution,
  fetchBias,
  fetchHitRate,
  fetchPromptVersions,
  type AttributionResponse,
  type BiasResponse,
  type HitRateResponse,
} from '../api'
import { AttributionByStrategyPanel, AttributionPanel } from './AttributionPanel'
import { BiasPanel } from './BiasPanel'
import { HitRateTable } from './HitRateTable'

// WP-9.6: side-by-side A/B compare, driven by the same WP-9.5 endpoints as
// the single-version view — just called once per column with a different
// prompt_version. Funnel is deliberately excluded: cycle_funnel() has no
// prompt_version filter (see options_agent/ui/review.py's get_funnel()
// docstring), so a per-column funnel would show identical numbers under two
// different version headers. PerformanceScreen renders funnel once, outside
// this component, with a note explaining why.

interface ColumnData {
  hitRate: HitRateResponse | null
  attribution: AttributionResponse | null
  bias: BiasResponse | null
}

const EMPTY_COLUMN: ColumnData = { hitRate: null, attribution: null, bias: null }

function CompareColumn({
  label,
  since,
  versions,
  selected,
  onSelect,
}: {
  label: string
  since?: string
  versions: string[]
  selected: string
  onSelect: (version: string) => void
}) {
  const [data, setData] = useState<ColumnData>(EMPTY_COLUMN)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!selected) {
      setData(EMPTY_COLUMN)
      setError(null)
      return
    }
    let cancelled = false
    const filters = { since, prompt_version: selected }
    Promise.all([fetchHitRate(filters), fetchAttribution(filters), fetchBias(filters)])
      .then(([hitRate, attribution, bias]) => {
        if (cancelled) return
        setData({ hitRate, attribution, bias })
        setError(null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [since, selected])

  return (
    <div className="compare-column">
      <div className="compare-column__header">
        <span className="compare-column__label">{label}</span>
        <select
          className="cycle-filters__input"
          value={selected}
          onChange={(e) => onSelect(e.target.value)}
        >
          <option value="">select a version</option>
          {versions.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </div>
      {error && <div className="console-error">Failed to load: {error}</div>}
      {!selected && <div className="review-table--empty">pick a prompt_version to compare</div>}
      {data.hitRate && <HitRateTable hitRate={data.hitRate} />}
      {data.attribution && (
        <>
          <AttributionPanel attribution={data.attribution} />
          <AttributionByStrategyPanel attribution={data.attribution} />
        </>
      )}
      {data.bias && <BiasPanel bias={data.bias} />}
    </div>
  )
}

export function PerformanceCompare({ since }: { since?: string }) {
  const [versions, setVersions] = useState<string[]>([])
  const [versionA, setVersionA] = useState('')
  const [versionB, setVersionB] = useState('')

  useEffect(() => {
    let cancelled = false
    fetchPromptVersions()
      .then((v) => {
        if (cancelled) return
        setVersions(v)
        // Default to the two most-recent distinct versions so the compare
        // view is already populated instead of starting on two empty pickers.
        if (v.length >= 2) {
          setVersionA((prev) => prev || v[v.length - 2])
          setVersionB((prev) => prev || v[v.length - 1])
        } else if (v.length === 1) {
          setVersionA((prev) => prev || v[0])
        }
      })
      .catch(() => {
        // Non-fatal: pickers stay empty; the "no prompt versions" state below covers it.
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (versions.length === 0) {
    return <div className="review-table--empty">no prompt versions recorded yet</div>
  }

  return (
    <div className="grid2eq">
      <CompareColumn
        label="Version A"
        since={since}
        versions={versions}
        selected={versionA}
        onSelect={setVersionA}
      />
      <CompareColumn
        label="Version B"
        since={since}
        versions={versions}
        selected={versionB}
        onSelect={setVersionB}
      />
    </div>
  )
}
