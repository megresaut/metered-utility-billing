import { useEffect, useState } from 'react'
import { api, type ScrapeJob } from '../lib/api'
import { fmtDateTime } from '../lib/format'
import { Card, EmptyState, PageHeader, Pill, Spinner } from '../components/ui'

export default function Jobs() {
  const [jobs, setJobs] = useState<ScrapeJob[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .get<ScrapeJob[]>('/api/scrape-jobs')
        .then((j) => alive && setJobs(j))
        .catch((e) => alive && setError(e.message))
    load()
    // Poll while jobs may be in flight.
    const t = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  return (
    <>
      <PageHeader
        title="Capture Log"
        subtitle="Every automated bill pull across your connected provider accounts."
      />
      {error && <p className="mb-4 text-sm text-red-700">{error}</p>}
      <Card>
        {!jobs ? (
          <Spinner />
        ) : jobs.length === 0 ? (
          <EmptyState
            title="No scrape jobs yet"
            hint='Use "Scrape now" on a utility account to pull its latest bill.'
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Job</th>
                <th className="px-3 py-3">Provider</th>
                <th className="px-3 py-3">Property</th>
                <th className="px-3 py-3">Status</th>
                <th className="px-3 py-3">Requested</th>
                <th className="px-5 py-3">Result</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className="border-b border-stone-100 last:border-0 hover:bg-stone-50">
                  <td className="px-5 py-3 font-medium text-stone-700">#{j.id}</td>
                  <td className="px-3 py-3">
                    <div className="font-medium text-stone-800">{j.provider_name}</div>
                    <div className="text-xs text-stone-400">acct {j.account_number}</div>
                  </td>
                  <td className="px-3 py-3 text-stone-600">{j.property_name}</td>
                  <td className="px-3 py-3">
                    <Pill value={j.status} />
                    {j.attempt > 1 && (
                      <span className="ml-1.5 text-xs text-stone-400">attempt {j.attempt}</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-stone-500">
                    {fmtDateTime(j.requested_at)}
                    <div className="text-xs text-stone-400">
                      {j.requested_by === 'scheduler-cron' ? 'scheduled' : 'manual'}
                    </div>
                  </td>
                  <td className="max-w-sm px-5 py-3 text-xs text-stone-500">
                    {j.status === 'failed' ? (
                      <span className="text-red-600">{j.error_message.slice(0, 200)}</span>
                    ) : j.status === 'succeeded' ? (
                      <span className="text-[#006300]">Bill captured ✓</span>
                    ) : j.status === 'running' ? (
                      'In progress…'
                    ) : (
                      'Waiting for worker'
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
