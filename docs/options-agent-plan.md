# AI-Driven Options Trading Agent — Architecture & Plan

> **Status:** Planning / pre-build design doc
> **Last updated:** 2026-06-06
> **Broker target:** Alpaca (paper first)
> **Agent structure:** Single-agent, designed to extend to multi-agent

---

## ⚠️ Framing and risk

This is an **engineering and learning experiment**, not a strategy with an expected edge. Treat it accordingly.

- An LLM has **no inherent predictive edge** in markets. The value here is the system you build (data discipline, guardrails, observability, a journal you learn from), not the model's market opinions.
- Options add **time decay (theta)** and **volatility dynamics (IV crush)** that punish being directionally right-but-late. Many naive "AI thinks X goes up → buy calls" systems lose even on correct calls.
- This document is **not financial advice.** Options trading carries substantial risk, including losses that can exceed the premium paid for some strategies. Read the OCC's *Characteristics and Risks of Standardized Options* (the "ODD") before trading. (See references.)
- **Validation bar before any real money:** paper-trade for ~90 days / 100+ trades, compare against a buy-and-hold benchmark on the same universe, confirm every order carries a stop-loss and a position cap, and start live (if ever) with a small fraction of intended capital and a manual kill switch.

---

## 1. Broker decision: Alpaca

**Why Alpaca over Robinhood for an options bot:**

- **Official, automation-sanctioned API** with **free paper trading** — the single most important property for this experiment.
- **Multi-leg ("Level 3") options** support: spreads, straddles, strangles, iron condors, etc., on US-listed equity/ETF options, commission-free.
- Options **chain data includes Greeks and implied volatility**.
- Mature SDK (`alpaca-py`) and a single orders endpoint that handles both single- and multi-leg orders.

**Why not Robinhood (for now):**

- Robinhood shipped an **official agentic trading product** via an MCP server (`https://agent.robinhood.com/mcp/trading`) that plugs into Claude Code/Desktop, ChatGPT, Cursor, etc. — **but it is currently long-equities-only**. No options chain, no Greeks, no options order tool.
- The unofficial `robin-stocks` library *can* trade options, but **using unofficial/reverse-engineered API libraries violates Robinhood's Terms of Service** and risks account termination plus breakage when Robinhood changes its endpoints. There is also **no paper-trading environment**.
- Note: Robinhood's MCP also hands the model an order-placement tool directly. We deliberately do **not** want the model to hold execution authority (see Core Principle 1).

**Other serious options-capable APIs** (for later, if you outgrow Alpaca): **Interactive Brokers** via `ib_async` (most powerful, heavier setup via TWS/Gateway), and **Tradier** (developer-friendly, has a sandbox).

---

## 2. Agent structure decision: single-agent

We are starting **single-agent** and designing so a multi-agent "challenger" is a clean later addition.

**What "multi-agent" actually means.** The term covers three different things; only the third is a real architectural fork:

1. *One agent, many tools* — still single-agent. Calling ten tools is not multi-agent.
2. *Functional decomposition* — separate LLM steps for distinct jobs (news filter → analysis → strategy pick). A pipeline.
3. *Role/debate* — distinct personas (bull, bear, risk manager, trader) that form views and reconcile. **This is the real fork:** multiple independent reasoning contexts producing intermediate judgments.

**Why single-agent first:**

- **Coherence** — one context sees all evidence at once; no information loss between stages.
- **Cost & speed** — multi-agent can be 5–15× the LLM calls per decision, slowing the dev/iteration loop you'll lean on heavily during paper trading.
- **Attribution** — one reasoning trace means when a trade goes wrong you can read exactly *why* and fix it. Debate structures muddy attribution (which persona whiffed?). You can't improve what you can't isolate.

**The honest case for multi-agent (and its catch):**

