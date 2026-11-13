<script setup lang="ts">
import { useMaintenanceStore } from '../stores/maintenance'
import MaintenanceFilters from './MaintenanceFilters.vue'

const maintenance = useMaintenanceStore()
</script>

<template>
  <section class="event-toolbar" aria-label="事件列表筛选">
    <div class="event-tabs" role="tablist" aria-label="事件状态">
      <button
        type="button"
        role="tab"
        class="event-tab"
        :class="{ 'event-tab--active': maintenance.activeTab === 'pending' }"
        :aria-selected="maintenance.activeTab === 'pending'"
        @click="maintenance.setActiveTab('pending')"
      >
        新事件分析
        <span class="event-tab__count">{{ maintenance.pendingCount }}</span>
      </button>
      <button
        type="button"
        role="tab"
        class="event-tab"
        :class="{ 'event-tab--active': maintenance.activeTab === 'confirmed' }"
        :aria-selected="maintenance.activeTab === 'confirmed'"
        @click="maintenance.setActiveTab('confirmed')"
      >
        已确认记录
        <span class="event-tab__count">{{ maintenance.confirmedCount }}</span>
      </button>
    </div>

    <MaintenanceFilters />

  </section>
</template>

<style scoped>
.event-toolbar {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 22px;
  padding: 0 2px 4px;
}

.event-tabs {
  display: flex;
  align-items: center;
  gap: 30px;
}

.event-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 52px;
  padding: 15px 2px 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #526c80;
  font-size: 15px;
  white-space: nowrap;
}

.event-tab:hover {
  color: #176cc0;
}

.event-tab--active {
  border-bottom-color: #176cc0;
  color: #176cc0;
  font-weight: 600;
}

.event-tab__count {
  display: inline-grid;
  min-width: 23px;
  height: 23px;
  place-items: center;
  padding: 0 6px;
  border-radius: 999px;
  background: #edf3f8;
  color: #617a8f;
  font-size: 12px;
  font-weight: 500;
}

.event-tab--active .event-tab__count {
  background: #e8f2fc;
  color: #176cc0;
}

@media (max-width: 1100px) {
  .event-toolbar {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .event-tabs {
    justify-self: start;
  }
}

@media (max-width: 620px) {
  .event-tabs {
    gap: 20px;
  }
}

@media (max-width: 760px) {
  .event-toolbar {
    align-items: stretch;
    flex-direction: column;
    gap: 8px;
  }
}
</style>
