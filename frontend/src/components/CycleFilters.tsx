import { useState } from 'react'
import type { ActionTaken, CycleFilters as CycleFiltersState } from '../api'

const ACTION_OPTIONS: ActionTaken[] = [
  'OPENED',
  'CLOSED',
  'ROLLED',
  'NO_ACTION_GATED',
  'NO_ACTION_AGENT',
  'SIZED_TO_ZERO',
  'REJECTED',
  'EXECUTION_FAILED',
]

interface CycleFiltersProps {
  filters: CycleFiltersState
  onChange: (filters: CycleFiltersState) => void
}

export function CycleFilters({ filters, onChange }: CycleFiltersProps) {
  // query_journal's symbol filter is an exact match on the underlying column
  // (indexed lookup, not a substring search) — committing on every keystroke
  // means typing "S" toward "SPY" queries symbol="S" and briefly empties the
  // list. Buffer locally and only apply on blur/Enter, uppercased to match
  // how underlying is stored.
  const [symbolInput, setSymbolInput] = useState(filters.symbol ?? '')

  const commitSymbol = () => {
    const normalized = symbolInput.trim().toUpperCase()
    setSymbolInput(normalized)
    if (normalized !== (filters.symbol ?? '')) {
      onChange({ ...filters, symbol: normalized || undefined })
    }
  }

  return (
    <div className="cycle-filters">
      <input
        className="cycle-filters__input"
        type="text"
        placeholder="symbol (exact) — Enter to apply"
        value={symbolInput}
        onChange={(e) => setSymbolInput(e.target.value)}
        onBlur={commitSymbol}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commitSymbol()
        }}
      />
      <select
        className="cycle-filters__input"
        value={filters.action_type ?? ''}
        onChange={(e) =>
          onChange({
            ...filters,
            action_type: (e.target.value || undefined) as ActionTaken | undefined,
          })
        }
      >
        <option value="">all actions</option>
        {ACTION_OPTIONS.map((action) => (
          <option key={action} value={action}>
            {action}
          </option>
        ))}
      </select>
      <input
        className="cycle-filters__input"
        type="date"
        value={filters.date_from?.slice(0, 10) ?? ''}
        onChange={(e) =>
          onChange({
            ...filters,
            date_from: e.target.value ? `${e.target.value}T00:00:00Z` : undefined,
          })
        }
      />
      <input
        className="cycle-filters__input"
        type="date"
        value={filters.date_to?.slice(0, 10) ?? ''}
        onChange={(e) =>
          onChange({
            ...filters,
            date_to: e.target.value ? `${e.target.value}T23:59:59Z` : undefined,
          })
        }
      />
    </div>
  )
}
