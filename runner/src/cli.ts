#!/usr/bin/env node
import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { DSH_CLI_CAPABILITIES, DshAdapter } from './adapters/dsh-cli.js'
import { WebUiAdapter, webUiConfigFromProfile } from './adapters/web-ui.js'
import type { Mode } from './domain.js'
import { MODES } from './domain.js'
import { mkdtemp, readFile, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { buildEvalsetFromMd } from './md-compile.js'
import { findSessionFiles, inspectSession, renderInspect } from './inspect.js'
import { runEvaluation } from './orchestrator.js'
import { loadEvalset, planEpisodes } from './planner.js'
import type { AgentAdapter } from './ports.js'
import { assertModelConfigComplete, ConfigError, loadProfile } from './profile.js'

/** 退出码：0 完成；1 参数、配置、评测集错误或找不到 dsh；2 完成但 error/timeout 占比超过阈值 */
export const EXIT_OK = 0
export const EXIT_CONFIG = 1
export const EXIT_UNHEALTHY = 2

const USAGE = `用法：
  skill-eval run --evalset <evalset.jsonl> --profile <profile.yaml> --out <目录> [选项]
  skill-eval run --cases <md 目录或文件>... --profile <profile.yaml> --out <目录> [选项]
                                              直接用 markdown 启动：自动编译为一次性评测集
                                              （adhoc-<时间戳> 版本，落在 <out>/dist；不参与
                                              版本对比与 --resume，正式 run 请用 --evalset）
  skill-eval run ... --dry-run                 只列出任务与跳过项，不运行 dsh
  skill-eval inspect <session 日志或目录> [--skill-tool skill]

run 选项：
  --cases <路径>...           markdown 目录或文件（可重复；与 --evalset 互斥）
  --modes natural,forced    干预方式，默认 natural；forced 在第一轮前加 /skill名 强制加载
  --trials N                每个用例每种干预的独立采样次数，默认 3
  --concurrency N           并发数，默认 1
  --dims 知识召回,...        只跑这些维度
  --only kr-0201,kr-0202#1  只跑这些用例（case_id 或 uid）
  --topic 进单资料审核       只跑该业务主题
  --anchors on|off          锚点并入开关；默认全量 run（无 --dims/--only/--topic）并入
  --resume                  输出目录已有结果时续跑，跳过已完成的会话
  --keep-workspace          保留每段会话的工作区（默认删除，session 日志总是保留）
  --max-error-rate R        error 与 timeout 占比超过 R 时退出码为 2，默认 0.2
  --adapter dsh-cli|web-ui  执行器：CLI 无头进程，或起 dsh web 服务驱动真实 UI（默认 dsh-cli；
                            web-ui 需 Profile 配置 web.home/web.port，会话原生落在该 home）
  --model <名>              覆盖会话模型（经每会话 overlay 覆盖 agent-default-model；需与
                            --reasoning-effort 同时给，生效 profile id 追加 @模型-努力程度）
  --reasoning-effort <级别> 努力程度（low/medium/high 等，以平台实际支持为准）
  --title <名>              执行记录标题（写入 run.json，复核服务显示为 run 名；
                            不给时服务端按 评测集·profile·modes×trials 拼默认标题）
`

export async function main(argv: string[], out = process.stdout, err = process.stderr): Promise<number> {
  const [command, ...rest] = argv
  try {
    if (command === 'run') return await cmdRun(rest, out, err)
    if (command === 'inspect') return await cmdInspect(rest, out)
    out.write(USAGE)
    return command === undefined || command === '--help' || command === '-h' ? EXIT_OK : EXIT_CONFIG
  } catch (e) {
    if (e instanceof ConfigError || (e instanceof Error && e.message.startsWith('eval_run:'))) {
      err.write(`${e.message}\n`)
      return EXIT_CONFIG
    }
    if (e instanceof TypeError && 'code' in e && String((e as { code: unknown }).code).startsWith('ERR_PARSE_ARGS')) {
      err.write(`${e.message}\n\n${USAGE}`)
      return EXIT_CONFIG
    }
    throw e
  }
}

async function cmdRun(args: string[], out: NodeJS.WritableStream, err: NodeJS.WritableStream): Promise<number> {
  const { values } = parseArgs({
    args,
    options: {
      evalset: { type: 'string' },
      cases: { type: 'string', multiple: true },
      profile: { type: 'string' },
      out: { type: 'string' },
      modes: { type: 'string', default: 'natural' },
      trials: { type: 'string', default: '3' },
      concurrency: { type: 'string', default: '1' },
      dims: { type: 'string' },
      only: { type: 'string' },
      topic: { type: 'string' },
      anchors: { type: 'string' },
      resume: { type: 'boolean', default: false },
      'keep-workspace': { type: 'boolean', default: false },
      'max-error-rate': { type: 'string', default: '0.2' },
      adapter: { type: 'string', default: 'dsh-cli' },
      model: { type: 'string' },
      'reasoning-effort': { type: 'string' },
      title: { type: 'string' },
      'dry-run': { type: 'boolean', default: false },
    },
    strict: true,
  })
  const missing = (['evalset', 'profile', 'out'] as const).filter((k) => !values[k] && !(k === 'evalset' && values.cases?.length))
  if (missing.length > 0 && !(values['dry-run'] && (values.evalset || values.cases?.length) && values.profile)) {
    throw new ConfigError(`缺少参数：${missing.map((m) => `--${m}`).join(' ')}\n\n${USAGE}`)
  }
  if (values.evalset && values.cases?.length) throw new ConfigError('--cases 与 --evalset 互斥，只能给一个')
  const modes = list(values.modes) as Mode[]
  const badMode = modes.find((m) => !MODES.includes(m))
  if (badMode || modes.length === 0) throw new ConfigError(`--modes 只能是 ${MODES.join('、')}，收到 ${String(values.modes)}`)
  const trials = positiveInt(values.trials, '--trials')
  const concurrency = positiveInt(values.concurrency, '--concurrency')
  const maxErrorRate = Number(values['max-error-rate'])
  if (!(maxErrorRate >= 0 && maxErrorRate <= 1)) throw new ConfigError('--max-error-rate 必须在 0 到 1 之间')
  const dims = list(values.dims)
  const only = list(values.only)
  const topic = values.topic ?? ''
  if (values.anchors !== undefined && values.anchors !== 'on' && values.anchors !== 'off') {
    throw new ConfigError('--anchors 只能是 on 或 off')
  }

  const { profile, sha256: profileSha256 } = await loadProfile(values.profile as string)
  if (profile.dsh.env) Object.assign(process.env, profile.dsh.env)

  // 模型覆盖：overlay 逐会话覆盖 agent-default-model；生效值必须可从 profile id 区分
  // （profile 是比较轴的处理条件，模型/努力程度不同而 id 相同会让对比误判可比）。
  if (values.model !== undefined || values['reasoning-effort'] !== undefined) {
    if (values.model !== undefined) profile.model = values.model as string
    if (values['reasoning-effort'] !== undefined) profile.reasoning_effort = values['reasoning-effort'] as string
    assertModelConfigComplete(profile, '--model/--reasoning-effort')
    profile.id = `${profile.id}@${profile.model}-${profile.reasoning_effort}`
  }

  // --cases：现场编译一次性评测集（dry-run 无 --out 时用临时目录）
  let evalsetPath = values.evalset as string | undefined
  if (values.cases && values.cases.length > 0) {
    const base = values.out ?? (await mkdtemp(join(tmpdir(), 'eval-adhoc-')))
    const built = await buildEvalsetFromMd(values.cases, base, (t) => err.write(t))
    evalsetPath = built.path
    err.write(`已从 md 编译 ${built.version} → ${built.path}
`)
  }

  // 锚点：anchors.txt 与 Profile 同目录；全量 run（无过滤）默认并入，--anchors 可覆盖
  const anchorsFile = join(dirname(values.profile as string), 'anchors.txt')
  const anchorList = (await readFile(anchorsFile, 'utf8').catch(() => ''))
    .split('\n').map((l) => l.trim()).filter((l) => l !== '' && !l.startsWith('#'))
  const fullRun = dims.length === 0 && only.length === 0 && topic === ''
  const anchors = (values.anchors ? values.anchors === 'on' : fullRun) ? anchorList : []

  const lastAverages = values.out ? await lastRunAverages(values.out, profile.id) : undefined

  if (values['dry-run']) {
    const { rows } = await loadEvalset(evalsetPath as string)
    const known = new Set(rows.flatMap((r) => [r.uid, r.case_id]))
    for (const a of anchors) if (!known.has(a)) out.write(`  锚点 ${a} 不在当前评测集中，请替换（anchors.txt）\n`)
    const plan = planEpisodes(rows, profile, DSH_CLI_CAPABILITIES, 'dsh-cli', {
      modes, trials, dims, only, topic, anchors, skillsMode: profile.skills_mode,
    })
    const totalTurns = plan.episodes.reduce((n, e) => n + e.messages.length, 0)
    out.write(
      `评测集 ${String(rows.length)} 行，筛选后 ${String(plan.selected)} 行；计划会话 ${String(plan.episodes.length)} 段（${modes.join('、')} × ${String(trials)} 次），共 ${String(totalTurns)} 轮\n`,
    )
    if (lastAverages) {
      out.write(`  估算：约 ${String(Math.round(totalTurns * lastAverages.tokensPerTurn))} tokens、${(totalTurns * lastAverages.hoursPerTurn).toFixed(1)} 小时（按上一 run 每轮均值）\n`)
      const b = profile.budget
      if (b?.max_tokens && totalTurns * lastAverages.tokensPerTurn > b.max_tokens) out.write(`  提醒：估算超过 budget.max_tokens=${String(b.max_tokens)}\n`)
      if (b?.max_hours && totalTurns * lastAverages.hoursPerTurn > b.max_hours) out.write(`  提醒：估算超过 budget.max_hours=${String(b.max_hours)}\n`)
    }
    out.write(`  skills_mode=${profile.skills_mode}${anchors.length > 0 ? `，锚点 ${String(anchors.length)} 个` : ''}\n`)
    if (profile.model) {
      out.write(`  模型 ${profile.model}（effort ${profile.reasoning_effort}）经 overlay 覆盖，profile id=${profile.id}\n`)
    }
    for (const s of plan.skipped) out.write(`  跳过 ${s.uid}${s.mode ? `（${s.mode}）` : ''}：${s.reason}\n`)
    for (const e of plan.episodes.filter((x) => x.trial === 1)) {
      const turns = e.messages.length
      const first = e.mode === 'forced' ? `/${e.plan.forcedSkills.join(' /')} ${e.messages[0] ?? ''}` : (e.messages[0] ?? '')
      out.write(`  ${e.row.uid} [${e.mode}]${e.anchor ? ' ⚓' : ''} ${turns > 1 ? `${String(turns)} 轮，首轮：` : ''}${first}\n`)
    }
    return EXIT_OK
  }

  const adapterName = values.adapter ?? 'dsh-cli'
  if (adapterName !== 'dsh-cli' && adapterName !== 'web-ui') {
    throw new ConfigError(`--adapter 只能是 dsh-cli、web-ui，收到 ${String(adapterName)}`)
  }
  if (adapterName === 'web-ui' && !profile.web) throw new ConfigError('web-ui 适配器需要 Profile 配置 web 段（home/port）')
  const adapter: AgentAdapter = adapterName === 'web-ui' ? new WebUiAdapter(webUiConfigFromProfile(profile)) : new DshAdapter(profile)
  err.write(
    `dsh ${adapter.version ?? '<版本未知>'}，Profile ${profile.id}${profile.dsh.mirror_sessions ? `，会话镜像到 ${profile.dsh.mirror_sessions}` : ''}\n`,
  )
  try {
    const manifest = await runEvaluation({
      lastAverages,
      evalsetPath: evalsetPath as string,
      profilePath: values.profile as string,
      profile,
      profileSha256,
      outDir: values.out as string,
      adapter,
      title: values.title ?? undefined,
      modes,
      trials,
      concurrency,
      dims,
      only,
      topic: topic || undefined,
      anchors,
      resume: values.resume,
      keepWorkspace: values['keep-workspace'],
      onEpisodeDone: (r, done, total) => {
        const live = r.turns.filter((t) => t.source === 'live').length
        const failedAt = r.failed_turn !== null ? `（第 ${String(r.failed_turn)} 轮）` : ''
        const mirrored = r.artifacts.session_mirror ? ' 已镜像' : ''
        err.write(
          `[${String(done)}/${String(total)}] ${r.uid} ${r.mode} t${String(r.trial)} ${r.status}${failedAt} ` +
            `轮次=${String(live)} skill=${r.loaded_skills.join(',') || '-'} ${String(r.duration_ms)}ms${mirrored}\n`,
        )
      },
    })
    const c = manifest.status_counts
    out.write(
      `完成 ${manifest.run_id}${manifest.title ? `（${manifest.title}）` : ''}：执行 ${String(manifest.episodes.executed)} 段会话，续跑跳过 ${String(manifest.episodes.resumed)}，` +
        `ok ${String(c.ok)} / incomplete ${String(c.incomplete)} / timeout ${String(c.timeout)} / error ${String(c.error)}，` +
        `用例跳过 ${String(manifest.skipped.length)} 项\n`,
    )
    if (manifest.error_rate > maxErrorRate) {
      err.write(`error 与 timeout 占比 ${String(manifest.error_rate)} 超过阈值 ${String(maxErrorRate)}\n`)
      return EXIT_UNHEALTHY
    }
    return EXIT_OK
  } finally {
    await adapter.dispose?.()
  }
}

async function cmdInspect(args: string[], out: NodeJS.WritableStream): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { 'skill-tool': { type: 'string', default: 'skill' } },
    allowPositionals: true,
    strict: true,
  })
  if (positionals.length !== 1) throw new ConfigError(`inspect 需要一个路径\n\n${USAGE}`)
  const files = await findSessionFiles(positionals[0] as string).catch(() => {
    throw new ConfigError(`路径不存在：${String(positionals[0])}`)
  })
  if (files.length === 0) throw new ConfigError(`${String(positionals[0])} 下没有 session 日志`)
  for (const f of files) out.write(`${renderInspect(await inspectSession(f, values['skill-tool']))}\n\n`)
  return EXIT_OK
}

