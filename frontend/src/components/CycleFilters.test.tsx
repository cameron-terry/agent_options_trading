import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CycleFilters } from './CycleFilters'

describe('CycleFilters', () => {
  it('buffers symbol input without committing on each keystroke', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<CycleFilters filters={{}} onChange={onChange} />)

    await user.type(screen.getByPlaceholderText(/symbol/), 'spy')

    // Typing must not fire onChange — an exact-match query on a partial symbol
    // would briefly empty the list (see component comment).
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits an uppercased symbol on Enter', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<CycleFilters filters={{}} onChange={onChange} />)

    const input = screen.getByPlaceholderText(/symbol/)
    await user.type(input, 'spy')
    await user.keyboard('{Enter}')

    expect(onChange).toHaveBeenCalledWith({ symbol: 'SPY' })
  })

  it('commits the symbol on blur', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<CycleFilters filters={{}} onChange={onChange} />)

    await user.type(screen.getByPlaceholderText(/symbol/), 'qqq')
    await user.tab()

    expect(onChange).toHaveBeenCalledWith({ symbol: 'QQQ' })
  })

  it('applies an action-type filter on select', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<CycleFilters filters={{}} onChange={onChange} />)

    await user.selectOptions(screen.getByRole('combobox'), 'REJECTED')

    expect(onChange).toHaveBeenCalledWith({ action_type: 'REJECTED' })
  })

  it('wraps the date-from filter to a start-of-day UTC timestamp', () => {
    const onChange = vi.fn()
    const { container } = render(<CycleFilters filters={{}} onChange={onChange} />)
    const dateInputs = container.querySelectorAll('input[type="date"]')

    fireEvent.change(dateInputs[0], { target: { value: '2026-07-01' } })

    expect(onChange).toHaveBeenCalledWith({ date_from: '2026-07-01T00:00:00Z' })
  })

  it('wraps the date-to filter to an end-of-day UTC timestamp', () => {
    const onChange = vi.fn()
    const { container } = render(<CycleFilters filters={{}} onChange={onChange} />)
    const dateInputs = container.querySelectorAll('input[type="date"]')

    fireEvent.change(dateInputs[1], { target: { value: '2026-07-12' } })

    expect(onChange).toHaveBeenCalledWith({ date_to: '2026-07-12T23:59:59Z' })
  })
})
