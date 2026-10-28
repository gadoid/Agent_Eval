import { randomBytes } from 'node:crypto'
import { mkdir, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import type { EpisodeRecord, Profile, RunManifest } from './domain.js'
import { RECORD_SCHEMA } from './domain.js'
import { episodeId, runEpisode } from './episode-runner.js'
import type { EpisodeSpec } from './episode-runner.js'
import { INTERVENTIONS } from './interventions/index.js'
import { materializeSkills, rehearseInjections } from './materialize.js'
import { loadEvalset, planEpisodes } from './planner.js'
import type { PlanOptions, PlannedEpisode } from './planner.js'
import type { AgentAdapter } from './ports.js'
import { mirrorSessions } from './mirror.js'
import { ConfigError } from './profile.js'
import { computeSnapshot, inventoryStale, loadInventory, subjectSection } from './snapshot.js'
import { checkResumable, countStatuses, ResultStore } from './store.js'

export interface EvaluationOptions extends PlanOptions {
  /** 诊断执行（doc/15 §3.10）：显式给 --modes/--anchors 时的 run 标记，不进正式分 */
  diagnostic?: boolean
  /** 执行所用测法的 method_sha（run.json 留档；scoring 落 runs.method_sha） */
  methodSha?: string
  evalsetPath: string
  profilePath: string
  profile: Profile
  profileSha256: string
  outDir: string
  adapter: AgentAdapter
  concurrency: number
  resume?: boolean
  keepWorkspace?: boolean
  /** 执行记录标题（--title）：写进 run.json，入库为 runs.title；纯展示层，不参与任何键 */
  title?: string
  /** 成本估算基数：上一同 Profile run 的每轮平均 token / 小时（cli 从历史 run.json 取） */
  lastAverages?: { tokensPerTurn: number; hoursPerTurn: number }
  onEpisodeDone?: (record: EpisodeRecord, done: number, total: number) => void
}

/** 一次运行：快照与预演 → 规划 → 并发执行各段会话（isolated 时先物化）→ 落盘 → 运行清单 */
export async function runEvaluation(options: EvaluationOptions): Promise<RunManifest> {
  // CLI 通道凭据/home 定向：默认 home(~/.dsh) 的 profile 可能被改动，评测固定指向有效 home
  if (options.profile.dsh.home) process.env.DSH_HOME = options.profile.dsh.home
  if (options.profile.dsh.env) Object.assign(process.env, options.profile.dsh.env)
  const outDir = resolve(options.outDir)
  const { rows, sha256: evalsetSha } = await loadEvalset(options.evalsetPath)
  const { adapter, profile } = options
  const warnings: string[] = []

  const inventory = profile.skills_mode === 'isolated' && profile.skills_inventory
    ? await loadInventory(profile.skills_inventory)
    : null
  if (profile.skills_mode === 'isolated' && profile.skills_inventory && !inventory) {
    warnings.push('skills_inventory 文件不可读（P0-2 盘点缺失）')
  }
  const stale = await inventoryStale(profile)
  if (stale) warnings.push(`inventory 过期：${stale}`)

  const snapshot = await computeSnapshot(profile, resolve(options.evalsetPath), evalsetSha, {
    name: adapter.name,
    version: adapter.version ?? null,
  })

  // 注入预演（isolated 才有注入可跑；default-roots 下 ec 已在规划期整体跳过）
  let rehearsal: ReadonlyMap<string, string> | undefined
  if (profile.skills_mode !== 'default-roots' && profile.skills_roots && profile.skills_roots.length > 0) {
    const result = await rehearseInjections(rows, profile.skills_roots)
    rehearsal = new Map(
      [...result].filter(([, v]) => !v.ok).map(([uid, v]) => [uid, `${v.reason ?? 'injection-key-missed'}（预演）`]),
    )
  }

  const { episodes, skipped } = planEpisodes(rows, profile, adapter.capabilities, adapter.name, {
    ...options,
    skillsMode: profile.skills_mode,
    rehearsal,
  })

  await mkdir(outDir, { recursive: true })
  const store = new ResultStore(outDir)
  const previous = await store.readRecords()
  if (previous.length > 0 && !options.resume) {
    throw new ConfigError(`${store.resultsPath} 已存在，续跑请加 --resume，否则换一个输出目录`)
  }
  const previousManifest = previous.length > 0 ? await store.readManifest() : null
  if (previousManifest && previousManifest.profile.content.id !== profile.id) {
    throw new ConfigError(`已有结果来自 Profile ${previousManifest.profile.content.id}，与本次 ${profile.id} 不一致，不能续跑`)
  }
  checkResumable(previous, profile, rows, {
    previous: (previousManifest?.snapshot as Record<string, unknown> | undefined) ?? null,
    current: snapshot as unknown as Record<string, unknown>,
  })

  const done = new Set(previous.map((r) => episodeId(r.uid, r.mode, r.trial)))
  const pending = episodes.filter((e) => !done.has(episodeId(e.row.uid, e.mode, e.trial)))
  const runId = previousManifest?.run_id ?? previous[0]?.run_id ?? newRunId()

  // 成本估算：计划总轮数 × 上一 run 每轮平均（无历史则只报轮数）
  const totalTurns = episodes.reduce((n, e) => n + e.messages.length, 0)
  const est = options.lastAverages
    ? {
        total_turns: totalTurns,
        est_tokens: Math.round(totalTurns * options.lastAverages.tokensPerTurn),
        est_hours: round(totalTurns * options.lastAverages.hoursPerTurn),
      }
    : { total_turns: totalTurns, est_tokens: null, est_hours: null }

  const manifest: RunManifest = {
    schema: RECORD_SCHEMA,
    run_id: runId,
    title: options.title?.trim() || previousManifest?.title || null,
    started_at: previousManifest?.started_at ?? new Date().toISOString(),
    finished_at: null,
    evalset: {
      path: resolve(options.evalsetPath),
      sha256: evalsetSha,
      versions: [...new Set(rows.map((r) => r.evalset_version))].sort(),
      rows: rows.length,
    },
    profile: { path: resolve(options.profilePath), sha256: options.profileSha256, content: profile },
    agent: { name: adapter.name, version: adapter.version ?? null, capabilities: adapter.capabilities },
    modes: [...options.modes],
    diagnostic: options.diagnostic ?? false,
    method: options.methodSha ? { method_sha: options.methodSha } : null,
    trials: options.trials,
    concurrency: options.concurrency,
    filters: { dims: [...(options.dims ?? [])], only: [...(options.only ?? [])] },
    episodes: { planned: episodes.length, resumed: episodes.length - pending.length, executed: 0 },
    status_counts: countStatuses(previous),
    error_rate: 0,
    skipped,
    snapshot: snapshot as unknown as Record<string, unknown>,
    subject: subjectSection(profile, snapshot),
    anchor_uids: [...new Set(episodes.filter((e) => e.anchor).map((e) => e.row.uid))],
    estimate: est,
    warnings,
  }
  await store.writeManifest(manifest)

  const startedWall = Date.now()
  let next = 0
  let finished = 0
  const worker = async (): Promise<void> => {
    while (next < pending.length) {
      const planned = pending[next++] as PlannedEpisode
      const record = await executeOne(planned, runId, outDir, options, inventory)
      await store.append(record)
      manifest.episodes.executed++
      manifest.status_counts[record.status]++
      finished++
      options.onEpisodeDone?.(record, finished, pending.length)
    }
  }
  const workers = Math.max(1, Math.min(options.concurrency, pending.length || 1))
  await Promise.all(Array.from({ length: workers }, worker))

  const all = await store.finalize()
  manifest.status_counts = countStatuses(all)
  const failed = manifest.status_counts.error + manifest.status_counts.timeout
  manifest.error_rate = all.length === 0 ? 0 : Math.round((failed / all.length) * 10_000) / 10_000
  const totalTokens = all.reduce((n, r) => n + r.usage.tokens.total, 0)
  manifest.actuals = { total_tokens: totalTokens, hours: round((Date.now() - startedWall) / 3_600_000) }
  // 快照收尾：models_observed 汇总 + 非阻断告警
  const models = [...new Set(all.flatMap((r) => r.turns.map((t) => t.observations?.model).filter((m): m is string => !!m)))]
  if (models.length > 0) {
    ;(manifest.snapshot as Record<string, unknown>).models_observed = models
    if (models.length > 1) warnings.push(`model_drift：本 run 观测到多个模型标识（${models.join('、')}），结论需按模型分组看`)
  }
  if (profile.skills_mode === 'isolated' || profile.skills_mode === 'bundled-copy') {
    const baseline = snapshot.skills_base
    const mismatch = all.filter(
      (r) => r.skills_sha256 && r.skills_sha256 !== baseline && (r.injections_applied ?? []).length === 0,
    )
    if (mismatch.length > 0) warnings.push(`skills_mismatch：${String(mismatch.length)} 段无注入会话的副本 sha256 与基底不一致`)
  }
  manifest.warnings = warnings
  manifest.finished_at = new Date().toISOString()
  await store.writeManifest(manifest)
  return manifest
}

async function executeOne(
  planned: PlannedEpisode,
  runId: string,
  outDir: string,
  options: EvaluationOptions,
  inventory: Awaited<ReturnType<typeof loadInventory>>,
): Promise<EpisodeRecord> {
  const runDir = join(
    outDir,
    '.work',
    `${String(planned.seq).padStart(4, '0')}-${slug(planned.row.uid)}-${planned.mode}-t${String(planned.trial)}`,
  )
  const workspace = join(runDir, 'workspace')
  await rm(runDir, { recursive: true, force: true })
  await mkdir(workspace, { recursive: true })

  const materializing = options.profile.skills_mode !== 'default-roots' && options.profile.skills_roots  // isolated | bundled-copy
  let skills: EpisodeSpec['skills']
  if (materializing) {
    const mat = await materializeSkills(runDir, options.profile.skills_roots ?? [], planned.row.injections ?? [], inventory)
    if (mat.error) {
      // 物化失败（文件缺失 / key 未命中）：不降级为无注入运行，整段会话记 error
      return failedRecord(planned, runId, runDir, mat.error.code === 'injection-key-missed'
        ? 'injection-key-missed'
        : 'injection-file-missing', `skill 物化失败：${mat.error.detail}`)
    }
    skills = { dirs: mat.dirs, sha256: mat.sha256, applied: mat.applied }
  }

  const record = await runEpisode(
    {
      runId,
      row: planned.row,
      mode: planned.mode,
      trial: planned.trial,
      intervention: INTERVENTIONS[planned.mode],
      plan: planned.plan,
      runDir,
      workspace,
      skills,
      env: options.profile.skills_mode === 'bundled-copy' && skills
        ? { DSH_BUNDLED_SKILL_DIR: skills.dirs[0] as string }
        : undefined,
      anchor: planned.anchor,
    },
    { adapter: options.adapter, profile: options.profile },
  )
  // 双端留存：把本段会话的 session 日志镜像到客户端会话目录（可选，Profile 配置）
  const mirrorRoot = options.profile.dsh.mirror_sessions
  if (mirrorRoot) {
    const target = resolve(mirrorRoot)
    const copied = await mirrorSessions(join(runDir, 'session'), target)
    record.artifacts.session_mirror = copied > 0 ? target : null
  }
  // 工作区在整段会话结束后才删除：续接会话要求工作目录不变
  if (!options.keepWorkspace) await rm(workspace, { recursive: true, force: true })
  return record
}

function failedRecord(planned: PlannedEpisode, runId: string, runDir: string, errorCode: string, error: string): EpisodeRecord {
  return {
    schema: RECORD_SCHEMA,
    run_id: runId,
    episode_id: episodeId(planned.row.uid, planned.mode, planned.trial),
    uid: planned.row.uid,
    case_id: planned.row.case_id,
    variant_index: planned.row.variant_index,
    dim: planned.row.dim,
    topic: planned.row.topic ?? null,
    granularity: planned.row.granularity ?? null,
    mode: planned.mode,
    trial: planned.trial,
    history: planned.row.history ?? 'live',
    evalset_version: planned.row.evalset_version,
    skills_sha256: null,
    anchor: planned.anchor,
    forced_skills: planned.plan.forcedSkills,
    status: 'error',
    error,
    error_code: errorCode,
    failed_turn: null,
    turns: [],
    final_output: '',
    loaded_skills: [],
    usage: { steps: 0, tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, total: 0 } },
    duration_ms: 0,
    artifacts: { session_log: null, session_id: null, run_dir: runDir },
    case: planned.row,
    finished_at: new Date().toISOString(),
  }
}

function slug(s: string): string {
  return s.replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'case'
}

function newRunId(): string {
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z')
  return `${ts}-${randomBytes(2).toString('hex')}`
}

function round(n: number): number {
  return Math.round(n * 1000) / 1000
}
