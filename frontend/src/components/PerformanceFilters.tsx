import { useState } from 'react'
import type { ReviewFilters } from '../api'

// Version picker (distinct prompt_version values from the journal) and the
// side-by-side compare layout are WP-9.6 scope — this is a free-text filter
// only, same as the CLI's --prompt-version flag.
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
}

export function PerformanceFilters({ filters, onChange }: PerformanceFiltersProps) {
  // The range select is a day-count preset, not the derived ISO `since`
  // timestamp itself — tracked locally so the control stays a controlled
  // <select> without trying to reverse-map an arbitrary ISO string back to
  // one of the four presets.
  const [rangeDays, setRangeDays] = useState<number | null>(null)

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
      <input
        className="cycle-filters__input"
        type="text"
        placeholder="prompt_version — exact match"
        value={filters.prompt_version ?? ''}
        onChange={(e) =>
          onChange({ ...filters, prompt_version: e.target.value.trim() || undefined })
        }
      />
    </div>
  )
}
