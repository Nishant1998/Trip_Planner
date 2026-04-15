import React, { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import SampleTripCards from '../components/SampleTripCards'
import {
  Sparkles,
  Compass,
  ArrowRight,
  CheckCircle2,
  Route,
  Bot,
  Clock,
  ChevronDown,
} from 'lucide-react'

const Landing: React.FC = () => {
  const { hash } = useLocation()
  useEffect(() => {
    if (hash === '#sample-trips') document.getElementById('sample-trips')?.scrollIntoView()
    else if (!hash) window.scrollTo(0, 0)
  }, [hash])
  const scrollToHowItWorks = () => {
    const el = document.getElementById('how-it-works')
    if (el) {
      el.scrollIntoView({ behavior: 'smooth' })
    }
  }

  return (
    <div className="min-h-[calc(100vh-4rem)] flex flex-col justify-between bg-gradient-to-b from-slate-50/60 via-white to-slate-50/80 text-slate-900">
      {/* ========================================================================= */}
      {/* 1. HERO SECTION (High-Impact Value Prop + Realistic AI Chat Simulator)    */}
      {/* ========================================================================= */}
      <section className="relative isolate px-4 pt-12 sm:pt-16 pb-16 sm:pb-20 overflow-hidden">
        {/* Ambient Radial Mesh Background */}
        <div className="absolute inset-x-0 top-0 -z-10 h-[580px] bg-[radial-gradient(ellipse_at_20%_20%,rgba(99,102,241,0.15),transparent_40%),radial-gradient(ellipse_at_80%_15%,rgba(139,92,246,0.12),transparent_35%),linear-gradient(180deg,#ffffff,rgba(248,250,252,0))]" />

        <div className="max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-[1.05fr_0.95fr] items-center gap-12 lg:gap-16">
          {/* Left Column: Headline, Value Proposition, & Clear CTAs */}
          <div className="text-center lg:text-left">
            {/* Architecture Pill Badge */}
            <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-indigo-50 border border-indigo-200/80 text-indigo-700 text-xs font-bold uppercase tracking-wider mb-6 shadow-2xs">
              <Sparkles className="w-3.5 h-3.5 text-indigo-600" />
              <span>Conversational AI • Day-by-day travel planning</span>
            </div>

            <h1 className="text-4xl sm:text-5xl lg:text-6xl font-black text-slate-900 tracking-tight leading-[1.12] mb-6">
              Turn your travel idea into{' '}
              <span className="bg-gradient-to-r from-indigo-600 via-indigo-700 to-violet-600 bg-clip-text text-transparent">
                a day-by-day plan.
              </span>
            </h1>

            <p className="text-base sm:text-lg text-slate-600 max-w-xl mx-auto lg:mx-0 mb-10 leading-relaxed">
              Tell Riya your dates, budget and interests. Explore a day-by-day draft with places to visit,
              travel options and{' '}<strong className="text-slate-900 font-semibold">details to confirm before booking</strong>.
            </p>

            {/* Clean Primary Action Buttons */}
            <div className="flex flex-col sm:flex-row items-center justify-center lg:justify-start gap-4">
              <Link
                to="/create"
                className="w-full sm:w-auto px-8 py-4 bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-sm rounded-full shadow-lg shadow-indigo-600/25 hover:shadow-indigo-600/35 hover:scale-[1.02] active:scale-[0.98] transition flex items-center justify-center gap-2 group cursor-pointer"
              >
                <Sparkles className="w-4 h-4 text-indigo-200 group-hover:rotate-12 transition-transform" />
                <span>Plan a Trip with AI</span>
                <ArrowRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" />
              </Link>

              <button
                onClick={scrollToHowItWorks}
                className="w-full sm:w-auto px-6 py-4 bg-white hover:bg-slate-50 text-slate-800 font-bold text-sm rounded-full border border-slate-200/90 shadow-2xs transition flex items-center justify-center gap-2 hover:border-slate-300 cursor-pointer"
              >
                <span>How It Works</span>
                <ChevronDown className="w-4 h-4 text-slate-500" />
              </button>
            </div>
          </div>

          {/* Right Column: Realistic Live AI Agent Chat Simulator */}
          <div className="relative mx-auto w-full max-w-lg lg:max-w-none">
            {/* Ambient Glow */}
            <div className="absolute -inset-4 bg-gradient-to-tr from-indigo-500/20 to-violet-500/20 rounded-3xl blur-2xl -z-10" />

            <div className="bg-white rounded-3xl border border-slate-200/90 shadow-2xl overflow-hidden transition-all duration-300">
              {/* Agent Card Header */}
              <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4 bg-slate-50/70">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-2xl bg-gradient-to-tr from-indigo-600 to-violet-600 text-white flex items-center justify-center font-black text-sm shadow-md shadow-indigo-500/20">
                    R
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-black text-slate-900">Riya</p>
                      <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-100">
                        Lead Travel Agent
                      </span>
                    </div>
                    <p className="text-xs text-emerald-600 flex items-center gap-1 font-semibold mt-0.5">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                      Example Conversation
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-1.5 text-xs font-bold text-slate-400 bg-white border border-slate-200 rounded-lg px-2.5 py-1 shadow-2xs">
                  <Clock className="w-3 h-3 text-slate-400" />
                  <span>Preview</span>
                </div>
              </div>

              {/* Chat Simulation Stream (Chronologically Realistic) */}
              <div className="p-5 space-y-3.5 bg-slate-50/30 text-xs sm:text-sm">
                {/* User Message 1 */}
                <div className="ml-auto max-w-[85%] rounded-2xl rounded-tr-sm bg-slate-900 p-4 text-white leading-relaxed shadow-md">
                  Hey Riya! I want to plan a 4-day trip to Goa for 2 adults with beachfront stays, a sunset cruise, and seafood dining under ₹50,000.
                </div>

                {/* Assistant Message 1 */}
                <div className="max-w-[90%] rounded-2xl rounded-tl-sm bg-white border border-slate-200/80 p-4 text-slate-700 leading-relaxed shadow-xs">
                  That sounds like a wonderful getaway! I've noted <strong className="text-slate-900">Goa, India</strong> for <strong className="text-slate-900">4 Days (2 Adults)</strong>, a beachfront stay preference and coastal dining within a <strong className="text-slate-900">₹50k budget</strong>.
                  <p className="mt-2 text-indigo-700 font-semibold">
                    Would you prefer North Goa (lively cafes & heritage) or South Goa (peaceful beaches)?
                  </p>
                </div>

                {/* Extracted Structured Constraints Brief (TriFlow Architecture) */}
                <div className="rounded-2xl border border-indigo-100 bg-indigo-50/70 p-3.5 shadow-2xs">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-[11px] font-black uppercase tracking-wider text-indigo-700 flex items-center gap-1.5">
                      <CheckCircle2 className="w-3.5 h-3.5 text-indigo-600" />
                      Extracted Constraints Brief
                    </p>
                    <span className="text-[10px] font-bold text-indigo-600">Example brief</span>
                  </div>

                  <div className="flex flex-wrap gap-1.5">
                    {['📍 Goa, India', '📅 4 Days', '👥 2 Adults', '👛 ₹50k Budget', '🏖️ Beachfront'].map((tag, idx) => (
                      <span
                        key={idx}
                        className="px-2.5 py-1 bg-white text-indigo-900 font-bold text-xs rounded-lg border border-indigo-100 shadow-2xs"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              </div>


            </div>
          </div>
        </div>
      </section>

      <SampleTripCards />

      {/* ========================================================================= */}
      {/* 2. HOW IT WORKS / ARCHITECTURE SECTION (Dual-Audience: User + Recruiter)  */}
      {/* ========================================================================= */}
      <section id="how-it-works" className="scroll-mt-16 border-y border-slate-200/80 bg-white py-16 sm:py-20 px-4 sm:px-6 lg:px-8">
        <div className="max-w-7xl mx-auto">
          <div className="text-center max-w-3xl mx-auto mb-14">
            <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-100 text-slate-700 text-xs font-bold uppercase tracking-wider mb-3">
              <Compass className="w-3.5 h-3.5 text-indigo-600" />
              <span>How Waypoint Works</span>
            </div>
            <h2 className="text-2xl sm:text-3xl lg:text-4xl font-black text-slate-900 tracking-tight">
              From a conversation to a clearer trip plan
            </h2>
            <p className="text-slate-500 text-sm sm:text-base mt-2 leading-relaxed">
              Collect your requirements, explore places and travel options, and review the details before you book.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-6 sm:gap-8">
            {/* Step 1 */}
            <div className="p-8 rounded-3xl bg-slate-50 border border-slate-200/80 hover:border-indigo-300 hover:shadow-md transition-all duration-300 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between mb-5">
                  <span className="text-xs font-black tracking-widest text-indigo-600 uppercase">Step 01</span>
                  <div className="w-10 h-10 rounded-2xl bg-indigo-100 text-indigo-700 flex items-center justify-center shadow-2xs">
                    <Bot className="w-5 h-5" />
                  </div>
                </div>
                <h3 className="text-lg font-black text-slate-900 mb-2">
                  Tell us what you want in plain English
                </h3>
                <p className="text-sm text-slate-600 leading-relaxed">
                  No exhausting dropdown forms. Your travel assistant converses naturally to understand your destination, dates, budget, travel party, and desired vibe.
                </p>
              </div>
              <div className="mt-6 pt-4 border-t border-slate-200/60 flex items-center gap-1.5 text-xs font-bold text-indigo-700">
                <span className="px-2 py-0.5 rounded-md bg-indigo-100/70 border border-indigo-200/50">
                  ✦ Multi-Agent Intent Extraction
                </span>
              </div>
            </div>

            {/* Step 2 */}
            <div className="p-8 rounded-3xl bg-slate-50 border border-slate-200/80 hover:border-emerald-300 hover:shadow-md transition-all duration-300 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between mb-5">
                  <span className="text-xs font-black tracking-widest text-emerald-600 uppercase">Step 02</span>
                  <div className="w-10 h-10 rounded-2xl bg-emerald-100 text-emerald-700 flex items-center justify-center shadow-2xs">
                    <Route className="w-5 h-5" />
                  </div>
                </div>
                <h3 className="text-lg font-black text-slate-900 mb-2">
                  Explore places and travel options
                </h3>
                <p className="text-sm text-slate-600 leading-relaxed">
                  The planner researches places and route options where provider data is available. Missing prices, opening hours and travel details remain clearly marked for confirmation.
                </p>
              </div>
              <div className="mt-6 pt-4 border-t border-slate-200/60 flex items-center gap-1.5 text-xs font-bold text-emerald-700">
                <span className="px-2 py-0.5 rounded-md bg-emerald-100/70 border border-emerald-200/50">
                  ✦ Places & Route Research
                </span>
              </div>
            </div>

            {/* Step 3 */}
            <div className="p-8 rounded-3xl bg-slate-50 border border-slate-200/80 hover:border-violet-300 hover:shadow-md transition-all duration-300 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between mb-5">
                  <span className="text-xs font-black tracking-widest text-violet-600 uppercase">Step 03</span>
                  <div className="w-10 h-10 rounded-2xl bg-violet-100 text-violet-700 flex items-center justify-center shadow-2xs">
                    <CheckCircle2 className="w-5 h-5" />
                  </div>
                </div>
                <h3 className="text-lg font-black text-slate-900 mb-2">
                  A day-by-day draft you can review
                </h3>
                <p className="text-sm text-slate-600 leading-relaxed">
                  Review your itinerary, stay preferences, route links and budget scope. Confirm bookings, current prices and availability before travelling.
                </p>
              </div>
              <div className="mt-6 pt-4 border-t border-slate-200/60 flex items-center gap-1.5 text-xs font-bold text-violet-700">
                <span className="px-2 py-0.5 rounded-md bg-violet-100/70 border border-violet-200/50">
                  ✦ Evidence Gate & Neon Cloud DB
                </span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Final call to action */}
      <section className="px-4 sm:px-6 lg:px-8 pb-16 max-w-7xl mx-auto w-full">
        <div className="bg-slate-950 rounded-[32px] p-8 sm:p-14 text-center text-white relative overflow-hidden shadow-2xl">
          {/* Ambient Glow */}
          <div className="absolute -top-24 left-1/2 -translate-x-1/2 w-96 h-96 bg-indigo-600/20 rounded-full blur-3xl pointer-events-none" />

          <div className="relative z-10 max-w-2xl mx-auto">
            <div className="w-12 h-12 rounded-2xl bg-indigo-500/20 border border-indigo-400/30 text-indigo-400 flex items-center justify-center mx-auto mb-5">
              <Compass className="w-6 h-6 animate-pulse" />
            </div>

            <h2 className="text-3xl sm:text-4xl font-black tracking-tight mb-4">
              Ready to plan your next journey?
            </h2>

            <p className="text-slate-400 text-sm sm:text-base leading-relaxed mb-8">
              Start with a single sentence. Waypoint helps organise your requirements into a day-by-day draft.
            </p>

            <div className="flex items-center justify-center">
              <Link
                to="/create"
                className="px-8 py-4 bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-sm rounded-full shadow-lg shadow-indigo-600/30 transition hover:scale-[1.02] active:scale-[0.98] cursor-pointer inline-flex items-center gap-2"
              >
                <Sparkles className="w-4 h-4" />
                <span>Start Planning with AI →</span>
              </Link>
            </div>
          </div>
        </div>
      </section>
    </div>
  )
}

export default Landing
