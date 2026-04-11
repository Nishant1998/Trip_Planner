import { useCallback, useEffect, useState } from 'react'
import { Activity, CheckCircle2, RefreshCw } from 'lucide-react'
import { apiFetch, readApiError } from '../lib/api'

type Allowance = { used: number; limit: number | null; remaining: number | null; resets_at: string }
type Usage = { access: 'demo' | 'full'; timezone: string; planning: Allowance; chat: Allowance }

function UsageRow({ label, allowance, period }: { label: string; allowance: Allowance; period: string }) {
  const exhausted = allowance.remaining === 0
  const percent = allowance.limit ? Math.min(100, allowance.used / allowance.limit * 100) : 0
  const reset = new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'Asia/Kolkata',
  }).format(new Date(allowance.resets_at)) + ' IST'

  return (
    <div>
      <div className="flex justify-between gap-4 text-sm mb-2">
        <span className="font-semibold text-slate-900">{label}</span>
        <span className="text-slate-600">{allowance.limit === null ? 'Unlimited' : `${allowance.used} of ${allowance.limit} used`}</span>
      </div>
      {allowance.limit !== null && <>
        <div role="progressbar" aria-label={`${label} used ${period}`} aria-valuemin={0}
          aria-valuemax={allowance.limit} aria-valuenow={Math.min(allowance.used, allowance.limit)}
          className="h-2 rounded-full bg-slate-100 overflow-hidden">
          <div className={`h-full rounded-full ${exhausted ? 'bg-amber-500' : 'bg-indigo-600'}`} style={{ width: `${percent}%` }} />
        </div>
        <p className={`text-xs mt-2 ${exhausted ? 'text-amber-800' : 'text-slate-500'}`}>
          {exhausted ? `${label} limit reached` : `${allowance.remaining} remaining ${period}`} · Resets {reset}
        </p>
      </>}
    </div>
  )
}

export default function DemoUsageCard() {
  const [usage, setUsage] = useState<Usage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await apiFetch('/api/users/usage', { cache: 'no-store' })
      if (!response.ok) throw new Error(await readApiError(response))
      setUsage(await response.json())
    } catch {
      setError('Could not load your allowance. Please try again.')
    } finally { setLoading(false) }
  }, [])
  useEffect(() => {
    void load()
    const refresh = () => { void load() }
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [load])

  return (
    <section className="mt-6 bg-white rounded-3xl p-6 sm:p-8 border border-slate-200 shadow-xs" aria-busy={loading}>
      <div className="flex items-center justify-between gap-3 mb-6">
        <div className="flex items-center gap-3">
          <span className="p-2 rounded-xl bg-indigo-50 text-indigo-600"><Activity className="w-5 h-5" /></span>
          <div>
            <h2 className="text-lg font-bold text-slate-900">{usage?.access === 'full' ? 'Full access' : 'Demo usage'}</h2>
            <p className="text-xs text-slate-500 mt-0.5">Your trip planning and chat allowance</p>
          </div>
        </div>
        <button type="button" aria-label="Refresh usage" disabled={loading} onClick={() => { void load() }}
          className="p-2 rounded-xl text-slate-500 hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-indigo-600 disabled:opacity-50">
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>
      {error ? <p role="alert" className="text-sm text-rose-700">{error}</p>
        : !usage ? <p role="status" className="text-sm text-slate-500">Loading your allowance…</p>
        : usage.access === 'full' ? <div className="flex items-start gap-3 p-4 bg-emerald-50 border border-emerald-100 rounded-2xl">
          <CheckCircle2 className="w-5 h-5 text-emerald-600 shrink-0" />
          <div><p className="text-sm font-semibold text-emerald-900">Unlimited planning and chat</p>
            <p className="text-xs text-emerald-800 mt-1">Your account has full access. Monthly planning and daily chat allowances do not apply.</p></div>
        </div> : <>
          <div className="space-y-6">
            <UsageRow label="Trip planning" allowance={usage.planning} period="this month" />
            <UsageRow label="Chat messages" allowance={usage.chat} period="today" />
          </div>
          <p className="text-xs text-slate-500 leading-relaxed mt-6 pt-4 border-t border-slate-100">
            A new plan or a manual regeneration uses one planning run. Viewing saved trips is always available.
            Allowances reset at midnight Indian Standard Time (IST).
          </p>
        </>}
    </section>
  )
}
