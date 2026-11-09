export type AssistantMessageRole = 'user' | 'assistant' | 'system'

export interface AssistantMessage {
  id: string
  role: AssistantMessageRole
  content: string
  createdAt: string
}
