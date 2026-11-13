import type { AssistantPrompt } from './types'

export const ASSISTANT_PROMPTS: AssistantPrompt[] = [
  {
    id: 'maintenance-advice',
    title: '设备维护建议',
    description: '按站点查看备电与探测器维护优先级',
    prompt:
      '请根据当前预测维护事件，给出 AS06、AS12 和 AS08 的维护优先级建议。',
    icon: 'maintenance',
  },
  {
    id: 'event-review',
    title: '维护事件解读',
    description: '解释 AI 风险结论与人工核查依据',
    prompt:
      '请解读 AS06 备电续航下降事件的 AI 结论，并说明需要人工核查什么。',
    icon: 'event',
  },
  {
    id: 'detector-status',
    title: '探测器状态分析',
    description: '汇总异常、离线情况与近期报警趋势',
    prompt: '请汇总当前探测器异常、离线情况和近期报警趋势。',
    icon: 'detector',
  },
  {
    id: 'knowledge-search',
    title: '维护知识检索',
    description: '检索规程、告警处置和备电要求',
    prompt: '请检索备电续航下降和探测器告警的处置要求。',
    icon: 'knowledge',
  },
]
