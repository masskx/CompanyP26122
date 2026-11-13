<script setup lang="ts">
import { computed } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'

const maintenance = useMaintenanceStore()

const hasFilters = computed(
  () =>
    maintenance.filters.siteId !== 'all' ||
    maintenance.filters.equipmentType !== 'all' ||
    maintenance.filters.keyword.trim().length > 0,
)
</script>

<template>
  <div class="event-filters" aria-label="事件筛选">
    <label class="event-filter event-filter--site">
      <span>站点</span>
      <select v-model="maintenance.filters.siteId">
        <option value="all">全部站点</option>
        <option
          v-for="site in maintenance.siteOptions"
          :key="site.value"
          :value="site.value"
        >
          {{ site.label }}
        </option>
      </select>
    </label>

    <label class="event-filter">
      <span>设备类型</span>
      <select v-model="maintenance.filters.equipmentType">
        <option value="all">全部</option>
        <option value="detector">探测器</option>
        <option value="backup_power">备电</option>
      </select>
    </label>

    <label class="event-search">
      <span class="event-search__hidden-label">搜索事件</span>
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
      class="event-filters__reset"
      @click="maintenance.resetFilters"
    >
      重置
    </button>
  </div>
</template>

<style scoped>
.event-filters {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  min-width: 0;
}

.event-filter {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #536b80;
  font-size: 12px;
  white-space: nowrap;
}

.event-filter select {
  width: 130px;
  height: 40px;
  padding: 0 32px 0 11px;
  border: 1px solid #cfdeea;
  border-radius: 8px;
  appearance: none;
  background:
    linear-gradient(45deg, transparent 50%, #6d8498 50%) calc(100% - 17px)
      50% / 5px 5px no-repeat,
    linear-gradient(135deg, #6d8498 50%, transparent 50%) calc(100% - 12px)
      50% / 5px 5px no-repeat,
    #fff;
  color: #405f78;
}

.event-filter--site select {
  width: 180px;
}

.event-search {
  position: relative;
  display: block;
  width: min(300px, 28vw);
  min-width: 230px;
}

.event-search input {
  width: 100%;
  height: 40px;
  padding: 0 13px 0 38px;
  border: 1px solid #cfdeea;
  border-radius: 8px;
  background: #fff;
  color: #385870;
  font-size: 13px;
}

.event-search input::placeholder {
  color: #718596;
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

.event-filters__reset {
  height: 40px;
  padding: 0 14px;
  border: 1px solid #d2dfe9;
  border-radius: 8px;
  background: #fff;
  color: #526c80;
  font-size: 13px;
}

.event-filters__reset:hover {
  border-color: #aac3d6;
  background: #f8fbfd;
}

@media (max-width: 960px) {
  .event-filters {
    flex-wrap: wrap;
    justify-content: flex-start;
  }
}

@media (max-width: 620px) {
  .event-filters {
    align-items: stretch;
    flex-direction: column;
  }

  .event-filter {
    justify-content: space-between;
  }

  .event-filter select,
  .event-search {
    width: 100%;
    min-width: 0;
  }

  .event-filters__reset {
    align-self: flex-start;
  }
}
</style>
