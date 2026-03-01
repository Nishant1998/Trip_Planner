import React from 'react'
import { Compass, X, Loader2 } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

const LoginModal: React.FC = () => {
  const { isLoginOpen, closeLogin, authError, isAuthenticating, loginAsTestUser, isTestMode } = useAuth()

  if (!isLoginOpen) return null

  return (
    <div
      role="dialog"
      aria-labelledby="sign-in-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !isAuthenticating) closeLogin()
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4 animate-in fade-in duration-200"
    >
      <div className="bg-white rounded-3xl shadow-2xl max-w-sm w-full p-6 sm:p-7 relative border border-slate-100">
        {!isAuthenticating && (
          <button
            type="button"
            aria-label="Close sign-in dialog"
            autoFocus
            onClick={closeLogin}
            className="absolute right-4 top-4 text-slate-400 hover:text-slate-700 w-8 h-8 rounded-full flex items-center justify-center hover:bg-slate-100 transition cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        )}

        <div className="flex items-center gap-2.5 mb-2">
          <div className="w-9 h-9 rounded-xl bg-indigo-600 text-white flex items-center justify-center shadow-md shadow-indigo-600/25">
            <Compass className="w-5 h-5" />
          </div>
          <span className="text-xl font-black text-slate-900 tracking-tight">Waypoint</span>
        </div>

        <h2 id="sign-in-title" className="text-lg font-black text-slate-900 mt-2">Sign in to Waypoint</h2>
        <p className="text-xs text-slate-500 mt-1 mb-5 leading-relaxed">
          {isTestMode
            ? 'Use the local test account to exercise trip planning without Google OAuth.'
            : 'Sign in with Google to save your verified itineraries, sync across devices, and customize travel preferences.'}
        </p>

        {authError && (
          <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs font-medium text-red-700">
            {authError}
          </div>
        )}

        {/* Active Authenticating Loading State */}
        {isAuthenticating ? (
          <div className="py-4 text-center">
            <Loader2 className="w-7 h-7 text-indigo-600 animate-spin mx-auto mb-2" />
            <p className="text-xs font-bold text-slate-800">Signing you in...</p>
            <p className="text-[11px] text-slate-400 mt-0.5">Syncing verified itineraries & profile</p>
          </div>
        ) : (
          <>
            {isTestMode ? (
              <button
                type="button"
                onClick={() => void loginAsTestUser()}
                className="w-full rounded-full bg-indigo-600 px-5 py-3 text-sm font-bold text-white shadow-md shadow-indigo-600/20 transition hover:bg-indigo-700"
              >
                Continue as test user
              </button>
            ) : (
              /* Google Identity Services Render Container */
              <div id="google-btn-container" className="flex justify-center my-2 min-h-[44px]" />
            )}
          </>
        )}
      </div>
    </div>
  )
}

export default LoginModal
