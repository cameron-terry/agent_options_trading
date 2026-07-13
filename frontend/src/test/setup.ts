// Vitest global setup. Registers jest-dom matchers (toBeInTheDocument, etc.)
// against Vitest's expect, tears down the rendered DOM after each test, and
// runs the MSW mock server for the whole run. Tests import
// { describe, it, expect } from 'vitest' explicitly (globals are off) — this
// file only wires up the pieces that must be global.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from './msw/server'

// The app fetches same-origin relative paths ('/api/...'). Node's fetch
// (undici) requires an absolute URL and, unlike a browser, won't resolve it
// against the document origin. MSW's interceptor (installed by server.listen)
// also parses the URL and would choke on a relative one — so this bridge must
// sit *outermost*: installed after listen(), delegating to MSW's patched fetch
// with an already-absolute URL. Handlers still match on pathname as written.
let mswFetch: typeof globalThis.fetch
let originalFetch: typeof globalThis.fetch

beforeAll(() => {
  // onUnhandledRequest: 'error' surfaces typos / missing handlers as loud test
  // failures instead of silent real network calls.
  server.listen({ onUnhandledRequest: 'error' })
  mswFetch = globalThis.fetch
  originalFetch = mswFetch
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === 'string' && input.startsWith('/')) {
      input = new URL(input, globalThis.location.origin).href
    }
    return mswFetch(input, init)
  }) as typeof globalThis.fetch
})

afterEach(() => {
  cleanup()
  server.resetHandlers()
})

afterAll(() => {
  server.close()
  globalThis.fetch = originalFetch
})
