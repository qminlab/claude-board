---
name: "board"
description: "지금 대화를 아이패드 Claude 대기판에 이름과 함께 띄우고, 대화가 이어지는 동안 작업중·확인 필요·완료 상태를 실시간으로 기록할 때 사용. '/board 이름', '/board 업데이트', '대기판에 띄워줘', '대기판에서 내려줘'."
---

# Claude 대기판에 이 대화 띄우기

사용자의 맥에서 대기판 서버가 `~/JARVIS/claude-board/inbox/` 폴더를 지켜보고 있다. 이 폴더에 상태 파일을 넣으면 아이패드 대기판에 카드가 뜨고 옮겨진다.

이 스킬은 대화에 **자동 기록 장치(board-live 모드)**를 붙인다. 장치는 이 대화에서 일어난 일(턴 시작, 권한 판정, 도구 실행 끝, 턴 끝)을 그대로 맥으로 보내기만 하고, 어느 칸에 둘지는 **맥의 대기판 서버(server.py)가 정한다**. 그래서 상태 규칙을 바꿀 때는 맥의 `~/JARVIS/claude-board/server.py`만 고치면 모든 대화에 바로 적용되고(서버는 파일이 바뀌면 스스로 다시 시작한다), 대화마다 `/board 업데이트`를 할 필요가 없다. 서버는 턴 시작에 `working`, 답이 끝나면 `done`, 작업 도중 권한 승인 창·폴더 허용·선택지 창이 뜨거나 오류·중단이 나면 `needs`로 둔다(앱에서 미리 "항상 허용"해 둔 도구는 창 없이 바로 실행되므로 `needs`로 올리지 않는다). 사용자가 장치를 켜지 않으면 3절대로 Claude가 매 턴 직접 기록한다.

**상태 기준** (사용자가 정함)
- `needs`(확인 필요): 작업 도중 사용자가 처리해야 진행되는 경우만. 권한 요청, 폴더·컴퓨터 접근 허용, 선택지 창, 오류, 중단.
- `done`(완료): 답이 끝난 모든 경우. 할 일을 마치고 다음 단계를 묻는 질문으로 끝나도, 사용자가 생성 버튼 등을 직접 눌러야 하는 안내로 끝나도 완료다.

**중요**: 장치 파일(`board-live/` 아래 아무 파일)을 쓰거나 고친 턴은, 턴이 끝날 때 장치가 새로 켜지면서 그 턴의 완료 신호가 빠진다. 그런 턴(등록·업데이트·이름 바꾸기)은 **최종 답변 바로 전, 맨 마지막 동작으로 2절 방법으로 `done`을 직접 기록**한다. 이걸 빠뜨리면 카드가 작업중에 멈춰 있는다.

## 1. 등록 (스킬을 부른 그 턴, 순서대로)

1. **이름**: 사용자가 스킬과 함께 적은 이름을 그대로 쓴다(예: `/board 애니연출보정`). 이름이 없으면 한 번만 묻는다.
2. **id**: 영문 소문자와 숫자로 8자짜리 무작위 id를 하나 정한다. 이 대화가 끝날 때까지 바꾸지 않는다.
3. **맥 폴더 연결**: `get_device_info`의 connectedFolders에 `~/JARVIS/claude-board`(또는 그 상위 `~/JARVIS`)가 없으면 `device_request_folder_access`로 `~/JARVIS/claude-board` 접근을 요청한다. 맥 도구가 없으면 `enable__mcp__remote-devices__`로 시작하는 도구를 먼저 부른다. 맥과 연결되지 않은 대화(폰에서 연 대화 등)이거나 거절되면 "이 대화는 맥과 연결돼 있지 않아서 대기판에 띄울 수 없어요"라고 한 줄로 알리고 끝낸다.
4. **자동 기록 장치 붙이기**:
   1. Skill 도구로 `plugin-authoring` 스킬을 부른다. 스킬 본문의 "WHERE TO WRITE IT"에 이 대화의 모드 폴더 경로(`/root/.claude/dev-mods/<세션 id>/`)가 나온다. 스킬의 다른 안내(테스트 작성, 공유 등)는 따르지 않는다.
   2. 그 폴더 아래 `board-live/`에 아래 4개 파일을 Write 도구로 그대로 만든다(이미 있으면 덮어써서 최신 장치로 바꾼다). `board.json`에만 이번 id·이름을 넣는다.
   3. 사용자에게 알린다: "곧 'Enable hot reloading for this session?' 창이 뜨면 Enable for this session을 선택하면, 이 대화는 자동으로 대기판에 기록돼요."
