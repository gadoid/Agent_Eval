import type { Mode } from '../domain.js'
import type { Intervention } from '../ports.js'
import { buildForcedPrompt, forcedSkillsFor } from '../profile.js'

/** 不干预：原样发送 */
export const natural: Intervention = {
  mode: 'natural',
  plan: () => ({ ok: true, forcedSkills: [] }),
  transform: (message) => message,
}

/**
 * 强制加载：按 Profile.routing 把用例的业务主题映射到 skill，
 * 在第一条实际发送的消息前加 /skill名，让 skill 内容从对话开始就在上下文中。
 */
export const forced: Intervention = {
  mode: 'forced',
  plan(row, profile) {
    const skills = forcedSkillsFor(row.topic, profile)
    if (skills) return { ok: true, forcedSkills: skills }
    return {
      ok: false,
      reason: row.topic ? `Profile.routing 中没有主题「${row.topic}」` : '用例没有 topic，无法确定要强制加载的 skill',
    }
  },
  transform: (message, liveIndex, plan) => (liveIndex === 0 ? buildForcedPrompt(message, plan.forcedSkills) : message),
}

export const INTERVENTIONS: Record<Mode, Intervention> = { natural, forced }
