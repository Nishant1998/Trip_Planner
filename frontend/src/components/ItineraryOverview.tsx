import { AlertCircle, Calendar, Clock3, Download, RefreshCw, Users, Wallet } from 'lucide-react'
import type { NormalizedPlan } from '../lib/plan'

interface Props {
  plan: NormalizedPlan
  onReview: () => void
  onRefresh: () => void
  refreshing: boolean
  refreshError: string | null
}

function displayDate(value?: string, includeYear = true) {
  if (!value) return 'Dates to confirm'
  const date = new Date(`${value.slice(0, 10)}T00:00:00Z`)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('en-IN', {
    day: 'numeric', month: 'short', year: includeYear ? 'numeric' : undefined, timeZone: 'UTC',
  }).format(date)
}

export default function ItineraryOverview({ plan, onReview, onRefresh, refreshing, refreshError }: Props) {
  const issues = plan.alerts.filter(alert => alert.severity === 'error').length
  const needsReview = issues > 0 || plan.alerts.length > 0 || plan.deliveryStatus?.toLowerCase().includes('draft')
  const firstDate = plan.days[0]?.date
  const lastDate = plan.days[plan.days.length - 1]?.date
  const dates = firstDate === lastDate ? displayDate(firstDate)
    : `${displayDate(firstDate, firstDate?.slice(0, 4) !== lastDate?.slice(0, 4))} – ${displayDate(lastDate)}`
  const stats = [
    { label: 'Dates', value: dates, Icon: Calendar },
    { label: 'Duration', value: `${plan.durationDays} ${plan.durationDays === 1 ? 'day' : 'days'}`, Icon: Clock3 },
    { label: 'Travellers', value: `${plan.travelers.adults} ${plan.travelers.adults === 1 ? 'adult' : 'adults'}${plan.travelers.children ? ` · ${plan.travelers.children} ${plan.travelers.children === 1 ? 'child' : 'children'}` : ''}`, Icon: Users },
    { label: 'Trip budget', value: `${plan.currency} ${plan.budgetTotal}`, Icon: Wallet },
  ]

  return (
    <header className="border-b border-slate-200 bg-white px-4 py-5 sm:px-6 print:mb-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-widest text-indigo-600">Your trip · itinerary draft</p>
          <h2 className="break-words text-2xl font-bold tracking-tight text-slate-900">{plan.destination}</h2>
        </div>
        <button type="button" aria-label="Export itinerary as PDF" title="Export itinerary as PDF" onClick={() => window.print()}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-slate-200 px-3 text-xs font-semibold text-slate-700 transition hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 print:hidden">
          <Download className="h-4 w-4" aria-hidden="true" /><span className="hidden sm:inline">Export PDF</span>
        </button>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 xl:grid-cols-4">
        {stats.map(({ label, value, Icon }) => (
          <div key={label} className="min-w-0">
            <dt className="flex items-center gap-1.5 text-[11px] text-slate-500"><Icon className="h-3.5 w-3.5" aria-hidden="true" />{label}</dt>
            <dd className="mt-1 break-words text-xs font-semibold leading-relaxed text-slate-800">{value}</dd>
          </div>
        ))}
      </dl>
      {plan.summary && <p className="mt-4 text-sm leading-relaxed text-slate-600">{plan.summary}</p>}

      <div className={`mt-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs ${needsReview ? 'border-amber-200 bg-amber-50 text-amber-950' : 'border-slate-200 bg-slate-50 text-slate-700'}`}>
        <div className="flex min-w-0 items-start gap-2">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <p className="leading-relaxed"><span className="font-semibold">{issues ? `${issues} ${issues === 1 ? 'issue needs' : 'issues need'} review.` : 'Draft itinerary.'}</span> Confirm unverified details before booking.</p>
        </div>
        <button type="button" onClick={onReview}
          className="min-h-11 shrink-0 rounded-md px-2 font-semibold underline decoration-amber-300 underline-offset-4 hover:bg-amber-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 print:hidden">
          Review checklist
        </button>
      </div>
      {plan.budgetHealth.withinBudget === false && <p className="mt-2 text-xs font-semibold text-amber-900">Estimated costs may exceed your trip budget.</p>}

      {plan.displayRefreshRequired && Boolean(plan.placeIds?.length) && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600 print:hidden">
          <p>Saved place names and details need refreshing.</p>
          <button type="button" onClick={onRefresh} disabled={refreshing}
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-slate-200 px-3 font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600">
            <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
            {refreshing ? 'Refreshing…' : 'Refresh place details'}
          </button>
          {refreshError && <p role="alert" className="w-full text-amber-900">{refreshError}</p>}
        </div>
      )}
    </header>
  )
}
