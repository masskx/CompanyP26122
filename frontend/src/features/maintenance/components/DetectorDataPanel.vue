<script setup lang="ts">
import { computed, ref } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'
import type {
  DetectorTrendPeriod,
  MaintenanceEvent,
  TimeSeriesPoint,
} from '../types'
import EChart from './EChart.vue'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const periods: Array<{
  key: DetectorTrendPeriod
  label: string
  range: string
}> = [
  {
    key: 'current24h',
    label: '近 24 小时',
    range: '事件发生前 24 小时',
  },
  {
    key: 'lastMonth',
    label: '上月同期',
    range: '上月同一 24 小时窗口',
  },
  {
    key: 'lastYear',
    label: '去年同期',
    range: '去年同一 24 小时窗口',
  },
]

const activePeriod = ref<DetectorTrendPeriod>('current24h')

const activeData = computed(
  () => props.event.detectorTrend[activePeriod.value],
)

const activePeriodMeta = computed(
  () =>
    periods.find((period) => period.key === activePeriod.value) ?? periods[0],
)

const timeLabels = computed(() =>
  Array.from(
    new Set([
      ...activeData.value.doseRate.map((point) => point.time),
      ...activeData.value.rainfall.map((point) => point.time),
    ]),
  ),
)

function alignSeries(points: TimeSeriesPoint[]) {
  const valuesByTime = new Map(
    points.map((point) => [point.time, point.value]),
  )

  return timeLabels.value.map((time) => valuesByTime.get(time) ?? null)
}

function missingCount(points: TimeSeriesPoint[]) {
  return timeLabels.value.length - alignSeries(points).filter(
    (value) => value !== null,
  ).length
}

const doseRateMissing = computed(() =>
  missingCount(activeData.value.doseRate),
)

const rainfallMissing = computed(() =>
  missingCount(activeData.value.rainfall),
)

const sampleSummary = computed(() => {
  const total = timeLabels.value.length

  return {
    total,
    doseValid: total - doseRateMissing.value,
    rainfallValid: total - rainfallMissing.value,
  }
})

function movePeriod(offset: number, event: KeyboardEvent) {
  const currentIndex = periods.findIndex(
    (period) => period.key === activePeriod.value,
  )
  const nextIndex =
    (currentIndex + offset + periods.length) % periods.length
  const nextPeriod = periods[nextIndex]

  if (!nextPeriod) {
    return
  }

  activePeriod.value = nextPeriod.key
  const switchElement = (event.currentTarget as HTMLElement).closest(
    '.period-switch',
  )

  requestAnimationFrame(() => {
    const buttons =
      switchElement?.querySelectorAll<HTMLButtonElement>('button')
    buttons?.[nextIndex]?.focus()
  })
}

const chartOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 420,
  color: ['#1976c9', '#86bfe8'],
  tooltip: {
    trigger: 'axis',
    axisPointer: {
      type: 'line',
    },
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
    right: 54,
    bottom: 30,
    left: 54,
  },
  xAxis: {
    type: 'category',
    boundaryGap: true,
    data: timeLabels.value,
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
  yAxis: [
    {
      type: 'value',
      name: 'μGy/h',
      min: 0.035,
      max: 0.1,
      interval: 0.01,
      nameTextStyle: {
        color: '#8a9dab',
        fontSize: 11,
        padding: [0, 0, 0, -18],
      },
      axisLabel: {
        color: '#8499aa',
        fontSize: 11,
        formatter: (value: number) => value.toFixed(3),
      },
      splitLine: {
        lineStyle: {
          color: '#edf2f6',
        },
      },
    },
    {
      type: 'value',
      name: 'mm',
      min: 0,
      max: 3,
      interval: 0.5,
      nameTextStyle: {
        color: '#8a9dab',
        fontSize: 11,
        padding: [0, -12, 0, 0],
      },
      axisLabel: {
        color: '#8499aa',
        fontSize: 11,
        formatter: (value: number) => value.toFixed(1),
      },
      splitLine: {
        show: false,
      },
    },
  ],
  series: [
    {
      name: '剂量率',
      type: 'scatter',
      yAxisIndex: 0,
      data: alignSeries(activeData.value.doseRate),
      symbol: 'circle',
      symbolSize: 7,
      itemStyle: {
        borderColor: '#fff',
        borderWidth: 1.5,
      },
      emphasis: {
        scale: 1.35,
      },
    },
    {
      name: '雨量',
      type: 'bar',
      yAxisIndex: 1,
      data: alignSeries(activeData.value.rainfall),
      barWidth: 10,
      itemStyle: {
        borderRadius: [3, 3, 0, 0],
      },
    },
  ],
}))
</script>

