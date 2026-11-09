import type {
  AiAnalysis,
  AnalysisRecommendation,
  BackupTrend,
  DetectorTrend,
  MaintenanceEvent,
} from '../types'

const CHART_TIMES = [
  '00:00',
  '03:00',
  '06:00',
  '09:00',
  '12:00',
  '15:00',
  '18:00',
  '21:00',
  '24:00',
]

const DOSE_RATE_BASE = [
  0.05, 0.056, 0.052, 0.047, 0.054, 0.062, 0.081, 0.069, 0.051,
]
const RAINFALL_BASE = [0, 0, 0, 0.1, 0.8, 2.2, 2.8, 1.4, 0.3]
const BATTERY_BASE = [96, 95, 93, 91, 88, 83, 78, 72, 62]

function createDetectorTrend(
  doseMultiplier = 1,
  rainfallMultiplier = 1,
): DetectorTrend {
  return {
    doseRate: CHART_TIMES.map((time, index) => ({
      time,
      value: Number((DOSE_RATE_BASE[index] * doseMultiplier).toFixed(3)),
    })),
    rainfall: CHART_TIMES.map((time, index) => ({
      time,
      value: Number((RAINFALL_BASE[index] * rainfallMultiplier).toFixed(1)),
    })),
  }
}

function createBackupTrend(
  currentBattery: number,
  estimatedRuntimeHours: number,
  historicalRuntimeHours: number,
): BackupTrend {
  return {
    battery: CHART_TIMES.map((time, index) => {
      if (index === CHART_TIMES.length - 1) {
        return { time, value: currentBattery }
      }

      return {
        time,
        value: Math.round(BATTERY_BASE[index] * (currentBattery / 62)),
      }
    }),
    currentBattery,
    estimatedRuntimeHours,
    historicalRuntimeHours,
  }
}

function mediumPriority(
  action: string,
  rationale: string,
): AnalysisRecommendation {
  return {
    priority: 'medium',
    action,
    rationale,
    requiresHumanConfirmation: true,
  }
}

function highPriority(
  action: string,
  rationale: string,
): AnalysisRecommendation {
  return {
    priority: 'high',
    action,
    rationale,
    requiresHumanConfirmation: true,
  }
}

function createDetectorAnalysis(
  siteId: string,
  eventTitle: string,
  dosePeak: number,
  rainfallPeak: number,
  confidence: number,
  generatedAt: string,
  summary?: string,
): AiAnalysis {
  return {
    summary:
      summary ??
      `${siteId} 站点在${eventTitle}期间出现剂量率短时升高，峰值约 ${dosePeak.toFixed(3)} μGy/h；同期降雨量最高约 ${rainfallPeak.toFixed(1)} mm。现有证据更支持先行核查降雨和环境因素，暂不能仅凭该波动确认电离室故障。`,
    evidence: [
      {
        source: 'MES 监测数据库',
        metric: 'γ 剂量率',
        timeRange: '近 24 小时',
        observation: `剂量率由平稳区间短时升至 ${dosePeak.toFixed(3)} μGy/h，随后回落。`,
      },
      {
        source: 'MES 监测数据库',
        metric: '降雨量',
        timeRange: '近 24 小时',
        observation: `剂量率升高时段与降雨峰值 ${rainfallPeak.toFixed(1)} mm 部分重合。`,
      },
      {
        source: '设备快照',
        metric: '设备状态',
        timeRange: '当前',
        observation: '设备通信与供电状态正常，未发现离线或供电中断证据。',
      },
    ],
    recommendations: [
      mediumPriority(
        '核对降雨、温湿度及周边作业记录',
        '当前剂量率变化与降雨时段重合，应先排除环境因素。',
      ),
      mediumPriority(
        '复核探测器高压、通信和自检记录',
        '若环境因素无法解释，再检查设备运行参数和自检结果。',
      ),
    ],
    dataGaps: [
      '尚缺探测器高压与能量谱原始记录。',
      '尚缺同站点更长历史窗口的降雨修正参考。',
    ],
    followUp:
      '记录现场核查结果，并对比处理前后剂量率和降雨趋势；若异常持续或与降雨无相关，再升级检查范围。',
    confidence,
    modelName: 'Qwen3-30B-A3B',
    modelPrecision: 'W8A8',
    generatedAt,
  }
}

