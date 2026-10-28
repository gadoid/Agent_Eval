import { readFile } from 'node:fs/promises'
import type { AdapterCapabilities, EvalRow, Mode, Profile, SkippedRow, SkillsMode } from './domain.js'
import { userTurnsOf } from './domain.js'
import { INTERVENTIONS } from './interventions/index.js'
import type { InterventionPlan } from './ports.js'
import { ConfigError, sha256 } from './profile.js'

/** 读取 evalset.jsonl，校验执行需要的字段；所有问题带行号一次报出 */
export async function loadEvalset(path: string): Promise<{ rows: EvalRow[]; sha256: string; header: SnapshotHeader | null }> {
  let text: string
  try {
    text = await readFile(path, 'utf8')
  } catch (err) {
    throw new ConfigError(`无法读取评测集 ${path}：${(err as Error).message}`)
  }
  const { rows, header } = parseEvalsetWithHeader(text, path)
  return { rows, sha256: sha256(text), header }
}

/** dist 首行快照头（doc/15 §3.9）：{snapshot: {version, method_sha, include}} */
export interface SnapshotHeader {
  version: string
  method_sha: string
  include?: string
}

/** 快照测法（同名 .snapshot.json sidecar；doc/15 §3.10 P2） */
export interface SnapshotMethod {
  modes: Record<string, string>
  run_rewrites: string[]
  anchors: string[]
}

export async function loadMethod(distPath: string): Promise<{ methodSha: string; method: SnapshotMethod } | null> {
  const sidecar = distPath.replace(/\.jsonl?$/, '') + '.snapshot.json'
  let raw: string
  try {
    raw = await readFile(sidecar, 'utf8')
  } catch {
    return null                                    // 旧 dist 无测法（回退 CLI 参数执行）
  }
  const parsed = JSON.parse(raw) as { method_sha?: string; method?: Partial<SnapshotMethod> }
  const m = parsed.method ?? {}
  return {
    methodSha: parsed.method_sha ?? '',
    method: {
      modes: (m.modes ?? {}) as Record<string, string>,
      run_rewrites: m.run_rewrites ?? [],
      anchors: m.anchors ?? [],
    },
  }
}

export function parseEvalset(text: string, source = '<evalset>'): EvalRow[] {
  return parseEvalsetWithHeader(text, source).rows
}

function parseEvalsetWithHeader(text: string, source = '<evalset>'): { rows: EvalRow[]; header: SnapshotHeader | null } {
  const rows: EvalRow[] = []
  const errors: string[] = []
  const seen = new Map<string, number>()
  let header: SnapshotHeader | null = null
  text.split(/\r?\n/).forEach((line, i) => {
    const no = i + 1
    if (line.trim() === '') return
    let row: Record<string, unknown>
    try {
      row = JSON.parse(line) as Record<string, unknown>
    } catch {
      errors.push(`第 ${no} 行：不是合法的 JSON`)
      return
    }
    if (row.snapshot && !row.uid) {                 // 快照头行（P2）：记录后跳过
      const s = row.snapshot as Record<string, unknown>
      header = { version: String(s.version ?? ''), method_sha: String(s.method_sha ?? '') }
      return
    }
    const problems: string[] = []
    if (typeof row.uid !== 'string' || row.uid === '') problems.push('uid')
    if (typeof row.case_id !== 'string' || row.case_id === '') problems.push('case_id')
    if (!Number.isInteger(row.variant_index)) problems.push('variant_index')
    if (typeof row.evalset_version !== 'string' || row.evalset_version === '') problems.push('evalset_version')
    if (typeof row.dim !== 'string' || row.dim === '') problems.push('dim')
    const turns = row.turns
    const badTurn = (t: unknown) =>
      typeof t === 'string' ? t === '' : (!!t && typeof t === 'object' && typeof (t as { content?: unknown }).content === 'string' && (t as { content: string }).content !== '' && (t as { role?: unknown }).role === 'user') ? false : true
    if (!Array.isArray(turns) || turns.length === 0 || turns.some(badTurn)) problems.push('turns')
    const refs = row.assistant_refs
    if (refs !== undefined && (!Array.isArray(refs) || (Array.isArray(turns) && refs.length !== turns.length) || refs.some((r) => r !== null && typeof r !== 'string'))) {
      problems.push('assistant_refs（应与 turns 等长，元素为文本或 null）')
    }
    if (row.history !== undefined && row.history !== 'live' && row.history !== 'prefill') problems.push('history')
    if (problems.length > 0) {
      errors.push(`第 ${no} 行：字段缺失或格式错误：${problems.join('、')}`)
      return
    }
    const uid = row.uid as string
    if (seen.has(uid)) {
      errors.push(`第 ${no} 行：uid ${uid} 与第 ${String(seen.get(uid))} 行重复`)
      return
    }
    seen.set(uid, no)
    rows.push(row as EvalRow)
  })
  if (errors.length > 0) throw new ConfigError(`${source}:\n  ${errors.join('\n  ')}`)
  if (rows.length === 0) throw new ConfigError(`${source}: 评测集为空`)
  return { rows, header }
}

export interface PlannedEpisode {
  seq: number
  row: EvalRow
  mode: Mode
  trial: number
  plan: Extract<InterventionPlan, { ok: true }>
  /** 该会话的用户消息（turns 归一） */
  messages: string[]
  /** 锚点会话（绊线用，trials 已提升到 ≥2） */
  anchor?: boolean
}

