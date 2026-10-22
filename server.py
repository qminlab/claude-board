#!/usr/bin/env python3
"""Claude 대기판 서버 — 표준 라이브러리만 사용 (Python 3.8 이상).

상태가 들어오는 두 길:
  1) Claude Code 훅  → POST /hook
  2) Claude 앱 대화  → inbox/ 폴더에 JSON 파일 (Claude가 직접 써 넣음) 또는 POST /event

화면: 같은 와이파이의 아이패드에서 http://<맥 IP>:7777 접속
"""
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox")
STATE_FILE = os.path.join(BASE, "state.json")
BOARD_FILE = os.path.join(BASE, "board.html")
KEY_FILE = os.path.join(BASE, "key.txt")
PORT = int(os.environ.get("CLAUDE_BOARD_PORT", "7777"))          # http: 이 맥의 훅·기록용
HTTPS_PORT = int(os.environ.get("CLAUDE_BOARD_HTTPS_PORT", "7443"))  # https: 아이패드 화면용
CERT_DIR = os.path.join(BASE, "certs")
CA_KEY = os.path.join(CERT_DIR, "ca.key")
CA_CRT = os.path.join(CERT_DIR, "ca.crt")
SRV_KEY = os.path.join(CERT_DIR, "server.key")
SRV_CRT = os.path.join(CERT_DIR, "server.crt")
SRV_NAMES = os.path.join(CERT_DIR, "server.names")

DONE_TTL = 60 * 60          # 완료 카드는 1시간 뒤 화면에서만 숨김 (연결은 유지, 새 신호가 오면 다시 뜸)
STALE_TTL = 3 * 60 * 60     # 3시간 동안 아무 신호 없는 카드는 정리 (지운 대화 등)
WORKING_STALE_TTL = 6 * 60 * 60  # 작업중 카드는 긴 작업일 수 있어서 6시간까지 기다림
LINK_TTL = 30 * 24 * 60 * 60  # 연결은 시간이 지나도 끊지 않음 (30일 동안 신호가 없을 때만 목록에서 정리)
BLOCK_TTL = 7 * 24 * 60 * 60
STATUSES = ("working", "needs", "done")

R_PERMISSION = "권한 승인"
R_QUESTION = "질문에 답 필요"
R_CHOICE = "선택 필요"
R_IDLE = "입력 대기"
R_DEFAULT = "확인 필요"

LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

lock = threading.Lock()
state = {"day": "", "counter": 0, "sessions": {}, "links": {}, "blocked": {}, "names": {}}


# ---------- 접근 키 (아이패드만 화면을 보고 카드를 내릴 수 있게) ----------
def load_key():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(8)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    return key


KEY = ""


# ---------- 상태 저장 ----------
def load_state():
    global state
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            state = data
    except (OSError, ValueError):
        pass
    for k in ("links", "blocked", "names", "auto_tools", "prompt_tools"):
        if not isinstance(state.get(k), dict):
            state[k] = {}
    for sid, s in state["sessions"].items():   # 예전 상태 파일: 카드가 있으면 연결도 만듦
        state["links"].setdefault(sid, {"id": sid, "source": s.get("source", ""), "first": s.get("since", 0),
                                        "title": s.get("title", ""), "project": s.get("project", ""),
                                        "status": s.get("status", ""), "last": s.get("updated", 0)})


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def next_number():
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:          # 날짜가 바뀌면 1번부터
        state["day"] = today
        state["counter"] = 0
    state["counter"] = state.get("counter", 0) % 999 + 1
    return state["counter"]


def clip(text, n=60):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def upsert(sid, source, status, project=None, title=None, note=None, reason=None, alert=None, error=False):
    now = time.time()
    s = state["sessions"].get(sid)
    if s is None:
        s = {
            "id": sid, "no": next_number(), "source": source,
            "project": project or title or "이름 없음", "title": "", "status": status,
            "note": "", "since": now, "work_started": now, "updated": now,
        }
        state["sessions"][sid] = s
    else:
        if s["status"] != status:
            if status == "working" and s["status"] == "done":
                s["work_started"] = now      # 새 요청 → 작업 시간 다시 잼
            s["since"] = now
        if project:
            s["project"] = project
    if title:
        s["title"] = title
    if note is not None:
        s["note"] = note
    s["status"] = status
    s["updated"] = now
    s["reason"] = (reason or R_DEFAULT) if status == "needs" else ""
    s["alert"] = (alert or "") if status == "needs" else ""
    s["error"] = bool(error)
    link = state["links"].setdefault(sid, {"id": sid, "source": source, "first": now})
    link.update({"title": s["title"], "project": s["project"], "status": status, "last": now})
    link.pop("ended", None)
    s["name"] = link["name"] = state["names"].get(sid, "")


