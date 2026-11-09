<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
import MaintenanceEventList from './components/MaintenanceEventList.vue'
import MaintenanceToolbar from './components/MaintenanceToolbar.vue'
import { useMaintenanceStore } from './stores/maintenance'

const MaintenanceEventDialog = defineAsyncComponent(
  () => import('./components/MaintenanceEventDialog.vue'),
)
const maintenance = useMaintenanceStore()
</script>

<template>
  <main class="maintenance-page">
    <div class="maintenance-page__inner">
      <header class="maintenance-page__header">
        <div>
          <small>PREDICTIVE MAINTENANCE</small>
          <h1>设备预测性维护</h1>
          <p>
            按报警与运行趋势分析探测器、备电状态，逐条核查 AI
            结论并保留人工确认记录。
          </p>
        </div>
        <span class="maintenance-page__mode">
          <i aria-hidden="true"></i>
          AI 辅助分析 · 演示模式
        </span>
      </header>

      <MaintenanceToolbar />
      <MaintenanceEventList />
    </div>

    <MaintenanceEventDialog
      v-if="maintenance.selectedEvent"
      :event="maintenance.selectedEvent"
    />

    <Transition name="notice">
      <div
        v-if="maintenance.notice"
        class="maintenance-notice"
        role="status"
        aria-live="polite"
      >
        <span aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M5.5 12.5L9.5 16.5L18.5 7.5"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        {{ maintenance.notice }}
      </div>
    </Transition>
  </main>
</template>

<style scoped>
.maintenance-page {
  min-height: 100vh;
  padding: 32px 34px 94px;
  background:
    radial-gradient(circle at 88% -5%, rgb(18 110 208 / 8%), transparent 34rem),
    linear-gradient(180deg, #edf3f8 0%, #f3f6f9 44%, #f5f7f9 100%);
}

.maintenance-page__inner {
  display: grid;
  width: min(100%, 1480px);
  gap: 20px;
  margin: 0 auto;
}

.maintenance-page__header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  padding: 4px 2px 8px;
}

.maintenance-page__header > div > small {
  color: #718ba0;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 1.7px;
}

.maintenance-page__header h1 {
  margin: 8px 0 8px;
  color: #153c5c;
  font-size: clamp(25px, 2.3vw, 32px);
  font-weight: 600;
  letter-spacing: 0.2px;
}

.maintenance-page__header p {
  max-width: 850px;
  margin: 0;
  color: #6e8598;
  font-size: 13px;
  line-height: 1.7;
}

.maintenance-page__mode {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
  padding: 7px 11px;
  border: 1px solid #d8e4ed;
  border-radius: 999px;
  background: rgb(255 255 255 / 78%);
  color: #718798;
  font-size: 11px;
}

.maintenance-page__mode i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #3c9b7a;
  box-shadow: 0 0 0 4px rgb(60 155 122 / 12%);
}

.maintenance-notice {
  position: fixed;
  z-index: 80;
  bottom: 24px;
  left: 50%;
  display: flex;
  align-items: center;
  gap: 9px;
  max-width: calc(100vw - 32px);
  padding: 10px 16px 10px 12px;
  border: 1px solid #d5e6df;
  border-radius: 8px;
  background: #f7fcf9;
  box-shadow: 0 13px 36px rgb(19 57 38 / 16%);
  color: #376f5a;
  font-size: 12px;
  transform: translateX(-50%);
}

.maintenance-notice > span {
  display: grid;
  width: 24px;
  height: 24px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: #def2e8;
  color: #2e8665;
}

.maintenance-notice svg {
  width: 16px;
  height: 16px;
}

.notice-enter-active,
.notice-leave-active {
  transition:
    opacity 160ms ease,
    transform 160ms ease;
}

.notice-enter-from,
.notice-leave-to {
  opacity: 0;
  transform: translate(-50%, 8px);
}

@media (max-width: 760px) {
  .maintenance-page {
    padding: 23px 17px 94px;
  }

  .maintenance-page__header {
    align-items: flex-start;
    flex-direction: column;
    gap: 13px;
  }

  .maintenance-page__mode {
    margin-bottom: 0;
  }

  .maintenance-notice {
    bottom: 16px;
  }
}
</style>
