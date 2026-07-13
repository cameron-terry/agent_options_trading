import type { FunnelResponse } from '../api'

interface FunnelRow {
  label: string
  count: number
  denom: number
}

function rows(f: FunnelResponse): FunnelRow[] {
  return [
    { label: 'Cycles run', count: f.total, denom: f.total },
    { label: 'Passed gates', count: f.reasoned, denom: f.total },
    { label: 'Agent proposed', count: f.proposed, denom: f.total },
    {
      label: 'Passed validation',
      count: f.proposed - f.rejected,
      denom: f.total,
    },
    { label: 'Opened', count: f.opened, denom: f.total },
  ]
}

export function FunnelPanel({ funnel }: { funnel: FunnelResponse }) {
  const funnelRows = rows(funnel)
  const maxCount = funnelRows[0]?.count || 1

  return (
    <div className="panel">
      <h2>
        Entry-cycle funnel <small>cycle_funnel()</small>
      </h2>
      {funnel.total === 0 ? (
        <div className="funnel--empty">no cycles in range</div>
      ) : (
        <>
          <div className="funnel">
            {funnelRows.map((row, i) => {
              const prevCount = i === 0 ? row.count : funnelRows[i - 1].count
              const drop = i === 0 ? null : prevCount - row.count
              return (
                <div className="funnel__row" key={row.label}>
                  <span className="funnel__label">{row.label}</span>
                  <div className="funnel__bar-wrap">
                    <div
                      className="funnel__bar"
                      style={{ width: `${(row.count / maxCount) * 100}%` }}
                    />
                  </div>
                  <span className="funnel__count">
                    {row.count}
                    {drop !== null && drop > 0 && <small> −{drop}</small>}
                  </span>
                </div>
              )
            })}
          </div>
          <div className="funnel__dropoffs">
            drop-offs: gated {funnel.gated} · agent no-action {funnel.no_action_agent} ·
            rejected {funnel.rejected} · sized-to-zero {funnel.sized_to_zero} · exec failed{' '}
            {funnel.execution_failed}
          </div>
        </>
      )}
    </div>
  )
}

export function RejectionsByRulePanel({ funnel }: { funnel: FunnelResponse }) {
  return (
    <div className="panel">
      <h2>
        Rejections by rule{' '}
        <small>rejection_rule_ids · {funnel.rejected} total</small>
      </h2>
      {funnel.rejections_by_rule.length === 0 ? (
        <div className="review-table--empty">no rejections in range</div>
      ) : (
        <table className="review-table">
          <thead>
            <tr>
              <th>Rule</th>
              <th className="num">Fires</th>
            </tr>
          </thead>
          <tbody>
            {funnel.rejections_by_rule.map((r) => (
              <tr key={r.rule_id}>
                <td className="review-table__mono">{r.rule_id}</td>
                <td className="num">{r.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
