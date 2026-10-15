import type { Engine, Register } from 'claude-code'

// 이 대화의 상태를 맥의 ~/JARVIS/claude-board/inbox/ 로 보낸다.
// 설정(id·title·project)은 모드 폴더의 board.json. 없으면 아무것도 하지 않는다.
// 확인 필요(needs) = 작업 도중 막힌 경우만: 권한 승인·폴더 허용·선택지 창·오류·중단. 답이 끝나면 완료(done).
type Cfg = { id: string; title?: string; project?: string; off?: boolean }
type Status = 'working' | 'needs' | 'done' | 'ended'
type Extra = { note?: string; reason?: string; error?: boolean }

const ASK_TOOLS = /request_folder_access|request_access|request_delete_permission|request_full_control|AskUserQuestion/
const OWN_TOOL = /device_commit_files/

let current = ''

function clip(t: string, n = 60) {
  const s = t
    .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}

function lines(t: string) {
  return t.split('\n').map(l => l.trim()).filter(l => l && !/^(💡|```|Sources|출처)/.test(l))
}

function plain(l: string) {
  return l.replace(/^[#>*\-\s\d.]+/, '').replace(/\*\*/g, '').replace(/`/g, '')
}

function shortTool(name: string) {
  const parts = name.split('__')
  return parts[parts.length - 1] || name
}

// 권한 창에 뜬 도구를 사람이 읽기 쉬운 말로
function describe(tool: string, input: unknown) {
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  const host = (u: unknown) => {
    const m = /^https?:\/\/([^/?#]+)/.exec(String(u ?? ''))
    return m ? m[1] : ''
  }
  if (tool === 'WebFetch') return clip(`웹페이지 가져오기 ${host(i.url)}`.trim())
  if (tool === 'WebSearch') return clip(`웹 검색 ${String(i.query ?? '')}`.trim())
  if (tool === 'Bash') return clip(`명령 실행: ${String(i.command ?? '')}`)
  if (/^(Write|Edit|NotebookEdit)$/.test(tool)) return clip(`파일 수정: ${String(i.file_path ?? '')}`)
  if (/request_folder_access/.test(tool)) return '맥 폴더 접근 허용'
  if (/request_delete_permission/.test(tool)) return '맥 파일 삭제 허용'
  if (/computer|request_access|request_full_control/.test(tool)) return '컴퓨터 조작 허용'
  if (/claude-in-chrome|Claude_Browser/.test(tool)) return clip(`브라우저 조작: ${shortTool(tool)}`)
  if (tool === 'AskUserQuestion') return '선택지 질문'
  return clip(shortTool(tool))
}

// 답이 끝나면 언제나 완료. 다음 단계를 묻는 질문으로 끝나도 완료이고, 메모에 그 질문을 보여 준다.
function judge(answer: string): { status: Status; extra: Extra } {
  const ls = lines(answer)
  const last = ls[ls.length - 1] ?? ''
  const isQuestion = /[?？]$/.test(last.replace(/[*_`"')\]」』\s]+$/, ''))
  return { status: 'done', extra: { note: clip(plain(isQuestion ? last : (ls[0] ?? '응답 완료'))) } }
}

function send($: Engine, status: Status, extra: Extra) {
  current = status
  $.clock.after(0, () => {
    void post($, status, extra)
  })
}

async function post($: Engine, status: Status, extra: Extra) {
  try {
    const cfg = JSON.parse(await $.fs.read(`${$.plugin.root}/board.json`)) as Cfg
    if (!cfg.id || cfg.off) return
    const name = `${cfg.id}-${Date.now()}.json`
    const staged = `/mnt/user-data/outputs/board/${name}`
    const body = { id: cfg.id, title: cfg.title ?? '', project: cfg.project || cfg.title || '', status, ...extra }
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
      const note = clip(e.text || '')
      send($, 'working', note ? { note } : {})
    }
    return r
  })

  // 권한 승인 창이 뜰 때
  on('classic.PermissionRequest', async ($, e, next) => {
    if (!(e as { agent_id?: string }).agent_id) {
      send($, 'needs', { reason: '권한 승인', note: describe(String(e.tool_name), e.tool_input) })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // 폴더 접근·컴퓨터 사용 허락·선택지 질문처럼 사용자 응답을 기다리는 도구, 그리고 승인 뒤 다시 일할 때
  on('tool.call', async ($, e, next) => {
    const tool = String(e.tool)
    const mine = !e.agentId && !OWN_TOOL.test(tool)
    if (mine && ASK_TOOLS.test(tool)) {
      send($, 'needs', { reason: /AskUserQuestion/.test(tool) ? '선택 필요' : '권한 승인', note: describe(tool, e) })
    }
    const r = await next(e)
    if (mine && current === 'needs') send($, 'working', {})
    return r
  }).catch(($, e, next) => next(e))

  // 권한 판정이 "사용자에게 묻기"로 나오면(웹페이지 가져오기 확인 창 등) 확인 필요
  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    const tool = String(e.tool)
    if (r.decision === 'ask' && e.tool_use_id && !e.agentId && !OWN_TOOL.test(tool) && current !== 'needs') {
      send($, 'needs', { reason: '권한 승인', note: describe(tool, e.input) })
    }
    return r
  }).catch(($, e, next) => next(e))

  // 대화가 끝나면(지운 대화 등) 카드를 정리하도록 알림. 프로세스가 바로 꺼지면 못 보낼 수도 있다.
  on('session.end', async ($, e, next) => {
    const r = await next(e)
    await post($, 'ended', {})
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) {
      if (e.isAborted) send($, 'needs', { reason: '중단됨', note: '응답 도중 멈췄어요' })
      else if (e.reason !== 'answer') send($, 'needs', { reason: '오류', note: '오류로 멈췄어요', error: true })
      else {
        const j = judge(e.answer)
        send($, j.status, j.extra)
      }
    }
    return r
  })
}
