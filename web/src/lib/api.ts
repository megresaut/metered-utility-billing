import { DEMO, demoRequest } from './demo'

const TOKEN_KEY = 'metered.token'
const USER_KEY = 'metered.user'

export type User = {
  id: number
  email: string
  role: string
  org: { id: number; name: string }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  return raw ? (JSON.parse(raw) as User) : null
}

export function setSession(token: string, user: User) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (DEMO) return (await demoRequest(path, init)) as T

  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json')

  const res = await fetch(path, { ...init, headers })
  if (res.status === 401) {
    clearSession()
    if (!location.pathname.startsWith('/login')) location.href = '/login'
    throw new ApiError(401, 'unauthorized')
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const body = await res.json()
      if (body?.error) msg = body.error
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}

// Download links (PDF/CSV) must carry auth in the URL because an iframe/anchor
// can't set an Authorization header. Rather than put the long-lived session
// token in a URL (where it leaks into logs, history, and Referer), mint a
// short-lived, download-scoped token per download.

// downloadUrl resolves a URL with a fresh download token attached — for cases
// that render the URL (e.g. an <iframe src>).
export async function downloadUrl(path: string, params: Record<string, string> = {}): Promise<string> {
  const { token } = await api.post<{ token: string }>('/api/download-token')
  const qs = new URLSearchParams(params)
  qs.set('token', token)
  return `${path}?${qs.toString()}`
}

// openDownload opens a download URL in a new tab. The blank tab is opened
// synchronously to preserve the click gesture (so it isn't popup-blocked), then
// navigated once the short-lived token resolves.
export function openDownload(path: string, params: Record<string, string> = {}) {
  const w = window.open('', '_blank')
  api
    .post<{ token: string }>('/api/download-token')
    .then(({ token }) => {
      const qs = new URLSearchParams(params)
      qs.set('token', token)
      const url = `${path}?${qs.toString()}`
      if (w) w.location.href = url
      else window.location.href = url
    })
    .catch(() => w?.close())
}

// Types mirroring the API
export type Provider = { id: number; code: string; display_name: string; category: string }

export type Property = {
  id: number
  name: string
  address: string
  created_at: string
  account_count: number
  bill_count: number
  outstanding_cents: number
}

export type UtilityAccount = {
  id: number
  property_id: number
  property_name: string
  provider_id: number
  provider_code: string
  provider_name: string
  account_number: string
  service_address: string
  username: string
  has_credentials: boolean
  active: boolean
  next_scrape_at: string | null
  last_run_at: string | null
  consecutive_failures: number
  created_at: string
}

export type Bill = {
  id: number
  property_id: number | null
  property_name: string
  utility_account_id: number | null
  account_number: string
  provider_code: string
  vendor_name: string
  category: string
  amount_cents: number
  statement_date: string | null
  due_date: string | null
  service_start: string | null
  service_end: string | null
  status: 'outstanding' | 'overdue' | 'paid'
  source: 'scrape' | 'upload'
  parse_confidence: number | null
  created_at: string
}

export type ScrapeJob = {
  id: number
  utility_account_id: number
  account_number: string
  provider_code: string
  provider_name: string
  property_name: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  attempt: number
  requested_by: string
  requested_at: string
  started_at: string | null
  finished_at: string | null
  error_message: string
}

export type OrgUser = {
  id: number
  email: string
  role: string
  created_at: string
}

export type Summary = {
  outstanding: { count: number; cents: number }
  overdue: { count: number; cents: number }
  paid: { count: number; cents: number }
  total: { count: number; cents: number }
  monthly_spend: { month: string; property_id: number | null; property_name: string; cents: number }[]
}
