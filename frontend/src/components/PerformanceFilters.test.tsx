import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { PerformanceFilters } from './PerformanceFilters'
import { server } from '../test/msw/server'
import { promptVersionsFixture } from '../test/msw/handlers'

describe('PerformanceFilters', () => {
  it('populates the prompt_version dropdown from the journal', async () => {
    render(<PerformanceFilters filters={{}} onChange={() => {}} />)

    for (const version of promptVersionsFixture) {
      expect(await screen.findByRole('option', { name: version })).toBeInTheDocument()
    }
  })

  it('calls onChange with the selected prompt_version', async () => {
    const onChange = vi.fn()
    render(<PerformanceFilters filters={{}} onChange={onChange} />)
    await screen.findByRole('option', { name: 'v2.1.0' })

    await userEvent.selectOptions(screen.getByDisplayValue('prompt_version: all'), 'v2.1.0')

    expect(onChange).toHaveBeenCalledWith({ prompt_version: 'v2.1.0', since: undefined })
  })

  it('still renders a usable filter row when the version fetch fails', async () => {
    server.use(http.get('/api/review/prompt-versions', () => new HttpResponse(null, { status: 500 })))
    render(<PerformanceFilters filters={{}} onChange={() => {}} />)

    await waitFor(() => {
      expect(screen.getByDisplayValue('prompt_version: all')).toBeInTheDocument()
    })
  })
})
