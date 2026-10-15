import type { EngineInterface as Engine, Register } from 'claude-code'

// 이 대화에서 일어난 일을 그대로 맥의 ~/JARVIS/claude-board/inbox/ 로 보낸다.
// 어느 칸(작업중·확인 필요·완료)에 둘지는 맥의 대기판 서버가 정한다.
// 그래서 규칙이 바뀌어도 이 파일은 그대로 두고 맥의 server.py만 고치면 모든 대화에 적용된다.
// 설정(id·title·project)은 모드 폴더의 board.json. 없거나 off면 아무것도 보내지 않는다.
type Cfg = { id: string; title?: string; project?: string; off?: boolean }
type Signal = Record<string, unknown>

const ASK_TOOLS = /request_folder_access|request_access|request_delete_permission|request_full_control|AskUserQuestion/
const OWN_TOOL = /device_commit_files/
const INPUT_KEYS = ['url', 'query', 'command', 'file_path', 'description']

const asked = new Set<string>()

function cut(v: unknown, n: number) {
  const s = String(v ?? '')
  return s.length <= n ? s : s.slice(0, n)
}

// 권한 창 설명에 쓸 만한 인자만 짧게
function pick(input: unknown) {
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  const out: Record<string, string> = {}
  for (const k of INPUT_KEYS) if (i[k] != null) out[k] = cut(i[k], 200)
  return out
}

function send($: Engine, ev: string, extra: Signal = {}) {
  const ts = Date.now()
  $.clock.after(0, () => {
    void post($, { ev, ts, ...extra })
  })
}

async function post($: Engine, signal: Signal) {
  try {
    const cfg = JSON.parse(await $.fs.read(`${$.plugin.root}/board.json`)) as Cfg
    if (!cfg.id || cfg.off) return
    const name = `${cfg.id}-${String(signal.ts ?? Date.now())}-${String(signal.ev)}.json`
    const staged = `/mnt/user-data/outputs/board/${name}`
    const body = { id: cfg.id, title: cfg.title ?? '', project: cfg.project || cfg.title || '', ...signal }
    await $.fs.write(staged, JSON.stringify(body))
    await $.tool.call({
      tool: 'mcp__remote-devices__device_commit_files',
      files: [{ stagedPath: staged, devicePath: `~/JARVIS/claude-board/inbox/${name}` }],
    } as never)
  } catch {
    // 대기판 기록 실패는 대화를 방해하지 않는다
  }
}

export const register: Register = on => {
  on('turn.start', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) {
      asked.clear()
      send($, 'start', { text: cut(e.text, 300) })
    }
    return r
  })

  on('classic.PermissionRequest', async ($, e, next) => {
    const tool = String(e.tool_name)
    if (!(e as { agent_id?: string }).agent_id && !OWN_TOOL.test(tool)) {
      asked.add(`tool:${tool}`)
      send($, 'ask', { tool, input: pick(e.tool_input) })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    const tool = String(e.tool)
    if (r.decision === 'ask' && e.tool_use_id && !e.agentId && !OWN_TOOL.test(tool)) {
      asked.add(e.tool_use_id)
      send($, 'ask', { use: e.tool_use_id, tool, input: pick(e.input) })
    }
    return r
  }).catch(($, e, next) => next(e))

  on('tool.call', async ($, e, next) => {
    const tool = String(e.tool)
    const id = e.tool_use_id || ''
    const mine = !e.agentId && !OWN_TOOL.test(tool)
    if (mine && ASK_TOOLS.test(tool) && id) {
      asked.add(id)
      send($, /AskUserQuestion/.test(tool) ? 'question' : 'ask', { use: id, tool, input: pick(e) })
    }
    try {
      return await next(e)
    } finally {
      if (mine && (asked.has(id) || asked.has(`tool:${tool}`))) {
        asked.delete(id)
        asked.delete(`tool:${tool}`)
        send($, 'ran', { use: id, tool })
      }
    }
  }).catch(($, e, next) => next(e))

  on('session.end', async ($, e, next) => {
    const r = await next(e)
    await post($, { ev: 'ended', ts: Date.now() })
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) {
      asked.clear()
      const kind = e.isAborted ? 'aborted' : e.reason !== 'answer' ? 'error' : 'answer'
      const text = kind === 'answer' ? String(e.answer ?? '') : ''
      send($, 'end', { kind, head: text.slice(0, 600), tail: text.slice(-600) })
    }
    return r
  })
}
