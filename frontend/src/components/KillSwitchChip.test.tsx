import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { KillSwitchChip } from './KillSwitchChip'

describe('KillSwitchChip', () => {
  it('labels and classes the NONE (trading) state', () => {
    render(<KillSwitchChip state="NONE" />)
    const chip = screen.getByText('TRADING · NONE')
    expect(chip).toHaveClass('kill-switch-chip--none')
  })

  it('labels and classes the HALT state', () => {
    render(<KillSwitchChip state="HALT" />)
    expect(screen.getByText('HALTED')).toHaveClass('kill-switch-chip--halt')
  })

  it('labels and classes the FLATTEN state', () => {
    render(<KillSwitchChip state="FLATTEN" />)
    expect(screen.getByText('FLATTENING')).toHaveClass('kill-switch-chip--flatten')
  })
})
