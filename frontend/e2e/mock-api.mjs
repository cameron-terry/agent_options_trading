// Zero-dependency mock of the FastAPI backend for the Playwright smoke suite.
// It exists to exercise the one flow jsdom/Vitest can't: the EventSource-driven
// live refresh in App.tsx. The Vite dev server proxies /api here (see
// vite.config.ts), so the browser talks to the real SPA over a real network.
//
// The SSE endpoint drives a deterministic 0 -> 1 transition: each new
// EventSource connection resets the cycle counter to 0 (the client loads and
// renders 0), then ~700ms later the server bumps it to 1 and pushes an
// `update` event, which makes the client re-fetch and re-render 1. The smoke
// test asserts that transition. The suite runs single-worker so this shared
// counter is never touched by two connections at once.
import { createServer } from 'node:http'

const PORT = 8000
const UPDATE_DELAY_MS = 700

let cyclesToday = 0

function overview() {
  return {
    kill_switch: { state: 'NONE' },
    tiles: {
      account_equity: { value: 100000, as_of: '2026-07-12T14:00:00Z' },
      realized_pnl: { total: 1250, closed_count: 4, hit_count: 3 },
      unrealized_pnl: { total: -320, open_position_count: 2 },
      cycles_today: { total: cyclesToday, by_action: {} },
    },
    equity_curve: [
      { timestamp: '2026-07-12T20:00:00Z', cumulative_realized_pnl: 1250, equity: 100000 },
    ],
    activity: [],
    mode: 'paper',
  }
}

function json(res, body) {
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(body))
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`)
  const path = url.pathname

  if (path === '/api/overview') {
    json(res, overview())
    return
  }
  if (path === '/api/positions') {
    json(res, [])
    return
  }
  if (path === '/api/cycles') {
    json(res, [])
    return
  }
  if (path.startsWith('/api/cycles/')) {
    json(res, [])
    return
  }
  if (path === '/api/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    // Flush headers so the browser fires EventSource.onopen (initial load).
    res.write(':ok\n\n')
    // Fresh connection => reset to the pre-update state the client will load.
    cyclesToday = 0
    const timer = setTimeout(() => {
      cyclesToday = 1
      res.write('event: update\ndata: {}\n\n')
    }, UPDATE_DELAY_MS)
    req.on('close', () => clearTimeout(timer))
    return
  }

  res.writeHead(404, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ detail: 'not found' }))
})

server.listen(PORT, () => {
  console.log(`mock-api listening on http://127.0.0.1:${PORT}`)
})
