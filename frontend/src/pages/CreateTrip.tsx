import React, { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Sparkles,
  Send,
  Calendar,
  MapPin,
  Car,
  Hotel,
  Download,
  CheckCircle2,
  Lock,
  Info,
  Zap,
  SlidersHorizontal,
  RefreshCw,
  Compass,
  Users,
  Wallet,
  Layers,
  Map,
  ExternalLink,
  Sun,
  CheckSquare,
  Square,
  Navigation,
  Loader2,
  AlertCircle,
  LogIn,
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readApiError } from '../lib/api'
import { getWelcomeMessage } from '../lib/welcome'
import { parseBackendPlan, placeholderCover, type NormalizedPlan } from '../lib/plan'
import { getPlanningState } from '../lib/planning-status'
import { applyChatStream, mergeChatMessages, type ChatMessage as Message } from '../lib/chat-messages'

interface JobStatus {
  status: string
  progress_percent: number
  current_step: string
  current_stage?: string | null
  progress_kind?: string | null
  completed_tasks: number
  total_tasks: number
  activity_detail?: string | null
  progress_sequence?: number | null
  failure_details?: any
}

interface PlanningStep {
  id: string
  label: string
  description: string
}


interface BackendTrip {
  id: string | number
  slug?: string
  name: string
  description?: string
  cover_image_url?: string
  status?: string
  duration_days?: number | null
  travelers?: { adults: number; children: number; seniors?: number }
  budget?: { currency: string; amount?: number | null }
  factors?: string[]
  origin_city?: string | null
  destination_items?: string[]
  trip_scope?: string | null
  spending_tier?: string | null
  travel_style?: string | null
  trip_purpose?: string | null
}


const DEFAULT_INITIAL_SUGGESTIONS = [
  '🌴 Help me plan a relaxing beach getaway',
  '👨‍👩‍👧‍👦 Build a family trip with kid-friendly activities',
  '🍜 Plan a food and culture focused journey',
]

const PLANNING_STEPS: PlanningStep[] = [
  { id: 'starting', label: 'Starting', description: 'Preparing your confirmed trip details.' },
  { id: 'checking_destination', label: 'Checking destination', description: 'Validating your destination and travel route.' },
  { id: 'finding_places', label: 'Finding places', description: 'Finding places that fit your trip.' },
  { id: 'checking_details', label: 'Checking details', description: 'Checking weather, practical details and current travel information.' },
  { id: 'building_itinerary', label: 'Building itinerary', description: 'Creating your day-by-day itinerary.' },
  { id: 'checking_routes', label: 'Checking routes', description: 'Checking routes and travel times.' },
  { id: 'estimating_costs', label: 'Estimating costs', description: 'Estimating costs against your budget.' },
  { id: 'reviewing_plan', label: 'Reviewing plan', description: 'Reviewing timing, feasibility and your trip preferences.' },
  { id: 'improving_plan', label: 'Improving plan', description: 'Making any needed improvements.' },
  { id: 'finalizing', label: 'Finalising itinerary', description: 'Preparing your finished itinerary.' },
]


function CreateTripSkeleton() {
  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-5 h-[calc(100vh-4.5rem)] flex flex-col animate-pulse" aria-label="Loading trip workspace">
      <div className="h-20 rounded-3xl border border-slate-200 bg-white px-5 flex items-center justify-between mb-5">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-2xl bg-slate-200" />
          <div className="space-y-2">
            <div className="h-4 w-44 rounded bg-slate-200" />
            <div className="h-3 w-32 rounded bg-slate-100" />
          </div>
        </div>
        <div className="h-8 w-28 rounded-xl bg-slate-100" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 flex-1 min-h-0">
        <div className="lg:col-span-5 rounded-3xl border border-slate-200 bg-white p-6 space-y-5">
          <div className="flex gap-3"><div className="h-7 w-7 rounded-xl bg-slate-200" /><div className="h-16 flex-1 rounded-2xl bg-slate-100" /></div>
          <div className="flex gap-3"><div className="h-7 w-7 rounded-xl bg-slate-200" /><div className="h-20 w-4/5 rounded-2xl bg-slate-100" /></div>
          <div className="flex gap-3"><div className="h-7 w-7 rounded-xl bg-slate-200" /><div className="h-14 w-3/5 rounded-2xl bg-slate-100" /></div>
        </div>
        <div className="lg:col-span-7 rounded-3xl border border-slate-200 bg-white p-6 sm:p-8 space-y-5">
          <div className="h-6 w-1/3 rounded bg-slate-200" />
          <div className="h-4 w-2/3 rounded bg-slate-100" />
          <div className="grid grid-cols-2 gap-4 pt-4"><div className="h-28 rounded-2xl bg-slate-100" /><div className="h-28 rounded-2xl bg-slate-100" /></div>
          <div className="h-32 rounded-2xl bg-slate-100" />
        </div>
      </div>
    </div>
  )
}