- *Pro:* an explicit adversarial step forces the bear case and prevents anchoring; **disagreement itself is a useful signal** (diverge strongly → size down or skip).
- *Catch:* two instances of the same model "debating" are **not independent** — they share training, priors, and input data. "Bull and bear agreed" is weak evidence; you may just be amplifying a shared bias. Genuine diversity comes from **different information or methods** (one reasons from IV/vol structure, one from technicals, one from news), **not** from role-play on identical inputs.

**The thing people get backwards:** the most valuable "second opinion" in trading is **deterministic, non-LLM validation**, not another LLM. A risk-manager *function* is more reliable than a risk-manager *persona* that can be talked into things. Risk lives in code (Core Principle 1).

**Decision:** single-agent now; revisit multi-agent only once the journal shows a **specific, measured failure mode** (e.g., persistent bullish skew, or repeatedly ignoring earnings IV crush) that a challenger could target. Then add it surgically and measure whether the journal improves.

---

## 3. Core architecture principles

**Principle 1 — The LLM proposes, code disposes.**
The agent has **no execution tool**. It receives read-only data tools and returns a *structured proposal*. It cannot place an order. Execution happens in plain code **only after** the proposal clears deterministic validation. This enforces the safety property at the architecture level, not by convention.

**Principle 2 — Two loops, not one.**

- A deterministic **exit/risk monitor** runs frequently (every 1–5 min during market hours) and **never touches the LLM**. Stop-losses, profit-targets, and time-stops must be fast and reliable, independent of model availability or cost.
- An **entry-reasoning** loop runs on a slower cadence (a few times a day) and is the **only** place the model is involved.

**Principle 3 — Defined-risk only.**
For this experiment, reject any naked short leg as a hard rule, not a preference.

**Principle 4 — Token discipline at the data layer.**
Pre-filter option chains before they reach the model's context. Garbage-in here costs more than any prompt tweak.

---

## 4. Module layout

```
options_agent/
  orchestrator.py        # cron entrypoints: run_entry_cycle(), run_monitor_cycle()
  config.py              # limits, universe, cadence, playbook params, kill-switch state

  data/
    providers/           # alpaca_data.py (+ polygon.py later) — raw adapters
    market.py            # underlying prices, VIX / regime classification
    chains.py            # chain fetch + PRE-FILTER (liquidity, DTE, strike window)
    greeks_iv.py         # greeks, IV, and computed IV-rank / IV-percentile
    events.py            # earnings, ex-div, macro calendar
    news.py              # headlines / sentiment (optional, phase 2)

  context/
    portfolio.py         # net delta/vega/theta, concentration, P&L
    assembler.py         # builds the compact context bundle the agent sees

  agent/
    reasoner.py          # the single LLM call (Anthropic SDK, tools + structured output)
    prompts.py           # system prompt + the strategy playbook
    schema.py            # TradeProposal models  <-- THE SEAM
    tools.py             # read-only tool definitions
    # challenger.py      # (future multi-agent drop-in — critiques a TradeProposal)

  risk/
    gates.py             # pre-flight gates (market open? blackout? buying power?)
    validator.py         # deterministic proposal validation  <-- THE HARD LAYER
    sizing.py            # contracts = f(conviction, risk budget, max loss)
    limits.py            # all numeric limits in one place

  execution/
    broker.py            # alpaca-py wrapper
    orders.py            # multi-leg construction, limit pricing (never market on options)
    reconcile.py         # broker <-> local state sync; detect fills/expiry/assignment

  monitor/
    exits.py             # deterministic stop / target / DTE exit logic

  state/
    db.py                # SQLite to start, Postgres later
    models.py            # Position, Order, Decision, ContextSnapshot
    journal.py           # decision + outcome logging  <-- OBSERVABILITY CORE

  obs/
    alerts.py            # notifications + kill-switch hooks
    review.py            # journal analytics: hit rate, P&L attribution, bias detection

  tests/
```

---

## 5. The entry-reasoning loop

Several steps can short-circuit **before** the expensive LLM call.

