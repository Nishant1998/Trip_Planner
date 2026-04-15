import { Link, useParams } from 'react-router-dom'
import { useEffect } from 'react'
import { ArrowLeft, ArrowRight, Clock, ExternalLink, MapPin, MessageCircle, Route } from 'lucide-react'
import { sampleDirections, sampleTrips } from '../data/sampleTrips'
import { formatBudget } from '../lib/currency'

const formatDate = (date: string) => new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${date}T12:00:00Z`))

export default function SampleTrip() {
  const { sampleId } = useParams()
  useEffect(() => { window.scrollTo(0, 0) }, [sampleId])
  const sample = sampleTrips.find(item => item.id === sampleId)
  if (!sample) return <div className="max-w-3xl mx-auto p-8"><h1 className="text-2xl font-bold">Sample not found</h1><Link className="inline-block mt-4 text-indigo-600 underline" to="/#sample-trips">Browse sample trips</Link></div>
  const { requirements } = sample
  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
      <Link to="/#sample-trips" className="inline-flex items-center gap-2 text-sm font-semibold text-slate-600 hover:text-indigo-600 mb-6"><ArrowLeft className="w-4 h-4" />All sample trips</Link>
      <header className="bg-white border border-slate-200 rounded-3xl overflow-hidden">
        <img src={sample.cover} alt={`${sample.destination} destination illustration`} width={960} height={480} fetchPriority="high" className="w-full h-40 sm:h-56 object-cover" />
        <div className="p-6 sm:p-8">
          <span className="inline-block px-3 py-1 bg-indigo-50 text-indigo-700 rounded-full text-xs font-bold mb-3">Read-only sample draft</span>
          <h1 className="text-2xl sm:text-3xl font-black text-slate-900">{sample.title}</h1>
          <p className="text-sm text-slate-500 mt-3">{formatDate(requirements.start_date)} – {formatDate(requirements.end_date)} · {requirements.adults} adults{requirements.children ? ` and ${requirements.children} child (age 8)` : ''} · {formatBudget({amount: requirements.budget, currency: requirements.currency})} requested total budget</p>
          <p className="text-slate-700 leading-relaxed mt-5">{sample.overview}</p>
          <p className="text-xs text-slate-500 mt-4">{sample.provenance} No live prices, hotel availability or guaranteed journey times are claimed.</p>
        </div>
      </header>
      <nav aria-label="Sample sections" className="flex flex-wrap gap-2 my-6">
        {[['itinerary','Itinerary'],['travel','Travel & stay'],['conversation','Chat excerpts'],['confirm','Things to confirm']].map(([id, label]) => <a key={id} href={`#${id}`} className="px-4 py-2 rounded-full bg-white border border-slate-200 text-sm font-semibold text-slate-600 hover:border-indigo-300 focus-visible:ring-2 focus-visible:ring-indigo-600">{label}</a>)}
      </nav>
      <section id="itinerary" className="scroll-mt-6" aria-labelledby="itinerary-title">
        <h2 id="itinerary-title" className="text-xl font-bold text-slate-900 mb-4">Your day-by-day draft</h2>
        <p className="text-sm text-slate-500 mb-5">Flexible periods keep these examples readable. Visit allowances are suggestions, not verified schedules.</p>
        <div className="space-y-6">{sample.days.map(day => (
          <article key={day.day} className="bg-white border border-slate-200 rounded-3xl p-5 sm:p-7">
            <div className="flex items-start gap-3 mb-6"><span className="bg-indigo-600 text-white font-bold w-10 h-10 flex items-center justify-center rounded-2xl shrink-0">{day.day}</span><div><p className="text-xs font-semibold text-slate-500">{formatDate(day.date)}</p><h3 className="font-bold text-lg text-slate-900 mt-1">{day.title}</h3></div></div>
            <ol className="space-y-5">{day.stops.map((stop, index) => (
              <li key={`${day.day}-${index}`} className="border-l-2 border-indigo-100 pl-4 sm:pl-5">
                <p className="text-xs font-bold uppercase tracking-wide text-indigo-600">{stop.period}</p>
                <div className="flex flex-wrap items-center gap-2 mt-1"><h4 className="font-bold text-slate-900">{stop.title}</h4>{stop.optional && <span className="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded">Optional</span>}</div>
                <p className="text-sm text-slate-600 leading-relaxed mt-2">{stop.description}</p>
                {stop.allowance && <p className="text-xs text-slate-500 inline-flex items-center gap-1.5 mt-2"><Clock className="w-3.5 h-3.5" />{stop.allowance} · planning allowance</p>}
              </li>
            ))}</ol>
          </article>
        ))}</div>
      </section>
      <section id="travel" className="bg-white border border-slate-200 rounded-3xl p-6 sm:p-8 mt-6 scroll-mt-6">
        <h2 className="text-xl font-bold text-slate-900 flex items-center gap-2"><Route className="w-5 h-5 text-indigo-600" />Travel & stay preferences</h2>
        <p className="text-sm text-slate-600 leading-relaxed mt-4">{sample.transport}</p>
        {sample.id === 'lonavala-family' && <div className="flex flex-wrap gap-3 mt-4">
          {[['Outbound directions','Kharghar, Navi Mumbai','Lonavala'],['Return directions','Lonavala','Kharghar, Navi Mumbai']].map(([label, origin, destination]) => <a key={label} href={sampleDirections(origin, destination)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 text-sm font-semibold text-indigo-600 border border-indigo-100 bg-indigo-50 px-4 py-2 rounded-xl"><MapPin className="w-4 h-4" />{label}<ExternalLink className="w-3.5 h-3.5" /></a>)}
        </div>}
        <h3 className="font-bold text-slate-900 mt-6">Accommodation to arrange</h3>
        <p className="text-sm text-slate-600 leading-relaxed mt-2">{sample.stay}</p>
      </section>
      <section id="conversation" className="bg-white border border-slate-200 rounded-3xl p-6 sm:p-8 mt-6 scroll-mt-6">
        <details><summary className="cursor-pointer font-bold text-slate-900 text-lg"><MessageCircle className="inline w-5 h-5 text-indigo-600 mr-2" />See how the trip brief was collected</summary>
          <p className="text-xs text-slate-500 mt-3 mb-5">Selected messages from the actual generated chat. The itinerary above was subsequently reviewed and simplified for this public example.</p>
          <div className="space-y-4">{sample.conversation.map((message, index) => <div key={index} className={`rounded-2xl p-4 text-sm leading-relaxed whitespace-pre-wrap break-words ${message.role === 'user' ? 'ml-4 sm:ml-12 bg-slate-900 text-white' : 'mr-4 sm:mr-12 bg-slate-50 text-slate-700 border border-slate-200'}`}><p className={`text-xs font-bold mb-2 ${message.role === 'user' ? 'text-slate-300' : 'text-indigo-600'}`}>{message.role === 'user' ? 'Traveller' : 'Riya'}</p>{message.content}</div>)}</div>
        </details>
      </section>
      <section id="confirm" className="bg-amber-50 border border-amber-200 rounded-3xl p-6 sm:p-8 mt-6 scroll-mt-6">
        <h2 className="text-lg font-bold text-amber-950">Before using this plan</h2>
        <ul className="list-disc pl-5 mt-3 space-y-2 text-sm text-amber-900 leading-relaxed">{sample.notes.map(note => <li key={note}>{note}</li>)}</ul>
        <div className="flex flex-wrap gap-x-5 gap-y-2 mt-5">{sample.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-sm text-amber-950 underline underline-offset-4">{source.label}<ExternalLink className="w-3 h-3" /></a>)}</div>
      </section>
      <div className="mt-8 flex flex-wrap items-center justify-between gap-4"><Link to="/#sample-trips" className="text-sm font-semibold text-slate-600">Browse another example</Link><Link to="/create" className="inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold px-5 py-3 rounded-xl">Plan your own trip<ArrowRight className="w-4 h-4" /></Link></div>
    </div>
  )
}
