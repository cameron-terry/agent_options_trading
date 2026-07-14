import { useEffect, useState } from 'react'
import { fetchPromptVersions, type ReviewFilters } from '../api'

// Side-by-side compare layout is WP-9.6 scope — this is a single-version
// filter only. The version list itself comes from GET /api/review/prompt-
// versions (WP-9.5), which WP-9.6 should reuse rather than duplicate.
const RANGE_OPTIONS: { label: string; days: number | null }[] = [
  { label: 'all time', days: null },
  { label: 'last 7 days', days: 7 },
  { label: 'last 30 days', days: 30 },
  { label: 'last 90 days', days: 90 },
]

function sinceForDays(days: number | null): string | undefined {
  if (days === null) return undefined
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

interface PerformanceFiltersProps {
  filters: ReviewFilters
  onChange: (filters: ReviewFilters) => void
  // "N cycles · M opened · K closed" — right-aligned in the same row as the
  // filters, matching the design reference. Rendered by the caller (which
  // owns the funnel/hit-rate data) rather than fetched here; omitted while
  // that data is still loading.
  summary?: string
  // WP-9.6: while the compare view is active, this filter's prompt_version
  // has no coordinated meaning (each compare column picks its own version)
  // — disabled rather than removed so `since` stays reachable in the same
  // row, with a title explaining why it's inert instead of silently doing
  // nothing.
  disablePromptVersion?: boolean
}

export function PerformanceFilters({
  filters,
  onChange,
  summary,
  disablePromptVersion,
}: PerformanceFiltersProps) {
  // The range select is a day-count preset, not the derived ISO `since`
  // timestamp itself — tracked locally so the control stays a controlled
  // <select> without trying to reverse-map an arbitrary ISO string back to
  // one of the four presets.
  const [rangeDays, setRangeDays] = useState<number | null>(null)
  const [promptVersions, setPromptVersions] = useState<string[]>([])

  useEffect(() => {
    let cancelled = false
    fetchPromptVersions()
      .then((versions) => {
        if (!cancelled) setPromptVersions(versions)
      })
      .catch(() => {
        // Non-fatal: the filter row still works with "all versions" only.
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="cycle-filters">
      <select
        className="cycle-filters__input"
        value={rangeDays ?? ''}
        onChange={(e) => {
          const days = e.target.value ? Number(e.target.value) : null
          setRangeDays(days)
          onChange({ ...filters, since: sinceForDays(days) })
        }}
      >
        {RANGE_OPTIONS.map((opt) => (
          <option key={opt.label} value={opt.days ?? ''}>
            {opt.label}
          </option>
        ))}
      </select>
      <select
        className="cycle-filters__input"
        value={filters.prompt_version ?? ''}
        disabled={disablePromptVersion}
        title={disablePromptVersion ? 'pick versions per column below while comparing' : undefined}
        onChange={(e) =>
          onChange({ ...filters, prompt_version: e.target.value || undefined })
        }
      >
        <option value="">prompt_version: all</option>
        {promptVersions.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      {summary && <span className="cycle-filters__summary">{summary}</span>}
    </div>
  )
}
