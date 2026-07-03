import type { KillSwitchState } from '../api'

const LABEL: Record<KillSwitchState, string> = {
  NONE: 'TRADING · NONE',
  HALT: 'HALTED',
  FLATTEN: 'FLATTENING',
}

export function KillSwitchChip({ state }: { state: KillSwitchState }) {
  return (
    <span className={`kill-switch-chip kill-switch-chip--${state.toLowerCase()}`}>
      <span className="kill-switch-chip__dot" />
      {LABEL[state]}
    </span>
  )
}
