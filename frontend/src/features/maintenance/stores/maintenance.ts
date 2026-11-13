import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { createDemoEvents } from '../fixtures/demoEvents'
import {
  equipmentTypeLabels,
  eventSourceLabels,
  type MaintenanceEvent,
  type MaintenanceFilters,
  type MaintenanceTab,
  type ReviewOutcome,
} from '../types'

const DEFAULT_FILTERS: MaintenanceFilters = {
  siteId: 'all',
  equipmentType: 'all',
  keyword: '',
}

export const useMaintenanceStore = defineStore('maintenance', () => {
  const events = ref<MaintenanceEvent[]>(createDemoEvents())
  const activeTab = ref<MaintenanceTab>('pending')
  const selectedEventId = ref<string | null>(null)
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

  const siteOptions = computed(() => {
    const sitesById = new Map<string, string>()

    for (const event of events.value) {
      if (!sitesById.has(event.siteId)) {
        sitesById.set(event.siteId, event.siteName)
      }
    }

    return Array.from(sitesById, ([value, name]) => ({
      value,
      label: `${value} · ${name}`,
    })).sort((left, right) =>
      left.value.localeCompare(right.value, 'zh-CN', {
        numeric: true,
        sensitivity: 'base',
      }),
    )
  })

  const filteredEvents = computed(() => {
    const keyword = filters.keyword.trim().toLocaleLowerCase('zh-CN')

    return events.value
      .filter((event) => event.status === activeTab.value)
      .filter(
        (event) => filters.siteId === 'all' || event.siteId === filters.siteId,
      )
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

  function openEvent(eventId: string) {
    selectedEventId.value = eventId
  }

  function closeEvent() {
    selectedEventId.value = null
  }

  function submitReview(
    eventId: string,
    outcome: ReviewOutcome,
    comment: string,
  ): boolean {
    const event = events.value.find((item) => item.id === eventId)
    const normalizedComment = comment.trim()

    if (
      !event ||
      event.status !== 'pending'
    ) {
      return false
    }

    const reviewedAt = new Date().toISOString()
    event.status = 'confirmed'
    event.review = {
      outcome,
      comment: normalizedComment,
      reviewedAt,
      reviewedBy: '值班操作员',
    }
    closeEvent()
    showNotice('人工审核已提交，事件已归档')
    return true
  }

  function updateReview(
    eventId: string,
    outcome: ReviewOutcome,
    comment: string,
  ): boolean {
    const event = events.value.find((item) => item.id === eventId)

    if (
      !event ||
      event.status !== 'confirmed' ||
      !event.review
    ) {
      return false
    }

    const normalizedComment = comment.trim()
    const updatedAt = new Date().toISOString()
    event.review = {
      ...event.review,
      outcome,
      comment: normalizedComment,
      updatedAt,
    }
    showNotice('已确认审核记录已更新')
    return true
  }

  return {
    events,
    activeTab,
    selectedEventId,
    selectedEvent,
    notice,
    filters,
    pendingCount,
    confirmedCount,
    siteOptions,
    filteredEvents,
    setActiveTab,
    resetFilters,
    openEvent,
    closeEvent,
    submitReview,
    updateReview,
  }
})
