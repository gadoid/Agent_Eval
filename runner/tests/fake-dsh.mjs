#!/usr/bin/env node
// 假 dsh：按真实命令行与日志格式工作，用于不依赖模型的端到端测试。
//   dsh --version
//   dsh --profile <p> --patch <overlay> [--session-id <id>] "<prompt>"
// 新会话在 overlay 指定的落盘根下写 session-<uuid>/session.v4.jsonl.zstd（header 帧 + 事件帧）；
// --session-id 续接时向同一文件追加一帧，回答中带上第 1 轮的用户消息，模拟上下文记忆。
// prompt 中的标记控制行为：
//   /<skill>     模拟用户显式调用：写一条注入事件，不调用 skill 工具
//   舱单 / 融资  模拟模型主动调用 skill 工具加载 order-intake
//   [badskill]   调用不存在的 skill，工具结果为错误
//   [hang]       永不退出（测试超时）
//   [crash]      不写日志，退出码 3
//   [incomplete] 回合以 max-steps 结束
//   [plain]      新会话写未压缩的 session.jsonl
//   [noid]       新会话目录不带 session- 前缀、header 不带 id（会话 id 无法确定）
import { existsSync, mkdirSync, readFileSync, writeFileSync, appendFileSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { zstdCompressSync } from 'node:zlib'

const argv = process.argv.slice(2)
if (argv[0] === '--version') {
  console.log('fake-dsh 0.2.1-test')
  process.exit(0)
}
const flag = (name) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : undefined)
const overlay = readFileSync(flag('--patch'), 'utf8')
const root = JSON.parse(/root: (".*")/.exec(overlay)[1])
const resumeId = flag('--session-id')
const prompt = argv[argv.length - 1]

if (prompt.includes('[hang]')) setInterval(() => {}, 1000)
else main()

function main() {
  if (prompt.includes('[crash]')) {
    process.stderr.write('boom\n')
    process.exit(3)
  }

  let dir
  let state
  if (resumeId) {
    dir = join(root, resumeId)
    if (!existsSync(join(dir, 'state.json'))) {
      process.stderr.write(`dsh: session ${resumeId} not found\n`)
      process.exit(1)
    }
    state = JSON.parse(readFileSync(join(dir, 'state.json'), 'utf8'))
  } else {
    const uuid = randomUUID()
    const noid = prompt.includes('[noid]')
    const id = noid ? `raw-${uuid}` : `session-${uuid}`
    dir = join(root, id)
    mkdirSync(dir, { recursive: true })
    // 记录逐会话环境（bundled-copy 模式 e2e 断言 DSH_BUNDLED_SKILL_DIR 用；overlay 供模型覆盖断言用）
    state = { id, seq: 0, users: [], plain: prompt.includes('[plain]'), noid, bundledEnv: process.env.DSH_BUNDLED_SKILL_DIR ?? null, overlay }
  }

  const frame = (type, data) => JSON.stringify({ type, seq: state.seq++, time: new Date().toISOString(), data })
  const events = [frame('user/message', { message: { content: [{ type: 'text', text: prompt }] } })]

  const forced = [...prompt.matchAll(/(?:^|\s)\/([a-z0-9-]+)(?=\s|$)/g)].map((m) => m[1])
  for (const name of forced) {
    events.push(frame('context/inject', { source: 'skill-invocation', message: { content: [{ type: 'text', text: `<skill_content name="${name}">forced body</skill_content>` }] } }))
  }
  const call = (name, callId, ok) => {
    events.push(frame('tool/call', { name: 'skill', callId, arguments: JSON.stringify({ name }) }))
    const text = ok ? `<skill_content name="${name}">\n<skill_instructions>进单资料审核规则……</skill_instructions>\n</skill_content>` : `Unknown skill: ${name}`
    events.push(frame('tool/result', { callId, message: { content: [{ type: 'tool-result', toolCallId: callId, isError: !ok, content: [{ type: 'text', text }] }] } }))
    events.push(frame('step/end', {}))
  }
  if (forced.length === 0 && /舱单|融资/.test(prompt)) call('order-intake', `c${String(state.seq)}`, true)
  if (prompt.includes('[badskill]')) call('nope', `c${String(state.seq)}`, false)

  const context = state.users.length > 0 ? `（上下文：${state.users[0]}）` : ''
  events.push(frame('assistant/message', { message: { content: [{ type: 'text', text: `回答：${prompt}${context}` }] }, usage: { inputTokens: 120, outputTokens: 30, reasoningTokens: 5 } }))
  events.push(frame('step/end', {}))
  events.push(frame('turn/end', { reason: { kind: prompt.includes('[incomplete]') ? 'max-steps' : 'completed' } }))
  state.users.push(prompt)

  const header = JSON.stringify(state.noid ? { type: 'session', delegationDepth: 0 } : { type: 'session', id: state.id, delegationDepth: 0, cwd: process.cwd() })
  const body = `${events.join('\n')}\n`
  if (state.plain) {
    const file = join(dir, 'session.jsonl')
    if (resumeId) appendFileSync(file, body)
    else writeFileSync(file, `${header}\n${body}`)
  } else {
    const file = join(dir, 'session.v4.jsonl.zstd')
    if (resumeId) appendFileSync(file, zstdCompressSync(Buffer.from(body)))
    else writeFileSync(file, Buffer.concat([zstdCompressSync(Buffer.from(`${header}\n`)), zstdCompressSync(Buffer.from(body))]))
  }
  writeFileSync(join(dir, 'state.json'), JSON.stringify(state))
  process.exit(0)
}
