import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api, clearSession, getUser, type ScrapeJob } from '../lib/api'

const nav = [
  { to: '/', label: 'Overview' },
  { to: '/bills', label: 'Bills' },
  { to: '/reports', label: 'Reports' },
  { to: '/properties', label: 'Properties' },
  { to: '/accounts', label: 'Accounts' },
  { to: '/jobs', label: 'Capture Log' },
  { to: '/integrations', label: 'Integrations' },
  { to: '/settings', label: 'Settings' },
]

function useDismiss<T extends HTMLElement>(open: boolean, onClose: () => void) {
  const ref = useRef<T>(null)
  useEffect(() => {
    if (!open) return
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open, onClose])
  return ref
}

function timeAgo(iso: string): string {
  const s = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

const jobDot: Record<string, string> = {
  succeeded: '#0ca30c',
  failed: '#d03b3b',
  running: '#2a78d6',
  queued: '#a8a29e',
}

export default function Layout() {
  const navigate = useNavigate()
  const user = getUser()

  // ----- search (⌘K) -----
  const searchRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  // ----- notifications (real capture activity) -----
  const [notifOpen, setNotifOpen] = useState(false)
  const [jobs, setJobs] = useState<ScrapeJob[]>([])
  const closeNotif = useCallback(() => setNotifOpen(false), [])
  const notifRef = useDismiss<HTMLDivElement>(notifOpen, closeNotif)
  useEffect(() => {
    api.get<ScrapeJob[]>('/api/scrape-jobs').then(setJobs).catch(() => {})
  }, [])
  const recent = jobs.slice(0, 6)
  const hasRecentFailure = jobs.some(
    (j) => j.status === 'failed' && j.requested_at && Date.now() - new Date(j.requested_at).getTime() < 7 * 86400_000,
  )

  // ----- account menu -----
  const [menuOpen, setMenuOpen] = useState(false)
  const closeMenu = useCallback(() => setMenuOpen(false), [])
  const menuRef = useDismiss<HTMLDivElement>(menuOpen, closeMenu)
  const initials = (user?.email ?? '?').slice(0, 2).toUpperCase()

  function submitSearch(e: React.FormEvent) {
    e.preventDefault()
    const q = query.trim()
    navigate(q ? `/bills?q=${encodeURIComponent(q)}` : '/bills')
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-stone-200/80 bg-white">
        {/* Tier 1 — workspace context */}
        <div className="mx-auto flex h-12 max-w-6xl items-center justify-between gap-4 px-6">
          <div className="flex min-w-0 items-center gap-2.5">
            <Link to="/" className="flex shrink-0 items-center gap-2">
              <div className="flex h-6.5 w-6.5 items-center justify-center rounded-md bg-stone-900 text-xs font-bold text-white">
                M
              </div>
              <span className="text-sm font-semibold tracking-tight text-stone-900">Metered</span>
            </Link>
            <span className="select-none text-stone-200">/</span>
            <span className="truncate text-sm text-stone-600">{user?.org.name}</span>
            <span className="shrink-0 rounded border border-stone-200 px-1 py-px text-[10px] font-medium uppercase tracking-wide text-stone-400">
              Pilot
            </span>
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            <form onSubmit={submitSearch} className="relative hidden md:block">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-stone-400"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-3.5-3.5" />
              </svg>
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search bills…"
                className="h-8 w-56 rounded-md border border-stone-200 bg-stone-50 pl-8 pr-10 text-[13px] text-stone-800 placeholder:text-stone-400 focus:border-stone-300 focus:bg-white focus:outline-none"
              />
              <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-stone-200 bg-white px-1 text-[10px] font-medium text-stone-400">
                ⌘K
              </kbd>
            </form>

            {/* notifications */}
            <div className="relative" ref={notifRef}>
              <button
                onClick={() => setNotifOpen((v) => !v)}
                title="Capture activity"
                className={`relative flex h-8 w-8 items-center justify-center rounded-md text-stone-500 hover:bg-stone-100 hover:text-stone-800 ${notifOpen ? 'bg-stone-100 text-stone-800' : ''}`}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" className="h-4 w-4">
                  <path d="M18 9a6 6 0 1 0-12 0c0 6-2.5 7-2.5 7h17S18 15 18 9" strokeLinecap="round" strokeLinejoin="round" />
                  <path d="M10 20a2.2 2.2 0 0 0 4 0" strokeLinecap="round" />
                </svg>
                {hasRecentFailure && (
                  <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-[#d03b3b]" />
                )}
              </button>
              {notifOpen && (
                <div className="absolute right-0 top-10 w-80 rounded-lg border border-stone-200 bg-white py-1 shadow-lg">
                  <div className="flex items-center justify-between px-3.5 py-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                      Capture activity
                    </span>
                    <Link
                      to="/jobs"
                      onClick={closeNotif}
                      className="text-xs font-medium text-stone-500 hover:text-stone-900"
                    >
                      View all
                    </Link>
                  </div>
                  {recent.length === 0 ? (
                    <p className="px-3.5 py-4 text-sm text-stone-400">No capture activity yet.</p>
                  ) : (
                    <ul className="max-h-80 overflow-y-auto">
                      {recent.map((j) => (
                        <li key={j.id}>
                          <Link
                            to="/jobs"
                            onClick={closeNotif}
                            className="flex items-start gap-2.5 px-3.5 py-2 hover:bg-stone-50"
                          >
                            <span
                              className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
                              style={{ background: jobDot[j.status] ?? '#a8a29e' }}
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[13px] text-stone-700">
                                {j.provider_name}{' '}
                                {j.status === 'succeeded'
                                  ? 'bill captured'
                                  : j.status === 'failed'
                                    ? 'capture failed'
                                    : `capture ${j.status}`}
                              </span>
                              <span className="block text-xs text-stone-400">
                                {j.property_name} · {timeAgo(j.requested_at)}
                              </span>
                            </span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>

            {/* help */}
            <a
              href="mailto:support@metered.example"
              title="Contact support"
              className="flex h-8 w-8 items-center justify-center rounded-md text-stone-500 hover:bg-stone-100 hover:text-stone-800"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" className="h-4 w-4">
                <circle cx="12" cy="12" r="9" />
                <path d="M9.2 9a2.9 2.9 0 0 1 5.6 1c0 1.8-2.6 2.2-2.6 3.6" strokeLinecap="round" />
                <circle cx="12" cy="17.3" r="0.6" fill="currentColor" stroke="none" />
              </svg>
            </a>

            {/* account menu */}
            <div className="relative" ref={menuRef}>
              <button
                onClick={() => setMenuOpen((v) => !v)}
                className="ml-0.5 flex h-7 w-7 items-center justify-center rounded-full bg-stone-200 text-[11px] font-semibold text-stone-700 ring-2 ring-transparent transition hover:ring-stone-200"
                title={user?.email}
              >
                {initials}
              </button>
              {menuOpen && (
                <div className="absolute right-0 top-10 w-60 rounded-lg border border-stone-200 bg-white py-1 shadow-lg">
                  <div className="border-b border-stone-100 px-3.5 py-2.5">
                    <div className="truncate text-[13px] font-medium text-stone-800">{user?.email}</div>
                    <div className="truncate text-xs capitalize text-stone-400">
                      {user?.role} · {user?.org.name}
                    </div>
                  </div>
                  <Link
                    to="/settings"
                    onClick={closeMenu}
                    className="block px-3.5 py-2 text-[13px] text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                  >
                    Workspace settings
                  </Link>
                  <a
                    href="mailto:support@metered.example"
                    className="block px-3.5 py-2 text-[13px] text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                  >
                    Contact support
                  </a>
                  <div className="my-1 border-t border-stone-100" />
                  <button
                    onClick={() => {
                      clearSession()
                      navigate('/login')
                    }}
                    className="block w-full px-3.5 py-2 text-left text-[13px] text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                  >
                    Sign out
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Tier 2 — navigation */}
        <nav className="mx-auto flex h-10 max-w-6xl items-stretch gap-0.5 overflow-x-auto px-6">
          {nav.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `relative flex items-center whitespace-nowrap rounded-md px-2.5 text-[13px] transition ${
                  isActive ? 'font-medium text-stone-900' : 'text-stone-500 hover:text-stone-900'
                }`
              }
            >
              {({ isActive }) => (
                <>
                  {label}
                  {isActive && (
                    <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-stone-900" />
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}
