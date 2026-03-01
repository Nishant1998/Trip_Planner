import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { useAuth } from '../context/AuthContext'

/** UX guard only; the backend remains the authority for private data. */
export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const { authReady, isAuthenticated, openLogin } = useAuth()
  const requestedLogin = useRef(false)

  useEffect(() => {
    if (authReady && !isAuthenticated && !requestedLogin.current) {
      requestedLogin.current = true
      openLogin()
    }
    if (isAuthenticated) requestedLogin.current = false
  }, [authReady, isAuthenticated, openLogin])

  if (!authReady) return <main aria-busy="true" className="min-h-screen" />
  if (!isAuthenticated) return <main className="min-h-screen" aria-label="Sign in required" />
  return <>{children}</>
}
