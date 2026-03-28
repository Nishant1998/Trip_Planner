export interface ChatMessage {
  id: string | number
  role: 'assistant' | 'user' | 'system'
  content?: string
  text?: string
  timestamp?: string
  suggestions?: string[]
  isWelcome?: boolean
  showConfirmationCard?: boolean
  snapshot?: { destination?: string; duration?: string; travelers?: string; budget?: string; tier?: string }
  clientMessageId?: string
  delivery?: 'pending' | 'streaming' | 'saved' | 'failed'
}

function sameMessage(left: ChatMessage, right: ChatMessage): boolean {
  return String(left.id) === String(right.id)
    || Boolean(left.clientMessageId && left.clientMessageId === right.clientMessageId)
    || Boolean(left.isWelcome && right.isWelcome)
}

export function mergeChatMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const merged = [...current]
  let previousIndex = -1
  for (const message of incoming) {
    const index = merged.findIndex(existing => sameMessage(existing, message))
    if (index >= 0) {
      merged[index] = message
      previousIndex = index
      continue
    }
    // History may lag behind the optimistic send or a live reply. Keep both
    // visible, inserting older server messages before the local tail.
    const localIndex = merged.findIndex(existing => existing.delivery === 'pending' || existing.delivery === 'streaming')
    const insertionIndex = previousIndex >= 0 ? previousIndex + 1 : localIndex >= 0 ? localIndex : merged.length
    merged.splice(insertionIndex, 0, message)
    previousIndex = insertionIndex
  }
  return merged
}

export function applyChatStream(messages: ChatMessage[], id: string, chunk?: string, text?: string): ChatMessage[] {
  const existing = messages.find(message => String(message.id) === id)
  if (existing?.delivery === 'saved') return messages
  const streamed: ChatMessage = {
    id, role: 'assistant', delivery: 'streaming',
    content: text ?? `${existing?.content ?? ''}${chunk ?? ''}`,
  }
  return existing ? messages.map(message => String(message.id) === id ? { ...message, ...streamed } : message) : [...messages, streamed]
}
