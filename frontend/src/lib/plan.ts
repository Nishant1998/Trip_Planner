export interface Activity {
  time: string
  title: string
  description: string
  category?: string
  duration?: string
  cost?: string
  driveTime?: string
  mapUrl?: string
  imageUrl?: string
  hasPlace?: boolean
  photoCredits?: string[]
}

export interface DayPlan {
  day: number
  date?: string
  title: string
  theme: string
  summary?: string
  workload?: string
  weatherSummary?: string
  activities: Activity[]
  stayRecommendation?: {
    name: string
    type: string
    pricePerNight: string
    rating?: number
    image: string
  }
}

export interface NormalizedPlan {
  travelNotes?: string[]
  stayNotes?: string[]
  destination: string
  deliveryStatus?: string
  displayRefreshRequired?: boolean
  placeIds?: string[]
  unresolvedPlaces?: number
  budgetTargets?: { categories: Record<string, string>; basis: string; nights: number }
  durationDays: number
  travelers: { adults: number; children: number }
  budgetTotal: string
  currency: string
  summary: string
  days: DayPlan[]
  stays: Array<{
    name: string
    type: string
    pricePerNight: string
    rating?: number
    image: string
  }>
  localTips: string[]
  packingList: string[]
  alerts: Array<{ severity: string; message: string }>
  budgetHealth: { withinBudget?: boolean; categories: Record<string, string> }
  planningNotes: string[]
  sources: Array<{ title: string; url: string }>
}


const PLACEHOLDER_COVERS = ['/covers/abstract-1.svg', '/covers/abstract-2.svg', '/covers/abstract-3.svg', '/covers/abstract-4.svg']

export function placeholderCover(seed: string): string {
  let hash = 0
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash << 5) - hash + seed.charCodeAt(index)
    hash |= 0
  }
  return PLACEHOLDER_COVERS[Math.abs(hash) % PLACEHOLDER_COVERS.length]
}


