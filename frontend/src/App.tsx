import React from 'react'
import { Outlet } from 'react-router-dom'
import Header from './components/Header'
import LoginModal from './components/LoginModal'

const App: React.FC = () => {
  return (
    <div className="min-h-screen flex flex-col bg-gray-50 print:bg-white">
      <div className="print:hidden">
        <Header />
      </div>
      <main className="flex-1 print:p-0 print:m-0">
        <Outlet />
      </main>
      <footer className="text-center py-5 border-t border-slate-200 bg-white print:hidden">
        <p className="text-xs text-slate-500 font-medium">
          <span className="font-semibold text-slate-700">Waypoint</span> • Intelligent travel planning made effortless.
        </p>
      </footer>
      <LoginModal />
    </div>
  )
}

export default App
