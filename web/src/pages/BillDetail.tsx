import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, downloadUrl, type Bill } from '../lib/api'
import { DEMO, demoPdfUrl } from '../lib/demo'
import { money, fmtDate } from '../lib/format'
import { Button, Card, Pill, Spinner } from '../components/ui'

export default function BillDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [bill, setBill] = useState<Bill | null>(null)
  const [pdfURL, setPdfURL] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api.get<Bill>(`/api/bills/${id}`).then(setBill).catch((e) => setError(e.message))
  }, [id])
  useEffect(load, [load])

  // Resolve a fresh download-scoped URL for the PDF iframe (the session token
  // never goes in the URL). In demo mode the bundled sample is used directly.
  useEffect(() => {
    if (!bill) return
    if (DEMO) {
      setPdfURL(demoPdfUrl())
      return
    }
    let ok = true
    downloadUrl(`/api/bills/${bill.id}/pdf`).then((u) => ok && setPdfURL(u))
    return () => {
      ok = false
    }
  }, [bill])

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (!bill) return <Spinner />

  async function setStatus(status: 'paid' | 'outstanding') {
    await api.patch(`/api/bills/${bill!.id}`, { status })
    load()
  }

  async function remove() {
    if (!confirm('Delete this bill? The PDF record will be removed from the dashboard.')) return
    await api.del(`/api/bills/${bill!.id}`)
    navigate('/bills')
  }

  const fields: [string, React.ReactNode][] = [
    ['Vendor', bill.vendor_name],
    ['Amount', <span className="font-semibold">{money(bill.amount_cents)}</span>],
    ['Statement date', fmtDate(bill.statement_date)],
    ['Due date', fmtDate(bill.due_date)],
    ['Service period', `${fmtDate(bill.service_start)} → ${fmtDate(bill.service_end)}`],
    ['Property', bill.property_name || '—'],
    ['Account #', bill.account_number || '—'],
    ['Category', bill.category || '—'],
    ['Status', <Pill value={bill.status} />],
    ['Source', <Pill value={bill.source} />],
  ]
  if (bill.parse_confidence != null) {
    fields.push(['AI confidence', `${bill.parse_confidence}%`])
  }

  return (
    <>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <Link to="/bills" className="text-sm font-medium text-stone-500 hover:text-stone-900">
            ← Bills
          </Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-stone-900">
            {bill.vendor_name}
          </h1>
          <p className="mt-0.5 text-sm text-stone-500">
            {bill.property_name || 'Unassigned'} · {fmtDate(bill.statement_date)}
          </p>
        </div>
        <div className="flex gap-2">
          {bill.status === 'paid' ? (
            <Button variant="secondary" onClick={() => setStatus('outstanding')}>
              Mark outstanding
            </Button>
          ) : (
            <Button onClick={() => setStatus('paid')}>✓ Mark paid</Button>
          )}
          <Button variant="danger" onClick={remove}>
            Delete
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="p-5 lg:col-span-2 self-start">
          <h2 className="mb-3 text-sm font-semibold text-stone-700">Extracted details</h2>
          <dl className="divide-y divide-stone-100">
            {fields.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between py-2.5">
                <dt className="text-sm text-stone-500">{k}</dt>
                <dd className="text-sm text-stone-800 text-right">{v}</dd>
              </div>
            ))}
          </dl>
        </Card>

        <Card className="overflow-hidden lg:col-span-3">
          <div className="border-b border-stone-100 px-5 py-3 text-sm font-semibold text-stone-700">
            Source PDF
          </div>
          {pdfURL ? (
            <iframe title="Bill PDF" src={pdfURL} className="h-[75vh] w-full" />
          ) : (
            <div className="flex h-[75vh] w-full items-center justify-center">
              <Spinner />
            </div>
          )}
        </Card>
      </div>
    </>
  )
}
