import http from '../client'
import type { AssistantCitation } from '@/features/assistant/types'

interface RawCitation {
  ref?: number
  kind?: string
  title?: string
  source?: string | null
  device_id?: string | null
  time_range?: string | null
  section?: string | null
  snippet?: string | null
  kb_version?: string | null
}

export interface RemoteSession {
  session_id: string
  user_id: string
  title: string | null
  message_count: number
  workflow: string | null
  created_at: string
  updated_at: string
}

export interface RemoteMessage {
  message_id: string
  session_id: string
  seq: number
  role: 'user' | 'assistant'
  content: string
  status: 'complete' | 'error' | 'interrupted'
  citations: RawCitation[]
  tools_used: string[]
  model: string | null
  kb_version: string | null
  error: string | null
  feedback: number | null
  created_at: string
}

const sessionsPath = '/v1/assistant/sessions'

export function listSessions(limit = 20, offset = 0): Promise<RemoteSession[]> {
  return http
    .get<RemoteSession[]>(sessionsPath, { params: { limit, offset } })
    .then((response) => response.data)
}

export function createSession(title?: string): Promise<RemoteSession> {
  return http
    .post<RemoteSession>(sessionsPath, title ? { title } : undefined)
    .then((response) => response.data)
}

export function getSession(sessionId: string): Promise<RemoteSession> {
  return http
    .get<RemoteSession>(`${sessionsPath}/${encodeURIComponent(sessionId)}`)
    .then((response) => response.data)
}

export function renameSession(
  sessionId: string,
  title: string,
): Promise<RemoteSession> {
  return http
    .patch<RemoteSession>(`${sessionsPath}/${encodeURIComponent(sessionId)}`, {
      title,
    })
    .then((response) => response.data)
}

export function deleteSession(sessionId: string): Promise<void> {
  return http
    .delete(`${sessionsPath}/${encodeURIComponent(sessionId)}`)
    .then(() => undefined)
}

export function listMessages(
  sessionId: string,
  limit = 50,
  beforeSeq?: number,
): Promise<RemoteMessage[]> {
  return http
    .get<RemoteMessage[]>(
      `${sessionsPath}/${encodeURIComponent(sessionId)}/messages`,
      {
        params: {
          limit,
          ...(beforeSeq === undefined ? {} : { before_seq: beforeSeq }),
        },
      },
    )
    .then((response) => response.data)
}

export function mapRemoteCitation(
  citation: RawCitation,
  sequence: number,
): AssistantCitation {
  const ref =
    typeof citation.ref === 'number' ? String(citation.ref) : String(sequence)
  const kind = citation.kind?.trim() || ''
  const source =
    citation.source?.trim() ||
    (kind === 'knowledge'
      ? '知识库'
      : kind === 'data'
        ? '设备数据'
        : '智能服务')

  return {
    id: `citation-${ref}`,
    title: citation.title?.trim() || `引用 ${ref}`,
    source,
    section:
      citation.section?.trim() ||
      citation.device_id?.trim() ||
      citation.time_range?.trim() ||
      '',
    version: citation.kb_version?.trim() || '',
    excerpt: citation.snippet || '',
  }
}