function createBackupAnalysis(
  siteId: string,
  currentBattery: number,
  estimatedRuntimeHours: number,
  historicalRuntimeHours: number,
  confidence: number,
  generatedAt: string,
  summary?: string,
): AiAnalysis {
  const runtimeDrop = Math.round(
    (1 - estimatedRuntimeHours / historicalRuntimeHours) * 100,
  )

  return {
    summary:
      summary ??
      `${siteId} 备电记录显示，相近负载下参考续航由 ${historicalRuntimeHours} 小时降至 ${estimatedRuntimeHours} 小时，下降约 ${runtimeDrop}%；当前电量为 ${currentBattery}%。电量低不等同于容量损耗，需要结合完整充放电记录继续验证。`,
    evidence: [
      {
        source: 'MES 监测数据库',
        metric: '参考续航',
        timeRange: '近 24 小时',
        observation: `相近负载条件下参考续航由 ${historicalRuntimeHours} 小时降至 ${estimatedRuntimeHours} 小时，下降约 ${runtimeDrop}%。`,
      },
      {
        source: 'MES 监测数据库',
        metric: '电池电量',
        timeRange: '近 24 小时',
        observation: `当前电量 ${currentBattery}%，当日电量整体呈持续下降趋势。`,
      },
      {
        source: '历史维护记录',
        metric: '备电运行基线',
        timeRange: '历史同期',
        observation: '未发现近期更换电池或调整负载的记录。',
      },
    ],
    recommendations: [
      highPriority(
        '检查充电模块、接线与实际负载',
        '续航下降可能与充电恢复、接线状态或负载变化有关。',
      ),
      mediumPriority(
        '安排受控容量测试',
        '只有受控放电结果才能判断电池容量是否发生实质衰减。',
      ),
      mediumPriority(
        '核对充电恢复过程',
        '结合完整充放电记录确认是否存在充电时间延长或恢复中断。',
      ),
    ],
    dataGaps: [
      '尚缺完整充放电曲线、环境温度和负载记录。',
      '历史基线仅覆盖近期同负载时段。',
    ],
    followUp:
      '记录人工检查和容量测试结果，对比处理前后的续航与充电恢复情况；若容量衰减得到确认，再评估更换电池。',
    confidence,
    modelName: 'Qwen3-30B-A3B',
    modelPrecision: 'W8A8',
    generatedAt,
  }
}