1. **Kill-switch check.** Read the flag first. If `HALT` or `FLATTEN`, skip entry reasoning entirely (the monitor still handles exits).
2. **Reconcile state.** Pull live account/positions/orders from Alpaca; diff against the local DB. Detect fills, expirations, assignments since the last run. *The broker is the source of truth for fills; the DB is the source of truth for intent and rationale.*
3. **Pre-flight gates** (`risk/gates.py`, deterministic). Market open? Outside open/close blackout windows? Buying power available? Under max open positions? If the action space is empty, log `NO_ACTION (gated)` and stop — don't pay for an LLM call with nothing to propose.
4. **Assemble context.** Portfolio state + net Greeks, then per-symbol: price, regime, IV rank, earnings-proximity flag, and a **pre-filtered** chain.
5. **Reason** (`agent/reasoner.py`, the one LLM call). Agent reads context via tools, returns a `TradeProposal`. It places nothing.
6. **Validate** (`risk/validator.py`, deterministic — the hard layer). Schema + risk checks. Reject → structured reason → journal.
7. **Size** (`risk/sizing.py`). Validated strategy + conviction → contract count, capped by risk budget.
8. **Execute** (`execution/`). Limit order at mid-or-better; multi-leg as a single order. Record the broker order ID.
9. **Journal everything** — including `NO_ACTION` and rejections.

### The monitor loop

Much simpler, runs far more often: for each open position, check stop / profit-target / DTE rules and submit closing orders if triggered. No model, no context assembly.

### Cadence

- Monitor: every 1–5 min during market hours.
- Entry reasoning: a few times a day, and **never** in the first/last several minutes of the session (wide spreads, bad fills).

---

## 6. The seam: structured proposal

This object makes guardrails machine-checkable, the journal queryable, and the future challenger a clean bolt-on (it critiques *this*, nothing else changes).

```python
class Leg(BaseModel):
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    strike: float
    expiration: date
    ratio: int = 1

class ExitPlan(BaseModel):
    profit_target_pct: float      # e.g. close at 50% of max profit
    stop_loss_mult: float         # e.g. 2x credit received
    time_stop_dte: int            # e.g. close at 21 DTE regardless

class TradeProposal(BaseModel):
    action: Literal["OPEN", "CLOSE", "ROLL", "NO_ACTION"]
    underlying: str
    strategy: str                 # must be in the allowed playbook
    legs: list[Leg]
    thesis: str                   # directional view
    iv_rationale: str             # WHY this strategy given IV regime — forces vol-awareness
    catalyst_check: str           # explicit earnings/ex-div acknowledgment
    conviction: float             # 0–1, feeds sizing and future disagreement signal
    est_max_loss: float
    est_max_profit: float
    breakevens: list[float]
    net_delta: float
    net_theta: float
    net_vega: float
    exit_plan: ExitPlan
    informed_by: list[str]        # journal entry / position IDs that shaped this
```

Two fields do quiet work:

- **`iv_rationale`** and **`catalyst_check`** are *required* prose. Forcing the model to articulate the volatility regime and earnings situation is how you stop the "directionally right, killed by IV crush" failure mode at the source.
- **`conviction`** is the hook where a future challenger's disagreement scales the position down.

---

## 7. Agent tools (read-only)

- `get_portfolio_state()` — positions, net Greeks, buying power, P&L
- `get_universe_snapshot()` — per symbol: price, IV rank, regime, earnings-proximity flag
- `get_filtered_chain(symbol, dte_window, strategy_hint)` — **the important one**
- `get_events(symbol)` — earnings, ex-div
- `get_news(symbol)` — optional, phase 2
- `get_journal(symbol | position_id)` — relevant past decisions and their outcomes

**`get_filtered_chain` matters more than the agent-count question.** A raw chain is hundreds of contracts × many fields; dumping it into context burns tokens and degrades attention. Pre-filter to: a liquidity threshold (open interest / volume, spread width), the playbook's DTE window, and a strike range (e.g., delta 0.15–0.45 or ±X% of spot). Return a compact table.

