import React, { useEffect, useState } from 'react'
import {
  Mail,
  Save,
  CheckCircle2,
  Sparkles,
  Lock,
  Loader2,
  Edit,
  XCircle,
} from 'lucide-react'
import { useAuth, UserProfile } from '../context/AuthContext'
import DemoUsageCard from '../components/DemoUsageCard'

const RESERVED_USERNAMES = new Set([
  'admin',
  'administrator',
  'root',
  'system',
  'support',
  'help',
  'api',
  'auth',
  'login',
  'logout',
  'dashboard',
  'settings',
  'profile',
  'null',
  'undefined',
  'create',
  'trip',
  'trips',
  'user',
  'guest',
  'waypoint',
])

export function validateUsername(username: string): { valid: boolean; error?: string } {
  const trimmed = username.trim()

  if (trimmed.length < 3) {
    return { valid: false, error: 'Username must be at least 3 characters long.' }
  }
  if (trimmed.length > 30) {
    return { valid: false, error: 'Username cannot exceed 30 characters.' }
  }
  if (!/^[a-zA-Z]/.test(trimmed)) {
    return { valid: false, error: 'Username must start with a letter (cannot start with a number or underscore).' }
  }
  if (!/^[a-zA-Z0-9_]+$/.test(trimmed)) {
    return { valid: false, error: 'Username can only contain letters, numbers, and underscores (no spaces).' }
  }
  if (trimmed.endsWith('_')) {
    return { valid: false, error: 'Username cannot end with an underscore.' }
  }
  if (trimmed.includes('__')) {
    return { valid: false, error: 'Username cannot contain consecutive underscores (__).' }
  }
  if (RESERVED_USERNAMES.has(trimmed.toLowerCase())) {
    return { valid: false, error: 'This username is reserved and cannot be used.' }
  }

  return { valid: true }
}

const COUNTRY_CODES = [
  { code: '+91', label: '+91 (India)' },
  { code: '+1', label: '+1 (USA / Canada)' },
  { code: '+44', label: '+44 (UK)' },
  { code: '+971', label: '+971 (UAE)' },
  { code: '+61', label: '+61 (Australia)' },
  { code: '+65', label: '+65 (Singapore)' },
  { code: '+49', label: '+49 (Germany)' },
  { code: '+33', label: '+33 (France)' },
  { code: '+81', label: '+81 (Japan)' },
]

