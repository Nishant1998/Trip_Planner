import { Component, type ErrorInfo, type ReactNode } from 'react'

interface State {
  hasError: boolean
}

export default class AppErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Never include user data or tokens in client diagnostics.
    console.error('Unhandled application error', error.name, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 text-center">
          <h1 className="text-2xl font-bold text-slate-900">Something went wrong</h1>
          <p className="mt-3 text-sm text-slate-600">Please refresh the page and try again.</p>
          <button
            className="mt-6 rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white"
            onClick={() => window.location.reload()}
          >
            Refresh page
          </button>
        </main>
      )
    }
    return this.props.children
  }
}