---

## 8. The hard layer: validator checks

All deterministic. Every rejection logged with a structured reason (rejections are valuable journal data — they show where the model's instincts diverge from policy).

- Schema valid; strategy in the **allowed playbook** and within your options approval level.
- **Defined-risk only** — reject any naked short leg (hard rule).
- Per-trade max loss ≤ risk cap (% of equity).
- Resulting portfolio net delta / net vega / total theta within configured bands.
- Concentration ≤ N% in any one underlying / sector.
- Liquidity: each leg's spread width ≤ threshold and OI ≥ threshold.
- Max loss is finite and computable.
- Exit plan present and sane.
- Event gate: earnings within blackout window → reject unless explicitly allowed by policy.
- Buying power sufficient; no duplicate/conflicting position; round-trip check if relevant.
- Kill switch not engaged.

---

## 9. Observability, memory, and safety

**Journal (per cycle):** context snapshot (or hash + pointer), the raw `TradeProposal`, the validation result with reasons, the sizing decision, the order(s) and broker IDs, and later the fill price and final outcome when the position closes. This closes the **proposal → outcome** loop, which is the whole point.

**Review engine (`obs/review.py`):** hit rate by strategy, P&L attribution, directional-bias detection, questions like "do we systematically lose on trades opened within 5 DTE of earnings?" This is also how you'll later justify (or reject) the challenger — with a *named, measured* failure mode rather than a hunch.

**Kill switch, two levels:**

- `HALT` — stop new entries; let the monitor run existing positions to their planned exits.
- `FLATTEN` — close everything now.

Both are a flag checked at the top of every cycle, backed by **Alpaca API key revocation** as the ultimate manual stop.

---

## 10. What information the agent needs

Roughly in priority order (a directional opinion is the *least* important input for options):

1. **Volatility context** — implied volatility per contract, plus **IV rank / IV percentile** (where current IV sits vs. its own trailing year). This drives buy-vs-sell-premium decisions. Pair with **realized/historical volatility** to see the vol risk premium. Add IV skew / term structure to go deeper.
2. **The Greeks** — per position and **aggregated across the portfolio**: delta, gamma, theta, vega. Net portfolio Greeks prevent hidden correlated exposure.
3. **Chain & liquidity** — strikes, expirations, bid/ask, mid, volume, open interest, and especially **bid-ask spread width**. Refuse illiquid contracts.
4. **Event calendar** — earnings (IV inflates into earnings, crushes after), ex-dividend (early-assignment risk on short calls), macro (FOMC, CPI, jobs).
5. **Underlying + technicals** — OHLCV, a few indicators (RSI, MACD, ATR, support/resistance), trend, market regime (VIX). Secondary to vol context.
6. **News & sentiment** — headlines, analyst ratings, optional social sentiment. Noisy context, not signal.
7. **Account state** — positions with cost basis, live P&L, buying power, margin, options approval level.
8. **Hard risk parameters** — max position size, max loss per trade, max net portfolio delta/vega, profit-target and stop logic. Enforced in **code**, not by the model.
9. **Memory** — a persistent **trade journal** (decision, rationale, outcome). Highest-leverage item for improving over time and for debugging.

**Playbook framing:** give the agent an explicit mapping of conditions → strategies (e.g., high IV rank → sell defined-risk credit spreads; low IV → debit spreads; neutral + high IV → iron condor) rather than free-forming each run. Constrained reasoning is more reliable and far easier to evaluate.

---

## 11. Build order

The guardrails get built and tested **before** the LLM exists.

1. **State + broker + reconcile** on a paper account. Connect, read positions, place one manual test order, reconcile cleanly.
2. **Data layer** — chain pre-filter + IV rank. Sanity-check IV-rank numbers against a known source before trusting them.
3. **Exit monitor + validator + sizing**, tested against **hand-written** `TradeProposal` objects with **no LLM**. This proves the safety net in isolation.
4. **Agent reasoner + prompts/playbook**, wired in behind the now-trusted guardrails.
5. **Journal + review + alerts + kill switch.**
6. **Long paper run**, iterating from the journal.

> Doing step 3 before step 4 is the step most people skip and regret: when the model later proposes something reckless, you already know with certainty the validator catches it.

---

## References

### Broker — Alpaca
- Options product overview — https://alpaca.markets/options
- Options trading API docs — https://docs.alpaca.markets/us/docs/options-trading
- Multi-leg (Level 3) options changelog — https://docs.alpaca.markets/changelog/multi-leg-level-3-options-trading-in-paper
- Paper trading docs — https://docs.alpaca.markets/us/docs/paper-trading
- "How to trade options with Alpaca" tutorial — https://alpaca.markets/learn/how-to-trade-options-with-alpaca
- Start paper trading guide — https://alpaca.markets/learn/start-paper-trading
- `alpaca-py` SDK — https://github.com/alpacahq/alpaca-py

### Robinhood (context for why it's deferred)
- Agentic Trading overview — https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Trading with your agent (tool list; equities-only) — https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- "Robinhood is now open to agents" (newsroom) — https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/
- Robinhood Trading MCP endpoint — `https://agent.robinhood.com/mcp/trading`
- `robin-stocks` (unofficial; ToS risk) — https://robin-stocks.readthedocs.io/en/latest/

### Other options-capable brokers (future)
- Interactive Brokers `ib_async` (maintained successor to `ib_insync`) — https://github.com/ib-api-reloaded/ib_async
- Tradier API docs — https://documentation.tradier.com/

### Data providers
- Polygon.io (options + Greeks + IV) — https://polygon.io/
- (Alpaca market data is included with the broker links above)

### Agent / orchestration
- Model Context Protocol (MCP) — https://modelcontextprotocol.io/
- Anthropic tool use docs — https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
- LangGraph (stateful agent graphs) — https://github.com/langchain-ai/langgraph
- CrewAI (multi-agent) — https://github.com/crewAIInc/crewAI
- AutoGen (multi-agent) — https://github.com/microsoft/autogen
- Pydantic (the `TradeProposal` schema) — https://docs.pydantic.dev/

### Scheduling
- APScheduler — https://github.com/agronholm/apscheduler
- Prefect — https://github.com/PrefectHQ/prefect

### Backtesting (validate before live)
- optopsy (options-specific) — https://github.com/michaelchu/optopsy
- backtesting.py — https://github.com/kernc/backtesting.py
- vectorbt — https://github.com/polakowo/vectorbt
- backtrader — https://github.com/mementum/backtrader
- QuantConnect LEAN — https://github.com/QuantConnect/Lean

### Reference repos (study the code; most are very new and moving fast)
- staskh/trading_skills (Claude + IBKR options advisor, MCP, ~23 tools) — https://github.com/staskh/trading_skills
- TauricResearch/TradingAgents (multi-agent "trading firm" reference) — https://github.com/TauricResearch/TradingAgents
- JakeNesler/Claude_Prophet (autonomous options + MCP; author advises paper-only) — https://github.com/JakeNesler/Claude_Prophet
- Trade-With-Claude/cbt-framework (backtesting-first, Claude Code) — https://github.com/Trade-With-Claude/cbt-framework
- tradermonty/claude-trading-skills (decision-process toolkit, not auto-trading) — https://github.com/tradermonty/claude-trading-skills
- quant-sentiment-ai/claude-equity-research (equity research plugin) — https://github.com/quant-sentiment-ai/claude-equity-research

### Risk disclosure
- OCC *Characteristics and Risks of Standardized Options* (the "ODD") — https://cdn.robinhood.com/assets/robinhood/legal/Characteristics%20and%20Risks%20of%20Standardized%20Options.pdf

---

*This document is for educational/engineering planning purposes and is not financial, investment, legal, or tax advice. Trading options involves substantial risk of loss.*
