import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  AssistantApiError,
  streamAssistantChat,
  type AssistantStreamEvent,
} from '@/api/modules/assistant'
import type {
  AssistantMessage,
  AssistantProgressStep,
  AssistantSession,
} from '../types'

interface PendingRun {
  sessionId: string
  assistantMessageId: string
  controller: AbortController
  startedAt: number
  requestId: string
}

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
  list_devices: '正在读取设备清单及最新读数。',
  get_site_overview: '正在汇总站点设备状态。',
  get_device_status: '正在读取设备状态与阈值评估。',
  get_telemetry: '正在读取设备时序数据。',
}

export const useAssistantStore = defineStore('assistant', () => {
  const isOpen = ref(false)
  const draft = ref('')
  const sessions = ref<AssistantSession[]>([])
  const activeSessionId = ref<string | null>(null)
  const pendingRun = ref<PendingRun | null>(null)
  const currentSession = computed(
    () =>
      sessions.value.find(
        (session) => session.id === activeSessionId.value,
      ) ?? null,
  )
  const messages = computed(() => currentSession.value?.messages ?? [])
  const isGenerating = computed(() => pendingRun.value !== null)
  const canSend = computed(
    () => draft.value.trim().length > 0 && !isGenerating.value,
  )
  let sessionSequence = 0
  let messageSequence = 0
  let requestSequence = 0

  function open() {
    isOpen.value = true
  }

  function close() {
    isOpen.value = false
  }

  function toggle() {
    isOpen.value = !isOpen.value
  }

  function makeSessionTitle(content: string): string {
    const normalized = content.replace(/\s+/g, ' ').trim()
    return normalized.length > 18
      ? `${normalized.slice(0, 18)}…`
      : normalized
  }

  function newRequestID(): string {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID()
    }
    requestSequence += 1
    return `assistant-turn-${Date.now()}-${requestSequence}`
  }

  function findSession(run: PendingRun): AssistantSession | undefined {
    return sessions.value.find((session) => session.id === run.sessionId)
  }

  function findMessage(run: PendingRun): AssistantMessage | undefined {
    return findSession(run)?.messages.find(
      (message) => message.id === run.assistantMessageId,
    )
  }

  function isCurrentRun(run: PendingRun): boolean {
    return pendingRun.value?.controller === run.controller
  }

  function setProgress(
    message: AssistantMessage,
    activeIndex: number,
  ) {
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

  function applyStreamEvent(run: PendingRun, event: AssistantStreamEvent) {
    if (!isCurrentRun(run)) {
      return
    }

    const session = findSession(run)
    const message = findMessage(run)
    if (!session || !message || message.status !== 'processing') {
      return
    }

    switch (event.type) {
      case 'session':
        session.remoteSessionId = event.sessionId
        setProgress(message, 1)
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

        if (event.message.sessionId) {
          session.remoteSessionId = event.message.sessionId
        }
        message.status = 'complete'
        message.content = finalContent
        message.citations =
          event.message.citations.length > 0
            ? event.message.citations
            : message.citations
        message.elapsedSeconds = Number(
          ((Date.now() - run.startedAt) / 1000).toFixed(1),
        )
        completeProgress(message)
        session.updatedAt = new Date().toISOString()
        pendingRun.value = null
        break
      }
      case 'unknown':
        break
    }
  }

  function messageErrorText(error: unknown): string {
    if (error instanceof AssistantApiError) {
      return error.message
    }
    if (error instanceof Error && error.message.trim()) {
      return error.message
    }
    return '智能服务暂时不可用，请稍后重试。'
  }

  function failRun(
    run: PendingRun,
    error: unknown,
    fallbackCode = 'assistant_request_failed',
  ) {
    if (!isCurrentRun(run)) {
      return
    }

    const session = findSession(run)
    const message = findMessage(run)
    pendingRun.value = null
    if (!session || !message || message.status !== 'processing') {
      return
    }

    const code =
      error instanceof AssistantApiError ? error.code : fallbackCode
    if (code === 'session_not_found') {
      session.remoteSessionId = undefined
    }

    message.status = 'error'
    message.errorCode = code
    message.content = messageErrorText(error)
    message.elapsedSeconds = Number(
      ((Date.now() - run.startedAt) / 1000).toFixed(1),
    )
    session.updatedAt = new Date().toISOString()
  }

  async function runAssistant(
    run: PendingRun,
    message: string,
    remoteSessionId: string | undefined,
  ) {
    try {
      await streamAssistantChat(message, remoteSessionId, {
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
    if (message?.status === 'processing') {
      message.status = 'cancelled'
      message.content = message.content.trim()
        ? `${message.content}\n\n本次回答已中止，可以重新提问。`
        : '本次回答已中止，可以重新提问。'
    }
  }

  function startNewConversation() {
    cancelPendingRun()
    activeSessionId.value = null
    draft.value = ''
  }

  function selectSession(sessionId: string) {
    if (!sessions.value.some((session) => session.id === sessionId)) {
      return
    }
    if (sessionId !== activeSessionId.value) {
      cancelPendingRun()
    }
    activeSessionId.value = sessionId
  }

  function sendMessage(): boolean {
    const content = draft.value.trim()
    if (!content || isGenerating.value) {
      return false
    }

    const now = new Date().toISOString()
    let session = currentSession.value
    if (!session) {
      sessionSequence += 1
      session = {
        id: `assistant-session-${sessionSequence}`,
        title: makeSessionTitle(content),
        messages: [],
        createdAt: now,
        updatedAt: now,
      }
      sessions.value.unshift(session)
      activeSessionId.value = session.id
    } else {
      session.updatedAt = now
      moveSessionToTop(session)
    }

    messageSequence += 1
    const requestId = newRequestID()
    session.messages.push({
      id: `assistant-message-${messageSequence}`,
      requestId,
      role: 'user',
      content,
      createdAt: now,
      status: 'complete',
    })

    messageSequence += 1
    const assistantMessage: AssistantMessage = {
      id: `assistant-message-${messageSequence}`,
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
      assistantMessageId: assistantMessage.id,
      controller: new AbortController(),
      startedAt: Date.now(),
      requestId,
    }
    pendingRun.value = run
    void runAssistant(run, content, session.remoteSessionId)
    return true
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
    open,
    close,
    toggle,
    startNewConversation,
    selectSession,
    sendMessage,
  }
})
