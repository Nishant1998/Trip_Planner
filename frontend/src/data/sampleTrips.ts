import data from './sampleTrips.json'

export interface SampleTrip {
  id: string
  title: string
  destination: string
  cover: string
  overview: string
  transport: string
  stay: string
  status: string
  provenance: string
  features: string[]
  notes: string[]
  sources: { label: string; url: string }[]
  requirements: { days: number; start_date: string; end_date: string; currency: string; budget: number; adults: number; children: number }
  days: { day: number; date: string; title: string; stops: {
    period: string; title: string; description: string; allowance?: string; optional?: boolean
    mapQuery?: string
    journey?: { origin: string; destination: string }
  }[] }[]
  conversation: { role: string; content: string }[]
}

export const sampleTrips: SampleTrip[] = data

export function sampleDirections(origin: string, destination: string): string {
  return `https://www.google.com/maps/dir/?${new URLSearchParams({ api: '1', origin, destination, travelmode: 'driving' })}`
}
