<script setup lang="ts">
import {
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import { renderMarkdown } from '../markdown'
import { ASSISTANT_PROMPTS } from '../prompts'
import { useAssistantStore } from '../stores/assistant'
import type { AssistantCitation, AssistantSession } from '../types'

const assistant = useAssistantStore()
const composer = ref<HTMLTextAreaElement | null>(null)
const messageList = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const historyPanel = ref<HTMLElement | null>(null)
const deleteDialog = ref<HTMLElement | null>(null)
const historyDrawerOpen = ref(false)
const editingSessionId = ref<string | null>(null)
const renameDraft = ref('')
const renameSaving = ref(false)
const deleteCandidate = ref<AssistantSession | null>(null)
const deleteSaving = ref(false)
let previousBodyOverflow = ''

const focusableSelector = [
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function formatCitationMeta(citation: AssistantCitation): string {
  return [citation.source, citation.section, citation.version]
    .filter((value) => value.trim().length > 0)
    .join(' · ')
}

function scrollToLatest(smooth = true) {
  void nextTick(() => {
    if (!messageList.value) {
      return
    }

    const reduceMotion = window.matchMedia(
      '(prefers-reduced-motion: reduce)',
    ).matches

    messageList.value.scrollTo({
      top: messageList.value.scrollHeight,
      behavior: smooth && !reduceMotion ? 'smooth' : 'auto',
    })
  })
}

async function submitMessage() {
  if (!assistant.sendMessage()) {
    return
  }

  scrollToLatest()
  await nextTick()
  composer.value?.focus()
}

function usePrompt(prompt: string) {
  assistant.draft = prompt
  void submitMessage()
}

async function startNewConversation() {
  assistant.startNewConversation()
  historyDrawerOpen.value = false
  await nextTick()
  scrollToLatest(false)
  composer.value?.focus()
}

async function selectSession(sessionId: string) {
  await assistant.selectSession(sessionId)
  historyDrawerOpen.value = false
  await nextTick()
  scrollToLatest(false)
  composer.value?.focus()
}

async function beginRename(session: AssistantSession) {
  editingSessionId.value = session.id
  renameDraft.value = session.title
  await nextTick()
  const input =
    historyPanel.value?.querySelector<HTMLInputElement>(
      '.assistant-history__rename input',
    )
  input?.focus()
  input?.select()
}

function cancelRename() {
  editingSessionId.value = null
  renameDraft.value = ''
  renameSaving.value = false
}

async function saveRename() {
  const sessionId = editingSessionId.value
  if (!sessionId || renameSaving.value) {
    return
  }
  renameSaving.value = true
  const saved = await assistant.renameSession(sessionId, renameDraft.value)
  renameSaving.value = false
  if (saved || !assistant.sessions.some((session) => session.id === sessionId)) {
    cancelRename()
  }
}

async function requestDelete(session: AssistantSession) {
  deleteCandidate.value = session
  await nextTick()
  deleteDialog.value?.querySelector<HTMLElement>('button')?.focus()
}

async function confirmDelete() {
  const session = deleteCandidate.value
  if (!session || deleteSaving.value) {
    return
  }
  deleteSaving.value = true
  const deleted = await assistant.deleteSession(session.id)
  deleteSaving.value = false
  if (deleted) {
    deleteCandidate.value = null
    historyDrawerOpen.value = false
    await nextTick()
    scrollToLatest(false)
    composer.value?.focus()
  }
}

async function toggleHistoryDrawer() {
  historyDrawerOpen.value = !historyDrawerOpen.value

  if (historyDrawerOpen.value) {
    await nextTick()
    historyPanel.value
      ?.querySelector<HTMLElement>('button')
      ?.focus()
  }
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (
    event.key === 'Enter' &&
    !event.shiftKey &&
    !event.isComposing
  ) {
    event.preventDefault()
    void submitMessage()
  }
}

function getFocusableElements(): HTMLElement[] {
  const root = deleteCandidate.value ? deleteDialog.value : panel.value
  if (!root) {
    return []
  }

  return Array.from(
    root.querySelectorAll<HTMLElement>(focusableSelector),
  ).filter((element) => element.offsetParent !== null)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (!assistant.isOpen) {
    return
  }

  if (event.key === 'Escape') {
    event.preventDefault()

    if (deleteCandidate.value) {
      deleteCandidate.value = null
      return
    }

    if (editingSessionId.value) {
      cancelRename()
      return
    }

    if (historyDrawerOpen.value) {
      historyDrawerOpen.value = false
      return
    }

    assistant.close()
    return
  }

  if (event.key !== 'Tab') {
    return
  }

  const focusable = getFocusableElements()
  if (focusable.length === 0) {
    event.preventDefault()
    return
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement

  if (event.shiftKey && (active === first || !panel.value?.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(
  () => assistant.currentSession?.id,
  () => {
    scrollToLatest(false)
  },
)

watch(
  () => assistant.messages,
  () => {
    scrollToLatest(assistant.isGenerating)
  },
  { deep: true },
)

onMounted(() => {
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', handleWindowKeydown)
  void nextTick(() => composer.value?.focus())
})

onBeforeUnmount(() => {
  document.body.style.overflow = previousBodyOverflow
  window.removeEventListener('keydown', handleWindowKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="assistant-modal">
      <div
        v-if="assistant.isOpen"
        class="assistant-mask"
        @mousedown.self="assistant.close"
      >
        <section
          ref="panel"
          class="assistant-window"
          role="dialog"
          aria-modal="true"
          aria-labelledby="assistant-panel-title"
        >
          <button
            v-if="historyDrawerOpen"
            type="button"
            class="assistant-history-scrim"
            aria-label="关闭会话历史"
            @click="historyDrawerOpen = false"
          ></button>

          <aside
            id="assistant-history"
            ref="historyPanel"
            class="assistant-sidebar"
            :class="{ 'assistant-sidebar--open': historyDrawerOpen }"
            aria-label="智能助手会话历史"
          >
            <div class="assistant-brand">
              <span class="assistant-brand__icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none">
                  <rect
                    x="3.5"
                    y="6"
                    width="17"
                    height="13"
                    rx="4"
                    stroke="currentColor"
                    stroke-width="1.7"
                  />
                  <path
                    d="M12 2.8V6M8.2 11H8.1M15.8 11H15.7M8 15H16"
                    stroke="currentColor"
                    stroke-width="1.7"
                    stroke-linecap="round"
                  />
                </svg>
              </span>
              <b>MES 智能助手</b>
            </div>

            <button
              type="button"
              class="assistant-new-chat"
              @click="startNewConversation"
            >
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M12 5V19M5 12H19"
                  stroke="currentColor"
                  stroke-width="1.8"
                  stroke-linecap="round"
                />
              </svg>
              新建对话
            </button>

            <small class="assistant-history-label">最近对话</small>

            <div class="assistant-history">
              <p v-if="assistant.isLoadingSessions && assistant.sessions.length === 0">
                正在加载对话…
              </p>
              <p
                v-else-if="
                  assistant.sessionError && assistant.sessions.length === 0
                "
                class="assistant-history__error"
              >
                {{ assistant.sessionError }}
                <button type="button" @click="assistant.loadSessions(true)">
                  重试
                </button>
              </p>
              <p v-else-if="assistant.sessions.length === 0">
                暂无历史对话
              </p>
              <div
                v-for="session in assistant.sessions"
                v-else
                :key="session.id"
                class="assistant-history__row"
              >
                <form
                  v-if="editingSessionId === session.id"
                  class="assistant-history__rename"
                  @submit.prevent="saveRename"
                  @keydown.esc.prevent="cancelRename"
                >
                  <input
                    v-model="renameDraft"
                    type="text"
                    maxlength="200"
                    aria-label="会话名称"
                    :disabled="renameSaving"
                  />
                  <button type="submit" :disabled="renameSaving">
                    {{ renameSaving ? '保存中' : '保存' }}
                  </button>
                  <button type="button" :disabled="renameSaving" @click="cancelRename">
                    取消
                  </button>
                </form>
                <template v-else>
                  <button
                    type="button"
                    class="assistant-history__item"
                    :class="{
                      'assistant-history__item--active':
                        session.id === assistant.activeSessionId,
                    }"
                    :aria-current="
                      session.id === assistant.activeSessionId
                        ? 'true'
                        : undefined
                    "
                    @click="selectSession(session.id)"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path
                        d="M6.5 4.5H15.5L19 8V19.5H6.5V4.5Z"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linejoin="round"
                      />
                      <path
                        d="M15.3 4.7V8.3H19M9.5 12H16M9.5 15.5H14"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                      />
                    </svg>
                    <span>
                      <b>{{ session.title }}</b>
                    </span>
                  </button>
                  <div class="assistant-history__actions">
                    <button
                      v-if="!session.isDraft"
                      type="button"
                      aria-label="重命名会话"
                      title="重命名"
                      @click="beginRename(session)"
                    >
                      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M5 16.8V19H7.2L16.8 9.4L14.6 7.2L5 16.8Z"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                        <path
                          d="M13.4 8.4L15.6 10.6"
                          stroke="currentColor"
                          stroke-width="1.5"
                        />
                      </svg>
                    </button>
                    <button
                      type="button"
                      aria-label="删除会话"
                      title="删除"
                      @click="requestDelete(session)"
                    >
                      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M7 8H17M9 8V6H15V8M9 11V17M12 11V17M15 11V17M8 8L9 19H15L16 8"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </button>
                  </div>
                </template>
              </div>
              <button
                v-if="assistant.sessionHasMore"
                type="button"
                class="assistant-history__more"
                :disabled="assistant.isLoadingMoreSessions"
                @click="assistant.loadMoreSessions()"
              >
                {{
                  assistant.isLoadingMoreSessions ? '加载中…' : '加载更多对话'
                }}
              </button>
            </div>

            <div class="assistant-knowledge">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M4.5 6.2L12 3.8L19.5 6.2V17.8L12 20.2L4.5 17.8V6.2Z"
                  stroke="currentColor"
                  stroke-width="1.5"
                  stroke-linejoin="round"
                />
                <path
                  d="M4.8 6.4L12 9L19.2 6.4M12 9V20"
                  stroke="currentColor"
                  stroke-width="1.5"
                  stroke-linejoin="round"
                />
              </svg>
              <div>MES 系统知识库</div>
            </div>
          </aside>

          <div
            v-if="deleteCandidate"
            class="assistant-confirm-scrim"
            @mousedown.self="deleteCandidate = null"
          >
            <section
              ref="deleteDialog"
              class="assistant-confirm"
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="assistant-delete-title"
            >
              <h3 id="assistant-delete-title">删除这条对话？</h3>
              <p>“{{ deleteCandidate.title }}”及其历史消息将从会话列表中移除。</p>
              <div>
                <button
                  type="button"
                  :disabled="deleteSaving"
                  @click="deleteCandidate = null"
                >
                  取消
                </button>
                <button
                  type="button"
                  class="danger"
                  :disabled="deleteSaving"
                  @click="confirmDelete"
                >
                  {{ deleteSaving ? '删除中…' : '删除' }}
                </button>
              </div>
            </section>
          </div>

          <div class="assistant-main">
            <header class="assistant-main__header">
              <div class="assistant-main__title">
                <button
                  type="button"
                  class="assistant-history-toggle"
                  aria-label="打开会话历史"
                  aria-controls="assistant-history"
                  :aria-expanded="historyDrawerOpen"
                  @click="toggleHistoryDrawer"
                >
                  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path
                      d="M5 7H19M5 12H15M5 17H19"
                      stroke="currentColor"
                      stroke-width="1.8"
                      stroke-linecap="round"
                    />
                  </svg>
                </button>
                <div>
                  <h2 id="assistant-panel-title">知识问答与维护分析</h2>
                  <p>
                    {{
                      assistant.currentSession?.title ||
                      '当前工作台 · 设备预测性维护'
                    }}
                  </p>
                </div>
              </div>

              <button
                type="button"
                class="assistant-close"
                aria-label="关闭智能助手"
                @click="assistant.close"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d="M6.5 6.5L17.5 17.5M17.5 6.5L6.5 17.5"
                    stroke="currentColor"
                    stroke-width="1.7"
                    stroke-linecap="round"
                  />
                </svg>
              </button>
            </header>

            <div ref="messageList" class="assistant-content">
              <div
                v-if="assistant.sessionError"
                class="assistant-session-notice"
                role="status"
              >
                <span>{{ assistant.sessionError }}</span>
                <button type="button" @click="assistant.clearSessionError()">
                  关闭
                </button>
              </div>
              <div
                v-if="assistant.currentSession && assistant.isLoadingMessages"
                class="assistant-message-loading"
              >
                正在加载历史消息…
              </div>
              <div
                v-if="
                  assistant.messages.length === 0 &&
                  !assistant.isLoadingMessages
                "
                class="assistant-welcome"
              >
                <span class="assistant-welcome__icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect
                      x="3.5"
                      y="6"
                      width="17"
                      height="13"
                      rx="4"
                      stroke="currentColor"
                      stroke-width="1.6"
                    />
                    <path
                      d="M12 2.8V6M8.2 11H8.1M15.8 11H15.7M8 15H16"
                      stroke="currentColor"
                      stroke-width="1.6"
                      stroke-linecap="round"
                    />
                  </svg>
                </span>
                <h3>有什么可以帮你？</h3>
                <p>查询维护知识，解读设备事件，获取可核查的维护建议。</p>

                <div class="assistant-prompts">
                  <button
                    v-for="prompt in ASSISTANT_PROMPTS"
                    :key="prompt.id"
                    type="button"
                    class="assistant-prompt"
                    @click="usePrompt(prompt.prompt)"
                  >
                    <span class="assistant-prompt__icon" aria-hidden="true">
                      <svg
                        v-if="prompt.icon === 'maintenance'"
                        viewBox="0 0 24 24"
                        fill="none"
                      >
                        <path
                          d="M12 20.3C8.1 17.7 4.8 14.6 4.8 10.5C4.8 7.9 6.7 6.2 9 6.2C10.4 6.2 11.5 6.8 12 7.8C12.5 6.8 13.6 6.2 15 6.2C17.3 6.2 19.2 7.9 19.2 10.5C19.2 14.6 15.9 17.7 12 20.3Z"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                        <path
                          d="M8 12H10L11.2 9.8L13 14L14.2 12H16"
                          stroke="currentColor"
                          stroke-width="1.4"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                      <svg
                        v-else-if="prompt.icon === 'event'"
                        viewBox="0 0 24 24"
                        fill="none"
                      >
                        <path
                          d="M5 19V5H19V19H5Z"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                        <path
                          d="M8 15L11 11.6L13.2 13.4L16.5 9"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                      <svg
                        v-else-if="prompt.icon === 'detector'"
                        viewBox="0 0 24 24"
                        fill="none"
                      >
                        <circle
                          cx="12"
                          cy="12"
                          r="2.2"
                          stroke="currentColor"
                          stroke-width="1.5"
                        />
                        <path
                          d="M7.8 7.8C5.5 10.1 5.5 13.9 7.8 16.2M16.2 7.8C18.5 10.1 18.5 13.9 16.2 16.2M4.8 4.8C1.2 8.4 1.2 15.6 4.8 19.2M19.2 4.8C22.8 8.4 22.8 15.6 19.2 19.2"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linecap="round"
                        />
                      </svg>
                      <svg
                        v-else
                        viewBox="0 0 24 24"
                        fill="none"
                      >
                        <path
                          d="M4.5 6.2L12 3.8L19.5 6.2V17.8L12 20.2L4.5 17.8V6.2Z"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                        <path
                          d="M4.8 6.4L12 9L19.2 6.4M12 9V20"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </span>
                    <span class="assistant-prompt__copy">
                      <b>{{ prompt.title }}</b>
                      <span>{{ prompt.description }}</span>
                    </span>
                    <svg
                      class="assistant-prompt__arrow"
                      viewBox="0 0 24 24"
                      fill="none"
                      aria-hidden="true"
                    >
                      <path
                        d="M8 16L16 8M10 8H16V14"
                        stroke="currentColor"
                        stroke-width="1.4"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      />
                    </svg>
                  </button>
                </div>
              </div>

              <div v-else class="assistant-conversation">
                <button
                  v-if="assistant.currentSession?.hasMoreMessages"
                  type="button"
                  class="assistant-load-older"
                  :disabled="assistant.isLoadingMoreMessages"
                  @click="assistant.loadMoreMessages()"
                >
                  {{
                    assistant.isLoadingMoreMessages
                      ? '正在加载更早消息…'
                      : '加载更早消息'
                  }}
                </button>
                <template
                  v-for="message in assistant.messages"
                  :key="message.id"
                >
                  <article
                    v-if="message.role === 'user'"
                    class="assistant-user-message"
                    :data-message-id="message.id"
                    :data-request-id="message.requestId"
                  >
                    <span class="assistant-user-message__avatar" aria-hidden="true">
                      <svg viewBox="0 0 24 24" fill="none">
                        <circle
                          cx="12"
                          cy="8"
                          r="3.2"
                          stroke="currentColor"
                          stroke-width="1.5"
                        />
                        <path
                          d="M5.5 19C6.3 15.8 8.7 14.2 12 14.2C15.3 14.2 17.7 15.8 18.5 19"
                          stroke="currentColor"
                          stroke-width="1.5"
                          stroke-linecap="round"
                        />
                      </svg>
                    </span>
                    <div class="assistant-user-message__body">
                      <p>{{ message.content }}</p>
                    </div>
                  </article>

                  <article
                    v-else-if="message.status === 'processing'"
                    class="assistant-progress"
                    :data-message-id="message.id"
                    :data-request-id="message.requestId"
                  >
                    <header>
                      <span class="assistant-progress__title">
                        <span class="assistant-spinner" aria-hidden="true">
                          <svg viewBox="0 0 24 24" fill="none">
                            <path
                              d="M20 12A8 8 0 1 1 16.2 5"
                              stroke="currentColor"
                              stroke-width="2"
                              stroke-linecap="round"
                            />
                          </svg>
                        </span>
                        <span>
                          <b>正在分析</b>
                        </span>
                      </span>
                    </header>

                    <ol>
                      <li
                        v-for="(step, index) in message.progress"
                        :key="step.id"
                        :class="`assistant-progress__step--${step.status}`"
                      >
                        <span class="assistant-progress__mark">
                          <svg
                            v-if="step.status === 'done'"
                            viewBox="0 0 24 24"
                            fill="none"
                          >
                            <path
                              d="M6 12.5L10 16.3L18 7.8"
                              stroke="currentColor"
                              stroke-width="2"
                              stroke-linecap="round"
                              stroke-linejoin="round"
                            />
                          </svg>
                          <span
                            v-else-if="step.status === 'working'"
                            class="assistant-progress__dot"
                          ></span>
                          <template v-else>{{ index + 1 }}</template>
                        </span>
                        <span>
                          <b>{{ step.label }}</b>
                          <small>{{ step.detail }}</small>
                        </span>
                      </li>
                    </ol>

                    <div
                      v-if="message.content"
                      class="assistant-answer__content assistant-answer__content--streaming"
                      aria-live="polite"
                      v-html="renderMarkdown(message.content)"
                    ></div>

                  </article>

                  <article
                    v-else-if="message.status === 'cancelled'"
                    class="assistant-cancelled"
                    :data-message-id="message.id"
                    :data-request-id="message.requestId"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <circle
                        cx="12"
                        cy="12"
                        r="8"
                        stroke="currentColor"
                        stroke-width="1.5"
                      />
                      <path
                        d="M9 9L15 15M15 9L9 15"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                      />
                    </svg>
                    {{ message.content }}
                  </article>

                  <article
                    v-else-if="message.status === 'error'"
                    class="assistant-error"
                    role="alert"
                    :data-message-id="message.id"
                    :data-request-id="message.requestId"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <circle
                        cx="12"
                        cy="12"
                        r="8"
                        stroke="currentColor"
                        stroke-width="1.5"
                      />
                      <path
                        d="M12 7.8V12.5M12 16.1V16.2"
                        stroke="currentColor"
                        stroke-width="1.7"
                        stroke-linecap="round"
                      />
                    </svg>
                    <div>
                      <b>本次回答未完成</b>
                      <p>{{ message.content }}</p>
                    </div>
                  </article>

                  <article
                    v-else
                    class="assistant-answer"
                    :data-message-id="message.id"
                    :data-request-id="message.requestId"
                  >
                    <div class="assistant-answer__identity">
                      <span class="assistant-answer__avatar" aria-hidden="true">
                        <svg viewBox="0 0 24 24" fill="none">
                          <rect
                            x="3.5"
                            y="6"
                            width="17"
                            height="13"
                            rx="4"
                            stroke="currentColor"
                            stroke-width="1.6"
                          />
                          <path
                            d="M12 2.8V6M8.2 11H8.1M15.8 11H15.7M8 15H16"
                            stroke="currentColor"
                            stroke-width="1.6"
                            stroke-linecap="round"
                          />
                        </svg>
                      </span>
                    </div>

                    <div class="assistant-answer__body">
                    <div
                      class="assistant-answer__content"
                      v-html="renderMarkdown(message.content)"
                    ></div>

                    <section
                      v-if="message.citations?.length"
                      class="assistant-citations"
                    >
                      <header>
                        <h4>引用来源</h4>
                        <span>{{ message.citations.length }} 条</span>
                      </header>
                      <ul>
                        <li
                          v-for="(citation, index) in message.citations"
                          :key="citation.id"
                        >
                          <span class="assistant-citations__index">
                            {{ String(index + 1).padStart(2, '0') }}
                          </span>
                          <div>
                            <b>{{ citation.title }}</b>
                            <small v-if="formatCitationMeta(citation)">
                              {{ formatCitationMeta(citation) }}
                            </small>
                          </div>
                        </li>
                      </ul>
                    </section>
                    </div>

                  </article>
                </template>
              </div>
            </div>

            <form class="assistant-compose" @submit.prevent="submitMessage">
              <div class="assistant-compose__box">
                <label>
                  <span class="assistant-visually-hidden">向智能助手提问</span>
                  <textarea
                    ref="composer"
                    v-model="assistant.draft"
                    rows="3"
                    placeholder="向 MES 智能助手提问，例如：请分析 AS06 备电续航下降事件"
                    aria-label="输入问题"
                    @keydown="handleComposerKeydown"
                  ></textarea>
                </label>
                <div class="assistant-compose__footer">
                  <button
                    type="submit"
                    :disabled="!assistant.canSend"
                    aria-label="发送问题"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path
                        d="M4 11.5L20 4.5L14.2 20L10.8 13.2L4 11.5Z"
                        stroke="currentColor"
                        stroke-width="1.6"
                        stroke-linejoin="round"
                      />
                      <path
                        d="M10.8 13.2L20 4.5"
                        stroke="currentColor"
                        stroke-width="1.6"
                        stroke-linecap="round"
                      />
                    </svg>
                  </button>
                </div>
              </div>
              <small>分析结果供人工核查参考</small>
            </form>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.assistant-mask {
  position: fixed;
  z-index: 100;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: rgb(8 31 56 / 50%);
  backdrop-filter: blur(4px);
}

.assistant-window {
  position: relative;
  display: grid;
  width: min(1200px, 96vw);
  height: min(900px, calc(100dvh - 64px));
  grid-template-columns: 220px minmax(0, 1fr);
  overflow: hidden;
  border: 1px solid #dce6ef;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 58px rgb(5 31 56 / 28%);
}

.assistant-sidebar {
  display: flex;
  min-height: 0;
  flex-direction: column;
  padding: 25px 18px 20px;
  border-right: 1px solid #e4eaf0;
  background: #f3f6fa;
}

.assistant-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 27px;
  color: #165fa4;
  font-size: 16px;
}

.assistant-brand b {
  font-weight: 650;
}

.assistant-brand__icon {
  display: grid;
  width: 30px;
  height: 30px;
  flex: none;
  place-items: center;
}

.assistant-brand__icon svg {
  width: 28px;
  height: 28px;
}

.assistant-new-chat {
  display: flex;
  width: 100%;
  min-height: 44px;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin-bottom: 29px;
  border: 1px solid #c8d9eb;
  border-radius: 7px;
  background: #fff;
  color: #1765ae;
  font-size: 14px;
  transition:
    border-color 150ms ease,
    background 150ms ease;
}

.assistant-new-chat:hover {
  border-color: #9ebedc;
  background: #f8fbfe;
}

.assistant-new-chat svg {
  width: 18px;
  height: 18px;
}

.assistant-history-label {
  margin: 0 10px 12px;
  color: #74869a;
  font-size: 12px;
}

.assistant-history {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
}

.assistant-history > p {
  margin: 0;
  padding: 0 10px;
  color: #8b99a9;
  font-size: 13px;
  line-height: 1.7;
}

.assistant-history > p button {
  margin-left: 8px;
  padding: 2px 6px;
  border: 0;
  background: transparent;
  color: #176fc5;
  font-size: 12px;
}

.assistant-history__error {
  color: #985858 !important;
}

.assistant-history__row {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 2px;
  margin-bottom: 5px;
  border-radius: 6px;
}

.assistant-history__row:hover,
.assistant-history__row:focus-within {
  background: #eaf1f8;
}

.assistant-history__item {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  padding: 11px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #566d83;
  font-size: 13px;
  text-align: left;
}

.assistant-history__item:hover {
  background: transparent;
}

.assistant-history__item--active {
  background: #e3edf8;
  color: #1765ae;
}

.assistant-history__item svg {
  width: 16px;
  height: 16px;
  flex: none;
}

.assistant-history__item span {
  display: grid;
  min-width: 0;
  overflow: hidden;
  gap: 4px;
}

.assistant-history__item b {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-history__item b {
  font-size: 13px;
  font-weight: 500;
}

.assistant-history__actions {
  display: flex;
  align-items: center;
  gap: 1px;
  padding-right: 4px;
  opacity: 0;
  transition: opacity 120ms ease;
}

.assistant-history__row:hover .assistant-history__actions,
.assistant-history__row:focus-within .assistant-history__actions {
  opacity: 1;
}

.assistant-history__actions button {
  display: grid;
  width: 25px;
  height: 25px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: #6f8498;
}

.assistant-history__actions button:hover {
  background: #dfe9f3;
  color: #245f91;
}

.assistant-history__actions svg {
  width: 15px;
  height: 15px;
}

.assistant-history__rename {
  display: grid;
  min-width: 0;
  grid-column: 1 / -1;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 5px;
  padding: 7px;
  border-radius: 6px;
  background: #e8f0f8;
}

.assistant-history__rename input {
  min-width: 0;
  padding: 6px 7px;
  border: 1px solid #b8cee1;
  border-radius: 5px;
  outline: 0;
  color: #35536d;
  font-size: 12px;
}

.assistant-history__rename input:focus {
  border-color: #5f94c3;
  box-shadow: 0 0 0 2px #d6e8f7;
}

.assistant-history__rename button,
.assistant-history__more {
  border: 0;
  border-radius: 5px;
  background: #fff;
  color: #3e6d93;
  font-size: 11px;
}

.assistant-history__rename button {
  padding: 0 7px;
}

.assistant-history__more {
  width: 100%;
  margin-top: 6px;
  padding: 8px;
  background: #e9f0f7;
}

.assistant-history__rename button:disabled,
.assistant-history__more:disabled {
  opacity: 0.6;
}

.assistant-knowledge {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: 18px;
  padding: 18px 5px 0;
  border-top: 1px solid #dce5ed;
  color: #536f89;
  font-size: 13px;
}

.assistant-knowledge svg {
  width: 20px;
  height: 20px;
  flex: none;
  color: #637e97;
}

.assistant-history-scrim {
  display: none;
  border: 0;
}

.assistant-confirm-scrim {
  position: absolute;
  z-index: 6;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgb(20 42 62 / 24%);
}

.assistant-confirm {
  width: min(390px, 100%);
  padding: 24px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 18px 48px rgb(16 40 62 / 22%);
}

.assistant-confirm h3 {
  margin: 0;
  color: #28465e;
  font-size: 18px;
}

.assistant-confirm p {
  margin: 12px 0 22px;
  color: #6f8395;
  font-size: 13px;
  line-height: 1.75;
}

.assistant-confirm > div {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}

.assistant-confirm button {
  padding: 8px 16px;
  border: 1px solid #d6e1ea;
  border-radius: 7px;
  background: #fff;
  color: #4c667d;
  font-size: 13px;
}

.assistant-confirm button.danger {
  border-color: #bc5b5b;
  background: #b85252;
  color: #fff;
}

.assistant-confirm button:disabled {
  opacity: 0.6;
}

.assistant-main {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-rows: auto minmax(0, 1fr) auto;
}

.assistant-main__header {
  display: flex;
  min-height: 76px;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 16px 27px;
  border-bottom: 1px solid #edf1f5;
}

.assistant-main__title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
}

.assistant-main__title h2 {
  margin: 0;
  overflow: hidden;
  color: #243f57;
  font-size: 15px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-main__title p {
  max-width: 620px;
  margin: 6px 0 0;
  overflow: hidden;
  color: #8092a4;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-history-toggle {
  display: none;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border: 1px solid #d9e4ee;
  border-radius: 7px;
  background: #fff;
  color: #4d6f8c;
}

.assistant-history-toggle svg {
  width: 20px;
  height: 20px;
}

.assistant-close {
  display: grid;
  width: 36px;
  height: 36px;
  flex: none;
  place-items: center;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #607589;
}

.assistant-close:hover {
  background: #f1f5f9;
  color: #294e6c;
}

.assistant-close svg {
  width: 23px;
  height: 23px;
}

.assistant-content {
  min-height: 0;
  overflow-y: auto;
  padding: 28px 44px 30px;
  scrollbar-color: #bdcbd8 transparent;
  scrollbar-width: thin;
}

.assistant-session-notice {
  display: flex;
  max-width: 820px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 auto 18px;
  padding: 10px 13px;
  border: 1px solid #eed5d5;
  border-radius: 7px;
  background: #fff8f8;
  color: #8a5353;
  font-size: 12px;
}

.assistant-session-notice button {
  border: 0;
  background: transparent;
  color: #885454;
  font-size: 12px;
}

.assistant-message-loading {
  max-width: 820px;
  margin: 34px auto;
  color: #8294a5;
  font-size: 13px;
  text-align: center;
}

.assistant-load-older {
  display: block;
  margin: 0 auto 22px;
  padding: 7px 14px;
  border: 1px solid #dbe6ef;
  border-radius: 999px;
  background: #f8fafc;
  color: #5b7891;
  font-size: 12px;
}

.assistant-load-older:disabled {
  opacity: 0.6;
}

.assistant-welcome {
  max-width: 700px;
  margin: 34px auto 0;
  text-align: center;
}

.assistant-welcome__icon {
  display: inline-grid;
  width: 64px;
  height: 64px;
  place-items: center;
  border-radius: 18px;
  background: #dcecff;
  color: #1b73cb;
}

.assistant-welcome__icon svg {
  width: 36px;
  height: 36px;
}

.assistant-welcome h3 {
  margin: 24px 0 11px;
  color: #203b54;
  font-size: 28px;
  font-weight: 650;
  letter-spacing: -0.3px;
}

.assistant-welcome > p {
  margin: 0;
  color: #74879b;
  font-size: 14px;
  line-height: 1.8;
}

.assistant-prompts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-top: 33px;
  text-align: left;
}

.assistant-prompt {
  position: relative;
  display: grid;
  min-height: 92px;
  grid-template-columns: 28px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  padding: 18px 32px 18px 16px;
  border: 1px solid #e1e9f1;
  border-radius: 9px;
  background: #fff;
  color: #2e5577;
  text-align: left;
  transition:
    border-color 150ms ease,
    box-shadow 150ms ease,
    transform 150ms ease;
}

.assistant-prompt:hover {
  border-color: #a9c7e1;
  box-shadow: 0 8px 22px rgb(32 80 118 / 8%);
  transform: translateY(-1px);
}

.assistant-prompt__icon {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
}

.assistant-prompt__icon svg {
  width: 24px;
  height: 24px;
}

.assistant-prompt__copy {
  display: grid;
  gap: 7px;
  min-width: 0;
}

.assistant-prompt__copy b {
  color: #294e6d;
  font-size: 14px;
  font-weight: 600;
}

.assistant-prompt__copy > span {
  color: #7b8e9f;
  font-size: 13px;
  line-height: 1.65;
}

.assistant-prompt__arrow {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 15px;
  height: 15px;
  color: #8cabc4;
}

.assistant-conversation {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 8px;
}

.assistant-user-message {
  display: flex;
  flex-direction: row-reverse;
  align-items: flex-start;
  justify-content: flex-start;
  gap: 12px;
  margin: 2px 0 28px;
}

.assistant-user-message__avatar {
  display: grid;
  width: 32px;
  height: 32px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: #edf3f8;
  color: #6283a2;
}

.assistant-user-message__avatar svg {
  width: 22px;
  height: 22px;
}

.assistant-user-message__body {
  display: grid;
  max-width: min(72%, 580px);
  min-width: 0;
  justify-items: end;
}

.assistant-user-message__body p {
  margin: 0;
  padding: 8px 12px;
  border-radius: 12px 4px 12px 12px;
  background: #f0f5fa;
  color: #38546c;
  font-size: 13px;
  line-height: 1.7;
  text-align: left;
  white-space: pre-wrap;
  word-break: break-word;
}

.assistant-progress {
  max-width: 720px;
  margin: 4px auto 28px;
}

.assistant-progress > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  color: #236aa7;
}

.assistant-progress__title {
  display: flex;
  align-items: center;
  gap: 11px;
}

.assistant-progress__title b {
  font-size: 16px;
  font-weight: 650;
}

.assistant-spinner {
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  color: #347eb8;
}

.assistant-spinner svg {
  width: 22px;
  height: 22px;
  animation: assistant-spin 1.4s linear infinite;
}

.assistant-progress ol {
  display: grid;
  gap: 0;
  margin: 23px 0 0 10px;
  padding: 0;
  list-style: none;
}

.assistant-progress li {
  position: relative;
  display: flex;
  gap: 13px;
  margin-left: 1px;
  padding: 0 0 20px 23px;
  border-left: 1px solid #dce6ef;
  color: #8b9aa8;
}

.assistant-progress li:last-child {
  border-left-color: transparent;
}

.assistant-progress__mark {
  position: absolute;
  top: 0;
  left: -10px;
  display: grid;
  width: 20px;
  height: 22px;
  place-items: center;
  background: #fff;
  color: #8798a8;
  font-size: 10px;
  font-style: normal;
}

.assistant-progress__mark svg {
  width: 19px;
  height: 19px;
  color: #3d8a70;
}

.assistant-progress__dot {
  width: 8px;
  height: 8px;
  border: 2px solid #f3f7fb;
  border-radius: 50%;
  background: #2776b5;
  box-shadow: 0 0 0 1px #2776b5;
}

.assistant-progress li > span:last-child {
  display: grid;
  gap: 6px;
}

.assistant-progress li b {
  color: #657b8e;
  font-size: 13px;
  font-weight: 600;
}

.assistant-progress li small {
  color: #8d9dab;
  font-size: 12px;
  line-height: 1.7;
}

.assistant-progress__step--working b {
  color: #246fae;
}

.assistant-progress__step--working small {
  color: #68839a;
}

.assistant-progress__step--done b {
  color: #4c7566;
}

.assistant-cancelled {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 8px 0 26px;
  padding: 13px 16px;
  border: 1px solid #e3e9ef;
  border-radius: 8px;
  background: #f7f9fb;
  color: #778a9c;
  font-size: 13px;
  white-space: pre-wrap;
}

.assistant-cancelled svg {
  width: 19px;
  height: 19px;
  flex: none;
}

.assistant-error {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  margin: 8px 0 26px;
  padding: 14px 16px;
  border: 1px solid #efd7d7;
  border-radius: 8px;
  background: #fff8f8;
  color: #8a4d4d;
  font-size: 13px;
  line-height: 1.75;
}

.assistant-error > svg {
  width: 20px;
  height: 20px;
  flex: none;
  margin-top: 1px;
}

.assistant-error b {
  display: block;
  color: #743b3b;
  font-size: 13px;
  font-weight: 650;
}

.assistant-error p {
  margin: 5px 0 0;
  white-space: pre-wrap;
}

.assistant-answer {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding-right: 44px;
  margin-bottom: 34px;
  color: #516a82;
  font-size: 14px;
  line-height: 1.9;
}

.assistant-answer__content--streaming {
  margin: 16px 0 14px !important;
  padding: 14px 16px;
  border: 1px solid #e3ebf2;
  border-radius: 8px;
  background: #fbfcfd;
}

.assistant-answer__identity {
  display: flex;
  width: 30px;
  flex: none;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.assistant-answer__avatar {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 50%;
  background: #edf3f8;
  color: #1e6db0;
}

.assistant-answer__avatar svg {
  width: 21px;
  height: 21px;
}

.assistant-answer__body {
  min-width: 0;
  flex: 1;
  padding: 10px 13px 8px;
  border: 1px solid #e1eaf2;
  border-radius: 4px 12px 12px 12px;
  background: #fbfcfe;
  box-shadow: 0 1px 2px rgb(31 71 103 / 4%);
}

.assistant-answer__content > :first-child {
  margin: 0 0 12px;
}

.assistant-answer__content h1,
.assistant-answer__content h2,
.assistant-answer__content h3,
.assistant-answer__content h4,
.assistant-answer__content h5,
.assistant-answer__content h6 {
  margin: 23px 0 11px;
  color: #203b54;
  font-weight: 650;
  line-height: 1.5;
}

.assistant-answer__content h1 {
  font-size: 23px;
}

.assistant-answer__content h2 {
  font-size: 20px;
}

.assistant-answer__content h3 {
  font-size: 18px;
}

.assistant-answer__content h4,
.assistant-answer__content h5,
.assistant-answer__content h6 {
  font-size: 15px;
}

.assistant-answer__content p {
  margin: 0 0 12px;
}

.assistant-answer__content ul,
.assistant-answer__content ol {
  display: grid;
  gap: 7px;
  margin: 10px 0 14px;
  padding-left: 22px;
}

.assistant-answer__content li::marker {
  color: #3d84bf;
}

.assistant-answer__content blockquote {
  margin: 14px 0;
  padding: 9px 14px;
  border-left: 3px solid #84acd0;
  background: #f5f8fb;
  color: #60788c;
}

.assistant-answer__content code {
  padding: 2px 5px;
  border-radius: 4px;
  background: #eef3f7;
  color: #315873;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 0.92em;
}

.assistant-answer__content pre {
  overflow-x: auto;
  margin: 14px 0;
  padding: 13px 15px;
  border-radius: 7px;
  background: #f1f5f8;
}

.assistant-answer__content pre code {
  padding: 0;
  background: transparent;
}

.assistant-answer__content a {
  color: #176fc5;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.assistant-answer__content table {
  width: 100%;
  margin: 14px 0;
  border-collapse: collapse;
}

.assistant-answer__content th,
.assistant-answer__content td {
  padding: 8px 10px;
  border: 1px solid #dce6ee;
  text-align: left;
}

.assistant-answer__content th {
  background: #f3f7fa;
  color: #294b66;
}

.assistant-citations h4 {
  margin: 0 0 9px;
  color: #294b66;
  font-size: 15px;
  font-weight: 650;
}

.assistant-citations {
  margin-top: 30px;
  padding-top: 20px;
  border-top: 1px solid #e5ecf2;
}

.assistant-citations > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.assistant-citations > header span {
  color: #8a9baa;
  font-size: 12px;
}

.assistant-citations ul {
  display: grid;
  gap: 9px;
  margin: 11px 0 0;
  padding: 0;
  list-style: none;
}

.assistant-citations li {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  padding: 13px;
  border-radius: 7px;
  background: #f3f6fa;
}

.assistant-citations__index {
  display: grid;
  width: 25px;
  height: 25px;
  flex: none;
  place-items: center;
  border-radius: 5px;
  background: #e5eef7;
  color: #47749b;
  font-size: 11px;
  font-weight: 650;
}

.assistant-citations li > div {
  min-width: 0;
}

.assistant-citations b {
  display: block;
  color: #3a5b75;
  font-size: 13px;
  font-weight: 600;
}

.assistant-citations small {
  display: block;
  margin-top: 5px;
  color: #8a9aa9;
  font-size: 12px;
  line-height: 1.6;
}

.assistant-compose {
  padding: 12px 36px 18px;
  background: #fff;
}

.assistant-compose__box {
  padding: 13px 14px 11px;
  border: 1px solid #ccdaea;
  border-radius: 11px;
  background: #fff;
  transition:
    border-color 150ms ease,
    box-shadow 150ms ease;
}

.assistant-compose__box:focus-within {
  border-color: #74a6d6;
  box-shadow: 0 0 0 3px #edf5fc;
}

.assistant-compose label {
  display: block;
}

.assistant-compose textarea {
  width: 100%;
  min-height: 65px;
  resize: none;
  padding: 2px 4px;
  border: 0;
  outline: 0;
  background: transparent;
  color: #304d66;
  font-size: 14px;
  line-height: 1.8;
}

.assistant-compose textarea::placeholder {
  color: #92a1ae;
}

.assistant-compose__footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 14px;
  color: #8b9aa9;
  font-size: 12px;
}

.assistant-compose__footer button {
  display: grid;
  width: 36px;
  height: 32px;
  flex: none;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: #176fc5;
  color: #fff;
  transition:
    background 150ms ease,
    opacity 150ms ease;
}

.assistant-compose__footer button:hover:not(:disabled) {
  background: #0b5eae;
}

.assistant-compose__footer button:disabled {
  background: #9bc1e4;
  cursor: default;
  opacity: 0.7;
}

.assistant-compose__footer button svg {
  width: 20px;
  height: 20px;
}

.assistant-compose > small {
  display: block;
  margin-top: 11px;
  color: #8b9aa9;
  font-size: 12px;
  text-align: center;
}

.assistant-visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.assistant-modal-enter-active,
.assistant-modal-leave-active {
  transition: opacity 180ms ease;
}

.assistant-modal-enter-active .assistant-window,
.assistant-modal-leave-active .assistant-window {
  transition: transform 180ms ease;
}

.assistant-modal-enter-from,
.assistant-modal-leave-to {
  opacity: 0;
}

.assistant-modal-enter-from .assistant-window,
.assistant-modal-leave-to .assistant-window {
  transform: translateY(10px) scale(0.985);
}

@keyframes assistant-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1024px) {
  .assistant-mask {
    padding: 16px;
  }

  .assistant-window {
    width: calc(100vw - 32px);
    height: calc(100dvh - 32px);
    grid-template-columns: minmax(0, 1fr);
  }

  .assistant-sidebar {
    position: absolute;
    z-index: 4;
    top: 0;
    bottom: 0;
    left: 0;
    width: min(280px, 84vw);
    border-right: 1px solid #e4eaf0;
    box-shadow: 14px 0 30px rgb(18 48 75 / 14%);
    transform: translateX(-105%);
    transition: transform 180ms ease;
  }

  .assistant-sidebar--open {
    transform: translateX(0);
  }

  .assistant-history-scrim {
    position: absolute;
    z-index: 3;
    inset: 0;
    display: block;
    background: rgb(26 48 68 / 18%);
  }

  .assistant-history-toggle {
    display: grid;
  }

  .assistant-content {
    padding-right: 30px;
    padding-left: 30px;
  }

  .assistant-compose {
    padding-right: 24px;
    padding-left: 24px;
  }
}

@media (max-width: 700px) {
  .assistant-content {
    padding: 24px 20px 26px;
  }

  .assistant-main__header {
    min-height: 70px;
    padding: 13px 16px;
  }

  .assistant-main__title h2 {
    font-size: 14px;
  }

  .assistant-main__title p {
    max-width: 52vw;
  }

  .assistant-welcome {
    margin-top: 18px;
  }

  .assistant-welcome h3 {
    font-size: 24px;
  }

  .assistant-prompts {
    gap: 10px;
  }

  .assistant-prompt {
    grid-template-columns: 24px minmax(0, 1fr);
    gap: 8px;
    min-height: 86px;
    padding: 14px 28px 14px 13px;
  }

  .assistant-prompt__copy > span {
    font-size: 12px;
  }

  .assistant-user-message__body p {
    font-size: 13px;
  }

  .assistant-answer {
    gap: 10px;
    padding-right: 38px;
  }

  .assistant-answer__identity {
    width: 28px;
  }

  .assistant-answer__avatar {
    width: 28px;
    height: 28px;
  }

  .assistant-answer__avatar svg {
    width: 20px;
    height: 20px;
  }

  .assistant-answer__content h1 {
    font-size: 21px;
  }

  .assistant-answer__content h2 {
    font-size: 19px;
  }

  .assistant-compose {
    padding: 10px 16px 13px;
  }

  .assistant-compose textarea {
    min-height: 54px;
  }

  .assistant-compose > small {
    margin-top: 7px;
  }
}

@media (max-width: 640px) {
  .assistant-mask {
    padding: 0;
  }

  .assistant-window {
    width: 100vw;
    height: 100dvh;
    max-height: none;
    border: 0;
    border-radius: 0;
  }

  .assistant-main__header {
    padding-top: max(13px, env(safe-area-inset-top));
  }

  .assistant-prompts {
    grid-template-columns: minmax(0, 1fr);
  }

  .assistant-welcome {
    margin-top: 6px;
  }

  .assistant-welcome__icon {
    width: 56px;
    height: 56px;
    border-radius: 16px;
  }

  .assistant-welcome__icon svg {
    width: 31px;
    height: 31px;
  }

  .assistant-welcome h3 {
    margin-top: 20px;
    font-size: 23px;
  }

  .assistant-prompts {
    margin-top: 24px;
  }

  .assistant-compose {
    padding-bottom: max(13px, env(safe-area-inset-bottom));
  }

  .assistant-conversation {
    padding-bottom: 2px;
  }

  .assistant-user-message {
    gap: 9px;
    margin-bottom: 23px;
  }

  .assistant-user-message__body {
    max-width: 82%;
  }

  .assistant-user-message__avatar {
    width: 29px;
    height: 29px;
  }

  .assistant-citations li {
    padding: 11px;
  }
}

@media (max-height: 720px) {
  .assistant-content {
    padding-top: 19px;
  }

  .assistant-welcome {
    margin-top: 4px;
  }

  .assistant-welcome__icon {
    width: 54px;
    height: 54px;
  }

  .assistant-welcome__icon svg {
    width: 30px;
    height: 30px;
  }

  .assistant-welcome h3 {
    margin-top: 17px;
    font-size: 24px;
  }

  .assistant-prompts {
    margin-top: 21px;
  }

  .assistant-prompt {
    min-height: 78px;
    padding-top: 12px;
    padding-bottom: 12px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .assistant-window,
  .assistant-sidebar,
  .assistant-prompt,
  .assistant-compose__box {
    transition: none;
  }

  .assistant-spinner svg {
    animation-duration: 3s;
  }

  .assistant-modal-enter-active,
  .assistant-modal-leave-active,
  .assistant-modal-enter-active .assistant-window,
  .assistant-modal-leave-active .assistant-window {
    transition-duration: 1ms;
  }
}
</style>
