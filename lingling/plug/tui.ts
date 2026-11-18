import { existsSync, readFileSync, renameSync, unlinkSync } from "node:fs"
import { Plugin } from "@opencode/plugin/tui"

type Ask = {
  sessionID: string
  size?: number
  error?: string
}

type Model = {
  providerID: string
  id: string
  variant?: string
}

type ToastVariant = "info" | "success" | "warning" | "error"

const env = process.env
const MS = 1000
const file = env.LINGLING_RECOVER_FILE || ""
const name = env.LINGLING_NAME || "Lingling"
const poll = Number(env.LINGLING_RECOVER_POLL) * MS
const toastTime = Number(env.LINGLING_TOAST)
const waitTime = Number(env.LINGLING_RESCUE_WAIT) * MS
const summaryPoll = Number(env.LINGLING_RESCUE_POLL) * MS
const summaryCheck = Number(env.LINGLING_RESCUE_CHECK) * MS
const seenCap = Number(env.LINGLING_RESCUE_SEEN)
const tries = Number(env.LINGLING_RESCUE_TRIES)
const thousand = Number(env.LINGLING_TOKENS_K)
const claimed = env.LINGLING_CLAIMED_SUFFIX || ""
const freeSuffix = env.LINGLING_FREE_SUFFIX || ""
const rescueID = env.LINGLING_PLUGIN_RESCUE || "lingling.rescue"
const markers = (env.LINGLING_LIMIT_MARKERS || "")
  .split(",")
  .map((item) => item.trim().toLowerCase())
  .filter(Boolean)

const SUMMARY_TEMPLATE = `You MUST use this format for your response (you may omit sections that aren't applicable). Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Requirements
- [constraints, preferences, requirements, and scope boundaries stated by the user, or "(none)"]

## Decisions
- [decisions already made and why, or "(none)"]

## Work State
Break the objective into smaller goals and report which are completed, which are being worked on, and which are blocked.
### Completed
- [goals that have been completed; otherwise "(none)"]

### Active
- [goals currently being worked on; otherwise "(none)"]

### Blocked
- [anything blocking progress, and why; otherwise "(none)"]

## Next Move
1. [ordered list of next actions, or "(none)"]

## Relevant Files
List the files and directories, other than the current working directory, that another agent would need to open to continue this work. Include at most 15, most important first. Do not list every file that was read or changed. Include paths outside the current working directory when relevant. If none, write "(none)".
- \`[file or directory path]\`: [brief reason it matters]

## Important Context
- [facts the next agent cannot continue without and cannot easily find on its own; or "(none)"]
</template>`

const SUMMARY_RULES = `Rules:
- Keep each section concise. Use terse, single-line bullets, not prose paragraphs or nested lists.
- Prefer short references over detailed restatement. It is fine to leave out information the next agent can recover from the code or the files listed above.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers.
- Carry forward only user questions or requests that remain unanswered or require further action. Do not repeat ones that newer history has answered or resolved. Preserve exact wording when carrying one forward.
- Preserve consequential workflow state, including whether changes are uncommitted, committed, pushed, under review, or merged.
- Do not mention the summary process or that context was compacted.`

const SUMMARY_PROMPT = [
  "You MUST summarize the conversation above into a structured summary that will be given to another agent to resume the work.",
  "Summarize only what the user and the assistant said and did. Leave out instructions and setup the assistant was given rather than told by the user: repository conventions, instruction files such as AGENTS.md, and environment details like the session ID. The next agent receives current versions of all of these separately.",
  SUMMARY_TEMPLATE,
  SUMMARY_RULES,
  "Do not continue the task or call tools.",
  "Return only the structured summary in the requested format. Do not include a preamble, explanation, or other commentary.",
].join("\n\n")

const NUDGE =
  "The previous response did not fill in the required summary template. Do not call tools. Return the summary as text using the exact section headings from the template."

