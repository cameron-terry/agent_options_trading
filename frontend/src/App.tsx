import { useEffect, useState } from 'react'
import { fetchOverview, fetchPositions, type OverviewResponse, type PositionSummary } from './api'
import { ActivityFeed } from './components/ActivityFeed'
import { AskScreen } from './components/AskScreen'
import { DecisionsScreen } from './components/DecisionsScreen'
import { EquityCurve } from './components/EquityCurve'
import { KillSwitchChip } from './components/KillSwitchChip'
import { KillSwitchPanel } from './components/KillSwitchPanel'
import { PerformanceScreen } from './components/PerformanceScreen'
import { PositionsTable } from './components/PositionsTable'
import { Tiles } from './components/Tiles'

// Screen switching is local state, not a router — matches the design
// reference's presentational tabs (WP-9.2 decision). Both this and the
// selected cycle live here so a citation from the Ask screen (WP-9.9) can
// deep-link into the Decision explorer just by setting both, and so the
// eventual router swap is contained to one place.
type Screen = 'overview' | 'decisions' | 'performance' | 'ask'

function App() {
  const [screen, setScreen] = useState<Screen>('overview')
  const [selectedCycleId, setSelectedCycleId] = useState<string | null>(null)
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [positions, setPositions] = useState<PositionSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [killSwitchOpen, setKillSwitchOpen] = useState(false)

  const citeCycle = (cycleId: string) => {
    setSelectedCycleId(cycleId)
    setScreen('decisions')
  }

  useEffect(() => {
    let cancelled = false

    const load = () => {
      Promise.all([fetchOverview(), fetchPositions()])
        .then(([overviewRes, positionsRes]) => {
          if (cancelled) return
          setOverview(overviewRes)
          setPositions(positionsRes)
          setError(null)
        })
        .catch((err: Error) => {
          if (cancelled) return
          setError(err.message)
          // A failed fetch shouldn't leave the positions panel claiming to
          // still be loading forever — the error banner above already says
          // what happened, so stop stalling on "loading positions…".
          setPositions((prev) => prev ?? [])
        })
    }

    // WP-9.4: the server pushes a lightweight "table X changed" tick over
    // SSE rather than the row itself — the client already has full-state
    // fetchers (fetchOverview/fetchPositions), so a tick just re-triggers
    // those instead of duplicating response-shaping logic in two places.
    // EventSource.onopen fires both on the initial connect and after every
    // browser-driven auto-reconnect, so one handler covers first load and
    // resync-after-drop — no Last-Event-ID bookkeeping needed (WP-9.4
    // decision, 2026-07-12).
    const source = new EventSource('/api/events')
    source.onopen = load
    source.addEventListener('update', load)
    return () => {
      cancelled = true
      source.close()
    }
  }, [])

  return (
    <main className="frame">
      <header className="console-header">
        <span className="console-header__brand">
          OPTIONS AGENT {overview && <span>/ {overview.mode}</span>}
        </span>
        <nav className="apptabs">
          <span
            className={screen === 'overview' ? 'on' : undefined}
            onClick={() => setScreen('overview')}
          >
            Overview
          </span>
          <span
            className={screen === 'decisions' ? 'on' : undefined}
            onClick={() => setScreen('decisions')}
          >
            Decisions
          </span>
          <span
            className={screen === 'performance' ? 'on' : undefined}
            onClick={() => setScreen('performance')}
          >
            Performance
          </span>
          <span
            className={screen === 'ask' ? 'on' : undefined}
            onClick={() => setScreen('ask')}
          >
            Ask
          </span>
        </nav>
        <div className="console-header__right">
          {overview && (
            <button
              className="kill-switch-chip-button"
              onClick={() => setKillSwitchOpen(true)}
              aria-label="Open kill-switch console"
            >
              <KillSwitchChip state={overview.kill_switch.state} />
            </button>
          )}
          {overview && overview.tiles.account_equity.value !== null && (
            <span className="console-header__equity">
              EQ <b>${overview.tiles.account_equity.value.toLocaleString()}</b>
            </span>
          )}
        </div>
      </header>

      {screen === 'overview' && (
        <div className="console-screen">
          {error && <div className="console-error">Failed to load: {error}</div>}

          {overview && (
            <>
              <Tiles tiles={overview.tiles} />

              <div className="console-panels">
                <section className="panel">
                  <h2>
                    Equity curve{' '}
                    <small>{overview.equity_curve.length} sessions · realized + marked</small>
                  </h2>
                  <EquityCurve points={overview.equity_curve} />
                </section>
                <section className="panel">
                  <h2>
                    Live activity <small>journal + alerts</small>
                  </h2>
                  <ActivityFeed items={overview.activity} />
                </section>
              </div>
            </>
          )}

          <section className="panel">
            <h2>
              Open positions{' '}
              <small>marks from monitor cache — never fetched live by the UI</small>
            </h2>
            <PositionsTable positions={positions} />
          </section>
        </div>
      )}

      {screen === 'decisions' && (
        <DecisionsScreen selectedCycleId={selectedCycleId} onSelectCycle={setSelectedCycleId} />
      )}

      {screen === 'performance' && <PerformanceScreen />}

      {screen === 'ask' && <AskScreen onCiteCycle={citeCycle} />}

      {killSwitchOpen && <KillSwitchPanel onClose={() => setKillSwitchOpen(false)} />}
    </main>
  )
}

export default App