import { useEffect, useMemo, useState } from 'react'
import { api, openDownload, type Bill } from '../lib/api'
import { money } from '../lib/format'
import {
  CATEGORY_COLORS,
  CATEGORY_LABELS,
  billCategory,
  categoryTotals,
  monthKey,
  monthLabel,
} from '../lib/insights'
import { Button, Card, PageHeader, Spinner } from '../components/ui'
import { DEMO, demoExportCsv } from '../lib/demo'

export default function Reports() {
  const [bills, setBills] = useState<Bill[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<Bill[]>('/api/bills').then(setBills).catch((e) => setError(e.message))
  }, [])

  const pivot = useMemo(() => {
    if (!bills) return { months: [] as string[], rows: [] as { property: string; cells: number[]; total: number }[], colTotals: [] as number[] }
    const months = [...new Set(bills.map(monthKey))].sort()
    const props = [...new Set(bills.map((b) => b.property_name || 'Unassigned'))].sort()
    const idx = new Map(months.map((m, i) => [m, i]))
    const rows = props.map((property) => {
      const cells = months.map(() => 0)
      for (const b of bills) {
        if ((b.property_name || 'Unassigned') !== property) continue
        cells[idx.get(monthKey(b))!] += b.amount_cents
      }
      return { property, cells, total: cells.reduce((a, c) => a + c, 0) }
    })
    const colTotals = months.map((_, i) => rows.reduce((a, r) => a + r.cells[i], 0))
    return { months, rows, colTotals }
  }, [bills])

  const vendors = useMemo(() => {
    if (!bills) return []
    const map = new Map<string, { cents: number; count: number; category: string }>()
    for (const b of bills) {
      const cur = map.get(b.vendor_name) ?? { cents: 0, count: 0, category: billCategory(b) }
      cur.cents += b.amount_cents
      cur.count += 1
      map.set(b.vendor_name, cur)
    }
    return [...map.entries()]
      .map(([vendor, v]) => ({ vendor, ...v }))
      .sort((a, b) => b.cents - a.cents)
      .slice(0, 8)
  }, [bills])

  const cats = useMemo(() => (bills ? categoryTotals(bills) : []), [bills])
  const maxCat = cats[0]?.cents ?? 1

  function download(status?: string) {
    if (DEMO) {
      const params = new URLSearchParams()
      if (status) params.set('status', status)
      demoExportCsv(params)
      return
    }
    openDownload('/api/export/csv', status ? { status } : {})
  }

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (!bills) return <Spinner />

  const grand = pivot.colTotals.reduce((a, c) => a + c, 0)

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Spend rollups across your portfolio — ready for your accounting import."
        actions={
          <>
            <Button variant="secondary" onClick={() => download('outstanding')}>
              ⤓ Outstanding CSV
            </Button>
            <Button onClick={() => download()}>⤓ Export all bills</Button>
          </>
        }
      />

      <Card className="overflow-x-auto">
        <div className="border-b border-stone-100 px-5 py-4">
          <h2 className="text-sm font-semibold text-stone-700">Monthly spend by property</h2>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
              <th className="px-5 py-3">Property</th>
              {pivot.months.map((m) => (
                <th key={m} className="px-3 py-3 text-right">{monthLabel(m)}</th>
              ))}
              <th className="px-5 py-3 text-right">Total</th>
            </tr>
          </thead>
          <tbody>
            {pivot.rows.map((r) => (
              <tr key={r.property} className="border-b border-stone-50 hover:bg-stone-50/60">
                <td className="px-5 py-3 font-medium text-stone-800">{r.property}</td>
                {r.cells.map((c, i) => (
                  <td key={i} className="px-3 py-3 text-right text-stone-600">
                    {c ? money(c) : <span className="text-stone-300">—</span>}
                  </td>
                ))}
                <td className="px-5 py-3 text-right font-semibold text-stone-900">{money(r.total)}</td>
              </tr>
            ))}
            <tr className="bg-stone-50/80">
              <td className="px-5 py-3 text-sm font-semibold text-stone-700">Portfolio total</td>
              {pivot.colTotals.map((c, i) => (
                <td key={i} className="px-3 py-3 text-right font-medium text-stone-700">{c ? money(c) : '—'}</td>
              ))}
              <td className="px-5 py-3 text-right font-bold text-stone-900">{money(grand)}</td>
            </tr>
          </tbody>
        </table>
      </Card>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <h2 className="mb-4 text-sm font-semibold text-stone-700">Spend by category</h2>
          <ul className="space-y-3">
            {cats.map((c) => (
              <li key={c.category}>
                <div className="mb-1 flex items-center justify-between text-sm">
                  <span className="text-stone-600">{CATEGORY_LABELS[c.category]}</span>
                  <span className="font-medium text-stone-800">{money(c.cents)}</span>
                </div>
                <div className="h-2 rounded-full bg-stone-100">
                  <div
                    className="h-2 rounded-full"
                    style={{ width: `${Math.max(4, (c.cents / maxCat) * 100)}%`, background: '#2a78d6' }}
                  />
                </div>
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <div className="border-b border-stone-100 px-5 py-4">
            <h2 className="text-sm font-semibold text-stone-700">Top vendors</h2>
          </div>
          <table className="w-full text-sm">
            <tbody>
              {vendors.map((v) => (
                <tr key={v.vendor} className="border-b border-stone-50 last:border-0">
                  <td className="px-5 py-2.5">
                    <span className="flex items-center gap-2 font-medium text-stone-800">
                      <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLORS[v.category] }} />
                      {v.vendor}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-right text-xs text-stone-400">{v.count} bills</td>
                  <td className="px-5 py-2.5 text-right font-medium text-stone-800">{money(v.cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  )
}
