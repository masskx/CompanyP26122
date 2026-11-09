<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'
import type { MaintenanceEvent } from '../types'
import { formatPercent } from '../utils'
import EChart from './EChart.vue'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const chartOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 420,
  color: ['#1976c9'],
  tooltip: {
    trigger: 'axis',
    backgroundColor: 'rgba(255, 255, 255, 0.98)',
    borderColor: '#d8e4ee',
    textStyle: {
      color: '#36536b',
      fontSize: 12,
    },
  },
  legend: {
    top: 0,
    right: 2,
    icon: 'roundRect',
    itemWidth: 10,
    itemHeight: 7,
    textStyle: {
      color: '#667f93',
      fontSize: 11,
    },
  },
  grid: {
    top: 48,
    right: 26,
    bottom: 30,
    left: 50,
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: props.event.backupTrend.battery.map((point) => point.time),
    axisLine: {
      lineStyle: {
        color: '#dce6ee',
      },
    },
    axisTick: {
      show: false,
    },
    axisLabel: {
      color: '#8499aa',
      fontSize: 11,
    },
  },
  yAxis: {
    type: 'value',
    name: '%',
    min: 50,
    max: 100,
    interval: 10,
    nameTextStyle: {
      color: '#8a9dab',
      fontSize: 11,
    },
    axisLabel: {
      color: '#8499aa',
      fontSize: 11,
    },
    splitLine: {
      lineStyle: {
        color: '#edf2f6',
      },
    },
  },
  series: [
    {
      name: '电池电量',
      type: 'line',
      data: props.event.backupTrend.battery.map((point) => point.value),
      symbol: 'circle',
      symbolSize: 5,
      smooth: true,
      lineStyle: {
        width: 2,
      },
      areaStyle: {
        color: 'rgba(25, 118, 201, 0.08)',
      },
    },
  ],
}))
</script>

<template>
  <section class="data-panel" aria-labelledby="backup-data-title">
    <header class="data-panel__heading">
      <div>
        <span class="data-panel__eyebrow">POWER REFERENCE</span>
        <h3 id="backup-data-title">备电 · 近 24 小时电量与续航参考</h3>
        <p>电量来自设备记录，续航参考用于辅助判断容量趋势</p>
      </div>
      <span class="data-panel__source">演示数据 · 待接入数据库</span>
    </header>

    <div class="backup-stats">
      <div>
        <span>当前电量</span>
        <strong>{{ formatPercent(event.backupTrend.currentBattery / 100) }}</strong>
        <small>电量不等同于剩余容量</small>
      </div>
      <div>
        <span>参考续航</span>
        <strong>{{ event.backupTrend.estimatedRuntimeHours }} h</strong>
        <small>相近负载条件</small>
      </div>
      <div>
        <span>历史参考</span>
        <strong>{{ event.backupTrend.historicalRuntimeHours }} h</strong>
        <small>需核对完整充放电过程</small>
      </div>
    </div>

    <EChart :option="chartOption" label="近 24 小时备电电量折线图" />
  </section>
</template>

<style scoped>
.data-panel {
  min-width: 0;
  padding: 20px 20px 12px;
  border: 1px solid #dce7f0;
  border-radius: 9px;
  background: #fff;
}

.data-panel__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 17px;
}

.data-panel__eyebrow {
  color: #8aa0b1;
  font-size: 9px;
  letter-spacing: 1.45px;
}

.data-panel__heading h3 {
  margin: 5px 0 5px;
  color: #294f6d;
  font-size: 15px;
  font-weight: 600;
}

.data-panel__heading p {
  margin: 0;
  color: #8498a8;
  font-size: 11px;
}

.data-panel__source {
  flex: none;
  padding: 4px 8px;
  border-radius: 999px;
  background: #f1f5f8;
  color: #7a8e9f;
  font-size: 10px;
}

.backup-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-bottom: 8px;
  border: 1px solid #e2eaf1;
  border-radius: 8px;
  background: #fbfdff;
}

.backup-stats > div {
  min-width: 0;
  padding: 14px 16px;
  border-right: 1px solid #e5ecf2;
}

.backup-stats > div:last-child {
  border-right: 0;
}

.backup-stats span,
.backup-stats small {
  display: block;
  color: #8397a8;
  font-size: 11px;
}

.backup-stats strong {
  display: block;
  margin: 6px 0 5px;
  color: #234d70;
  font-size: 25px;
  font-weight: 600;
  line-height: 1.1;
}

@media (max-width: 640px) {
  .data-panel {
    padding: 17px 12px 8px;
  }

  .data-panel__heading {
    flex-direction: column;
    gap: 9px;
  }

  .backup-stats {
    grid-template-columns: 1fr;
  }

  .backup-stats > div {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 2px 14px;
    border-right: 0;
    border-bottom: 1px solid #e5ecf2;
  }

  .backup-stats > div:last-child {
    border-bottom: 0;
  }

  .backup-stats strong {
    grid-column: 2;
    grid-row: 1 / 3;
    margin: 0;
  }
}
</style>
