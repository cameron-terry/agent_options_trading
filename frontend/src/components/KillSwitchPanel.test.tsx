import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { KillSwitchPanel } from './KillSwitchPanel'
import { server } from '../test/msw/server'
import { killSwitchStatusFixture } from '../test/msw/handlers'

describe('KillSwitchPanel', () => {
  it('renders current state, history, and alert-delivery failures', async () => {
    render(<KillSwitchPanel onClose={() => {}} />)

    await screen.findByText('Current state:')
    expect(
      document.querySelector('.kill-switch-panel__state .kill-switch-chip'),
    ).toHaveTextContent('NONE')
    expect(screen.getByText('reconcile mismatch')).toBeInTheDocument()
    expect(screen.getByText('issue resolved')).toBeInTheDocument()
    expect(screen.getByText('Discord webhook timed out')).toBeInTheDocument()
  })

  it('calls onClose when the close button is clicked', async () => {
    const onClose = vi.fn()
    render(<KillSwitchPanel onClose={onClose} />)
    await screen.findByText('Current state:')

    await userEvent.click(screen.getByLabelText('Close'))

    expect(onClose).toHaveBeenCalled()
  })

  it('arms HALT with zero-friction confirmation — reason alone enables submit', async () => {
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')

    await userEvent.click(screen.getByText('Arm HALT'))
    const confirmButton = screen.getByText('Confirm HALT')
    expect(confirmButton).toBeDisabled()

    await userEvent.type(screen.getByPlaceholderText('Why are you doing this?'), 'testing')
    expect(confirmButton).toBeEnabled()

    await userEvent.click(confirmButton)
    await waitFor(() => expect(screen.queryByText('Confirm HALT')).not.toBeInTheDocument())
  })

  it('requires the typed word FLATTEN before submit is enabled', async () => {
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')

    await userEvent.click(screen.getByText('Arm FLATTEN'))
    await userEvent.type(screen.getByPlaceholderText('Why are you doing this?'), 'vega breach')
    const confirmButton = screen.getByText('Confirm FLATTEN')
    expect(confirmButton).toBeDisabled()

    await userEvent.type(screen.getByPlaceholderText('FLATTEN'), 'flatten')
    expect(confirmButton).toBeDisabled() // wrong case must not satisfy the check

    await userEvent.clear(screen.getByPlaceholderText('FLATTEN'))
    await userEvent.type(screen.getByPlaceholderText('FLATTEN'), 'FLATTEN')
    expect(confirmButton).toBeEnabled()
  })

  it('disables Resume when the current state is already NONE', async () => {
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')

    expect(screen.getByText('Resume')).toBeDisabled()
  })

  it('enables Resume and requires typed RESUME confirmation when halted', async () => {
    server.use(
      http.get('/api/killswitch', () =>
        HttpResponse.json({ ...killSwitchStatusFixture, state: 'HALT' }),
      ),
    )
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')
    expect(
      document.querySelector('.kill-switch-panel__state .kill-switch-chip'),
    ).toHaveTextContent('HALT')

    const resumeButton = screen.getByText('Resume')
    expect(resumeButton).toBeEnabled()

    await userEvent.click(resumeButton)
    await userEvent.type(screen.getByPlaceholderText('Why are you doing this?'), 'resolved')
    const confirmButton = screen.getByText('Confirm RESUME')
    expect(confirmButton).toBeDisabled()

    await userEvent.type(screen.getByPlaceholderText('RESUME'), 'RESUME')
    expect(confirmButton).toBeEnabled()
  })

  it('cancel clears the pending form', async () => {
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')

    await userEvent.click(screen.getByText('Arm HALT'))
    await userEvent.type(screen.getByPlaceholderText('Why are you doing this?'), 'testing')
    await userEvent.click(screen.getByText('Cancel'))

    expect(screen.queryByText('Confirm HALT')).not.toBeInTheDocument()
  })

  it('shows the server error message when the action is rejected', async () => {
    server.use(
      http.post('/api/killswitch', () =>
        HttpResponse.json(
          { detail: [{ msg: 'confirmation must be exactly \'FLATTEN\'' }] },
          { status: 422 },
        ),
      ),
    )
    render(<KillSwitchPanel onClose={() => {}} />)
    await screen.findByText('Current state:')

    await userEvent.click(screen.getByText('Arm FLATTEN'))
    await userEvent.type(screen.getByPlaceholderText('Why are you doing this?'), 'test')
    await userEvent.type(screen.getByPlaceholderText('FLATTEN'), 'FLATTEN')
    await userEvent.click(screen.getByText('Confirm FLATTEN'))

    expect(await screen.findByText(/confirmation must be exactly/)).toBeInTheDocument()
  })

  it('renders empty states when history and alert failures are both empty', async () => {
    server.use(
      http.get('/api/killswitch', () =>
        HttpResponse.json({ state: 'NONE', history: [], alert_failures: [] }),
      ),
    )
    render(<KillSwitchPanel onClose={() => {}} />)

    expect(await screen.findByText('No kill-switch history recorded.')).toBeInTheDocument()
    expect(screen.getByText('No delivery failures recorded.')).toBeInTheDocument()
  })
})