def rename(sid, name):
    """아이패드에서 정한 표시 이름. 빈 이름이면 원래 이름으로 되돌림. 대화 쪽 이름이 바뀌어도 이 이름이 우선."""
    name = clip(name, 40)
    if name:
        state["names"][sid] = name
    else:
        state["names"].pop(sid, None)
    for box in (state["sessions"], state["links"]):
        if sid in box:
            box[sid]["name"] = name


def touch(sid):
    s = state["sessions"].get(sid)
    if s:
        s["updated"] = time.time()
    if sid in state["links"]:
        state["links"][sid]["last"] = time.time()


def remove(sid):
    """카드만 내림 (연결은 유지)."""
    state["sessions"].pop(sid, None)


def disconnect(sid):
    """연결 끊기: 카드와 연결을 지우고, 이후 이 연결에서 오는 신호는 무시."""
    state["sessions"].pop(sid, None)
    state["links"].pop(sid, None)
    state["names"].pop(sid, None)
    state["blocked"][sid] = time.time()


def ended(sid):
    """대화(세션)가 끝났다는 신호: 확인 필요 카드가 아니면 카드와 연결을 정리."""
    s = state["sessions"].get(sid)
    if s is None or s["status"] != "needs":
        state["sessions"].pop(sid, None)
        state["links"].pop(sid, None)


def app_ended(sid):
    """앱 대화의 '세션 종료' 신호.

    앱 대화는 대화가 길어져 맥락을 정리하거나, 잠시 쉬는 사이 작업 환경이 다시 켜질 때도 이 신호가 와요.
    대화가 지워진 것과 구별할 수 없어서, 카드와 연결을 바로 지우지 않고 표시만 해 둬요.
    정말 지운 대화면 신호가 끊기니 오래된 카드·연결 정리(cleanup)가 알아서 치워요.
    """
    link = state["links"].get(sid)
    if link is not None:
        link["ended"] = time.time()
    print(f"세션 종료 신호: {sid} (카드·연결은 유지)", flush=True)


def cleanup():
    now = time.time()
    for sid, s in list(state["sessions"].items()):
        if s["status"] == "done" and now - s["since"] > DONE_TTL:
            remove(sid)
        elif now - s.get("updated", s["since"]) > (WORKING_STALE_TTL if s["status"] == "working" else STALE_TTL):
            remove(sid)
    for sid, link in list(state["links"].items()):
        if now - link.get("last", 0) > LINK_TTL:
            state["links"].pop(sid, None)
    for sid, ts in list(state["blocked"].items()):
        if now - ts > BLOCK_TTL:
            state["blocked"].pop(sid, None)


# ---------- Claude Code 훅 ----------
def project_from_cwd(cwd):
    """cwd가 속한 git 저장소 폴더 이름 = 프로젝트 이름. 없으면 cwd 폴더 이름."""
    if not cwd:
        return "Claude Code"
    path = os.path.abspath(cwd)
    cur = path
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return os.path.basename(cur) or cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.basename(path) or path


def describe_tool(data):
    name = data.get("tool_name") or "도구"
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    detail = ti.get("command") or ti.get("file_path") or ti.get("url") or ti.get("description") or ""
    return clip(f"{name}: {detail}" if detail else name)


def trailing_question(message):
    """답변이 질문으로 끝났으면 그 줄을 돌려줌 (완료가 아니라 내 답을 기다리는 멈춤)."""
    lines = [l.strip() for l in str(message or "").strip().splitlines() if l.strip()]
    for line in reversed(lines[-4:]):
        if line.startswith(("💡", "```", "Sources", "출처")):
            continue
        bare = line.rstrip(" *_`\"')]」』").strip()
        if bare.endswith(("?", "？")):
            return clip(line.replace("**", ""))
        return None
    return None


