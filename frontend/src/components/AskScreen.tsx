import { useState } from 'react'
import {
  streamAsk,
  type AskAnswerPayload,
  type AskHistoryTurn,
  type AskQueryResultPayload,
} from '../api'

// Client-held, capped conversation history (WP-9.9 decision) — only
// completed (question, answer) exchanges are resent; a turn that errored
// out contributes no answer_text to summarize, so it's excluded rather than
// poisoning the next question's context.
const MAX_HISTORY_TURNS = 5

interface ChatQuery {
  sql: string
  status: 'running' | 'done' | 'error'
  result: AskQueryResultPayload | null
  error: string | null
}

interface ChatTurn {
  question: string
  queries: ChatQuery[]
  answer: AskAnswerPayload | null
  error: string | null
}

interface AskScreenProps {
  onCiteCycle: (cycleId: string) => void
}

function historyFor(turns: ChatTurn[]): AskHistoryTurn[] {
  return turns
    .filter((t): t is ChatTurn & { answer: AskAnswerPayload } => t.answer !== null)
    .slice(-MAX_HISTORY_TURNS)
    .map((t) => ({ question: t.question, answer_text: t.answer.answer_text }))
}

export function AskScreen({ onCiteCycle }: AskScreenProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)

  const submit = async (question: string) => {
    const trimmed = question.trim()
    if (!trimmed || pending) return

    const history = historyFor(turns)
    const turnIndex = turns.length
    setTurns((prev) => [...prev, { question: trimmed, queries: [], answer: null, error: null }])
    setInput('')
    setPending(true)

    const updateTurn = (updater: (turn: ChatTurn) => ChatTurn) => {
      setTurns((prev) => prev.map((t, i) => (i === turnIndex ? updater(t) : t)))
    }

    // finally, not a trailing statement — streamAsk() already catches its own
    // read-loop errors internally, but this is the backstop that guarantees
    // the input never stays stuck disabled even if that changes later.
    try {
      await streamAsk(trimmed, history, {
        onQueryStarted: (sql) => {
          updateTurn((t) => ({
            ...t,
            queries: [...t.queries, { sql, status: 'running', result: null, error: null }],
          }))
        },
        onQueryResult: (payload) => {
          updateTurn((t) => {
            const queries = t.queries.slice()
            queries[queries.length - 1] = {
              sql: payload.sql,
              status: 'done',
              result: payload,
              error: null,
            }
            return { ...t, queries }
          })
        },
        onQueryError: (payload) => {
          updateTurn((t) => {
            const queries = t.queries.slice()
            queries[queries.length - 1] = {
              sql: payload.sql,
              status: 'error',
              result: null,
              error: payload.error,
            }
            return { ...t, queries }
          })
        },
        onAnswer: (payload) => {
          updateTurn((t) => ({ ...t, answer: payload }))
        },
        onError: (message) => {
          updateTurn((t) => ({ ...t, error: message }))
        },
      })
    } catch {
      updateTurn((t) => (t.answer || t.error ? t : { ...t, error: 'Something went wrong.' }))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="console-screen">
      <div className="chat">
        {turns.length === 0 && (
          <p className="chat--empty">Ask about trades, decisions, or performance.</p>
        )}
        {turns.map((turn, i) => (
          <ChatExchange key={i} turn={turn} onCiteCycle={onCiteCycle} />
        ))}
      </div>
      <form
        className="ask-bar"
        onSubmit={(e) => {
          e.preventDefault()
          void submit(input)
        }}
      >
        <input
          className="ask-bar__input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about trades, decisions, or performance…"
          disabled={pending}
        />
        <span className="ask-bar__kbd">{pending ? '…' : '⏎'}</span>
      </form>
    </div>
  )
}

function ChatExchange({
  turn,
  onCiteCycle,
}: {
  turn: ChatTurn
  onCiteCycle: (cycleId: string) => void
}) {
  const done = turn.answer !== null || turn.error !== null

  return (
    <>
      <div className="chat-msg chat-msg--user">{turn.question}</div>
      <div className="chat-msg chat-msg--agent">
        <div className="chat-msg__planrow">
          <span className="action-chip action-chip--info">SELECT-only</span>
          <span className="action-chip action-chip--muted">
            {turn.queries.length} {turn.queries.length === 1 ? 'query' : 'queries'}
          </span>
          {turn.answer && turn.answer.tables_touched.length > 0 && (
            <span className="action-chip action-chip--muted">
              {turn.answer.tables_touched.join(' ⋈ ')}
            </span>
          )}
        </div>

        {turn.queries.map((query, i) => (
          <QueryBlock key={i} query={query} />
        ))}

        {turn.answer && (
          <>
            <div className="chat-msg__text">{renderAnswerText(turn.answer.answer_text)}</div>
            {turn.answer.cited_cycle_ids.length > 0 && (
              <div className="chat-msg__cite">
                drawn from cycles{' '}
                {turn.answer.cited_cycle_ids.map((cycleId, i) => (
                  <span key={cycleId}>
                    {i > 0 && ' '}
                    <a
                      href="#"
                      onClick={(e) => {
                        e.preventDefault()
                        onCiteCycle(cycleId)
                      }}
                    >
                      {cycleId}
                    </a>
                  </span>
                ))}
              </div>
            )}
          </>
        )}

        {turn.error && <div className="chat-msg__error">{turn.error}</div>}
        {!done && <div className="chat-msg__pending">thinking…</div>}
      </div>
    </>
  )
}

// answer_text supports one markdown affordance — **bold** — for key figures
// and caveats (the system prompt in agent/ask/prompts.py instructs the model
// to use it), so caveats render visually distinct per the design reference
// rather than blending into the surrounding prose.
function renderAnswerText(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((part, i) =>
    part.startsWith('**') && part.endsWith('**') ? (
      <b key={i}>{part.slice(2, -2)}</b>
    ) : (
      part
    ),
  )
}

function QueryBlock({ query }: { query: ChatQuery }) {
  return (
    <details className="chat-msg__sql" open={query.status === 'running'}>
      <summary>{query.status === 'running' ? 'running query…' : 'show query'}</summary>
      <pre>{query.sql}</pre>
      {query.status === 'error' && <div className="chat-msg__query-error">{query.error}</div>}
      {query.status === 'done' && query.result && <ResultTable result={query.result} />}
    </details>
  )
}

function ResultTable({ result }: { result: AskQueryResultPayload }) {
  if (result.rows.length === 0) {
    return <p className="chat-msg__minitbl-empty">0 rows</p>
  }
  return (
    <div className="chat-msg__minitbl">
      <table className="review-table">
        <thead>
          <tr>
            {result.columns.map((col) => (
              <th key={col}>{col}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.rows.map((row, i) => (
            <tr key={i}>
              {result.columns.map((col) => (
                <td key={col}>{String(row[col] ?? '—')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {result.truncated && (
        <p className="chat-msg__truncated">truncated at {result.row_cap} rows</p>
      )}
    </div>
  )
}