export interface PlanOptions {
  modes: readonly Mode[]
  trials: number
  dims?: readonly string[]
  only?: readonly string[]
  topic?: string
  /** 全量 run（无 --dims/--only/--topic 过滤）时并入锚点；锚点 trials 取 max(trials, 2) */
  anchors?: readonly string[]
  /** isolated 前置：ec 维度只在 isolated 运行（阻断项机制，01 §2） */
  skillsMode?: SkillsMode
  /** 规划期注入预演结果：uid → 未命中原因 */
  rehearsal?: ReadonlyMap<string, string>
  /** 快照测法（doc/15 §3.10）：给定时按维度模式执行、按 run_rewrites 过滤变体；
   *  与 modes 同时给时 modes 优先（诊断执行）。 */
  method?: SnapshotMethod
}

/**
 * 用例 × 干预 × trials 展开为 Episode。执行器做不到的用例在这里跳过并写明原因，
 * 不在运行时悄悄降级。
 */
export function planEpisodes(
  rows: readonly EvalRow[],
  profile: Profile,
  capabilities: AdapterCapabilities,
  agentName: string,
  options: PlanOptions,
): { episodes: PlannedEpisode[]; skipped: SkippedRow[]; selected: number } {
  const dims = new Set(options.dims ?? [])
  const only = new Set(options.only ?? [])
  const topic = options.topic ?? ''
  const skillsMode = options.skillsMode ?? 'default-roots'
  const anchors = new Set(options.anchors ?? [])
  const episodes: PlannedEpisode[] = []
  const skipped: SkippedRow[] = []
  let selected = 0
  const addEpisode = (row: EvalRow, mode: Mode, trial: number, plan: Extract<InterventionPlan, { ok: true }>, anchor: boolean) => {
    episodes.push({ seq: episodes.length + 1, row, mode, trial, plan, messages: userTurnsOf(row), anchor: anchor || undefined })
  }
  const method = options.method
  for (const row of rows) {
    if (dims.size > 0 && !dims.has(row.dim)) continue
    if (only.size > 0 && !only.has(row.uid) && !only.has(row.case_id)) continue
    if (topic !== '' && row.topic !== topic) continue
    if (method && (row.variant_index ?? 0) > 0) {
      // 测法管执行范围（doc/15 §3.10）：种子总跑；run_rewrites 之外的改写不跑（诱导默认不跑）
      const rw = typeof row.rewrite === 'string' ? row.rewrite : undefined
      if (rw === undefined || !method.run_rewrites.includes(rw)) {
        skipped.push({ uid: row.uid, mode: null, reason: `测法未纳入改写「${String(rw)}」不执行（run_rewrites）` })
        continue
      }
    }
    const rowModes: readonly Mode[] = method
      ? [((method.modes[row.dim] ?? method.modes.default ?? 'natural') as Mode)]
      : options.modes
    selected++
    const history = row.history ?? 'live'
    const messages = userTurnsOf(row)
    if (messages.length > 1 && history === 'live' && !capabilities.multiTurn) {
      skipped.push({ uid: row.uid, mode: null, reason: `执行器 ${agentName} 不支持多轮对话` })
      continue
    }
    if (history === 'prefill' && !capabilities.prefillHistory) {
      skipped.push({ uid: row.uid, mode: null, reason: `执行器 ${agentName} 不支持预置历史（history: prefill）` })
      continue
    }
    if (row.dim === '错误知识纠正' && skillsMode === 'default-roots') {
      skipped.push({ uid: row.uid, mode: null, reason: 'isolation-not-verified：default-roots 无隔离机制，ec 不运行；bundled-copy/isolated 模式可运行（P0-3 已验证 DSH_BUNDLED_SKILL_DIR 路线）' })
      continue
    }
    const rehearsed = options.rehearsal?.get(row.uid)
    if (rehearsed !== undefined) {
      skipped.push({ uid: row.uid, mode: null, reason: rehearsed })
      continue
    }
    const forbidden = forbiddenSkillsOf(row)
    for (const mode of rowModes) {
      const plan = INTERVENTIONS[mode].plan(row, profile)
      if (!plan.ok) {
        skipped.push({ uid: row.uid, mode, reason: plan.reason })
        continue
      }
      if (mode === 'forced' && plan.forcedSkills.some((s) => forbidden.includes(s))) {
        skipped.push({ uid: row.uid, mode, reason: 'forced 目标被用例路径「禁止」加载，跳过 forced 保留 natural' })
        continue
      }
      const isAnchor = anchors.has(row.uid)
      const trials = isAnchor ? Math.max(options.trials, 2) : options.trials
      for (let trial = 1; trial <= trials; trial++) {
        addEpisode(row, mode, trial, plan, isAnchor)
      }
    }
  }
  // 锚点补充：筛选条件之外的锚点 uid 若在评测集中且未被过滤维度的选择逻辑挡住，也纳入
  // （全量 run 下上面已覆盖；这里只补全 --dims/--only 之外的锚点不重复添加）
  return { episodes, skipped, selected }
}

/** 用例路径中被「禁止」加载的 skill（forced 目标冲突检查） */
export function forbiddenSkillsOf(row: EvalRow): string[] {
  const path = row.path
  if (!Array.isArray(path)) return []
  const out: string[] = []
  for (const p of path as { type?: string; action?: { kind?: string; skill?: string } }[]) {
    if (p?.type === 'forbid' && p.action?.kind === 'load_skill' && typeof p.action.skill === 'string') out.push(p.action.skill)
  }
  return out
}