def classify_notification(data):
    kind = str(data.get("notification_type") or data.get("type") or "")
    msg = str(data.get("message") or "")
    low = msg.lower()
    if kind == "idle_prompt" or (not kind and "waiting for your input" in low):
        return "idle", msg
    if kind == "permission_prompt" or (not kind and "permission" in low):
        return "permission", msg
    if kind.startswith("elicitation") and kind not in ("elicitation_complete", "elicitation_response"):
        return "choice", msg
    if kind == "agent_needs_input":
        return "input", msg
    return None, msg


def handle_hook(data):
    event = data.get("hook_event_name", "")
    sid = data.get("session_id")
    if not sid or data.get("agent_id"):      # 서브에이전트 이벤트는 무시
        return
    sid = "code:" + str(sid)
    if sid in state["blocked"]:              # 대기판에서 연결을 끊은 세션
        return
    project = project_from_cwd(data.get("cwd"))
    title = clip(data.get("session_title"), 40) or None   # /rename 으로 정한 세션 이름
    cur = state["sessions"].get(sid)
    status = cur["status"] if cur else None

    if event == "UserPromptSubmit":
        upsert(sid, "code", "working", project, title, note=clip(data.get("prompt")))

    elif event == "PermissionRequest":
        upsert(sid, "code", "needs", project, title, reason=R_PERMISSION, alert=describe_tool(data))

    elif event == "Notification":
        kind, msg = classify_notification(data)
        if kind == "idle":
            # 완료된 카드는 그대로 두고, 끝났다는 신호 없이 멈춘 경우(Esc 중단 등)만 확인 필요로
            if status == "working":
                upsert(sid, "code", "needs", project, title, reason=R_IDLE, alert="완료 신호 없이 멈춰 있어요 (중단됐을 수 있어요)")
        elif kind == "permission":
            if status == "needs" and cur.get("reason") == R_PERMISSION and cur.get("alert"):
                touch(sid)                  # 이미 PermissionRequest 로 자세히 표시 중
            else:
                m = re.search(r"permission to use (.+)", msg)
                upsert(sid, "code", "needs", project, title, reason=R_PERMISSION, alert=clip(m.group(1)) if m else clip(msg))
        elif kind == "choice":
            upsert(sid, "code", "needs", project, title, reason=R_CHOICE, alert=clip(msg))
        elif kind == "input":
            upsert(sid, "code", "needs", project, title, reason=R_IDLE, alert=clip(msg))
        else:
            touch(sid)

    elif event == "PreToolUse":
        if data.get("tool_name") == "AskUserQuestion":
            ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
            qs = ti.get("questions") if isinstance(ti.get("questions"), list) else []
            q = qs[0].get("question") if qs and isinstance(qs[0], dict) else ""
            upsert(sid, "code", "needs", project, title, reason=R_QUESTION, alert=clip(q))
        elif status == "needs":
            upsert(sid, "code", "working", project, title)
        else:
            touch(sid)

    elif event in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
        if status == "needs":               # 승인(폰에서 한 것 포함) 후 다시 작업 → 작업중
            upsert(sid, "code", "working", project, title)
        else:
            touch(sid)

    elif event == "Stop":
        # 할 일을 마치고 다음 단계를 묻는 질문도 완료로 본다 (확인 필요는 권한·오류·중단만)
        q = trailing_question(data.get("last_assistant_message"))
        upsert(sid, "code", "done", project, title, note=q if q else None)

    elif event == "StopFailure":
        upsert(sid, "code", "needs", project, title, reason="오류", alert="오류로 멈췄어요", error=True)

    elif event == "SessionEnd":
        ended(sid)


