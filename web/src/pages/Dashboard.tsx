import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type Bill, type Summary, type UtilityAccount } from '../lib/api'
import { money, fmtDate } from '../lib/format'
import {
  CATEGORY_COLORS,
  CATEGORY_LABELS,
  CHART,
  SERIES_COLORS,
  billCategory,
  categoryTotals,
  daysUntil,
  monthLabel,
} from '../lib/insights'
import { Card, EmptyState, PageHeader, Pill, Spinner } from '../components/ui'

const tooltipStyle = {
  border: '1px solid #e7e5e4',
  borderRadius: 8,
  fontSize: 12,
  boxShadow: '0 4px 12px rgba(28,25,23,0.06)',
}

export default function Dashboard() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [bills, setBills] = useState<Bill[] | null>(null)
  const [accounts, setAccounts] = useState<UtilityAccount[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([
      api.get<Summary>('/api/bills/summary'),
      api.get<Bill[]>('/api/bills'),
      api.get<UtilityAccount[]>('/api/utility-accounts'),
    ])
      .then(([s, b, a]) => {
        setSummary(s)
        setBills(b)
        setAccounts(a)
      })
      .catch((e) => setError(e.message))
  }, [])

  const chart = useMemo(() => {
    if (!summary) return { data: [], properties: [] as string[] }
    const properties = [...new Set(summary.monthly_spend.map((s) => s.property_name))]
    const byMonth = new Map<string, Record<string, number | string>>()
    for (const row of summary.monthly_spend) {
      const entry = byMonth.get(row.month) ?? { month: row.month }
      entry[row.property_name] = ((entry[row.property_name] as number) ?? 0) + row.cents / 100
      byMonth.set(row.month, entry)
    }
    return {
      data: [...byMonth.values()].sort((a, b) => String(a.month).localeCompare(String(b.month))),
      properties,
    }
  }, [summary])

  const cats = useMemo(() => (bills ? categoryTotals(bills) : []), [bills])
  const maxCat = cats[0]?.cents ?? 1

  const upcoming = useMemo(
    () =>
      (bills ?? [])
        .filter((b) => b.status !== 'paid' && b.due_date)
        .sort((a, b) => (a.due_date! < b.due_date! ? -1 : 1))
        .slice(0, 6),
    [bills],
  )

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (!summary || !bills) return <Spinner />

  const connected = accounts.filter((a) => a.has_credentials).length
  const autoCaptured = bills.filter((b) => b.source === 'scrape').length
  const autoPct = bills.length ? Math.round((autoCaptured / bills.length) * 100) : 0

  const stats = [
    { label: 'Outstanding', cents: summary.outstanding.cents, count: summary.outstanding.count, dot: '#b45309' },
    { label: 'Overdue', cents: summary.overdue.cents, count: summary.overdue.count, dot: '#d03b3b' },
    { label: 'Paid', cents: summary.paid.cents, count: summary.paid.count, dot: '#0ca30c' },
    { label: 'Total captured', cents: summary.total.cents, count: summary.total.count, dot: null },
  ]

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle={`${connected} of ${accounts.length} accounts on auto-capture · ${autoPct}% of bills arrive hands-free`}
      />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {stats.map((s) => (
          <Card key={s.label} className="px-5 py-4">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-stone-400">
              {s.dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.dot }} />}
              {s.label}
            </div>
            <div className="mt-1.5 text-2xl font-semibold tracking-tight text-stone-900 tabular-nums">
              {money(s.cents)}
            </div>
            <div className="mt-0.5 text-xs text-stone-400">
              {s.count} bill{s.count === 1 ? '' : 's'}
            </div>
          </Card>
        ))}
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-3">
        <Card className="p-5 lg:col-span-2">
          <h2 className="text-sm font-semibold text-stone-800">Monthly spend</h2>
          <p className="mb-4 mt-0.5 text-xs text-stone-400">By property, last 7 months</p>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart.data} barCategoryGap="28%">
                <CartesianGrid stroke={CHART.grid} strokeDasharray="0" vertical={false} />
                <XAxis
                  dataKey="month"
                  tick={{ fontSize: 11, fill: CHART.axis }}
                  tickFormatter={(m: string) => monthLabel(m)}
                  axisLine={{ stroke: CHART.grid }}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: CHART.axis }}
                  tickFormatter={(v: number) => `$${v.toLocaleString()}`}
                  axisLine={false}
                  tickLine={false}
                  width={48}
                />
                <Tooltip
                  formatter={(v) => `$${Number(v).toFixed(2)}`}
                  contentStyle={tooltipStyle}
                  labelStyle={{ color: '#1c1917', fontWeight: 600 }}
                  itemStyle={{ color: '#57534e' }}
                  cursor={{ fill: 'rgba(28,25,23,0.04)' }}
                />
                <Legend
                  wrapperStyle={{ fontSize: 12, color: '#57534e' }}
                  iconType="circle"
                  iconSize={7}
                />
                {chart.properties.map((p, i) => (
                  <Bar
                    key={p}
                    dataKey={p}
                    stackId="spend"
                    fill={SERIES_COLORS[i % SERIES_COLORS.length]}
                    stroke="#ffffff"
                    strokeWidth={1}
                    radius={i === chart.properties.length - 1 ? [3, 3, 0, 0] : undefined}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="p-5">
          <h2 className="text-sm font-semibold text-stone-800">Spend by category</h2>
          <p className="mb-4 mt-0.5 text-xs text-stone-400">All time</p>
          <ul className="space-y-4">
            {cats.map((c) => (
              <li key={c.category}>
                <div className="mb-1.5 flex items-baseline justify-between text-sm">
                  <span className="text-stone-600">{CATEGORY_LABELS[c.category]}</span>
                  <span className="font-medium text-stone-900 tabular-nums">{money(c.cents)}</span>
                </div>
                <div className="h-1.5 rounded-full bg-stone-100">
                  <div
                    className="h-1.5 rounded-full"
                    style={{ width: `${Math.max(3, (c.cents / maxCat) * 100)}%`, background: CHART.bar }}
                  />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-3">
        <Card>
          <div className="border-b border-stone-100 px-5 py-3.5">
            <h2 className="text-sm font-semibold text-stone-800">Upcoming payments</h2>
          </div>
          {upcoming.length === 0 ? (
            <EmptyState title="Nothing due" hint="All caught up — no unpaid bills with due dates." />
          ) : (
            <ul className="divide-y divide-stone-100">
              {upcoming.map((b) => {
                const d = daysUntil(b.due_date)
                return (
                  <li key={b.id}>
                    <Link
                      to={`/bills/${b.id}`}
                      className="flex items-center justify-between px-5 py-3 hover:bg-stone-50"
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-stone-800">{b.vendor_name}</div>
                        <div className="text-xs text-stone-400">{b.property_name}</div>
                      </div>
                      <div className="ml-3 text-right">
                        <div className="text-sm font-medium text-stone-900 tabular-nums">
                          {money(b.amount_cents)}
                        </div>
                        <div
                          className={`text-xs ${
                            d != null && d < 0
                              ? 'font-medium text-[#d03b3b]'
                              : 'text-stone-400'
                          }`}
                        >
                          {d == null ? '—' : d < 0 ? `${-d}d overdue` : d === 0 ? 'due today' : `due in ${d}d`}
                        </div>
                      </div>
                    </Link>
                  </li>
                )
              })}
            </ul>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="flex items-center justify-between border-b border-stone-100 px-5 py-3.5">
            <h2 className="text-sm font-semibold text-stone-800">Recent bills</h2>
            <Link to="/bills" className="text-xs font-medium text-stone-500 hover:text-stone-900">
              View all →
            </Link>
          </div>
          <table className="w-full text-sm">
            <tbody>
              {bills.slice(0, 7).map((b) => (
                <tr key={b.id} className="border-b border-stone-100 last:border-0 hover:bg-stone-50">
                  <td className="px-5 py-3">
                    <Link to={`/bills/${b.id}`} className="font-medium text-stone-800 hover:underline">
                      {b.vendor_name}
                    </Link>
                    <div className="text-xs text-stone-400">{b.property_name || '—'}</div>
                  </td>
                  <td className="px-3 py-3">
                    <span className="inline-flex items-center gap-1.5 text-xs text-stone-500">
                      <span
                        className="h-1.5 w-1.5 rounded-full"
                        style={{ background: CATEGORY_COLORS[billCategory(b)] }}
                      />
                      {CATEGORY_LABELS[billCategory(b)]}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-stone-500">{fmtDate(b.statement_date)}</td>
                  <td className="px-3 py-3"><Pill value={b.status} /></td>
                  <td className="px-3 py-3"><Pill value={b.source} /></td>
                  <td className="px-5 py-3 text-right font-medium text-stone-900 tabular-nums">
                    {money(b.amount_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  )
}
