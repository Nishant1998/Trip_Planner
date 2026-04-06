import React, { useState, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Calendar,
  Check,
  MapPin,
  ArrowRight,
  Search,
  Compass,
  Users,
  User,
  Wallet,
  Lock,
  Trash2,
  ChevronDown,
  ChevronUp,
  SlidersHorizontal,
  Layers,
  Sparkles,
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { apiFetch } from '../lib/api'
import { formatBudget } from '../lib/currency'

interface BackendTripCard {
  id: string
  slug: string
  name: string
  destination?: string | null
  description?: string | null
  cover_image_url?: string | null
  cover_source_url?: string | null
  cover_credit?: string | null
  dates?: string | null
  start_date?: string | null
  end_date?: string | null
  duration_days?: number | null
  status: string
  factors?: string[]
  updated_ago?: string
  created_at?: string
  travelers?: { adults?: number; children?: number; seniors?: number } | null
  budget?: { currency?: string; amount?: number | null } | null
}

const ABSTRACT_COVERS = [
  '/covers/abstract-1.svg',
  '/covers/abstract-2.svg',
  '/covers/abstract-3.svg',
  '/covers/abstract-4.svg',
]

function getFallbackCover(key: string): string {
  let hash = 0
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash << 5) - hash + key.charCodeAt(index)
    hash |= 0
  }
  return ABSTRACT_COVERS[Math.abs(hash) % ABSTRACT_COVERS.length]
}

// â”€â”€ Status badge â€” rich JSX matching reference â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function StatusBadge({ status }: { status: string }) {
  const s = (status || '').toLowerCase()
  if (s === 'confirmed' || s === 'completed' || s === 'ready') {
    return (
      <span className="inline-flex items-center gap-1 px-3 py-1 text-xs font-bold rounded-full bg-emerald-500/90 text-white backdrop-blur shadow-sm border border-emerald-400/40">
        <Check className="h-3.5 w-3.5" aria-hidden="true" /><span>Ready</span>
      </span>
    )
  }
  if (s === 'planning' || s === 'checkpoint_review' || s === 'draft_review') {
    return (
      <span className="inline-flex items-center gap-1.5 px-3 py-1 text-xs font-bold rounded-full bg-amber-500/95 text-white backdrop-blur shadow-sm border border-amber-300/50">
        <span className="w-1.5 h-1.5 rounded-full bg-white animate-ping" />
        <span>Planning</span>
      </span>
    )
  }
  if (s === 'customizing' || s === 'collecting' || s === 'awaiting_confirmation') {
    return (
      <span className="inline-flex items-center gap-1.5 px-3 py-1 text-xs font-bold rounded-full bg-blue-600/90 text-white backdrop-blur shadow-sm border border-blue-400/40 animate-pulse">
        <span className="w-1.5 h-1.5 rounded-full bg-white animate-ping" />
        <span>Customizing</span>
      </span>
    )
  }
  if (s === 'archived' || s === 'cancelled') {
    return (
      <span className="inline-flex items-center gap-1 px-3 py-1 text-xs font-medium rounded-full bg-slate-700/80 text-slate-300 backdrop-blur shadow-sm border border-slate-600/50">
        <span>Archived</span>
      </span>
    )
  }
  if (s === 'failed' || s === 'attention' || s === 'error') {
    return (
      <span className="inline-flex items-center gap-1 px-3 py-1 text-xs font-bold rounded-full bg-rose-600/90 text-white backdrop-blur shadow-sm border border-rose-400/40">
        <span>Attention</span>
      </span>
    )
  }
  // draft / default
  return (
    <span className="inline-flex items-center gap-1 px-3 py-1 text-xs font-medium rounded-full bg-slate-900/80 text-slate-200 backdrop-blur shadow-sm border border-slate-700/50">
      <span>Draft</span>
    </span>
  )
}

