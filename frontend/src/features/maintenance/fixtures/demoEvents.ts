import type {
  AiAnalysis,
  AnalysisAction,
  AnalysisDataSnapshot,
  AnalysisFinding,
  AnalysisLimitation,
  AnalysisVersions,
  BackupTrend,
  DetectorPeriodTrend,
  DetectorTrend,
  MaintenanceEvent,
  RiskLevel,
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

function createDetectorPeriod(
  doseMultiplier: number,
  rainfallMultiplier: number,
  phase: number,
): DetectorPeriodTrend {
  const missingDoseIndex = phase === 0 ? 3 : phase === 1 ? 6 : 4
  const missingRainfallIndex = phase === 0 ? 7 : phase === 1 ? 1 : 8

  return {
    doseRate: CHART_TIMES.map((time, index) => ({
      time,
      value:
        index === missingDoseIndex
          ? null
          : Number(
              (
                DOSE_RATE_BASE[index] * doseMultiplier +
                Math.sin((index + phase) * 0.8) * 0.0015
              ).toFixed(3),
            ),
    })),
    rainfall: CHART_TIMES.map((time, index) => ({
      time,
      value:
        index === missingRainfallIndex
          ? null
          : Number(
              (
                RAINFALL_BASE[index] *
                rainfallMultiplier *
                (1 - phase * 0.08)
              ).toFixed(1),
            ),
    })),
  }
}

function createDetectorTrend(
  doseMultiplier = 1,
  rainfallMultiplier = 1,
): DetectorTrend {
  return {
    current24h: createDetectorPeriod(doseMultiplier, rainfallMultiplier, 0),
    lastMonth: createDetectorPeriod(
      doseMultiplier * 0.93,
      rainfallMultiplier * 0.74,
      1,
    ),
    lastYear: createDetectorPeriod(
      doseMultiplier * 0.97,
      rainfallMultiplier * 0.52,
      2,
    ),
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

const ANALYSIS_VERSIONS: AnalysisVersions = {
  ruleSet: 'maintenance-rules-2026.11.1',
  actionCatalog: 'maintenance-actions-2026.11.1',
  prompt: 'maintenance-narrator-v1',
  model: {
    name: 'Qwen3-14B',
    precision: 'W8A8',
    revision: 'demo',
  },
}

function createDataSnapshot(
  generatedAt: string,
  completeness: number,
): AnalysisDataSnapshot {
  const end = new Date(generatedAt)

  return {
    window: {
      start: new Date(end.getTime() - 24 * 60 * 60 * 1000).toISOString(),
      end: end.toISOString(),
    },
    dataAsOf: generatedAt,
    sampleCount: Math.round(8640 * completeness),
    completeness,
    missingRate: Number((1 - completeness).toFixed(3)),
  }
}

function createAction(
  actionCode: string,
  priority: AnalysisAction['priority'],
  text: string,
): AnalysisAction {
  return {
    actionCode,
    priority,
    text,
    requiresHumanConfirmation: true,
  }
}

function createDetectorAnalysis(
  siteId: string,
  eventTitle: string,
  dosePeak: number,
  rainfallPeak: number,
  riskLevel: RiskLevel,
  generatedAt: string,
  explanation?: string,
): AiAnalysis {
  const insufficient = riskLevel === 'insufficient_data'
  const findingSeverity = riskLevel === 'inspect' ? 'inspect' : 'watch'
  const baselineDose = 0.061
  const doseChange = Number(
    (((dosePeak - baselineDose) / baselineDose) * 100).toFixed(1),
  )
  const findings: AnalysisFinding[] = insufficient
    ? []
    : [
        {
          ruleId: 'DETECTOR-HIGH-DOSE',
          code: 'detector.dose_rate_high',
          severity: findingSeverity,
          metric: 'dose_rate',
          observed: dosePeak,
          baseline: baselineDose,
          changePercent: doseChange,
          window: '近 24 小时',
          message: `剂量率短时升至 ${dosePeak.toFixed(3)} μGy/h，高于近 24 小时基线。`,
          sourceRefs: [`ts:${siteId}:dose_rate:${generatedAt}`],
        },
        {
          ruleId: 'DETECTOR-RAIN-CORRELATION',
          code: 'detector.rainfall_correlated_rise',
          severity: 'watch',
          metric: 'rainfall',
          observed: rainfallPeak,
          baseline: null,
          changePercent: null,
          window: '近 24 小时',
          message: `剂量率升高时段与降雨峰值 ${rainfallPeak.toFixed(1)} mm 部分重合。`,
          sourceRefs: [`ts:${siteId}:rainfall:${generatedAt}`],
        },
        ...(riskLevel === 'inspect'
          ? [
              {
                ruleId: 'DETECTOR-CONSECUTIVE-ALARM',
                code: 'detector.consecutive_alarm',
                severity: 'inspect' as const,
                metric: 'dose_rate',
                observed: 3,
                baseline: 3,
                changePercent: null,
                window: '近 24 小时',
                message: '连续 3 个采样点超过报警限值。',
                sourceRefs: [`ts:${siteId}:dose_rate:alarm:${generatedAt}`],
              },
            ]
          : []),
      ]
  const actions = insufficient
    ? []
    : [
        createAction(
          'detector.verify_rainfall_context',
          'primary',
          '核对同期降雨、环境温湿度和周边作业记录',
        ),
        createAction(
          'detector.check_high_voltage',
          'secondary',
          '复核探测器高压、通信和自检记录',
        ),
      ]
  const limitations: AnalysisLimitation[] = [
    insufficient
      ? {
          code: 'detector.insufficient_samples',
          text: '近 24 小时有效剂量率样本不足，数据完整率低于 80%。',
          affects: '无法判断剂量率变化是否构成设备异常。',
        }
      : {
          code: 'detector.missing_high_voltage',
          text: '缺少探测器高压与能量谱原始记录。',
          affects: '无法进一步排除设备本体参数变化。',
        },
  ]

  return {
    explanation:
      explanation ??
      (insufficient
        ? `${siteId} 站点的${eventTitle}存在有效数据不足，当前无法判断设备是否异常，应先补齐数据采集后重新分析。`
        : `${siteId} 站点在${eventTitle}期间出现剂量率短时升高，峰值约 ${dosePeak.toFixed(3)} μGy/h；同期降雨量最高约 ${rainfallPeak.toFixed(1)} mm。现有依据更支持先行核查降雨和环境因素。`),
    explanationSource: insufficient ? 'template' : 'model',
    findings,
    actions,
    limitations,
    dataSnapshot: createDataSnapshot(
      generatedAt,
      insufficient ? 0.57 : 0.96,
    ),
    versions: ANALYSIS_VERSIONS,
    generatedAt,
  }
}

function createBackupAnalysis(
  siteId: string,
  currentBattery: number,
  estimatedRuntimeHours: number,
  historicalRuntimeHours: number,
  riskLevel: RiskLevel,
  generatedAt: string,
  explanation?: string,
): AiAnalysis {
  const runtimeDrop = Math.round(
    (1 - estimatedRuntimeHours / historicalRuntimeHours) * 100,
  )
  const insufficient = riskLevel === 'insufficient_data'
  const findingSeverity = riskLevel === 'inspect' ? 'inspect' : 'watch'
  const findings: AnalysisFinding[] = insufficient
    ? []
    : [
        {
          ruleId: 'BACKUP-RUNTIME-DROP',
          code: 'backup.runtime_drop',
          severity: findingSeverity,
          metric: 'runtime_hours',
          observed: estimatedRuntimeHours,
          baseline: historicalRuntimeHours,
          changePercent: -runtimeDrop,
          window: '近 24 小时',
          message: `参考续航由 ${historicalRuntimeHours} 小时降至 ${estimatedRuntimeHours} 小时，下降约 ${runtimeDrop}%。`,
          sourceRefs: [`ts:${siteId}:runtime:${generatedAt}`],
        },
        {
          ruleId: 'BACKUP-BATTERY-DECLINE',
          code: 'backup.battery_declining',
          severity: 'watch',
          metric: 'battery_percent',
          observed: currentBattery,
          baseline: null,
          changePercent: null,
          window: '近 24 小时',
          message: `当前电量 ${currentBattery}%，当日电量整体呈持续下降趋势。`,
          sourceRefs: [`ts:${siteId}:battery:${generatedAt}`],
        },
        {
          ruleId: 'BACKUP-HISTORY-BASELINE',
          code: 'backup.history_baseline',
          severity: 'watch',
          metric: 'maintenance_history',
          observed: null,
          baseline: historicalRuntimeHours,
          changePercent: null,
          window: '历史同期',
          message: '未发现近期更换电池或调整负载的记录。',
          sourceRefs: [`history:${siteId}:${generatedAt}`],
        },
      ]
  const actions = insufficient
    ? []
    : [
        createAction(
          'backup.check_charging_path',
          'primary',
          '检查充电模块、接线与实际负载',
        ),
        createAction(
          'backup.capacity_test',
          'secondary',
          '安排受控容量测试',
        ),
        createAction(
          'backup.compare_recovery',
          'secondary',
          '对比处理前后的续航与充电恢复情况',
        ),
      ]
  const limitations: AnalysisLimitation[] = [
    insufficient
      ? {
          code: 'backup.insufficient_runtime_samples',
          text: '缺少连续充放电曲线与有效续航样本，数据完整率低于 80%。',
          affects: '无法判断续航估算偏差是否来自电池容量变化。',
        }
      : {
          code: 'backup.missing_charge_curve',
          text: '缺少完整充放电曲线、环境温度和负载记录。',
          affects: '无法确认容量衰减程度。',
        },
  ]

  return {
    explanation:
      explanation ??
      (insufficient
        ? `${siteId} 备电事件缺少有效的续航与充放电数据，当前无法判断电池状态，应先补齐监测数据后重新分析。`
        : `${siteId} 备电记录显示，相近负载下参考续航由 ${historicalRuntimeHours} 小时降至 ${estimatedRuntimeHours} 小时，下降约 ${runtimeDrop}%；当前电量为 ${currentBattery}%。建议检查充电链路并结合完整充放电记录继续验证。`),
    explanationSource: insufficient ? 'template' : 'model',
    findings,
    actions,
    limitations,
    dataSnapshot: createDataSnapshot(
      generatedAt,
      insufficient ? 0.55 : 0.97,
    ),
    versions: ANALYSIS_VERSIONS,
    generatedAt,
  }
}

const additionalPendingEventSeeds = [
  {
    eventNumber: 'EV-1015-020',
    time: '07:20',
    siteId: 'AS01',
    siteName: '三废区',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '剂量率间歇升高事件',
    eventSource: 'monitoring_anomaly',
    riskLevel: 'watch',
    currentBattery: 76,
    estimatedRuntimeHours: 22,
    historicalRuntimeHours: 24,
  },
  {
    eventNumber: 'EV-1015-019',
    time: '06:55',
    siteId: 'BS01',
    siteName: '栗树村',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电充电耗时增加',
    eventSource: 'run_trend',
    riskLevel: 'watch',
    currentBattery: 71,
    estimatedRuntimeHours: 20,
    historicalRuntimeHours: 23,
  },
  {
    eventNumber: 'EV-1015-018',
    time: '06:30',
    siteId: 'AS04',
    siteName: '监测站 04',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '降雨期间剂量率波动',
    eventSource: 'monitoring_anomaly',
    riskLevel: 'watch',
    currentBattery: 79,
    estimatedRuntimeHours: 23,
    historicalRuntimeHours: 24,
  },
  {
    eventNumber: 'EV-1015-017',
    time: '06:05',
    siteId: 'BS03',
    siteName: '南港头村',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电电压短时波动',
    eventSource: 'device_alarm',
    riskLevel: 'watch',
    currentBattery: 67,
    estimatedRuntimeHours: 19,
    historicalRuntimeHours: 22,
  },
  {
    eventNumber: 'EV-1015-016',
    time: '05:40',
    siteId: 'AS13',
    siteName: '高温堆厂前区',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '剂量率阶跃变化事件',
    eventSource: 'run_trend',
    riskLevel: 'watch',
    currentBattery: 74,
    estimatedRuntimeHours: 21,
    historicalRuntimeHours: 23,
  },
  {
    eventNumber: 'EV-1015-015',
    time: '05:15',
    siteId: 'BS14',
    siteName: '镆铘岛',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电续航参考下降',
    eventSource: 'run_trend',
    riskLevel: 'inspect',
    currentBattery: 61,
    estimatedRuntimeHours: 17,
    historicalRuntimeHours: 23,
  },
  {
    eventNumber: 'EV-1015-014',
    time: '04:50',
    siteId: 'AS05',
    siteName: '监测站 05',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '探测器自检记录异常',
    eventSource: 'device_alarm',
    riskLevel: 'inspect',
    currentBattery: 70,
    estimatedRuntimeHours: 20,
    historicalRuntimeHours: 22,
  },
  {
    eventNumber: 'EV-1015-013',
    time: '04:25',
    siteId: 'BS12',
    siteName: '宁波街道办',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电充电恢复偏慢',
    eventSource: 'run_trend',
    riskLevel: 'watch',
    currentBattery: 72,
    estimatedRuntimeHours: 20,
    historicalRuntimeHours: 22,
  },
  {
    eventNumber: 'EV-1015-012',
    time: '04:00',
    siteId: 'AS09',
    siteName: '监测站 09',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '剂量率持续偏高事件',
    eventSource: 'monitoring_anomaly',
    riskLevel: 'inspect',
    currentBattery: 73,
    estimatedRuntimeHours: 21,
    historicalRuntimeHours: 23,
  },
  {
    eventNumber: 'EV-1015-011',
    time: '03:35',
    siteId: 'BS15',
    siteName: '荣成生活区',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电电压波动事件',
    eventSource: 'device_alarm',
    riskLevel: 'watch',
    currentBattery: 66,
    estimatedRuntimeHours: 19,
    historicalRuntimeHours: 21,
  },
  {
    eventNumber: 'EV-1015-010',
    time: '03:10',
    siteId: 'AS10',
    siteName: '监测站 10',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '通信恢复后数据跳变',
    eventSource: 'device_alarm',
    riskLevel: 'insufficient_data',
    currentBattery: 69,
    estimatedRuntimeHours: 20,
    historicalRuntimeHours: 21,
  },
  {
    eventNumber: 'EV-1015-009',
    time: '02:45',
    siteId: 'BQ01',
    siteName: '候工楼',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '备电负载变化事件',
    eventSource: 'run_trend',
    riskLevel: 'watch',
    currentBattery: 75,
    estimatedRuntimeHours: 22,
    historicalRuntimeHours: 24,
  },
  {
    eventNumber: 'EV-1015-008',
    time: '02:20',
    siteId: 'AS14',
    siteName: '综合实验楼',
    equipmentType: 'detector',
    equipmentName: '电离室探测器',
    eventTitle: '剂量率短时回落事件',
    eventSource: 'run_trend',
    riskLevel: 'watch',
    currentBattery: 77,
    estimatedRuntimeHours: 22,
    historicalRuntimeHours: 24,
  },
  {
    eventNumber: 'EV-1015-007',
    time: '01:55',
    siteId: 'BQ02',
    siteName: '辅助厂房',
    equipmentType: 'backup_power',
    equipmentName: '备电系统',
    eventTitle: '续航估算偏差事件',
    eventSource: 'run_trend',
    riskLevel: 'insufficient_data',
    currentBattery: 63,
    estimatedRuntimeHours: 18,
    historicalRuntimeHours: 20,
  },
] as const

function createAdditionalPendingEvents(): MaintenanceEvent[] {
  return additionalPendingEventSeeds.map((seed, index) => {
    const triggeredAt = `2026-10-15T${seed.time}:00+08:00`
    const isDetector = seed.equipmentType === 'detector'
    const dosePeak = Number((0.074 + (index % 5) * 0.003).toFixed(3))
    const rainfallPeak = Number((1.4 + (index % 4) * 0.4).toFixed(1))

    return {
      id: seed.eventNumber.toLowerCase(),
      eventNumber: seed.eventNumber,
      triggeredAt,
      siteId: seed.siteId,
      siteName: seed.siteName,
      equipmentType: seed.equipmentType,
      equipmentName: seed.equipmentName,
      eventTitle: seed.eventTitle,
      eventSource: seed.eventSource,
      riskLevel: seed.riskLevel,
      status: 'pending',
      detectorTrend: createDetectorTrend(
        0.9 + (index % 5) * 0.04,
        0.5 + (index % 3) * 0.25,
      ),
      backupTrend: createBackupTrend(
        seed.currentBattery,
        seed.estimatedRuntimeHours,
        seed.historicalRuntimeHours,
      ),
      aiAnalysis: isDetector
        ? createDetectorAnalysis(
            seed.siteId,
            seed.eventTitle,
            dosePeak,
            rainfallPeak,
            seed.riskLevel,
            triggeredAt,
          )
        : createBackupAnalysis(
            seed.siteId,
            seed.currentBattery,
            seed.estimatedRuntimeHours,
            seed.historicalRuntimeHours,
            seed.riskLevel,
            triggeredAt,
          ),
    }
  })
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
        'inspect',
        '2026-10-15T10:20:00+08:00',
      ),
    },
    ...createAdditionalPendingEvents(),
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
        'watch',
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
        'watch',
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
        'watch',
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
        'insufficient_data',
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
        'watch',
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
        'watch',
        '2026-10-14T16:50:00+08:00',
      ),
      review: {
        outcome: 'accurate',
        comment: '已核对现场降雨和周边作业记录，核查结果与 AI 降雨影响判断一致。',
        reviewedAt: '2026-10-14T17:30:00+08:00',
        reviewedBy: '值班操作员',
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
        'inspect',
        '2026-10-14T14:20:00+08:00',
      ),
      review: {
        outcome: 'needs_revision',
        comment: '当日临时测试增加了备电负载，模型未考虑该因素，续航下降结论需修正。',
        reviewedAt: '2026-10-14T15:20:00+08:00',
        reviewedBy: '值班操作员',
      },
    },
  ]
}
