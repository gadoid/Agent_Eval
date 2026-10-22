#!/usr/bin/env node
/**
 * 本地 dsh 客户端一键接入（dsh 与 ElineAI 兼容适配，doc/07 §6.1）。
 *
 * 通用 DeepSeek Harness（如 D:\dsh）：
 *   node tools/setup-dsh-local.mjs --dsh D:/dsh
 *   ① 定位主 exe 与 asar 内 CLI（bin 支持带引号的空格路径）
 *   ② 用 --from-default-profile headless 创建评测 profile（默认 evalheadless）
 *   ③ 写账号态 patch（dsh-headless 默认禁用 authorization/deepseek-account/credentials，
 *      需按 id 重启用并指向 deepseek-account provider，走桌面端登录态，无需 API key）
 *   ④ 探测 CLI 版本，生成 runner/profile.dsh-local.yaml
 *
 * ElineAI 客户端（asar 内含产品 trade skill 包）：检测到后不自动改写，按 doc/07 §6.1
 * 用 runner/profile-templates/evalheadless/ 模板恢复，Profile 用 profile.default-roots.yaml。
 */
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync, statSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function die(msg) {
  console.error(`setup-dsh-local: ${msg}`)
  process.exit(1)
}

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 ? process.argv[i + 1] : def
}

const dshDir = resolve(arg('dsh', '') || '')
const home = resolve(arg('home', '') || join(homedir(), '.dsh'))
const profileName = arg('profile', 'evalheadless')
const outYaml = resolve(REPO, arg('out', join('runner', 'profile.dsh-local.yaml')))
const force = process.argv.includes('--force')

if (!dshDir || !existsSync(dshDir)) die('缺少 --dsh <客户端目录>（如 D:/dsh）')

// ① 主 exe：目录下体积最大的 exe（排除卸载器/提权工具）
const exes = readdirSync(dshDir).filter((f) => f.endsWith('.exe') && !/uninstall|elevate/i.test(f))
  .map((f) => ({ f, size: statSync(join(dshDir, f)).size })).sort((a, b) => b.size - a.size)
if (!exes.length) die(`${dshDir} 下没有 exe`)
const exe = join(dshDir, exes[0].f)

const asar = join(dshDir, 'resources', 'app.asar')
if (!existsSync(asar)) die(`找不到 ${asar}`)
const CLI_REL = 'dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'

// asar 头部是全量文件索引：只读前 8MB 判定客户端类型与 CLI 是否在册
// （索引里存包内相对路径 node_modules/@deepseek-ai/...，不带 dsh/ 挂载前缀）
const head = readFileSync(asar).subarray(0, 8 * 1024 * 1024)
const isEline = head.includes('dsh-web-app/skills')
if (!head.includes('@deepseek-ai/dsh-desktop-host/lib/cli.js')) {
  die(`asar 内未找到 dsh-desktop-host/lib/cli.js（客户端结构未知，手工确认后重试）`)
}
const cli = `${asar}/${CLI_REL}`

const binEnv = { ...process.env, ELECTRON_RUN_AS_NODE: '1', DSH_HOME: home }

function runCli(args) {
  return spawnSync(`"${exe}"`, args, { shell: true, env: binEnv, encoding: 'utf8', timeout: 120_000 })
}

// ④ 版本探测（兼容性冒烟）
const ver = runCli(['--expose-internals', cli, '--version'])
const version = (ver.stdout || '').trim().split('\n')[0] || '?'
if (ver.status !== 0) die(`CLI 版本探测失败：${(ver.stderr || '').slice(-300)}`)

if (isEline) {
  console.log(`检测到 ElineAI 客户端（asar 含产品 trade skill 包），CLI ${version}。`)
  console.log('ElineAI 链路不走本脚本的账号态 patch：按 doc/07 §6.1 从 runner/profile-templates/evalheadless/ 恢复 profile，')
  console.log('评测 Profile 使用 runner/profile.default-roots.yaml（DSH_BUNDLED_SKILL_DIR 挂产品 skill）。')
  console.log('参考 bin 写法（带引号）：')
  console.log(`  bin: '"${exe}" --expose-internals ${cli}'`)
  process.exit(0)
}

// ② 创建评测 profile（--from-default-profile headless；boot 因无任务报错属预期，目录已生成）
const patchPath = join(home, 'profiles', profileName, 'cordis.patch.yml')
if (existsSync(patchPath) && !force) {
  console.log(`profile ${profileName} 已存在，跳过创建（--force 覆盖）：${patchPath}`)
} else {
  runCli(['--expose-internals', cli, profileName, '--from-default-profile', 'headless'])
  if (!existsSync(patchPath)) die(`创建 profile 失败：${patchPath} 未生成`)
  console.log(`已创建 profile ${profileName}（headless 模板）`)
}

// ③ 账号态 patch：重启用被 dsh-headless 禁用的三条目 + deepseek-account provider
const patch = `# eval 评测链路的 patch 层（tools/setup-dsh-local.mjs 生成）：走 deepseek-account 账号态（非 API key）。
# dsh-headless bundle 默认禁用 authorization / deepseek-account / credentials 三条目
# （headless 设计为 API-key 鉴权），此处按 id 重新启用，把桌面端登录态接入 CLI。
- id: authorization
  name: "@deepseek-ai/dsh-authorization"
  disabled: false
- id: deepseek-account
  name: "@deepseek-ai/dsh-deepseek-account-platform"
  disabled: false
  config:
    desktopPlatform: ${process.platform === 'darwin' ? 'darwin' : 'win32'}
- id: credentials
  name: "@deepseek-ai/dsh-credentials-local"
  disabled: false
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-account
    model: deepseek-flash
    reasoningEffort: high
`
writeFileSync(patchPath, patch, 'utf8')
console.log(`已写账号态 patch：${patchPath}`)

// ⑤ 生成 runner Profile（bin 带引号，支持 exe 路径含空格）
if (existsSync(outYaml) && !force) {
  console.log(`已存在 ${outYaml}，不覆盖（--force 覆盖）`)
} else {
  writeFileSync(outYaml, `# 本地 dsh 客户端链路（tools/setup-dsh-local.mjs 生成）：账号态（deepseek-account）。
id: dsh-local-account
dsh:
  bin: '"${exe}" --expose-internals ${cli}'
  profile: ${profileName}
  home: ${home.replaceAll('\\', '/')}
  env:
    ELECTRON_RUN_AS_NODE: "1"
  timeout_ms: 600000
skills_mode: default-roots
skill_tool: skill
routing: {}
`, 'utf8')
  console.log(`已生成 Profile：${outYaml}`)
}

console.log(`\nCLI ${version} 接入完成。下一步（runner/ 下）：`)
console.log(`  node lib/cli.js run --cases ../evalset/cases/order_intake --profile ${outYaml.replaceAll('\\', '/').split('/').pop()} \\`)
console.log(`      --out out/<name> --modes natural --trials 1 --only kr-0201#0 --dry-run`)
console.log('去掉 --dry-run 真跑；打分：python -m scoring score runner/out/<name>（仓库根）。')
