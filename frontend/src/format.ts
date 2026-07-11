export function formatCurrency(value: number): string {
  const sign = value < 0 ? '-' : ''
  return `${sign}$${Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`
}

export function formatSignedCurrency(value: number): string {
  const sign = value >= 0 ? '+' : ''
  return `${sign}${formatCurrency(value)}`
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function formatPct(pct: number): string {
  return `${Math.round(pct * 100)}%`
}
