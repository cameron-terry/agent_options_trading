import { useEffect, useRef, useState } from 'react'
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
  // This component unmounts/remounts on every screen switch (App.tsx
  // conditionally renders it), so the fetch effect below fires on every
  // navigation into Decisions, including a citation deep-link from the Ask
  // screen (WP-9.9) that arrives with selectedCycleId already set. Track
  // whether this is that first mount so the effect can distinguish it from
  // a later, user-driven filter change.
  const isInitialMount = useRef(true)

  useEffect(() => {
    let cancelled = false
    const preserveIncomingSelection = isInitialMount.current
    isInitialMount.current = false
    fetchCycles(filters)
      .then((result) => {
        if (cancelled) return
        setCycles(result)
        setError(null)
        // On mount, an incoming selection (e.g. a citation deep-link) wins
        // if it's still present in the fetched list — don't clobber a
        // just-set selection. Otherwise (including every later filter
        // change), always jump to the newest matching cycle rather than
        // leaving the previous selection in place: keeping a stale
        // selection reads as "the trace emptied out" whenever that cycle
        // happens to have no proposal/transcript (e.g. a NO_ACTION cycle),
        // even though its data is correct.
        if (
          preserveIncomingSelection &&
          selectedCycleId !== null &&
          result.some((c) => c.cycle_id === selectedCycleId)
        ) {
          return
        }
        onSelectCycle(result.length > 0 ? result[0].cycle_id : null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
    // Re-fetch on filter change only — selectedCycleId is read (not
    // depended on) inside the callback above; it's not a dependency because
    // the effect must not re-run just because this same effect's callback
    // changed it.
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
