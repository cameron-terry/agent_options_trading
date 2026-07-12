import type { ActionTaken, CycleListItem } from '../api'
import { formatTime } from '../format'

const ACTION_TONE: Record<ActionTaken, 'info' | 'muted' | 'critical'> = {
  OPENED: 'info',
  CLOSED: 'info',
  ROLLED: 'info',
  NO_ACTION_GATED: 'muted',
  NO_ACTION_AGENT: 'muted',
  SIZED_TO_ZERO: 'muted',
  REJECTED: 'critical',
  EXECUTION_FAILED: 'critical',
}

interface CycleListProps {
  cycles: CycleListItem[]
  selectedCycleId: string | null
  onSelect: (cycleId: string) => void
}

export function CycleList({ cycles, selectedCycleId, onSelect }: CycleListProps) {
  if (cycles.length === 0) {
    return <div className="cycle-list cycle-list--empty">no cycles in range</div>
  }

  return (
    <div className="cycle-list">
      {cycles.map((cycle) => (
        <button
          key={cycle.cycle_id}
          type="button"
          className={`cycle-list__item${
            cycle.cycle_id === selectedCycleId ? ' cycle-list__item--selected' : ''
          }`}
          onClick={() => onSelect(cycle.cycle_id)}
        >
          <div className="cycle-list__row">
            <span className="cycle-list__symbol">{cycle.underlying ?? '—'}</span>
            <span className={`action-chip action-chip--${ACTION_TONE[cycle.action_taken]}`}>
              {cycle.action_taken}
            </span>
          </div>
          <time className="cycle-list__time">
            {formatTime(cycle.timestamp)} · {cycle.cycle_id}
          </time>
        </button>
      ))}
    </div>
  )
}