const CreateTrip: React.FC = () => {
  const { tripId } = useParams<{ tripId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { isAuthenticated, openLogin, user } = useAuth()
  const queryClient = useQueryClient()
  const userKey = user?.id ?? 'anonymous'
  const tripQueryKey = useMemo(() => ['trip', userKey, tripId] as const, [userKey, tripId])
  const messagesQueryKey = useMemo(() => ['trip-messages', userKey, tripId] as const, [userKey, tripId])
  const planQueryKey = useMemo(() => ['trip-plan', userKey, tripId] as const, [userKey, tripId])
  const cachedTrip = tripId ? queryClient.getQueryData<BackendTrip>(tripQueryKey) : undefined
  const cachedMessages = tripId ? queryClient.getQueryData<Message[]>(messagesQueryKey) : undefined
  const cachedPlan = tripId ? queryClient.getQueryData<NormalizedPlan | null>(planQueryKey) : undefined

  const [trip, setTrip] = useState<BackendTrip | null>(cachedTrip ?? null)
  const [welcomeMessage, setWelcomeMessage] = useState(() => getWelcomeMessage(user))
  const [messages, setMessages] = useState<Message[]>(cachedMessages ?? [
    {
      id: 'welcome',
      role: 'assistant',
      content: welcomeMessage,
      isWelcome: true,
      timestamp: 'Just now',
      suggestions: DEFAULT_INITIAL_SUGGESTIONS,
    },
  ])

  const [input, setInput] = useState('')
  const [pendingMessage, setPendingMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [thinkingStep, setThinkingStep] = useState<string | null>(null)
  const [processingDots, setProcessingDots] = useState('.')
  const [currentSuggestions, setCurrentSuggestions] = useState<string[]>(DEFAULT_INITIAL_SUGGESTIONS)
  const [job, setJob] = useState<JobStatus | null>(null)
  const [plan, setPlan] = useState<NormalizedPlan | null>(cachedPlan ?? null)
  const [refreshingDisplay, setRefreshingDisplay] = useState(false)
  const [displayError, setDisplayError] = useState<string | null>(null)
  const [isInitialTripLoading, setIsInitialTripLoading] = useState(Boolean(tripId && (!cachedTrip || !cachedMessages)))
  const [isCanvasOpen, setIsCanvasOpen] = useState<boolean>(false)
  const [activeTab, setActiveTab] = useState<'timeline' | 'map' | 'stays' | 'tips'>('timeline')
  const [selectedDayFilter, setSelectedDayFilter] = useState<number | 'all'>('all')
  const [checkedItems, setCheckedItems] = useState<Record<number, boolean>>({})

  const chatEndRef = useRef<HTMLDivElement>(null)
  const followChatRef = useRef(true)
  const inputRef = useRef<HTMLInputElement>(null)
  const activeStreamMessageIdRef = useRef<string | null>(null)
  const historyRequestRef = useRef(0)
  const collectionPendingRef = useRef(false)
  const pendingWelcomeMessageRef = useRef<string | null>(null)
  const previousTripIdRef = useRef<string | undefined>(tripId)

  const headers = useCallback((json = false): Record<string, string> => {
    return json ? { 'Content-Type': 'application/json' } : {}
  }, [])

  const sendMessageFromEffect = useEffectEvent((message: string) => {
    void handleSendMessage(message)
  })

  // Load Trip details from backend
  const loadTrip = useCallback(async (force = false) => {
    if (!tripId || !isAuthenticated) return
    try {
      const nextTrip = await queryClient.fetchQuery({
        queryKey: tripQueryKey,
        staleTime: force ? 0 : undefined,
        queryFn: async (): Promise<BackendTrip | null> => {
          const res = await apiFetch(`/api/trips/${tripId}`, { headers: headers() })
          if (!res.ok) throw new Error(`Trip request failed (${res.status})`)
          const data = await res.json()
          return data.trip ?? null
        },
      })
      if (previousTripIdRef.current === tripId) setTrip(nextTrip)
    } catch (err) {
      console.error('[CreateTrip] loadTrip failed', err)
    }
  }, [headers, isAuthenticated, queryClient, tripId, tripQueryKey])

  // Load chat messages from backend
  const loadMessages = useCallback(async (force = false) => {
    if (!tripId || !isAuthenticated) return
    const request = ++historyRequestRef.current
    try {
      if (force) await queryClient.cancelQueries({ queryKey: messagesQueryKey, exact: true })
      const nextMessages = await queryClient.fetchQuery({
        queryKey: messagesQueryKey,
        staleTime: force ? 0 : undefined,
        queryFn: async ({ signal }): Promise<Message[]> => {
          const res = await apiFetch(`/api/trips/${tripId}/messages`, { headers: headers(), signal })
          if (!res.ok) throw new Error(`Messages request failed (${res.status})`)
          const data = await res.json()
          return Array.isArray(data.messages)
            ? data.messages.map((m: any) => ({
              id: m.id,
              role: m.role,
              content: m.content || m.text,
              timestamp: m.created_at || 'Just now',
              clientMessageId: m.meta?.client_message_id,
              delivery: 'saved',
              isWelcome: m.meta?.branch === 'welcome',
              showConfirmationCard: m.meta?.status === 'awaiting_confirmation',
              snapshot: m.meta?.snapshot,
              suggestions: Array.isArray(m.meta?.suggestions) ? m.meta.suggestions : undefined,
            }))
            : []
        },
      })
      if (request !== historyRequestRef.current || previousTripIdRef.current !== tripId) return
      if (nextMessages.length > 0) {
        const visibleWelcome = pendingWelcomeMessageRef.current
        const persistedWelcome = nextMessages.find((message) => message.isWelcome)
        const visibleMessages = visibleWelcome
          ? persistedWelcome
            ? nextMessages.map((message) => message.isWelcome
              ? { ...message, content: visibleWelcome }
              : message)
            : [
                {
                  id: 'welcome',
                  role: 'assistant' as const,
                  content: visibleWelcome,
                  timestamp: 'Just now',
                  isWelcome: true,
                  suggestions: DEFAULT_INITIAL_SUGGESTIONS,
                },
                ...nextMessages,
              ]
          : nextMessages
        setMessages(current => mergeChatMessages(current, visibleMessages))
        const lastAssistantMessage = [...visibleMessages].reverse().find((message) => message.role === 'assistant')
        if (!collectionPendingRef.current) setCurrentSuggestions(lastAssistantMessage?.suggestions ?? [])
      }
    } catch (err) {
      if (request === historyRequestRef.current && previousTripIdRef.current === tripId) {
        console.error('[CreateTrip] loadMessages failed', err)
      }
    }
  }, [headers, isAuthenticated, messagesQueryKey, queryClient, tripId])

  // Load finalized plan from backend
  const loadPlan = useCallback(async (force = false) => {
    if (!tripId || !isAuthenticated) return
    try {
      const nextPlan = await queryClient.fetchQuery({
        queryKey: planQueryKey,
        staleTime: force ? 0 : undefined,
        queryFn: async (): Promise<NormalizedPlan | null> => {
          const res = await apiFetch(`/api/trips/${tripId}/plan`, { headers: headers() })
          if (!res.ok) return null
          const data = await res.json()
          return parseBackendPlan(data)
        },
      })
      if (previousTripIdRef.current === tripId) setPlan(nextPlan)
    } catch (err) {
      console.debug('[CreateTrip] loadPlan: Plan not ready yet', err)
    }
  }, [headers, isAuthenticated, planQueryKey, queryClient, tripId])

  const refreshPlanDisplay = async () => {
    if (!tripId || !isAuthenticated || refreshingDisplay) return
    setRefreshingDisplay(true)
    setDisplayError(null)
    try {
      const response = await apiFetch(`/api/trips/${tripId}/plan/display`, {
        method: 'POST', headers: headers(true),
        body: JSON.stringify({ place_ids: plan?.placeIds?.slice(0, 10), include_photos: false }),
      })
      if (!response.ok) throw new Error('Place details could not be refreshed. The saved itinerary is unchanged.')
      const nextPlan = parseBackendPlan(await response.json())
      if (!nextPlan) throw new Error('The refreshed plan could not be displayed.')
      if (previousTripIdRef.current === tripId) {
        setPlan(nextPlan)
        queryClient.setQueryData(planQueryKey, nextPlan)
      }
    } catch (error) {
      setDisplayError(error instanceof Error ? error.message : 'Place details could not be refreshed.')
    } finally {
      setRefreshingDisplay(false)
    }
  }

  // The persisted job is the source of truth. SSE asks for an immediate
  // refresh while this page is open; the polling effect below covers missed
  // events, reconnects, and users who return to a planning trip later.
  const refreshJobStatus = useCallback(async () => {
    if (!tripId || !isAuthenticated) return
    try {
      const response = await apiFetch(`/api/trips/${tripId}/job-status`, { headers: headers() })
      if (!response.ok) return
      const result = (await response.json()) as JobStatus
      if (previousTripIdRef.current !== tripId) return
      setJob(result.status === 'idle' || result.status === 'not_started' ? null : result)
      if (result.status === 'completed') {
        await Promise.all([loadTrip(true), loadPlan(true)])
      }
    } catch (err) {
      console.error('[CreateTrip] refresh job status failed', err)
    }
  }, [headers, isAuthenticated, loadPlan, loadTrip, tripId])

  // Initial Load
  useEffect(() => {
    if (!tripId) {
      const returnedFromExistingTrip = previousTripIdRef.current !== undefined
      previousTripIdRef.current = undefined
      setTrip(null)
      setPlan(null)
      setJob(null)
      setIsCanvasOpen(false)
      setIsInitialTripLoading(false)
      setCurrentSuggestions(DEFAULT_INITIAL_SUGGESTIONS)
      // The initial state has already chosen a greeting. Only choose another
      // when the user actively leaves an existing trip for a new blank trip.
      if (returnedFromExistingTrip) {
        collectionPendingRef.current = false
        setBusy(false)
        setThinkingStep(null)
        const nextWelcomeMessage = getWelcomeMessage(user)
        setWelcomeMessage(nextWelcomeMessage)
        setMessages([
          {
            id: 'welcome',
            role: 'assistant',
            content: nextWelcomeMessage,
            isWelcome: true,
            timestamp: 'Just now',
            suggestions: DEFAULT_INITIAL_SUGGESTIONS,
          },
        ])
      }
      return
    }
    if (previousTripIdRef.current !== tripId) {
      const initialHandoff = previousTripIdRef.current === undefined
        && Boolean(queryClient.getQueryData(messagesQueryKey))
      setMessages(queryClient.getQueryData<Message[]>(messagesQueryKey) ?? [])
      setTrip(queryClient.getQueryData<BackendTrip>(tripQueryKey) ?? null)
      setPlan(queryClient.getQueryData<NormalizedPlan>(planQueryKey) ?? null)
      setJob(null)
      activeStreamMessageIdRef.current = null
      followChatRef.current = true
      if (!initialHandoff) {
        collectionPendingRef.current = false
        setBusy(false)
        setThinkingStep(null)
      }
    }
    previousTripIdRef.current = tripId
    // Keep the current chat visible while its newly created slug is placed in
    // the URL. The query cache is seeded before navigation below, so this is
    // a client-side URL transition rather than a workspace reload.
    const hasCachedWorkspace = Boolean(queryClient.getQueryData(messagesQueryKey))
    setIsInitialTripLoading(!hasCachedWorkspace)
    let isCurrentTrip = true
    void Promise.all([loadTrip(), loadMessages(), loadPlan(), refreshJobStatus()]).finally(() => {
      if (isCurrentTrip) setIsInitialTripLoading(false)
    })
    return () => { isCurrentTrip = false }
  }, [tripId, user, loadTrip, loadMessages, loadPlan, messagesQueryKey, tripQueryKey, planQueryKey, queryClient, refreshJobStatus])

  // Automatically send pending message after user completes login
  useEffect(() => {
    if (isAuthenticated && pendingMessage) {
      const msg = pendingMessage
      setPendingMessage(null)
      sendMessageFromEffect(msg)
    }
  }, [isAuthenticated, pendingMessage, sendMessageFromEffect])

  // Handle Starter Prompt from landing page
  useEffect(() => {
    const starterPrompt = (location.state as { starterPrompt?: string } | null)?.starterPrompt
    if (!tripId && starterPrompt) {
      if (!isAuthenticated) {
        setInput(starterPrompt)
        setPendingMessage(starterPrompt)
        openLogin()
      } else {
        sendMessageFromEffect(starterPrompt)
      }
      window.history.replaceState({}, document.title)
    }
  }, [location.state, tripId, isAuthenticated, openLogin, sendMessageFromEffect])

  useEffect(() => {
    // Token streaming can update the chat many times per second. Restarting a
    // smooth scroll for every token makes the reply visibly jump and stutter.
    if (followChatRef.current) chatEndRef.current?.scrollIntoView({ behavior: 'auto', block: 'nearest' })
  }, [messages])

  useEffect(() => {
    if (!busy || !thinkingStep) {
      setProcessingDots('.')
      return
    }

    let count = 1
    setProcessingDots('.')
    const timer = window.setInterval(() => {
      count = count === 3 ? 1 : count + 1
      setProcessingDots('.'.repeat(count))
    }, 400)
    return () => window.clearInterval(timer)
  }, [busy, thinkingStep])

  // SSE Stream Listener for real-time status & token streaming
  useEffect(() => {
    if (!tripId || !isAuthenticated) return
    const controller = new AbortController()
    let retryTimer: number | undefined
    let retryCount = 0

    const reconnect = () => {
      if (controller.signal.aborted || retryCount >= 5) return
      const delay = Math.min(1_000 * 2 ** retryCount, 15_000)
      retryCount += 1
      retryTimer = window.setTimeout(() => void stream(), delay)
    }

    const stream = async () => {
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      try {
        const response = await apiFetch(`/api/trips/${tripId}/stream`, {
          headers: headers(),
          signal: controller.signal,
        })
        if (!response.ok || !response.body) {
          return
        }

        retryCount = 0
        reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (!controller.signal.aborted) {
          const next = await reader.read()
          if (next.done) break
          buffer += decoder.decode(next.value, { stream: true })
          const events = buffer.split('\n\n')
          buffer = events.pop() ?? ''

          events.forEach((event) => {
            if (previousTripIdRef.current !== tripId) return
            const name = event.match(/^event:\s*(.+)$/m)?.[1] ?? 'message'
            const raw = event.match(/^data:\s*(.+)$/m)?.[1]
            if (!raw) return

            try {
              const payload = JSON.parse(raw)
              if (name === 'status') {
                setThinkingStep(payload.message || null)
                if (payload.status !== 'idle' && payload.status !== 'error') {
                  collectionPendingRef.current = true
                  setBusy(true)
                  setCurrentSuggestions([])
                }
                // Reconnection may happen after the turn finished. Only an
                // explicit terminal snapshot can complete a missed live turn.
                if (payload.turn_complete === true && collectionPendingRef.current) {
                  collectionPendingRef.current = false
                  activeStreamMessageIdRef.current = null
                  setBusy(false)
                  setThinkingStep(null)
                  void loadMessages(true)
                }
              }
              if (name === 'planning_status') {
                // Progress is already durable in planning_jobs before this
                // event is published. Read it immediately for live updates.
                void refreshJobStatus()
              }
              if (name === 'error_event' && payload.message) {
                collectionPendingRef.current = false
                activeStreamMessageIdRef.current = null
                setMessages((current) => {
                  const message: Message = {
                    id: payload.message_id ? String(payload.message_id) : `error_${Date.now()}`,
                    role: 'assistant', content: payload.message, timestamp: 'Just now',
                    delivery: payload.message_id ? 'saved' : 'failed',
                  }
                  return mergeChatMessages(current, [message])
                })
                setThinkingStep(null)
                setBusy(false)
              }
              if (name === 'suggestions' && Array.isArray(payload.suggestions)) {
                setCurrentSuggestions(payload.suggestions)
              }
              if (name === 'stream' && (payload.text || payload.chunk)) {
                const streamMessageId = payload.message_id ? String(payload.message_id) : activeStreamMessageIdRef.current
                  ?? `streaming_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
                activeStreamMessageIdRef.current = streamMessageId
                setMessages(current => applyChatStream(current, streamMessageId, payload.chunk, payload.text))
              }
              if (name === 'checkpoint') {
                setMessages((current) => {
                  const nextMessages = [...current]
                  const last = nextMessages[nextMessages.length - 1]
                  if (last) {
                    last.showConfirmationCard = true
                    last.snapshot = payload
                  }
                  return nextMessages
                })
                void loadTrip(true)
              }
              if (name === 'stream_end') {
                collectionPendingRef.current = false
                activeStreamMessageIdRef.current = null
                setThinkingStep(null)
                setBusy(false)
                void loadMessages(true)
                void loadTrip(true)
              }
              if (payload.job_status) {
                setJob(payload.job_status)
              }
            } catch {
              /* ignore keepalive comments */
            }
          })
        }
      } catch (err) {
        if ((err as DOMException).name !== 'AbortError') {
          console.debug('[CreateTrip] SSE stream disconnected; retrying while polling remains active.')
        }
      } finally {
        reader?.releaseLock()
        if (!controller.signal.aborted) reconnect()
      }
    }

    void stream()
    return () => {
      controller.abort()
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [tripId, isAuthenticated, headers, loadMessages, loadTrip, refreshJobStatus])

  // Poll background job when solver is running
  useEffect(() => {
    // Phase 1 does not create a planning job. Polling it here was repeatedly
    // calling the protected endpoint before authentication (and for every
    // collecting trip), which flooded the backend with 401 responses.
    const isPlanning = trip?.status === 'planning' || job?.status === 'running' || job?.status === 'queued'
    if (!tripId || !isAuthenticated || !isPlanning) return
    void refreshJobStatus()
    // SSE normally delivers the change immediately. Ten seconds is only the
    // recovery interval, keeping the UI correct if a stream is interrupted.
    const timer = window.setInterval(() => void refreshJobStatus(), 10_000)
    return () => window.clearInterval(timer)
  }, [tripId, isAuthenticated, trip?.status, job?.status, plan, refreshJobStatus])

  // Send Message
  const handleSendMessage = async (textToSend?: string) => {
    const text = (textToSend || input).trim()
    if (!text || busy || collectionPendingRef.current || hasPlanningStarted) return

    // Unauthenticated Guard: Require user to login first before dispatching message
    if (!isAuthenticated) {
      setInput(text)
      setPendingMessage(text)
      openLogin()
      return
    }

    setInput('')
    // Suggestions describe the previous reply. Hide them while the next
    // request is in progress, until the server publishes relevant new ones.
    setCurrentSuggestions([])
    setBusy(true)
    collectionPendingRef.current = true
    followChatRef.current = true
    activeStreamMessageIdRef.current = null
    setThinkingStep('Sending your message')

    // Immediately reflect the user's message in the chat
    const clientMessageId = crypto.randomUUID()
    const userMsg: Message = {
      id: `u_${clientMessageId}`,
      role: 'user',
      content: text,
      timestamp: 'Just now',
      clientMessageId,
      delivery: 'pending',
    }
    const optimisticMessages = [...messages, userMsg]
    setMessages(optimisticMessages)

    // If starting a fresh trip on /create without a slug
    if (!tripId) {
      pendingWelcomeMessageRef.current = welcomeMessage
      try {
        const res = await apiFetch('/api/trips/init', {
          method: 'POST',
          headers: headers(true),
          body: JSON.stringify({
            text,
            client_message_id: clientMessageId,
            welcome_message: welcomeMessage,
          }),
        })
        if (res.ok) {
          const data = await res.json()
          if (data.slug) {
            // The message is already durable and its collection turn is queued.
            // SSE will replace this short handoff label with the first live
            // backend stage (for example, "Understanding your message").
            setThinkingStep('Setting up your trip…')
            void queryClient.invalidateQueries({ queryKey: ['trips'] })
            const nextTrip: BackendTrip = {
              id: String(data.trip_id),
              slug: data.slug,
              name: data.trip_name || 'New Adventure',
              status: 'collecting',
            }
            queryClient.setQueryData(['trip', userKey, data.slug], nextTrip)
            queryClient.setQueryData(['trip-messages', userKey, data.slug], optimisticMessages)
            navigate(`/trips/${data.slug}`, { replace: true })
            return
          }
          throw new Error('The server did not return a trip address.')
        } else {
          const errData = await res.json().catch(() => ({}))
          setMessages((prev) => [
            ...prev,
            {
              id: `err_${Date.now()}`,
              role: 'assistant',
              content: `⚠️ Error initializing trip: ${errData.detail || 'Please try again.'}`,
              timestamp: 'Just now',
            },
          ])
          collectionPendingRef.current = false
          setBusy(false)
          setThinkingStep(null)
        }
      } catch (err) {
        console.error('[CreateTrip] Failed to create initial trip', err)
        setMessages((prev) => [
          ...prev,
          {
            id: `err_${Date.now()}`,
            role: 'assistant',
            content: '⚠️ Network error connecting to AI Riya backend. Please ensure server is running.',
            timestamp: 'Just now',
          },
        ])
        collectionPendingRef.current = false
        setBusy(false)
        setThinkingStep(null)
      }
    } else {
      // Sending chat message to existing trip
      try {
        const res = await apiFetch(`/api/trips/${tripId}/chat`, {
          method: 'POST',
          headers: headers(true),
          body: JSON.stringify({ text, client_message_id: clientMessageId }),
        })
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}))
          setMessages((prev) => [
            ...prev,
            {
              id: `err_${Date.now()}`,
              role: 'assistant',
              content: `⚠️ ${errData.detail || 'Could not send message. Please try again.'}`,
              timestamp: 'Just now',
            },
          ])
          collectionPendingRef.current = false
          setBusy(false)
          setThinkingStep(null)
        } else {
          setThinkingStep('Riya is reviewing your message…')
          void queryClient.invalidateQueries({ queryKey: ['trips'] })
        }
      } catch (err) {
        console.error('[CreateTrip] Failed to send chat message', err)
        setMessages((prev) => [
          ...prev,
          {
            id: `err_${Date.now()}`,
            role: 'assistant',
            content: '⚠️ Network connection issue. Please check your internet connection.',
            timestamp: 'Just now',
          },
        ])
        collectionPendingRef.current = false
        setBusy(false)
        setThinkingStep(null)
      }
    }
  }

  // Trigger Phase 2 Autonomous Planning Solver
  const triggerPlanning = async () => {
    if (!tripId || busy) return
    if (!isAuthenticated) {
      openLogin()
      return
    }

    if (!hasPlanningFailure) {
      await handleSendMessage('Yes, start planning')
      return
    }

    setBusy(true)
    setThinkingStep('Requesting another planning attempt')
    try {
      const res = await apiFetch(`/api/trips/${tripId}/planning/retry`, {
        method: 'POST',
        headers: headers(true),
      })
      if (!res.ok) throw new Error(await readApiError(res))
      await refreshJobStatus()
      void queryClient.invalidateQueries({ queryKey: ['trips'] })
    } catch (err) {
      setMessages(current => [...current, {
        id: `error_${Date.now()}`, role: 'assistant',
        content: err instanceof Error ? err.message : 'Planning could not be restarted. Please try again.',
      }])
    } finally {
      setBusy(false)
      setThinkingStep(null)
    }
  }

  const handleSuggestionClick = (suggestion: string) => {
    if (!isAuthenticated) {
      setInput(suggestion)
      setPendingMessage(suggestion)
      openLogin()
      return
    }
    void handleSendMessage(suggestion)
  }

  const togglePackingCheck = (index: number) => {
    setCheckedItems((prev) => ({ ...prev, [index]: !prev[index] }))
  }

  const { isDone, isPlanning, hasPlanningFailure, hasPlanningStarted } = getPlanningState(
    trip?.status, job?.status, Boolean(plan),
  )
  const showPlanningCanvas = isPlanning
  const isComposerLocked = hasPlanningStarted
  const planningFailureMessage = typeof job?.failure_details?.message === 'string'
    ? job.failure_details.message
    : 'We could not finish this itinerary. You can retry planning.'

  useEffect(() => {
    if (isPlanning || isDone) setIsCanvasOpen(true)
  }, [isDone, isPlanning])

  const destinationName = trip?.name ? trip.name.replace(/^Trip to /i, '') : 'Your Trip'
  const latestAssistantMessageId = [...messages].reverse().find(message => message.role === 'assistant')?.id
  const durationLabel = trip?.duration_days ? `${trip.duration_days} Days` : 'Dates TBD'
  const travelerCount = (trip?.travelers?.adults ?? 0) + (trip?.travelers?.children ?? 0) + (trip?.travelers?.seniors ?? 0)
  const travelerLabel = travelerCount > 0 ? `${travelerCount} ${travelerCount === 1 ? 'Traveler' : 'Travelers'}` : 'Travelers TBD'
  const budgetLabel = trip?.budget?.amount != null
    ? `${trip.budget.currency || 'INR'} ${Number(trip.budget.amount).toLocaleString('en-IN')}`
    : null
  const containerWidthClass = isCanvasOpen ? 'max-w-7xl' : 'max-w-5xl'

  if (isInitialTripLoading) return <CreateTripSkeleton />

  return (
    <div className={`${containerWidthClass} mx-auto px-4 sm:px-6 lg:px-8 py-5 min-h-[calc(100vh-4.5rem)] lg:h-[calc(100vh-4.5rem)] flex flex-col transition-all duration-300 print:p-0 print:m-0 print:h-auto print:max-w-full print:block`}>

      {/* ========================================================================= */}
      {/* LOGIN GUARD OVERLAY — shown when user is not authenticated                */}
      {/* ========================================================================= */}
      {!isAuthenticated && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-sm">
          <div className="bg-white rounded-3xl shadow-2xl p-8 sm:p-10 max-w-sm w-full mx-4 text-center">
            <div className="w-16 h-16 rounded-2xl bg-indigo-600 text-white flex items-center justify-center mx-auto mb-5 shadow-lg shadow-indigo-600/30">
              <Sparkles className="w-8 h-8" />
            </div>
            <h2 className="text-2xl font-black text-gray-900 mb-2">Sign in to Plan</h2>
            <p className="text-sm text-gray-500 mb-7 leading-relaxed">
              Create a free account to start planning your trip with AI. Your itineraries are saved to your profile.
            </p>
            <button
              onClick={openLogin}
              className="w-full flex items-center justify-center gap-2 px-6 py-3.5 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold rounded-2xl transition shadow-md shadow-indigo-600/25 cursor-pointer"
            >
              <LogIn className="w-4 h-4" />
              Sign in to Continue
            </button>
            <button
              onClick={() => navigate('/')}
              className="mt-3 w-full px-6 py-2.5 text-sm font-semibold text-gray-500 hover:text-gray-700 transition cursor-pointer"
            >
              ← Back to Home
            </button>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* TOP HUD: DYNAMIC TRIP IDENTITY & LIVE MEMORY CONSTRAINTS                  */}
      {/* ========================================================================= */}
      <div className="bg-white rounded-3xl p-4 sm:p-5 mb-5 border border-slate-200 shadow-xs flex flex-wrap items-center justify-between gap-4 print:hidden">
        <div className="flex items-center gap-3.5">
          <div className="w-10 h-10 rounded-2xl bg-indigo-600 text-white flex items-center justify-center shadow-md shadow-indigo-600/25 shrink-0">
            <Sparkles className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="font-black text-slate-900 text-base sm:text-lg tracking-tight">
                {trip?.name || 'Autonomous Trip Planner'}
              </h1>
              {trip?.slug && (
                <span className="text-[11px] font-mono font-bold text-slate-500 bg-slate-100 px-2 py-0.5 rounded-lg border border-slate-200/80">
                  #{trip.slug.slice(0, 8)}
                </span>
              )}
            </div>
            <p className="text-xs font-medium text-slate-500 mt-0.5 flex items-center gap-1.5">
              {isDone
                ? '✓ Itinerary draft • Review details before booking'
                : isPlanning
                ? '● Solving route & verified opening hours...'
                : '● Interactive Discovery with AI Riya'}
            </p>
          </div>
        </div>

        {/* Constraint Badges */}
        <div className="flex items-center gap-2.5 flex-wrap">
          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-50 border border-slate-200 text-slate-700 text-xs font-bold shadow-2xs">
            <MapPin className="w-3.5 h-3.5 text-indigo-600" />
            <span>{destinationName}</span>
          </div>

          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-50 border border-slate-200 text-slate-700 text-xs font-bold shadow-2xs">
            <Calendar className="w-3.5 h-3.5 text-indigo-600" />
            <span>{durationLabel}</span>
          </div>

          <div className="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-50 border border-slate-200 text-slate-700 text-xs font-bold shadow-2xs">
            <Users className="w-3.5 h-3.5 text-indigo-600" />
            <span>{travelerLabel}</span>
          </div>

          {budgetLabel && (
            <div className="hidden md:flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-indigo-50 border border-indigo-200 text-indigo-700 text-xs font-bold shadow-2xs">
              <Wallet className="w-3.5 h-3.5 text-indigo-600" />
              <span>{budgetLabel}</span>
            </div>
          )}

          <button
            onClick={() => setIsCanvasOpen(!isCanvasOpen)}
            disabled={isPlanning || isDone}
            className={`px-3.5 py-1.5 rounded-xl text-xs font-bold transition flex items-center gap-1.5 cursor-pointer border ${
              isCanvasOpen
                ? 'bg-indigo-50 text-indigo-700 border-indigo-200'
                : 'bg-white text-slate-700 border-slate-200 hover:bg-slate-50'
            } disabled:cursor-not-allowed disabled:opacity-60`}
          >
            <SlidersHorizontal className="w-3.5 h-3.5" />
            <span>{isCanvasOpen ? 'Hide Canvas' : 'View Canvas'}</span>
          </button>
        </div>
      </div>

      {/* ========================================================================= */}
      {/* MAIN SPLIT-SCREEN WORKSPACE                                               */}
      {/* ========================================================================= */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 flex-1 min-h-0 print:block print:w-full">
        {/* LEFT PANE: AI Chat Companion */}
        <div
          className={`${
            isCanvasOpen ? 'lg:col-span-5' : 'lg:col-span-12 w-full'
          } min-w-0 bg-white rounded-3xl border border-slate-200 shadow-sm flex flex-col h-full overflow-hidden transition-all duration-300 print:hidden`}
        >
          {/* Messages Stream */}
          <div className="flex-1 min-w-0 p-4 sm:p-6 overflow-y-auto space-y-4" onScroll={event => {
            const element = event.currentTarget
            followChatRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
          }}>
            {messages.map((m) => (
              <div key={m.id} className={`flex gap-3 ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                {m.role === 'assistant' && (
                  <div className="w-7 h-7 rounded-xl bg-indigo-600 text-white flex items-center justify-center text-xs font-black shrink-0 shadow-xs mt-0.5">
                    R
                  </div>
                )}
                <div
                  className={`min-w-0 max-w-[88%] break-words rounded-2xl px-4 py-3 text-xs sm:text-sm leading-relaxed ${
                    m.role === 'user'
                      ? 'bg-slate-900 text-white rounded-tr-xs shadow-md'
                      : 'bg-slate-100/80 text-slate-800 rounded-tl-xs border border-slate-200/60'
                  }`}
                >
                  <p className="whitespace-pre-wrap">{m.content || m.text}</p>

                  {/* Confirmation Gate Card */}
                  {m.showConfirmationCard && trip?.status === 'awaiting_confirmation'
                    && m.id === latestAssistantMessageId
                    && !isDone && !isPlanning && (
                    <div className="mt-3.5 bg-white rounded-2xl p-4 border border-indigo-100 shadow-sm text-slate-900 text-left">
                      <div className="flex items-center justify-between pb-2 mb-3 border-b border-slate-100">
                        <span className="text-[11px] font-black uppercase tracking-wider text-indigo-700 flex items-center gap-1.5">
                          <CheckCircle2 className="w-3.5 h-3.5 text-indigo-600" />
                          Trip Parameters Snapshot
                        </span>
                        <span className="text-[10px] font-bold text-emerald-600 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-md">
                          Details ready to plan
                        </span>
                      </div>

                      {m.snapshot && (
                        <div className="grid grid-cols-2 gap-2 text-xs mb-3.5">
                          {[
                            ['Destination', m.snapshot.destination],
                            ['Duration', m.snapshot.duration],
                            ['Party Size', m.snapshot.travelers],
                            ['Budget', m.snapshot.budget],
                          ].filter(([, value]) => Boolean(value)).map(([label, value]) => (
                            <div key={label} className="bg-slate-50 p-2 rounded-xl border border-slate-100">
                              <p className="text-[10px] text-slate-400 font-medium">{label}</p>
                              <p className="font-bold text-slate-800 truncate">{value}</p>
                            </div>
                          ))}
                        </div>
                      )}

                      <button
                        onClick={triggerPlanning}
                        disabled={busy}
                        className="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-xs sm:text-sm rounded-xl shadow-md shadow-indigo-600/25 transition flex items-center justify-center gap-2 cursor-pointer"
                      >
                        <Zap className="w-4 h-4 text-amber-400" />
                        <span>Start planning</span>
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}

            {busy && thinkingStep && (
              <div role="status" aria-live="polite" className="flex items-center gap-2 text-xs font-bold text-indigo-600 bg-indigo-50/80 border border-indigo-100 px-3.5 py-2 rounded-xl max-w-sm animate-pulse ml-10 shadow-2xs">
                <RefreshCw className="w-3.5 h-3.5 animate-spin text-indigo-600" />
                <span>
                  {thinkingStep.replace(/[.…]+$/, '')}
                  <span aria-hidden="true">{processingDots}</span>
                </span>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Suggestions */}
          {!isDone && currentSuggestions.length > 0 && (
            <div className="p-3 border-t border-slate-100 bg-slate-50/50">
              <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">
                💡 Suggested Replies:
              </p>
              <div className="flex flex-wrap gap-1.5">
                {currentSuggestions.map((sug, idx) => (
                  <button
                    key={idx}
                    onClick={() => handleSuggestionClick(sug)}
                    disabled={busy || isComposerLocked}
                    className="text-left text-xs bg-white hover:bg-indigo-50 hover:text-indigo-700 hover:border-indigo-300 text-slate-700 px-3 py-1.5 rounded-xl border border-slate-200 transition shadow-2xs cursor-pointer font-medium disabled:opacity-50"
                  >
                    {sug}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Input Area */}
          <div className="p-3.5 sm:p-4 border-t border-slate-200 bg-white">
            {isDone ? (
              <div className="p-3.5 rounded-2xl bg-emerald-50 border border-emerald-200 text-emerald-850 text-xs font-bold flex items-center justify-center gap-2 text-center">
                <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
                <span>{plan?.deliveryStatus === 'DRAFT_NEEDS_CONFIRMATION'
                  ? 'Draft available — Review unresolved travel and visit details on the canvas.'
                  : 'Itinerary available — Review your route and schedule before booking.'}</span>
              </div>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  handleSendMessage()
                }}
                className="flex items-center gap-2"
              >
                <input
                  ref={inputRef}
                  type="text"
                  aria-label="Trip requirements message"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={
                    !isAuthenticated
                      ? 'Type your trip request (Sign-in required to plan)...'
                      : isComposerLocked
                      ? 'Planning is in progress. Chat is locked for this trip.'
                      : 'Describe what you want for this trip...'
                  }
                  disabled={isComposerLocked}
                  className="flex-1 min-w-0 rounded-xl border border-slate-300 px-4 py-2.5 text-xs sm:text-sm text-slate-900 focus:ring-2 focus:ring-indigo-500 focus:outline-none disabled:bg-slate-50"
                />
                <button
                  type="submit"
                  aria-label="Send message"
                  disabled={!input.trim() || busy || isComposerLocked}
                  className="px-4 py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-xl shadow-md shadow-indigo-600/20 transition flex items-center justify-center cursor-pointer"
                  title={!isAuthenticated ? 'Sign in with Google to send message' : 'Send message'}
                >
                  {!isAuthenticated ? <Lock className="w-4 h-4" /> : <Send className="w-4 h-4" />}
                </button>
              </form>
            )}
          </div>
        </div>

        {/* RIGHT PANE: Visual Canvas */}
        {isCanvasOpen && (
          <div className="lg:col-span-7 bg-white rounded-3xl border border-slate-200 shadow-sm flex flex-col h-full overflow-hidden transition-all duration-300 print:block print:w-full print:col-span-12 print:border-none print:shadow-none print:p-0">
            {/* STATE 1: Discovery */}
            {hasPlanningFailure && (
              <div className="flex-1 p-6 sm:p-8 flex flex-col justify-center items-center text-center overflow-y-auto">
                <div className="w-14 h-14 rounded-2xl bg-rose-50 text-rose-600 flex items-center justify-center mb-4 border border-rose-100">
                  <AlertCircle className="w-7 h-7" />
                </div>
                <h3 className="text-xl font-black text-slate-900 mb-2">Planning needs your attention</h3>
                <p className="max-w-md text-sm text-slate-600 leading-relaxed mb-6">{planningFailureMessage}</p>
                <button onClick={triggerPlanning} className="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold shadow-md transition cursor-pointer">
                  Retry Planning
                </button>
              </div>
            )}

            {!hasPlanningFailure && !showPlanningCanvas && !isDone && (
              <div className="flex-1 p-6 sm:p-8 flex flex-col justify-between overflow-y-auto">
                <div className="text-center max-w-md mx-auto">
                  <div className="w-14 h-14 rounded-2xl bg-indigo-50 text-indigo-600 flex items-center justify-center mx-auto mb-4 shadow-xs border border-indigo-100">
                    <Compass className="w-7 h-7 animate-pulse" />
                  </div>
                  <h3 className="text-xl font-black text-slate-900 mb-1.5">Interactive Itinerary Canvas</h3>
                  <p className="text-slate-500 text-xs sm:text-sm leading-relaxed mb-6">
                    As you chat with Riya, this canvas compiles your day-by-day timeline, driving routes, and verified stay accommodations.
                  </p>

                  <div className="bg-slate-50 border border-slate-200/90 rounded-2xl p-4 text-left shadow-2xs mb-6">
                    <div className="flex items-center justify-between mb-3 pb-2 border-b border-slate-200">
                      <span className="text-[11px] font-extrabold text-slate-700 uppercase tracking-wider flex items-center gap-1.5">
                        <Layers className="w-3.5 h-3.5 text-indigo-600" />
                        Live Extracted Parameters
                      </span>
                      <span className="text-[10px] font-bold text-indigo-600 bg-indigo-50 border border-indigo-100 px-2 py-0.5 rounded-md">
                        {trip?.name ? 'Brief In Progress' : 'Awaiting Input'}
                      </span>
                    </div>

                    <div className="flex flex-wrap gap-1.5">
                      {trip?.name && (
                        <span className="px-2.5 py-1 bg-white text-indigo-900 font-bold text-xs rounded-lg border border-indigo-100 shadow-2xs">
                          📍 {trip.name}
                        </span>
                      )}
                      {trip?.duration_days && (
                        <span className="px-2.5 py-1 bg-white text-indigo-900 font-bold text-xs rounded-lg border border-indigo-100 shadow-2xs">
                          📅 {trip.duration_days} Days
                        </span>
                      )}
                      {trip?.travelers?.adults && (
                        <span className="px-2.5 py-1 bg-white text-indigo-900 font-bold text-xs rounded-lg border border-indigo-100 shadow-2xs">
                          👥 {trip.travelers.adults} Adults
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <div className="pt-4 border-t border-slate-100 text-center">
                  <p className="text-[11px] text-slate-400 font-medium">
                    ● Complete brief in chat ➔ Click "Launch Planner" on Riya's confirmation card
                  </p>
                </div>
              </div>
            )}

            {/* STATE 2: Planning Solver Running */}
            {showPlanningCanvas && (
              <div className="flex-1 p-6 sm:p-8 flex flex-col justify-between overflow-y-auto">
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-amber-50 border border-amber-200 text-amber-800 text-xs font-bold">
                      <span className="w-2 h-2 rounded-full bg-amber-500 animate-ping" />
                      <span>Planning Engine Active</span>
                    </div>
                    <span className="text-xs font-mono font-bold text-slate-400">
                      {job?.completed_tasks ?? 0} of {job?.total_tasks ?? PLANNING_STEPS.length} planning steps complete
                    </span>
                  </div>

                  <h3 className="text-xl sm:text-2xl font-black text-slate-900 mb-2">
                    {`Building your ${destinationName} itinerary`}
                  </h3>
                  <p className="text-xs sm:text-sm font-semibold text-indigo-600 mb-6">
                    {job?.current_step || 'Preparing your confirmed trip details...'}
                  </p>

                  <div className="w-full bg-slate-100 rounded-full h-3 mb-6 overflow-hidden border border-slate-200">
                    <div
                      className="bg-gradient-to-r from-indigo-600 via-indigo-700 to-violet-600 h-3 rounded-full transition-all duration-500"
                      style={{ width: `${job?.progress_percent ?? 0}%` }}
                    />
                  </div>
                </div>

                <section className="mt-4 rounded-2xl border border-slate-200 bg-slate-50 p-4 sm:p-5" aria-live="polite">
                  <div className="relative ml-1">
                    <div className="absolute left-[13px] top-4 bottom-4 w-px bg-slate-200" aria-hidden="true" />
                    <div className="space-y-1.5">
                      {PLANNING_STEPS.map((step, index) => {
                        const currentStageIndex = Math.max(0, PLANNING_STEPS.findIndex((item) => item.id === job?.current_stage))
                        const completedCount = job?.completed_tasks ?? 0
                        const isComplete = index < completedCount || (index === currentStageIndex && job?.progress_kind === 'stage_completed')
                        const isCurrent = !isComplete && index === currentStageIndex && completedCount < PLANNING_STEPS.length
                        const description = job?.activity_detail || step.description
                        return (
                          <div key={step.id} className={`relative flex items-start gap-3 rounded-xl px-2 py-1.5 transition-all duration-300 ${isCurrent ? 'bg-indigo-50 border border-indigo-100 py-2.5 shadow-2xs' : 'border border-transparent'}`}>
                            <div className={`relative z-10 mt-0.5 h-7 w-7 rounded-full flex items-center justify-center text-[11px] font-black shrink-0 border ${
                              isComplete
                                ? 'bg-emerald-500 border-emerald-500 text-white'
                                : isCurrent
                                ? 'bg-indigo-600 border-indigo-600 text-white shadow-sm shadow-indigo-600/30'
                                : 'bg-white border-slate-300 text-slate-400'
                            }`}>
                              {isComplete ? <CheckCircle2 className="w-4 h-4" /> : isCurrent ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : index + 1}
                            </div>
                            <div className="min-w-0 pt-0.5">
                              <p className={`text-xs sm:text-sm font-bold ${isCurrent ? 'text-indigo-700' : isComplete ? 'text-slate-800' : 'text-slate-400'}`}>{step.label}</p>
                              {isCurrent && <p className="mt-0.5 text-[11px] sm:text-xs leading-relaxed text-indigo-600">{description}</p>}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                  <p className="mt-4 border-t border-slate-200 pt-3 text-[11px] leading-relaxed text-slate-500 text-center">
                    You can close this page — planning continues safely in the background.
                  </p>
                </section>
              </div>
            )}

            {/* STATE 3: Finalized Master Itinerary Canvas */}
            {isDone && plan && (
              <div className="flex-1 flex flex-col h-full overflow-hidden">
                {/* Header Banner */}
                <div className="p-4 sm:p-5 border-b border-slate-200 bg-gradient-to-r from-indigo-600 to-violet-700 text-white flex flex-wrap items-center justify-between gap-4 shrink-0 print:bg-indigo-700 print:text-white print:rounded-2xl print:mb-4">
                  <div>
                    <h2 className="text-lg sm:text-xl font-black">{plan.destination} Itinerary Draft</h2>
                    <p className="text-xs text-indigo-100 mt-0.5">
                      {plan.durationDays} Days • {plan.travelers?.adults ?? 0} Adults • Trip budget: {plan.currency} {plan.budgetTotal}
                    </p>
                  </div>

                  <div className="flex items-center gap-2 print:hidden">
                    <button
                      onClick={() => window.print()}
                      className="px-3.5 py-1.5 rounded-xl bg-white/20 hover:bg-white/30 text-white text-xs font-bold transition flex items-center gap-1.5 cursor-pointer"
                    >
                      <Download className="w-3.5 h-3.5" /> Export PDF
                    </button>
                  </div>
                </div>

                {plan.deliveryStatus === 'adjusted_draft' && (
                  <div className="px-4 sm:px-5 py-3 border-b border-amber-200 bg-amber-50 text-amber-950 text-xs sm:text-sm leading-relaxed print:hidden">
                    <span className="font-black">Adjusted plan:</span> Some details could not be verified automatically. Use this itinerary as a guide and confirm routes, opening hours, availability and prices before booking.
                  </div>
                )}

                {(plan.alerts.length > 0 || plan.budgetHealth.withinBudget !== undefined) && (
                  <div className="px-4 sm:px-5 py-3 border-b border-slate-200 bg-slate-50 space-y-2">
                    {plan.budgetHealth.withinBudget !== undefined && (
                      <div className={`text-xs font-bold rounded-xl px-3 py-2 border ${
                        plan.budgetHealth.withinBudget
                          ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
                          : 'bg-amber-50 text-amber-900 border-amber-200'
                      }`}>
                        {plan.budgetHealth.withinBudget ? 'Budget check: this itinerary is within your target.' : 'Budget check: this itinerary may exceed your target.'}
                      </div>
                    )}
                    {plan.alerts.filter((alert, index) => alert.severity === 'error' || index === 0).map((alert, index) => (
                      <div key={`${alert.message}-${index}`} className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2">
                        {alert.message}
                      </div>
                    ))}
                    {plan.alerts.some((alert, index) => alert.severity !== 'error' && index > 0) && (
                      <details className="text-xs text-amber-900">
                        <summary className="cursor-pointer font-semibold">More planning notes &amp; confirmations</summary>
                        <ul className="mt-2 space-y-2 max-h-48 overflow-y-auto">
                          {plan.alerts.filter((alert, index) => alert.severity !== 'error' && index > 0).map((alert, index) => <li key={`${index}-${alert.message}`}>{alert.message}</li>)}
                        </ul>
                      </details>
                    )}
                  </div>
                )}

                <div className="px-4 sm:px-5 py-2.5 border-b border-indigo-100 bg-indigo-50 text-xs text-indigo-900">
                  <span className="font-bold">Before booking:</span> This AI-generated itinerary is a planning draft. Confirm routes, prices, opening hours, and reservations independently.
                </div>
                {plan.displayRefreshRequired && Boolean(plan.placeIds?.length) && (
                  <div className="px-4 sm:px-5 py-3 border-b border-slate-200 text-xs text-slate-700 print:hidden">
                    <p>Saved place details have expired. Refresh up to ten selected places; no itinerary generation or photo downloads.</p>
                    <button type="button" onClick={refreshPlanDisplay} disabled={refreshingDisplay}
                      className="mt-2 inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 font-semibold disabled:opacity-50">
                      <RefreshCw className={`h-3.5 w-3.5 ${refreshingDisplay ? 'animate-spin' : ''}`} />
                      {refreshingDisplay ? 'Refreshing place details…' : 'Refresh place details'}
                    </button>
                    {displayError && <p role="alert" className="mt-2 text-amber-800">{displayError}</p>}
                  </div>
                )}

                {plan.summary && <p className="border-b border-slate-200 px-4 py-4 text-sm leading-relaxed text-slate-700 sm:px-5">{plan.summary}</p>}

                {/* 4 Interactive Navigation Tabs */}
                <div className="flex border-b border-slate-200 bg-slate-50 px-3 sm:px-4 shrink-0 overflow-x-auto print:hidden">
                  <button
                    onClick={() => setActiveTab('timeline')}
                    className={`py-3 px-3.5 text-xs font-bold border-b-2 transition flex items-center gap-1.5 cursor-pointer shrink-0 ${
                      activeTab === 'timeline'
                        ? 'border-indigo-600 text-indigo-700 bg-white'
                        : 'border-transparent text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    <Calendar className="w-3.5 h-3.5" /> Timeline
                  </button>
                  <button
                    onClick={() => setActiveTab('map')}
                    className={`py-3 px-3.5 text-xs font-bold border-b-2 transition flex items-center gap-1.5 cursor-pointer shrink-0 ${
                      activeTab === 'map'
                        ? 'border-indigo-600 text-indigo-700 bg-white'
                        : 'border-transparent text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    <Map className="w-3.5 h-3.5" /> Map & Stops
                  </button>
                  <button
                    onClick={() => setActiveTab('stays')}
                    className={`py-3 px-3.5 text-xs font-bold border-b-2 transition flex items-center gap-1.5 cursor-pointer shrink-0 ${
                      activeTab === 'stays'
                        ? 'border-indigo-600 text-indigo-700 bg-white'
                        : 'border-transparent text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    <Hotel className="w-3.5 h-3.5" /> Stays & Dining
                  </button>
                  <button
                    onClick={() => setActiveTab('tips')}
                    className={`py-3 px-3.5 text-xs font-bold border-b-2 transition flex items-center gap-1.5 cursor-pointer shrink-0 ${
                      activeTab === 'tips'
                        ? 'border-indigo-600 text-indigo-700 bg-white'
                        : 'border-transparent text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    <Info className="w-3.5 h-3.5" /> Tips & Packing
                  </button>
                </div>

                {/* Tab Contents & Printable Document */}
                <div className="flex-1 p-4 sm:p-6 overflow-y-auto print:p-0 print:overflow-visible space-y-6">
                  {/* SECTION 1: TIMELINE */}
                  <div className={`${activeTab === 'timeline' ? 'block' : 'hidden'} print:block space-y-6 print:mb-8`}>
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
                                {act.hasPlace !== false && <img
                                  src={act.imageUrl || placeholderCover(act.title)}
                                  alt={act.imageUrl ? act.title : ''}
                                  className="w-16 h-16 sm:w-20 sm:h-20 rounded-xl object-cover shrink-0 border border-slate-100 shadow-2xs"
                                />}

                                <div className="flex-1 min-w-0">
                                  <div className="flex flex-wrap items-center justify-between gap-2 mb-1">
                                    <div className="flex items-center gap-2 flex-wrap">
                                      <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 font-mono font-bold text-xs">
                                        {act.time}
                                      </span>
                                      <h4 className="font-bold text-xs sm:text-sm text-slate-900 truncate">
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

                  {plan.budgetTargets && activeTab === 'timeline' && (
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
                      {plan.stays.map((stay, idx) => (
                        <div
                          key={idx}
                          className="bg-white rounded-3xl border border-slate-200 overflow-hidden shadow-xs hover:border-indigo-300 transition group print:border-slate-300 print:break-inside-avoid"
                        >
                          <div className="relative h-40 overflow-hidden">
                            <img
                              src={stay.image}
                              alt={stay.name}
                              className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                            />
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

                    {(Object.keys(plan.budgetHealth.categories).length > 0 || plan.planningNotes.length > 0 || plan.sources.length > 0) && (
                      <details className="rounded-2xl border border-slate-200 bg-white p-4 print:hidden">
                        <summary className="cursor-pointer text-sm font-bold text-slate-800">Trip details & planning notes</summary>
                        <div className="mt-4 space-y-4 text-xs text-slate-600">
                          {Object.keys(plan.budgetHealth.categories).length > 0 && (
                            <div>
                              <h4 className="font-bold text-slate-800 mb-2">Budget breakdown</h4>
                              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                {Object.entries(plan.budgetHealth.categories).map(([category, value]) => (
                                  <div key={category} className="rounded-xl bg-slate-50 px-3 py-2 border border-slate-100 flex justify-between gap-3">
                                    <span className="capitalize">{category.replace(/_/g, ' ')}</span><span className="font-semibold text-slate-800">{value}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {plan.planningNotes.length > 0 && (
                            <div>
                              <h4 className="font-bold text-slate-800 mb-2">Things to know</h4>
                              <ul className="space-y-2">{plan.planningNotes.map((note, index) => <li key={index} className="flex gap-2"><span className="text-indigo-600">•</span><span>{note}</span></li>)}</ul>
                            </div>
                          )}
                          {plan.sources.length > 0 && (
                            <div>
                              <h4 className="font-bold text-slate-800 mb-2">Helpful sources</h4>
                              <div className="flex flex-wrap gap-2">{plan.sources.map((source, index) => <a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer" className="text-indigo-700 hover:underline font-semibold">{source.title}</a>)}</div>
                            </div>
                          )}
                        </div>
                      </details>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default CreateTrip
