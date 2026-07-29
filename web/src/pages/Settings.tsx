import { useEffect, useState } from 'react'
import { api, openDownload, getUser, type Bill, type OrgUser } from '../lib/api'
import { fmtDate } from '../lib/format'
import { DEMO, demoExportCsv } from '../lib/demo'
import { Button, Card, PageHeader, Spinner } from '../components/ui'

export default function Settings() {
  const [users, setUsers] = useState<OrgUser[] | null>(null)
  const [billCount, setBillCount] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const user = getUser()

  useEffect(() => {
    api.get<OrgUser[]>('/api/org-users').then(setUsers).catch((e) => setError(e.message))
    api.get<Bill[]>('/api/bills').then((b) => setBillCount(b.length)).catch(() => {})
  }, [])

  if (error) return <p className="text-sm text-red-700">{error}</p>
  if (!users) return <Spinner />

  return (
    <>
      <PageHeader title="Settings" subtitle="Organization, team, and security." />

      {notice && (
        <div className="mb-4 rounded-lg border border-stone-200 bg-stone-100 px-3 py-2 text-sm text-stone-700">
          {notice}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <h2 className="mb-4 text-sm font-semibold text-stone-700">Organization</h2>
          <dl className="divide-y divide-stone-100">
            <Row k="Name" v={user?.org.name ?? '—'} />
            <Row k="Workspace ID" v={`org_${user?.org.id}`} />
            <Row
              k="Plan"
              v={
                <span className="inline-flex items-center rounded-md border border-stone-200 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-stone-600">
                  Pilot · unlimited bills
                </span>
              }
            />
            <Row k="Bills captured" v={billCount == null ? '…' : `${billCount} total`} />
          </dl>
          <p className="mt-4 text-xs text-stone-400">
            Billing and plan changes are handled by your Metered account manager during the pilot.
          </p>
        </Card>

        <Card className="p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-stone-700">Team</h2>
            <Button
              variant="secondary"
              onClick={() =>
                setNotice('Teammate invites are provisioned by your Metered account manager during the pilot — email support@metered.example.')
              }
            >
              + Invite teammate
            </Button>
          </div>
          <ul className="divide-y divide-stone-100">
            {users.map((u) => (
              <li key={u.id} className="flex items-center justify-between py-2.5">
                <div className="flex items-center gap-3">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-stone-100 text-xs font-semibold uppercase text-stone-600">
                    {u.email.slice(0, 2)}
                  </span>
                  <div>
                    <div className="text-sm font-medium text-stone-800">{u.email}</div>
                    <div className="text-xs text-stone-400">Joined {fmtDate(u.created_at)}</div>
                  </div>
                </div>
                <span className="rounded-full bg-stone-100 px-2 py-0.5 text-xs font-medium capitalize text-stone-600 ring-1 ring-inset ring-stone-500/20">
                  {u.role}
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card className="p-5">
          <h2 className="mb-4 text-sm font-semibold text-stone-700">Security</h2>
          <dl className="divide-y divide-stone-100">
            <Row k="Credential storage" v="AES-256-GCM, encrypted at rest" />
            <Row k="Credential access" v="Decrypted only at capture time" />
            <Row k="Tenant isolation" v="Workspace-scoped on every request" />
            <Row k="Sessions" v="Signed tokens, 7-day expiry" />
          </dl>
          <p className="mt-4 text-xs text-stone-400">
            Utility-portal passwords are never displayed, logged, or returned by the API after entry.
          </p>
        </Card>

        <Card className="p-5">
          <h2 className="mb-4 text-sm font-semibold text-stone-700">Your data</h2>
          <p className="mb-4 text-sm text-stone-500">
            Everything Metered captures belongs to you. Export the full bill ledger anytime —
            amounts, dates, properties, and account numbers in an accounting-ready CSV.
          </p>
          <Button
            variant="secondary"
            onClick={() => {
              if (DEMO) return void demoExportCsv(new URLSearchParams())
              openDownload('/api/export/csv')
            }}
          >
            ⤓ Export all data (CSV)
          </Button>
          <div className="mt-6 border-t border-stone-100 pt-4">
            <div className="text-sm font-medium text-stone-700">Close workspace</div>
            <p className="mt-1 text-xs text-stone-400">
              Contact your account manager to close this workspace and receive a final data export.
            </p>
          </div>
        </Card>
      </div>
    </>
  )
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-2.5">
      <dt className="text-sm text-stone-500">{k}</dt>
      <dd className="text-sm text-stone-800">{v}</dd>
    </div>
  )
}
