import { Link, useParams } from 'react-router-dom'
import { useEffect } from 'react'
import { ArrowLeft, ArrowRight, MessageCircle } from 'lucide-react'
import { sampleTrips } from '../data/sampleTrips'
import { sampleToPlan } from '../lib/sample-plan'
import ItineraryCanvas from '../components/ItineraryCanvas'

export default function SampleTrip() {
  const { sampleId } = useParams()
  useEffect(() => { window.scrollTo(0, 0) }, [sampleId])
  const sample = sampleTrips.find(item => item.id === sampleId)
  if (!sample) return <div className="max-w-3xl mx-auto p-8"><h1 className="text-2xl font-bold">Sample not found</h1><Link className="inline-block mt-4 text-indigo-600 underline" to="/#sample-trips">Browse sample trips</Link></div>
  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
      <Link to="/#sample-trips" className="inline-flex items-center gap-2 text-sm font-semibold text-slate-600 hover:text-indigo-600 mb-5"><ArrowLeft className="w-4 h-4" />All sample trips</Link>
      <div className="mb-5">
        <span className="inline-block px-3 py-1 bg-indigo-50 text-indigo-700 rounded-full text-xs font-bold mb-3">Read-only sample draft</span>
        <h1 className="text-xl sm:text-2xl font-bold text-slate-900">{sample.title}</h1>
        <p className="text-xs leading-relaxed text-slate-500 mt-2">{sample.provenance} Explore the same itinerary tabs used for your own trips. No sign-in or demo allowance is needed.</p>
      </div>
      <section aria-label="Sample itinerary" className="rounded-3xl border border-slate-200 bg-white overflow-hidden shadow-sm">
        <ItineraryCanvas key={sample.id} plan={sampleToPlan(sample)} />
      </section>
      <section id="conversation" className="bg-white border border-slate-200 rounded-3xl p-6 sm:p-8 mt-6 scroll-mt-6">
        <details><summary className="cursor-pointer font-bold text-slate-900 text-lg"><MessageCircle className="inline w-5 h-5 text-indigo-600 mr-2" />See how the trip brief was collected</summary>
          <p className="text-xs text-slate-500 mt-3 mb-5">Selected messages from the actual generated chat. The itinerary above was subsequently reviewed and simplified for this public example.</p>
          <div className="space-y-4">{sample.conversation.map((message, index) => <div key={index} className={`rounded-2xl p-4 text-sm leading-relaxed whitespace-pre-wrap break-words ${message.role === 'user' ? 'ml-4 sm:ml-12 bg-slate-900 text-white' : 'mr-4 sm:mr-12 bg-slate-50 text-slate-700 border border-slate-200'}`}><p className={`text-xs font-bold mb-2 ${message.role === 'user' ? 'text-slate-300' : 'text-indigo-600'}`}>{message.role === 'user' ? 'Traveller' : 'Riya'}</p>{message.content}</div>)}</div>
        </details>
      </section>

      <div className="mt-8 flex flex-wrap items-center justify-between gap-4"><Link to="/#sample-trips" className="text-sm font-semibold text-slate-600">Browse another example</Link><Link to="/create" className="inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold px-5 py-3 rounded-xl">Plan your own trip<ArrowRight className="w-4 h-4" /></Link></div>
    </div>
  )
}
