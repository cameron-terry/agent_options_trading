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

// Lightest → darkest down the funnel (longer bars are lighter), matching
// the design reference's --f1..--f5 tokens exactly — one shade per row, not
// a CSS gradient within a single bar.
const FUNNEL_BAR_COLORS = [
  'var(--f1)',
  'var(--f2)',
  'var(--f3)',
  'var(--f4)',
  'var(--f5)',
]

// One-line, rule-keyed interpretive hints for the rejections-by-rule table —
// a fixed dictionary (not per-cycle-computed commentary), analogous to the
// design reference's "Reading" column. Falls back to a generic string for
// any ValidationRuleId not yet covered here.
const RULE_READING: Record<string, string> = {
  INVALID_SCHEMA: 'proposal failed schema validation',
  UNKNOWN_STRATEGY: 'strategy outside the allowed playbook',
  APPROVAL_LEVEL: 'exceeds the account approval level',
  NAKED_SHORT: 'included an uncovered short leg',
  MAX_LOSS_CAP: 'risk exceeds the per-trade max-loss cap',
  MAX_LOSS_NOT_FINITE: 'unbounded max loss — likely a malformed spread',
  PORTFOLIO_DELTA_BAND: 'would push portfolio delta outside its band',
  PORTFOLIO_VEGA_BAND: 'would push portfolio vega outside its band',
  PORTFOLIO_THETA_FLOOR: 'would drop portfolio theta below its floor',
  CONCENTRATION_UNDERLYING: 'too much risk already in this underlying',
  CONCENTRATION_SECTOR: 'too much risk already in this sector',
  LIQUIDITY_SPREAD: 'agent picks wide markets — prompt nudge?',
  LIQUIDITY_OPEN_INTEREST: 'too little open interest to size safely',
  INVALID_EXIT_PLAN: 'exit plan fields missing or out of bounds',
  EVENT_BLACKOUT: 'proposes into an earnings/event blackout window',
  BUYING_POWER: 'insufficient buying power for the proposed size',
  DUPLICATE_POSITION: 'an equivalent position is already open',
  CONFLICTING_POSITION: "conflicts with an existing position's direction",
  KILL_SWITCH: 'kill switch was active at proposal time',
  EVENT_DATA_MISSING: 'required market/event data unavailable — failed closed',
  LOW_CONVICTION: "agent's own conviction score was low",
  NEAR_DELTA_BAND: 'portfolio delta is near its band edge',
  NEAR_VEGA_BAND: 'portfolio vega is near its band edge',
  NEAR_THETA_FLOOR: 'portfolio theta is near its floor',
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
                      style={{
                        width: `${(row.count / maxCount) * 100}%`,
                        background: FUNNEL_BAR_COLORS[i % FUNNEL_BAR_COLORS.length],
                      }}
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
              <th>Reading</th>
            </tr>
          </thead>
          <tbody>
            {funnel.rejections_by_rule.map((r) => (
              <tr key={r.rule_id}>
                <td className="review-table__mono">{r.rule_id}</td>
                <td className="num">{r.count}</td>
                <td className="review-table__reading">
                  {RULE_READING[r.rule_id] ?? 'no reading available'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
