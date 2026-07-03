import { useEffect, useState } from 'react'
import { fetchOverview, fetchPositions, type OverviewResponse, type PositionSummary } from './api'
import { ActivityFeed } from './components/ActivityFeed'
import { EquityCurve } from './components/EquityCurve'
import { KillSwitchChip } from './components/KillSwitchChip'
import { PositionsTable } from './components/PositionsTable'
import { Tiles } from './components/Tiles'

// v1 refresh strategy (WP-9.2 decision, 2026-07-03): simple client polling.
// WP-9.4's SSE stream isn't built yet — this interval is swapped for a push
// subscription once it lands, with no change to the components above.
const POLL_INTERVAL_MS = 20_000

function App() {
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
    <main className="console">
      <header className="console-header">
        <span className="console-header__brand">OPTIONS AGENT</span>
        {overview && <KillSwitchChip state={overview.kill_switch.state} />}
        {overview && overview.tiles.account_equity.value !== null && (
          <span className="console-header__equity">
            EQ ${overview.tiles.account_equity.value.toLocaleString()}
          </span>
        )}
      </header>

      {error && <div className="console-error">Failed to load: {error}</div>}

      {overview && (
        <>
          <Tiles tiles={overview.tiles} />

          <div className="console-panels">
            <section className="panel">
              <h2>Equity Curve</h2>
              <EquityCurve points={overview.equity_curve} />
            </section>
            <section className="panel">
              <h2>Live Activity</h2>
              <ActivityFeed items={overview.activity} />
            </section>
          </div>
        </>
      )}

      <section className="panel">
        <h2>Open Positions</h2>
        <PositionsTable positions={positions ?? []} />
      </section>
    </main>
  )
}

export default App