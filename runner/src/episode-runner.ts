import type { EpisodeRecord, EvalRow, Mode, Profile, TurnOutcome, TurnRecord, TurnStatus } from './domain.js'
import { RECORD_SCHEMA, userTurnsOf } from './domain.js'
import type { AppliedInjection } from './materialize.js'
import { ScriptedDriver } from './drivers/scripted.js'
import type { AgentAdapter, AgentSession, Intervention, InterventionPlan, PrefillTurn, UserDriver } from './ports.js'
import { observe } from './probes/index.js'
import { emptyTokenUsage } from './vendor/types.js'

export interface EpisodeSpec {
  runId: string
  row: EvalRow
  mode: Mode
  trial: number
  intervention: Intervention
  plan: Extract<InterventionPlan, { ok: true }>
  runDir: string
  workspace: string
  /** isolated 模式的 skill 物化结果（dirs 由适配器经 SessionContext 接线） */
  skills?: { dirs: readonly string[]; sha256: string | null; applied: AppliedInjection[] }
  /** 逐会话环境变量覆盖（bundled-copy：DSH_BUNDLED_SKILL_DIR 指向本会话副本） */
  env?: Readonly<Record<string, string>>
  /** 锚点会话标记 */
  anchor?: boolean
}

export interface EpisodeDeps {
  adapter: AgentAdapter
  profile: Profile
  /** 默认按用例脚本驱动；以后的模拟用户从这里注入 */
  driverFor?: (row: EvalRow, liveTurns: readonly string[]) => UserDriver
}

/**
 * 驱动一段完整会话：
 *   准备  开启会话（prefill 时把前面各轮作为历史预置）
 *   循环  driver 给出下一条消息 → 干预改写 → 发送 → 探针观测本轮；任何一轮不是 ok 即停止
 *   收尾  结束会话，组装 EpisodeRecord
 * 本函数不抛异常：适配器或驱动的异常都记为 error 轮。
 */
