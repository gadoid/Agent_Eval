import { describe, expect, it } from 'vitest'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import type { InjectionOp } from '../src/domain.js'
import { userTurnsOf } from '../src/domain.js'
import { buildModelOverlayEntries, buildSkillsOverlayEntries, materializeSkills, rehearseInjections } from '../src/materialize.js'
import { parseEvalset, planEpisodes } from '../src/planner.js'
import { parseProfile } from '../src/profile.js'
import { computeSnapshot, hashTree } from '../src/snapshot.js'

const ALL = { multiTurn: true, prefillHistory: false }

async function makeRoot(files: Record<string, string>): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), 'skills-'))
  for (const [name, text] of Object.entries(files)) {
    await mkdir(join(root, name, '..'), { recursive: true })
    await writeFile(join(root, name), text, 'utf8')
  }
  return root
}

const INJ: InjectionOp[] = [
  { id: 'J1', point: '电放时机', option: '反转', rev: 1, file: 'trade-documents.md',
    key: '收齐尾款后电放', value: '未收尾款也可先电放' },
]

describe('materialize：副本、注入与接线（03 §5.2）', () => {
  it('复制基底并应用注入：精确替换、计数、sha256 稳定', async () => {
    const root = await makeRoot({ 'trade-documents.md': '---\nname: trade-documents\ndescription: x\n---\n规则：收齐尾款后电放。\n' })
    const runDir = await mkdtemp(join(tmpdir(), 'ep-'))
    const m1 = await materializeSkills(runDir, [root], INJ)
    expect(m1.error).toBeUndefined()
    expect(m1.applied).toEqual([{ point: '电放时机', option: '反转', rev: 1, found: 1 }])
    const patched = await readFile(join(runDir, 'skills', '0', 'trade-documents.md'), 'utf8')
    expect(patched).toContain('未收尾款也可先电放')
    expect(patched).not.toContain('收齐尾款后电放')
    const m2 = await materializeSkills(runDir, [root], INJ)
    expect(m2.sha256).toBe(m1.sha256)          // 同输入同指纹
    expect(m2.sha256).not.toBe(await hashTree([root]))  // 与基底不同（已替换）
  })

  it('无注入即原样副本：sha256 等于基底（环境对称原则）', async () => {
    const root = await makeRoot({ 'a.md': 'A' })
    const runDir = await mkdtemp(join(tmpdir(), 'ep-'))
    const m = await materializeSkills(runDir, [root], [])
    expect(m.applied).toEqual([])
    expect(m.sha256).toBe(await hashTree([root]))
  })

  it('key 未命中 → error（injection-key-missed），不降级为无注入运行', async () => {
    const root = await makeRoot({ 'trade-documents.md': '内容里没有 key' })
    const runDir = await mkdtemp(join(tmpdir(), 'ep-'))
    const m = await materializeSkills(runDir, [root], INJ)
    expect(m.error?.code).toBe('injection-key-missed')
  })

  it('文件缺失 → injection-file-missing', async () => {
    const root = await makeRoot({ 'other.md': 'x' })
    const runDir = await mkdtemp(join(tmpdir(), 'ep-'))
    const m = await materializeSkills(runDir, [root], INJ)
    expect(m.error?.code).toBe('injection-file-missing')
  })

  it('同名 skill 按盘点生效来源去重', async () => {
    const r0 = await makeRoot({ 's.md': '---\nname: s\n---\n根0内容' })
    const r1 = await makeRoot({ 's.md': '---\nname: s\n---\n根1内容' })
    const runDir = await mkdtemp(join(tmpdir(), 'ep-'))
    const inv = { roots: [], skills: [{ name: 's', root: r1, file: 's.md' }] }
    await materializeSkills(runDir, [r0, r1], [], inv)
    await expect(readFile(join(runDir, 'skills', '0', 's.md'))).rejects.toThrow()   // 非生效副本被删
    expect(await readFile(join(runDir, 'skills', '1', 's.md'), 'utf8')).toContain('根1内容')
  })

  it('overlay 生成 skill-filesystem 段（includeDefaultRoots: false）', () => {
    const yaml = buildSkillsOverlayEntries(['D:/a/skills/0'])
    expect(yaml).toContain("name: '@deepseek-ai/dsh-skill-filesystem'")
    expect(yaml).toContain('includeDefaultRoots: false')
    expect(yaml).toContain('"D:/a/skills/0"')
  })
})

describe('注入预演（规划期，只读）', () => {
  it('key 命中 0 次的用例判不可运行', async () => {
    const root = await makeRoot({ 'trade-documents.md': '无关内容' })
    const rows = [{ uid: 'ec-1#0', injections: INJ }, { uid: 'kr-1#0' }]
    const r = await rehearseInjections(rows, [root])
    expect(r.get('ec-1#0')?.ok).toBe(false)
    expect(r.get('ec-1#0')?.reason).toBe('injection-key-missed')
    expect(r.has('kr-1#0')).toBe(false)
  })
})