// â”€â”€ Travelers badge â€” 3 variants matching reference â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function TravelersBadge({ travelers }: { travelers: BackendTripCard['travelers'] }) {
  const adults = travelers?.adults ?? 0
  const children = travelers?.children ?? 0
  const total = adults + children
  if (total === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-slate-600 bg-slate-100 px-2 py-0.5 rounded-md">
        <User className="w-3 h-3 text-slate-400" /> Travelers TBD
      </span>
    )
  }
  if (adults === 1 && children === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold text-violet-700 bg-violet-50 px-2.5 py-1 rounded-lg border border-violet-200/60">
        <User className="w-3.5 h-3.5 text-violet-500" /> Solo Traveler
      </span>
    )
  }
  if (adults > 1 && children === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold text-blue-700 bg-blue-50 px-2.5 py-1 rounded-lg border border-blue-200/60">
        <Users className="w-3.5 h-3.5 text-blue-500" /> {adults} Adults
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-bold text-amber-900 bg-amber-50 px-2.5 py-1 rounded-lg border border-amber-200/70">
      <Users className="w-3.5 h-3.5 text-amber-600" />
      {adults} Adults {'\u00b7'} {children} {children === 1 ? 'Child' : 'Kids'}
    </span>
  )
}

// â”€â”€ Budget formatter â€” full currency support â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

// â”€â”€ Creation date fallback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function formatCreationDate(iso?: string): string {
  if (!iso) return 'Draft'
  try {
    return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  } catch { return 'Draft' }
}

