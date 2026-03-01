export {}

declare global {
  interface Window {
    google: any
  }
}
// The installed lucide package exposes runtime icons but this locked version
// omits a declaration entrypoint under TypeScript's bundler resolution.
declare module 'lucide-react'