function take(): Ask | null {
  if (!file || !claimed || !existsSync(file)) return null

  const held = file + claimed

  try {
    renameSync(file, held)
  } catch {
    return null
  }

  try {
    const data = JSON.parse(readFileSync(held, "utf8"))
    if (!data || typeof data.sessionID !== "string" || !data.sessionID) return null
    return data
  } catch {
    return null
  } finally {
    try {
      unlinkSync(held)
    } catch {
      // then gone
    }
  }
}

function idOf(value: any): string {
  return value?.id || value?.session?.id || value?.data?.id || value?.info?.id || ""
}

function partsOf(item: any): any[] {
  if (Array.isArray(item)) return item
  if (Array.isArray(item?.content)) return item.content
  if (Array.isArray(item?.parts)) return item.parts
  if (Array.isArray(item?.data?.parts)) return item.data.parts
  if (Array.isArray(item?.info?.parts)) return item.info.parts
  return []
}

function show(value: any): string {
  if (value === undefined || value === null) return ""
  if (typeof value === "string") return value

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function outputText(value: any): string {
  if (!Array.isArray(value)) return show(value)

  return value.map((item: any) => {
    if (item?.type === "text") return String(item.text || "")
    if (item?.type === "file") {
      return "[file] " + String(item.name || item.filename || item.mime || "")
    }
    return show(item)
  }).filter(Boolean).join("\n")
}

function partText(part: any): string {
  const type = String(part?.type || "")

  if (type === "text") return String(part.text || "")
  if (type === "reasoning") return ""

  if (type === "tool") {
    const state = part.state || {}
    const input = show(state.input)
    const output = outputText(state.content ?? state.output ?? state.error)

    return "[tool] " + (part.name || part.tool || "tool")
      + (input ? "\ninput:\n" + input : "")
      + (output ? "\noutput:\n" + output : "")
  }

  if (type === "file") {
    return "[file] " + String(part.path || part.name || part.url || "")
  }

  if (type === "patch") {
    return "[patch]\n" + String(part.text || part.patch || "")
  }

  if (type === "subtask") {
    return "[subtask] " + String(part.description || part.prompt || "")
  }

  if (type === "step-start" || type === "step-finish" || type === "snapshot") {
    return ""
  }

  if (typeof part?.text === "string") return part.text

  return show(part)
}

function textOf(item: any): string {
  return partsOf(item).map(partText).filter(Boolean).join("\n").trim()
}

function plainTextOf(item: any): string {
  const parts = partsOf(item).filter((part) => part?.type === "text")
  return parts.map((part) => String(part.text || "")).join("\n").trim()
}

function roleOf(info: any): string {
  return String(info?.role || info?.type || "")
}

function errorOf(value: any): string {
  const error = value?.error || value
  return String(error?.message || error?.data?.message || error?.name || "").trim()
}

function isLimit(value: any): boolean {
  const text = (errorOf(value) || String(value) || show(value)).toLowerCase()
  return markers.some((marker) => text.includes(marker))
}

function answerText(items: any[]): string {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index]
    const info = item?.info || item || {}

    if (roleOf(info) !== "assistant") continue

    const text = plainTextOf(item)
    if (text) return text
  }

  return ""
}

function failureOf(items: any[]): string {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const info = items[index]?.info || items[index] || {}
    if (roleOf(info) !== "assistant") continue

    const error = info.error
    if (!error) return ""

    return errorOf(error) || "summary failed"
  }

  return ""
}

function completedOf(item: any): boolean {
  const info = item?.info || item || {}
  return Boolean(info?.time?.completed || info?.finish)
}

function transcriptOf(items: any[]): string {
  const out: string[] = []

  for (const item of items) {
    const info = item?.info || item || {}
    const role = roleOf(info) || "message"
    const text = textOf(item) || String(info.text || info.summary || "").trim()
    const error = info.error || info.retry?.error || item?.error
    const errorText = error ? "[error]\n" + errorOf(error) : ""
    const body = [text, errorText].filter(Boolean).join("\n")

    if (!body) continue

    out.push("### " + role + "\n" + body)
  }

  return out.join("\n\n")
}