5. **이번 턴은 직접 기록**: 함께 온 작업이 있으면 시작 전에 `working`을 기록한다. 그리고 **최종 답변 바로 전, 맨 마지막 동작으로 반드시 `done`**(note: 결과 한 줄, 등록만이면 "대기판 등록")을 2절 방법으로 기록한다.
6. 사용자에게 한 줄: "대기판에 '이름'으로 띄웠어요. (id: xxxxxxxx)". 그다음 함께 온 작업을 이어서 한다(그 작업을 끝낸 뒤 5번의 `done`).

### 장치 파일

`board-live/.claude-plugin/plugin.json`
```json
{ "name": "board-live", "version": "1.2.0", "description": "이 대화에서 일어난 일을 맥의 Claude 대기판으로 보냄 (판단은 맥 서버가 함)" }
```

`board-live/hooks/hooks.json`
```json
{ "modules": ["./register.ts"] }
```

`board-live/board.json` (id·이름만 바꿔서)
```json
{ "id": "<id>", "title": "<이름>", "project": "<이름>" }
```

`board-live/hooks/register.ts` (그대로)
```ts
import type { EngineInterface as Engine, Register } from 'claude-code'

// 이 대화에서 일어난 일을 그대로 맥의 ~/JARVIS/claude-board/inbox/ 로 보낸다.
// 어느 칸(작업중·확인 필요·완료)에 둘지는 맥의 대기판 서버가 정한다.
// 그래서 규칙이 바뀌어도 이 파일은 그대로 두고 맥의 server.py만 고치면 모든 대화에 적용된다.
// 설정(id·title·project)은 모드 폴더의 board.json. 없거나 off면 아무것도 보내지 않는다.
// 1.1: 승인 판정의 자세한 내용과 실제 실행 시간도 보냄.
// 1.2: 턴마다 turnId와, 턴이 끝날 때 아직 돌고 있는 백그라운드 에이전트 수도 보냄.
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

// 실행이 끝나면 실제 실행 시간(승인 기다린 시간 제외)도 보냄 → 맥 서버가 "미리 허용된 도구"인지 정확히 앎
type Finished = { tool_name?: string; tool_use_id?: string; duration_ms?: number; agent_id?: string }
function finished($: Engine, e: Finished) {
  const id = String(e.tool_use_id ?? '')
  const tool = String(e.tool_name ?? '')
  if (e.agent_id || !id || !(asked.has(id) || asked.has(`tool:${tool}`))) return
  asked.delete(id)
  asked.delete(`tool:${tool}`)
  send($, 'ran', { use: id, tool, run_ms: typeof e.duration_ms === 'number' ? e.duration_ms : null })
}

// 턴이 끝나도 아직 돌고 있는 백그라운드 에이전트(메인 대화가 띄운 것) 수
async function busyAgents($: Engine) {
  try {
    const list = await $.agent.list()
    return list.filter(a => !a.parentId && (a.status === 'running' || a.status === 'pending' || a.status === 'waiting')).length
  } catch {
    return 0
  }
}

export const register: Register = on => {
  on('turn.start', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) {
      asked.clear()
      send($, 'start', { text: cut(e.text, 300), turn: e.turnId })
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
      const v = r as { decider?: string; rule?: string; hook?: string; reason?: string }
      send($, 'ask', {
        use: e.tool_use_id, tool, input: pick(e.input),
        decider: v.decider ?? null, rule: v.rule ?? null, hook: v.hook ?? null, why: cut(v.reason, 120),
      })
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

  on('classic.PostToolUse', async ($, e, next) => {
    finished($, e as Finished)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('classic.PostToolUseFailure', async ($, e, next) => {
    finished($, e as Finished)
    return next(e)
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
      const bg = await busyAgents($)
      send($, 'end', { kind, head: text.slice(0, 600), tail: text.slice(-600), turn: e.turnId, bg })
    }
    return r
  })
}
```

