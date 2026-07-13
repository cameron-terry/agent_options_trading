import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { PerformanceScreen } from './PerformanceScreen'
import { server } from '../test/msw/server'

describe('PerformanceScreen', () => {
  it('loads and renders all four review panels', async () => {
    render(<PerformanceScreen />)

    expect(await screen.findByText('Entry-cycle funnel')).toBeInTheDocument()
    expect(screen.getByText('Rejections by rule')).toBeInTheDocument()
    expect(screen.getByText('Hit rate by strategy')).toBeInTheDocument()
    expect(screen.getByText('P&L attribution by underlying')).toBeInTheDocument()
    expect(screen.getByText('Bias monitor')).toBeInTheDocument()
  })

  it('surfaces a load error', async () => {
    server.use(http.get('/api/review/funnel', () => new HttpResponse(null, { status: 500 })))
    render(<PerformanceScreen />)

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
  })
})
