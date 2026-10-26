import { mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import type { AdapterCapabilities, EpisodeRecord, RunManifest, TurnOutcome } from '../lib/domain.js'
import { runEvaluation } from '../lib/orchestrator.js'
import type { AgentAdapter, AgentSession, PrefillTurn, SessionContext } from '../lib/ports.js'
import { parseProfile } from '../lib/profile.js'

const tokens = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, reasoning: 0, total: 2 }

/** 内存中的适配器：记住每段会话收到的消息，回答里带上第一轮内容 */
class FakeAdapter implements AgentAdapter {
  readonly name = 'fake'
  readonly version = 'fake-1'
  sessions: { ctx: SessionContext; prefill?: readonly PrefillTurn[]; sent: string[] }[] = []
  active = 0
  maxActive = 0
  constructor(
    readonly capabilities: AdapterCapabilities = { multiTurn: true, prefillHistory: false },
    private readonly behave: (message: string, turn: number) => Partial<TurnOutcome> | 'throw' = () => ({}),
  ) {}
  async open(ctx: SessionContext, prefill?: readonly PrefillTurn[]): Promise<AgentSession> {
    const s = { ctx, prefill, sent: [] as string[] }
    this.sessions.push(s)
    // 模拟 dsh 在 runDir/session/<slug>/session-<uuid>/ 落盘日志
    await mkdir(join(ctx.runDir, 'session', '--fake-ws--', 'session-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'), { recursive: true })
    return {
      send: async (message: string) => {
        this.active++
        this.maxActive = Math.max(this.maxActive, this.active)
        await new Promise((r) => setTimeout(r, 5))
        this.active--
        s.sent.push(message)
        const extra = this.behave(message, s.sent.length)
        if (extra === 'throw') throw new Error('adapter exploded')
        const first = prefill?.[0]?.user ?? s.sent[0]
        const skill = message.includes('舱单') ? [{ name: 'skill', callId: 'c1', argsJson: '{"name":"order-intake"}' }] : []
        return {
          status: 'ok',
          durationMs: 5,
          trace: {
            finalText: `答：${message}｜上下文：${first}`,
            turnEnd: 'completed',
            toolCalls: skill,
            toolResults: skill.length ? [{ name: 'skill', callId: 'c1', text: '<skill_content name="order-intake">x' }] : [],
            toolErrors: [],
            steps: 1,
            tokens,
          },
          ...extra,
        }
      },
      close: async () => ({ sessionLog: join(ctx.runDir, 'log'), sessionId: 'sid' }),
    }
  }
}

const profile = parseProfile('id: p1\nrouting:\n  进单资料审核: [order-intake]\n')
const row = (uid: string, extra: Record<string, unknown> = {}) =>
  JSON.stringify({ uid, case_id: uid.split('#')[0], variant_index: Number(uid.split('#')[1]), evalset_version: 'v1', dim: '知识召回', topic: '进单资料审核', turns: [`问题 ${uid}`], ...extra })

let dir: string
let evalsetPath: string
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'orch-'))
  evalsetPath = join(dir, 'evalset.jsonl')
  await writeFile(
    evalsetPath,
    [
      row('kr-2#0', { turns: ['江苏舱单能办吗？'] }),
      row('kr-1#0'),
      row('mt-1#0', { dim: '多轮上下文召回', turns: ['提单号 A1', '船期下周', '那提单号是多少？'] }),
      row('mt-2#0', { dim: '多轮上下文召回', turns: ['客户自己报关', '要哪些单据？'], assistant_refs: ['收到', null], history: 'prefill' }),
    ].join('\n'),
  )
})

const base = (adapter: AgentAdapter, extra: Record<string, unknown> = {}) => ({
  evalsetPath,
  profilePath: join(dir, 'profile.yaml'),
  profile,
  profileSha256: 'x',
  outDir: join(dir, 'out'),
  adapter,
  modes: ['natural', 'forced'] as const,
  trials: 2,
  concurrency: 3,
  ...extra,
})
const results = async (): Promise<EpisodeRecord[]> =>
  (await readFile(join(dir, 'out', 'results.jsonl'), 'utf8')).trim().split('\n').map((l) => JSON.parse(l) as EpisodeRecord)
const manifest = async (): Promise<RunManifest> => JSON.parse(await readFile(join(dir, 'out', 'run.json'), 'utf8')) as RunManifest

