import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { beforeAll, describe, expect, it } from 'vitest'
import { DshAdapter } from '../lib/adapters/dsh-cli.js'
import { main } from '../lib/cli.js'
import type { EpisodeRecord, RunManifest } from '../lib/domain.js'
import { findSessionFiles, inspectSession } from '../lib/inspect.js'
import { parseProfile } from '../lib/profile.js'

const here = dirname(fileURLToPath(import.meta.url))
// exe 路径带空格时（如 C:\Program Files\nodejs）必须双引号包裹，splitDshBin 支持引号拆分；
// 外层再套 YAML 单引号，避免 YAML 把内层双引号当作字符串边界。
const FAKE_DSH = `"${process.execPath}" ${join(here, 'fake-dsh.mjs')}`
// win32 上 python3 常是 Microsoft Store 空壳别名，先试真解释器 python
const PY = process.platform === 'win32' ? 'python' : 'python3'
const REPO = resolve(here, '..', '..')

const profileYaml = (timeout = 10000) => `id: fake-v1
dsh:
  bin: '${FAKE_DSH}'
  profile: headless
  timeout_ms: ${String(timeout)}
routing:
  进单资料审核: [order-intake]
`

let tmp: string
beforeAll(async () => {
  tmp = await mkdtemp(join(tmpdir(), 'e2e-'))
})

async function conversation(messages: string[], timeout = 10000) {
  const adapter = new DshAdapter(parseProfile(profileYaml(timeout)))
  const runDir = await mkdtemp(join(tmp, 'run-'))
  const session = await adapter.open({ runDir, workspace: join(runDir, 'workspace'), timeoutMs: timeout })
  const outcomes = []
  for (const m of messages) outcomes.push(await session.send(m))
  return { adapter, outcomes, artifacts: await session.close() }
}

class Sink {
  text = ''
  write(s: string) {
    this.text += s
    return true
  }
}

// 命令行端到端与 bundled-copy 两个 describe 共用（后者依赖前者 beforeAll 编译的 evalset）
let evalset: string
let profilePath: string

describe('DshAdapter（假 dsh）', () => {
  it('单轮：探测版本，读取 zstd 日志，采集回答与 skill 调用', async () => {
    const { adapter, outcomes, artifacts } = await conversation(['江苏的舱单能办吗？'])
    expect(adapter.version).toBe('fake-dsh 0.2.1-test')
    expect(outcomes[0]).toMatchObject({ status: 'ok', exitCode: 0, trace: { turnEnd: 'completed', finalText: '回答：江苏的舱单能办吗？', steps: 2 } })
    expect(outcomes[0]?.trace.toolCalls.map((c) => c.name)).toEqual(['skill'])
    expect(artifacts.sessionLog).toMatch(/session-[0-9a-f-]+[\\/]session\.v4\.jsonl\.zstd$/)
    expect(artifacts.sessionId).toMatch(/^session-/)
  })

  it('多轮：用 --session-id 续接，每轮只解析本轮新增的日志', async () => {
    const { outcomes } = await conversation(['提单号 COSU6281734，客户自己报关', '船期下周三', '这单融资要哪些单据？'])
    expect(outcomes.map((o) => o.status)).toEqual(['ok', 'ok', 'ok'])
    expect(outcomes[2]?.trace.finalText).toBe('回答：这单融资要哪些单据？（上下文：提单号 COSU6281734，客户自己报关）')
    // 每轮的工具调用与 token 只属于本轮
    expect(outcomes.map((o) => o.trace.toolCalls.length)).toEqual([0, 0, 1])
    expect(outcomes.map((o) => o.trace.tokens.input)).toEqual([120, 120, 120])
  })

  it('未压缩日志的多轮续接；回合未正常结束记为 incomplete', async () => {
    const { outcomes } = await conversation(['[plain] 第一轮', '[incomplete] 第二轮'])
    expect(outcomes.map((o) => o.status)).toEqual(['ok', 'incomplete'])
    expect(outcomes[1]?.trace.finalText).toContain('（上下文：[plain] 第一轮）')
  })

  it('会话 id 无法确定时，第二轮明确报错', async () => {
    const { outcomes } = await conversation(['[noid] 第一轮', '第二轮'])
    expect(outcomes[0]?.status).toBe('ok')
    expect(outcomes[1]).toMatchObject({ status: 'error', error: expect.stringContaining('会话 id') })
  })

  it('非零退出、超时、找不到 dsh', async () => {
    const crash = await conversation(['[crash]'])
    expect(crash.outcomes[0]).toMatchObject({ status: 'error', exitCode: 3, stderrTail: 'boom' })
    const started = Date.now()
    const hang = await conversation(['[hang]'], 800)
    expect(hang.outcomes[0]?.status).toBe('timeout')
    expect(Date.now() - started).toBeLessThan(5000)
    expect(() => new DshAdapter(parseProfile('id: x\ndsh: { bin: /no/such/dsh }\n'))).toThrow(/dsh executable not found/)
  })
})

