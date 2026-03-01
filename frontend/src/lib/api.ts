const configuredApiBase = import.meta.env.VITE_API_BASE?.trim()

// Same-origin deployment needs no public API URL. A separate frontend/API
// deployment must set VITE_API_BASE during its build.
export const API_BASE = configuredApiBase
  ? configuredApiBase.replace(/\/$/, '')
  : import.meta.env.DEV
    ? 'http://127.0.0.1:8000'
    : ''

export async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
  })
  return response
}

export async function readApiError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null)
  return body?.detail || body?.error?.reason || `Request failed (${response.status})`
}
