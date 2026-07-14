import { useEffect, useState } from 'react'
import {
  fetchKillSwitchStatus,
  postKillSwitchAction,
  type KillSwitchActionType,
  type KillSwitchStatusResponse,
} from '../api'

// HALT is deliberately absent — arming is zero-friction per the card,
// matching obs/__main__.py's `set HALT` (see ui/killswitch.py docstring).
const CONFIRMATION_REQUIRED: Record<KillSwitchActionType, string | null> = {
  HALT: null,
  FLATTEN: 'FLATTEN',
  RESUME: 'RESUME',
}

const ACTION_LABEL: Record<KillSwitchActionType, string> = {
  HALT: 'Arm HALT',
  FLATTEN: 'Arm FLATTEN',
  RESUME: 'Resume',
}

export function KillSwitchPanel({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<KillSwitchStatusResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<KillSwitchActionType | null>(null)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const load = () => {
    fetchKillSwitchStatus()
      .then((res) => {
        setStatus(res)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
  }

  useEffect(load, [])

  const startAction = (action: KillSwitchActionType) => {
    setPending(action)
    setReason('')
    setConfirmation('')
    setError(null)
  }

  const cancelAction = () => {
    setPending(null)
    setReason('')
    setConfirmation('')
  }

  const requiredConfirmation = pending ? CONFIRMATION_REQUIRED[pending] : null
  const canSubmit =
    pending !== null &&
    reason.trim().length > 0 &&
    (requiredConfirmation === null || confirmation === requiredConfirmation)

  const submit = () => {
    if (!pending) return
    setSubmitting(true)
    postKillSwitchAction({
      action: pending,
      reason,
      confirmation: requiredConfirmation ? confirmation : undefined,
    })
      .then(() => {
        setSubmitting(false)
        cancelAction()
        load()
      })
      .catch((err: Error) => {
        setSubmitting(false)
        setError(err.message)
      })
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal kill-switch-panel"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Kill switch"
      >
        <div className="modal__header">
          <h2>Kill switch</h2>
          <button className="modal__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {error && <div className="console-error">{error}</div>}

        {status && (
          <>
            <div className="kill-switch-panel__state">
              Current state:{' '}
              <span className={`kill-switch-chip kill-switch-chip--${status.state.toLowerCase()}`}>
                <span className="kill-switch-chip__dot" />
                {status.state}
              </span>
            </div>

            <div className="kill-switch-panel__actions">
              <button onClick={() => startAction('HALT')} disabled={submitting}>
                {ACTION_LABEL.HALT}
              </button>
              <button onClick={() => startAction('FLATTEN')} disabled={submitting}>
                {ACTION_LABEL.FLATTEN}
              </button>
              <button
                onClick={() => startAction('RESUME')}
                disabled={submitting || status.state === 'NONE'}
              >
                {ACTION_LABEL.RESUME}
              </button>
            </div>

            {pending && (
              <div className="kill-switch-panel__form">
                <label className="kill-switch-panel__label">
                  Reason (required)
                  <input
                    className="cycle-filters__input"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="Why are you doing this?"
                  />
                </label>
                {requiredConfirmation && (
                  <label className="kill-switch-panel__label">
                    Type {requiredConfirmation} to confirm
                    <input
                      className="cycle-filters__input"
                      value={confirmation}
                      onChange={(e) => setConfirmation(e.target.value)}
                      placeholder={requiredConfirmation}
                    />
                  </label>
                )}
                <div className="kill-switch-panel__form-actions">
                  <button onClick={cancelAction} disabled={submitting}>
                    Cancel
                  </button>
                  <button
                    className="kill-switch-panel__confirm"
                    onClick={submit}
                    disabled={!canSubmit || submitting}
                  >
                    {submitting ? 'Submitting…' : `Confirm ${pending}`}
                  </button>
                </div>
              </div>
            )}

            <h3 className="kill-switch-panel__subhead">History</h3>
            {status.history.length === 0 ? (
              <div className="kill-switch-panel__empty">
                No kill-switch history recorded.
              </div>
            ) : (
              <div className="tblwrap">
                <table className="review-table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>State</th>
                      <th>By</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.history.map((h) => (
                      <tr key={h.id}>
                        <td className="review-table__mono">
                          {new Date(h.created_at).toLocaleString()}
                        </td>
                        <td>{h.state}</td>
                        <td>{h.set_by}</td>
                        <td>{h.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <h3 className="kill-switch-panel__subhead">
              Alert delivery health <small>alert_delivery_failures</small>
            </h3>
            {status.alert_failures.length === 0 ? (
              <div className="kill-switch-panel__empty">
                No delivery failures recorded.
              </div>
            ) : (
              <div className="tblwrap">
                <table className="review-table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Event</th>
                      <th>Severity</th>
                      <th>Detail</th>
                      <th className="num">Attempts</th>
                      <th>Last error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.alert_failures.map((f) => (
                      <tr key={f.id}>
                        <td className="review-table__mono">
                          {new Date(f.attempted_at).toLocaleString()}
                        </td>
                        <td>{f.event_type}</td>
                        <td>{f.severity}</td>
                        <td>{f.detail}</td>
                        <td className="num">{f.attempts}</td>
                        <td>{f.last_error}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