# ---------- 앱 대화 (inbox 파일) / 일반 이벤트 ----------
def handle_event(data, source="app"):
    raw_id = str(data.get("id") or "").strip()
    status = str(data.get("status") or "").strip()
    if not raw_id:
        return False
    sid = source + ":" + raw_id
    if status == "remove":
        disconnect(sid) if data.get("disconnect") else remove(sid)
        return True
    if status == "ended":
        app_ended(sid) if source == "app" else ended(sid)
        return True
    if sid in state["blocked"]:              # 대기판에서 연결을 끊은 대화
        return True
    if data.get("ev"):                       # 자동 기록 장치(1.0~)가 보낸 날것 신호 → 판단은 여기서
        handle_relay(data, sid, source)
        return True
    if status not in STATUSES:
        return False
    note = data.get("note")
    deferred.pop(sid, None)
    cur = state["sessions"].get(sid)
    if (source == "app" and status == "needs" and not data.get("error")
            and cur is not None and cur["status"] == "working"):
        deferred[sid] = {"due": time.time() + GRACE, "args": {
            "project": clip(data.get("project"), 30) or None, "title": clip(data.get("title"), 40) or None,
            "reason": clip(data.get("reason"), 16) or None, "alert": clip(note)}}
        touch(sid)
        return True
    upsert(
        sid, source, status,
        project=clip(data.get("project"), 30) or None,
        title=clip(data.get("title"), 40) or None,
        note=clip(note) if note is not None and status != "needs" else None,
        reason=clip(data.get("reason"), 16) or None,
        alert=clip(note) if status == "needs" else None,
        error=bool(data.get("error")),
    )
    return True


# ---------- 앱 대화 자동 기록 장치 (board-live 1.0~) ----------
# 장치는 일어난 일만 그대로 보내고(턴 시작·권한 판정·도구 실행 끝·턴 끝), 무엇을 어느 칸에 둘지는 여기서 정한다.
# 그래서 규칙을 바꿀 때 이 파일만 고치면 모든 대화에 바로 적용된다.
GRACE = 10.0           # 같은 작업에서 이만큼 막혀 있어야 확인 필요로 옮김 (잠깐 묻고 바로 넘어가는 건 깜빡임만 생겨서 무시)
LEARN_WITHIN = GRACE   # 확인 필요로 넘어가기 전에 실행이 끝났으면 미리 허용된 도구(묶음)로 기억
LEARNED_GRACE = 180.0  # 미리 허용된 걸로 아는 도구는 이만큼 기다림 (timeout 걸린 긴 명령도 확인 필요로 안 가게)
HUMAN_WAIT = 2.5       # 장치 1.1+: 승인에 이보다 오래 걸렸으면 사람이 눌러 준 것 → 그 도구 묶음은 "물어보는 도구"
ASK_TOOLS = re.compile(r"request_folder_access|request_access|request_delete_permission|request_full_control|AskUserQuestion")
SKIP_LINE = ("💡", "```", "Sources", "출처")
rt = {}                # 대화별 진행 상황(메모리): 턴 시작 시각, 승인 대기 중인 도구 등
deferred = {}          # 직접 기록된 확인 필요(예전 장치·수동 기록)를 잠시 미뤄 둔 것


def strip_tags(text):
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", " ", str(text or ""))
    return re.sub(r"<[^>]+>", " ", text)


def friendly(tool, inp):
    """권한 창에 뜬 도구를 사람이 읽기 쉬운 말로."""
    tool = str(tool or "")
    i = inp if isinstance(inp, dict) else {}
    short = tool.split("__")[-1] or tool
    if tool == "WebFetch":
        m = re.match(r"https?://([^/?#]+)", str(i.get("url") or ""))
        return clip(f"웹페이지 가져오기 {m.group(1) if m else ''}".strip())
    if tool == "WebSearch":
        return clip(f"웹 검색 {i.get('query') or ''}".strip())
    if tool == "Bash" or tool.endswith("device_bash"):
        return clip(f"명령 실행: {i.get('command') or ''}")
    if tool in ("Write", "Edit", "NotebookEdit"):
        return clip(f"파일 수정: {i.get('file_path') or ''}")
    if "request_folder_access" in tool:
        return "맥 폴더 접근 허용"
    if "request_delete_permission" in tool:
        return "맥 파일 삭제 허용"
    if re.search(r"computer|request_access|request_full_control", tool):
        return "컴퓨터 조작 허용"
    if re.search(r"claude-in-chrome|Claude_Browser", tool):
        return clip(f"브라우저 조작: {short}")
    if tool == "AskUserQuestion":
        return "선택지 질문"
    return clip(short)


