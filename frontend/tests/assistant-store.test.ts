import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { ApiError } from '../src/api'
import { useAssistantStore } from '../src/features/assistant/stores/assistant'

const conversationMocks = vi.hoisted(() => ({
  listSessions: vi.fn(),
  createSession: vi.fn(),
  getSession: vi.fn(),
  renameSession: vi.fn(),
  deleteSession: vi.fn(),
  listMessages: vi.fn(),
  mapRemoteCitation: vi.fn((citation, sequence) => ({
    id: `citation-${citation.ref ?? sequence}`,
    title: citation.title || '引用',
    source: citation.source || '设备数据',
    section: citation.device_id || '',
    version: citation.kb_version || '',
    excerpt: citation.snippet || '',
  })),
}))

const streamAssistantChat = vi.hoisted(() => vi.fn())

vi.mock('@/api/modules/conversation', () => conversationMocks)
vi.mock('@/api/modules/assistant', async () => {
  const actual = await vi.importActual<
    typeof import('../src/api/modules/assistant')
  >('../src/api/modules/assistant')
  return { ...actual, streamAssistantChat }
})

const remoteSession = {
  session_id: 's_1',
  user_id: 'anonymous',
  title: '已有对话',
  message_count: 2,
  workflow: null,
  created_at: '2026-11-16T08:00:00Z',
  updated_at: '2026-11-16T08:02:00Z',
}

function remoteMessage(seq: number) {
  return {
    message_id: `m_${seq}`,
    session_id: 's_1',
    seq,
    role: seq % 2 === 1 ? ('user' as const) : ('assistant' as const),
    content: `消息 ${seq}`,
    status: 'complete' as const,
    citations: [],
    tools_used: [],
    model: null,
    kb_version: null,
    error: null,
    feedback: null,
    created_at: `2026-11-16T08:${String(seq).padStart(2, '0')}:00Z`,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  conversationMocks.listSessions.mockResolvedValue([])
})

describe('assistant conversation store', () => {
  it('loads the global server session list when opened', async () => {
    conversationMocks.listSessions.mockResolvedValue([remoteSession])
    const store = useAssistantStore()

    store.open()

    await vi.waitFor(() => expect(store.sessions).toHaveLength(1))
    expect(store.sessions[0]).toMatchObject({
      id: 's_1',
      title: '已有对话',
      messageCount: 2,
      isDraft: false,
    })
    expect(conversationMocks.listSessions).toHaveBeenCalledWith(20, 0)
  })

  it('promotes the first turn to the backend session id and continues it', async () => {
    let call = 0
    streamAssistantChat.mockImplementation(
      async (_message: string, _sessionId: string | undefined, options) => {
        call += 1
        const suffix = call === 1 ? 'one' : 'two'
        options.onEvent({
          type: 'session',
          sessionId: 's_remote',
          userMessageId: `m_user_${suffix}`,
        })
        options.onEvent({
          type: 'done',
          message: {
            messageId: `m_answer_${suffix}`,
            content: `回答 ${suffix}`,
            citations: [],
            sessionId: 's_remote',
            status: 'complete',
            seq: call * 2,
          },
        })
      },
    )
    const store = useAssistantStore()

    store.draft = '第一个问题'
    expect(store.sendMessage()).toBe(true)

    await vi.waitFor(() => expect(store.currentSession?.id).toBe('s_remote'))
    expect(streamAssistantChat.mock.calls[0][1]).toBeUndefined()
    expect(store.messages[0].id).toBe('m_user_one')
    expect(store.messages[1]).toMatchObject({
      id: 'm_answer_one',
      content: '回答 one',
      status: 'complete',
    })

    store.draft = '继续追问'
    expect(store.sendMessage()).toBe(true)

    await vi.waitFor(() => expect(streamAssistantChat).toHaveBeenCalledTimes(2))
    expect(streamAssistantChat.mock.calls[1][1]).toBe('s_remote')
  })

  it('loads message history and prepends older pages without duplicates', async () => {
    conversationMocks.listSessions.mockResolvedValue([remoteSession])
    conversationMocks.getSession.mockResolvedValue(remoteSession)
    conversationMocks.listMessages
      .mockResolvedValueOnce(
        Array.from({ length: 50 }, (_, index) => remoteMessage(51 + index)),
      )
      .mockResolvedValueOnce(
        Array.from({ length: 51 }, (_, index) => remoteMessage(1 + index)),
      )
    const store = useAssistantStore()

    store.open()
    await vi.waitFor(() => expect(store.sessions).toHaveLength(1))
    await store.selectSession('s_1')

    expect(store.messages).toHaveLength(50)
    expect(store.currentSession?.hasMoreMessages).toBe(true)

    await store.loadMoreMessages()

    expect(store.messages).toHaveLength(100)
    expect(store.messages[0].seq).toBe(1)
    expect(store.messages.at(-1)?.seq).toBe(100)
    expect(conversationMocks.listMessages).toHaveBeenLastCalledWith(
      's_1',
      50,
      51,
    )
  })

  it('renames and deletes sessions and removes stale active history', async () => {
    conversationMocks.listSessions.mockResolvedValue([remoteSession])
    conversationMocks.renameSession.mockResolvedValue({
      ...remoteSession,
      title: '新标题',
    })
    conversationMocks.deleteSession.mockResolvedValue(undefined)
    const store = useAssistantStore()

    store.open()
    await vi.waitFor(() => expect(store.sessions).toHaveLength(1))
    expect(await store.renameSession('s_1', '新标题')).toBe(true)
    expect(store.sessions[0].title).toBe('新标题')

    store.activeSessionId = 's_1'
    expect(await store.deleteSession('s_1')).toBe(true)
    expect(store.sessions).toHaveLength(0)
    expect(store.activeSessionId).toBeNull()

    conversationMocks.listSessions.mockResolvedValue([remoteSession])
    conversationMocks.getSession.mockRejectedValue(
      new ApiError('session not found', {
        status: 404,
      }),
    )
    conversationMocks.listMessages.mockRejectedValue(
      new ApiError('session not found', {
        status: 404,
      }),
    )
    await store.loadSessions(true)
    await store.selectSession('s_1')
    await vi.waitFor(() => expect(store.sessions).toHaveLength(0))
    expect(store.sessionError).toContain('会话已失效')
  })
})