async function messagesOf(context: any, sessionID: string): Promise<any[]> {
  const read = context.client?.session?.context

  if (typeof read === "function") {
    try {
      const result = await read({ sessionID })
      const data = result?.data ?? result
      const items = Array.isArray(data)
        ? data
        : Array.isArray(data?.messages)
          ? data.messages
          : []
      if (items.length) return items
    } catch {
      // then cache
    }
  }

  try {
    await context.data?.session?.message?.sync?.(sessionID)
  } catch {
    // then cached
  }

  try {
    return context.data?.session?.message?.list?.(sessionID) || []
  } catch {
    return []
  }
}

function locationOf(context: any): any {
  if (context.location) return context.location

  try {
    return context.data.location.default()
  } catch {
    return undefined
  }
}

function limitOf(item: any): number {
  return Number(item?.limit?.context || item?.limit?.input || 0)
}

function isFree(item: any): boolean {
  const id = String(item?.id || item?.modelID || "")
  const raw = item?.cost
  const cost = Array.isArray(raw) ? raw : raw ? [raw] : []

  return Boolean(id) && ((freeSuffix !== "" && id.endsWith(freeSuffix))
    || (cost.length > 0 && cost.every((entry: any) =>
      Number(entry?.input || 0) === 0
      && Number(entry?.output || 0) === 0)))
}

function providerOf(item: any): string {
  return String(item?.providerID || item?.provider?.id || "")
}

function modelOf(item: any): Model {
  const model: Model = {
    providerID: providerOf(item),
    id: String(item?.id || item?.modelID || ""),
  }

  if (typeof item?.variant === "string" && item.variant) {
    model.variant = item.variant
  }

  return model
}

function chosenModel(context: any): Model | undefined {
  const current = context.ui?.model?.current?.()
  if (!current) return undefined

  const model = modelOf(current)
  return model.providerID && model.id ? model : undefined
}

function lastModel(items: any[]): Model | undefined {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const info = items[index]?.info || items[index] || {}

    if (roleOf(info) !== "assistant") continue

    const held = info.model && typeof info.model === "object" ? info.model : info
    const model = modelOf({
      providerID: held.providerID || info.providerID,
      modelID: held.modelID || held.id || info.modelID,
      variant: held.variant || info.variant,
    })

    if (model.providerID && model.id) return model
  }

  return undefined
}

async function pickModel(context: any): Promise<Model | undefined> {
  const location = locationOf(context)
  const cache = context.data?.location?.model

  if (cache?.sync) {
    try {
      await cache.sync(location)
    } catch {
      // then cached
    }
  }

  let models: any[] = []

  if (cache?.list) {
    try {
      models = cache.list(location) || []
    } catch {
      models = []
    }
  }

  const free = models.filter((item) => item?.status !== "deprecated"
    && providerOf(item) && isFree(item))

  free.sort((left, right) => limitOf(right) - limitOf(left))

  if (free[0]) return modelOf(free[0])

  return undefined
}

async function createSession(
  context: any,
  title: string,
  model?: Model,
): Promise<string> {
  const input: any = { title }
  const location = locationOf(context)

  if (location) input.location = location
  if (model) input.model = model

  const made = await context.client.session.create(input)
  const id = idOf(made)

  if (!id) throw new Error("new session failed")
  return id
}

async function promptSession(
  context: any,
  sessionID: string,
  text: string,
): Promise<any> {
  return context.client.session.prompt({ sessionID, text })
}