// â”€â”€ Trip Card â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
const TripCard: React.FC<{
  trip: BackendTripCard
  onDeleteRequest: (trip: BackendTripCard) => void
  onTagClick: (tag: string) => void
}> = ({ trip, onDeleteRequest, onTagClick }) => {
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState(false)

  const isAttention = ['failed', 'attention', 'error'].includes((trip.status || '').toLowerCase())
  const factors = Array.isArray(trip.factors) ? trip.factors : []
  const hasNoDesc = !trip.description || trip.description.trim().length === 0
  const isLongDesc = !!trip.description && trip.description.length > 200

  // Robust title fallback: name â†’ "Trip to {destination}" â†’ "Untitled Trip Â· date"
  const tripTitle = trip.name?.trim()
    ? trip.name.trim()
    : trip.destination?.trim()
    ? `Trip to ${trip.destination.trim()}`
    : `Untitled Trip \u00b7 ${formatCreationDate(trip.created_at)}`

  const destinationDisplay = trip.destination?.trim()
    || trip.name?.replace(/^Trip to /i, '').trim()
    || 'Where to next?'

  const datesDisplay = trip.dates || (trip.start_date ? trip.start_date.slice(0, 10) : null)
  const fallbackCover = getFallbackCover(trip.slug || trip.id || trip.destination || tripTitle)

  return (
    <div className="group bg-white rounded-3xl border border-slate-200/90 overflow-hidden shadow-xs hover:shadow-xl transition-all duration-300 flex flex-col justify-between hover:border-indigo-200">

      {/* â”€â”€ Cover image â”€â”€ */}
      <div
        className="relative h-52 w-full overflow-hidden bg-slate-900 cursor-pointer"
        onClick={() => navigate(`/trips/${trip.slug}`)}
      >
        <img
          src={trip.cover_image_url || fallbackCover}
          alt={tripTitle}
          onError={(event) => {
            event.currentTarget.src = fallbackCover
          }}
          className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
        />
        <div className="absolute inset-0 bg-gradient-to-t from-slate-950/80 via-slate-950/20 to-black/30" />
        {trip.cover_source_url && <a href={trip.cover_source_url} target="_blank" rel="noreferrer"
          onClick={event => event.stopPropagation()}
          className="absolute bottom-1 right-2 z-10 max-w-[70%] truncate text-[9px] text-white underline">{trip.cover_credit ? `Photo: ${trip.cover_credit} · ` : ''}Google Maps</a>}

        {/* Top badges */}
        <div className="absolute top-3.5 left-3.5 right-3.5 flex items-center justify-between">
          <span className="px-2.5 py-1 text-[11px] font-bold rounded-full bg-black/60 text-white backdrop-blur border border-white/20 flex items-center gap-1">
            <Calendar className="w-3 h-3 text-indigo-300" />
            {trip.duration_days && trip.duration_days > 0 ? `${trip.duration_days} Days` : datesDisplay || 'Flexible Dates'}
          </span>
          <StatusBadge status={trip.status} />
        </div>

        {/* Bottom: destination + title */}
        <div className="absolute bottom-3.5 left-4 right-4 text-white">
          <p className="text-xs font-semibold text-indigo-200 flex items-center gap-1 mb-1">
            <MapPin className="w-3.5 h-3.5 text-indigo-300" />
            {destinationDisplay}
          </p>
          <h3
            className={`font-extrabold text-lg sm:text-xl leading-snug drop-shadow-sm line-clamp-2 ${
              !trip.name?.trim() ? 'text-slate-300 italic font-semibold' : ''
            }`}
            title={tripTitle}
          >
            {tripTitle}
          </h3>
        </div>
      </div>

      {/* â”€â”€ Card body â”€â”€ */}
      <div className="p-5 flex-1 flex flex-col justify-between">
        <div>
          {/* Travelers + Budget row */}
          <div className="flex items-center justify-between gap-2 mb-3.5">
            <TravelersBadge travelers={trip.travelers} />
            <span className="text-xs font-bold text-slate-800 bg-slate-100 px-2.5 py-1 rounded-lg border border-slate-200/60 flex items-center gap-1.5">
              <Wallet className="w-3.5 h-3.5 text-emerald-600" />
              {formatBudget(trip.budget)}
            </span>
          </div>

          {/* Description */}
          <div className="mb-4 text-xs">
            {hasNoDesc ? (
              <div className="p-2.5 rounded-xl bg-slate-50 border border-dashed border-slate-300 text-slate-400 italic flex items-center gap-2">
                <Layers className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                <span>No description added. Click below to continue AI discovery notes.</span>
              </div>
            ) : (
              <div>
                <p className={`text-slate-600 leading-relaxed ${!expanded && isLongDesc ? 'line-clamp-2' : ''}`}>
                  {trip.description}
                </p>
                {isLongDesc && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v) }}
                    className="mt-1 text-[11px] font-bold text-indigo-600 hover:text-indigo-800 flex items-center gap-0.5 transition cursor-pointer"
                  >
                    {expanded ? (
                      <><span>Show less</span><ChevronUp className="w-3 h-3" /></>
                    ) : (
                      <><span>Read full overview</span><ChevronDown className="w-3 h-3" /></>
                    )}
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Factor tags â€” clickable to set search */}
          {factors.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-4">
              {factors.slice(0, 3).map((f, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); onTagClick(f) }}
                  className="px-2 py-0.5 bg-slate-100 hover:bg-indigo-50 hover:text-indigo-700 hover:border-indigo-200 text-slate-700 rounded-md text-[11px] font-medium border border-slate-200/60 transition cursor-pointer"
                  title={`Filter by: ${f}`}
                >
                  #{f}
                </button>
              ))}
              {factors.length > 3 && (
                <span className="px-1.5 py-0.5 bg-slate-50 text-slate-400 rounded-md text-[10px] font-medium border border-slate-200">
                  +{factors.length - 3}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="pt-3.5 border-t border-slate-100 flex items-center justify-between gap-2">
          <button
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onDeleteRequest(trip) }}
            className="p-2 text-slate-400 hover:text-rose-600 rounded-xl hover:bg-rose-50 transition cursor-pointer"
            title="Delete Trip"
          >
            <Trash2 className="w-4 h-4" />
          </button>

          <Link
            to={`/trips/${trip.slug}`}
            className={`px-4 py-2 font-bold text-xs rounded-xl transition-all duration-200 flex items-center gap-1.5 group/btn ${
              isAttention
                ? 'bg-rose-50 hover:bg-rose-600 text-rose-700 hover:text-white border border-rose-200/80'
                : 'bg-indigo-50 hover:bg-indigo-600 text-indigo-700 hover:text-white border border-indigo-100'
            }`}
          >
            <span>{isAttention ? 'Review & Retry' : 'Open Trip'}</span>
            <ArrowRight className="w-3.5 h-3.5 group-hover/btn:translate-x-1 transition-transform" />
          </Link>
        </div>
      </div>
    </div>
  )
}

const STATUS_FILTERS = [
  { key: 'all',         label: 'All' },
  { key: 'ready',       label: 'Ready' },
  { key: 'planning',    label: 'Planning' },
  { key: 'customizing', label: 'Customizing' },
  { key: 'draft',       label: 'Draft' },
  { key: 'archived',    label: 'Archived' },
  { key: 'attention',   label: 'Attention' },
] as const

