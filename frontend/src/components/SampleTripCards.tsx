import { Link } from 'react-router-dom'
import { ArrowRight, CalendarDays, Users } from 'lucide-react'
import { sampleTrips } from '../data/sampleTrips'
import { formatBudget } from '../lib/currency'

export default function SampleTripCards() {
  return (
    <section id="sample-trips" className="px-4 sm:px-6 lg:px-8 pb-16 scroll-mt-20" aria-labelledby="sample-trips-title">
      <div className="max-w-7xl mx-auto">
        <div className="max-w-2xl mb-8">
          <p className="text-xs font-bold uppercase tracking-wider text-indigo-600 mb-2">Explore before you sign in</p>
          <h2 id="sample-trips-title" className="text-2xl sm:text-3xl font-black text-slate-900">See what a trip with Riya looks like</h2>
          <p className="text-sm sm:text-base text-slate-600 mt-3 leading-relaxed">Three generated and reviewed sample drafts. Browse the itinerary and chat excerpts instantly—no account or planning allowance needed.</p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {sampleTrips.map(sample => (
            <Link key={sample.id} to={`/examples/${sample.id}`} className="group flex flex-col bg-white rounded-3xl border border-slate-200 overflow-hidden shadow-xs hover:border-indigo-300 hover:shadow-md transition focus-visible:outline-2 focus-visible:outline-indigo-600 focus-visible:outline-offset-4">
              <div className="relative aspect-[2/1] shrink-0 bg-slate-100">
                <img src={sample.cover} alt={`${sample.destination} destination photo`} width={960} height={480} loading="lazy" decoding="async"
                  title={`Photo: ${sample.coverPhoto.author} · ${sample.coverPhoto.license}. Credits and license in the sample Review tab.`}
                  onError={event => {
                    const image = event.currentTarget
                    if (image.getAttribute('src') === '/covers/abstract-1.svg') return
                    image.src = '/covers/abstract-1.svg'
                    image.alt = `${sample.destination} trip cover unavailable`
                    image.title = 'Destination photo unavailable'
                  }}
                  className="absolute inset-0 w-full h-full max-w-full object-cover" />
                <span className="absolute top-4 left-4 bg-white/95 rounded-full px-3 py-1 text-xs font-bold text-slate-700">Sample draft</span>
              </div>
              <div className="p-5 sm:p-6 flex flex-col flex-1">
                <p className="text-xs font-bold uppercase tracking-wider text-indigo-600 mb-2">{sample.destination}</p>
                <h3 className="text-lg font-bold text-slate-900 leading-snug">{sample.title}</h3>
                <div className="flex flex-wrap gap-x-4 gap-y-2 mt-4 text-xs text-slate-600">
                  <span className="inline-flex items-center gap-1.5"><CalendarDays className="w-3.5 h-3.5" />{sample.requirements.days} days</span>
                  <span className="inline-flex items-center gap-1.5"><Users className="w-3.5 h-3.5" />{sample.requirements.adults} adults{sample.requirements.children ? ` + ${sample.requirements.children} child` : ''}</span>
                </div>
                <p className="text-sm font-semibold text-slate-900 mt-3">{formatBudget({ amount: sample.requirements.budget, currency: sample.requirements.currency })} <span className="text-xs font-normal text-slate-500">requested budget</span></p>
                <div className="flex flex-wrap gap-1.5 mt-4 mb-5">{sample.features.slice(0, 3).map(feature => <span key={feature} className="text-xs text-slate-600 bg-slate-50 border border-slate-100 rounded-lg px-2 py-1">{feature}</span>)}</div>
                <div className="mt-auto pt-4 border-t border-slate-100 flex items-center justify-between text-sm font-bold text-indigo-600">View sample itinerary<ArrowRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" /></div>
              </div>
            </Link>
          ))}
        </div>
      </div>
    </section>
  )
}
