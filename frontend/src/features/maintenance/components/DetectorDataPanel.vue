<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'
import type { MaintenanceEvent } from '../types'
import EChart from './EChart.vue'

const props = defineProps<{
  event: MaintenanceEvent
}>()

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
    data: props.event.detectorTrend.doseRate.map((point) => point.time),
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
      type: 'line',
      yAxisIndex: 0,
      data: props.event.detectorTrend.doseRate.map((point) => point.value),
      symbol: 'circle',
      symbolSize: 5,
      smooth: false,
      lineStyle: {
        width: 2,
      },
      areaStyle: {
        color: 'rgba(25, 118, 201, 0.08)',
      },
    },
    {
      name: '雨量',
      type: 'bar',
      yAxisIndex: 1,
      data: props.event.detectorTrend.rainfall.map((point) => point.value),
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
        <span class="data-panel__eyebrow">DATABASE TREND</span>
        <h3 id="detector-data-title">电离室 · 剂量率与雨量</h3>
        <p>近 24 小时趋势，曲线为剂量率，柱状为降雨量</p>
      </div>
      <span class="data-panel__source">演示数据 · 待接入数据库</span>
    </header>
    <EChart
      :option="chartOption"
      label="近 24 小时剂量率折线图与降雨量柱状图"
    />
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
  margin-bottom: 2px;
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

@media (max-width: 640px) {
  .data-panel {
    padding: 17px 12px 8px;
  }

  .data-panel__heading {
    flex-direction: column;
    gap: 9px;
  }
}
</style>
