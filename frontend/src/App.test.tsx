import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import App from './App'
import { server } from './test/msw/server'

// jsdom has no EventSource implementation, and App opens one on mount to
// receive WP-9.4's SSE re-fetch ticks. A minimal stub is enough here: it just
// needs to exist and fire onopen once so App's initial load() call runs.
class FakeEventSource {
  onopen: (() => void) | null = null
  addEventListener() {
    // no 'update' events needed for these tests — only the initial load.
  }
  close() {}
  constructor() {
    queueMicrotask(() => this.onopen?.())
  }
}

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App — Overview positions loading', () => {
  it('shows a loading state before the first fetch resolves, then the loaded positions', async () => {
    render(<App />)
    expect(screen.getByText('loading positions…')).toBeInTheDocument()

    // Default MSW handler returns the seeded positions fixture (has rows).
    expect(await screen.findByRole('row', { name: /SPY/ })).toBeInTheDocument()
    expect(screen.queryByText('loading positions…')).not.toBeInTheDocument()
  })

  it('stops claiming to load positions once the initial fetch fails', async () => {
    server.use(http.get('/api/positions', () => new HttpResponse(null, { status: 500 })))
    render(<App />)

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
    // The error banner already says what happened — the panel below it must
    // not go on claiming positions are still loading.
    expect(screen.queryByText('loading positions…')).not.toBeInTheDocument()
    expect(screen.getByText('no open positions')).toBeInTheDocument()
  })
})
