import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http } from 'msw'
import { AskScreen } from './AskScreen'
import { server } from '../test/msw/server'
import { askAnswerFixture, sseResponse } from '../test/msw/handlers'

async function ask(question: string) {
  const user = userEvent.setup()
  render(<AskScreen onCiteCycle={vi.fn()} />)
  const input = screen.getByPlaceholderText(/ask about trades/i)
  await user.type(input, question)
  await user.keyboard('{Enter}')
  return user
}

describe('AskScreen', () => {
  it('shows an empty state before any question is asked', () => {
    render(<AskScreen onCiteCycle={vi.fn()} />)
    expect(screen.getByText(/ask about trades, decisions, or performance/i)).toBeInTheDocument()
  })

  it('streams a question through plan chips, a query block, and the final answer', async () => {
    await ask('How many bull put spreads opened this window?')

    expect(
      screen.getByText('How many bull put spreads opened this window?'),
    ).toBeInTheDocument()

    // Plan chips and the collapsible SQL block appear as query_started/
    // query_result events land.
    expect(await screen.findByText('1 query')).toBeInTheDocument()
    expect(screen.getByText('SELECT-only')).toBeInTheDocument()
    expect(
      screen.getByText("SELECT cycle_id FROM journal_records WHERE strategy='bull_put_spread'"),
    ).toBeInTheDocument()

    // Inline result table from query_result — query by cell role since
    // 'cyc-1' also appears as a citation link further down this same message.
    expect(await screen.findByRole('cell', { name: 'cyc-1' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'cyc-2' })).toBeInTheDocument()

    // Final prose answer + tables-touched chip, once the answer event lands.
    expect(await screen.findByText('2 bull put spreads opened this window.')).toBeInTheDocument()
    expect(screen.getByText('journal_records')).toBeInTheDocument()
  })

  it('renders cited cycle_ids as clickable links into the Decision explorer', async () => {
    const onCiteCycle = vi.fn()
    const user = userEvent.setup()
    render(<AskScreen onCiteCycle={onCiteCycle} />)
    await user.type(screen.getByPlaceholderText(/ask about trades/i), 'How many opened?')
    await user.keyboard('{Enter}')

    const citeLink = await screen.findByRole('link', { name: 'cyc-1' })
    await user.click(citeLink)

    expect(onCiteCycle).toHaveBeenCalledWith('cyc-1')
  })

  it('renders a query_error event as a visible failure, not a silent retry', async () => {
    server.use(
      http.post('/api/ask', () =>
        sseResponse([
          ['query_started', { sql: 'DELETE FROM journal_records' }],
          [
            'query_error',
            { sql: 'DELETE FROM journal_records', error: 'Only SELECT statements are allowed.' },
          ],
          ['answer', { answer_text: "I can't do that.", executed_sql: [], cited_cycle_ids: [], tables_touched: [] }],
        ]),
      ),
    )

    await ask('Delete everything')

    expect(await screen.findByText('Only SELECT statements are allowed.')).toBeInTheDocument()
    expect(await screen.findByText("I can't do that.")).toBeInTheDocument()
  })

  it('renders a terminal error event distinctly from a normal answer', async () => {
    server.use(
      http.post('/api/ask', () =>
        sseResponse([['error', { message: "Model called unknown tool 'cancel_order'" }]]),
      ),
    )

    await ask('Cancel my orders')

    expect(
      await screen.findByText(/Model called unknown tool 'cancel_order'/),
    ).toBeInTheDocument()
  })

  it('sends prior exchanges as capped history on a follow-up question', async () => {
    let capturedBody: unknown
    server.use(
      http.post('/api/ask', async ({ request }) => {
        capturedBody = await request.json()
        return sseResponse(askAnswerFixture)
      }),
    )

    const user = await ask('How many SPY trades?')
    await screen.findByText('2 bull put spreads opened this window.')

    await user.type(screen.getByPlaceholderText(/ask about trades/i), 'And how many hit?')
    await user.keyboard('{Enter}')

    await waitFor(() => {
      expect(capturedBody).toMatchObject({
        question: 'And how many hit?',
        history: [
          {
            question: 'How many SPY trades?',
            answer_text: '2 bull put spreads opened this window.',
          },
        ],
      })
    })
  })

  it('disables the input while a question is in flight', async () => {
    // A stream that stays open until the test closes it, so the in-flight
    // state can actually be observed rather than racing MSW's instant mock
    // resolution.
    let controller: ReadableStreamDefaultController<Uint8Array>
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c
      },
    })
    server.use(
      http.post(
        '/api/ask',
        () =>
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }),
      ),
    )

    const user = userEvent.setup()
    render(<AskScreen onCiteCycle={vi.fn()} />)
    const input = screen.getByPlaceholderText(/ask about trades/i)
    await user.type(input, 'How many opened?')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(input).toBeDisabled())

    const encoder = new TextEncoder()
    controller!.enqueue(
      encoder.encode(
        `event: answer\ndata: ${JSON.stringify({
          answer_text: 'done',
          executed_sql: [],
          cited_cycle_ids: [],
          tables_touched: [],
        })}\n\n`,
      ),
    )
    controller!.close()

    await waitFor(() => expect(input).not.toBeDisabled())
  })
})
