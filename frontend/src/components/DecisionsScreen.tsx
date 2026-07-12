import { useEffect, useState } from 'react'
import {
  fetchCycleDetail,
  fetchCycles,
  type CycleDetail,
  type CycleFilters as CycleFiltersState,
  type CycleListItem,
} from '../api'
import { CycleFilters } from './CycleFilters'
import { CycleList } from './CycleList'
import { CycleTrace } from './CycleTrace'

interface DecisionsScreenProps {
  selectedCycleId: string | null
  onSelectCycle: (cycleId: string | null) => void
}

export function DecisionsScreen({ selectedCycleId, onSelectCycle }: DecisionsScreenProps) {
  const [filters, setFilters] = useState<CycleFiltersState>({})
  const [cycles, setCycles] = useState<CycleListItem[] | null>(null)
  const [detail, setDetail] = useState<CycleDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchCycles(filters)
      .then((result) => {
        if (cancelled) return
        setCycles(result)
        setError(null)
        // A filter change re-scopes the view — always jump to the newest
        // matching cycle rather than leaving the previous selection in
        // place. Keeping a stale selection reads as "the trace emptied
        // out" whenever that cycle happens to have no proposal/transcript
        // (e.g. a NO_ACTION cycle), even though its data is correct.
        onSelectCycle(result.length > 0 ? result[0].cycle_id : null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
    // Re-fetch on filter change only — selectedCycleId is deliberately not a
    // dependency, it's mutated by this effect's own callback above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  useEffect(() => {
    if (selectedCycleId === null) {
      setDetail(null)
      return
    }
    let cancelled = false
    fetchCycleDetail(selectedCycleId)
      .then((result) => {
        if (cancelled) return
        setDetail(result)
        setError(null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [selectedCycleId])

  return (
    <div className="console-screen">
      {error && <div className="console-error">Failed to load: {error}</div>}
      <CycleFilters filters={filters} onChange={setFilters} />
      <div className="decision-explorer">
        <div className="panel decision-explorer__list">
          <h2>
            Cycles <small>{cycles ? `${cycles.length} in range` : 'loading…'}</small>
          </h2>
          <CycleList
            cycles={cycles ?? []}
            selectedCycleId={selectedCycleId}
            onSelect={onSelectCycle}
          />
        </div>
        <CycleTrace detail={detail} />
      </div>
    </div>
  )
}