def answer_note(head, tail):
    """답이 끝났을 때 메모: 질문으로 끝났으면 그 질문, 아니면 첫 줄."""
    def lines(t):
        out = []
        for l in strip_tags(t).splitlines():
            l = l.strip()
            if l and not l.startswith(SKIP_LINE):
                out.append(re.sub(r"^[#>*\-\s\d.]+", "", l).replace("**", "").replace("`", ""))
        return [l for l in out if l]
    first = (lines(head) or ["응답 완료"])[0]
    last_lines = lines(tail)
    last = last_lines[-1] if last_lines else ""
    bare = re.sub(r"[*_`\"')\]」』\s]+$", "", last)
    return clip(last if bare.endswith(("?", "？")) else first)


def family(tool):
    """같은 묶음의 도구는 한꺼번에 기억 (브라우저 도구 하나가 미리 허용돼 있으면 나머지 브라우저 도구도 같음).
    mcp__claude-in-chrome__navigate → mcp__claude-in-chrome__*
    mcp__remote-devices__Claude_Browser__navigate → mcp__remote-devices__Claude_Browser__*"""
    parts = tool.split("__")
    if tool.startswith("mcp__") and len(parts) >= 3:
        return "__".join(parts[:-1]) + "__*"
    return tool


# 처음부터 미리 허용된 걸로 보는 묶음: 브라우저 도구는 한 번에 여러 동작을 해서 10초를 넘기기 쉬워요
# 맥 연결 도구(명령 실행·파일 올리기 등)는 사용자가 "항상 허용"해 둠. 접근 요청류(ASK_TOOLS)는 따로 10초 규칙.
DEFAULT_AUTO = {"mcp__claude-in-chrome__*", "mcp__remote-devices__Claude_Browser__*", "mcp__remote-devices__*"}


def learned(tool):
    fam = family(tool)
    if fam in state.get("prompt_tools", {}) or tool in state.get("prompt_tools", {}):
        return False                        # 실제로 승인 창을 띄운 적이 있는 도구 묶음
    known = state.get("auto_tools", {})
    return tool in known or fam in known or fam in DEFAULT_AUTO


def learn_exact(tool, waited_ms):
    """장치 1.1+가 보낸 실제 실행 시간으로 정확히 배움: 기다린 시간 = 전체 - 실행 시간."""
    if not tool or ASK_TOOLS.search(tool):
        return
    fam = family(tool)
    auto, prompt = state.setdefault("auto_tools", {}), state.setdefault("prompt_tools", {})
    if waited_ms < HUMAN_WAIT * 1000:
        auto[fam] = time.time()
        prompt.pop(fam, None)
    else:
        prompt[fam] = time.time()
        auto.pop(fam, None)
    print(f"승인 대기 {waited_ms / 1000:.1f}초: {tool} → {'미리 허용' if waited_ms < HUMAN_WAIT * 1000 else '물어보는 도구'}", flush=True)


def learn(tool):
    if tool and not ASK_TOOLS.search(tool):
        state.setdefault("auto_tools", {})[family(tool)] = time.time()


