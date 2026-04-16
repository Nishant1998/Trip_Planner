import { sampleDirections, type SampleTrip } from '../data/sampleTrips'
import type { NormalizedPlan } from './plan'

// Public examples are reviewed drafts, not provider-verified booking records.
export function sampleToPlan(sample: SampleTrip): NormalizedPlan {
  const { requirements } = sample
  return {
    destination: sample.destination,
    deliveryStatus: 'sample_draft',
    displayRefreshRequired: false,
    placeIds: [],
    durationDays: requirements.days,
    travelers: { adults: requirements.adults, children: requirements.children },
    budgetTotal: requirements.budget.toLocaleString('en-IN'),
    currency: requirements.currency,
    summary: sample.overview,
    days: sample.days.map(day => ({
      day: day.day,
      date: day.date,
      title: day.title,
      theme: 'Relaxed pace',
      activities: day.stops.map(stop => ({
        time: stop.period,
        title: stop.title,
        description: stop.optional ? `Optional · ${stop.description}` : stop.description,
        duration: stop.allowance ? `${stop.allowance} · planning allowance` : 'Flexible',
        hasPlace: Boolean(stop.mapQuery || stop.journey),
        mapUrl: stop.journey ? sampleDirections(stop.journey.origin, stop.journey.destination)
          : stop.mapQuery ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(stop.mapQuery)}` : undefined,
      })),
    })),
    stays: [],
    travelNotes: [sample.transport],
    stayNotes: [sample.stay],
    localTips: [sample.transport, 'Flexible periods and visit allowances are suggestions, not verified schedules.'],
    packingList: [],
    alerts: sample.notes.map(message => ({ severity: 'warning', message })),
    budgetHealth: { categories: {} },
    planningNotes: [sample.provenance, 'No live prices, hotel availability or guaranteed journey times are claimed.', sample.stay],
    sources: sample.sources.map(source => ({ title: source.label, url: source.url })),
  }
}
