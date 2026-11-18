import { writeFile } from "node:fs/promises"
import { Plugin } from "@opencode/plugin"

const signal = process.env.LINGLING_SWITCH_FILE
const limit = process.env.LINGLING_FREE_LIMIT || ""
const name = process.env.LINGLING_PLUGIN_WALL || "lingling.wall"
const events = (process.env.LINGLING_FAIL_EVENTS || "")
  .split(",")
  .map((item) => item.trim())
  .filter(Boolean)

type Held = {
  sessionID: string
  messageID: string
  message: string
  retryAfterMs: number
}

export default Plugin.define({
  id: name,
  async setup(ctx) {
    if (!events.length) return () => {}

    const signaled = new Set<string>()
    const controller = new AbortController()

    function wall(data: any): Held | null {
      const error = data?.error || {}
      const sessionID = String(data?.sessionID || "")

      if (!sessionID || error.type !== limit) return null

      return {
        sessionID,
        messageID: String(data?.assistantMessageID || data?.messageID || ""),
        message: String(error.message || error.type || ""),
        retryAfterMs: Number(error.retryAfterMs) || 0,
      }
    }

    function send(found: Held) {
      if (!signal) return
      if (found.messageID && signaled.has(found.messageID)) return
      if (found.messageID) signaled.add(found.messageID)

      const item = {
        at: Date.now(),
        retryAfterMs: found.retryAfterMs,
        sessionID: found.sessionID,
        messageID: found.messageID,
        message: found.message,
      }

      writeFile(signal, JSON.stringify(item)).catch(() => {})
    }

    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({
          types: events,
          signal: controller.signal,
        })) {
          const found = wall(event.data || {})
          if (found) send(found)
        }
      } catch {
        // stream closed
      }
    })()

    return () => controller.abort()
  },
})
