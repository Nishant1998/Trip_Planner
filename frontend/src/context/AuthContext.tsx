import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { apiFetch, readApiError } from '../lib/api'

export interface UserProfile {
  id: string | number
  name: string
  email: string
  role?: string
  picture?: string
  photoURL?: string
  username?: string
  phone_number?: string | null
  dob?: string | null
  gender?: string
  citizenship?: string | null
  spendingTier?: string
  spending_tier?: string
  home_city?: string
  authProvider?: string
}

interface AuthContextType {
  user: UserProfile | null
  isAuthenticated: boolean
  authReady: boolean
  isLoginOpen: boolean
  isAuthenticating: boolean
  authError: string | null
  isTestMode: boolean
  openLogin: () => void
  closeLogin: () => void
  logout: () => Promise<void>
  loginAsTestUser: () => Promise<boolean>
  updateProfile: (updates: Partial<UserProfile>) => Promise<boolean>
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)
const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID?.trim() ?? ''

type CredentialResponse = { credential: string }

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null)
  const [isLoginOpen, setIsLoginOpen] = useState(false)
  const [isAuthenticating, setIsAuthenticating] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)
  const [isTestMode, setIsTestMode] = useState(false)
  const [authReady, setAuthReady] = useState(false)

  const loadProfile = useCallback(async (): Promise<UserProfile | null> => {
    const response = await apiFetch('/api/users/profile')
    if (!response.ok) return null
    return response.json() as Promise<UserProfile>
  }, [])

  useEffect(() => {
    void loadProfile()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setAuthReady(true))
  }, [loadProfile])

  useEffect(() => {
    void apiFetch('/api/auth/test/status')
      .then((response) => (response.ok ? response.json() : { enabled: false }))
      .then((data) => setIsTestMode(data?.enabled === true))
      .catch(() => setIsTestMode(false))
  }, [])

  useEffect(() => {
    if (!isLoginOpen || isTestMode) return
    if (!GOOGLE_CLIENT_ID) {
      setAuthError('Google Sign-In is not configured for this environment.')
      return
    }
    if (!window.google) {
      setAuthError('Google Sign-In is loading. Please wait and try again.')
      return
    }
    const container = document.getElementById('google-btn-container')
    if (!container) return
    container.replaceChildren()

    window.google.accounts.id.initialize({
      client_id: GOOGLE_CLIENT_ID,
      auto_select: false,
      ux_mode: 'popup',
      callback: async (credential: CredentialResponse) => {
        if (!credential.credential) {
          setAuthError('No credential was received from Google.')
          return
        }
        setIsAuthenticating(true)
        setAuthError(null)
        try {
          const response = await apiFetch('/api/auth/google', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id_token: credential.credential }),
          })
          if (!response.ok) {
            setAuthError(await readApiError(response))
            return
          }
          const profile = await loadProfile()
          if (!profile) {
            setAuthError('Your session was created, but your profile could not be loaded.')
            return
          }
          setUser(profile)
          setIsLoginOpen(false)
        } catch {
          setAuthError('Unable to reach the backend. Please try again.')
        } finally {
          setIsAuthenticating(false)
        }
      },
    })
    window.google.accounts.id.renderButton(container, {
      theme: 'filled_blue',
      size: 'large',
      shape: 'pill',
      width: 320,
    })
  }, [isLoginOpen, isTestMode, loadProfile])

  const openLogin = useCallback(() => {
    setAuthError(null)
    setIsLoginOpen(true)
  }, [])

  const closeLogin = useCallback(() => {
    if (!isAuthenticating) setIsLoginOpen(false)
  }, [isAuthenticating])

  const loginAsTestUser = useCallback(async (): Promise<boolean> => {
    setAuthError(null)
    setIsAuthenticating(true)
    try {
      const response = await apiFetch('/api/auth/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!response.ok) {
        setAuthError(await readApiError(response))
        return false
      }
      const profile = await loadProfile()
      if (!profile) {
        setAuthError('Test session was created, but the profile could not be loaded.')
        return false
      }
      setUser(profile)
      setIsLoginOpen(false)
      return true
    } catch {
      setAuthError('Unable to reach the backend. Please try again.')
      return false
    } finally {
      setIsAuthenticating(false)
    }
  }, [loadProfile])

  const updateProfile = useCallback(async (updates: Partial<UserProfile>) => {
    if (!user) return false
    try {
      const response = await apiFetch('/api/users/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      })
      if (!response.ok) return false
      const profile = await loadProfile()
      if (profile) setUser(profile)
      return Boolean(profile)
    } catch {
      return false
    }
  }, [loadProfile, user])

  const logout = useCallback(async () => {
    try {
      if (user?.email && window.google?.accounts?.id?.revoke) {
        window.google.accounts.id.revoke(user.email, () => undefined)
      }
      await apiFetch('/api/auth/logout', { method: 'POST' })
    } finally {
      setUser(null)
      window.location.assign('/')
    }
  }, [user?.email])

  const value = useMemo<AuthContextType>(() => ({
    user,
    isAuthenticated: Boolean(user),
    authReady,
    isLoginOpen,
    isAuthenticating,
    authError,
    isTestMode,
    openLogin,
    closeLogin,
    logout,
    loginAsTestUser,
    updateProfile,
  }), [
    user, authReady, isLoginOpen, isAuthenticating, authError, isTestMode,
    openLogin, closeLogin, logout, loginAsTestUser, updateProfile,
  ])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within an AuthProvider')
  return context
}