const Profile: React.FC = () => {
  const { user, isAuthenticated, openLogin, updateProfile } = useAuth()

  const [data, setData] = useState<UserProfile | null>(user)
  const [editMode, setEditMode] = useState(false)
  const [draft, setDraft] = useState<Partial<UserProfile>>({})
  const [phoneCountryCode, setPhoneCountryCode] = useState('+91')
  const [phoneNationalNumber, setPhoneNationalNumber] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  useEffect(() => {
    if (user) {
      setData(user)
      if (user.phone_number) {
        const parts = user.phone_number.split(' ')
        if (parts.length >= 2) {
          setPhoneCountryCode(parts[0])
          setPhoneNationalNumber(parts[1])
        } else {
          setPhoneNationalNumber(user.phone_number)
        }
      }
    }
  }, [user])

  const startEdit = () => {
    if (!data) return
    setDraft({
      name: data.name || '',
      username: data.username || '',
      dob: data.dob || '',
      gender: data.gender || 'prefer_not_to_say',
      citizenship: data.citizenship || '',
      spendingTier: data.spendingTier || data.spending_tier || 'standard',
      home_city: data.home_city || '',
    })
    if (data.phone_number) {
      const parts = data.phone_number.split(' ')
      if (parts.length >= 2) {
        setPhoneCountryCode(parts[0])
        setPhoneNationalNumber(parts[1])
      } else {
        setPhoneNationalNumber(data.phone_number)
      }
    }
    setEditMode(true)
    setMessage(null)
  }

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target
    setDraft((d) => ({ ...d, [name]: value }))
  }

  const handlePhoneDigitsChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value.replace(/[^\d]/g, '').slice(0, 10)
    setPhoneNationalNumber(raw)
  }

  const handleSave = async () => {
    if (!data) return
    setMessage(null)

    // Validate Username
    if (draft.username !== undefined && draft.username.trim()) {
      const validation = validateUsername(draft.username)
      if (!validation.valid) {
        setMessage({
          type: 'error',
          text: validation.error || 'Invalid username format.',
        })
        return
      }
    }

    if (phoneNationalNumber.trim() && phoneNationalNumber.length !== 10) {
      setMessage({
        type: 'error',
        text: 'Please enter a valid 10-digit phone number (e.g. 9876543210)',
      })
      return
    }

    const formattedPhone = phoneNationalNumber.trim()
      ? `${phoneCountryCode} ${phoneNationalNumber}`
      : null

    const formattedUsername = draft.username?.trim().toLowerCase() || data.username

    const updated: Partial<UserProfile> = {
      ...draft,
      username: formattedUsername,
      phone_number: formattedPhone,
    }

    setLoading(true)
    const success = await updateProfile(updated)
    setLoading(false)

    if (success) {
      setData((prev) => (prev ? { ...prev, ...updated } : null))
      setEditMode(false)
      setMessage({ type: 'success', text: 'Profile updated in database successfully!' })
      setTimeout(() => setMessage(null), 3500)
    } else {
      setMessage({ type: 'error', text: 'Failed to update profile. Please try again.' })
    }
  }

  if (!isAuthenticated || !data) {
    return (
      <div className="max-w-md mx-auto px-4 py-20 text-center">
        <div className="w-16 h-16 rounded-3xl bg-indigo-50 text-indigo-600 flex items-center justify-center mx-auto mb-5 shadow-xs border border-indigo-100">
          <Lock className="w-8 h-8" />
        </div>
        <h2 className="text-2xl font-black text-slate-900 mb-2">Sign In to View Profile</h2>
        <p className="text-xs sm:text-sm text-slate-500 mb-6 leading-relaxed">
          Sign in with your Google account to customize your travel preferences and view profile settings.
        </p>
        <button
          onClick={openLogin}
          className="px-6 py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-xs sm:text-sm rounded-2xl shadow-md shadow-indigo-600/25 transition cursor-pointer"
        >
          Sign In with Google
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Header Banner */}
      <div className="bg-gradient-to-r from-indigo-600 to-violet-700 rounded-3xl p-6 sm:p-8 text-white shadow-lg mb-8 flex flex-col sm:flex-row items-center gap-6">
        <div className="relative">
          {data.photoURL || data.picture ? (
            <img
              src={data.photoURL || data.picture}
              alt={data.name || 'User'}
              className="w-24 h-24 rounded-full object-cover border-4 border-white/30 shadow-md"
            />
          ) : (
            <div className="w-24 h-24 rounded-full bg-white/20 border-4 border-white/30 flex items-center justify-center text-3xl font-black shadow-md">
              {data.name ? data.name[0] : 'U'}
            </div>
          )}
          <span className="absolute bottom-1 right-1 w-6 h-6 rounded-full bg-emerald-400 border-2 border-white flex items-center justify-center text-slate-900 text-xs font-bold shadow-xs">
            ✓
          </span>
        </div>

        <div className="text-center sm:text-left flex-1">
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-white/15 text-indigo-100 text-xs font-bold mb-2">
            <Sparkles className="w-3.5 h-3.5" />
            <span>Traveler Profile</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-black">{data.name || 'Traveler'}</h1>
          <p className="text-xs sm:text-sm text-indigo-100 mt-1">
            {data.email}
            {data.home_city && ` • ${data.home_city}`}
            {data.citizenship && `, ${data.citizenship}`}
          </p>
        </div>
      </div>

      {/* Profile Form Card */}
      <div className="bg-white rounded-3xl p-6 sm:p-8 border border-slate-200 shadow-xs">
        <div className="flex items-center justify-between mb-6 pb-4 border-b border-slate-100">
          <div>
            <h2 className="text-lg font-black text-slate-900">Traveler Identity & Preferences</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              These details help calibrate pacing, hotel tiers, and dietary defaults.
            </p>
          </div>
          {!editMode ? (
            <button
              onClick={startEdit}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold text-indigo-600 bg-indigo-50 border border-indigo-200 rounded-xl hover:bg-indigo-100 transition cursor-pointer"
            >
              <Edit className="w-3.5 h-3.5" />
              <span>Edit Profile</span>
            </button>
          ) : (
            <button
              onClick={() => setEditMode(false)}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold text-slate-600 bg-slate-100 border border-slate-200 rounded-xl hover:bg-slate-200 transition cursor-pointer"
            >
              <XCircle className="w-3.5 h-3.5" />
              <span>Cancel</span>
            </button>
          )}
        </div>

        {message && (
          <div
            className={`mb-6 p-3.5 rounded-2xl flex items-center gap-2 text-xs font-bold ${
              message.type === 'success'
                ? 'bg-emerald-50 text-emerald-800 border border-emerald-200'
                : 'bg-rose-50 text-rose-800 border border-rose-200'
            }`}
          >
            {message.type === 'success' ? (
              <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
            ) : (
              <XCircle className="w-4 h-4 text-rose-600 shrink-0" />
            )}
            <span>{message.text}</span>
          </div>
        )}

        <div className="space-y-6">
          {/* Row 1: Name & Username */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Full Name
              </label>
              {editMode ? (
                <input
                  type="text"
                  name="name"
                  value={draft.name || ''}
                  onChange={handleChange}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  required
                />
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100">
                  {data.name || 'Not provided'}
                </p>
              )}
            </div>

            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Username
              </label>
              {editMode ? (
                <div>
                  <input
                    type="text"
                    name="username"
                    value={draft.username || ''}
                    onChange={handleChange}
                    placeholder="e.g. nishant_valvi"
                    className="w-full px-4 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                    required
                  />
                  <p className="text-[10px] text-slate-400 mt-1">
                    3–30 chars, starts with a letter, letters, numbers, and _ only.
                  </p>
                </div>
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100">
                  {data.username || (data.email ? data.email.split('@')[0] : 'Not provided')}
                </p>
              )}
            </div>
          </div>

          {/* Row 2: Email & Phone */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Email Address (Google Identity)
              </label>
              <div className="relative">
                <Mail className="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2" />
                <input
                  type="email"
                  value={data.email}
                  readOnly
                  className="w-full pl-10 pr-4 py-2.5 rounded-xl border border-slate-200 bg-slate-50 text-slate-500 text-xs sm:text-sm cursor-not-allowed focus:outline-none"
                />
              </div>
            </div>

            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Phone Number
              </label>
              {editMode ? (
                <div className="flex gap-2">
                  <select
                    value={phoneCountryCode}
                    onChange={(e) => setPhoneCountryCode(e.target.value)}
                    className="px-3 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm bg-white focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  >
                    {COUNTRY_CODES.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <input
                    type="tel"
                    value={phoneNationalNumber}
                    onChange={handlePhoneDigitsChange}
                    placeholder="10-digit number"
                    className="flex-1 px-4 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  />
                </div>
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100">
                  {data.phone_number || 'Not provided'}
                </p>
              )}
            </div>
          </div>

          {/* Row 3: Home City, Citizenship, Gender */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-5">
            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Home City
              </label>
              {editMode ? (
                <input
                  type="text"
                  name="home_city"
                  value={draft.home_city || ''}
                  onChange={handleChange}
                  placeholder="e.g. Mumbai"
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                />
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100">
                  {data.home_city || 'Not provided'}
                </p>
              )}
            </div>

            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Citizenship
              </label>
              {editMode ? (
                <input
                  type="text"
                  name="citizenship"
                  value={draft.citizenship || ''}
                  onChange={handleChange}
                  placeholder="e.g. India"
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                />
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100">
                  {data.citizenship || 'Not provided'}
                </p>
              )}
            </div>

            <div>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                Gender
              </label>
              {editMode ? (
                <select
                  name="gender"
                  value={draft.gender || 'prefer_not_to_say'}
                  onChange={handleChange}
                  className="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-xs sm:text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none bg-white"
                >
                  <option value="male">Male</option>
                  <option value="female">Female</option>
                  <option value="other">Other</option>
                  <option value="prefer_not_to_say">Prefer not to say</option>
                </select>
              ) : (
                <p className="text-xs sm:text-sm font-semibold text-slate-900 bg-slate-50 px-4 py-2.5 rounded-xl border border-slate-100 capitalize">
                  {data.gender || 'Prefer not to say'}
                </p>
              )}
            </div>
          </div>

          {/* Row 4: Spending Tier */}
          <div>
            <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
              Default Budget & Spending Tier
            </label>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                { id: 'economy', label: 'Economy', desc: 'Hostels & budget transit' },
                { id: 'standard', label: 'Standard', desc: '3-4★ hotels & balanced pace' },
                { id: 'premium', label: 'Premium', desc: '4-5★ boutique stays' },
                { id: 'luxury', label: 'Luxury', desc: '5★ beachfront & private transfers' },
              ].map((t) => {
                const currentTier = editMode ? draft.spendingTier : (data.spendingTier || data.spending_tier)
                return (
                  <button
                    type="button"
                    key={t.id}
                    disabled={!editMode}
                    onClick={() => setDraft((d) => ({ ...d, spendingTier: t.id }))}
                    className={`p-3.5 rounded-2xl border text-left transition ${
                      currentTier === t.id
                        ? 'border-indigo-600 bg-indigo-50/70 text-indigo-900 shadow-2xs'
                        : 'border-slate-200 bg-white text-slate-700'
                    } ${editMode ? 'cursor-pointer hover:bg-slate-50' : 'cursor-default'}`}
                  >
                    <p className="font-bold text-xs">{t.label}</p>
                    <p className="text-[11px] text-slate-500 mt-0.5">{t.desc}</p>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Action Button */}
          {editMode && (
            <div className="pt-4 border-t border-slate-100 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setEditMode(false)}
                className="px-5 py-2.5 rounded-xl border border-slate-200 text-slate-600 font-bold text-xs sm:text-sm hover:bg-slate-50 transition cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={loading}
                onClick={handleSave}
                className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-bold text-xs sm:text-sm rounded-xl shadow-md shadow-indigo-600/20 transition flex items-center gap-2 cursor-pointer"
              >
                {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                <span>Save</span>
              </button>
            </div>
          )}
        </div>
      </div>
      <DemoUsageCard key={data.id} />
    </div>
  )
}

export default Profile
