import { setupServer } from 'msw/node'
import { handlers } from './handlers'

// Shared mock server for the whole test run. Lifecycle (listen/reset/close) is
// wired up in ../setup.ts so every test file gets the default handlers and a
// clean slate between tests.
export const server = setupServer(...handlers)
