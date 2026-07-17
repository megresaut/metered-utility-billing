import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Provider, type UtilityAccount } from '../lib/api'
import { CATEGORY_COLORS, CATEGORY_LABELS } from '../lib/insights'
import { Card, PageHeader, Spinner } from '../components/ui'

export default function Integrations() {
  const [providers, setProviders] = useState<Provider[] | null>(null)
  const [accounts, setAccounts] = useState<UtilityAccount[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([api.get<Provider[]>('/api/providers'), api.get<UtilityAccount[]>('/api/utility-accounts')])
      .then(([p, a]) => {
        setProviders(p)
        setAccounts(a)
      })
      .catch((e) => setError(e.message))
  }, [])

  const connectedByProvider = useMemo(() => {
    const m = new Map<number, number>()
    for (const a of accounts) {
      if (a.has_credentials) m.set(a.provider_id, (m.get(a.provider_id) ?? 0) + 1)
    }
    return m
  }, [accounts])

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (!providers) return <Spinner />

  const connected = providers.filter((p) => connectedByProvider.has(p.id))
  const categories = [...new Set(providers.map((p) => p.category))].sort()

  return (
    <>
      <PageHeader
        title="Integrations"
        subtitle={`${providers.length} native utility-portal integrations — bills pulled automatically, no forwarding or scanning. ${connected.length} connected for your portfolio.`}
      />

      <div className="mb-6 rounded-xl border border-stone-200 bg-white px-5 py-4 text-sm text-stone-600">
        <span className="font-semibold">Don't see your provider?</span> Every bill still works with
        Metered — drop the PDF on the{' '}
        <Link to="/bills" className="font-medium text-stone-900 underline decoration-stone-300 underline-offset-2 hover:decoration-stone-900">
          Bills page
        </Link>{' '}
        and AI extracts it in seconds. New portal integrations ship regularly.
      </div>

      {categories.map((cat) => (
        <div key={cat} className="mb-8">
          <h2 className="mb-3 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-stone-400">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: CATEGORY_COLORS[cat] ?? '#898781' }} />
            {CATEGORY_LABELS[cat] ?? cat}
          </h2>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {providers
              .filter((p) => p.category === cat)
              .map((p) => {
                const n = connectedByProvider.get(p.id) ?? 0
                return (
                  <Card key={p.id} className="flex items-center gap-4 p-4">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-stone-200 bg-stone-50 text-sm font-semibold text-stone-700">
                      {p.display_name.slice(0, 1)}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold text-stone-800">{p.display_name}</div>
                      <div className="text-xs text-stone-400">Automated portal capture</div>
                    </div>
                    {n > 0 ? (
                      <span className="inline-flex items-center gap-1.5 text-xs font-medium text-stone-600">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#0ca30c]" />
                        Connected{n > 1 ? ` ×${n}` : ''}
                      </span>
                    ) : (
                      <Link
                        to="/accounts"
                        className="text-xs font-medium text-stone-500 hover:text-stone-900"
                      >
                        Connect →
                      </Link>
                    )}
                  </Card>
                )
              })}
          </div>
        </div>
      ))}
    </>
  )
}
