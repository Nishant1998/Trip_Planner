import { useState } from 'react'
import { Info, MapPin } from 'lucide-react'

export default function PlaceImage({ src, name, className = '', photoSourceUrl, photoCredit }: {
  src?: string; name: string; className?: string; photoSourceUrl?: string; photoCredit?: string
}) {
  const [failedSource, setFailedSource] = useState<string>()
  const available = src && !src.startsWith('/covers/abstract-') && failedSource !== src
  return (
    <figure className={`relative shrink-0 overflow-hidden rounded-xl ${className}`}>
      {available ? <img src={src} alt={name} loading="lazy"
        decoding="async"
        onError={() => setFailedSource(src)} className="h-full w-full max-w-full object-cover" />
        : <div className="flex h-full w-full items-center justify-center rounded-xl bg-slate-100 text-slate-400">
          <MapPin className="h-6 w-6" aria-hidden="true" />
        </div>}
      {available && photoSourceUrl && <a href={photoSourceUrl} target="_blank" rel="noreferrer"
        aria-label={`Photo credit: ${photoCredit || name}`} title={`Photo: ${photoCredit || name}`}
        className="absolute bottom-1 right-1 rounded-full bg-white/90 p-1 text-slate-700 focus-visible:outline-2 focus-visible:outline-indigo-600">
        <Info className="h-3 w-3" aria-hidden="true" />
      </a>}
    </figure>
  )
}
