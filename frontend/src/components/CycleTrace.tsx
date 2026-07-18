import { useState } from 'react'
import type { CycleDetail, OrderLink, PositionLink, ToolCallRecord } from '../api'
import { formatCurrency, formatPct, formatSignedCurrency, formatTime } from '../format'

const GIST_MAX_CHARS = 64
const BODY_TRUNCATE_LINES = 10

// result_json is an opaque, heterogeneous blob (state/journal.py stores it
// pre-serialized precisely because every tool's result shape differs) — no
// schema exists to build a tool-aware one-line summary from it. A truncated
// raw preview is the ceiling of what's safely derivable without inventing a
// per-tool summarizer.
function summarizeResult(resultJson: string): string {
  const trimmed = resultJson.trim().replace(/\s+/g, ' ')
  return trimmed.length > GIST_MAX_CHARS
    ? `${trimmed.slice(0, GIST_MAX_CHARS)}…`
    : trimmed
}

// result_json is often minified; pretty-printing it first is what makes
// "10 lines or more" a meaningful truncation threshold rather than one
// giant unbroken line.
function prettyResult(resultJson: string): string {
  try {
    return JSON.stringify(JSON.parse(resultJson), null, 2)
  } catch {
    return resultJson
  }
}

function ResultBody({ resultJson }: { resultJson: string }) {
  const [expanded, setExpanded] = useState(false)
  const pretty = prettyResult(resultJson)
  const lines = pretty.split('\n')
  if (lines.length <= BODY_TRUNCATE_LINES) {
    return <div className="tool-transcript__body">{pretty}</div>
  }
  const preview = lines.slice(0, BODY_TRUNCATE_LINES).join('\n')
  const rest = lines.slice(BODY_TRUNCATE_LINES).join('\n')
  return (
    <div className="tool-transcript__body">
      {preview}
      <details
        className="tool-transcript__body-more"
        open={expanded}
        onToggle={(e) => setExpanded(e.currentTarget.open)}
      >
        <summary>
          {expanded ? 'show less' : `show ${lines.length - BODY_TRUNCATE_LINES} more lines`}
        </summary>
        {rest}
      </details>
    </div>
  )
}

function TranscriptStep({
  index,
  step,
  defaultOpen,
}: {
  index: number
  step: ToolCallRecord
  defaultOpen: boolean
}) {
  const args = Object.entries(step.tool_input)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(', ')
  return (
    <details className="tool-transcript__step" open={defaultOpen}>
      <summary>
        <span className="tool-transcript__index">{index}</span>
        <span className="tool-transcript__name">{step.tool_name}</span>
        <span className="tool-transcript__args">({args})</span>
        <span className="tool-transcript__gist">{summarizeResult(step.result_json)}</span>
      </summary>
      <ResultBody resultJson={step.result_json} />
    </details>
  )
}

function TranscriptPanel({ transcript }: { transcript: ToolCallRecord[] }) {
  return (
    <div className="panel">
      <h2>
        Exploration transcript <small>read-only tools · stored verbatim, replayable</small>
      </h2>
      {transcript.length === 0 ? (
        <p className="tool-transcript--empty">
          no tool calls recorded — cycle short-circuited before the LLM call, or predates
          WP-6.4
        </p>
      ) : (
        transcript.map((step, i) => (
          <TranscriptStep
            key={i}
            index={i + 1}
            step={step}
            defaultOpen={i === transcript.length - 1}
          />
        ))
      )}
    </div>
  )
}

