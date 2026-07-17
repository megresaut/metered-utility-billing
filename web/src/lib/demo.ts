// Demo mode — serves the bundled data snapshot (web/public/demo/*.json) so the
// Vercel marketing/demo deploy is fully interactive without the Go API,
// Postgres, or the Python scrapers. Enabled at build time with VITE_DEMO=1.
// Mutations are applied in-memory only (reset on reload). The real app path in
// api.ts is untouched — when VITE_DEMO is unset, none of this runs.

import type { User } from './api'

export const DEMO = import.meta.env.VITE_DEMO === '1'

export const DEMO_USER: User = {
  id: 1,
  email: 'demo@harborview.example',
  role: 'admin',
  org: { id: 1, name: 'Harborview Property Management' },
}

const cache: Record<string, unknown> = {}

async function load(name: string): Promise<unknown> {
  if (!(name in cache)) {
    const res = await fetch(`${import.meta.env.BASE_URL}demo/${name}.json`)
    cache[name] = await res.json()
  }
  // Return a deep copy so in-memory mutations don't corrupt the cache.
  return structuredClone(cache[name])
}

type Row = Record<string, unknown>

// In-memory status overrides applied on top of the snapshot (mark paid, etc.).
const billStatus = new Map<number, string>()

function withOverrides(bills: Row[]): Row[] {
  return bills.map((b) => {
    const id = b.id as number
    if (billStatus.has(id)) {
      const status = billStatus.get(id)!
      // 'overdue' is derived; store only outstanding|paid and re-derive.
      const derived =
        status === 'outstanding' && b.due_date && (b.due_date as string) < today() ? 'overdue' : status
      return { ...b, status: derived }
    }
    return b
  })
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

function matchStatus(b: Row, status: string | null): boolean {
  if (!status) return true
  return b.status === status
}

// Handle a request in demo mode. Returns the parsed body (any).
export async function demoRequest(path: string, init: RequestInit = {}): Promise<unknown> {
  const method = (init.method ?? 'GET').toUpperCase()
  const url = new URL(path, 'http://demo.local')
  const p = url.pathname

  // Auth
  if (p === '/api/login') return { token: 'demo-token', user: DEMO_USER }
  if (p === '/api/me') return { org: DEMO_USER.org, role: DEMO_USER.role }

  // Mutations — apply in-memory, echo success.
  if (method === 'PATCH' && p.startsWith('/api/bills/')) {
    const id = Number(p.split('/')[3])
    const body = init.body ? JSON.parse(init.body as string) : {}
    if (body.status) billStatus.set(id, body.status)
    return { ok: true }
  }
  if (method === 'DELETE') return { ok: true }
  if (method === 'POST' && p === '/api/scrape-jobs')
    return { job_id: 999, status: 'queued' }
  if (method === 'POST' && p === '/api/scheduler/run') return { enqueued: 0 }
  if (method === 'POST' || method === 'PUT') return { id: 999, ok: true }

  // Collections
  if (p === '/api/bills/summary') return load('bills_summary')
  if (p === '/api/bills') {
    const bills = withOverrides((await load('bills')) as Row[])
    const status = url.searchParams.get('status')
    const propertyId = url.searchParams.get('property_id')
    return bills.filter(
      (b) =>
        (status === 'overdue' ? b.status === 'overdue' : matchStatus(b, status)) &&
        (!propertyId || String(b.property_id) === propertyId),
    )
  }
  if (p.startsWith('/api/bills/')) {
    const id = Number(p.split('/')[3])
    const bills = withOverrides((await load('bills')) as Row[])
    return bills.find((b) => b.id === id) ?? null
  }
  if (p === '/api/properties') return load('properties')
  if (p === '/api/utility-accounts') return load('utility-accounts')
  if (p === '/api/scrape-jobs') return load('scrape-jobs')
  if (p === '/api/providers') return load('providers')
  if (p === '/api/org-users') return load('org-users')

  return []
}

// In demo mode all bill PDFs resolve to the one bundled sample.
export function demoPdfUrl(): string {
  return `${import.meta.env.BASE_URL}demo/sample.pdf`
}

// Client-side CSV export in demo mode (mirrors the API's export columns).
export async function demoExportCsv(params: URLSearchParams): Promise<void> {
  const bills = withOverrides((await load('bills')) as Row[])
  const status = params.get('status')
  const propertyId = params.get('property_id')
  const rows = bills.filter(
    (b) =>
      (status ? b.status === status : true) &&
      (!propertyId || String(b.property_id) === propertyId),
  )
  const header = [
    'Vendor', 'Amount', 'Statement Date', 'Due Date', 'Service Start',
    'Service End', 'Property', 'Account Number', 'Status', 'Source', 'Category',
  ]
  const cents = (v: unknown) => {
    const n = Number(v ?? 0)
    return `${Math.floor(n / 100)}.${String(n % 100).padStart(2, '0')}`
  }
  const esc = (v: unknown) => {
    const s = String(v ?? '')
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [header.join(',')]
  for (const b of rows) {
    lines.push(
      [
        b.vendor_name, cents(b.amount_cents), b.statement_date, b.due_date,
        b.service_start, b.service_end, b.property_name, b.account_number,
        b.status, b.source === 'scrape' ? 'auto' : 'manual', b.category,
      ]
        .map(esc)
        .join(','),
    )
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `metered-bills-${today()}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
