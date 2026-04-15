import { createBrowserRouter } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import App from './App'
import Landing from './pages/Landing'
import MyTrips from './pages/MyTrips'
import Profile from './pages/Profile'
import SampleTrip from './pages/SampleTrip'
import ProtectedRoute from './components/ProtectedRoute'

const CreateTrip = lazy(() => import('./pages/CreateTrip'))
const tripWorkspace = <Suspense fallback={<p role="status" className="max-w-5xl mx-auto px-6 py-10 text-sm text-slate-500">Opening your trip workspace…</p>}><CreateTrip /></Suspense>

export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Landing /> },
      { path: '/examples/:sampleId', element: <SampleTrip /> },
      { path: '/create', element: <ProtectedRoute>{tripWorkspace}</ProtectedRoute> },
      { path: '/trips/:tripId', element: <ProtectedRoute>{tripWorkspace}</ProtectedRoute> },
      { path: '/my-trips', element: <ProtectedRoute><MyTrips /></ProtectedRoute> },
      { path: '/profile', element: <Profile /> },
    ],
  },
])
