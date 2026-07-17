import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, getToken, type Bill, type Property } from '../lib/api'
import { DEMO, demoExportCsv } from '../lib/demo'
import { money, fmtDate } from '../lib/format'
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Modal,
  PageHeader,
  Pill,
  Spinner,
  inputCls,
} from '../components/ui'

const statusFilters = ['all', 'outstanding', 'overdue', 'paid'] as const

export default function Bills() {
  const [bills, setBills] = useState<Bill[] | null>(null)
  const [properties, setProperties] = useState<Property[]>([])
  const [status, setStatus] = useState<(typeof statusFilters)[number]>('all')
  const [propertyID, setPropertyID] = useState<string>('')
  const [showUpload, setShowUpload] = useState(false)
  const [error, setError] = useState('')
  const [searchParams, setSearchParams] = useSearchParams()
  const q = searchParams.get('q') ?? ''

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (status !== 'all') params.set('status', status)
    if (propertyID) params.set('property_id', propertyID)
    api
      .get<Bill[]>(`/api/bills?${params}`)
      .then(setBills)
      .catch((e) => setError(e.message))
  }, [status, propertyID])

  useEffect(load, [load])
  useEffect(() => {
    api.get<Property[]>('/api/properties').then(setProperties).catch(() => {})
  }, [])

  const needle = q.trim().toLowerCase()
  const visible = (bills ?? []).filter(
    (b) =>
      !needle ||
      `${b.vendor_name} ${b.property_name} ${b.account_number}`.toLowerCase().includes(needle),
  )

  function exportCSV() {
    const params = new URLSearchParams()
    if (propertyID) params.set('property_id', propertyID)
    if (DEMO) {
      demoExportCsv(params)
      return
    }
    params.set('token', getToken() ?? '')
    window.open(`/api/export/csv?${params}`, '_blank')
  }

  return (
    <>
      <PageHeader
        title="Bills"
        subtitle="Every captured bill — auto-scraped or uploaded — in one place."
        actions={
          <>
            <Button variant="secondary" onClick={exportCSV}>
              ⤓ Export CSV
            </Button>
            <Button onClick={() => setShowUpload(true)}>+ Upload bill PDF</Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg bg-stone-100 p-0.5">
          {statusFilters.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium capitalize transition ${
                status === s ? 'bg-white text-stone-900 shadow-sm' : 'text-stone-500 hover:text-stone-800'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
        <select
          className={`${inputCls} w-auto`}
          value={propertyID}
          onChange={(e) => setPropertyID(e.target.value)}
        >
          <option value="">All properties</option>
          {properties.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      {error && <ErrorNote message={error} />}

      {q && (
        <div className="mb-4 flex items-center gap-2 text-sm text-stone-500">
          Showing results for <span className="font-medium text-stone-800">“{q}”</span>
          <button
            onClick={() => setSearchParams({})}
            className="rounded-md border border-stone-200 px-1.5 py-0.5 text-xs text-stone-500 hover:bg-stone-50"
          >
            Clear
          </button>
        </div>
      )}

      <Card>
        {!bills ? (
          <Spinner />
        ) : visible.length === 0 ? (
          <EmptyState
            title="No bills match this view"
            hint="Try clearing filters, upload a PDF, or trigger a scrape from Utility Accounts."
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Vendor</th>
                <th className="px-3 py-3">Property</th>
                <th className="px-3 py-3">Statement</th>
                <th className="px-3 py-3">Due</th>
                <th className="px-3 py-3">Status</th>
                <th className="px-3 py-3">Source</th>
                <th className="px-5 py-3 text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((b) => (
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
                  <td className="px-3 py-3 text-stone-600">{b.property_name || '—'}</td>
                  <td className="px-3 py-3 text-stone-500">{fmtDate(b.statement_date)}</td>
                  <td className="px-3 py-3 text-stone-500">{fmtDate(b.due_date)}</td>
                  <td className="px-3 py-3"><Pill value={b.status} /></td>
                  <td className="px-3 py-3"><Pill value={b.source} /></td>
                  <td className="px-5 py-3 text-right font-medium text-stone-800">
                    {money(b.amount_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {showUpload && (
        <UploadModal
          properties={properties}
          onClose={() => setShowUpload(false)}
          onUploaded={() => {
            setShowUpload(false)
            load()
          }}
        />
      )}
    </>
  )
}

function UploadModal({
  properties,
  onClose,
  onUploaded,
}: {
  properties: Property[]
  onClose: () => void
  onUploaded: () => void
}) {
  const [propertyID, setPropertyID] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!file) return
    setBusy(true)
    setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      if (propertyID) form.append('property_id', propertyID)
      const bill = await api.upload<{ id: number }>('/api/bills/upload', form)
      onUploaded()
      navigate(`/bills/${bill.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
      setBusy(false)
    }
  }

  return (
    <Modal title="Upload a bill PDF" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-stone-500">
          Drop in any utility bill — AI extracts the vendor, amount, and dates automatically.
        </p>
        <Field label="Property (optional)">
          <select className={inputCls} value={propertyID} onChange={(e) => setPropertyID(e.target.value)}>
            <option value="">Unassigned</option>
            {properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
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
