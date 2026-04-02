import { AlertCircle, Calendar, Hotel, Info, Map } from 'lucide-react'

export type ItinerarySection = 'timeline' | 'map' | 'stays' | 'tips' | 'review'

const sections = [
  { id: 'timeline', label: 'Timeline', Icon: Calendar },
  { id: 'map', label: 'Map & stops', Icon: Map },
  { id: 'stays', label: 'Stays & dining', Icon: Hotel },
  { id: 'tips', label: 'Tips & packing', Icon: Info },
  { id: 'review', label: 'Review', Icon: AlertCircle },
] as const

export default function ItineraryTabs({ active, onChange, issues }: {
  active: ItinerarySection; onChange: (section: ItinerarySection) => void; issues: number
}) {
  return (
    <nav aria-label="Itinerary sections" className="sticky top-0 z-10 flex flex-wrap border-b border-slate-200 bg-white px-2 sm:px-4 print:hidden">
      {sections.map(({ id, label, Icon }) => (
        <button key={id} type="button" aria-pressed={active === id} onClick={() => onChange(id)}
          className={`inline-flex min-h-11 items-center gap-1.5 border-b-2 px-2 py-3 text-xs font-semibold transition focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-indigo-600 sm:px-3 ${active === id ? 'border-indigo-600 text-indigo-700' : 'border-transparent text-slate-600 hover:bg-slate-50 hover:text-slate-900'}`}>
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />{label}
          {id === 'review' && issues > 0 && <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-950" aria-label={`${issues} issues need review`}>{issues}</span>}
        </button>
      ))}
    </nav>
  )
}
