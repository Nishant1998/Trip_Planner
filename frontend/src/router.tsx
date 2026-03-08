import { createBrowserRouter } from 'react-router-dom'
import App from './App'
import Landing from './pages/Landing'
import CreateTrip from './pages/CreateTrip'
import MyTrips from './pages/MyTrips'
import Profile from './pages/Profile'
import ProtectedRoute from './components/ProtectedRoute'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Landing /> },
      { path: '/create', element: <ProtectedRoute><CreateTrip /></ProtectedRoute> },
      { path: '/trips/:tripId', element: <ProtectedRoute><CreateTrip /></ProtectedRoute> },
      { path: '/my-trips', element: <ProtectedRoute><MyTrips /></ProtectedRoute> },
      { path: '/profile', element: <Profile /> },
    ],
  },
])