describe('Profile 新字段校验', () => {
  it('isolated 必须给 skills_roots 与 skills_inventory', () => {
    expect(() => parseProfile('id: a\nskills_mode: isolated\n')).toThrow(/skills_roots/)
    expect(() => parseProfile('id: a\nskills_mode: isolated\nskills_roots: [D:/s]\n')).toThrow(/skills_inventory/)
    const ok = parseProfile('id: a\nskills_mode: isolated\nskills_roots: [D:/s]\nskills_inventory: inv.yaml\nbudget:\n  max_tokens: 1000\n')
    expect(ok.skills_mode).toBe('isolated')
    expect(ok.budget?.max_tokens).toBe(1000)
  })
  it('未知 skills_mode 报错；default-roots 为缺省', () => {
    expect(() => parseProfile('id: a\nskills_mode: xxx\n')).toThrow(/skills_mode/)
    expect(parseProfile('id: a\n').skills_mode).toBe('default-roots')
  })

  it('bundled-copy 需要 skills_roots，不需要 inventory', () => {
    expect(() => parseProfile('id: a\nskills_mode: bundled-copy\n')).toThrow(/skills_roots/)
    const ok = parseProfile('id: a\nskills_mode: bundled-copy\nskills_roots: [D:/eval-skills/base]\n')
    expect(ok.skills_mode).toBe('bundled-copy')
    expect(ok.skills_roots).toEqual(['D:/eval-skills/base'])
  })
  it('bundled-copy 只允许单个技能根（多份技能用 isolated）', () => {
    expect(() => parseProfile('id: a\nskills_mode: bundled-copy\nskills_roots: [D:/s0, D:/s1]\n'))
      .toThrow(/单个技能根/)
  })
})

describe('锚点与 trials 提升（01 §9）', () => {
  const rows = parseEvalset(
    [
      { uid: 'kr-1#0', case_id: 'kr-1', variant_index: 0, evalset_version: 'v', dim: '知识召回', topic: 't', turns: ['q'] },
    ].map((r) => JSON.stringify(r)).join('\n'),
  )
  const p = parseProfile('id: a\nrouting:\n  t: [s]\n')
  it('锚点 trials 取 max(N,2) 且带 ⚓ 标记', () => {
    const plan = planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1, anchors: ['kr-1#0'] })
    expect(plan.episodes).toHaveLength(2)
    expect(plan.episodes.every((e) => e.anchor === true)).toBe(true)
  })
  it('非锚点不受影响', () => {
    const plan = planEpisodes(rows, p, ALL, 'x', { modes: ['natural'], trials: 1 })
    expect(plan.episodes).toHaveLength(1)
    expect(plan.episodes[0]?.anchor).toBeUndefined()
  })
})

describe('环境快照（01 §5）', () => {
  it('键齐全且稳定；skills_base 随基底内容变化', async () => {
    const root = await makeRoot({ 'x.md': 'v1' })
    const prof = parseProfile(`id: a\nskills_mode: isolated\nskills_roots: [${root.replace(/\\/g, '/')}] \nskills_inventory: nope.yaml\n`)
    const s1 = await computeSnapshot(prof, 'e.jsonl', 'sha', { name: 'dsh-cli', version: '1' })
    expect(s1.evalset).toEqual({ file: 'e.jsonl', sha256: 'sha' })
    expect(s1.skills_mode).toBe('isolated')
    expect(s1.eline_profile.method).toBe('files')
    const s2 = await computeSnapshot(prof, 'e.jsonl', 'sha', { name: 'dsh-cli', version: '1' })
    expect(s2.skills_base).toBe(s1.skills_base)
    await writeFile(join(root, 'x.md'), 'v2', 'utf8')
    const s3 = await computeSnapshot(prof, 'e.jsonl', 'sha', { name: 'dsh-cli', version: '1' })
    expect(s3.skills_base).not.toBe(s1.skills_base)
    expect(s3.skills_base_stale).toBeUndefined()
  })

  it('导出清单与实体根一致时不标过期，根被改动后标记 skills_base_stale', async () => {
    const root = await makeRoot({ 'x.md': 'v1' })
    const prof = parseProfile(`id: a\nskills_mode: bundled-copy\nskills_roots: [${root.replace(/\\/g, '/')}]\n`)
    await writeFile(`${root}.manifest.json`, JSON.stringify({ sha256: await hashTree([root]) }), 'utf8')
    const ok = await computeSnapshot(prof, 'e.jsonl', 'sha', { name: 'dsh-cli', version: '1' })
    expect(ok.skills_base_stale).toBeUndefined()
    await writeFile(join(root, 'x.md'), 'v2', 'utf8')
    const stale = await computeSnapshot(prof, 'e.jsonl', 'sha', { name: 'dsh-cli', version: '1' })
    expect(stale.skills_base_stale).toBe(true)
  })
})

