import type { Bill } from './api'

// Muted identity hues drawn from a CVD-validated categorical palette.
export const CATEGORY_COLORS: Record<string, string> = {
  water: '#2a78d6',
  electric: '#eda100',
  gas: '#eb6834',
  internet: '#4a3aa7',
  waste: '#898781',
  other: '#1baf7a',
}

// Property series for stacked charts — validated slot order (CVD-safe),
// never re-ordered or cycled.
export const SERIES_COLORS = ['#2a78d6', '#008300', '#e87ba4', '#eda100']

// Chart chrome ink
export const CHART = {
  grid: '#e7e5e4',
  axis: '#a8a29e',
  bar: '#2a78d6',
}

export const CATEGORY_LABELS: Record<string, string> = {
  water: 'Water & Sewer',
  electric: 'Electric',
  gas: 'Gas',
  internet: 'Internet & Telecom',
  waste: 'Waste',
  other: 'Other',
}

export function billCategory(b: Bill): string {
  return b.category && CATEGORY_COLORS[b.category] ? b.category : 'other'
}

export function categoryTotals(bills: Bill[]): { category: string; cents: number }[] {
  const totals = new Map<string, number>()
  for (const b of bills) {
    const c = billCategory(b)
    totals.set(c, (totals.get(c) ?? 0) + b.amount_cents)
  }
  return [...totals.entries()]
    .map(([category, cents]) => ({ category, cents }))
    .sort((a, b) => b.cents - a.cents)
}

export function monthKey(b: Bill): string {
  return (b.statement_date ?? b.created_at).slice(0, 7)
}

export function monthLabel(key: string): string {
  const [y, m] = key.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString('en-US', {
    month: 'short',
    timeZone: 'UTC',
  })
}

export function daysUntil(iso: string | null): number | null {
  if (!iso) return null
  const due = new Date(iso)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return Math.round((due.getTime() - today.getTime()) / 86_400_000)
}