describe('命令行端到端：md 用例 → evalset.jsonl → results.jsonl', () => {
  beforeAll(async () => {
    evalset = join(tmp, 'evalset.jsonl')
    // 对话记录写法、live 模式（examples 中的 mt-0003 是 prefill，会按能力跳过）
    const extra = join(tmp, 'extra-cases')
    await mkdir(extra, { recursive: true })
    await writeFile(
      join(extra, 'mt-0901.md'),
      '---\nid: mt-0901\ndim: 多轮上下文召回\nstatus: structured\n---\n\n## 对话记录\n用户：客户自己报关，提单号 COSU6281734。\n助手：收到。\n用户：这单融资要哪些单据？\n\n## 关键条件\n- 上下文：报关方\n\n## 必要事实\n- F1：放行后的报关单 【关键词：放行后的报关单】\n',
    )
    execFileSync(PY, [join(REPO, 'evalset', 'md2case.py'), 'build', join(REPO, 'evalset', 'cases', 'order_intake'), join(REPO, 'evalset', 'examples', 'multi_turn'), extra, '-o', evalset, '--version', 'v0-draft', '--include', 'structured,admitted'])
    profilePath = join(tmp, 'profile.yaml')
    await writeFile(profilePath, profileYaml())
  })

  it('dry-run 列出会话与跳过项，不需要 dsh', async () => {
    const out = new Sink()
    const code = await main(['run', '--evalset', evalset, '--profile', profilePath, '--dry-run', '--modes', 'natural,forced', '--trials', '2'], out as never, new Sink() as never)
    expect(code).toBe(0)
    expect(out.text).toContain('[forced] /order-intake 我司报关的融资业务，需要客户提供哪些融资单据？')
    expect(out.text).toMatch(/跳过 ec-0201#0：isolation-not-verified/)
    expect(out.text).toMatch(/mt-0201#0 \[natural\] 3 轮，首轮：/)
    expect(out.text).toMatch(/跳过 mt-0003#0：执行器 dsh-cli 不支持预置历史/)
  })

  it('四种多轮写法都能跑通，结果按轮记录', async () => {
    const outDir = join(tmp, 'out')
    const err = new Sink()
    const code = await main(
      ['run', '--evalset', evalset, '--profile', profilePath, '--out', outDir, '--modes', 'natural,forced', '--trials', '1', '--concurrency', '4', '--only', 'mt-0001,mt-0002,mt-0004,mt-0901,mt-0201,kr-0205'],
      new Sink() as never,
      err as never,
    )
    expect(code).toBe(0)
    const rows = (await readFile(join(outDir, 'results.jsonl'), 'utf8')).trim().split('\n').map((l) => JSON.parse(l) as EpisodeRecord)
    const get = (uid: string, mode = 'natural') => rows.find((r) => r.uid === uid && r.mode === mode) as EpisodeRecord

    // 列表写法（mt-0201）：3 轮，最后一轮带上第 1 轮的上下文，且加载了 skill
    const mt = get('mt-0201#0')
    expect(mt.turns.map((t) => t.status)).toEqual(['ok', 'ok', 'ok'])
    expect(mt.final_output).toContain('（上下文：我这有一单，客户说报关他们自己找人做')
    expect(mt.loaded_skills).toEqual(['order-intake'])
    expect(mt.artifacts.session_id).toMatch(/^session-/)
    // 分轮小节写法（mt-0002）：多行消息原样发送
    expect(get('mt-0002#0').turns[0]?.input).toBe('客户 C 发来一单出口资料，情况如下：\n提单号 OOLU2701556，我司报关，要做融资。')
    // jsonl 写法（mt-0004）：2 轮
    expect(get('mt-0004#0').turns).toHaveLength(2)
    // 对话记录写法（mt-0901，live）：记录中的助手回复只作参考，不发送
    expect(get('mt-0901#0').turns.map((t) => t.input)).toEqual(['客户自己报关，提单号 COSU6281734。', '这单融资要哪些单据？'])
    expect(get('mt-0901#0').final_output).toContain('（上下文：客户自己报关')
    // 强制加载只作用于第一轮
    expect(get('mt-0201#0', 'forced').turns.map((t) => t.input.startsWith('/order-intake '))).toEqual([true, false, false])
    expect(rows.every((r) => r.status === 'ok' && r.artifacts.session_log && existsSync(r.artifacts.session_log))).toBe(true)
    // 工作区在会话结束后删除
    expect(rows.every((r) => !existsSync(join(r.artifacts.run_dir, 'workspace')))).toBe(true)

    const m = JSON.parse(await readFile(join(outDir, 'run.json'), 'utf8')) as RunManifest
    expect(m).toMatchObject({ agent: { name: 'dsh-cli', version: 'fake-dsh 0.2.1-test' }, error_rate: 0 })
    expect(err.text).toMatch(/mt-0201#0 natural t1 ok 轮次=3 skill=order-intake/)

    // inspect：强制注入在日志中的形式
    const forcedLog = get('kr-0205#0', 'forced').artifacts.session_log as string
    const report = await inspectSession(forcedLog)
    expect(report.frameTypes['context/inject']).toBe(1)
    expect((await findSessionFiles(join(outDir, '.work'))).length).toBe(rows.length)
  })

  it('中途某一轮失败：记录失败轮次，退出码按错误占比判定', async () => {
    const bad = join(tmp, 'bad.jsonl')
    await writeFile(bad, `${JSON.stringify({ uid: 'x#0', case_id: 'x', variant_index: 0, evalset_version: 'v', dim: 'd', turns: ['第一轮', '[crash] 第二轮', '第三轮'] })}\n`)
    const out = join(tmp, 'out-bad')
    const code = await main(['run', '--evalset', bad, '--profile', profilePath, '--out', out, '--trials', '1'], new Sink() as never, new Sink() as never)
    expect(code).toBe(2)
    const [rec] = (await readFile(join(out, 'results.jsonl'), 'utf8')).trim().split('\n').map((l) => JSON.parse(l) as EpisodeRecord)
    expect(rec).toMatchObject({ status: 'error', failed_turn: 2 })
    expect(rec?.turns).toHaveLength(2)
  })

  it('参数与配置错误返回 1', async () => {
    expect(await main(['run', '--evalset', evalset], new Sink() as never, new Sink() as never)).toBe(1)
    expect(await main(['run', '--evalset', evalset, '--profile', profilePath, '--out', join(tmp, 'o'), '--modes', 'sometimes'], new Sink() as never, new Sink() as never)).toBe(1)
    expect(await main(['bogus'], new Sink() as never, new Sink() as never)).toBe(1)
  })
})

describe('--cases：markdown 直接启动', () => {
  let prof: string
  beforeAll(async () => {
    prof = join(tmp, 'cases-profile.yaml')
    await writeFile(prof, profileYaml())
  })
  it('dry-run 可直接吃 md 目录，自动编译（adhoc 版本）', async () => {
    const out = new Sink()
    const code = await main(
      ['run', '--cases', join(REPO, 'evalset', 'examples', 'multi_turn'),
       '--profile', prof, '--dry-run', '--modes', 'natural', '--trials', '1'],
      out as never, new Sink() as never,
    )
    expect(code).toBe(0)
    expect(out.text).toMatch(/mt-0001#0 \[natural\]/)
    expect(out.text).toContain('跳过 mt-0003#0：执行器 dsh-cli 不支持预置历史')
  })
  it('--cases 与 --evalset 互斥', async () => {
    const code = await main(
      ['run', '--cases', join(REPO, 'evalset', 'examples'), '--evalset', 'x.jsonl',
       '--profile', prof, '--dry-run'],
      new Sink() as never, new Sink() as never,
    )
    expect(code).toBe(1)
  })
})

describe('bundled-copy 模式（P0-3：逐会话副本 + DSH_BUNDLED_SKILL_DIR）', () => {
  it('ec 纳入规划；副本物化注入；逐会话 env 到达 dsh 进程', async () => {
    // 基底根：含 ec-0201 J1 注入 key 的 trade-documents.md
    const root = join(tmp, 'skills-root')
    await mkdir(join(root, 'sub'), { recursive: true })
    await writeFile(join(root, 'trade-documents.md'),
      '# trade-documents\n\n- **电放 Telex Release**：货物仍在本方掌控（收齐尾款）后，放单给买方快速提货。\n', 'utf8')
    const prof = join(tmp, 'bundledcopy-profile.yaml')
    await writeFile(prof, `id: fake-bundledcopy
dsh:
  bin: '${FAKE_DSH}'
  profile: headless
  timeout_ms: 10000
skills_mode: bundled-copy
skills_roots: ['${root.replace(/\\/g, '/')}']
`)

    const outDir = join(tmp, 'out-bundledcopy')
    const code = await main(
      ['run', '--evalset', evalset, '--profile', prof, '--out', outDir,
       '--modes', 'natural', '--trials', '1', '--only', 'ec-0201'],
      new Sink() as never, new Sink() as never,
    )
    expect(code).toBe(0)
    const records: EpisodeRecord[] = (await readFile(join(outDir, 'results.jsonl'), 'utf8'))
      .split('\n').filter(Boolean).map((l) => JSON.parse(l))
    expect(records).toHaveLength(1)                              // ec 没有被 isolation-not-verified 跳过
    const rec = records[0] as EpisodeRecord
    expect(rec.status).toBe('ok')
    expect(rec.skills_sha256).toBeTruthy()
    expect(rec.injections_applied?.[0]).toMatchObject({ point: '电放时机', option: '反转', found: 1 })
    // 副本中 key 已被替换为注入值
    const workDir = join(outDir, '.work', '0001-ec-0201-0-natural-t1')
    const copy = await readFile(join(workDir, 'skills', '0', 'trade-documents.md'), 'utf8')
    expect(copy).toContain('未收尾款也可先电放提货')
    expect(copy).not.toContain('货物仍在本方掌控（收齐尾款）后，放单给买方快速提货')
    // 逐会话 env 到达 dsh 进程（fake dsh 把 DSH_BUNDLED_SKILL_DIR 记进 state.json）
    const states = (await import('node:fs')).readdirSync(join(workDir, 'session'), { recursive: false })
    const sessionId = states.find((s) => String(s).startsWith('session-'))
    const state = JSON.parse(await readFile(join(workDir, 'session', sessionId as string, 'state.json'), 'utf8'))
    expect(state.bundledEnv).toBe(join(workDir, 'skills', '0'))
  })
})
