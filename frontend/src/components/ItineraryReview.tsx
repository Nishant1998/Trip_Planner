import { AlertCircle, ExternalLink, Info } from 'lucide-react'
import type { NormalizedPlan } from '../lib/plan'

export default function ItineraryReview({ plan }: { plan: NormalizedPlan }) {
  const issues = plan.alerts.filter(alert => alert.severity === 'error')
  const notes = plan.alerts.filter(alert => alert.severity !== 'error')
  return (
    <section aria-labelledby="itinerary-review-title" className="space-y-5">
      <div>
        <h3 id="itinerary-review-title" className="text-lg font-bold text-slate-900">Review before booking</h3>
        <p className="mt-1 text-sm leading-relaxed text-slate-600">This is a planning draft. Check routes, opening hours, prices and reservations before relying on it.</p>
      </div>
      {issues.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-amber-950"><AlertCircle className="h-4 w-4" aria-hidden="true" />Needs attention · {issues.length}</h4>
          <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-amber-950">
            {issues.map((alert, index) => <li key={`${index}-${alert.message}`}>{alert.message}</li>)}
          </ul>
        </div>
      )}
      {notes.length > 0 && (
        <details className="rounded-xl border border-slate-200 bg-white p-4 print:hidden">
          <summary className="cursor-pointer rounded font-semibold text-slate-800 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-indigo-600">Other checks and practical notes · {notes.length}</summary>
          <ul className="mt-4 list-disc space-y-3 pl-5 text-sm leading-relaxed text-slate-600">
            {notes.map((alert, index) => <li key={`${index}-${alert.message}`}>{alert.message}</li>)}
          </ul>
        </details>
      )}
      {notes.length > 0 && (
        <div className="hidden print:block">
          <h4 className="text-sm font-semibold">Other checks and practical notes</h4>
          <ul className="mt-3 list-disc space-y-2 pl-5 text-sm">
            {notes.map((alert, index) => <li key={index}>{alert.message}</li>)}
          </ul>
        </div>
      )}
      {Object.keys(plan.budgetHealth.categories).length > 0 && (
        <div className="rounded-xl border border-slate-200 p-4">
          <h4 className="text-sm font-semibold text-slate-900">Estimated cost breakdown</h4>
          <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {Object.entries(plan.budgetHealth.categories).map(([category, value]) => (
              <div key={category} className="flex justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2 text-xs">
                <dt className="capitalize text-slate-600">{category.replace(/_/g, ' ')}</dt><dd className="font-semibold text-slate-800">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      {plan.planningNotes.length > 0 && (
        <div className="rounded-xl border border-slate-200 p-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Info className="h-4 w-4" aria-hidden="true" />Assumptions and cost notes</h4>
          <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-slate-600">
            {plan.planningNotes.map((note, index) => <li key={index}>{note}</li>)}
          </ul>
        </div>
      )}
      {plan.sources.length > 0 && (
        <div className="rounded-xl border border-slate-200 p-4">
          <h4 className="text-sm font-semibold text-slate-900">Sources</h4>
          <ul className="mt-3 space-y-2 text-sm">
            {plan.sources.map((source, index) => <li key={`${source.url}-${index}`}><a href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 text-indigo-700 underline underline-offset-4">{source.title}<ExternalLink className="h-3.5 w-3.5" aria-hidden="true" /></a></li>)}
          </ul>
        </div>
      )}
    </section>
  )
}
