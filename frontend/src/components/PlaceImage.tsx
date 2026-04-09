import { useState } from 'react'
import { MapPin } from 'lucide-react'

export default function PlaceImage({ src, name, className = '' }: {
  src?: string; name: string; className?: string
}) {
  const [failedSource, setFailedSource] = useState<string>()
  const available = src && !src.startsWith('/covers/abstract-') && failedSource !== src
  return (
    <figure className={`shrink-0 ${className}`}>
      {available ? <img src={src} alt={name} loading="lazy"
        onError={() => setFailedSource(src)} className="h-full w-full rounded-xl object-cover" />
        : <div className="flex h-full w-full items-center justify-center rounded-xl bg-slate-100 text-slate-400">
          <MapPin className="h-6 w-6" aria-hidden="true" />
        </div>}
    </figure>
  )
}
