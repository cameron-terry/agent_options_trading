import { useEffect, useState } from 'react'
import { fetchOverview, fetchPositions, type OverviewResponse, type PositionSummary } from './api'
import { ActivityFeed } from './components/ActivityFeed'
import { DecisionsScreen } from './components/DecisionsScreen'
import { EquityCurve } from './components/EquityCurve'
import { KillSwitchChip } from './components/KillSwitchChip'
import { PositionsTable } from './components/PositionsTable'
import { Tiles } from './components/Tiles'

// Screen switching is local state, not a router — matches the design
// reference's presentational tabs (WP-9.2 decision). Performance/Ask aren't
// built yet so their tabs stay inert. Both this and the selected cycle live
// here so WP-9.9's URL-addressable cycle requirement (citations must deep
// link into the Decision explorer) is a contained swap to a router later,
// not an unwind of state scattered across screens.
type Screen = 'overview' | 'decisions'

function App() {
  const [screen, setScreen] = useState<Screen>('overview')
  const [selectedCycleId, setSelectedCycleId] = useState<string | null>(null)
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [positions, setPositions] = useState<PositionSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

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
          <span>Performance</span>
          <span>Ask</span>
        </nav>
        <div className="console-header__right">
          {overview && <KillSwitchChip state={overview.kill_switch.state} />}
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
            <PositionsTable positions={positions ?? []} />
          </section>
        </div>
      )}

      {screen === 'decisions' && (
        <DecisionsScreen selectedCycleId={selectedCycleId} onSelectCycle={setSelectedCycleId} />
      )}
    </main>
  )
}

export default App