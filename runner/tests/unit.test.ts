import { describe, expect, it } from 'vitest'
import { mkdtemp, mkdir, writeFile, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { deriveSessionId } from '../lib/adapters/dsh-cli.js'
import { listSessionDirs, waitForNewSessionDir, parseServiceUrl } from '../lib/adapters/web-ui.js'
import { ScriptedDriver } from '../lib/drivers/scripted.js'
import type { TurnRecord } from '../lib/domain.js'
import { forced, natural } from '../lib/interventions/index.js'
import { mirrorSessions } from '../lib/mirror.js'
import { parseEvalset, planEpisodes } from '../lib/planner.js'
import { extractInjectionsObserved, extractLoadedSkills } from '../lib/probes/index.js'
import { buildForcedPrompt, ConfigError, forcedSkillsFor, parseProfile } from '../lib/profile.js'

const PROFILE = `
id: v1-baseline
dsh: { profile: headless, timeout_ms: 5000 }
routing:
  进单资料审核: [order-intake]
  信用证结算: lc-settlement
`
const ALL = { multiTurn: true, prefillHistory: true }
const NO_PREFILL = { multiTurn: true, prefillHistory: false }

describe('profile', () => {
  it('补全缺省值，字符串路由转为列表', () => {
    const p = parseProfile('id: a\n')
    expect(p.dsh).toEqual({ bin: undefined, profile: 'headless', timeout_ms: 600000 })
    expect(p.skill_tool).toBe('skill')
    expect(parseProfile(PROFILE).routing['信用证结算']).toEqual(['lc-settlement'])
  })

  it('一次报出全部错误', () => {
    try {
      parseProfile('id: "a b"\ndsh: { timeout_ms: -1 }\nrouting: { 主题: [Bad_Name] }\nextra: 1\n')
      expect.unreachable()
    } catch (e) {
      expect(e).toBeInstanceOf(ConfigError)
      for (const part of ['id 必填', 'timeout_ms', '非法 skill 名', '未知字段 extra']) expect((e as Error).message).toContain(part)
    }
  })

  it('强制加载前缀', () => {
    const p = parseProfile(PROFILE)
    expect(forcedSkillsFor('进单资料审核', p)).toEqual(['order-intake'])
    expect(forcedSkillsFor('不存在', p)).toBeNull()
    expect(buildForcedPrompt('江苏舱单能办吗？', ['a', 'b'])).toBe('/a /b 江苏舱单能办吗？')
  })

  it('mirror_sessions 可选，配置后进入 Profile', () => {
    expect(parseProfile(PROFILE).dsh.mirror_sessions).toBeUndefined()
    expect(parseProfile('id: a\ndsh: { mirror_sessions: "C:/x/y" }\n').dsh.mirror_sessions).toBe('C:/x/y')
    expect(() => parseProfile('id: a\ndsh: { mirror_sessions: "" }\n')).toThrow(/mirror_sessions/)
  })

  it('web 段：校验与补全 headless', () => {
    const p = parseProfile('id: a\nweb: { home: "C:/h", port: 3081 }\n')
    expect(p.web).toEqual({ home: 'C:/h', port: 3081, headless: true })
    expect(parseProfile('id: a\nweb: { home: "C:/h", port: 3081, headless: false }\n').web?.headless).toBe(false)
    expect(parseProfile('id: a\n').web).toBeUndefined()
    expect(() => parseProfile('id: a\nweb: { home: "", port: 3081 }\n')).toThrow(/web\.home/)
    expect(() => parseProfile('id: a\nweb: { home: "C:/h", port: 0 }\n')).toThrow(/web\.port/)
    expect(() => parseProfile('id: a\nweb: {}\n')).toThrow(/web/)
  })
})

describe('驱动、干预、探针', () => {
  const live = (index: number): TurnRecord => ({ index, source: 'live', input: '', output: '', status: 'ok', turn_end: null, exit_code: null, duration_ms: 0, observations: null })
  const pre = (index: number): TurnRecord => ({ ...live(index), source: 'prefill' })

  it('脚本驱动只按实际发送的轮次推进，预置历史不计数', () => {
    const d = new ScriptedDriver(['一', '二'])
    expect(d.next([])).toBe('一')
    expect(d.next([pre(1), pre(2)])).toBe('一')
    expect(d.next([live(1)])).toBe('二')
    expect(d.next([live(1), live(2)])).toBeNull()
  })

  it('强制加载只改写第一条实际发送的消息', () => {
    const p = parseProfile(PROFILE)
    const row = { uid: 'a#0', case_id: 'a', variant_index: 0, evalset_version: 'v', dim: 'd', turns: ['x'], topic: '进单资料审核' }
    const plan = forced.plan(row, p)
    expect(plan).toEqual({ ok: true, forcedSkills: ['order-intake'] })
    if (!plan.ok) return
    expect(forced.transform('问题', 0, plan)).toBe('/order-intake 问题')
    expect(forced.transform('追问', 1, plan)).toBe('追问')
    expect(forced.plan({ ...row, topic: null }, p)).toMatchObject({ ok: false, reason: expect.stringContaining('没有 topic') })
    expect(natural.transform('问题', 0, { ok: true, forcedSkills: [] })).toBe('问题')
  })

  it('skill 探针按 callId 关联，区分成功与失败并计算哈希', () => {
    const call = (name: string, id?: string) => ({ name: 'skill', callId: id, argsJson: JSON.stringify({ name }) })
    const skills = extractLoadedSkills(
      [call('order-intake', 'c1'), { name: 'bash', callId: 'c2', argsJson: '{}' }, call('nope', 'c3')],
      [
        { name: 'skill', callId: 'c1', text: '<skill_content name="order-intake">x</skill_content>' },
        { name: 'skill', callId: 'c3', text: 'Unknown skill: nope' },
      ],
      'skill',
    )
    expect(skills.map((s) => [s.name, s.ok])).toEqual([['order-intake', true], ['nope', false]])
    expect(skills[0]?.sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(extractLoadedSkills([{ name: 'load_skill', argsJson: '{"name":"a"}' }], [], 'load_skill')).toHaveLength(1)
  })

  it('注入观测：改写命中为 true，删除看原 key 是否消失，未加载 skill 为 undefined', () => {
    const call = { name: 'skill', callId: 'c1', argsJson: '{"name":"trade-documents"}' }
    const rewrite = { id: 'J1', point: '电放时机', option: '反转', rev: 1, file: 'trade-documents.md', key: '收齐尾款后放单', value: '未收尾款也可先电放' }
    const remove = { ...rewrite, id: 'J2', option: '删除', value: '' }
    const loaded = (text: string) => ({ name: 'skill', callId: 'c1', text })
    expect(extractInjectionsObserved([call], [loaded('<skill_content name="trade-documents">未收尾款也可先电放')], 'skill', [rewrite])).toEqual({ J1: true })
    expect(extractInjectionsObserved([call], [loaded('<skill_content name="trade-documents">收齐尾款后放单')], 'skill', [rewrite])).toEqual({ J1: false })
    expect(extractInjectionsObserved([call], [loaded('<skill_content name="trade-documents">没有该条')], 'skill', [remove])).toEqual({ J2: true })
    expect(extractInjectionsObserved([call], [loaded('<skill_content name="trade-documents">收齐尾款后放单')], 'skill', [remove])).toEqual({ J2: false })
    expect(extractInjectionsObserved([], [], 'skill', [rewrite])).toBeUndefined()
    expect(extractInjectionsObserved([call], [loaded('<skill_content name="trade-documents">x')], 'skill', [])).toBeUndefined()
  })

  it('会话 id 取自目录名，否则取 header', () => {
    expect(deriveSessionId('/r/session-abc/session.v4.jsonl.zstd', undefined)).toBe('session-abc')
    expect(deriveSessionId('/r/raw-1/session.jsonl', '{"type":"session","id":"s-9"}')).toBe('s-9')
    expect(deriveSessionId('/r/raw-1/session.jsonl', '{"type":"session","data":{"sessionId":"s-8"}}')).toBe('s-8')
    expect(deriveSessionId('/r/raw-1/session.jsonl', '{"type":"session"}')).toBeNull()
  })

  it('镜像：拷贝 session-<uuid> 到目标目录（跨 slug 层级），重复执行跳过，裸 UUID 子会话不拷', async () => {
    const root = await mkdtemp(join(tmpdir(), 'mirror-'))
    const slug = join(root, 'run', 'session', '--some-workspace--')
    const sid = 'session-11111111-2222-3333-4444-555555555555'
    await mkdir(join(slug, sid), { recursive: true })
    await mkdir(join(slug, '99999999-2222-3333-4444-555555555555'), { recursive: true }) // 子会话（裸 UUID）
    await writeFile(join(slug, sid, 'session.v4.jsonl.zstd'), 'log')
    const target = join(root, 'client')
    expect(await mirrorSessions(join(root, 'run', 'session'), target)).toBe(1)
    expect(await readdir(target)).toEqual([sid])
    expect(await mirrorSessions(join(root, 'run', 'session'), target)).toBe(1) // 幂等：已存在跳过
    expect(await mirrorSessions(join(root, 'no-such'), target)).toBe(0)
  })

  it('web-ui：解析服务地址；发现新建的会话目录', async () => {
    expect(parseServiceUrl('dsh web: http://127.0.0.1:3081/?token=Ab_cd-12')).toBe('http://127.0.0.1:3081/?token=Ab_cd-12')
    expect(parseServiceUrl('别的内容')).toBeNull()

    const home = await mkdtemp(join(tmpdir(), 'webui-'))
    const before = await listSessionDirs(home)
    expect(before.size).toBe(0)
    const sid = 'session-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    const appeared = waitForNewSessionDir(home, before, 5_000).then((p) => p.endsWith(sid))
    await new Promise((r) => setTimeout(r, 600))
    await mkdir(join(home, 'sessions', '--ws--', sid), { recursive: true })
    expect(await appeared).toBe(true)
    expect((await listSessionDirs(home)).size).toBe(1)
  })
})

describe('评测集与规划', () => {
  const row = (uid: string, extra: Record<string, unknown> = {}) =>
    JSON.stringify({ uid, case_id: uid.split('#')[0], variant_index: 0, evalset_version: 'v1', dim: '知识召回', turns: ['问题'], ...extra })

  it('校验字段并报出行号', () => {
    const text = [row('a#0'), '{bad', JSON.stringify({ uid: 'b#0' }), row('a#0'), row('c#0', { turns: ['一', '二'], assistant_refs: [null] })].join('\n')
    expect(() => parseEvalset(text)).toThrow(/第 2 行：不是合法的 JSON[\s\S]*第 3 行：字段缺失[\s\S]*第 4 行：uid a#0 与第 1 行重复[\s\S]*第 5 行：.*assistant_refs/)
  })

  it('多轮 live 可运行；prefill 按执行器能力决定；强制加载按路由决定', () => {
    const rows = parseEvalset(
      [
        row('kr-1#0', { topic: '进单资料审核' }),
        row('kr-2#0'),
        row('mt-1#0', { turns: ['一', '二'], dim: '多轮上下文召回', topic: '进单资料审核' }),
        row('mt-2#0', { turns: ['一', '二'], assistant_refs: ['回', null], history: 'prefill' }),
      ].join('\n'),
    )
    const p = parseProfile(PROFILE)
    const plan = planEpisodes(rows, p, NO_PREFILL, 'dsh-cli', { modes: ['natural', 'forced'], trials: 2 })
    expect(plan.episodes.map((e) => `${e.row.uid}/${e.mode}`).filter((_, i) => i % 2 === 0)).toEqual(['kr-1#0/natural', 'kr-1#0/forced', 'kr-2#0/natural', 'mt-1#0/natural', 'mt-1#0/forced'])
    expect(plan.skipped).toEqual([
      { uid: 'kr-2#0', mode: 'forced', reason: expect.stringContaining('没有 topic') },
      { uid: 'mt-2#0', mode: null, reason: '执行器 dsh-cli 不支持预置历史（history: prefill）' },
    ])
    expect(planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1 }).episodes).toHaveLength(4)
    expect(planEpisodes(rows, p, { multiTurn: false, prefillHistory: false }, 'x', { modes: ['natural'], trials: 1 }).skipped[0]?.reason).toContain('不支持多轮')
  })

  it('按维度与用例筛选', () => {
    const rows = parseEvalset([row('kr-1#0'), row('kr-1#1'), row('ec-1#0', { dim: '错误知识纠正' })].join('\n'))
    const p = parseProfile(PROFILE)
    expect(planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, only: ['kr-1'] }).episodes).toHaveLength(2)
    expect(planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, only: ['kr-1#1'] }).episodes).toHaveLength(1)
    // default-roots（过渡期）下 ec 整体跳过；isolated / bundled-copy 纳入（阻断项机制）
    const ecSkip = planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, dims: ['错误知识纠正'] })
    expect(ecSkip.episodes).toHaveLength(0)
    expect(ecSkip.skipped[0]?.reason).toContain('isolation-not-verified')
    expect(planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, dims: ['错误知识纠正'], skillsMode: 'isolated' }).episodes).toHaveLength(1)
    expect(planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, dims: ['错误知识纠正'], skillsMode: 'bundled-copy' }).episodes).toHaveLength(1)
  })
})