<template>
  <section class="data-panel" aria-labelledby="detector-data-title">
    <header class="data-panel__heading">
      <div>
        <h3 id="detector-data-title">电离室 · 剂量率与雨量</h3>
        <p>离散采样 · 缺测不补零、不连线</p>
      </div>
    </header>

    <div class="period-controls">
      <div
        class="period-switch"
        role="tablist"
        aria-label="电离室数据时间范围"
      >
        <button
          v-for="period in periods"
          :key="period.key"
          :id="`detector-period-${period.key}`"
          type="button"
          role="tab"
          aria-controls="detector-period-panel"
          :aria-selected="activePeriod === period.key"
          :tabindex="activePeriod === period.key ? 0 : -1"
          :class="{ active: activePeriod === period.key }"
          @click="activePeriod = period.key"
          @keydown.left.prevent="movePeriod(-1, $event)"
          @keydown.right.prevent="movePeriod(1, $event)"
        >
          {{ period.label }}
        </button>
      </div>
      <span>{{ activePeriodMeta.range }}</span>
    </div>

    <div
      id="detector-period-panel"
      class="period-panel"
      role="tabpanel"
      :aria-labelledby="`detector-period-${activePeriod}`"
    >
      <EChart
        :option="chartOption"
        :label="`${activePeriodMeta.label}剂量率散点图与降雨量柱状图`"
        height="240px"
      />

      <div class="sampling-summary" aria-live="polite">
        <span>时段采样点 {{ sampleSummary.total }} 个</span>
        <span :data-missing="doseRateMissing > 0">
          剂量率有效 {{ sampleSummary.doseValid }} 个
          <template v-if="doseRateMissing > 0">
            · 缺测 {{ doseRateMissing }} 个
          </template>
        </span>
        <span :data-missing="rainfallMissing > 0">
          雨量有效 {{ sampleSummary.rainfallValid }} 个
          <template v-if="rainfallMissing > 0">
            · 缺测 {{ rainfallMissing }} 个
          </template>
        </span>
      </div>

    </div>
  </section>
</template>

<style scoped>
.data-panel {
  display: flex;
  height: 100%;
  min-width: 0;
  flex-direction: column;
  padding: 16px;
  border: 1px solid var(--color-line);
  border-radius: var(--radius-panel);
  background: #fff;
}

.data-panel__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 2px;
}

.data-panel__heading h3 {
  margin: 0 0 5px;
  color: #244b69;
  font-size: 17px;
  font-weight: 650;
  line-height: 1.45;
}

.data-panel__heading p {
  margin: 0;
  color: #5f7487;
  font-size: 12px;
  line-height: 1.55;
}

.period-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 12px;
}

.period-panel {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
}

.period-controls > span {
  color: #60788b;
  font-size: 12px;
  white-space: nowrap;
}

.period-switch {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid #d6e2eb;
  border-radius: 8px;
  background: #f4f8fb;
}

.period-switch button {
  min-height: 36px;
  padding: 0 13px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #526c80;
  font-size: 12px;
  white-space: nowrap;
}

.period-switch button:hover {
  color: #176cc0;
}

.period-switch button.active {
  background: #fff;
  box-shadow: 0 1px 4px rgb(28 71 104 / 12%);
  color: #176cc0;
  font-weight: 600;
}

.sampling-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin: 0;
  padding: 0 1px;
}

.sampling-summary span {
  padding: 4px 8px;
  border-radius: 999px;
  background: #f2f6f9;
  color: #5f7487;
  font-size: 12px;
  line-height: 1.4;
}

.sampling-summary span[data-missing="true"] {
  background: #fff6e6;
  color: #9a6918;
}

@media (max-width: 640px) {
  .data-panel {
    padding: 16px;
  }

  .data-panel__heading {
    flex-direction: column;
    gap: 9px;
  }

  .period-controls {
    align-items: flex-start;
    flex-direction: column;
    gap: 9px;
    margin-top: 14px;
  }

  .period-switch {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .period-switch button {
    padding: 0 5px;
  }
}
</style>
