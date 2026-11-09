<script setup lang="ts">
import { computed } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'

const maintenance = useMaintenanceStore()

const hasFilters = computed(
  () =>
    maintenance.filters.equipmentType !== 'all' ||
    maintenance.filters.keyword.trim().length > 0,
)
</script>

<template>
  <section class="event-toolbar" aria-label="工单列表筛选">
    <div class="event-tabs" role="tablist" aria-label="工单状态">
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

    <div class="event-toolbar__filters">
      <label class="event-filter">
        <span>设备类型</span>
        <select v-model="maintenance.filters.equipmentType">
          <option value="all">全部</option>
          <option value="detector">探测器</option>
          <option value="backup_power">备电</option>
        </select>
      </label>

      <label class="event-search">
        <span class="event-search__hidden-label">搜索工单</span>
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle
            cx="10.8"
            cy="10.8"
            r="6.3"
            stroke="currentColor"
            stroke-width="1.8"
          />
          <path
            d="M15.5 15.5L20 20"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
          />
        </svg>
        <input
          v-model.trim="maintenance.filters.keyword"
          type="search"
          placeholder="事件编号 / 站点 / 事件名称"
        />
      </label>

      <button
        v-if="hasFilters"
        type="button"
        class="event-toolbar__reset"
        @click="maintenance.resetFilters"
      >
        重置
      </button>
    </div>
  </section>
</template>

<style scoped>
.event-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  padding: 0 2px;
}

.event-tabs {
  display: flex;
  align-items: center;
  gap: 27px;
}

.event-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 12px 1px 13px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #607a90;
  font-size: 14px;
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
  min-width: 21px;
  height: 21px;
  place-items: center;
  padding: 0 6px;
  border-radius: 999px;
  background: #edf3f8;
  color: #617a8f;
  font-size: 11px;
  font-weight: 500;
}

.event-tab--active .event-tab__count {
  background: #e8f2fc;
  color: #176cc0;
}

.event-toolbar__filters {
  display: flex;
  align-items: center;
  gap: 12px;
}

.event-filter {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #71869a;
  font-size: 12px;
  white-space: nowrap;
}

.event-filter select {
  width: 130px;
  height: 38px;
  padding: 0 32px 0 11px;
  border: 1px solid #cfdeea;
  border-radius: 6px;
  appearance: none;
  background:
    linear-gradient(45deg, transparent 50%, #6d8498 50%) calc(100% - 17px)
      50% / 5px 5px no-repeat,
    linear-gradient(135deg, #6d8498 50%, transparent 50%) calc(100% - 12px)
      50% / 5px 5px no-repeat,
    #fff;
  color: #405f78;
}

.event-search {
  position: relative;
  display: block;
  width: 280px;
}

.event-search input {
  width: 100%;
  height: 38px;
  padding: 0 13px 0 38px;
  border: 1px solid #cfdeea;
  border-radius: 6px;
  background: #fff;
  color: #385870;
  font-size: 12px;
}

.event-search input::placeholder {
  color: #96a7b5;
}

.event-search input:focus {
  border-color: #73a7d0;
  outline: none;
  box-shadow: 0 0 0 3px rgb(23 108 192 / 8%);
}

.event-search svg {
  position: absolute;
  z-index: 1;
  top: 50%;
  left: 12px;
  width: 17px;
  height: 17px;
  color: #7f95a6;
  transform: translateY(-50%);
  pointer-events: none;
}

.event-search__hidden-label {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.event-toolbar__reset {
  height: 38px;
  padding: 0 12px;
  border: 1px solid #d2dfe9;
  border-radius: 6px;
  background: #fff;
  color: #627c90;
  font-size: 12px;
}

.event-toolbar__reset:hover {
  border-color: #aac3d6;
  background: #f8fbfd;
}

@media (max-width: 960px) {
  .event-toolbar {
    align-items: stretch;
    flex-direction: column;
    gap: 14px;
  }

  .event-toolbar__filters {
    flex-wrap: wrap;
  }
}

@media (max-width: 620px) {
  .event-tabs {
    gap: 20px;
  }

  .event-toolbar__filters {
    align-items: stretch;
    flex-direction: column;
  }

  .event-filter {
    justify-content: space-between;
  }

  .event-filter select,
  .event-search {
    width: 100%;
  }

  .event-toolbar__reset {
    align-self: flex-start;
  }
}
</style>
