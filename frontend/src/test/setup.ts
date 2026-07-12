// Vitest global setup. Registers jest-dom matchers (toBeInTheDocument, etc.)
// against Vitest's expect, and tears down the rendered DOM after each test.
// Tests import { describe, it, expect } from 'vitest' explicitly (globals are
// off) — this file only wires up the pieces that must be global.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
