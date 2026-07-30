import { describe, it, expect } from 'vitest'
import { formatCurrency, formatPrice, formatSignedCurrency, formatPct, formatTime } from './format'

describe('formatCurrency', () => {
  it('formats a whole dollar amount with no cents', () => {
    expect(formatCurrency(500)).toBe('$500')
  })

  it('rounds to the nearest dollar', () => {
    expect(formatCurrency(12.7)).toBe('$13')
  })

  it('prefixes negatives with a minus before the dollar sign', () => {
    expect(formatCurrency(-42)).toBe('-$42')
  })

  it('renders zero without a sign', () => {
    expect(formatCurrency(0)).toBe('$0')
  })

  it('groups thousands', () => {
    expect(formatCurrency(1234)).toBe('$1,234')
  })
})

describe('formatPrice', () => {
  it('keeps two decimal places even for sub-dollar values', () => {
    expect(formatPrice(0.18)).toBe('$0.18')
    expect(formatPrice(0.15)).toBe('$0.15')
  })

  it('does not collapse distinct values that formatCurrency would round together', () => {
    expect(formatPrice(2.1)).toBe('$2.10')
    expect(formatPrice(1.45)).toBe('$1.45')
  })

  it('prefixes negatives with a minus before the dollar sign', () => {
    expect(formatPrice(-2.1)).toBe('-$2.10')
  })

  it('groups thousands', () => {
    expect(formatPrice(1234.5)).toBe('$1,234.50')
  })
})

describe('formatSignedCurrency', () => {
  it('adds an explicit plus for non-negative values', () => {
    expect(formatSignedCurrency(1000)).toBe('+$1,000')
    expect(formatSignedCurrency(0)).toBe('+$0')
  })

  it('keeps the minus for negative values', () => {
    expect(formatSignedCurrency(-1000)).toBe('-$1,000')
  })
})

describe('formatPct', () => {
  it('converts a fraction to a rounded whole percent', () => {
    expect(formatPct(0.5)).toBe('50%')
    expect(formatPct(0.333)).toBe('33%')
  })
})

describe('formatTime', () => {
  // Locale/timezone-dependent, so assert the shape (HH:MM, 24h) rather than an
  // exact wall-clock value to keep the test deterministic across environments.
  it('renders a 24-hour HH:MM string', () => {
    expect(formatTime('2026-07-12T14:05:00Z')).toMatch(/^\d{2}:\d{2}$/)
  })
})
