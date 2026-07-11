import type { ActivityItem } from '../api'
import { formatTime } from '../format'

export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  if (items.length === 0) {
    return <div className="activity-feed activity-feed--empty">no activity yet</div>
  }

  return (
    <ul className="activity-feed">
      {items.map((item) => {
        const [action, ...rest] = item.headline.split(' ')
        return (
          <li key={`${item.kind}-${item.cycle_id ?? ''}-${item.position_id ?? ''}-${item.timestamp}`}>
            <span className="activity-feed__time">{formatTime(item.timestamp)}</span>
            <span className="activity-feed__body">
              <strong>{action}</strong> {rest.join(' ')}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