def handle_relay(data, sid, source):
    ev = str(data.get("ev"))
    try:
        ts = float(data.get("ts"))
    except (TypeError, ValueError):
        ts = time.time() * 1000
    project = clip(data.get("project"), 30) or None
    title = clip(data.get("title"), 40) or None
    tool = str(data.get("tool") or "")
    key = str(data.get("use") or "") or "tool:" + tool
    r = rt.setdefault(sid, {"turn": 0, "closed": 0, "pending": {}, "ran": {}})

    def late():                             # 이미 지난 턴의 신호 (파일이 늦게 도착)
        return ts < r["turn"] or (r["closed"] >= r["turn"] and ts <= r["closed"])

    if ev == "ended":
        app_ended(sid)
        rt.pop(sid, None)
    elif ev == "start":
        if ts < r["turn"]:
            return
        r.update(turn=ts, closed=0, pending={}, ran={})
        note = clip(strip_tags(data.get("text")))
        upsert(sid, source, "working", project, title, note=note or "")
    elif ev == "end":
        if ts < r["turn"]:
            return
        r.update(closed=ts, pending={}, ran={})
        kind = data.get("kind")
        if kind == "aborted":
            upsert(sid, source, "needs", project, title, reason="중단됨", alert="응답 도중 멈췄어요")
        elif kind == "error":
            upsert(sid, source, "needs", project, title, reason="오류", alert="오류로 멈췄어요", error=True)
        else:
            upsert(sid, source, "done", project, title, note=answer_note(data.get("head"), data.get("tail")))
    elif ev in ("ask", "question"):
        if late():
            return
        if key in r["ran"]:                 # 실행 끝 신호가 먼저 도착함
            if r["ran"].pop(key) - ts < LEARN_WITHIN * 1000:
                learn(tool)
            return
        if any(p["tool"] == tool for p in r["pending"].values()):
            return                          # 같은 도구의 두 번째 판정 신호 (중복)
        note = friendly(tool, data.get("input"))
        if data.get("decider") is not None or data.get("rule") or data.get("hook"):
            print(f"승인 판정: {tool} decider={data.get('decider')} rule={data.get('rule')} hook={data.get('hook')}", flush=True)
        if ev == "question":
            wait, reason = GRACE, R_CHOICE
        else:
            wait, reason = (LEARNED_GRACE if learned(tool) and not ASK_TOOLS.search(tool) else GRACE), R_PERMISSION
        r["pending"][key] = {"tool": tool, "ts": ts, "due": time.time() + wait, "shown": False,
                             "note": note, "reason": reason, "project": project, "title": title}
    elif ev == "ran":
        hit = [k for k, p in r["pending"].items() if k == key or p["tool"] == tool]
        if not hit:
            if not late():
                r["ran"][key] = ts
            return
        run_ms = data.get("run_ms")
        for k in hit:
            p = r["pending"].pop(k)
            if isinstance(run_ms, (int, float)):
                learn_exact(tool, max(0.0, ts - p["ts"] - run_ms))
            elif not p["shown"] and ts - p["ts"] < LEARN_WITHIN * 1000:
                learn(tool)
        s = state["sessions"].get(sid)
        if s and s["status"] == "needs" and not any(p["shown"] for p in r["pending"].values()) and not late():
            upsert(sid, source, "working", project, title)


def check_pending():
    """기다려 봐도 실행이 안 끝난 도구 → 정말 승인 창을 기다리는 중이므로 확인 필요."""
    changed = False
    now = time.time()
    for sid, r in rt.items():
        for p in r["pending"].values():
            if p["shown"] or now < p["due"]:
                continue
            p["shown"] = True
            s = state["sessions"].get(sid)
            if s and s["status"] == "working":
                upsert(sid, s["source"], "needs", p.get("project"), p.get("title"),
                       reason=p.get("reason") or R_PERMISSION, alert=p["note"])
                changed = True
    for sid, d in list(deferred.items()):    # 직접 기록된 확인 필요도 10초 버티면 올림
        if now < d["due"]:
            continue
        deferred.pop(sid)
        s = state["sessions"].get(sid)
        if s and s["status"] == "working":
            upsert(sid, s["source"], "needs", **d["args"])
            changed = True
    return changed


SELF = os.path.abspath(__file__)


