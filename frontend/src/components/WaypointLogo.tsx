import React from 'react'

interface WaypointLogoProps {
  className?: string
  iconOnly?: boolean
  size?: 'sm' | 'md' | 'lg'
}

export const WaypointMark: React.FC<{ className?: string }> = ({ className = 'w-7 h-7' }) => {
  return (
    <svg
      viewBox="0 0 36 36"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="Waypoint Icon"
    >
      <defs>
        <linearGradient id="wp-grad-primary" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#6366F1" />
          <stop offset="50%" stopColor="#8B5CF6" />
          <stop offset="100%" stopColor="#4F46E5" />
        </linearGradient>
        <linearGradient id="wp-grad-inner" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#FFFFFF" />
          <stop offset="100%" stopColor="#E0E7FF" />
        </linearGradient>
        <filter id="wp-glow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="2" stdDeviation="2" floodColor="#6366F1" floodOpacity="0.35" />
        </filter>
      </defs>

      {/* Outer Navigational Star / Waypoint Diamond */}
      <path
        d="M18 2L22.2 12.8C22.6 13.8 23.4 14.6 24.4 15L35.2 19.2C36.3 19.6 36.3 21.2 35.2 21.6L24.4 25.8C23.4 26.2 22.6 27 22.2 28L18 38.8C17.6 39.9 16 39.9 15.6 38.8L11.4 28C11 27 10.2 26.2 9.2 25.8L-1.6 21.6C-2.7 21.2 -2.7 19.6 -1.6 19.2L9.2 15C10.2 14.6 11 13.8 11.4 12.8L15.6 2C16 0.9 17.6 0.9 18 2Z"
        transform="matrix(0.85 0 0 0.85 2.7 2.7)"
        fill="url(#wp-grad-primary)"
        filter="url(#wp-glow)"
      />

      {/* Inner Precision Geo-Pin Node */}
      <circle cx="18" cy="18" r="5" fill="url(#wp-grad-inner)" />
      <circle cx="18" cy="18" r="2.2" fill="#4F46E5" />
    </svg>
  )
}

export const WaypointLogo: React.FC<WaypointLogoProps> = ({
  className = '',
  iconOnly = false,
  size = 'md',
}) => {
  const iconSizes = {
    sm: 'w-6 h-6',
    md: 'w-8 h-8',
    lg: 'w-10 h-10',
  }

  const textSizes = {
    sm: 'text-lg',
    md: 'text-2xl',
    lg: 'text-3xl',
  }

  return (
    <div className={`flex items-center gap-2.5 select-none ${className}`}>
      <div className="relative flex items-center justify-center shrink-0">
        <WaypointMark className={iconSizes[size]} />
      </div>

      {!iconOnly && (
        <div className="flex items-baseline gap-1">
          <span
            className={`${textSizes[size]} font-black text-slate-900 tracking-tight leading-none`}
          >
            Waypoint
          </span>
          <span className="w-1.5 h-1.5 rounded-full bg-indigo-600 inline-block mb-0.5 animate-pulse" />
        </div>
      )}
    </div>
  )
}

export default WaypointLogo
