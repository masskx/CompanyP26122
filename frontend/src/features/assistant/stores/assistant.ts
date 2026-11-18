import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '@/api'
import {
  AssistantApiError,
  streamAssistantChat,
  type AssistantStreamEvent,
} from '@/api/modules/assistant'
import {
  deleteSession as deleteRemoteSession,
  getSession,
  listMessages,
  listSessions,
  mapRemoteCitation,
  renameSession as renameRemoteSession,
  type RemoteMessage,
  type RemoteSession,
} from '@/api/modules/conversation'
import type {
  AssistantMessage,
  AssistantProgressStep,
  AssistantSession,
} from '../types'

interface PendingRun {
  sessionId: string
  remoteSessionId?: string
  userMessageId: string
  assistantMessageId: string
  controller: AbortController
  startedAt: number
  requestId: string
}

const SESSION_PAGE_SIZE = 20
const MESSAGE_PAGE_SIZE = 50

const progressTemplate: Omit<AssistantProgressStep, 'status'>[] = [
  {
    id: 'connect',
    label: '连接智能服务',
    detail: '正在建立与算力服务的对话连接。',
  },
  {
    id: 'reason',
    label: '理解问题并检索依据',
    detail: '按问题需要检索知识或读取设备数据。',
  },
  {
    id: 'answer',
    label: '生成回答',
    detail: '整理模型输出与可追溯的引用来源。',
  },
]

const toolDetails: Record<string, string> = {
  search_knowledge: '正在检索维护知识库。',
  list_stations: '正在读取站点清单。',
  get_station_status: '正在读取站点状态与阈值评估。',
  get_telemetry: '正在读取站点时序数据。',
  run_maintenance_check: '正在执行维护规则分析。',
  get_work_orders: '正在读取维护工单副本。',
}

function errorCode(error: unknown): string {
  if (error instanceof AssistantApiError || error instanceof ApiError) {
    return error.code || 'assistant_request_failed'
  }
  return 'assistant_request_failed'
}

function errorText(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '智能服务暂时不可用，请稍后重试。'
}

function isSessionNotFound(error: unknown): boolean {
  return (
    errorCode(error) === 'session_not_found' ||
    (error instanceof ApiError && error.status === 404) ||
    (error instanceof AssistantApiError && error.status === 404)
  )
}

function sessionTitle(content: string): string {
  return content.replace(/\s+/g, ' ').trim().slice(0, 40) || '新对话'
}

function mapRemoteSession(
  remote: RemoteSession,
  existing?: AssistantSession,
): AssistantSession {
  return {
    id: remote.session_id,
    title: remote.title?.trim() || '未命名对话',
    workflow: remote.workflow || undefined,
    messageCount: remote.message_count,
    messages: existing?.messages ?? [],
    createdAt: remote.created_at,
    updatedAt: remote.updated_at,
    isDraft: false,
    oldestSeq: existing?.oldestSeq,
    hasMoreMessages: existing?.hasMoreMessages,
    messagesLoaded: existing?.messagesLoaded,
  }
}

function mapRemoteMessage(remote: RemoteMessage): AssistantMessage {
  const status =
    remote.status === 'interrupted'
      ? 'cancelled'
      : remote.status === 'error'
        ? 'error'
        : 'complete'

  return {
    id: remote.message_id,
    role: remote.role,
    content:
      remote.content ||
      (status === 'error' ? remote.error || '本次回答生成失败。' : ''),
    createdAt: remote.created_at,
    seq: remote.seq,
    status,
    citations: (remote.citations ?? []).map((citation, index) =>
      mapRemoteCitation(citation, index + 1),
    ),
    errorCode: status === 'error' ? 'assistant_persisted_error' : undefined,
    errorMessage: remote.error || undefined,
  }
}

function mergeMessages(
  older: AssistantMessage[],
  newer: AssistantMessage[],
): AssistantMessage[] {
  const byKey = new Map<string, AssistantMessage>()
  for (const message of [...older, ...newer]) {
    const key = message.id || `seq-${message.seq ?? message.createdAt}`
    byKey.set(key, message)
  }
  return [...byKey.values()].sort((left, right) => {
    if (left.seq !== undefined && right.seq !== undefined && left.seq !== right.seq) {
      return left.seq - right.seq
    }
    return new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime()
  })
}

