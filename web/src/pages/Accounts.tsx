import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Property, type Provider, type UtilityAccount } from '../lib/api'
import { fmtDateTime } from '../lib/format'
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Modal,
  PageHeader,
  Spinner,
  inputCls,
} from '../components/ui'

export default function Accounts() {
  const [accounts, setAccounts] = useState<UtilityAccount[] | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [properties, setProperties] = useState<Property[]>([])
  const [editing, setEditing] = useState<UtilityAccount | 'new' | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const navigate = useNavigate()

  const load = useCallback(() => {
    api.get<UtilityAccount[]>('/api/utility-accounts').then(setAccounts).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  useEffect(() => {
    api.get<Provider[]>('/api/providers').then(setProviders).catch(() => {})
    api.get<Property[]>('/api/properties').then(setProperties).catch(() => {})
  }, [])

  async function scrapeNow(a: UtilityAccount) {
    setError('')
    setNotice('')
    try {
      await api.post('/api/scrape-jobs', { utility_account_id: a.id })
      setNotice(`Scrape queued for ${a.provider_name} acct ${a.account_number} — track it on the Scrape Jobs page.`)
      setTimeout(() => navigate('/jobs'), 900)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to queue scrape')
    }
  }

  async function remove(a: UtilityAccount) {
    if (!confirm(`Remove ${a.provider_name} account ${a.account_number}?`)) return
    await api.del(`/api/utility-accounts/${a.id}`)
    load()
  }

  return (
    <>
      <PageHeader
        title="Utility Accounts"
        subtitle="Connect provider logins for auto-scraping, or track manual-only accounts."
        actions={<Button onClick={() => setEditing('new')}>+ Add account</Button>}
      />
      {error && <div className="mb-4"><ErrorNote message={error} /></div>}
      {notice && (
        <div className="mb-4 rounded-lg border border-stone-200 bg-stone-100 px-3 py-2 text-sm text-stone-700">
          {notice}
        </div>
      )}
      <Card>
        {!accounts ? (
          <Spinner />
        ) : accounts.length === 0 ? (
          <EmptyState
            title="No utility accounts yet"
            hint="Add an account with portal credentials to start auto-scraping bills."
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Provider</th>
                <th className="px-3 py-3">Property</th>
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
                  <td className="px-3 py-3 text-stone-600">{a.property_name}</td>
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
                    {!a.active && (
                      <span className="ml-1 text-xs text-stone-400">(inactive)</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-stone-500">
                    {a.last_run_at ? fmtDateTime(a.last_run_at) : '—'}
                  </td>
                  <td className="px-3 py-3 text-stone-500">
                    {a.has_credentials ? fmtDateTime(a.next_scrape_at) : '—'}
                    {a.consecutive_failures > 0 && (
                      <div className="text-xs text-[#d03b3b]">
                        {a.consecutive_failures} recent failure{a.consecutive_failures === 1 ? '' : 's'}
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-3 text-right whitespace-nowrap">
                    {a.has_credentials && a.active && (
                      <button
                        onClick={() => scrapeNow(a)}
                        className="mr-3 text-sm font-medium text-stone-500 hover:text-stone-900"
                      >
                        Scrape now
                      </button>
                    )}
                    <button
                      onClick={() => setEditing(a)}
                      className="mr-3 text-sm font-medium text-stone-500 hover:text-stone-900"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => remove(a)}
                      className="text-sm font-medium text-stone-400 hover:text-red-700"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {editing && (
        <AccountModal
          account={editing === 'new' ? null : editing}
          providers={providers}
          properties={properties}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            load()
          }}
        />
      )}
    </>
  )
}

function AccountModal({
  account,
  providers,
  properties,
  onClose,
  onSaved,
}: {
  account: UtilityAccount | null
  providers: Provider[]
  properties: Property[]
  onClose: () => void
  onSaved: () => void
}) {
  const isEdit = account !== null
  const [form, setForm] = useState({
    property_id: account ? String(account.property_id) : '',
    provider_id: account ? String(account.provider_id) : '',
    account_number: account?.account_number ?? '',
    service_address: account?.service_address ?? '',
    username: account?.username ?? '',
    password: '',
    sec_answer: '',
    active: account ? account.active : true,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const provider = providers.find((p) => p.id === Number(form.provider_id))
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }))

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    // Provider and property are immutable on the account; the PUT ignores them,
    // so on edit we only send the mutable fields.
    const body = {
      account_number: form.account_number,
      service_address: form.service_address,
      username: form.username,
      password: form.password, // blank on edit = keep existing credentials
      sec_answer: form.sec_answer,
    }
    try {
      if (isEdit) {
        await api.put(`/api/utility-accounts/${account!.id}`, { ...body, active: form.active })
      } else {
        await api.post('/api/utility-accounts', {
          ...body,
          property_id: Number(form.property_id),
          provider_id: Number(form.provider_id),
        })
      }
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
      setBusy(false)
    }
  }

  return (
    <Modal title={isEdit ? 'Edit utility account' : 'Add utility account'} onClose={onClose} wide>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Property" hint={isEdit ? 'Cannot be changed after creation' : undefined}>
            {isEdit ? (
              <input className={inputCls} value={account!.property_name} disabled />
            ) : (
              <select className={inputCls} value={form.property_id} onChange={set('property_id')} required>
                <option value="">Select…</option>
                {properties.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Provider" hint={isEdit ? 'Cannot be changed after creation' : undefined}>
            {isEdit ? (
              <input className={inputCls} value={account!.provider_name} disabled />
            ) : (
              <select className={inputCls} value={form.provider_id} onChange={set('provider_id')} required>
                <option value="">Select…</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.display_name} ({p.category})
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Account number">
            <input className={inputCls} value={form.account_number} onChange={set('account_number')} required />
          </Field>
          <Field label="Service address" hint="Required by some providers (CNG, Eversource, UI…)">
            <input className={inputCls} value={form.service_address} onChange={set('service_address')} />
          </Field>
        </div>

        <div className="rounded-lg bg-stone-50 p-4 ring-1 ring-inset ring-stone-200">
          <p className="mb-3 text-sm font-medium text-stone-700">
            Portal credentials{' '}
            <span className="font-normal text-stone-400">
              {isEdit ? '— leave password blank to keep existing' : '— leave blank for manual-only'}
            </span>
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Portal username">
              <input className={inputCls} value={form.username} onChange={set('username')} autoComplete="off" />
            </Field>
            <Field
              label="Portal password"
              hint={isEdit ? 'Enter a new password to replace stored credentials' : 'Encrypted with AES-256 before storage'}
            >
              <input
                className={inputCls}
                type="password"
                value={form.password}
                onChange={set('password')}
                autoComplete="new-password"
                placeholder={isEdit && account!.has_credentials ? '••••••••' : undefined}
              />
            </Field>
            {provider?.code === 'fios' && (
              <Field label="Security question answer" hint="Fios asks a security question at login">
                <input className={inputCls} value={form.sec_answer} onChange={set('sec_answer')} />
              </Field>
            )}
          </div>
        </div>

        {isEdit && (
          <label className="flex items-center gap-2 text-sm text-stone-700">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(e) => setForm((f) => ({ ...f, active: e.target.checked }))}
              className="h-4 w-4 rounded border-stone-300"
            />
            Active <span className="text-stone-400">— inactive accounts are skipped by the scheduler</span>
          </label>
        )}

        {error && <ErrorNote message={error} />}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? 'Saving…' : isEdit ? 'Save changes' : 'Add account'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
