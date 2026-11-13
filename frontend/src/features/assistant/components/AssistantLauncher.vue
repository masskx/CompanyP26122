<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import AssistantPanel from './AssistantPanel.vue'
import { useAssistantStore } from '../stores/assistant'

const assistant = useAssistantStore()
const launcher = ref<HTMLButtonElement | null>(null)

watch(
  () => assistant.isOpen,
  async (isOpen) => {
    if (!isOpen) {
      await nextTick()
      launcher.value?.focus()
    }
  },
)
</script>

<template>
  <AssistantPanel v-if="assistant.isOpen" />

  <Transition name="assistant-launcher">
    <button
      v-if="!assistant.isOpen"
      ref="launcher"
      type="button"
      class="assistant-launcher"
      aria-label="打开智能助手"
      aria-haspopup="dialog"
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
  z-index: 65;
  right: 26px;
  bottom: 24px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 12px 19px;
  border: 1px solid #438ed5;
  border-radius: 26px;
  background: #176fc5;
  box-shadow: 0 4px 18px rgb(18 61 100 / 17%);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  transition:
    background 150ms ease,
    box-shadow 150ms ease;
}

.assistant-launcher:hover {
  background: #0b5eae;
  box-shadow: 0 7px 22px rgb(18 61 100 / 22%);
}

.assistant-launcher__icon {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
}

.assistant-launcher__icon svg {
  width: 26px;
  height: 26px;
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

@media (max-width: 520px) {
  .assistant-launcher {
    right: 16px;
    bottom: 16px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .assistant-launcher {
    transition: none;
  }
}
</style>
