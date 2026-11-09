import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { createDemoEvents } from '../fixtures/demoEvents'
import {
  equipmentTypeLabels,
  eventSourceLabels,
  type AiFeedbackRating,
  type ConfirmationDecision,
  type DialogTab,
  type MaintenanceEvent,
  type MaintenanceFilters,
  type MaintenanceTab,
} from '../types'

const DEFAULT_FILTERS: MaintenanceFilters = {
  equipmentType: 'all',
  keyword: '',
}

export const useMaintenanceStore = defineStore('maintenance', () => {
  const events = ref<MaintenanceEvent[]>(createDemoEvents())
  const activeTab = ref<MaintenanceTab>('pending')
  const selectedEventId = ref<string | null>(null)
  const dialogTab = ref<DialogTab>('analysis')
  const notice = ref<string | null>(null)
  const filters = reactive<MaintenanceFilters>({ ...DEFAULT_FILTERS })
  let noticeTimer: ReturnType<typeof setTimeout> | undefined

  const pendingCount = computed(
    () => events.value.filter((event) => event.status === 'pending').length,
  )

  const confirmedCount = computed(
    () => events.value.filter((event) => event.status === 'confirmed').length,
  )

  const selectedEvent = computed(
    () =>
      events.value.find((event) => event.id === selectedEventId.value) ?? null,
  )

  const filteredEvents = computed(() => {
    const keyword = filters.keyword.trim().toLocaleLowerCase('zh-CN')

    return events.value
      .filter((event) => event.status === activeTab.value)
      .filter(
        (event) =>
          filters.equipmentType === 'all' ||
          event.equipmentType === filters.equipmentType,
      )
      .filter((event) => {
        if (!keyword) {
          return true
        }

        const searchableText = [
          event.eventNumber,
          event.siteId,
          event.siteName,
          event.equipmentName,
          event.eventTitle,
          equipmentTypeLabels[event.equipmentType],
          eventSourceLabels[event.eventSource],
        ]
          .join(' ')
          .toLocaleLowerCase('zh-CN')

        return searchableText.includes(keyword)
      })
      .slice()
      .sort(
        (left, right) =>
          new Date(right.triggeredAt).getTime() -
          new Date(left.triggeredAt).getTime(),
      )
  })

  function showNotice(message: string) {
    notice.value = message

    if (noticeTimer) {
      clearTimeout(noticeTimer)
    }

    noticeTimer = setTimeout(() => {
      notice.value = null
    }, 3200)
  }

  function setActiveTab(tab: MaintenanceTab) {
    activeTab.value = tab
  }

  function resetFilters() {
    Object.assign(filters, DEFAULT_FILTERS)
  }

  function openEvent(eventId: string, tab: DialogTab = 'analysis') {
    selectedEventId.value = eventId
    dialogTab.value = tab
  }

  function closeEvent() {
    selectedEventId.value = null
    dialogTab.value = 'analysis'
  }

  function saveAiFeedback(
    eventId: string,
    rating: AiFeedbackRating,
    comment: string,
  ): boolean {
    const event = events.value.find((item) => item.id === eventId)

    if (!event) {
      return false
    }

    event.aiFeedback = {
      rating,
      comment: comment.trim(),
      submittedAt: new Date().toISOString(),
    }
    showNotice('AI 结论评价已保存')
    return true
  }

  function confirmEvent(
    eventId: string,
    decision: ConfirmationDecision,
    comment: string,
  ): boolean {
    const event = events.value.find((item) => item.id === eventId)
    const normalizedComment = comment.trim()

    if (
      !event ||
      event.status !== 'pending' ||
      (decision === 'rejected' && !normalizedComment)
    ) {
      return false
    }

    event.status = 'confirmed'
    event.confirmation = {
      decision,
      comment: normalizedComment,
      confirmedAt: new Date().toISOString(),
      confirmedBy: '值班操作员',
    }
    closeEvent()
    showNotice('人工确认已提交，工单已归档')
    return true
  }

  return {
    events,
    activeTab,
    selectedEventId,
    selectedEvent,
    dialogTab,
    notice,
    filters,
    pendingCount,
    confirmedCount,
    filteredEvents,
    setActiveTab,
    resetFilters,
    openEvent,
    closeEvent,
    saveAiFeedback,
    confirmEvent,
  }
})