// Parse backend plan JSON into standardized presentation format
export const parseBackendPlan = (raw: any): NormalizedPlan | null => {
  if (!raw) return null
  try {
    const response = typeof raw === 'string' ? JSON.parse(raw) : raw
    const payload = response.plan?.plan_json ?? response.plan ?? response
    const p = typeof payload === 'string' ? JSON.parse(payload) : payload
    if (p.schema_version === 'trip_plan.v2') {
      const summary = p.trip_summary
      const unresolved = (event: any) => Boolean(event.place_id) && (!event.title || event.title === 'Selected place (refresh details)')
      const unresolvedPlaces = p.days.reduce((count: number, day: any) => count + day.events.filter(unresolved).length, 0)
      const currency = summary.currency || p.total_estimated_cost?.currency || ''
      const money = (value: unknown, unit = currency): string =>
        typeof value === 'number' && Number.isFinite(value)
          ? `${unit} ${value.toLocaleString()}`.trim()
          : 'Cost to confirm'
      const days: DayPlan[] = p.days.map((day: any) => ({
        day: day.day,
        date: day.date,
        title: day.title || `Day ${day.day}${day.base_location ? ` — ${day.base_location}` : ''}`,
        theme: day.base_location || 'Location to confirm',
        summary: day.summary || undefined,
        workload: day.workload ? `${day.workload.visit_minutes ?? '?'} min of visits · ${day.workload.poi_capacity_minutes ?? '?'} min visit allowance · ${day.workload.status === 'within_allowance' ? 'Within planning allowance, not verified travel time' : (day.workload.status || 'unknown').replace(/_/g, ' ')}` : undefined,
        weatherSummary: day.weather?.summary,
        activities: day.events.filter((event: any) => !unresolved(event)).map((event: any) => {
          const clock = typeof event.start === 'string' ? event.start.match(/T(\d{2}:\d{2})/)?.[1] : undefined
          return {
            time: clock ? `${clock}${event.timing_status === 'provisional' ? ' (provisional)' : ''}`
              : event.period ? `${event.period.charAt(0).toUpperCase()}${event.period.slice(1)} · flexible` : 'Time to confirm',
            title: event.title,
            description: [event.reason, event.address].filter(Boolean).join(' · '),
            category: event.kind,
            duration: Number.isFinite(event.visit_minutes)
              ? `${event.visit_minutes} min${event.duration_basis === 'planning_allowance' ? ' allowance' : ''}`
              : 'Duration to confirm',
            cost: ['activity', 'meal', 'travel'].includes(event.kind) ? money(event.estimated_cost, event.currency || currency) : undefined,
            hasPlace: Boolean(event.place_id),
            driveTime: Number.isFinite(event.travel_from_previous?.duration_s)
              ? `${Math.ceil(event.travel_from_previous.duration_s / 60)} min travel` : undefined,
            mapUrl: event.map_url || undefined,
            imageUrl: event.display_photos?.[0]?.uri || event.display_photos?.[0]?.url || undefined,
            photoCredits: (event.display_photos?.[0]?.attribution || []).map((author: any) => author.displayName).filter(Boolean),
          }
        }),
      }))
      const costs = p.total_estimated_cost || {}
      return {
        destination: summary.requested_destinations.join(' → ') || summary.title,
        deliveryStatus: p.delivery_status || (p.status === 'partial' ? 'DRAFT_NEEDS_CONFIRMATION' : undefined),
        displayRefreshRequired: response.display_fields_status === 'refresh_required' || unresolvedPlaces > 0,
        unresolvedPlaces,
        placeIds: [...new Set<string>(p.days.flatMap((day: any) => day.events.map((event: any) => event.place_id).filter(Boolean)))],
        budgetTargets: p.budget_guidance ? {
          categories: Object.fromEntries(Object.entries(p.budget_guidance.category_limits).map(([key, value]) => [key, money(value, p.budget_guidance.currency)])),
          basis: p.budget_guidance.basis,
          nights: p.budget_guidance.nights,
        } : undefined,
        durationDays: days.length,
        travelers: { adults: summary.adults, children: summary.children },
        budgetTotal: summary.budget != null ? Number(summary.budget).toLocaleString() : 'Budget unavailable',
        currency,
        summary: summary.overview || summary.title,
        days,
        stays: (p.accommodation || []).filter((record: any) => record.selected?.name).map((record: any) => ({
          name: record.selected?.name || p.destinations.find((destination: any) => destination.id === record.stay_request.destination_id)?.label || 'Accommodation to confirm',
          type: record.reason || 'Accommodation to confirm',
          pricePerNight: record.selected?.price_basis === 'per_night'
            ? money(record.selected.price, record.selected.currency || currency) : 'Nightly price to confirm',
          image: record.selected?.image_url || '',
        })),
        localTips: (p.local_tips || []).filter((tip: unknown): tip is string => typeof tip === 'string' && tip.trim().length > 0),
        packingList: (p.packing_list || []).filter((item: unknown): item is string => typeof item === 'string' && item.trim().length > 0),
        alerts: Array.from(new globalThis.Map<string, { severity: string; message: string }>(
          [...(p.warnings || []), ...(response.warnings || []).map((message: string) => ({ message, severity: 'warning' }))]
            .map((warning: any) => [warning.message, { severity: warning.severity || 'warning', message: warning.message }]),
        ).values()).sort((a, b) => Number(b.severity === 'error') - Number(a.severity === 'error')),
        budgetHealth: {
          withinBudget: costs.budget_status === 'within' ? true : costs.budget_status === 'over' ? false : undefined,
          categories: Object.fromEntries(Object.entries(costs.category_totals || {}).map(([key, value]) => [key, money(value, costs.currency || currency)])),
        },
        planningNotes: [
          ...(summary.assumptions || []),
          ...(costs.unknown_components?.length ? [`Costs to confirm: ${costs.unknown_components.join(', ')}`] : []),
        ],
        sources: [],
      }
    }
    // The final autonomous pipeline already supplies a display-ready contract.
    // Keep this direct path factual: unknown data is shown as unavailable,
    // never replaced with made-up ratings, times, or visit durations.
    if (p.trip_summary && Array.isArray(p.days) && p.mandatory_summary) {
      const directDays: DayPlan[] = p.days.map((day: any) => ({
        day: day.day_number,
        date: day.date,
        title: day.title || `Day ${day.day_number}`,
        theme: [day.theme || 'Planned activities', day.start_anchor && day.end_anchor
          ? `${day.start_anchor} → ${day.end_anchor}` : null].filter(Boolean).join(' · '),
        activities: [
          ...(day.transport_legs || []).map((leg: any) => ({
            time: !leg.uncertainty && leg.start_time && leg.start_time !== 'unknown' ? leg.start_time : 'Time to confirm',
            title: `${leg.origin_name || 'Start'} → ${leg.destination_name || 'Destination'}`,
            description: `${leg.selected_mode || 'Transport to confirm'} · ${leg.uncertainty
              ? 'Route and arrival time need confirmation' : leg.provenance || 'Researched route'}`,
            category: 'Transport',
            duration: Number.isFinite(leg.duration_minutes) ? `${leg.duration_minutes} min` : 'Duration to confirm',
            cost: 'Cost unavailable',
            driveTime: undefined,
          })),
          ...(day.activities || []).map((activity: any) => ({
          time: activity.status === 'requires_confirmation' || activity.start_time === 'unknown'
            ? 'Time to confirm' : activity.start_time || 'Time to confirm',
          title: activity.name,
          description: [
            ...(activity.notes || []),
            activity.address,
            ...(activity.restrictions || []),
            ...(activity.accessibility_notes || []),
          ].filter(Boolean).join(' · '),
          category: (activity.categories || []).join(', ') || undefined,
          duration: Number.isFinite(activity.visit_duration_minutes)
            ? `${activity.visit_duration_minutes} min${activity.status === 'requires_confirmation' ? ' provisional allowance' : ''}`
            : 'Duration unavailable',
          cost: 'Cost unavailable',
          driveTime: undefined,
        })),
          ...(day.timeline || [])
            .filter((item: any) => ['MEAL', 'REST', 'WAIT', 'ACCESS', 'BUFFER'].includes(item.type))
            .map((item: any) => ({
              time: item.start_time && item.start_time !== 'unknown' ? item.start_time : 'Time to confirm',
              title: ({ MEAL: 'Meal break', REST: 'Rest break', WAIT: 'Waiting time', ACCESS: 'Entry / access', BUFFER: 'Travel buffer' } as Record<string, string>)[item.type],
              description: (item.notes || []).join(' · '),
              category: 'Schedule',
              duration: Number.isFinite(item.duration_minutes) ? `${item.duration_minutes} min` : 'Duration to confirm',
              cost: 'Cost unavailable',
              driveTime: undefined,
            })),
        ].sort((a, b) => {
          const time = (value: string) => /^\d{2}:\d{2}$/.test(value) ? value : '99:99'
          return time(a.time).localeCompare(time(b.time))
        }),
      }))
      const warnings = [
        ...(p.warnings || []),
        ...(p.unresolved_items || []).map((item: any) => item.message).filter(Boolean),
        ...p.days.flatMap((day: any) => [...(day.warnings || []), ...(day.unresolved_items || [])]
          .map((message: string) => `Day ${day.day_number}: ${message}`)),
      ]
      return {
        destination: p.trip_summary.title || 'Trip itinerary',
        deliveryStatus: p.delivery_status || p.status,
        durationDays: p.trip_summary.duration_days || directDays.length,
        travelers: {
          adults: Number(p.trip_summary.travellers?.num_adults || 0),
          children: Number(p.trip_summary.travellers?.num_children || 0),
        },
        budgetTotal: p.budget_summary?.trip_budget?.total != null
          ? String(p.budget_summary.trip_budget.total)
          : 'Budget unavailable',
        currency: p.trip_summary.currency || 'INR',
        summary: p.trip_summary.title || 'Your researched itinerary is ready.',
        days: directDays,
        stays: (p.bases || []).map((base: any) => ({
          name: base.name,
          type: 'Base location',
          pricePerNight: 'Accommodation to confirm',
          image: placeholderCover(base.name || base.id || 'base'),
        })),
        localTips: [],
        packingList: [],
        alerts: [...new Set<string>(warnings)].map((message: string) => ({ severity: 'warning', message })),
        budgetHealth: { categories: {} },
        planningNotes: (p.mandatory_summary.items || [])
          .filter((item: any) => item.status !== 'SATISFIED')
          .map((item: any) => item.reason || `${item.label}: ${item.status}`),
        sources: [],
      }
    }
    const deck = p.presentation || p
    const daysRaw = deck.days || p.days || []
    const staysRaw = deck.stays || p.stays || []

    const normalizedDays: DayPlan[] = daysRaw.map((d: any, idx: number) => {
      const items = d.items || d.timeline || []
      const hasDestinationActivity = items.some((item: any) => item.kind === 'activity')
      const hasTravel = items.some((item: any) => item.kind === 'transport')
      const acts: Activity[] = items.map((item: any) => {
        let drive = ''
        if (item.travel_from_previous?.duration_minutes) {
          drive = `🚗 ${item.travel_from_previous.duration_minutes} mins drive`
        } else if (item.drive_time) {
          drive = item.drive_time
        }

        return {
          time: item.time || item.start_time || 'Time to confirm',
          title: item.title || item.name || 'Activity',
          description: item.description || '',
          category: item.kind || 'activity',
          duration: item.duration || 'Duration to confirm',
          cost: typeof item.estimated_cost === 'number'
            ? `${p.currency || 'INR'} ${item.estimated_cost.toLocaleString()}`
            : item.estimated_cost?.maximum != null
              ? `${p.currency || 'INR'} ${Number(item.estimated_cost.maximum).toLocaleString()}`
              : typeof item.estimated_cost === 'string' ? item.estimated_cost : item.cost || 'Cost to confirm',
          driveTime: drive,
          mapUrl: item.place?.google_maps_url || item.map_url,
          imageUrl: item.image_url || item.image || item.place?.image_url,
        }
      })

      return {
        day: d.day || d.day_number || idx + 1,
        date: d.date || d.date_label,
        title: d.title || (hasDestinationActivity
          ? `Day ${d.day || idx + 1}`
          : hasTravel
            ? `Day ${d.day || idx + 1} — Travel day`
            : `Day ${d.day || idx + 1} — Free time`),
        theme: hasDestinationActivity ? (d.theme || 'Exploration & Discovery') : hasTravel ? 'Arrival, departure or transfer' : 'Flexible time',
        weatherSummary: d.weather?.summary,
        activities: acts,
        stayRecommendation: d.stayRecommendation || {
          name: `Choose accommodation near ${d.base_stop || 'your planned area'}`,
          type: 'Accommodation to confirm',
          pricePerNight: 'Nightly price to confirm',
          image: placeholderCover(d.base_stop || d.title || `day-${idx + 1}`),
        },
      }
    })

    const normalizedStays = staysRaw.map((s: any) => ({
      name: s.name || s.stop || 'Accommodation to confirm',
      type: s.description || s.type || 'Accommodation',
      pricePerNight: s.pricePerNight || 'Nightly price to confirm',
      rating: s.rating ?? undefined,
      image: s.image || s.image_url || placeholderCover(s.name || s.stop || 'stay'),
    }))
    const rawAlerts = [...(deck.alerts || []), ...(p.warnings || []), ...(p.blocking_issues || [])]
    const alerts = rawAlerts
      .map((alert: any) => typeof alert === 'string'
        ? { severity: 'warning', message: alert }
        : { severity: alert.severity || 'warning', message: alert.message || '' })
      .filter((alert: { message: string }) => Boolean(alert.message))
    const planningNotes = [
      ...(p.intelligence_briefing?.items || []).map((item: any) => item.message).filter(Boolean),
      ...(p.assumptions || []),
      ...(p.relaxations || []).map((item: any) => item.message || item.reason || item.description).filter(Boolean),
    ]
    const sources = (p.sources || [])
      .filter((source: any) => source?.url)
      .map((source: any) => ({ title: source.title || source.provider || 'Trip source', url: source.url }))

    const resolvedDestination = (p.resolved_stops || [])
      .map((stop: any) => typeof stop === 'string' ? stop : stop?.name || stop?.city)
      .filter(Boolean)
      .join(' → ')

    return {
      destination: p.destination || resolvedDestination || p.title || 'Destination pending',
      deliveryStatus: p.delivery_status,
      durationDays: normalizedDays.length || p.durationDays || 0,
      travelers: p.travelers || { adults: 0, children: 0 },
      budgetTotal: p.budget?.summary || p.budgetTotal || 'Budget pending',
      currency: p.currency || 'INR',
      summary: p.summary || 'Your itinerary is being prepared from your confirmed preferences.',
      days: normalizedDays,
      stays: normalizedStays,
      localTips: p.localTips || p.local_tips || [],
      packingList: p.packingList || p.checklist || p.packing_list || [],
      alerts,
      budgetHealth: {
        withinBudget: deck.budget?.within_budget ?? p.budget?.within_budget,
        categories: Object.fromEntries(Object.entries(deck.budget?.categories || p.budget?.category_totals || {}).map(([name, value]: [string, any]) => [
          name,
          typeof value === 'string' ? value : value?.summary || value?.maximum || value?.minimum || 'Estimate available',
        ])),
      },
      planningNotes,
      sources,
    }
  } catch (err) {
    console.error('[CreateTrip] parseBackendPlan failed', err)
    return null
  }
}