async function waitForSummary(context: any, sessionID: string): Promise<string> {
  const start = Date.now()
  let running = false
  let checked = start

  while (Date.now() - start < waitTime) {
    let status = ""
    try {
      status = context.data?.session?.status?.(sessionID) || ""
    } catch {
      status = ""
    }

    if (status === "running") running = true
    if (running && status === "idle") break

    if (Date.now() - checked >= summaryCheck) {
      const list = await messagesOf(context, sessionID)
      const failure = failureOf(list)
      if (failure) throw new Error(failure)

      const done = list.filter((item) => {
        const info = item?.info || item || {}
        return roleOf(info) === "assistant" && completedOf(item) && plainTextOf(item)
      })

      if (done.length) return plainTextOf(done[done.length - 1])
      checked = Date.now()
    }

    await new Promise((resolve) => setTimeout(resolve, summaryPoll))
  }

  await new Promise((resolve) => setTimeout(resolve, summaryPoll))

  const list = await messagesOf(context, sessionID)
  const failure = failureOf(list)
  if (failure) throw new Error(failure)

  const text = answerText(list)
  if (text) return text

  throw new Error("summary timed out")
}

async function waitForFailure(
  context: any,
  sessionID: string,
  ms: number,
): Promise<string> {
  const start = Date.now()

  while (Date.now() - start < ms) {
    const failure = failureOf(await messagesOf(context, sessionID))
    if (failure) return failure
    await new Promise((resolve) => setTimeout(resolve, summaryPoll))
  }

  return ""
}

async function stopSession(context: any, sessionID: string): Promise<void> {
  const session = context.client?.session
  if (!session) return

  try {
    await session.interrupt({ sessionID })
    return
  } catch {
    // then abort
  }

  try {
    await session.abort({ sessionID })
  } catch {
    // then continue
  }
}

async function removeSession(context: any, sessionID: string): Promise<void> {
  try {
    await context.client.session.remove({ sessionID })
  } catch {
    // then keep
  }
}

function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: any

  const limit = new Promise<T>((_, reject) => {
    timer = setTimeout(() => reject(new Error("summary timed out")), ms)
  })

  return Promise.race([work, limit]).finally(() => clearTimeout(timer))
}

function hasSummary(text: string): boolean {
  return /(^|\n)##\s+Objective\b/.test(text)
}

function withError(summary: string, error: string): string {
  const text = String(error || "").replace(/\s+/g, " ").trim()
  if (!text || summary.includes(text)) return summary

  const line = "- Triggering error: " + text
  const heading = "## Important Context"
  const at = summary.indexOf(heading)

  if (at < 0) return summary.trimEnd() + "\n\n" + heading + "\n" + line

  const end = summary.indexOf("\n", at)
  if (end < 0) return summary + "\n" + line

  return summary.slice(0, end + 1) + line + "\n" + summary.slice(end + 1)
}

function summaryPrompt(transcript: string, error: string): string {
  const body = error && !transcript.includes(error)
    ? transcript + "\n\n### error\n" + error
    : transcript

  return "Conversation:\n" + body + "\n\n" + SUMMARY_PROMPT
}

function handoffPrompt(transcript: string): string {
  return "Continue this conversation in this new session. "
    + "Do not summarize it or start over. Here is the current context:\n\n"
    + transcript
    + "\n\nContinue from the latest user request."
}

