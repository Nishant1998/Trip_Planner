import React, { useState } from 'react'
import { Car, ExternalLink, MapPin, Sun } from 'lucide-react'
import type { NormalizedPlan } from '../lib/plan'
import PlaceImage from './PlaceImage'

export default function ItineraryTimeline({ plan, active }: { plan: NormalizedPlan; active: boolean }) {
  const [selectedDayFilter, setSelectedDayFilter] = useState<number | 'all'>('all')
  return <>
    {/* SECTION 1: TIMELINE */}
    <div className={`${active ? 'block' : 'hidden'} print:block space-y-6 print:mb-8`}>
      <div className="flex items-center gap-1.5 pb-2 overflow-x-auto print:hidden">
        <button
          onClick={() => setSelectedDayFilter('all')}
          className={`px-3 py-1 rounded-lg text-xs font-bold transition cursor-pointer border shrink-0 ${
            selectedDayFilter === 'all'
              ? 'bg-indigo-600 text-white border-indigo-600 shadow-xs'
              : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
          }`}
        >
          All Days ({plan.days.length})
        </button>
        {plan.days.map((d) => (
          <button
            key={d.day}
            onClick={() => setSelectedDayFilter(d.day)}
            className={`px-3 py-1 rounded-lg text-xs font-bold transition cursor-pointer border shrink-0 ${
              selectedDayFilter === d.day
                ? 'bg-indigo-600 text-white border-indigo-600 shadow-xs'
                : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
            }`}
          >
            Day {d.day}
          </button>
        ))}
      </div>

      {plan.days.map((day) => (
        <div
          key={day.day}
          className={`${
            selectedDayFilter === 'all' || selectedDayFilter === day.day ? 'block' : 'hidden'
          } print:block bg-slate-50/70 rounded-2xl p-4 sm:p-5 border border-slate-200 print:bg-white print:border-slate-300 print:mb-6 print:break-inside-avoid`}
        >
          <div className="flex items-center justify-between mb-4 border-b border-slate-200 pb-3">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-black uppercase tracking-wider text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded-md border border-indigo-100 print:bg-slate-100 print:text-slate-900">
                  Day {day.day}
                </span>
                {day.weatherSummary && (
                  <span className="text-xs text-slate-500 font-medium flex items-center gap-1">
                    <Sun className="w-3.5 h-3.5 text-amber-500" /> {day.weatherSummary}
                  </span>
                )}
              </div>
              <h3 className="text-base sm:text-lg font-bold text-slate-900 mt-1">{day.title}</h3>
            </div>
            <span className="text-[11px] font-semibold text-slate-600 bg-white px-3 py-1 rounded-full border border-slate-200 shadow-2xs hidden sm:inline-block">
              {day.theme}
            </span>
          </div>
          {day.date && <p className="mb-2 text-xs font-medium text-slate-500">{day.date}</p>}
          {day.summary && <p className="mb-3 text-sm leading-relaxed text-slate-700">{day.summary}</p>}
          {day.workload && <p className="mb-4 text-xs leading-relaxed text-slate-600">{day.workload}</p>}
          <div className="space-y-3">
            {day.activities.map((act, idx) => (
              <React.Fragment key={idx}>
                {idx > 0 && act.driveTime && (
                  <div className="flex items-center gap-2 pl-3 py-1 text-slate-400 text-xs">
                    <div className="w-0.5 h-4 bg-slate-300 ml-1" />
                    <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-lg bg-slate-100 border border-slate-200 text-slate-700 text-[11px] font-semibold">
                      <Car className="w-3 h-3 text-slate-500" />
                      {act.driveTime}
                    </span>
                  </div>
                )}

                <div className="bg-white rounded-2xl p-3.5 border border-slate-200 shadow-xs hover:border-indigo-200 transition flex gap-3.5 items-start print:border-slate-300">
                  {act.hasPlace !== false && <PlaceImage src={act.imageUrl} name={act.title}
                    className="w-16 h-16 sm:w-20 sm:h-20" />}

                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center justify-between gap-2 mb-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 font-mono font-bold text-xs">
                          {act.time}
                        </span>
                        <h4 className="break-words font-bold text-xs leading-relaxed sm:text-sm text-slate-900">
                          {act.title}
                        </h4>
                      </div>
                      {act.cost && <span className="text-[11px] font-semibold text-slate-700 bg-slate-100 px-2 py-0.5 rounded-md border border-slate-200 shrink-0">
                        {act.cost}
                      </span>}
                    </div>

                    <p className="text-xs text-slate-600 leading-relaxed mb-2">
                      {act.description}
                    </p>
                    {act.imageUrl && Boolean(act.photoCredits?.length) && <p className="mb-2 text-[10px] leading-relaxed text-slate-500">
                      Photo: {act.photoCredits?.join(', ')} · Google Maps
                    </p>}

                    <div className="flex items-center justify-between pt-1 border-t border-slate-50 text-xs print:hidden">
                      <span className="text-[11px] text-slate-400 font-medium">
                        Duration: {act.duration}
                      </span>
                      {act.hasPlace !== false && <a
                        href={
                          act.mapUrl ||
                          `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
                            act.title + ' ' + plan.destination
                          )}`
                        }
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-[11px] font-bold text-indigo-600 hover:text-indigo-800 hover:underline cursor-pointer"
                      >
                        <MapPin className="w-3 h-3" />
                        <span>Directions</span>
                        <ExternalLink className="w-2.5 h-2.5" />
                      </a>}
                    </div>
                  </div>
                </div>
              </React.Fragment>
            ))}
          </div>
        </div>
      ))}
    </div>

    {plan.budgetTargets && active && (
      <section className="rounded-2xl border border-slate-200 bg-white p-4 sm:p-5 print:break-inside-avoid">
        <h3 className="text-base font-bold text-slate-900">Full-trip spending targets</h3>
        <p className="mt-2 text-xs leading-relaxed text-slate-600">{plan.budgetTargets.basis}</p>
        <dl className="mt-3 divide-y divide-slate-100 text-sm">
          {Object.entries(plan.budgetTargets.categories).map(([category, amount]) => (
            <div key={category} className="flex justify-between gap-4 py-2">
              <dt className="capitalize text-slate-600">{category.replace(/_/g, ' ')}</dt>
              <dd className="font-semibold text-slate-900">{amount}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3 text-xs font-medium text-amber-800">Targets cover all travelers and {plan.budgetTargets.nights} nights. They are not prices or proof that the trip fits the budget.</p>
      </section>
    )}


  </>
}