function ProposalPanel({ proposal }: { proposal: NonNullable<CycleDetail['proposal']> }) {
  return (
    <div className="panel">
      <h2>
        Proposal{' '}
        <small>
          {proposal.strategy} on {proposal.underlying}
        </small>
      </h2>
      <div className="proposal">
        <blockquote>
          <b>Thesis:</b> {proposal.thesis}
        </blockquote>
        <blockquote>
          <b>IV rationale:</b> {proposal.iv_rationale}
        </blockquote>
        <table className="legs-table">
          <thead>
            <tr>
              <th>Leg</th>
              <th className="num">Strike</th>
              <th>Exp</th>
            </tr>
          </thead>
          <tbody>
            {proposal.legs.map((leg, i) => (
              <tr key={i}>
                <td>
                  {leg.side.toUpperCase()} {leg.right.toUpperCase()}
                  {leg.ratio !== 1 ? ` ×${leg.ratio}` : ''}
                </td>
                <td className="num">{leg.strike}</td>
                <td>{leg.expiration}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="proposal__meta">
          <span>
            <b>est_max_profit</b> {formatCurrency(proposal.est_max_profit)}
          </span>
          <span>
            <b>est_max_loss</b> {formatCurrency(proposal.est_max_loss)}
          </span>
          <span>
            <b>exit</b> PT {Math.round(proposal.exit_plan.profit_target_pct * 100)}% · SL{' '}
            {Math.round(proposal.exit_plan.stop_loss_max_loss_fraction * 100)}% of max loss ·
            time-stop {proposal.exit_plan.time_stop_dte} DTE
          </span>
        </div>
      </div>
    </div>
  )
}

function ValidationPanel({ detail }: { detail: CycleDetail }) {
  const vr = detail.validation_result
  if (vr === null) {
    return (
      <div className="panel">
        <h2>Validation</h2>
        <p className="validation-rules--empty">no validation recorded for this cycle</p>
      </div>
    )
  }

  const failCount = vr.reasons.filter((r) => r.severity === 'error').length

  return (
    <div className="panel">
      <h2>
        Validation{' '}
        <small>{vr.passed ? 'passed' : `${failCount} rule${failCount === 1 ? '' : 's'} failed`}</small>
      </h2>
      {vr.reasons.length === 0 ? (
        <p className="validation-rules--empty">no rule reasons recorded</p>
      ) : (
        <>
          <div className="validation-rules">
            {vr.reasons.map((r) => (
              <span
                key={r.rule_id}
                className={`validation-rules__rule${
                  r.severity === 'error' ? ' validation-rules__rule--fail' : ''
                }`}
              >
                {r.rule_id}
              </span>
            ))}
          </div>
          <ul className="validation-rules__reasons">
            {vr.reasons.map((r) => (
              <li key={r.rule_id}>
                <b>{r.rule_id}</b> — {r.human_message}
                {r.observed !== null && r.limit !== null
                  ? ` (observed ${r.observed}, limit ${r.limit})`
                  : ''}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function OrderRow({ link }: { link: OrderLink }) {
  if (link.anomaly || link.order === null) {
    return (
      <p className="sizing-panel__anomaly">
        order {link.id} — not found (broken history)
      </p>
    )
  }
  const order = link.order
  return (
    <p>
      Order <span className="sizing-panel__ref">{order.id}</span> {order.status.toLowerCase()}
      {order.filled_at ? ` ${formatTime(order.filled_at)}` : ''}
      {order.net_fill_price !== null && (
        <>
          {' '}
          at net {order.net_fill_price >= 0 ? 'debit' : 'credit'}{' '}
          <b>{Math.abs(order.net_fill_price).toFixed(2)}</b>
        </>
      )}
      .
    </p>
  )
}

function PositionRow({ link }: { link: PositionLink }) {
  if (link.anomaly || link.position === null) {
    return (
      <p className="sizing-panel__anomaly">
        position {link.id} — not found (broken history)
      </p>
    )
  }
  const pos = link.position
  return (
    <p>
      Position <span className="sizing-panel__ref">{pos.id}</span> — {pos.status.toLowerCase()},{' '}
      {formatSignedCurrency(pos.unrealized_pnl)} unrealized.
      {link.outcomes.map((o) => (
        <span key={o.id} className="sizing-panel__outcome">
          {' '}
          {o.event_type} realized {formatSignedCurrency(o.realized_pnl)}.
        </span>
      ))}
    </p>
  )
}

function SizingPanel({ detail }: { detail: CycleDetail }) {
  return (
    <div className="panel">
      <h2>Sizing → order → outcome</h2>
      <div className="sizing-panel">
        {detail.sizing_result ? (
          <p>
            Sized to{' '}
            <b>
              {detail.sizing_result.contracts} contract
              {detail.sizing_result.contracts === 1 ? '' : 's'}
            </b>{' '}
            — max loss {formatCurrency(detail.sizing_result.sized_max_loss)} ={' '}
            {formatPct(detail.sizing_result.risk_budget_used)} of equity
            {detail.sizing_result.binding_constraint
              ? ` (${detail.sizing_result.binding_constraint})`
              : ''}
            .
          </p>
        ) : (
          <p className="sizing-panel--empty">no sizing result recorded</p>
        )}
        {detail.orders.map((link) => (
          <OrderRow key={link.id} link={link} />
        ))}
        {detail.positions.map((link) => (
          <PositionRow key={link.id} link={link} />
        ))}
        {detail.sizing_result === null &&
          detail.orders.length === 0 &&
          detail.positions.length === 0 && (
            <p className="sizing-panel--empty">no order or position linked to this cycle</p>
          )}
      </div>
    </div>
  )
}

function CycleHeader({ detail }: { detail: CycleDetail }) {
  return (
    <div className="panel">
      <h2>
        Cycle {detail.cycle_id} <small>ActionTaken: {detail.action_taken}</small>
      </h2>
      <div className="cycle-header__meta">
        <span>
          <b>model</b> {detail.model_id}
        </span>
        <span>
          <b>prompt</b> {detail.prompt_version}
        </span>
        <span>
          <b>limits</b> {detail.limits_version}
        </span>
        <span>
          <b>context</b> {detail.context_hash.slice(0, 12)}
        </span>
        {detail.conviction !== null && (
          <span>
            <b>conviction</b> {detail.conviction.toFixed(2)}
          </span>
        )}
        <span>
          <b>tool calls</b> {detail.tool_calls_transcript.length}
        </span>
      </div>
    </div>
  )
}

export function CycleTrace({ detail }: { detail: CycleDetail | null }) {
  if (detail === null) {
    return <div className="cycle-trace cycle-trace--empty">select a cycle to view its trace</div>
  }

  return (
    <div className="cycle-trace">
      <CycleHeader detail={detail} />
      <TranscriptPanel transcript={detail.tool_calls_transcript} />
      {detail.proposal && <ProposalPanel proposal={detail.proposal} />}
      <div className="grid2eq">
        <ValidationPanel detail={detail} />
        <SizingPanel detail={detail} />
      </div>
    </div>
  )
}
