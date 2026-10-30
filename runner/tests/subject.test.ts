import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { canonicalJson, cordisIdentity, subjectOf, subjectSection } from '../src/snapshot.js'
import { sha256 } from '../src/profile.js'
import type { Profile } from '../src/domain.js'

/** 被测指纹双实现对拍（doc/15 §7.4）：与 tests/test_subject.py 共用同一 fixture。 */
const fixture = JSON.parse(
  readFileSync(join(__dirname, '..', '..', 'tests', 'fixtures', 'subject_manifest.json'), 'utf-8'),
) as {
  cases: Array<{
    name: string
    manifest: { profile: { id: string; content: Record<string, unknown> }; snapshot: Record<string, unknown> }
    ts_profile_overrides: Record<string, string>
    expected_subject_fp: string
    expected_incomplete: boolean
  }>
}

function profileOf(c: (typeof fixture)['cases'][number]): Profile {
  const content = c.manifest.profile.content as Record<string, unknown>
  const p = {
    ...(content as object),
    id: c.manifest.profile.id,
    ...(c.ts_profile_overrides as object),
  } as Profile
  return p
}

describe('subject fingerprint（与 scoring/subject.py 对拍）', () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      const snap = c.manifest.snapshot as Parameters<typeof subjectOf>[1]
      const section = subjectSection(profileOf(c), snap)
      expect(section.subject_fp).toBe(c.expected_subject_fp)
      expect(section.incomplete).toBe(c.expected_incomplete)
    })
  }
  it('canonicalJson 键排序与紧凑分隔符', () => {
    expect(canonicalJson({ b: 1, a: 'x' })).toBe('{"a":"x","b":1}')
    expect(canonicalJson({ 中文: '值' })).toBe('{"中文":"值"}')
  })
})

/** cordis 三件套 → 被测身份段（doc/18 §2.1）：临时 DSH_HOME 下建 profiles/<name>/ 三件套 */
describe('cordisIdentity（doc/18 §2.1）', () => {
  async function setup(files: Record<string, string>): Promise<Profile> {
    const home = await mkdtemp(join(tmpdir(), 'dsh-cordis-'))
    const dir = join(home, 'profiles', 'evaltest')
    await mkdir(dir, { recursive: true })
    for (const [name, text] of Object.entries(files)) await writeFile(join(dir, name), text, 'utf8')
    return { id: 't', dsh: { profile: 'evaltest', timeout_ms: 1, home },
             skills_mode: 'default-roots', skill_tool: 'skill', routing: {} } as Profile
  }
  const pkg = (bundles: string[] = ['@deepseek-ai/dsh-base']) =>
    JSON.stringify({ name: 'p', private: true, dependencies: {}, dsh: { profile: { bundles } } })
  const patch = `- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config: { provider: deepseek-account, model: glm-5.3, reasoningEffort: high }
- insert:
    - id: persona
      name: '@deepseek-ai/dsh-persona'
      config: { suffix: hello, prefix: 你好 }
- id: tool-ask-user
  name: '@deepseek-ai/dsh-tool-ask-user'
  config: { mode: timed, timeout: 60 }
`

  it('有效模型与 persona/plugins 指纹（persona=config，plugins 排除 persona/adm、含 bundles 清单）', async () => {
    const prof = await setup({ 'cordis.yml': '[]\n', 'cordis.patch.yml': patch, 'package.json': pkg() })
    const c = await cordisIdentity(prof)
    expect(c).not.toBeNull()
    expect(c!.model).toBe('glm-5.3')
    expect(c!.reasoning_effort).toBe('high')
    expect(c!.model_provider).toBe('deepseek-account')
    expect(c!.persona_from).toBe('cordis')
    expect(c!.persona_sha).toBe(sha256(canonicalJson({ persona: { suffix: 'hello', prefix: '你好' } })))
    expect(c!.plugins_sha).toBe(sha256(canonicalJson({
      entries: [{ id: 'tool-ask-user', name: '@deepseek-ai/dsh-tool-ask-user',
                  config: { mode: 'timed', timeout: 60 } }],
      bundles: ['@deepseek-ai/dsh-base'],
    })))
  })

  it('只改 persona → 只有 persona_sha 变；只改 bundles → 只有 plugins_sha 变', async () => {
    const a = await cordisIdentity(await setup({ 'cordis.patch.yml': patch, 'package.json': pkg() }))
    const b = await cordisIdentity(await setup({
      'cordis.patch.yml': patch.replace('prefix: 你好', 'prefix: 再见'), 'package.json': pkg() }))
    const d = await cordisIdentity(await setup({ 'cordis.patch.yml': patch,
                                                 'package.json': pkg(['@deepseek-ai/dsh-headless']) }))
    expect(b!.persona_sha).not.toBe(a!.persona_sha)
    expect(b!.plugins_sha).toBe(a!.plugins_sha)
    expect(b!.model).toBe(a!.model)
    expect(d!.persona_sha).toBe(a!.persona_sha)
    expect(d!.plugins_sha).not.toBe(a!.plugins_sha)
  })

  it('patch 同 id 覆盖 cordis.yml；三件套全缺 → null（旧环境不分裂）', async () => {
    const over = await cordisIdentity(await setup({
      'cordis.yml': '- id: persona\n  config: { prefix: 旧 }\n',
      'cordis.patch.yml': '- id: persona\n  config: { prefix: 新 }\n',
      'package.json': pkg([]) }))
    expect(over!.persona_sha).toBe(sha256(canonicalJson({ persona: { prefix: '新' } })))
    const none = await cordisIdentity(await setup({ 'readme.md': 'x' }))
    expect(none).toBeNull()
  })

  it('profile.prompt_plugins 缺省 [persona]，声明后按 id 取条目', async () => {
    const extra = patch + '- id: memory-recall\n  config: { topk: 5 }\n'
    const prof = await setup({ 'cordis.patch.yml': extra, 'package.json': pkg([]) })
    const onlyPersona = await cordisIdentity(prof)
    expect(onlyPersona!.persona_sha).toBe(sha256(canonicalJson({ persona: { suffix: 'hello', prefix: '你好' } })))
    const both = await cordisIdentity({ ...prof, prompt_plugins: ['persona', 'memory-recall'] } as Profile)
    expect(both!.persona_sha).toBe(sha256(canonicalJson({
      persona: { suffix: 'hello', prefix: '你好' }, 'memory-recall': { topk: 5 } })))
    // memory-recall 移出 plugins_sha
    expect(both!.plugins_sha).not.toBe(onlyPersona!.plugins_sha)
  })
})
