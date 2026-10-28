import { describe, expect, it } from 'vitest'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Writable } from 'node:stream'
import { parseProfile } from '../src/profile.ts'
import { loadEvalset, loadMethod, planEpisodes } from '../src/planner.ts'
import type { EvalRow } from '../src/domain.ts'

/** 测法执行（doc/15 §3.10 P2）：不给 --modes 时按快照测法规划。 */
class Sink extends Writable {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  _write(_c: any, _e: any, cb: () => void) { cb() }
}

const DSH_CLI_CAPABILITIES = { multiTurn: true, prefillHistory: true }

const row = (over: Partial<EvalRow> = {}): EvalRow => ({
  uid: 'kr-1#0', case_id: 'kr-1', variant_index: 0, evalset_version: 'v1',
  dim: '知识召回', turns: ['甲？'], ...over,
} as EvalRow)

describe('测法执行（P2）', () => {
  it('loadMethod 读 sidecar；无 sidecar 的旧 dist 返回 null', async () => {
    const tmp = await mkdtemp(join(tmpdir(), 'm-'))
    const dist = join(tmp, 'evalset-v1.jsonl')
    await writeFile(dist, '{"uid":"a"}\n', 'utf8')
    expect(await loadMethod(dist)).toBeNull()
    await writeFile(join(tmp, 'evalset-v1.snapshot.json'), JSON.stringify({
      method_sha: 'ab12', method: { modes: { default: 'natural' }, run_rewrites: ['口语'], anchors: [] },
    }), 'utf8')
    const pack = await loadMethod(dist)
    expect(pack?.methodSha).toBe('ab12')
    expect(pack?.method.modes.default).toBe('natural')
  })

  it('按测法：ec 维度用 forced、其他维度 natural；run_rewrites 外的变体不执行', () => {
    const profile = parseProfile('id: p\nskills_mode: default-roots\nrouting:\n  进单资料审核: [s1]\n')
    const rows = [
      row(),
      row({ uid: 'kr-1#1', variant_index: 1, rewrite: '口语', turns: ['口语变体'] }),
      row({ uid: 'kr-1#2', variant_index: 1, rewrite: '诱导', turns: ['诱导变体'] }),
      row({ uid: 'ec-1#0', case_id: 'ec-1', dim: '错误知识纠正', topic: '进单资料审核' } as Partial<EvalRow>),
    ]
    const method = { modes: { default: 'natural', 错误知识纠正: 'forced' },
                     run_rewrites: ['口语'], anchors: [] }
    // 隔离检查看 options.skillsMode（cli 从 profile 透传），这里同步传 bundled-copy
    const isoProfile = parseProfile('id: p\nskills_mode: bundled-copy\nskills_roots: [r1]\nrouting:\n  进单资料审核: [s1]\n')
    const plan = planEpisodes(rows, isoProfile, DSH_CLI_CAPABILITIES, 'dsh-cli', {
      modes: ['natural'], trials: 1, method, skillsMode: 'bundled-copy',
    })
    const modes = new Map(plan.episodes.map((e) => [e.row.uid, e.mode]))
    expect(modes.get('kr-1#0')).toBe('natural')          // 测法 default
    expect(modes.get('ec-1#0')).toBe('forced')           // 测法 ec=forced
    expect(modes.has('kr-1#1')).toBe(true)               // 口语在 run_rewrites
    expect(modes.has('kr-1#2')).toBe(false)              // 诱导不在 run_rewrites → 不执行
    expect(plan.skipped.some((s) => s.uid === 'kr-1#2' && s.reason?.includes('run_rewrites'))).toBe(true)
  })

  it('无测法（旧 dist）回退 options.modes', () => {
    const profile = parseProfile('id: p\nskills_mode: default-roots\nrouting:\n  进单资料审核: [s1]\n')
    const plan = planEpisodes([row({ topic: '进单资料审核' } as Partial<EvalRow>)], profile,
      DSH_CLI_CAPABILITIES, 'dsh-cli', { modes: ['natural', 'forced'], trials: 1 })
    expect(plan.episodes.map((e) => e.mode).sort()).toEqual(['forced', 'natural'])
  })

  it('快照头行被 loadEvalset 跳过并解析出 header', async () => {
    const tmp = await mkdtemp(join(tmpdir(), 'h-'))
    const dist = join(tmp, 'e.jsonl')
    await writeFile(dist,
      '{"snapshot": {"version": "vX", "method_sha": "ab12", "include": "structured"}}\n' +
      JSON.stringify(row()) + '\n', 'utf8')
    const { rows, header } = await loadEvalset(dist)
    expect(rows).toHaveLength(1)
    expect(header?.version).toBe('vX')
  })
})
