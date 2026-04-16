import { useState } from 'react'
import { CheckCircle2, CheckSquare, ExternalLink, Info, MapPin, Navigation, Square } from 'lucide-react'
import type { NormalizedPlan } from '../lib/plan'
import PlaceImage from './PlaceImage'
import ItineraryOverview from './ItineraryOverview'
import ItineraryReview from './ItineraryReview'
import ItineraryTabs, { type ItinerarySection } from './ItineraryTabs'
import ItineraryTimeline from './ItineraryTimeline'

interface Props {
  plan: NormalizedPlan
  onRefresh?: () => void
  refreshing?: boolean
  refreshError?: string | null
}

export default function ItineraryCanvas({ plan, onRefresh, refreshing = false, refreshError = null }: Props) {
  const [activeTab, setActiveTab] = useState<ItinerarySection>('timeline')
  const [checkedItems, setCheckedItems] = useState<Record<number, boolean>>({})
  const togglePackingCheck = (index: number) => {
    setCheckedItems(previous => ({ ...previous, [index]: !previous[index] }))
  }
  return (
    <div className="flex-1 min-h-0 overflow-y-auto print:overflow-visible">
      <ItineraryOverview plan={plan} onReview={() => setActiveTab('review')}
        onRefresh={onRefresh} refreshing={refreshing} refreshError={refreshError} />
      <ItineraryTabs active={activeTab} onChange={setActiveTab}
        issues={plan.alerts.filter(alert => alert.severity === 'error').length} />

      {/* Tab Contents & Printable Document */}
      <div className="p-4 sm:p-6 print:p-0 space-y-6">
        <ItineraryTimeline plan={plan} active={activeTab === 'timeline'} />

        {/* SECTION 2: MAP & STOPS (Screen Only) */}
        {activeTab === 'map' && (
          <div className="space-y-6 print:hidden">
            <div className="bg-slate-900 rounded-3xl p-5 sm:p-6 text-white shadow-lg relative overflow-hidden">
              <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                <div>
                  <div className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 text-[11px] font-bold mb-2 border border-indigo-400/20">
                    <Navigation className="w-3 h-3" />
                    <span>Chronological Route Sequence</span>
                  </div>
                  <h3 className="text-base sm:text-lg font-black">Interactive Route & Waypoints</h3>
                  <p className="text-xs text-slate-300 mt-0.5">
                    Selected places in itinerary order. Confirm directions and travel times before leaving.
                  </p>
                </div>

                <a
                  href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(plan.destination)}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs shadow-md transition cursor-pointer shrink-0"
                >
                  <MapPin className="w-3.5 h-3.5" />
                  <span>Open in Google Maps</span>
                  <ExternalLink className="w-3 h-3" />
                </a>
              </div>
            </div>

            <div className="space-y-3">
              <h4 className="text-xs font-black uppercase tracking-wider text-slate-400">
                Chronological Waypoint Stops:
              </h4>
              {plan.days.flatMap((d) => d.activities).filter((act) => act.hasPlace !== false).map((act, idx) => (
                <div
                  key={idx}
                  className="flex items-center justify-between p-3 sm:p-3.5 rounded-2xl bg-slate-50 border border-slate-200/80 hover:bg-white hover:border-indigo-200 transition shadow-2xs gap-3"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="w-7 h-7 rounded-full bg-indigo-600 text-white font-black text-xs flex items-center justify-center shrink-0 shadow-xs">
                      {idx + 1}
                    </div>
                    <div className="min-w-0">
                      <h5 className="font-bold text-xs text-slate-900 truncate">{act.title}</h5>
                      <p className="text-[11px] text-slate-500 truncate">{act.description}</p>
                    </div>
                  </div>
                  <span className="text-[10px] font-bold text-slate-600 bg-white border border-slate-200 px-2.5 py-1 rounded-md shrink-0">
                    {act.time}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* SECTION 3: STAYS & DINING */}
        <div className={`${activeTab === 'stays' ? 'block' : 'hidden'} print:block space-y-6 print:mb-8 print:break-inside-avoid`}>
          <div className="flex items-center justify-between">
            <h3 className="font-bold text-slate-900 text-sm">Curated Accommodations</h3>
            <span className="text-xs text-slate-400 font-medium">Estimated stay suggestions</span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {plan.stays.length === 0 && <p className="text-sm text-slate-600">No hotel has been selected for this itinerary yet. Check the review notes before choosing a stay.</p>}
            {plan.stays.map((stay, idx) => (
              <div
                key={idx}
                className="bg-white rounded-3xl border border-slate-200 overflow-hidden shadow-xs hover:border-indigo-300 transition group print:border-slate-300 print:break-inside-avoid"
              >
                <div className="relative h-40 overflow-hidden">
                  <PlaceImage src={stay.image} name={stay.name} className="h-full w-full" />
                  <span className="absolute top-3 left-3 px-2.5 py-1 rounded-lg bg-slate-900/80 backdrop-blur-md text-white font-bold text-[10px]">
                    Stay Recommendation
                  </span>
                </div>
                <div className="p-4">
                  <div className="flex items-center justify-between text-xs font-bold mb-1">
                    <h4 className="text-slate-900 text-sm font-black">{stay.name}</h4>
                    {stay.rating ? <span className="text-amber-500">★ {stay.rating}</span> : <span className="text-slate-400">Confirm before booking</span>}
                  </div>
                  <p className="text-xs text-slate-500">{stay.type}</p>
                  <div className="mt-4 pt-3 border-t border-slate-100 flex items-center justify-between text-xs">
                    <span className="font-black text-emerald-700">{stay.pricePerNight}</span>
                    <span className="text-[11px] text-indigo-600 font-bold">Confirm before booking</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* SECTION 4: LOCAL TIPS & PACKING CHECKLIST */}
        <div className={`${activeTab === 'tips' ? 'block' : 'hidden'} print:block space-y-6 print:break-inside-avoid`}>
          <div className="bg-indigo-50/60 rounded-2xl p-5 border border-indigo-100 print:bg-white print:border-slate-300">
            <h3 className="font-bold text-indigo-900 print:text-slate-900 text-sm mb-3 flex items-center gap-2">
              <Info className="w-4 h-4 text-indigo-600" />
              <span>Practical travel tips</span>
            </h3>
            <ul className="space-y-2.5 text-xs text-indigo-900 print:text-slate-700">
              {plan.localTips.length === 0 && <li>No additional travel tips are available for this plan.</li>}
              {plan.localTips.map((tip, idx) => (
                <li key={idx} className="flex items-start gap-2.5">
                  <span className="text-indigo-600 font-bold shrink-0">•</span>
                  <span className="leading-relaxed">{tip}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="bg-emerald-50/60 rounded-2xl p-5 border border-emerald-100 print:bg-white print:border-slate-300">
            <h3 className="font-bold text-emerald-900 print:text-slate-900 text-sm mb-3 flex items-center gap-2">
              <CheckCircle2 className="w-4 h-4 text-emerald-600" />
              <span>🎒 Packing Checklist</span>
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {plan.packingList.length === 0 && <p className="text-xs text-slate-600">No packing suggestions are available for this plan.</p>}
              {plan.packingList.map((item, idx) => (
                <button
                  key={idx}
                  aria-pressed={Boolean(checkedItems[idx])}
                  onClick={() => togglePackingCheck(idx)}
                  className="w-full flex items-center gap-2.5 p-2 rounded-xl hover:bg-emerald-100/50 text-left transition cursor-pointer text-xs text-emerald-950 print:text-slate-800 font-medium"
                >
                  {checkedItems[idx] ? (
                    <CheckSquare className="w-4 h-4 text-emerald-600 shrink-0" />
                  ) : (
                    <Square className="w-4 h-4 text-emerald-400 shrink-0" />
                  )}
                  <span className={checkedItems[idx] ? 'line-through text-emerald-600' : ''}>
                    {item}
                  </span>
                </button>
              ))}
            </div>
          </div>

        </div>
        <div className={`${activeTab === 'review' ? 'block' : 'hidden'} print:block print:break-before-page`}>
          <ItineraryReview plan={plan} />
        </div>
      </div>
    </div>
  )
}