export const useAssistantStore = defineStore('assistant', () => {
  const isOpen = ref(false)
  const draft = ref('')
  const sessions = ref<AssistantSession[]>([])
  const activeSessionId = ref<string | null>(null)
  const pendingRun = ref<PendingRun | null>(null)
  const isLoadingSessions = ref(false)
  const isLoadingMoreSessions = ref(false)
  const isLoadingMessages = ref(false)
  const isLoadingMoreMessages = ref(false)
  const sessionError = ref<string | null>(null)
  const sessionHasMore = ref(false)
  const sessionOffset = ref(0)

  const currentSession = computed(
    () =>
      sessions.value.find(
        (session) => session.id === activeSessionId.value,
      ) ?? null,
  )
  const messages = computed(() => currentSession.value?.messages ?? [])
  const isGenerating = computed(() => pendingRun.value !== null)
  const canSend = computed(
    () =>
      draft.value.trim().length > 0 &&
      !isGenerating.value &&
      !isLoadingMessages.value,
  )

  let sessionSequence = 0
  let messageSequence = 0
  let requestSequence = 0
  let messageLoadSequence = 0

  function open() {
    isOpen.value = true
    void loadSessions(true)
  }

  function close() {
    isOpen.value = false
  }

  function toggle() {
    isOpen.value = !isOpen.value
    if (isOpen.value) {
      void loadSessions(true)
    }
  }

  function newRequestID(): string {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID()
    }
    requestSequence += 1
    return `assistant-turn-${Date.now()}-${requestSequence}`
  }

  function sortSessions() {
    sessions.value = [...sessions.value].sort(
      (left, right) =>
        new Date(right.updatedAt).getTime() -
        new Date(left.updatedAt).getTime(),
    )
  }

  function findSession(sessionId: string): AssistantSession | undefined {
    return sessions.value.find((session) => session.id === sessionId)
  }

  function findMessage(run: PendingRun): AssistantMessage | undefined {
    return findSession(run.sessionId)?.messages.find(
      (message) => message.id === run.assistantMessageId,
    )
  }

  function isCurrentRun(run: PendingRun): boolean {
    return pendingRun.value?.controller === run.controller
  }

  function setProgress(message: AssistantMessage, activeIndex: number) {
    message.progress?.forEach((step, index) => {
      step.status =
        index < activeIndex
          ? 'done'
          : index === activeIndex
            ? 'working'
            : 'pending'
    })
  }

  function completeProgress(message: AssistantMessage) {
    message.progress?.forEach((step) => {
      step.status = 'done'
    })
  }

  function moveSessionToTop(session: AssistantSession) {
    sessions.value = [
      session,
      ...sessions.value.filter((item) => item.id !== session.id),
    ]
  }

  function removeSessionLocal(sessionId: string) {
    sessions.value = sessions.value.filter((session) => session.id !== sessionId)
    if (activeSessionId.value === sessionId) {
      activeSessionId.value = null
    }
  }

  function handleSessionNotFound(message?: string) {
    sessionError.value = message || '会话已失效，已开始新对话。'
    if (activeSessionId.value) {
      removeSessionLocal(activeSessionId.value)
    }
  }

  async function loadSessions(reset = false) {
    if (isLoadingSessions.value || isLoadingMoreSessions.value) {
      return
    }
    const loading = reset ? isLoadingSessions : isLoadingMoreSessions
    loading.value = true
    if (reset) {
      sessionError.value = null
    }

    const offset = reset ? 0 : sessionOffset.value
    try {
      const remote = await listSessions(SESSION_PAGE_SIZE, offset)
      const byId = new Map(sessions.value.map((session) => [session.id, session]))
      const mapped = remote.map((item) =>
        mapRemoteSession(item, byId.get(item.session_id)),
      )
      if (reset) {
        const activeId = activeSessionId.value
        const keep = sessions.value.filter(
          (session) =>
            session.isDraft ||
            (session.id === activeId &&
              !remote.some((item) => item.session_id === session.id)),
        )
        sessions.value = [...mapped, ...keep]
        sessionOffset.value = remote.length
      } else {
        const known = new Set(sessions.value.map((session) => session.id))
        sessions.value = [
          ...sessions.value,
          ...mapped.filter((session) => !known.has(session.id)),
        ]
        sessionOffset.value += remote.length
      }
      sessionHasMore.value = remote.length === SESSION_PAGE_SIZE
      sortSessions()
    } catch (error) {
      sessionError.value = errorText(error)
    } finally {
      loading.value = false
    }
  }

  function loadMoreSessions() {
    if (!sessionHasMore.value) {
      return
    }
    void loadSessions(false)
  }

  async function loadSessionMessages(sessionId: string) {
    const session = findSession(sessionId)
    if (!session || session.isDraft) {
      return
    }

    const loadId = ++messageLoadSequence
    isLoadingMessages.value = true
    sessionError.value = null
    try {
      const [remoteSession, remoteMessages] = await Promise.all([
        getSession(sessionId),
        listMessages(sessionId, MESSAGE_PAGE_SIZE),
      ])
      if (loadId !== messageLoadSequence || activeSessionId.value !== sessionId) {
        return
      }

      const target = findSession(sessionId)
      if (!target) {
        return
      }
      const mapped = mapRemoteSession(remoteSession, target)
      Object.assign(target, mapped)
      target.messages = remoteMessages.map(mapRemoteMessage)
      target.messagesLoaded = true
      target.oldestSeq = target.messages[0]?.seq
      target.hasMoreMessages = remoteMessages.length === MESSAGE_PAGE_SIZE
    } catch (error) {
      if (loadId !== messageLoadSequence) {
        return
      }
      if (isSessionNotFound(error)) {
        removeSessionLocal(sessionId)
        handleSessionNotFound()
      } else {
        sessionError.value = errorText(error)
      }
    } finally {
      if (loadId === messageLoadSequence) {
        isLoadingMessages.value = false
      }
    }
  }

  async function loadMoreMessages() {
    const session = currentSession.value
    if (
      !session ||
      session.isDraft ||
      !session.hasMoreMessages ||
      session.oldestSeq === undefined ||
      isLoadingMoreMessages.value
    ) {
      return
    }

    isLoadingMoreMessages.value = true
    try {
      const older = await listMessages(
        session.id,
        MESSAGE_PAGE_SIZE,
        session.oldestSeq,
      )
      const target = findSession(session.id)
      if (!target || activeSessionId.value !== session.id) {
        return
      }
      target.messages = mergeMessages(
        older.map(mapRemoteMessage),
        target.messages,
      )
      target.oldestSeq = target.messages[0]?.seq
      target.hasMoreMessages = older.length === MESSAGE_PAGE_SIZE
    } catch (error) {
      if (isSessionNotFound(error)) {
        removeSessionLocal(session.id)
        handleSessionNotFound()
      } else {
        sessionError.value = errorText(error)
      }
    } finally {
      isLoadingMoreMessages.value = false
    }
  }

  async function selectSession(sessionId: string) {
    const session = findSession(sessionId)
    if (!session) {
      return
    }
    if (sessionId === activeSessionId.value) {
      return
    }
    cancelPendingRun()
    activeSessionId.value = sessionId
    if (!session.isDraft) {
      await loadSessionMessages(sessionId)
    }
  }

  function replaceSessionId(run: PendingRun, nextId: string) {
    const session = findSession(run.sessionId)
    if (!session) {
      return
    }
    const previousId = session.id
    session.id = nextId
    session.isDraft = false
    session.messagesLoaded = true
    run.remoteSessionId = nextId
    run.sessionId = nextId
    if (activeSessionId.value === previousId) {
      activeSessionId.value = nextId
    }
  }

  function replaceUserMessageId(run: PendingRun, nextId: string) {
    if (!nextId) {
      return
    }
    const session = findSession(run.sessionId)
    const message = session?.messages.find(
      (item) => item.id === run.userMessageId,
    )
    if (message) {
      message.id = nextId
    }
    run.userMessageId = nextId
  }

  function applyStreamEvent(run: PendingRun, event: AssistantStreamEvent) {
    if (!isCurrentRun(run)) {
      return
    }

    let session = findSession(run.sessionId)
    let message = findMessage(run)
    if (!session || !message || message.status !== 'processing') {
      return
    }

    switch (event.type) {
      case 'session':
        if (run.remoteSessionId !== event.sessionId) {
          replaceSessionId(run, event.sessionId)
        }
        if (event.userMessageId) {
          replaceUserMessageId(run, event.userMessageId)
        }
        session = findSession(run.sessionId)
        message = findMessage(run)
        if (session) {
          session.messageCount = Math.max(session.messageCount, 1)
        }
        if (message) {
          setProgress(message, 1)
        }
        break
      case 'tool':
        setProgress(message, 1)
        if (message.progress?.[1]) {
          message.progress[1].detail =
            toolDetails[event.name] ?? `正在调用 ${event.name}。`
        }
        break
      case 'delta':
        setProgress(message, 2)
        message.content += event.text
        break
      case 'citation':
        message.citations ??= []
        if (!message.citations.some((item) => item.id === event.citation.id)) {
          message.citations.push(event.citation)
        }
        break
      case 'done': {
        const finalContent = event.message.content || message.content
        if (!finalContent.trim()) {
          failRun(run, new Error('智能服务返回了空回答。'), 'assistant_empty_answer')
          return
        }
        if (
          event.message.sessionId &&
          run.remoteSessionId !== event.message.sessionId
        ) {
          replaceSessionId(run, event.message.sessionId)
        }
        session = findSession(run.sessionId)
        message = findMessage(run)
        if (!session || !message) {
          return
        }
        if (event.message.messageId) {
          message.id = event.message.messageId
        }
        message.status =
          event.message.status === 'interrupted'
            ? 'cancelled'
            : event.message.status === 'error'
              ? 'error'
              : 'complete'
        message.content = finalContent
        message.seq = event.message.seq
        message.citations =
          event.message.citations.length > 0
            ? event.message.citations
            : message.citations
        message.elapsedSeconds = Number(
          ((Date.now() - run.startedAt) / 1000).toFixed(1),
        )
        completeProgress(message)
        session.isDraft = false
        session.messagesLoaded = true
        session.updatedAt =
          event.message.createdAt || new Date().toISOString()
        session.messageCount = Math.max(
          session.messageCount,
          session.messages.length,
        )
        sortSessions()
        pendingRun.value = null
        break
      }
      case 'unknown':
        break
    }
  }

  function failRun(
    run: PendingRun,
    error: unknown,
    fallbackCode = 'assistant_request_failed',
  ) {
    if (!isCurrentRun(run)) {
      return
    }

    const session = findSession(run.sessionId)
    const message = findMessage(run)
    pendingRun.value = null
    if (!session || !message || message.status !== 'processing') {
      return
    }

    const code =
      error instanceof AssistantApiError || error instanceof ApiError
        ? error.code || fallbackCode
        : fallbackCode
    if (isSessionNotFound(error)) {
      removeSessionLocal(session.id)
      handleSessionNotFound()
      return
    }

    message.status = 'error'
    message.errorCode = code
    message.errorMessage = errorText(error)
    message.content = errorText(error)
    message.elapsedSeconds = Number(
      ((Date.now() - run.startedAt) / 1000).toFixed(1),
    )
    session.messageCount = Math.max(
      session.messageCount,
      session.messages.length,
    )
    session.updatedAt = new Date().toISOString()
  }

  async function runAssistant(run: PendingRun, message: string) {
    try {
      await streamAssistantChat(message, run.remoteSessionId, {
        signal: run.controller.signal,
        requestId: run.requestId,
        onEvent: (event) => applyStreamEvent(run, event),
      })
      if (isCurrentRun(run)) {
        failRun(
          run,
          new Error('智能服务未返回完整回答，请重试。'),
          'assistant_stream_incomplete',
        )
      }
    } catch (error) {
      if (run.controller.signal.aborted) {
        if (isCurrentRun(run)) {
          failRun(
            run,
            new Error('本次回答已中止，可以重新提问。'),
            'assistant_cancelled',
          )
        }
        return
      }
      failRun(run, error)
    }
  }

  function cancelPendingRun() {
    const run = pendingRun.value
    if (!run) {
      return
    }

    pendingRun.value = null
    run.controller.abort()
    const message = findMessage(run)
    const session = findSession(run.sessionId)
    if (message?.status === 'processing') {
      message.status = 'cancelled'
      message.content = message.content.trim()
        ? `${message.content}\n\n本次回答已中止，可以重新提问。`
        : '本次回答已中止，可以重新提问。'
    }
    if (session) {
      session.messageCount = Math.max(
        session.messageCount,
        session.messages.length,
      )
      session.updatedAt = new Date().toISOString()
    }
  }

  function startNewConversation() {
    const draftSession = currentSession.value
    cancelPendingRun()
    if (draftSession?.isDraft) {
      removeSessionLocal(draftSession.id)
    }
    messageLoadSequence += 1
    isLoadingMessages.value = false
    isLoadingMoreMessages.value = false
    activeSessionId.value = null
    draft.value = ''
  }

  function sendMessage(): boolean {
    const content = draft.value.trim()
    if (!content || isGenerating.value || isLoadingMessages.value) {
      return false
    }

    const now = new Date().toISOString()
    let session = currentSession.value
    if (!session) {
      sessionSequence += 1
      session = {
        id: `draft-session-${Date.now()}-${sessionSequence}`,
        title: sessionTitle(content),
        messageCount: 0,
        messages: [],
        createdAt: now,
        updatedAt: now,
        isDraft: true,
        hasMoreMessages: false,
        messagesLoaded: true,
      }
      sessions.value.unshift(session)
      activeSessionId.value = session.id
    } else {
      session.updatedAt = now
      moveSessionToTop(session)
    }

    messageSequence += 1
    const userMessageId = `local-user-${Date.now()}-${messageSequence}`
    const requestId = newRequestID()
    session.messages.push({
      id: userMessageId,
      requestId,
      role: 'user',
      content,
      createdAt: now,
      status: 'complete',
    })

    messageSequence += 1
    const assistantMessageId = `local-assistant-${Date.now()}-${messageSequence}`
    const assistantMessage: AssistantMessage = {
      id: assistantMessageId,
      requestId,
      role: 'assistant',
      content: '',
      createdAt: now,
      status: 'processing',
      progress: progressTemplate.map((step) => ({
        ...step,
        status: 'pending',
      })),
    }
    setProgress(assistantMessage, 0)
    session.messages.push(assistantMessage)
    draft.value = ''

    const run: PendingRun = {
      sessionId: session.id,
      remoteSessionId: session.isDraft ? undefined : session.id,
      userMessageId,
      assistantMessageId,
      controller: new AbortController(),
      startedAt: Date.now(),
      requestId,
    }
    pendingRun.value = run
    void runAssistant(run, content)
    return true
  }

  async function renameSession(
    sessionId: string,
    nextTitle: string,
  ): Promise<boolean> {
    const title = nextTitle.trim()
    const session = findSession(sessionId)
    if (!session || session.isDraft) {
      return false
    }
    if (!title) {
      sessionError.value = '会话名称不能为空。'
      return false
    }
    if (title.length > 200) {
      sessionError.value = '会话名称不能超过 200 个字符。'
      return false
    }

    try {
      const remote = await renameRemoteSession(sessionId, title)
      const target = findSession(sessionId)
      if (target) {
        Object.assign(target, mapRemoteSession(remote, target))
        sessionError.value = null
      }
      return true
    } catch (error) {
      if (isSessionNotFound(error)) {
        removeSessionLocal(sessionId)
        handleSessionNotFound()
      } else {
        sessionError.value = errorText(error)
      }
      return false
    }
  }

  async function deleteSession(sessionId: string): Promise<boolean> {
    const session = findSession(sessionId)
    if (!session) {
      return false
    }
    if (pendingRun.value?.sessionId === sessionId) {
      cancelPendingRun()
    }
    if (session.isDraft) {
      removeSessionLocal(sessionId)
      return true
    }
    try {
      await deleteRemoteSession(sessionId)
    } catch (error) {
      if (!isSessionNotFound(error)) {
        sessionError.value = errorText(error)
        return false
      }
    }
    removeSessionLocal(sessionId)
    sessionError.value = null
    return true
  }

  function clearSessionError() {
    sessionError.value = null
  }

  return {
    isOpen,
    draft,
    sessions,
    activeSessionId,
    currentSession,
    messages,
    isGenerating,
    canSend,
    isLoadingSessions,
    isLoadingMoreSessions,
    isLoadingMessages,
    isLoadingMoreMessages,
    sessionError,
    sessionHasMore,
    open,
    close,
    toggle,
    loadSessions,
    loadMoreSessions,
    loadMoreMessages,
    startNewConversation,
    selectSession,
    sendMessage,
    renameSession,
    deleteSession,
    clearSessionError,
  }
})
