import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '@/api'
import {
  listStations,
  listWorkOrders,
  putWorkOrderReview,
  type StationSummary,
} from '@/api/modules/maintenance'
import {
  applyWorkOrderToEvent,
  indexStations,
  workOrderToEvent,
} from '../adapter'
import { loadEventTrends } from '../trends'
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

function errorText(error: unknown): string {
  if (error instanceof ApiError && error.message.trim()) {
    return error.message
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '维护事件请求失败，请稍后重试。'
}

export const useMaintenanceStore = defineStore('maintenance', () => {
  const events = ref<MaintenanceEvent[]>([])
  const activeTab = ref<MaintenanceTab>('pending')
  const selectedEventId = ref<string | null>(null)
  const notice = ref<string | null>(null)
  const filters = reactive<MaintenanceFilters>({ ...DEFAULT_FILTERS })
  const isLoadingEvents = ref(false)
  const eventsError = ref<string | null>(null)

  let stationIndex = new Map<string, StationSummary>()
  let noticeTimer: ReturnType<typeof setTimeout> | undefined
  const loadedTrends = new Set<string>()
  const loadingTrends = new Set<string>()

  const pendingCount = computed(
    () => events.value.filter((event) => event.status === 'pending').length,
  )

  const confirmedCount = computed(
    () => events.value.filter((event) => event.status === 'confirmed').length,
  )

  const selectedEvent = computed(
    () =>
      events.value.find(
        (event) => event.id === selectedEventId.value,
      ) ?? null,
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

  async function loadEvents() {
    if (isLoadingEvents.value) {
      return
    }

    isLoadingEvents.value = true
    eventsError.value = null
    loadedTrends.clear()
    loadingTrends.clear()

    try {
      const [orders, stations] = await Promise.all([
        listWorkOrders(),
        listStations().catch(() => [] as StationSummary[]),
      ])
      stationIndex = indexStations(stations)
      events.value = orders
        .map((order) => workOrderToEvent(order, stationIndex))
        .sort(
          (left, right) =>
            new Date(right.triggeredAt).getTime() -
            new Date(left.triggeredAt).getTime(),
        )

      if (
        selectedEventId.value &&
        !events.value.some((event) => event.id === selectedEventId.value)
      ) {
        selectedEventId.value = null
      }
    } catch (error) {
      eventsError.value = errorText(error)
    } finally {
      isLoadingEvents.value = false
    }
  }

  async function ensureTrends(eventId: string) {
    const event = events.value.find((item) => item.id === eventId)
    if (!event || loadedTrends.has(eventId) || loadingTrends.has(eventId)) {
      return
    }

    loadingTrends.add(eventId)
    try {
      const trends = await loadEventTrends(event)
      if (event.equipmentType === 'detector' && trends.detectorTrend) {
        event.detectorTrend = trends.detectorTrend
      }
      if (event.equipmentType === 'backup_power' && trends.backupTrend) {
        event.backupTrend = trends.backupTrend
      }
      loadedTrends.add(eventId)
    } catch {
      // Keep the empty trend; the panels explain that the period has no data.
    } finally {
      loadingTrends.delete(eventId)
    }
  }

  function setActiveTab(tab: MaintenanceTab) {
    activeTab.value = tab
  }

  function resetFilters() {
    Object.assign(filters, DEFAULT_FILTERS)
  }

  function openEvent(eventId: string) {
    selectedEventId.value = eventId
    void ensureTrends(eventId)
  }

  function closeEvent() {
    selectedEventId.value = null
  }

  async function submitReview(
    eventId: string,
    outcome: ReviewOutcome,
    comment: string,
  ): Promise<boolean> {
    const event = events.value.find((item) => item.id === eventId)

    if (!event || event.status !== 'pending') {
      return false
    }

    try {
      const updated = await putWorkOrderReview(eventId, {
        outcome,
        comment: comment.trim(),
      })
      applyWorkOrderToEvent(event, updated, stationIndex)
      closeEvent()
      showNotice('人工审核已提交，事件已归档')
      return true
    } catch (error) {
      showNotice(errorText(error))
      return false
    }
  }

  async function updateReview(
    eventId: string,
    outcome: ReviewOutcome,
    comment: string,
  ): Promise<boolean> {
    const event = events.value.find((item) => item.id === eventId)

    if (!event || event.status !== 'confirmed' || !event.review) {
      return false
    }

    try {
      const updated = await putWorkOrderReview(eventId, {
        outcome,
        comment: comment.trim(),
      })
      applyWorkOrderToEvent(event, updated, stationIndex)
      showNotice('已确认审核记录已更新')
      return true
    } catch (error) {
      showNotice(errorText(error))
      return false
    }
  }

  return {
    events,
    activeTab,
    selectedEventId,
    selectedEvent,
    notice,
    filters,
    isLoadingEvents,
    eventsError,
    pendingCount,
    confirmedCount,
    siteOptions,
    filteredEvents,
    loadEvents,
    setActiveTab,
    resetFilters,
    openEvent,
    closeEvent,
    submitReview,
    updateReview,
  }
})
