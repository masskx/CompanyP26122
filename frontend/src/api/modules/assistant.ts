import type { AssistantCitation } from '@/features/assistant/types'
import { SSEParser, type ServerSentEvent } from '../sse'

interface RawCitation {
  ref?: number
  kind?: string
  title?: string
  source?: string | null
  section?: string | null
  kb_version?: string | null
  device_id?: string | null
  time_range?: string | null
  snippet?: string | null
}

interface RawMessage {
  content?: string
  citations?: RawCitation[]
  model?: string | null
  created_at?: string
  session_id?: string
}

export interface AssistantDoneMessage {
  content: string
  citations: AssistantCitation[]
  model?: string
  createdAt?: string
  sessionId?: string
}

export type AssistantStreamEvent =
  | { type: 'session'; sessionId: string }
  | { type: 'tool'; name: string }
  | { type: 'delta'; text: string }
  | { type: 'citation'; citation: AssistantCitation }
  | { type: 'done'; message: AssistantDoneMessage }
  | { type: 'unknown'; event: string }

export interface AssistantStreamOptions {
  signal?: AbortSignal
  requestId?: string
  onEvent: (event: AssistantStreamEvent) => void
}

export class AssistantApiError extends Error {
  readonly code: string
  readonly status?: number
  readonly requestId?: string

  constructor(
    code: string,
    message: string,
    options: { status?: number; requestId?: string } = {},
  ) {
    super(message)
    this.name = 'AssistantApiError'
    this.code = code
    this.status = options.status
    this.requestId = options.requestId
  }
}

function apiURL(path: string): string {
  const base = (import.meta.env.VITE_API_BASE_URL?.trim() || '/api').replace(
    /\/+$/,
    '',
  )
  return `${base}${path}`
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asString(value: unknown): string {
  return asText(value).trim()
}

function parseJSON(data: string): unknown {
  try {
    return JSON.parse(data)
  } catch {
    throw new AssistantApiError(
      'assistant_invalid_event',
      '智能服务返回了无法解析的流式数据。',
    )
  }
}

function mapCitation(
  citation: RawCitation,
  sequence: number,
): AssistantCitation {
  const kind = asString(citation.kind)
  const ref =
    typeof citation.ref === 'number' ? String(citation.ref) : String(sequence)
  const source =
    asString(citation.source) ||
    (kind === 'knowledge' ? '知识库' : kind === 'data' ? '设备数据' : '智能服务')
  const section =
    asString(citation.section) ||
    asString(citation.device_id) ||
    asString(citation.time_range)

  return {
    id: `citation-${ref}`,
    title: asString(citation.title) || `引用 ${ref}`,
    source,
    section,
    version: asString(citation.kb_version),
    excerpt: asText(citation.snippet),
  }
}

export function parseAssistantEvent(
  event: ServerSentEvent,
  sequence = 0,
): AssistantStreamEvent {
  if (
    event.event !== 'session' &&
    event.event !== 'tool' &&
    event.event !== 'delta' &&
    event.event !== 'citation' &&
    event.event !== 'done' &&
    event.event !== 'error'
  ) {
    return { type: 'unknown', event: event.event }
  }

  const payload = parseJSON(event.data)
  if (typeof payload !== 'object' || payload === null) {
    throw new AssistantApiError(
      'assistant_invalid_event',
      `智能服务返回了无效的 ${event.event} 事件。`,
    )
  }

  const data = payload as Record<string, unknown>
  switch (event.event) {
    case 'session': {
      const sessionId = asString(data.session_id)
      if (!sessionId) {
        throw new AssistantApiError(
          'assistant_invalid_event',
          '智能服务未返回会话标识。',
        )
      }
      return { type: 'session', sessionId }
    }
    case 'tool':
      return {
        type: 'tool',
        name: asString(data.name) || '业务工具',
      }
    case 'delta':
      return {
        type: 'delta',
        text: asText(data.text),
      }
    case 'citation':
      return {
        type: 'citation',
        citation: mapCitation(data as RawCitation, sequence),
      }
    case 'done': {
      const rawMessage =
        typeof data.message === 'object' && data.message !== null
          ? (data.message as RawMessage)
          : null
      if (!rawMessage) {
        throw new AssistantApiError(
          'assistant_invalid_event',
          '智能服务未返回最终回答。',
        )
      }
      return {
        type: 'done',
        message: {
          content: asText(rawMessage.content),
          citations: (rawMessage.citations ?? []).map((item, index) =>
            mapCitation(item, index + 1),
          ),
          model: asString(rawMessage.model) || undefined,
          createdAt: asString(rawMessage.created_at) || undefined,
          sessionId: asString(rawMessage.session_id) || undefined,
        },
      }
    }
    case 'error':
      throw new AssistantApiError(
        asString(data.code) || 'assistant_stream_error',
        asString(data.message) || '智能服务处理失败。',
        { requestId: asString(data.request_id) || undefined },
      )
    default:
      return { type: 'unknown', event: event.event }
  }
}

async function readHTTPError(response: Response): Promise<AssistantApiError> {
  const text = await response.text().catch(() => '')
  let payload: Record<string, unknown> = {}
  if (text) {
    try {
      const parsed: unknown = JSON.parse(text)
      if (typeof parsed === 'object' && parsed !== null) {
        payload = parsed as Record<string, unknown>
      }
    } catch {
      // Keep the status-based fallback below.
    }
  }

  let code = asString(payload.code)
  let message = asString(payload.message)
  const detail = payload.detail
  if (typeof detail === 'string') {
    message ||= detail
  } else if (typeof detail === 'object' && detail !== null) {
    const detailRecord = detail as Record<string, unknown>
    code ||= asString(detailRecord.code)
    message ||= asString(detailRecord.message)
  }

  return new AssistantApiError(
    code || 'assistant_http_error',
    message || `智能服务请求失败（HTTP ${response.status}）。`,
    {
      status: response.status,
      requestId: asString(payload.request_id) || undefined,
    },
  )
}

export async function streamAssistantChat(
  message: string,
  sessionId: string | undefined,
  options: AssistantStreamOptions,
): Promise<void> {
  const response = await fetch(apiURL('/v1/assistant/chat'), {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      ...(options.requestId ? { 'X-Request-ID': options.requestId } : {}),
    },
    body: JSON.stringify({
      message,
      ...(sessionId ? { session_id: sessionId } : {}),
    }),
    signal: options.signal,
  })

  if (!response.ok) {
    throw await readHTTPError(response)
  }
  if (!response.body) {
    throw new AssistantApiError(
      'assistant_stream_unavailable',
      '当前浏览器无法读取智能服务的流式响应。',
    )
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SSEParser()
  let sequence = 0
  let terminal = false

  const consume = (events: ServerSentEvent[]) => {
    for (const rawEvent of events) {
      sequence += 1
      const event = parseAssistantEvent(rawEvent, sequence)
      options.onEvent(event)
      if (event.type === 'done') {
        terminal = true
      }
    }
  }

  try {
    while (!terminal) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      consume(parser.push(decoder.decode(value, { stream: true })))
    }

    if (!terminal) {
      consume(parser.push(decoder.decode()))
      consume(parser.finish())
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }

  if (!terminal) {
    throw new AssistantApiError(
      'assistant_stream_incomplete',
      '智能服务未完整结束本次回答，请重试。',
    )
  }
}