export default Plugin.define({
  id: rescueID,
  async setup(context) {
    if (!file || !claimed || !Number.isFinite(poll)) return () => {}

    const [, update] = context.storage.store("go-upsell", {
      initial: { dontShow: false, lastSeenAt: 0 },
    })

    await update((draft) => {
      draft.dontShow = true
      draft.lastSeenAt = Date.now()
    })

    const seen = new Set<string>()
    let busy = false

    const toast = (message: string, variant: ToastVariant) => {
      try {
        context.ui.toast.show({
          title: name,
          message,
          variant,
          duration: toastTime,
        })
      } catch {
        // then quiet
      }
    }

    const timer = setInterval(async () => {
      if (busy) return

      const ask = take()

      if (!ask) return

      if (seen.has(ask.sessionID)) return

      if (seen.size > seenCap) seen.clear()
      seen.add(ask.sessionID)

      busy = true
      let tempID = ""
      let nextID = ""
      const error = String(ask.error || "").trim()

      try {
        const userModel = chosenModel(context)
        await stopSession(context, ask.sessionID)

        const list = await messagesOf(context, ask.sessionID)
        const transcript = transcriptOf(list)

        if (!transcript) throw new Error("empty chat")

        const size = Number(ask.size) || 0
        const human = size ? Math.round(size / thousand) + "k" : ""
        const at = human ? " at " + human + " tokens" : ""
        const go = await context.ui.dialog.confirm({
          title: name + " rescue",
          message:
            "The free model stopped this session" + at + ". "
            + "Open a fresh session with the full context? "
            + "If that is rejected too, Lingling will summarize as a backup. "
            + "The old chat stays unchanged.",
          label: { confirm: "Continue", cancel: "Not now" },
        })

        if (!go) return

        const model = userModel || lastModel(list)

        nextID = await createSession(context, name + " handoff", model)
        seen.add(nextID)

        try {
          await withTimeout(
            promptSession(context, nextID, handoffPrompt(transcript)),
            waitTime,
          )
          const failure = await waitForFailure(context, nextID, summaryCheck)
          if (failure) throw new Error(failure)

          toast("New session ready", "success")

          try {
            context.ui.router.navigate({ type: "session", sessionID: nextID })
          } catch {
            // then stay
          }

          return
        } catch {
          toast("Full context handoff failed; summarizing as a backup...", "info")
          await removeSession(context, nextID)
          nextID = ""
        }

        const summaryModel = await pickModel(context)
        if (!summaryModel) throw new Error("no free model")

        toast("Summarizing with " + summaryModel.id + "...", "info")

        let summary = ""
        let summaryTries = 0

        while (!summary) {
          tempID = await createSession(context, name + " summary", summaryModel)
          seen.add(tempID)

          try {
            await withTimeout(
              promptSession(context, tempID, summaryPrompt(transcript, error)),
              waitTime,
            )

            summary = await waitForSummary(context, tempID)

            if (!hasSummary(summary)) {
              await withTimeout(promptSession(context, tempID, NUDGE), waitTime)
              summary = await waitForSummary(context, tempID)
            }

            if (!summary) throw new Error("empty summary")
            if (!hasSummary(summary)) throw new Error("summary format")
          } catch (err) {
            if (!isLimit(err) || summaryTries + 1 >= tries) throw err

            summary = ""
            summaryTries += 1
            toast("Summary lane hit the limit; trying a fresh address...", "info")
            await new Promise((resolve) => setTimeout(resolve, poll))
            await removeSession(context, tempID)
            tempID = ""
          }
        }

        summary = withError(summary, error)

        await removeSession(context, tempID)
        tempID = ""

        let handoffTries = 0

        while (true) {
          nextID = await createSession(context, name + " handoff", model)

          try {
            await withTimeout(promptSession(context, nextID, summary), waitTime)
            const failure = await waitForFailure(context, nextID, summaryCheck)
            if (!failure) break
            throw new Error(failure)
          } catch (err) {
            if (!isLimit(err) || handoffTries + 1 >= tries) throw err

            handoffTries += 1
            toast("Handoff lane hit the limit; trying a fresh address...", "info")
            await new Promise((resolve) => setTimeout(resolve, poll))
            await removeSession(context, nextID)
            nextID = ""
          }
        }

        toast("New session ready", "success")

        try {
          context.ui.router.navigate({ type: "session", sessionID: nextID })
        } catch {
          // then stay
        }
      } catch (err) {
        const text = err instanceof Error ? err.message : String(err)
        toast("Rescue failed: " + text + ". Old chat is unchanged.", "error")
        seen.delete(ask.sessionID)

        for (const id of [tempID, nextID]) {
          if (id) await removeSession(context, id)
        }
      } finally {
        busy = false
      }
    }, poll)

    return () => clearInterval(timer)
  },
})
