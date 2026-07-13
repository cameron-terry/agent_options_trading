import { describe, it, expect } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from './test/msw/server'
import {
  attributionFixture,
  biasFixture,
  cyclesFixture,
  funnelFixture,
  hitRateFixture,
  overviewFixture,
  positionsFixture,
  promptVersionsFixture,
} from './test/msw/handlers'
import {
  fetchAttribution,
  fetchBias,
  fetchCycleDetail,
  fetchCycles,
  fetchFunnel,
  fetchHitRate,
  fetchOverview,
  fetchPositions,
  fetchPromptVersions,
} from './api'

describe('fetchOverview', () => {
  it('returns the parsed overview payload', async () => {
    await expect(fetchOverview()).resolves.toEqual(overviewFixture)
  })
})

describe('fetchPositions', () => {
  it('returns the parsed positions array', async () => {
    await expect(fetchPositions()).resolves.toEqual(positionsFixture)
  })
})

describe('fetchCycles', () => {
  it('sends no query string when no filters are given', async () => {
    let requestUrl = ''
    server.use(
      http.get('/api/cycles', ({ request }) => {
        requestUrl = request.url
        return HttpResponse.json(cyclesFixture)
      }),
    )

    await fetchCycles()

    expect(new URL(requestUrl).search).toBe('')
  })

  it('serializes only the provided filters into the query string', async () => {
    let params = new URLSearchParams()
    server.use(
      http.get('/api/cycles', ({ request }) => {
        params = new URL(request.url).searchParams
        return HttpResponse.json(cyclesFixture)
      }),
    )

    await fetchCycles({ symbol: 'SPY', action_type: 'OPENED' })

    expect(params.get('symbol')).toBe('SPY')
    expect(params.get('action_type')).toBe('OPENED')
    // Unset filters are omitted entirely, not sent as empty values.
    expect(params.has('date_from')).toBe(false)
    expect(params.has('date_to')).toBe(false)
  })

  it('serializes date-range filters', async () => {
    let params = new URLSearchParams()
    server.use(
      http.get('/api/cycles', ({ request }) => {
        params = new URL(request.url).searchParams
        return HttpResponse.json(cyclesFixture)
      }),
    )

    await fetchCycles({ date_from: '2026-07-01', date_to: '2026-07-12' })

    expect(params.get('date_from')).toBe('2026-07-01')
    expect(params.get('date_to')).toBe('2026-07-12')
  })
})

describe('fetchCycleDetail', () => {
  it('URL-encodes the cycle id in the path', async () => {
    let requestPath = ''
    server.use(
      http.get('/api/cycles/:cycleId', ({ request }) => {
        requestPath = new URL(request.url).pathname
        return HttpResponse.json({ ...cyclesFixture[0], cycle_id: 'a/b' })
      }),
    )

    await fetchCycleDetail('a/b')

    // The slash in the id is percent-encoded so it stays a single path segment.
    expect(requestPath).toBe('/api/cycles/a%2Fb')
  })
})

describe('review endpoints', () => {
  it('fetchFunnel returns the parsed funnel payload', async () => {
    await expect(fetchFunnel()).resolves.toEqual(funnelFixture)
  })

  it('fetchHitRate returns the parsed hit-rate payload', async () => {
    await expect(fetchHitRate()).resolves.toEqual(hitRateFixture)
  })

  it('fetchAttribution returns the parsed attribution payload', async () => {
    await expect(fetchAttribution()).resolves.toEqual(attributionFixture)
  })

  it('fetchBias returns the parsed bias payload', async () => {
    await expect(fetchBias()).resolves.toEqual(biasFixture)
  })

  it('fetchPromptVersions returns the parsed version list', async () => {
    await expect(fetchPromptVersions()).resolves.toEqual(promptVersionsFixture)
  })

  it('sends no query string when no filters are given', async () => {
    let requestUrl = ''
    server.use(
      http.get('/api/review/funnel', ({ request }) => {
        requestUrl = request.url
        return HttpResponse.json(funnelFixture)
      }),
    )

    await fetchFunnel()

    expect(new URL(requestUrl).search).toBe('')
  })

  it('serializes since and prompt_version into the query string', async () => {
    let params = new URLSearchParams()
    server.use(
      http.get('/api/review/hit-rate', ({ request }) => {
        params = new URL(request.url).searchParams
        return HttpResponse.json(hitRateFixture)
      }),
    )

    await fetchHitRate({ since: '2026-07-01T00:00:00Z', prompt_version: 'v1.0.0' })

    expect(params.get('since')).toBe('2026-07-01T00:00:00Z')
    expect(params.get('prompt_version')).toBe('v1.0.0')
  })
})

describe('getJSON error handling', () => {
  it('rejects with url and status when the response is not ok', async () => {
    server.use(
      http.get('/api/overview', () => new HttpResponse(null, { status: 503 })),
    )

    await expect(fetchOverview()).rejects.toThrow('/api/overview → 503')
  })
})