def self_reload_if_changed(started_mtime):
    """server.py 파일이 바뀌면 스스로 다시 시작 (Claude가 맥에 새 버전을 올리면 바로 적용)."""
    try:
        mtime = os.path.getmtime(SELF)
        if mtime == started_mtime:
            return
        with open(SELF, encoding="utf-8") as f:
            compile(f.read(), SELF, "exec")      # 덜 써졌거나 문법이 틀린 파일이면 다음에 다시
    except (OSError, SyntaxError, ValueError):
        return
    with lock:
        save_state()
    print("server.py가 바뀌어서 다시 시작해요", flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def watch_inbox():
    """inbox 폴더를 0.2초마다 확인. 아직 다 안 써진 파일은 다음 차례에 다시 읽음."""
    os.makedirs(INBOX, exist_ok=True)
    started = os.path.getmtime(SELF)
    tick = 0
    while True:
        tick += 1
        if tick % 10 == 0:
            self_reload_if_changed(started)
        try:
            files = []
            for name in os.listdir(INBOX):
                if name.endswith(".json"):
                    path = os.path.join(INBOX, name)
                    files.append((os.path.getmtime(path), path))
            for mtime, path in sorted(files):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except ValueError:
                    if time.time() - mtime < 3:      # 쓰는 중일 수 있음 → 다음에 다시
                        continue
                    print("inbox 파일을 읽지 못함:", path, flush=True)
                    data = None
                except OSError:
                    continue
                if data is not None:
                    items = data if isinstance(data, list) else [data]
                    with lock:
                        for item in items:
                            if isinstance(item, dict):
                                handle_event(item, "app")
                        save_state()
                try:
                    os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass
        with lock:
            if check_pending():
                save_state()
        time.sleep(0.2)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeBoard/2.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def _authorized(self):
        """이 맥 자신, 또는 비밀 키(?k=...)를 가진 기기(아이패드)만 통과."""
        if self._local():
            return True
        given = parse_qs(urlparse(self.path).query).get("k", [""])[0] or self.headers.get("X-Board-Key", "")
        return bool(KEY) and secrets.compare_digest(given.encode("utf-8"), KEY.encode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._send(403, "<meta charset=utf-8><body style='background:#070708;color:#aaa;font:18px sans-serif;padding:40px'>대기판 주소가 올바르지 않아요. 맥에서 install.py가 알려준 주소(끝에 ?k=…가 붙은 주소)로 열어 주세요.", "text/html; charset=utf-8")
        if path in ("/", "/index.html"):
            try:
                with open(BOARD_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "board.html 이 없어요", "text/plain; charset=utf-8")
        elif path == "/ca.crt":                # 아이패드에 설치할 인증서
            try:
                with open(CA_CRT, "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, "인증서가 아직 없어요", "text/plain; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Disposition", 'attachment; filename="ClaudeBoard-CA.crt"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/links":                 # 연결 탭
            with lock:
                cleanup()
                links = sorted(state["links"].values(), key=lambda l: -l.get("last", 0))
                for l in links:
                    l["has_card"] = l["id"] in state["sessions"]
            self._send(200, json.dumps({"now": time.time(), "links": links}, ensure_ascii=False))
        elif path == "/state":
            with lock:
                cleanup()
                sessions = list(state["sessions"].values())
            self._send(200, json.dumps({"now": time.time(), "sessions": sessions}, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except ValueError:
            return self._send(400, '{"error":"bad json"}')
        if not isinstance(data, dict):
            data = {}

        if path in ("/hook", "/event") and not self._local():
            return self._send(403, "{}")       # 상태 변경은 이 맥에서 온 것만 받음
        if path in ("/dismiss", "/disconnect", "/rename") and not self._authorized():
            return self._send(403, "{}")

        if path == "/hook":                    # Claude Code 훅
            with lock:
                handle_hook(data)
                save_state()
            return self._send(200, "{}")
        if path == "/event":
            with lock:
                ok = handle_event(data, str(data.get("source") or "app"))
                save_state()
            return self._send(200 if ok else 400, "{}")
        if path == "/dismiss":                 # 완료 카드 탭: 카드만 내림 (연결 유지)
            with lock:
                remove(str(data.get("id", "")))
                save_state()
            return self._send(200, "{}")
        if path == "/rename":                  # 연결 탭: 표시 이름 바꾸기
            with lock:
                rename(str(data.get("id", "")), data.get("name", ""))
                save_state()
            return self._send(200, "{}")
        if path == "/disconnect":              # 길게 누르기·연결 탭: 연결 끊기
            with lock:
                disconnect(str(data.get("id", "")))
                save_state()
            return self._send(200, "{}")
        self._send(404, "{}")


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                                   # 인증서를 아직 신뢰하지 않은 기기의 접속 실패 등은 기록하지 않음


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 실제로 패킷을 보내지는 않음
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def local_hostname():
    try:
        r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3)
        return (r.stdout.strip() + ".local") if r.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------- https 인증서 (맥 안에서 직접 만드는 개인 인증기관) ----------
def openssl(*args):
    exe = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else "openssl"
    r = subprocess.run([exe, *args], capture_output=True, text=True, cwd=CERT_DIR)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])


def cert_names():
    dns = ["localhost"]
    host = local_hostname()
    if host:
        dns.append(host)
    ips = ["127.0.0.1"]
    ip = lan_ip()
    if ip:
        ips.append(ip)
    return dns, ips


def ensure_certs():
    """인증기관(한 번)과 서버 인증서(이름·IP가 바뀌면 새로)를 준비. 새로 만들었으면 True."""
    os.makedirs(CERT_DIR, exist_ok=True)
    cnf = os.path.join(CERT_DIR, "openssl.cnf")
    with open(cnf, "w") as f:
        f.write("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=Claude Board Local CA\nO=Qmin Lab\n"
                "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier=hash\n")
    if not (os.path.exists(CA_KEY) and os.path.exists(CA_CRT)):
        openssl("req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", CA_KEY, "-out", CA_CRT,
                "-days", "3650", "-sha256", "-config", cnf, "-extensions", "v3_ca")
        os.chmod(CA_KEY, 0o600)
        for old in (SRV_CRT, SRV_NAMES):
            if os.path.exists(old):
                os.remove(old)

    dns, ips = cert_names()
    want = json.dumps([dns, ips])
    try:
        with open(SRV_NAMES) as f:
            if f.read() == want and os.path.exists(SRV_CRT) and os.path.exists(SRV_KEY):
                return False
    except OSError:
        pass

    ext = os.path.join(CERT_DIR, "server.ext")
    san = ",".join([f"DNS:{d}" for d in dns] + [f"IP:{i}" for i in ips])
    with open(ext, "w") as f:
        f.write("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n")
    csr = os.path.join(CERT_DIR, "server.csr")
    openssl("req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", SRV_KEY, "-out", csr,
            "-config", cnf, "-subj", "/CN=Claude Board/O=Qmin Lab")
    openssl("x509", "-req", "-in", csr, "-CA", CA_CRT, "-CAkey", CA_KEY, "-CAcreateserial",
            "-out", SRV_CRT, "-days", "800", "-sha256", "-extfile", ext)
    os.chmod(SRV_KEY, 0o600)
    with open(SRV_NAMES, "w") as f:
        f.write(want)
    return True


def start_https():
    try:
        ensure_certs()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(SRV_CRT, SRV_KEY)
        httpsd = QuietServer(("0.0.0.0", HTTPS_PORT), Handler)
        httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True, do_handshake_on_connect=False)
    except Exception as e:  # https가 안 돼도 http 대기판은 그대로 동작
        print("https를 켜지 못했어요:", e, flush=True)
        return

    def watch():                            # 와이파이가 바뀌어 IP가 달라지면 인증서를 새로 만듦
        while True:
            time.sleep(120)
            try:
                if ensure_certs():
                    ctx.load_cert_chain(SRV_CRT, SRV_KEY)
                    print("IP가 바뀌어 인증서를 새로 만들었어요:", cert_names(), flush=True)
            except Exception as e:
                print("인증서 갱신 실패:", e, flush=True)

    threading.Thread(target=httpsd.serve_forever, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    return True


def main():
    global KEY
    KEY = load_key()
    load_state()
    threading.Thread(target=watch_inbox, daemon=True).start()
    try:
        httpd = QuietServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"{PORT}번 포트를 열 수 없어요 (이미 실행 중인가요?): {e}", flush=True)
        sys.exit(1)
    ip = lan_ip() or "맥의-IP"
    host = local_hostname()
    ok = start_https()
    print(f"Claude 대기판 실행 중 → 아이패드에서 http://{ip}:{PORT}/?k={KEY} 열기", flush=True)
    if ok:
        print(f"  https 주소: https://{host or ip}:{HTTPS_PORT}/?k={KEY}"
              + (f"  (또는 https://{ip}:{HTTPS_PORT}/?k={KEY})" if host else ""), flush=True)
        print(f"  아이패드 인증서 설치: http://{ip}:{PORT}/ca.crt?k={KEY}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
