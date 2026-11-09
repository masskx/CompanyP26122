<script setup lang="ts">
import AssistantPanel from './AssistantPanel.vue'
import { useAssistantStore } from '../stores/assistant'

const assistant = useAssistantStore()
</script>

<template>
  <Transition name="assistant-panel">
    <AssistantPanel v-if="assistant.isOpen" />
  </Transition>

  <Transition name="assistant-launcher">
    <button
      v-if="!assistant.isOpen"
      type="button"
      class="assistant-launcher"
      aria-label="打开智能助手"
      @click="assistant.open"
    >
      <span class="assistant-launcher__icon" aria-hidden="true">
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
      <span>智能助手</span>
    </button>
  </Transition>
</template>

<style scoped>
.assistant-launcher {
  position: fixed;
  z-index: 30;
  right: 28px;
  bottom: 25px;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  min-height: 52px;
  padding: 0 20px 0 15px;
  border: 1px solid rgb(255 255 255 / 17%);
  border-radius: 26px;
  background: linear-gradient(135deg, #1b75c8, #0b5bae);
  box-shadow:
    0 12px 28px rgb(8 70 127 / 28%),
    inset 0 1px rgb(255 255 255 / 18%);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.2px;
  transition:
    transform 160ms ease,
    box-shadow 160ms ease;
}

.assistant-launcher:hover {
  transform: translateY(-2px);
  box-shadow:
    0 16px 32px rgb(8 70 127 / 34%),
    inset 0 1px rgb(255 255 255 / 20%);
}

.assistant-launcher__icon {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 9px;
  background: rgb(255 255 255 / 15%);
}

.assistant-launcher__icon svg {
  width: 20px;
  height: 20px;
}

.assistant-launcher-enter-active,
.assistant-launcher-leave-active {
  transition:
    opacity 150ms ease,
    transform 150ms ease;
}

.assistant-launcher-enter-from,
.assistant-launcher-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

.assistant-panel-enter-active,
.assistant-panel-leave-active {
  transition:
    opacity 170ms ease,
    transform 170ms ease;
}

.assistant-panel-enter-from,
.assistant-panel-leave-to {
  opacity: 0;
  transform: translateY(12px) scale(0.98);
}

@media (max-width: 520px) {
  .assistant-launcher {
    right: 16px;
    bottom: 16px;
  }
}
</style>
