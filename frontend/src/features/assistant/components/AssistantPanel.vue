<script setup lang="ts">
import {
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
} from 'vue'
import { useAssistantStore } from '../stores/assistant'

const assistant = useAssistantStore()
const composer = ref<HTMLTextAreaElement | null>(null)
const messageList = ref<HTMLElement | null>(null)

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function submitMessage() {
  if (!assistant.sendMessage()) {
    return
  }

  await nextTick()
  if (messageList.value) {
    messageList.value.scrollTop = messageList.value.scrollHeight
  }
  composer.value?.focus()
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

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    assistant.close()
  }
}

onMounted(() => {
  composer.value?.focus()
  window.addEventListener('keydown', handleWindowKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleWindowKeydown)
})
</script>

<template>
  <aside
    class="assistant-panel"
    role="dialog"
    aria-modal="false"
    aria-labelledby="assistant-panel-title"
  >
    <header class="assistant-panel__header">
      <div class="assistant-panel__brand">
        <span class="assistant-panel__brand-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <rect
              x="4.5"
              y="6.5"
              width="15"
              height="12"
              rx="4"
              stroke="currentColor"
              stroke-width="1.8"
            />
            <path
              d="M12 3.5V6.5M8.8 11.5H8.7M15.3 11.5H15.2M8.5 15H15.5"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
          </svg>
        </span>
        <div>
          <h2 id="assistant-panel-title">MES 智能助手</h2>
          <p>维护知识与设备分析</p>
        </div>
      </div>
      <button
        type="button"
        class="assistant-panel__close"
        aria-label="关闭智能助手"
        @click="assistant.close"
      >
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M7 7L17 17M17 7L7 17"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
          />
        </svg>
      </button>
    </header>

    <div class="assistant-panel__notice">
      当前为界面骨架，输入内容仅保存在本地，不会发送到服务端。
    </div>

    <div ref="messageList" class="assistant-panel__messages">
      <div v-if="assistant.messages.length === 0" class="assistant-welcome">
        <span class="assistant-welcome__icon" aria-hidden="true">AI</span>
        <h3>有什么可以帮你？</h3>
        <p>接口接入后，可在此查询设备状态、维护知识和分析依据。</p>
      </div>

      <ul v-else class="assistant-messages">
        <li
          v-for="message in assistant.messages"
          :key="message.id"
          class="assistant-message"
          :data-role="message.role"
        >
          <p>{{ message.content }}</p>
          <time :datetime="message.createdAt">
            {{ formatTime(message.createdAt) }}
          </time>
        </li>
      </ul>
    </div>

    <form class="assistant-composer" @submit.prevent="submitMessage">
      <label class="assistant-composer__field">
        <span class="assistant-composer__hidden-label">向智能助手提问</span>
        <textarea
          ref="composer"
          v-model="assistant.draft"
          rows="3"
          placeholder="输入问题，当前仅作本地展示"
          @keydown="handleComposerKeydown"
        ></textarea>
      </label>
      <div class="assistant-composer__footer">
        <span>Enter 发送 · Shift + Enter 换行</span>
        <button type="submit" :disabled="!assistant.canSend">
          发送
        </button>
      </div>
    </form>
  </aside>
</template>

<style scoped>
.assistant-panel {
  position: fixed;
  z-index: 40;
  right: 24px;
  bottom: 24px;
  display: grid;
  width: min(420px, calc(100vw - 32px));
  height: min(620px, calc(100vh - 48px));
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  border: 1px solid #cbd9e5;
  border-radius: 15px;
  background: #fff;
  box-shadow: var(--shadow-floating);
}

.assistant-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 17px 18px;
  background: linear-gradient(135deg, #0a3a64, #0a4f85);
  color: #fff;
}

.assistant-panel__brand {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 11px;
}

.assistant-panel__brand-icon {
  display: grid;
  width: 36px;
  height: 36px;
  flex: none;
  place-items: center;
  border-radius: 10px;
  background: rgb(255 255 255 / 14%);
}

.assistant-panel__brand-icon svg {
  width: 23px;
  height: 23px;
}

.assistant-panel__brand h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.assistant-panel__brand p {
  margin: 4px 0 0;
  color: rgb(255 255 255 / 66%);
  font-size: 11px;
}

.assistant-panel__close {
  display: grid;
  width: 32px;
  height: 32px;
  flex: none;
  place-items: center;
  border: 1px solid rgb(255 255 255 / 10%);
  border-radius: 8px;
  background: rgb(255 255 255 / 8%);
  color: rgb(255 255 255 / 78%);
}

.assistant-panel__close:hover {
  background: rgb(255 255 255 / 15%);
  color: #fff;
}

.assistant-panel__close svg {
  width: 19px;
  height: 19px;
}

.assistant-panel__notice {
  padding: 9px 18px;
  border-bottom: 1px solid #e4ecf3;
  background: #f6f9fc;
  color: #7b8fa1;
  font-size: 11px;
  line-height: 1.5;
}

.assistant-panel__messages {
  overflow-y: auto;
  padding: 24px 18px;
  background:
    radial-gradient(circle at 85% 10%, rgb(18 110 208 / 5%), transparent 15rem),
    #fff;
}

.assistant-welcome {
  display: flex;
  max-width: 300px;
  min-height: 100%;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  margin: 0 auto;
  text-align: center;
}

.assistant-welcome__icon {
  display: grid;
  width: 58px;
  height: 58px;
  place-items: center;
  border: 1px solid #cfe0ed;
  border-radius: 18px;
  background: linear-gradient(145deg, #eef7ff, #dcecf9);
  color: #1468b1;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.7px;
  box-shadow: 0 12px 30px rgb(35 91 134 / 12%);
}

.assistant-welcome h3 {
  margin: 17px 0 8px;
  color: #294e6b;
  font-size: 17px;
  font-weight: 600;
}

.assistant-welcome p {
  margin: 0;
  color: #8295a5;
  font-size: 12px;
  line-height: 1.8;
}

.assistant-messages {
  display: grid;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.assistant-message {
  display: grid;
  justify-items: end;
}

.assistant-message p {
  max-width: 86%;
  margin: 0;
  padding: 11px 13px;
  border-radius: 12px 12px 3px 12px;
  background: var(--color-blue);
  color: #fff;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.assistant-message time {
  margin-top: 5px;
  color: #9caab7;
  font-size: 10px;
}

.assistant-composer {
  padding: 14px 16px 15px;
  border-top: 1px solid #e3ebf2;
  background: #fbfdff;
}

.assistant-composer__field {
  display: block;
}

.assistant-composer__hidden-label {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.assistant-composer textarea {
  width: 100%;
  min-height: 74px;
  resize: vertical;
  padding: 11px 12px;
  border: 1px solid #cfdce7;
  border-radius: 9px;
  background: #fff;
  color: #294b67;
  font-size: 13px;
  line-height: 1.6;
}

.assistant-composer textarea::placeholder {
  color: #9aaab7;
}

.assistant-composer textarea:focus {
  border-color: #79a9cf;
  outline: none;
  box-shadow: 0 0 0 3px rgb(18 110 208 / 9%);
}

.assistant-composer__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 9px;
}

.assistant-composer__footer span {
  color: #94a4b1;
  font-size: 10px;
}

.assistant-composer__footer button {
  min-width: 66px;
  height: 34px;
  border-radius: 7px;
  background: var(--color-blue);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}

.assistant-composer__footer button:disabled {
  background: #b7c6d3;
}

@media (max-width: 520px) {
  .assistant-panel {
    right: 16px;
    bottom: 16px;
    height: min(600px, calc(100vh - 32px));
  }
}
</style>
