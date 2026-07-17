import type { ReactNode } from 'react'

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-stone-200/80 bg-white ${className}`}>
      {children}
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-stone-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-stone-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

// Status is a dot + neutral label, never a colored badge.
const dotColor: Record<string, string> = {
  outstanding: '#b45309',
  overdue: '#d03b3b',
  paid: '#0ca30c',
  queued: '#a8a29e',
  running: '#2a78d6',
  succeeded: '#0ca30c',
  failed: '#d03b3b',
}

const pillLabel: Record<string, string> = {
  scrape: 'Auto',
  upload: 'Manual',
}

export function Pill({ value }: { value: string }) {
  // Source values render as a quiet outline chip; statuses as dot + label.
  if (value === 'scrape' || value === 'upload') {
    return (
      <span className="inline-flex items-center rounded-md border border-stone-200 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-stone-500">
        {pillLabel[value]}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm capitalize text-stone-600">
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: dotColor[value] ?? '#a8a29e' }}
      />
      {value}
    </span>
  )
}

export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  disabled,
  className = '',
}: {
  children: ReactNode
  onClick?: () => void
  type?: 'button' | 'submit'
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  disabled?: boolean
  className?: string
}) {
  const styles = {
    primary: 'bg-stone-900 text-white hover:bg-stone-700',
    secondary: 'bg-white text-stone-700 border border-stone-200 hover:bg-stone-50',
    danger: 'bg-white text-red-700 border border-stone-200 hover:border-red-200 hover:bg-red-50',
    ghost: 'text-stone-600 hover:bg-stone-100',
  }[variant]
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed ${styles} ${className}`}
    >
      {children}
    </button>
  )
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-stone-900/30 backdrop-blur-[2px]" onClick={onClose} />
      <div
        className={`relative w-full ${wide ? 'max-w-2xl' : 'max-w-md'} rounded-xl border border-stone-200 bg-white p-6 shadow-xl max-h-[90vh] overflow-y-auto`}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-stone-900">{title}</h2>
          <button onClick={onClose} className="text-stone-400 hover:text-stone-600 text-xl leading-none">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: ReactNode
  hint?: string
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-stone-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-stone-400">{hint}</span>}
    </label>
  )
}

export const inputCls =
  'w-full rounded-lg border border-stone-200 bg-white px-3 py-2 text-sm text-stone-900 placeholder:text-stone-400 focus:border-stone-400 focus:outline-none focus:ring-2 focus:ring-stone-200'

export function Spinner() {
  return (
    <div className="flex justify-center py-12">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-stone-200 border-t-stone-600" />
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-14 text-center">
      <p className="text-sm font-medium text-stone-600">{title}</p>
      {hint && <p className="mt-1 text-sm text-stone-400">{hint}</p>}
    </div>
  )
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}
    </div>
  )
}

// Table primitives so every list shares one look.
export const thCls = 'px-5 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-400'
export const tdCls = 'px-5 py-3'
