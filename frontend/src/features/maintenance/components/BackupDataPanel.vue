<script setup lang="ts">
import type { MaintenanceEvent } from '../types'
import { formatPercent } from '../utils'

const props = defineProps<{
  event: MaintenanceEvent
}>()
</script>

<template>
  <section
    class="data-panel data-panel--backup"
    aria-labelledby="backup-data-title"
  >
    <header class="data-panel__heading">
      <div>
        <p>备电状态</p>
        <h3 id="backup-data-title">近 24 小时电量与续航参考</h3>
      </div>
      <span class="data-panel__source">演示数据 · 待接入数据库</span>
    </header>

    <div class="backup-summary">
      <section class="charge-summary" aria-labelledby="charge-title">
        <div class="charge-summary__label">
          <h4 id="charge-title">当前电量</h4>
          <p>电量不等同于剩余容量</p>
        </div>
        <strong>
          {{ formatPercent(props.event.backupTrend.currentBattery / 100) }}
        </strong>
      </section>

      <section class="runtime-summary" aria-labelledby="runtime-title">
        <header>
          <h4 id="runtime-title">续航参考</h4>
          <span>相近负载条件</span>
        </header>

        <div class="runtime-values">
          <div>
            <span>当前估算</span>
            <strong>{{ props.event.backupTrend.estimatedRuntimeHours }} h</strong>
          </div>
          <i aria-hidden="true"></i>
          <div>
            <span>历史参考</span>
            <strong>
              {{ props.event.backupTrend.historicalRuntimeHours }} h
            </strong>
          </div>
        </div>

        <p>需核对完整充放电过程</p>
      </section>
    </div>
  </section>
</template>

<style scoped>
.data-panel {
  display: flex;
  height: 100%;
  min-width: 0;
  flex-direction: column;
  gap: 16px;
  padding: 18px;
  border: 1px solid var(--color-line);
  border-radius: var(--radius-panel);
  background: #fff;
}

.data-panel__heading {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px 16px;
}

.data-panel__heading > div {
  min-width: 0;
  flex: 1 1 210px;
}

.data-panel__heading p {
  margin: 0 0 5px;
  color: #5f7487;
  font-size: 12px;
  line-height: 1.4;
}

.data-panel__heading h3 {
  margin: 0;
  color: #244b69;
  font-size: 17px;
  font-weight: 650;
  line-height: 1.45;
}

.data-panel__source {
  flex: none;
  padding: 5px 9px;
  border-radius: 999px;
  background: #f1f5f8;
  color: #65798b;
  font-size: 12px;
  line-height: 1.4;
}

.backup-summary {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  gap: 14px;
}

.charge-summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: center;
  padding: 20px;
  border: 1px solid #d4e4f0;
  border-radius: 12px;
  background: linear-gradient(135deg, #f4f9fd 0%, #f9fcff 100%);
}

.charge-summary__label h4,
.runtime-summary header h4 {
  margin: 0;
  color: #2f5571;
  font-size: 14px;
  font-weight: 650;
}

.charge-summary__label p {
  margin: 7px 0 0;
  color: #5f7487;
  font-size: 12px;
  line-height: 1.55;
}

.charge-summary > strong {
  color: #174d79;
  font-size: 36px;
  font-weight: 680;
  letter-spacing: -0.6px;
  line-height: 1;
}

.runtime-summary {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  padding: 18px;
  border: 1px solid #e0e9f0;
  border-radius: 12px;
  background: #fbfdff;
}

.runtime-summary > header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.runtime-summary > header span,
.runtime-summary > p,
.runtime-values span {
  color: #65798b;
  font-size: 12px;
  line-height: 1.5;
}

.runtime-values {
  display: grid;
  flex: 1;
  grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1fr);
  gap: 16px;
  align-items: center;
  margin: 12px 0;
}

.runtime-values > div {
  display: grid;
  gap: 8px;
}

.runtime-values > div:last-child {
  text-align: right;
}

.runtime-values strong {
  color: #345c79;
  font-size: 28px;
  font-weight: 680;
  line-height: 1.1;
  white-space: nowrap;
}

.runtime-values > div:first-child strong {
  color: #1767ad;
}

.runtime-values > i {
  align-self: stretch;
  width: 1px;
  background: #e0e9f0;
}

.runtime-summary > p {
  margin: 0;
}

@media (max-width: 640px) {
  .data-panel {
    padding: 16px;
  }

  .data-panel__heading {
    flex-direction: column;
    gap: 10px;
  }
}
</style>