describe('schema 2 编译行兼容', () => {
  it('turns 对象与字符串两种形态归一', () => {
    const rows = parseEvalset(
      JSON.stringify({ uid: 'a#0', case_id: 'a', variant_index: 0, evalset_version: 'v', dim: '知识召回', turns: [{ role: 'user', content: '问题' }] }) + '\n' +
      JSON.stringify({ uid: 'b#0', case_id: 'b', variant_index: 0, evalset_version: 'v', dim: '知识召回', turns: ['旧格式'] }),
    )
    expect(rows.map(userTurnsOf)).toEqual([['问题'], ['旧格式']])
  })
})

describe('isolated 链路', () => {
  it('DshAdapter 把 skillsDirs 写进 overlay（skill-filesystem 段）', async () => {
    const { DshAdapter } = await import('../src/adapters/dsh-cli.js')
    const prof = parseProfile(`id: t
dsh:
  bin: node ${resolve(__dirname, 'fake-dsh.mjs')}
`)
    const adapter = new (DshAdapter as unknown as new (p: unknown) => { open(c: import('../src/ports.js').SessionContext): Promise<unknown> })(prof)
    const dir = await mkdtemp(join(tmpdir(), 'ov-'))
    await adapter.open({ runDir: dir, workspace: join(dir, 'ws'), timeoutMs: 1000, skillsDirs: [join(dir, 'skills', '0')] })
    const overlay = await readFile(join(dir, 'overlay.patch.yml'), 'utf8')
    expect(overlay).toContain("name: '@deepseek-ai/dsh-session-persistence-jsonl'")
    expect(overlay).toContain("name: '@deepseek-ai/dsh-skill-filesystem'")
    expect(overlay).toContain('includeDefaultRoots: false')
  })
})

describe('会话模型覆盖（model / reasoning_effort）', () => {
  it('model 与 reasoning_effort 需成对设置，model_provider 可选', () => {
    expect(() => parseProfile('id: a\nmodel: glm-5.3\n')).toThrow(/reasoning_effort/)
    expect(() => parseProfile('id: a\nreasoning_effort: high\n')).toThrow(/model/)
    const ok = parseProfile('id: a\nmodel: glm-5.3\nreasoning_effort: high\n')
    expect(ok.model).toBe('glm-5.3')
    expect(ok.reasoning_effort).toBe('high')
    expect(ok.model_provider).toBeUndefined()
    expect(parseProfile('id: a\nmodel: glm-5.3\nreasoning_effort: high\nmodel_provider: p\n').model_provider).toBe('p')
    expect(() => parseProfile('id: a\nmodel: ""\n')).toThrow(/model/)
  })
  it('buildModelOverlayEntries：未设置不生成；设置时产出完整 agent-default-model 条目', () => {
    expect(buildModelOverlayEntries({})).toBe('')
    expect(buildModelOverlayEntries({ model_provider: 'p' })).toBe('')
    const y = buildModelOverlayEntries({ model: 'glm-4.6', reasoning_effort: 'low' })
    expect(y).toContain('- id: agent-default-model')
    expect(y).toContain("name: '@deepseek-ai/dsh-agent-default-model'")
    expect(y).toContain('provider: deepseek-account')
    expect(y).toContain('model: "glm-4.6"')
    expect(y).toContain('reasoningEffort: "low"')
    expect(buildModelOverlayEntries({ model: 'm', reasoning_effort: 'h', model_provider: 'p' })).toContain('provider: p')
  })
  it('DshAdapter 把模型覆盖写进 overlay', async () => {
    const { DshAdapter } = await import('../src/adapters/dsh-cli.js')
    const prof = parseProfile(`id: t
model: glm-5.3
reasoning_effort: high
dsh:
  bin: node ${resolve(__dirname, 'fake-dsh.mjs')}
`)
    const adapter = new (DshAdapter as unknown as new (p: unknown) => { open(c: import('../src/ports.js').SessionContext): Promise<unknown> })(prof)
    const dir = await mkdtemp(join(tmpdir(), 'mov-'))
    await adapter.open({ runDir: dir, workspace: join(dir, 'ws'), timeoutMs: 1000 })
    const overlay = await readFile(join(dir, 'overlay.patch.yml'), 'utf8')
    expect(overlay).toContain('- id: agent-default-model')
    expect(overlay).toContain('model: "glm-5.3"')
    expect(overlay).toContain('reasoningEffort: "high"')
  })
})
