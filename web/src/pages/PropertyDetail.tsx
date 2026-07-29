import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  api,
  openDownload,
  type Bill,
  type Property,
  type UtilityAccount,
} from '../lib/api'
import { DEMO, demoExportCsv } from '../lib/demo'
import { money, fmtDate, fmtDateTime } from '../lib/format'
import { CHART, monthKey, monthLabel } from '../lib/insights'
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Modal,
  Pill,
  Spinner,
  inputCls,
} from '../components/ui'

const tooltipStyle = {
  border: '1px solid #e7e5e4',
  borderRadius: 8,
  fontSize: 12,
  boxShadow: '0 4px 12px rgba(28,25,23,0.06)',
}

export default function PropertyDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [property, setProperty] = useState<Property | null>(null)
  const [bills, setBills] = useState<Bill[] | null>(null)
  const [accounts, setAccounts] = useState<UtilityAccount[]>([])
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [scraping, setScraping] = useState<number | null>(null)
  const [showUpload, setShowUpload] = useState(false)

  // Compose the property view from the existing list endpoints — there is no
  // GET /api/properties/{id}, and both /api/bills?property_id and
  // /api/utility-accounts are already property-aware / filterable client-side.
  const load = useCallback(() => {
    setNotFound(false)
    setError('')
    return Promise.all([
      api.get<Property[]>('/api/properties'),
      api.get<Bill[]>(`/api/bills?property_id=${id}`),
      api.get<UtilityAccount[]>('/api/utility-accounts'),
    ])
      .then(([props, b, a]) => {
        const p = props.find((x) => String(x.id) === id) ?? null
        if (!p) {
          setNotFound(true)
          return
        }
        setProperty(p)
        setBills(b)
        setAccounts(a.filter((acct) => String(acct.property_id) === id))
      })
      .catch((e) => setError(e.message))
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  async function scrapeNow(a: UtilityAccount) {
    setError('')
    setNotice('')
    if (DEMO) {
      setNotice('Demo mode — auto-scraping is disabled. Connect the live app to run a real scrape.')
      return
    }
    setScraping(a.id)
    try {
      await api.post('/api/scrape-jobs', { utility_account_id: a.id })
      setNotice(
        `Scrape queued for ${a.provider_name} acct ${a.account_number} — track it on the Capture Log.`,
      )
      setTimeout(() => navigate('/jobs'), 1200)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to queue scrape')
    } finally {
      setScraping(null)
    }
  }

  // Stat tiles derived client-side from this property's bills. bill.status is
  // already resolved to outstanding | overdue | paid by the API (overdue is
  // derived at read time), so a straight group-by matches the Dashboard tiles.
  const stats = useMemo(() => {
    const acc = {
      outstanding: { cents: 0, count: 0 },
      overdue: { cents: 0, count: 0 },
      paid: { cents: 0, count: 0 },
      total: { cents: 0, count: 0 },
    }
    for (const b of bills ?? []) {
      const bucket = acc[b.status]
      if (bucket) {
        bucket.cents += b.amount_cents
        bucket.count += 1
      }
      acc.total.cents += b.amount_cents
      acc.total.count += 1
    }
    return acc
  }, [bills])

  // Monthly spend for this one property, last 12 months present in the data.
  const spend = useMemo(() => {
    const byMonth = new Map<string, number>()
    for (const b of bills ?? []) {
      const k = monthKey(b)
      byMonth.set(k, (byMonth.get(k) ?? 0) + b.amount_cents / 100)
    }
    return [...byMonth.entries()]
      .map(([month, cents]) => ({ month, cents }))
      .sort((a, b) => a.month.localeCompare(b.month))
      .slice(-12)
  }, [bills])

  function exportCSV() {
    if (DEMO) {
      demoExportCsv(new URLSearchParams({ property_id: String(id) }))
      return
    }
    openDownload('/api/export/csv', { property_id: String(id) })
  }

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (notFound) {
    return (
      <>
        <Link to="/properties" className="text-sm font-medium text-stone-500 hover:text-stone-900">
          ← Properties
        </Link>
        <div className="mt-4">
          <Card>
            <EmptyState
              title="Property not found"
              hint="It may have been removed, or belongs to another organization."
            />
          </Card>
        </div>
      </>
    )
  }
  if (!property || !bills) return <Spinner />

  const tiles = [
    { label: 'Outstanding', ...stats.outstanding, dot: '#b45309' },
    { label: 'Overdue', ...stats.overdue, dot: '#d03b3b' },
    { label: 'Paid', ...stats.paid, dot: '#0ca30c' },
    { label: 'Total captured', ...stats.total, dot: null as string | null },
  ]

  return (
    <>
      <div className="mb-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link to="/properties" className="text-sm font-medium text-stone-500 hover:text-stone-900">
            ← Properties
          </Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-stone-900">
            {property.name}
          </h1>
          <p className="mt-0.5 text-sm text-stone-500">{property.address || 'No address on file'}</p>
        </div>
        <div className="flex items-center gap-2">
          {!DEMO && <Button onClick={() => setShowUpload(true)}>+ Upload bill PDF</Button>}
          <Button variant="secondary" onClick={exportCSV}>
            ⤓ Export CSV
          </Button>
        </div>
      </div>

      {error && <div className="mb-4"><ErrorNote message={error} /></div>}
      {notice && (
        <div className="mb-4 rounded-lg border border-stone-200 bg-stone-100 px-3 py-2 text-sm text-stone-700">
          {notice}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {tiles.map((t) => (
          <Card key={t.label} className="px-5 py-4">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-stone-400">
              {t.dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: t.dot }} />}
              {t.label}
            </div>
            <div className="mt-1.5 text-2xl font-semibold tracking-tight text-stone-900 tabular-nums">
              {money(t.cents)}
            </div>
            <div className="mt-0.5 text-xs text-stone-400">
              {t.count} bill{t.count === 1 ? '' : 's'}
            </div>
          </Card>
        ))}
      </div>

      <Card className="mt-5 p-5">
        <h2 className="text-sm font-semibold text-stone-800">Monthly spend</h2>
        <p className="mb-4 mt-0.5 text-xs text-stone-400">This property, last 12 months</p>
        {spend.length === 0 ? (
          <EmptyState title="No spend yet" hint="Captured bills for this property will chart here." />
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={spend} barCategoryGap="28%">
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
                  labelFormatter={(m) => monthLabel(String(m))}
                  labelStyle={{ color: '#1c1917', fontWeight: 600 }}
                  itemStyle={{ color: '#57534e' }}
                  cursor={{ fill: 'rgba(28,25,23,0.04)' }}
                />
                <Bar dataKey="cents" name="Spend" fill={CHART.bar} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>

      <Card className="mt-5">
        <div className="border-b border-stone-100 px-5 py-3.5">
          <h2 className="text-sm font-semibold text-stone-800">
            Utility accounts <span className="text-stone-400">({accounts.length})</span>
          </h2>
        </div>
        {accounts.length === 0 ? (
          <EmptyState
            title="No utility accounts"
            hint="Add an account on the Utility Accounts page to track this property's providers."
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Provider</th>
                <th className="px-3 py-3">Account #</th>
                <th className="px-3 py-3">Mode</th>
                <th className="px-3 py-3">Last scrape</th>
                <th className="px-3 py-3">Next scrape</th>
                <th className="px-5 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id} className="border-b border-stone-100 last:border-0 hover:bg-stone-50">
                  <td className="px-5 py-3">
                    <div className="font-medium text-stone-800">{a.provider_name}</div>
                    <div className="text-xs text-stone-400">{a.username}</div>
                  </td>
                  <td className="px-3 py-3 text-stone-600">{a.account_number}</td>
                  <td className="px-3 py-3">
                    {a.has_credentials ? (
                      <span className="inline-flex items-center gap-1.5 text-sm text-stone-600">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#0ca30c]" />
                        Auto-capture
                      </span>
                    ) : (
                      <span className="inline-flex items-center rounded-md border border-stone-200 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-stone-500">
                        Manual
                      </span>
                    )}
                    {!a.active && <span className="ml-1 text-xs text-stone-400">(inactive)</span>}
                  </td>
                  <td className="px-3 py-3 text-stone-500">
                    {a.last_run_at ? fmtDateTime(a.last_run_at) : '—'}
                  </td>
                  <td className="px-3 py-3 text-stone-500">
                    {a.has_credentials ? fmtDateTime(a.next_scrape_at) : '—'}
                    {a.consecutive_failures > 0 && (
                      <div className="text-xs text-[#d03b3b]">
                        {a.consecutive_failures} recent failure
                        {a.consecutive_failures === 1 ? '' : 's'}
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-3 text-right whitespace-nowrap">
                    {a.has_credentials && a.active ? (
                      <button
                        onClick={() => scrapeNow(a)}
                        disabled={scraping === a.id}
                        className="text-sm font-medium text-stone-500 hover:text-stone-900 disabled:opacity-50"
                      >
                        {scraping === a.id ? 'Queuing…' : '↻ Scrape now'}
                      </button>
                    ) : (
                      <span className="text-sm text-stone-300">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card className="mt-5">
        <div className="border-b border-stone-100 px-5 py-3.5">
          <h2 className="text-sm font-semibold text-stone-800">
            Bills <span className="text-stone-400">({bills.length})</span>
          </h2>
        </div>
        {bills.length === 0 ? (
          <EmptyState
            title="No bills yet"
            hint="Upload a PDF or trigger a scrape to capture this property's bills."
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Vendor</th>
                <th className="px-3 py-3">Statement</th>
                <th className="px-3 py-3">Due</th>
                <th className="px-3 py-3">Status</th>
                <th className="px-3 py-3">Source</th>
                <th className="px-5 py-3 text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {bills.map((b) => (
                <tr key={b.id} className="border-b border-stone-100 last:border-0 hover:bg-stone-50">
                  <td className="px-5 py-3">
                    <Link
                      to={`/bills/${b.id}`}
                      className="font-medium text-stone-800 hover:underline"
                    >
                      {b.vendor_name}
                    </Link>
                    {b.account_number && (
                      <div className="text-xs text-stone-400">acct {b.account_number}</div>
                    )}
                  </td>
                  <td className="px-3 py-3 text-stone-500">{fmtDate(b.statement_date)}</td>
                  <td className="px-3 py-3 text-stone-500">{fmtDate(b.due_date)}</td>
                  <td className="px-3 py-3"><Pill value={b.status} /></td>
                  <td className="px-3 py-3"><Pill value={b.source} /></td>
                  <td className="px-5 py-3 text-right font-medium text-stone-800 tabular-nums">
                    {money(b.amount_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {showUpload && (
        <PropertyUploadModal
          propertyID={String(id)}
          accounts={accounts}
          onClose={() => setShowUpload(false)}
          onUploaded={(b) => {
            setShowUpload(false)
            setNotice(
              `Uploaded & parsed: ${b.vendor_name} · ${money(b.amount_cents)}.` +
                (b.utility_account_id ? ' Next auto-scrape for that account was rescheduled.' : ''),
            )
            load()
          }}
        />
      )}
    </>
  )
}

function PropertyUploadModal({
  propertyID,
  accounts,
  onClose,
  onUploaded,
}: {
  propertyID: string
  accounts: UtilityAccount[]
  onClose: () => void
  onUploaded: (bill: Bill) => void
}) {
  const [accountID, setAccountID] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!file) return
    setBusy(true)
    setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('property_id', propertyID)
      if (accountID) form.append('utility_account_id', accountID)
      const bill = await api.upload<Bill>('/api/bills/upload', form)
      onUploaded(bill)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
      setBusy(false)
    }
  }

  return (
    <Modal title="Upload a bill PDF" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-stone-500">
          Drop in any utility bill for this property — AI extracts the vendor, amount, and dates
          automatically.
        </p>
        <Field label="Utility account (optional)">
          <select
            className={inputCls}
            value={accountID}
            onChange={(e) => setAccountID(e.target.value)}
          >
            <option value="">Not linked to an account</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.provider_name} · {a.account_number}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-stone-400">
            Linking to an auto-capture account reschedules its next scrape so we don't re-pull this
            period.
          </p>
        </Field>
        <Field label="Bill PDF">
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm text-stone-600 file:mr-3 file:rounded-lg file:border-0 file:bg-stone-100 file:px-3 file:py-2 file:text-sm file:font-medium file:text-stone-700 hover:file:bg-stone-200"
            required
          />
        </Field>
        {error && <ErrorNote message={error} />}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !file}>
            {busy ? 'Extracting…' : 'Upload & extract'}
          </Button>
        </div>
        {busy && (
          <p className="text-xs text-stone-400">
            Reading the PDF with AI — this usually takes a few seconds.
          </p>
        )}
      </form>
    </Modal>
  )
}