export function createDemoEvents(): MaintenanceEvent[] {
  return [
    {
      id: 'ev-1015-006',
      eventNumber: 'EV-1015-006',
      triggeredAt: '2026-10-15T10:20:00+08:00',
      siteId: 'AS06',
      siteName: '监测站 06',
      equipmentType: 'backup_power',
      equipmentName: '备电系统',
      eventTitle: '备电续航下降事件',
      eventSource: 'run_trend',
      riskLevel: 'inspect',
      status: 'pending',
      detectorTrend: createDetectorTrend(0.94, 1.2),
      backupTrend: createBackupTrend(62, 18, 24),
      aiAnalysis: createBackupAnalysis(
        'AS06',
        62,
        18,
        24,
        0.84,
        '2026-10-15T10:20:00+08:00',
      ),
    },
    {
      id: 'ev-1015-005',
      eventNumber: 'EV-1015-005',
      triggeredAt: '2026-10-15T09:42:00+08:00',
      siteId: 'AS06',
      siteName: '监测站 06',
      equipmentType: 'detector',
      equipmentName: '电离室探测器',
      eventTitle: '剂量率短时波动事件',
      eventSource: 'monitoring_anomaly',
      riskLevel: 'watch',
      status: 'pending',
      detectorTrend: createDetectorTrend(1.08, 1),
      backupTrend: createBackupTrend(73, 21, 23),
      aiAnalysis: createDetectorAnalysis(
        'AS06',
        '剂量率短时波动',
        0.087,
        2.8,
        0.79,
        '2026-10-15T09:42:00+08:00',
      ),
    },
    {
      id: 'ev-1015-004',
      eventNumber: 'EV-1015-004',
      triggeredAt: '2026-10-15T09:10:00+08:00',
      siteId: 'AS12',
      siteName: '高温堆西南侧',
      equipmentType: 'detector',
      equipmentName: '电离室探测器',
      eventTitle: '电离室剂量率波动事件',
      eventSource: 'monitoring_anomaly',
      riskLevel: 'watch',
      status: 'pending',
      detectorTrend: createDetectorTrend(1.02, 0.9),
      backupTrend: createBackupTrend(69, 20, 22),
      aiAnalysis: createDetectorAnalysis(
        'AS12',
        '剂量率波动',
        0.083,
        2.5,
        0.76,
        '2026-10-15T09:10:00+08:00',
      ),
    },
    {
      id: 'ev-1015-003',
      eventNumber: 'EV-1015-003',
      triggeredAt: '2026-10-15T08:45:00+08:00',
      siteId: 'BS11',
      siteName: '东钱家村',
      equipmentType: 'backup_power',
      equipmentName: '备电系统',
      eventTitle: '备电充电恢复偏慢',
      eventSource: 'run_trend',
      riskLevel: 'watch',
      status: 'pending',
      detectorTrend: createDetectorTrend(0.88, 0.4),
      backupTrend: createBackupTrend(74, 20, 22),
      aiAnalysis: createBackupAnalysis(
        'BS11',
        74,
        20,
        22,
        0.74,
        '2026-10-15T08:45:00+08:00',
        '市电恢复后备电充电时间较历史同负载时段增加，当前电量已回升至 74%，但恢复速度偏慢。现有证据提示充电链路或电池状态需要进一步核查。',
      ),
    },
    {
      id: 'ev-1015-002',
      eventNumber: 'EV-1015-002',
      triggeredAt: '2026-10-15T08:20:00+08:00',
      siteId: 'AS08',
      siteName: '监测站 08',
      equipmentType: 'detector',
      equipmentName: '电离室探测器',
      eventTitle: '探测器设备故障报警',
      eventSource: 'device_alarm',
      riskLevel: 'insufficient_data',
      status: 'pending',
      detectorTrend: createDetectorTrend(0.72, 0.1),
      backupTrend: createBackupTrend(65, 19, 21),
      aiAnalysis: createDetectorAnalysis(
        'AS08',
        '设备故障报警',
        0.059,
        0.3,
        0.46,
        '2026-10-15T08:20:00+08:00',
        'AS08 站点存在探测器设备故障标记，但当前可读取数据不足以定位具体故障部件。建议保持设备告警状态并优先核查通信、自检和供电记录，系统未对缺失数据进行推测补齐。',
      ),
    },
    {
      id: 'ev-1015-001',
      eventNumber: 'EV-1015-001',
      triggeredAt: '2026-10-15T07:55:00+08:00',
      siteId: 'BS13',
      siteName: '东墩村',
      equipmentType: 'backup_power',
      equipmentName: '备电系统',
      eventTitle: '备电电压波动事件',
      eventSource: 'run_trend',
      riskLevel: 'watch',
      status: 'pending',
      detectorTrend: createDetectorTrend(0.91, 0.2),
      backupTrend: createBackupTrend(68, 19, 23),
      aiAnalysis: createBackupAnalysis(
        'BS13',
        68,
        19,
        23,
        0.71,
        '2026-10-15T07:55:00+08:00',
        'BS13 备电电压在近 24 小时内出现短时波动，参考续航低于历史均值。当前数据不能直接判定电池失效，需要结合充电模块和负载记录继续核查。',
      ),
    },
    {
      id: 'ev-1014-009',
      eventNumber: 'EV-1014-009',
      triggeredAt: '2026-10-14T16:50:00+08:00',
      siteId: 'AS03',
      siteName: '升压站南部',
      equipmentType: 'detector',
      equipmentName: '电离室探测器',
      eventTitle: '剂量率与降雨相关波动',
      eventSource: 'monitoring_anomaly',
      riskLevel: 'watch',
      status: 'confirmed',
      detectorTrend: createDetectorTrend(0.98, 1.1),
      backupTrend: createBackupTrend(78, 23, 24),
      aiAnalysis: createDetectorAnalysis(
        'AS03',
        '剂量率与降雨相关波动',
        0.079,
        2.6,
        0.82,
        '2026-10-14T16:50:00+08:00',
      ),
      aiFeedback: {
        rating: 'accurate',
        comment: '现场核查结果与降雨影响判断一致。',
        submittedAt: '2026-10-14T17:18:00+08:00',
      },
      confirmation: {
        decision: 'accepted',
        comment: '已核对现场降雨和周边作业记录，认可本次 AI 分析。',
        confirmedAt: '2026-10-14T17:30:00+08:00',
        confirmedBy: '值班操作员',
      },
    },
    {
      id: 'ev-1014-008',
      eventNumber: 'EV-1014-008',
      triggeredAt: '2026-10-14T14:20:00+08:00',
      siteId: 'AS02',
      siteName: '综合楼',
      equipmentType: 'backup_power',
      equipmentName: '备电系统',
      eventTitle: '备电容量测试建议',
      eventSource: 'run_trend',
      riskLevel: 'inspect',
      status: 'confirmed',
      detectorTrend: createDetectorTrend(0.86, 0.3),
      backupTrend: createBackupTrend(64, 17, 22),
      aiAnalysis: createBackupAnalysis(
        'AS02',
        64,
        17,
        22,
        0.69,
        '2026-10-14T14:20:00+08:00',
      ),
      aiFeedback: {
        rating: 'needs_revision',
        comment: '模型未考虑当日下午临时增加负载，续航下降结论需修正。',
        submittedAt: '2026-10-14T15:02:00+08:00',
      },
      confirmation: {
        decision: 'rejected',
        comment: '当日临时测试增加了备电负载，现场检查未发现电池异常。',
        confirmedAt: '2026-10-14T15:20:00+08:00',
        confirmedBy: '值班操作员',
      },
    },
  ]
}
