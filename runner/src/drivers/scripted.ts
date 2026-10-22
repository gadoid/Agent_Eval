import type { TurnRecord } from '../domain.js'
import type { UserDriver } from '../ports.js'

/** 按用例脚本逐条给出用户消息；只统计实际发送的轮次（不含预置历史） */
export class ScriptedDriver implements UserDriver {
  constructor(private readonly messages: readonly string[]) {}

  next(history: readonly TurnRecord[]): string | null {
    const sent = history.filter((t) => t.source === 'live').length
    return this.messages[sent] ?? null
  }
}