/** 找最近一次同 Profile run 的每轮平均用量（成本估算基数）；无历史返回 null */
async function lastRunAverages(outDir: string, profileId: string): Promise<{ tokensPerTurn: number; hoursPerTurn: number } | undefined> {
  const root = dirname(join(outDir))
  let entries: string[]
  try {
    entries = await readdir(root)
  } catch {
    return undefined
  }
  let best: { mtime: number; tokens: number; turns: number; hours: number } | null = null
  for (const name of entries) {
    const runJson = join(root, name, 'run.json')
    try {
      const text = await readFile(runJson, 'utf8')
      const m = JSON.parse(text) as { profile?: { content?: { id?: string } }; estimate?: { total_turns?: number }; actuals?: { total_tokens?: number; hours?: number } }
      if (m.profile?.content?.id !== profileId) continue
      const turns = m.estimate?.total_turns ?? 0
      const tokens = m.actuals?.total_tokens ?? 0
      const hours = m.actuals?.hours ?? 0
      if (turns <= 0 || tokens <= 0) continue
      const { stat } = { stat: await (await import('node:fs/promises')).stat(runJson) }
      if (best === null || stat.mtimeMs > best.mtime) best = { mtime: stat.mtimeMs, tokens, turns, hours }
    } catch {
      // 无 run.json 或损坏：跳过
    }
  }
  if (best === null) return undefined
  return { tokensPerTurn: best.tokens / best.turns, hoursPerTurn: best.hours / best.turns }
}

function list(value: string | undefined): string[] {
  return value ? value.split(',').map((s) => s.trim()).filter(Boolean) : []
}

function positiveInt(value: string | undefined, name: string): number {
  const n = Number(value)
  if (!Number.isInteger(n) || n <= 0) throw new ConfigError(`${name} 必须是正整数`)
  return n
}

// 直接运行（含经 npm bin 软链接调用）时执行；被测试 import 时不执行
function invokedDirectly(): boolean {
  if (!process.argv[1]) return false
  try {
    return realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)
  } catch {
    return false
  }
}
if (invokedDirectly()) {
  main(process.argv.slice(2)).then((code) => process.exit(code))
}
