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
        <h1>设备预测性维护</h1>
        <p>
          按报警与运行趋势分析探测器、备电状态，逐条核查 AI
          结论并保留人工审核记录。
        </p>
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
  padding: 30px 34px 94px;
  background:
    radial-gradient(circle at 88% -5%, rgb(18 110 208 / 8%), transparent 34rem),
    linear-gradient(180deg, #edf3f8 0%, #f3f6f9 44%, #f5f7f9 100%);
}

.maintenance-page__inner {
  display: grid;
  width: min(100%, 1480px);
  gap: 14px;
  margin: 0 auto;
}

.maintenance-page__header {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 24px;
  padding: 4px 2px 2px;
}

.maintenance-page__header h1 {
  flex: none;
  margin: 0;
  color: #153c5c;
  font-size: clamp(26px, 2.2vw, 28px);
  font-weight: 680;
  letter-spacing: 0.2px;
}

.maintenance-page__header p {
  min-width: 0;
  max-width: 920px;
  margin: 0;
  color: #526c80;
  font-size: 14px;
  line-height: 1.65;
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
  font-size: 13px;
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
    gap: 8px;
  }

  .maintenance-notice {
    bottom: 16px;
  }
}
</style>