describe('dsh-kernel splitDshBin（引号兼容）', () => {
  it('无引号时按空白拆分（与原行为一致）', async () => {
    const { splitDshBin } = await import('../lib/vendor/dsh-kernel.js')
    expect(splitDshBin('dsh --profile headless')).toEqual({ bin: 'dsh', prefixArgs: ['--profile', 'headless'] })
    expect(splitDshBin('  npx   -y  dsh ')).toEqual({ bin: 'npx', prefixArgs: ['-y', 'dsh'] })
  })
  it('双引号包裹的带空格 exe 路径不拆散', async () => {
    const { splitDshBin } = await import('../lib/vendor/dsh-kernel.js')
    const bin = '"D:/dsh/DeepSeek Harness.exe" --expose-internals D:/dsh/resources/app.asar/dsh/cli.js'
    const cmd = splitDshBin(bin)
    expect(cmd.bin).toBe('D:/dsh/DeepSeek Harness.exe')
    expect(cmd.prefixArgs).toEqual(['--expose-internals', 'D:/dsh/resources/app.asar/dsh/cli.js'])
  })
  it('空配置报错', async () => {
    const { splitDshBin } = await import('../lib/vendor/dsh-kernel.js')
    expect(() => splitDshBin('   ')).toThrow(/empty/)
  })
})
