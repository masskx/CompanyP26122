<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use, type EChartsCoreOption } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([
  BarChart,
  LineChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = defineProps<{
  option: EChartsCoreOption
  label: string
}>()

const chartElement = ref<HTMLElement | null>(null)
let chart: ReturnType<typeof init> | undefined
let resizeObserver: ResizeObserver | undefined

onMounted(() => {
  if (!chartElement.value) {
    return
  }

  chart = init(chartElement.value)
  chart.setOption(props.option)

  resizeObserver = new ResizeObserver(() => {
    chart?.resize()
  })
  resizeObserver.observe(chartElement.value)
})

watch(
  () => props.option,
  (option) => {
    chart?.setOption(option, { notMerge: true })
  },
  { deep: true },
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div ref="chartElement" class="echart" role="img" :aria-label="label"></div>
</template>

<style scoped>
.echart {
  width: 100%;
  height: 270px;
}
</style>