type StatusKey = (typeof STATUS_FILTERS)[number]['key']

const SORT_OPTIONS = [
  { value: 'newest',   label: 'Newest First' },
  { value: 'oldest',   label: 'Oldest First' },
  { value: 'name',     label: 'Name A–Z' },
  { value: 'duration', label: 'Longest Trip' },
  { value: 'budget',   label: 'Highest Budget' },
] as const

type SortKey = (typeof SORT_OPTIONS)[number]['value']

function TripCardSkeleton() {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm animate-pulse">
      <div className="h-36 bg-slate-200" />
      <div className="p-5 space-y-4">
        <div className="h-5 w-3/4 rounded bg-slate-200" />
        <div className="h-4 w-1/2 rounded bg-slate-100" />
        <div className="flex gap-2">
          <div className="h-6 w-16 rounded-full bg-slate-100" />
          <div className="h-6 w-20 rounded-full bg-slate-100" />
        </div>
        <div className="flex items-center justify-between border-t border-slate-100 pt-4">
          <div className="h-8 w-8 rounded-xl bg-slate-100" />
          <div className="h-8 w-24 rounded-xl bg-slate-200" />
        </div>
      </div>
    </div>
  )
}

const MyTrips: React.FC = () => {
  const { user, isAuthenticated, openLogin } = useAuth()
  const queryClient = useQueryClient()
  const userKey = user?.id ?? 'anonymous'
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusKey>('all')
  const [sortBy, setSortBy] = useState<SortKey>('newest')
  const [tripToDelete, setTripToDelete] = useState<BackendTripCard | null>(null)

  const tripsQuery = useQuery({
    queryKey: ['trips', userKey],
    enabled: isAuthenticated,
    queryFn: async (): Promise<BackendTripCard[]> => {
      const response = await apiFetch('/api/trips')
      if (!response.ok) throw new Error(`Failed to load trips (${response.status})`)
      const data = await response.json()
      return Array.isArray(data.trips) ? data.trips : []
    },
  })

  const trips = useMemo(() => tripsQuery.data ?? [], [tripsQuery.data])
  const loading = tripsQuery.isLoading

  const confirmDelete = async () => {
    if (!tripToDelete) return
    const queryKey = ['trips', userKey]
    const previousTrips = trips
    queryClient.setQueryData<BackendTripCard[]>(queryKey, (current = []) =>
      current.filter((trip) => trip.id !== tripToDelete.id)
    )
    setTripToDelete(null)
    try {
      const response = await apiFetch(`/api/trips/${tripToDelete.id}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(`Delete failed (${response.status})`)
    } catch (err) {
      console.error('[MyTrips] Delete failed', err)
      queryClient.setQueryData(queryKey, previousTrips)
    } finally {
      void queryClient.invalidateQueries({ queryKey: ['trips'] })
    }
  }

  const normaliseStatus = (status: string): StatusKey => {
    const s = (status || '').toLowerCase()
    if (s === 'confirmed' || s === 'completed' || s === 'ready') return 'ready'
    if (s === 'planning' || s === 'checkpoint_review' || s === 'draft_review') return 'planning'
    if (s === 'customizing' || s === 'collecting' || s === 'awaiting_confirmation') return 'customizing'
    if (s === 'archived' || s === 'cancelled') return 'archived'
    if (s === 'attention' || s === 'error' || s === 'failed') return 'attention'
    return 'draft'
  }

  const countFor = (key: StatusKey) =>
    key === 'all'
      ? trips.length
      : trips.filter((t) => normaliseStatus(t.status) === key).length

  const filtered = useMemo(() =>
    trips
      .filter((trip) => {
        if (statusFilter !== 'all' && normaliseStatus(trip.status) !== statusFilter) return false
        if (!search.trim()) return true
        const q = search.toLowerCase()
        return (
          trip.name.toLowerCase().includes(q) ||
          (trip.destination && trip.destination.toLowerCase().includes(q)) ||
          (trip.description && trip.description.toLowerCase().includes(q)) ||
          (trip.dates && trip.dates.toLowerCase().includes(q)) ||
          (Array.isArray(trip.factors) && trip.factors.some((f) => f.toLowerCase().includes(q)))
        )
      })
      .sort((a, b) => {
        if (sortBy === 'name') return a.name.localeCompare(b.name)
        if (sortBy === 'duration') return (b.duration_days ?? 0) - (a.duration_days ?? 0)
        if (sortBy === 'budget') return (b.budget?.amount ?? 0) - (a.budget?.amount ?? 0)
        if (sortBy === 'oldest') return new Date(a.created_at || '').getTime() - new Date(b.created_at || '').getTime()
        return new Date(b.created_at || '').getTime() - new Date(a.created_at || '').getTime()
      }),
    [trips, statusFilter, search, sortBy]
  )

  if (!isAuthenticated) {
    return (
      <div className="max-w-md mx-auto px-4 py-20 text-center">
        <div className="w-16 h-16 rounded-3xl bg-indigo-50 text-indigo-600 flex items-center justify-center mx-auto mb-5 shadow-xs border border-indigo-100">
          <Lock className="w-8 h-8" />
        </div>
        <h2 className="text-2xl font-black text-slate-900 mb-2">Sign In to View Trips</h2>
        <p className="text-xs sm:text-sm text-slate-500 mb-6 leading-relaxed">
          Your trips and verified itineraries are saved to your account.
        </p>
        <button onClick={openLogin} className="px-6 py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-xs sm:text-sm rounded-2xl shadow-md shadow-indigo-600/25 transition cursor-pointer">
          Sign In with Google
        </button>
      </div>
    )
  }

  return (
    <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 sm:py-10 w-full min-h-[calc(100vh-4rem)] flex flex-col">

      {/* ── Page Title ── */}
      <div className="pb-6 border-b border-slate-200/80 mb-6">
        <h1 className="text-3xl sm:text-4xl font-black text-slate-900 tracking-tight">My Trips</h1>
        <p className="text-slate-500 text-sm sm:text-base mt-1.5">
          Manage and explore your AI-planned travel itineraries.
        </p>
      </div>

      {/* ── Toolbar ── */}
      {trips.length > 0 && (
        <div className="w-full flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-3 mb-8 p-1.5 bg-slate-50/80 rounded-2xl border border-slate-200/80 shadow-2xs">
        <div className="flex items-center gap-1 overflow-x-auto p-1 bg-slate-200/60 rounded-xl">
          {STATUS_FILTERS.map(({ key, label }) => {
            const count = countFor(key)
            const active = statusFilter === key
            return (
              <button
                key={key}
                onClick={() => setStatusFilter(key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-bold whitespace-nowrap transition-all flex items-center gap-1.5 cursor-pointer ${
                  active ? 'bg-slate-900 text-white shadow-xs' : 'text-slate-600 hover:text-slate-900 hover:bg-slate-200/80'
                }`}
              >
                <span>{label}</span>
                <span className={`text-[10px] px-1.5 rounded-full font-semibold ${
                  active ? 'bg-slate-700 text-white' : 'bg-slate-300/80 text-slate-700'
                }`}>{count}</span>
              </button>
            )
          })}
        </div>

        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 px-1">
          <div className="relative min-w-[240px] flex-1 sm:flex-initial">
            <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search destination, tag, activity..."
              className="w-full pl-9 pr-8 py-1.5 text-xs font-medium rounded-xl bg-white border border-slate-300 focus:outline-none focus:ring-2 focus:ring-indigo-500 shadow-2xs"
            />
            {search && (
              <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-400 hover:text-slate-600 font-bold cursor-pointer">✕</button>
            )}
          </div>
          <div className="flex items-center gap-1.5 bg-white border border-slate-300 rounded-xl px-2.5 py-1.5 shadow-2xs">
            <SlidersHorizontal className="w-3.5 h-3.5 text-slate-500" />
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as SortKey)}
              className="text-xs font-semibold text-slate-700 bg-transparent border-none focus:outline-none cursor-pointer"
            >
              {SORT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
        </div>
      )}

      {/* ── Content ── */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6" aria-label="Loading your trips">
          <TripCardSkeleton />
          <TripCardSkeleton />
          <TripCardSkeleton />
        </div>
      ) : filtered.length === 0 ? (
        <div className="w-full py-16 sm:py-24 px-4 text-center flex flex-col items-center justify-center">
          {trips.length > 0 && (search || statusFilter !== 'all') ? (
            <div className="max-w-md mx-auto">
              <div className="w-16 h-16 rounded-3xl bg-slate-100 text-slate-400 flex items-center justify-center mx-auto mb-4 border border-slate-200">
                <Search className="w-7 h-7" />
              </div>
              <h3 className="text-xl font-bold text-slate-900 tracking-tight mb-2">No trips match "{search}"</h3>
              <p className="text-slate-500 text-sm mb-6 leading-relaxed">Try a different destination or clear your filters.</p>
              <button
                onClick={() => { setSearch(''); setStatusFilter('all') }}
                className="px-5 py-2.5 bg-slate-900 hover:bg-slate-800 text-white text-xs font-bold rounded-xl shadow-md transition cursor-pointer"
              >
                Clear Search & Filters
              </button>
            </div>
          ) : (
            <div className="max-w-md mx-auto flex flex-col items-center relative">
              <div className="absolute -top-10 w-48 h-48 bg-indigo-500/10 rounded-full blur-3xl -z-10 pointer-events-none" />
              <div className="w-20 h-20 rounded-3xl bg-gradient-to-tr from-indigo-500 to-violet-600 text-white flex items-center justify-center mx-auto mb-6 shadow-xl shadow-indigo-500/25">
                <Compass className="w-10 h-10 animate-pulse" />
              </div>
              <h2 className="text-3xl sm:text-4xl font-black text-slate-900 tracking-tight mb-3">No trips yet</h2>
              <p className="text-slate-500 text-sm sm:text-base max-w-sm mx-auto mb-8 leading-relaxed">
                Start a conversation with your travel assistant to craft your custom day-by-day travel itinerary.
              </p>
              <Link
                to="/create"
                className="inline-flex items-center justify-center gap-2 px-7 py-3.5 bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-sm rounded-full shadow-lg shadow-indigo-600/25 hover:scale-[1.02] active:scale-[0.98] transition-all group cursor-pointer"
              >
                <Sparkles className="w-4 h-4 text-indigo-200 group-hover:rotate-12 transition-transform" />
                <span>Plan a Trip with AI</span>
                <ArrowRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" />
              </Link>
            </div>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filtered.map((trip) => (
            <TripCard
              key={trip.id}
              trip={trip}
              onDeleteRequest={setTripToDelete}
              onTagClick={(tag) => setSearch(tag)}
            />
          ))}
        </div>
      )}

      {/* ── Delete Confirmation Modal ── */}
      {tripToDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/60 backdrop-blur-sm"
          onClick={() => setTripToDelete(null)}
        >
          <div
            className="bg-white rounded-3xl border border-slate-200 shadow-2xl max-w-sm w-full p-6 sm:p-7 text-center"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-14 h-14 rounded-2xl bg-rose-50 border border-rose-100 text-rose-600 flex items-center justify-center mx-auto mb-4">
              <Trash2 className="w-7 h-7" />
            </div>
            <h3 className="text-2xl font-black text-slate-900 tracking-tight mb-2">Delete Trip?</h3>
            <p className="text-sm text-slate-600 leading-relaxed mb-6">
              Are you sure you want to delete{' '}
              <strong className="font-extrabold text-slate-900">
                "{tripToDelete.name?.trim() || tripToDelete.destination?.trim() || 'Untitled Trip'}"
              </strong>
              ? This action cannot be undone.
            </p>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setTripToDelete(null)}
                className="flex-1 py-3 px-4 rounded-xl text-sm font-bold text-slate-700 bg-slate-100 hover:bg-slate-200 transition cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmDelete}
                className="flex-1 py-3 px-4 rounded-xl text-sm font-bold text-white bg-rose-600 hover:bg-rose-700 shadow-md shadow-rose-600/25 transition flex items-center justify-center gap-1.5 cursor-pointer"
              >
                <Trash2 className="w-4 h-4" />
                <span>Delete</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}

export default MyTrips