## 2. 직접 기록하는 법 (도구 호출 2번)

1. `/mnt/user-data/outputs/board/<id>-<현재 시각 밀리초>.json`에 파일을 쓴다. **매번 새 파일 이름**이어야 한다. 같은 경로를 다시 쓰면 맥에 예전 내용이 올라갈 수 있다.
2. `device_commit_files`로 그 파일을 `~/JARVIS/claude-board/inbox/<같은 파일 이름>`에 올린다.

```json
{"id": "<id>", "title": "<이름>", "project": "<이름>", "status": "working|needs|done|remove", "reason": "<needs일 때만>", "note": "<한 줄>"}
```

올린 뒤 확인 조회는 하지 않는다. 실패해도 작업은 계속하고 실패는 한 번만 알린다.

## 3. 장치가 켜지지 않은 대화에서만: 매 턴 직접 기록

"declined"나 "off" 알림을 받았거나 장치를 붙이지 못했으면, 이 대화가 끝날 때까지 매 턴 직접 기록한다.
- 턴 시작: 도구를 쓰는 작업이면 첫 작업 도구 전에 `working`.
- 작업 도중 권한·접근 허용·선택지를 기다리게 되면 그 도구를 부르기 전에 `needs`(reason: "권한 승인"·"선택 필요", note: 무엇), 응답을 받고 이어서 일하면 `working`.
- 턴 끝: 최종 답변 직전에 반드시 `done`(note: 결과 한 줄, 질문으로 끝나면 그 질문). 오류로 멈췄으면 `needs`(reason: "오류").

장치가 켜진 대화("enabled" 알림)에서는 직접 기록하지 않는다. 단, 위 **중요**에 적힌 대로 장치 파일을 고친 턴은 예외다.

기록할 때마다 대화에서 그 사실을 말하지 않는다.

## 4. 업데이트·이름 바꾸기·내리기

- "/board 업데이트": 이미 등록된 대화의 장치를 최신으로 바꾼다. `plugin-authoring` 스킬을 불러 모드 폴더를 확인한다. `board-live/.claude-plugin/plugin.json`의 version이 이미 1.2.0 이상이면, 규칙은 맥 서버에 있으므로 "이 대화는 이미 최신 장치예요"라고 한 줄로 알리고 아무 파일도 바꾸지 않는다(완료 기록도 따로 하지 않는다). 그보다 낮으면 `board.json`은 그대로 둔 채 나머지 장치 파일 3개만 위 내용으로 덮어쓴다. `board.json`이 없으면 1절 등록을 처음부터 한다. 파일을 바꿨으면 최종 답변 바로 전, 맨 마지막 동작으로 2절 방법으로 `done`(note: "대기판 장치 업데이트")을 기록하고, 사용자에게 "대기판 장치를 최신으로 바꿨어요. 앞으로는 이 대화에서 다시 업데이트할 필요 없어요"라고 한 줄로 알린다. Enable 창이 다시 뜨면 눌러 달라고 덧붙인다.
- 대기판에 보이는 이름은 아이패드 연결 탭의 "이름" 버튼으로도 바꿀 수 있다(그 이름이 대화 쪽 이름보다 우선한다).
- "대기판 이름 ○○로 바꿔줘": `board.json`의 title과 project를 바꾸고, 최종 답변 바로 전 맨 마지막 동작으로 2절 방법으로 `done`을 새 이름으로 기록한다.
- "대기판에서 내려줘", "/board 끝": 2절 방법으로 `{"status": "remove", "disconnect": true}`를 기록하고, `board.json`에 `"off": true`를 넣는다. 이후 이 대화에서는 기록하지 않는다.
- 아이패드에서 카드를 길게 누르거나 연결 탭에서 끊으면, 그 id의 신호는 대기판이 더 이상 받지 않는다. 다시 "/board 이름"을 부르면 새 id로 처음부터 등록한다(장치 파일도 최신으로 덮어쓴다).