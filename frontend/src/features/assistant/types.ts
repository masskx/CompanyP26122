export type AssistantMessageRole = 'user' | 'assistant' | 'system'
export type AssistantMessageStatus =
  | 'processing'
  | 'complete'
  | 'cancelled'
  | 'error'
export type AssistantProgressStatus = 'pending' | 'working' | 'done'
export type AssistantPromptIcon =
  | 'maintenance'
  | 'event'
  | 'detector'
  | 'knowledge'

export interface AssistantProgressStep {
  id: string
  label: string
  detail: string
  status: AssistantProgressStatus
}

export interface AssistantCitation {
  id: string
  title: string
  source: string
  section: string
  version: string
  excerpt: string
}

export interface AssistantPrompt {
  id: string
  title: string
  description: string
  prompt: string
  icon: AssistantPromptIcon
}

export interface AssistantMessage {
  id: string
  requestId?: string
  role: AssistantMessageRole
  content: string
  createdAt: string
  seq?: number
  status?: AssistantMessageStatus
  progress?: AssistantProgressStep[]
  citations?: AssistantCitation[]
  elapsedSeconds?: number
  errorCode?: string
  errorMessage?: string
}

export interface AssistantSession {
  id: string
  title: string
  workflow?: string
  messageCount: number
  messages: AssistantMessage[]
  createdAt: string
  updatedAt: string
  isDraft?: boolean
  oldestSeq?: number
  hasMoreMessages?: boolean
  messagesLoaded?: boolean
}