describe('runEvaluation', () => {
  it('多轮逐轮发送、同一会话共享工作区，记录每轮结果与观测', async () => {
    const adapter = new FakeAdapter()
    const m = await runEvaluation(base(adapter))
    const rows = await results()
    expect(rows).toHaveLength(3 * 2 * 2) // mt-2 为 prefill，执行器不支持，跳过
    const mt = rows.find((r) => r.uid === 'mt-1#0' && r.mode === 'natural') as EpisodeRecord
    expect(mt.turns.map((t) => [t.index, t.source, t.status])).toEqual([[1, 'live', 'ok'], [2, 'live', 'ok'], [3, 'live', 'ok']])
    expect(mt.final_output).toBe('答：那提单号是多少？｜上下文：提单号 A1')
    expect(mt).toMatchObject({ schema: 3, status: 'ok', failed_turn: null, history: 'live', usage: { steps: 3 } })
    const session = adapter.sessions.find((s) => s.sent[0] === '提单号 A1')
    expect(session?.sent).toHaveLength(3)
    // 强制加载只改写第一轮
    const forcedMt = rows.find((r) => r.uid === 'mt-1#0' && r.mode === 'forced') as EpisodeRecord
    expect(forcedMt.turns.map((t) => t.input)).toEqual(['/order-intake 提单号 A1', '船期下周', '那提单号是多少？'])
    expect(rows.find((r) => r.uid === 'kr-2#0')?.loaded_skills).toEqual(['order-intake'])
    expect(m.skipped).toEqual([{ uid: 'mt-2#0', mode: null, reason: expect.stringContaining('预置历史') }])
    expect(m.agent).toEqual({ name: 'fake', version: 'fake-1', capabilities: { multiTurn: true, prefillHistory: false } })
    expect(m.episodes).toEqual({ planned: 12, resumed: 0, executed: 12 })
    expect(adapter.maxActive).toBeGreaterThan(1)
    expect(adapter.maxActive).toBeLessThanOrEqual(3)
  })

  it('支持预置历史的执行器：前面各轮记为 prefill，只发送最后一轮', async () => {
    const adapter = new FakeAdapter({ multiTurn: true, prefillHistory: true })
    await runEvaluation(base(adapter, { modes: ['natural'], trials: 1, only: ['mt-2'] }))
    const [rec] = await results()
    expect(rec?.turns.map((t) => [t.index, t.source, t.output])).toEqual([[1, 'prefill', '收到'], [2, 'live', '答：要哪些单据？｜上下文：客户自己报关']])
    expect(adapter.sessions[0]?.prefill).toEqual([{ user: '客户自己报关', assistant: '收到' }])
    expect(adapter.sessions[0]?.sent).toEqual(['要哪些单据？'])
  })

  it('某一轮失败即停止该会话，记录失败轮次；异常不影响其他会话', async () => {
    const adapter = new FakeAdapter(undefined, (msg, turn) => (msg.includes('船期') ? { status: 'timeout', error: 't' } : msg.includes('kr-1') && turn === 1 ? 'throw' : {}))
    const m = await runEvaluation(base(adapter, { modes: ['natural'], trials: 1 }))
    const rows = await results()
    const mt = rows.find((r) => r.uid === 'mt-1#0') as EpisodeRecord
    expect(mt).toMatchObject({ status: 'timeout', failed_turn: 2, error: 't' })
    expect(mt.turns).toHaveLength(2)
    expect(rows.find((r) => r.uid === 'kr-1#0')).toMatchObject({ status: 'error', failed_turn: 1, error: expect.stringContaining('adapter exploded') })
    expect(m.status_counts).toEqual({ ok: 1, incomplete: 0, timeout: 1, error: 1 })
    expect(m.error_rate).toBeCloseTo(0.6667, 3)
  })

  it('配置 mirror_sessions 时每段会话自动镜像到目标目录并记录在 artifacts', async () => {
    const mirror = join(dir, 'client-sessions')
    await runEvaluation(base(new FakeAdapter(), { modes: ['natural'], trials: 1, only: ['kr-1'] }))
    const rows = await results()
    expect(rows.every((r) => r.artifacts.session_mirror === null || r.artifacts.session_mirror === undefined)).toBe(true)

    await runEvaluation(
      base(new FakeAdapter(), {
        modes: ['natural'],
        trials: 1,
        only: ['kr-1'],
        outDir: join(dir, 'out-mirror'),
        profile: parseProfile(`id: p1\nrouting:\n  进单资料审核: [order-intake]\ndsh: { mirror_sessions: "${join(dir, 'client-sessions').replaceAll('\\', '/')}" }\n`),
      }),
    )
    const mirrored = (await readFile(join(dir, 'out-mirror', 'results.jsonl'), 'utf8')).trim().split('\n').map((l) => JSON.parse(l) as EpisodeRecord)
    expect(mirrored.every((r) => r.artifacts.session_mirror === join(dir, 'client-sessions')))
    expect(await readdir(mirror)).toContain('session-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
  })

  it('续跑只执行缺少的会话并沿用 run_id；拒绝混入其他 Profile 的结果', async () => {
    await runEvaluation(base(new FakeAdapter(), { trials: 1 }))
    const first = await manifest()
    await expect(runEvaluation(base(new FakeAdapter(), { trials: 2 }))).rejects.toThrow(/--resume/)
    const adapter = new FakeAdapter()
    const m = await runEvaluation(base(adapter, { trials: 2, resume: true }))
    expect(adapter.sessions).toHaveLength(6)
    expect(m.episodes).toEqual({ planned: 12, resumed: 6, executed: 6 })
    expect(m.run_id).toBe(first.run_id)
    await expect(runEvaluation(base(new FakeAdapter(), { profile: parseProfile('id: p2\n'), resume: true }))).rejects.toThrow(/Profile p1/)
  })

  it('--title 写入 run.json，续跑未再给时保留原标题', async () => {
    await runEvaluation(base(new FakeAdapter(), { modes: ['natural'], trials: 1, only: ['kr-1'], title: '基线-glm53low' }))
    expect((await manifest()).title).toBe('基线-glm53low')
    await runEvaluation(base(new FakeAdapter(), { modes: ['natural'], trials: 1, only: ['kr-1'], resume: true }))
    expect((await manifest()).title).toBe('基线-glm53low')
    await runEvaluation(base(new FakeAdapter(), { modes: ['natural'], trials: 1, only: ['kr-2'], outDir: join(dir, 'out-2') }))
    const m2 = JSON.parse(await readFile(join(dir, 'out-2', 'run.json'), 'utf8')) as RunManifest
    expect(m2.title).toBeNull()                                      // 不给 --title → 不命名
  })
})
