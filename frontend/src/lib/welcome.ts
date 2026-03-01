import type { UserProfile } from '../context/AuthContext'

const NEUTRAL_MESSAGES = [
  'Ready when you are. Where would you like to travel?',
  'Tell me what kind of trip you are planning.',
  'Where are you dreaming of travelling, and for how long?',
  'Let’s build your trip. What destination is on your mind?',
  'Every great trip starts with an idea. What kind of escape are you looking for?',
]

const PERSONALIZED_MESSAGES = [
  'Hi {name}, I’m Riya. Where would you love to travel next?',
  'Hey {name}! Dreaming of a quick escape or a big adventure?',
  'If you could leave tomorrow, {name}, where would you go?',
  'Let’s turn your travel idea into a real plan, {name}. What is on your mind?',
  'Hi {name}! Beaches, mountains, food, history, or something completely different?',
]

function firstName(user?: UserProfile | null): string | null {
  const value = user?.name?.trim().split(/\s+/)[0]
  return value && value.length < 50 ? value : null
}

/** Pick a fresh welcome message whenever a new blank-trip screen is opened. */
export function getWelcomeMessage(user?: UserProfile | null): string {
  const name = firstName(user)
  // Signed-in users see an even mix of personal and neutral greetings. Guests
  // see only neutral wording because there is no reliable name to include.
  const options = name
    ? [...PERSONALIZED_MESSAGES, ...NEUTRAL_MESSAGES]
    : NEUTRAL_MESSAGES
  const index = Math.floor(Math.random() * options.length)
  return options[index].replace('{name}', name || '')
}
