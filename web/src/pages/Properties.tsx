import { useCallback, useEffect, useState } from 'react'
import { api, type Property } from '../lib/api'
import { money } from '../lib/format'
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

export default function Properties() {
  const [properties, setProperties] = useState<Property[] | null>(null)
  const [editing, setEditing] = useState<Property | 'new' | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api.get<Property[]>('/api/properties').then(setProperties).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])

  async function remove(p: Property) {
    if (!confirm(`Delete ${p.name}? Its utility accounts will also be removed.`)) return
    await api.del(`/api/properties/${p.id}`)
    load()
  }

  return (
    <>
      <PageHeader
        title="Properties"
        subtitle="The buildings and units whose utility bills you track."
        actions={<Button onClick={() => setEditing('new')}>+ Add property</Button>}
      />
      {error && <ErrorNote message={error} />}
      <Card>
        {!properties ? (
          <Spinner />
        ) : properties.length === 0 ? (
          <EmptyState title="No properties yet" hint="Add your first property to get started." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-stone-100 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400">
                <th className="px-5 py-3">Property</th>
                <th className="px-3 py-3">Utility accounts</th>
                <th className="px-3 py-3">Bills</th>
                <th className="px-3 py-3 text-right">Outstanding</th>
                <th className="px-5 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {properties.map((p) => (
                <tr key={p.id} className="border-b border-stone-100 last:border-0 hover:bg-stone-50">
                  <td className="px-5 py-3">
                    <div className="font-medium text-stone-800">{p.name}</div>
                    <div className="text-xs text-stone-400">{p.address}</div>
                  </td>
                  <td className="px-3 py-3 text-stone-600">{p.account_count}</td>
                  <td className="px-3 py-3 text-stone-600">{p.bill_count}</td>
                  <td className="px-3 py-3 text-right font-medium text-stone-900 tabular-nums">
                    {money(p.outstanding_cents)}
                  </td>
                  <td className="px-5 py-3 text-right">
                    <button
                      onClick={() => setEditing(p)}
                      className="mr-3 text-sm font-medium text-stone-500 hover:text-stone-900"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => remove(p)}
                      className="text-sm font-medium text-stone-400 hover:text-red-700"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {editing && (
        <PropertyModal
          property={editing === 'new' ? null : editing}
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

function PropertyModal({
  property,
  onClose,
  onSaved,
}: {
  property: Property | null
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(property?.name ?? '')
  const [address, setAddress] = useState(property?.address ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (property) {
        await api.put(`/api/properties/${property.id}`, { name, address })
      } else {
        await api.post('/api/properties', { name, address })
      }
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
      setBusy(false)
    }
  }

  return (
    <Modal title={property ? 'Edit property' : 'Add property'} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="12 Harbor Lane"
            required
            autoFocus
          />
        </Field>
        <Field label="Address">
          <input
            className={inputCls}
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="12 Harbor Lane, Norwalk, CT 06854"
          />
        </Field>
        {error && <ErrorNote message={error} />}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
