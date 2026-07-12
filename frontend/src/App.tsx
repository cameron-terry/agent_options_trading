import { useEffect, useState } from 'react'
import { fetchOverview, fetchPositions, type OverviewResponse, type PositionSummary } from './api'
import { ActivityFeed } from './components/ActivityFeed'
import { DecisionsScreen } from './components/DecisionsScreen'
import { EquityCurve } from './components/EquityCurve'
import { KillSwitchChip } from './components/KillSwitchChip'
import { PositionsTable } from './components/PositionsTable'
import { Tiles } from './components/Tiles'

// v1 refresh strategy (WP-9.2 decision, 2026-07-03): simple client polling.
// WP-9.4's SSE stream isn't built yet — this interval is swapped for a push
// subscription once it lands, with no change to the components above.
const POLL_INTERVAL_MS = 20_000

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

    load()
    const interval = setInterval(load, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
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