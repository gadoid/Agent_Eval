import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { collectFromFile } from '../src/vendor/collector.js'
import { extractLoadedSkills, parseSessionHeader, toolsFingerprint } from '../src/probes/index.js'
import { decodeZstdLog } from '../src/vendor/zstd.js'

/** 递归找 fixture 里唯一的 session.v4.jsonl.zstd（昨日 field-verify-eline 真实 forced 会话） */
function findZstd(dir: string): string {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      const found = findZstd(p)
      if (found) return found
    } else if (name.endsWith('.zstd')) {
      return p
    }
  }
  throw new Error(`no .zstd under ${dir}`)
}

const FIXTURE = resolve(__dirname, '..', '..', 'tests', 'fixtures', 'forced-episode')

describe('真实日志回归（dsh 0.2.0-rc.2 / eline，2026-10-21 field-verify-eline forced 会话）', () => {
  it('tool/result 的 message.content 直接是 text 块时，skill 加载判定为成功且能取内容指纹', async () => {
    const trace = await collectFromFile(findZstd(FIXTURE))
    // 该会话模型主动加载了 trade-finance 与 trade-finance-policy，结果均含 <skill_content>
    const loaded = extractLoadedSkills(trace.toolCalls, trace.toolResults, 'skill')
    const names = loaded.map((s) => `${s.name}:${s.ok ? 'ok' : 'fail'}`).sort()
    expect(names).toEqual(['trade-finance-policy:ok', 'trade-finance:ok'])
    // 成功加载可取内容 sha256（episode 级指纹依赖）
    for (const s of loaded) expect(s.sha256).toBeTruthy()
  })

  it('采集到最终回答与用量（日志切分与 usage 口径不变）', async () => {
    const trace = await collectFromFile(findZstd(FIXTURE))
    expect(trace.finalText.length).toBeGreaterThan(50)
    expect(trace.tokens.total).toBeGreaterThan(0)
    expect(trace.turnEnd).toBe('completed')
  })

  it('header 观测：解析 24 个生效工具与系统提示（doc/18 §2.2）', () => {
    const text = decodeZstdLog(readFileSync(findZstd(FIXTURE)))
    const header = parseSessionHeader(text)
    expect(header).not.toBeNull()
    expect(header!.tools.length).toBe(24)
    expect(header!.tools.map((t) => t.name)).toContain('web_search')
    expect(header!.systemPrompt.length).toBeGreaterThan(0)
    const fp = toolsFingerprint(header!.tools)
    expect(fp.names.length).toBe(24)                     // 名称去重排序
    expect(fp.sha256).toHaveLength(64)                   // (name, description, parameters) 排序哈希
    // 无 header 帧的文本返回 null；只有 system/message 时工具为空清单
    expect(parseSessionHeader('{"type":"user/message","data":{}}')).toBeNull()
    const sysOnly = parseSessionHeader('{"type":"system/message","data":{"message":"sys"}}')
    expect(sysOnly!.tools).toEqual([]) && expect(sysOnly!.systemPrompt).toBe('sys')
  })
})
