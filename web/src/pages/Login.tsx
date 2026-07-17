import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, setSession, type User } from '../lib/api'
import { DEMO } from '../lib/demo'
import { Button, ErrorNote, Field, inputCls } from '../components/ui'

export default function Login() {
  const [email, setEmail] = useState(DEMO ? 'demo@harborview.example' : '')
  const [password, setPassword] = useState(DEMO ? 'demo1234' : '')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const data = await api.post<{ token: string; user: User }>('/api/login', { email, password })
      setSession(data.token, data.user)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-stone-50 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-stone-900 text-lg font-bold text-white">
            M
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Metered</h1>
          <p className="mt-1 text-sm text-stone-500">
            Every utility bill, captured, extracted, and export-ready.
          </p>
        </div>

        <form
          onSubmit={submit}
          className="space-y-4 rounded-xl border border-stone-200 bg-white p-6 shadow-sm"
        >
          <Field label="Email">
            <input
              className={inputCls}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              required
              autoFocus
            />
          </Field>
          <Field label="Password">
            <input
              className={inputCls}
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
            />
          </Field>
          {error && <ErrorNote message={error} />}
          <Button type="submit" disabled={busy} className="w-full justify-center">
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>

        <p className="mt-4 text-center text-xs text-stone-400">
          Accounts are provisioned by your Metered administrator.
        </p>
      </div>
    </div>
  )
}
