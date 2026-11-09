import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { AssistantMessage } from '../types'

export const useAssistantStore = defineStore('assistant', () => {
  const isOpen = ref(false)
  const draft = ref('')
  const messages = ref<AssistantMessage[]>([])
  const canSend = computed(() => draft.value.trim().length > 0)
  let messageSequence = 0

  function open() {
    isOpen.value = true
  }

  function close() {
    isOpen.value = false
  }

  function toggle() {
    isOpen.value = !isOpen.value
  }

  function sendMessage(): boolean {
    const content = draft.value.trim()

    if (!content) {
      return false
    }

    messageSequence += 1
    messages.value.push({
      id: `local-${messageSequence}`,
      role: 'user',
      content,
      createdAt: new Date().toISOString(),
    })
    draft.value = ''

    return true
  }

  return {
    isOpen,
    draft,
    messages,
    canSend,
    open,
    close,
    toggle,
    sendMessage,
  }
})
