import React, { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Plus, User, LogOut, Compass } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

const Header: React.FC = () => {
  const location = useLocation()
  const { user, isAuthenticated, openLogin, logout } = useAuth()
  const [isMenuOpen, setIsMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  const isActive = (path: string) => location.pathname === path

  useEffect(() => {
    if (!isMenuOpen) return

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setIsMenuOpen(false)
      }
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsMenuOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePointer)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [isMenuOpen])

  return (
    <header className="sticky top-0 z-40 bg-white/90 backdrop-blur-md shadow-xs border-b border-gray-200 py-3">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 w-full flex flex-wrap gap-3 items-center justify-between">
        {/* Brand Logo with standard Lucide icon */}
        <Link to="/" className="flex items-center gap-2.5 group">
          <div className="w-9 h-9 rounded-xl bg-indigo-600 text-white flex items-center justify-center shadow-md shadow-indigo-600/25 group-hover:scale-105 transition-transform">
            <Compass className="w-5 h-5" />
          </div>
          <span className="text-2xl font-black text-gray-900 tracking-tight">Waypoint</span>
        </Link>

        {/* Navigation matching main website */}
        <nav className="flex items-center gap-3 sm:gap-4">
          <Link
            to="/create"
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold text-white bg-indigo-600 rounded-full hover:bg-indigo-700 transition shadow-md shadow-indigo-600/20"
          >
            <Plus className="w-4 h-4" />
            <span>Create Trip</span>
          </Link>

          {isAuthenticated && (
            <Link
              to="/my-trips"
              className={`px-3.5 py-2 text-sm font-semibold rounded-full transition ${
                isActive('/my-trips')
                  ? 'text-indigo-600 bg-indigo-50 font-bold'
                  : 'text-gray-700 hover:text-indigo-600 hover:bg-gray-50'
              }`}
            >
              My Trips
            </Link>
          )}

          {/* User Profile / Avatar Dropdown */}
          {isAuthenticated && user ? (
            <div ref={menuRef} className="relative">
              <button
                onClick={() => setIsMenuOpen((prev) => !prev)}
                className="rounded-full overflow-hidden border-2 border-indigo-500 hover:border-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 transition cursor-pointer"
                title={user.name}
              >
                {user.photoURL ? (
                  <img
                    src={user.photoURL}
                    alt={user.name}
                    className="w-9 h-9 rounded-full object-cover"
                  />
                ) : (
                  <div className="w-9 h-9 rounded-full bg-indigo-600 text-white flex items-center justify-center text-sm font-bold">
                    {user.name[0]}
                  </div>
                )}
              </button>

              {isMenuOpen && (
                <div className="absolute right-0 mt-2 w-48 bg-white rounded-2xl shadow-xl py-1.5 border border-gray-100 z-50 animate-in fade-in zoom-in-95 duration-150">
                  <div className="px-4 py-2 border-b border-gray-100">
                    <p className="text-xs font-bold text-gray-900 truncate">{user.name}</p>
                    <p className="text-[11px] text-gray-500 truncate">{user.email}</p>
                  </div>
                  <Link
                    to="/profile"
                    onClick={() => setIsMenuOpen(false)}
                    className="w-full flex items-center gap-2 px-4 py-2.5 text-xs font-semibold text-gray-700 hover:bg-indigo-50 hover:text-indigo-600 transition"
                  >
                    <User className="w-4 h-4" />
                    <span>Profile Page</span>
                  </Link>
                  <button
                    onClick={() => {
                      setIsMenuOpen(false)
                      logout()
                    }}
                    className="w-full flex items-center gap-2 px-4 py-2.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 transition cursor-pointer"
                  >
                    <LogOut className="w-4 h-4" />
                    <span>Logout</span>
                  </button>
                </div>
              )}
            </div>
          ) : (
            <button
              onClick={openLogin}
              className="px-4 py-2 text-sm font-semibold text-white bg-gray-900 rounded-full hover:bg-indigo-700 transition shadow-md cursor-pointer"
            >
              Sign In
            </button>
          )}
        </nav>
      </div>
    </header>
  )
}

export default Header