export async function runEpisode(spec: EpisodeSpec, deps: EpisodeDeps): Promise<EpisodeRecord> {
  const startedAt = Date.now()
  const { row, profile } = { row: spec.row, profile: deps.profile }
  const history = row.history ?? 'live'
  const refs = row.assistant_refs ?? []
  const allMessages = userTurnsOf(row)

  // prefill：前 n-1 轮直接记为预置历史，只实际发送最后一轮
  const prefilled: TurnRecord[] = []
  let liveMessages: readonly string[] = allMessages
  if (history === 'prefill') {
    const pairs: PrefillTurn[] = allMessages.slice(0, -1).map((user, i) => ({ user, assistant: refs[i] ?? '' }))
    pairs.forEach((p, i) => prefilled.push(prefillRecord(i + 1, p)))
    liveMessages = allMessages.slice(-1)
  }
  const driver = (deps.driverFor ?? ((_r, messages) => new ScriptedDriver(messages)))(row, liveMessages)

  const turns: TurnRecord[] = [...prefilled]
  let failedTurn: number | null = null
  let session: AgentSession | null = null
  let artifacts: { sessionLog: string | null; sessionId: string | null } = { sessionLog: null, sessionId: null }
  let episodeError: string | undefined

  try {
    session = await deps.adapter.open(
      { runDir: spec.runDir, workspace: spec.workspace, timeoutMs: profile.dsh.timeout_ms, skillsDirs: spec.skills?.dirs, env: spec.env },
      history === 'prefill' ? prefilled.map((t, i) => ({ user: t.input, assistant: refs[i] ?? '' })) : undefined,
    )
    let liveIndex = 0
    for (;;) {
      const message = driver.next(turns)
      if (message === null) break
      const input = spec.intervention.transform(message, liveIndex, spec.plan)
      const index = turns.length + 1
      let outcome: TurnOutcome
      try {
        outcome = await session.send(input)
      } catch (err) {
        outcome = { status: 'error', error: `适配器异常：${(err as Error).message}`, durationMs: 0, trace: emptyTraceOutcome() }
      }
      turns.push(liveRecord(index, input, outcome, profile))
      liveIndex++
      if (outcome.status !== 'ok') {
        failedTurn = index
        break
      }
    }
  } catch (err) {
    episodeError = `会话异常：${(err as Error).message}`
  } finally {
    if (session) {
      try {
        artifacts = await session.close()
      } catch {
        // 结束会话失败不影响已采集的结果
      }
    }
  }

  const live = turns.filter((t) => t.source === 'live')
  const failed = failedTurn !== null ? turns[failedTurn - 1] : undefined
  const status: TurnStatus = episodeError || live.length === 0 ? 'error' : (failed?.status ?? 'ok')
  const record: EpisodeRecord = {
    schema: RECORD_SCHEMA,
    run_id: spec.runId,
    episode_id: episodeId(row.uid, spec.mode, spec.trial),
    uid: row.uid,
    case_id: row.case_id,
    variant_index: row.variant_index,
    dim: row.dim,
    topic: row.topic ?? null,
    granularity: row.granularity ?? null,
    mode: spec.mode,
    trial: spec.trial,
    history,
    evalset_version: row.evalset_version,
    skills_sha256: spec.skills ? spec.skills.sha256 : null,
    injections_applied: spec.skills ? spec.skills.applied.map((a) => ({ point: a.point, option: a.option, rev: a.rev, found: a.found })) : undefined,
    anchor: spec.anchor,
    forced_skills: spec.plan.forcedSkills,
    status,
    failed_turn: failedTurn,
    turns,
    final_output: turns.at(-1)?.source === 'live' ? (turns.at(-1)?.output ?? '') : '',
    loaded_skills: uniqueSkills(live),
    usage: sumUsage(live),
    duration_ms: Date.now() - startedAt,
    artifacts: { session_log: artifacts.sessionLog, session_id: artifacts.sessionId, run_dir: spec.runDir },
    case: row,
    finished_at: new Date().toISOString(),
  }
  const error = episodeError ?? failed?.error ?? (live.length === 0 ? '没有发送任何消息' : undefined)
  if (error) record.error = error
  const errorCode = failed && 'errorCode' in failed && failed.errorCode ? String(failed.errorCode) : undefined
  if (errorCode) record.error_code = errorCode
  return record
}

export function episodeId(uid: string, mode: Mode, trial: number): string {
  return `${uid}|${mode}|${String(trial)}`
}

function liveRecord(index: number, input: string, outcome: TurnOutcome, profile: Profile): TurnRecord {
  const record: TurnRecord = {
    index,
    source: 'live',
    input,
    output: outcome.trace.finalText,
    status: outcome.status,
    turn_end: outcome.trace.turnEnd ?? null,
    exit_code: outcome.exitCode ?? null,
    duration_ms: outcome.durationMs,
    observations: observe(outcome.trace, profile),
  }
  if (outcome.error) record.error = outcome.error
  return record
}

function prefillRecord(index: number, pair: PrefillTurn): TurnRecord {
  return { index, source: 'prefill', input: pair.user, output: pair.assistant, status: 'ok', turn_end: null, exit_code: null, duration_ms: 0, observations: null }
}

function uniqueSkills(turns: readonly TurnRecord[]): string[] {
  const names: string[] = []
  for (const t of turns) for (const s of t.observations?.skills ?? []) if (!names.includes(s.name)) names.push(s.name)
  return names
}

function sumUsage(turns: readonly TurnRecord[]): { steps: number; tokens: ReturnType<typeof emptyTokenUsage> } {
  const tokens = emptyTokenUsage()
  let steps = 0
  for (const t of turns) {
    const u = t.observations?.usage
    if (!u) continue
    steps += u.steps
    for (const k of Object.keys(tokens) as (keyof typeof tokens)[]) tokens[k] += u.tokens[k]
  }
  return { steps, tokens }
}

function emptyTraceOutcome() {
  return { finalText: '', toolCalls: [], toolResults: [], toolErrors: [], steps: 0, tokens: emptyTokenUsage(), models: [] as string[] }
}
