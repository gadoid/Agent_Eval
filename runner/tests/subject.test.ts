import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { subjectOf, subjectSection, canonicalJson } from '../src/snapshot.js'
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
