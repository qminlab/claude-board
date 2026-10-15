#!/usr/bin/env python3
"""Claude 대기판 설치/제거.

  python3 install.py            → Claude Code 훅 등록 + 맥 로그인 시 서버 자동 실행
  python3 install.py --uninstall → 둘 다 되돌리기

~/.claude/settings.json 은 수정 전에 같은 폴더에 백업을 남겨요.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.expanduser("~/.claude/settings.json")
LABEL = "com.qminlab.claude-board"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
MARK = "# claude-board"

HOOK_CMD = (
    "curl -s -m 2 -H 'Content-Type: application/json' --data-binary @- "
    "http://127.0.0.1:7777/hook >/dev/null 2>&1 || true " + MARK
)
# 이벤트 → matcher (None 이면 matcher 없이 전부)
EVENTS = {
    "UserPromptSubmit": None,                         # 요청 보냄 → 작업중 (+ /rename 세션 이름)
    "PermissionRequest": None,                        # 권한 승인 창 → 확인 필요
    "Notification": "permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input",
    "PreToolUse": "AskUserQuestion",                  # 선택지 질문 → 확인 필요
    "PostToolUse": None,                              # 승인 후 다시 작업 → 작업중
    "PostToolUseFailure": None,
    "PermissionDenied": None,
    "Stop": None,                                     # 답변 끝 → 완료 (질문으로 끝나면 확인 필요)
    "StopFailure": None,                              # 오류로 끝 → 완료(오류)
    "SessionEnd": None,                               # 세션 종료 → 카드 내림
}


def is_ours(handler):
    return isinstance(handler, dict) and MARK in str(handler.get("command", ""))


def strip_ours(hooks):
    """기존에 넣었던 대기판 훅만 빼고 나머지는 그대로 둠."""
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                g = dict(g)
                g["hooks"] = [h for h in g["hooks"] if not is_ours(h)]
                if not g["hooks"]:
                    continue
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        sys.exit(f"{SETTINGS} 가 올바른 JSON이 아니라서 건드리지 않았어요. 파일을 확인해 주세요.")
    if not isinstance(data, dict):
        sys.exit(f"{SETTINGS} 형식이 예상과 달라서 건드리지 않았어요.")
    return data


def write_settings(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        backup = f"{SETTINGS}.bak-claude-board-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(SETTINGS, backup)
        print("  백업:", backup)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)


def install_hooks():
    data = load_settings()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    strip_ours(hooks)
    for event, matcher in EVENTS.items():
        group = {"hooks": [{"type": "command", "command": HOOK_CMD, "async": True, "timeout": 5}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    data["hooks"] = hooks
    write_settings(data)
    print("✓ Claude Code 훅 등록:", ", ".join(EVENTS))


def uninstall_hooks():
    data = load_settings()
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        strip_ours(hooks)
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
        write_settings(data)
    print("✓ Claude Code 훅 제거")


def launchctl(*args):
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True)
    except FileNotFoundError:          # 맥이 아닌 곳
        return subprocess.CompletedProcess(args, 1, "", "launchctl 없음")


def install_autostart():
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, os.path.join(BASE, "server.py")],
        "WorkingDirectory": BASE,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.join(BASE, "server.log"),
        "StandardErrorPath": os.path.join(BASE, "server.log"),
    }
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")
    r = launchctl("bootstrap", domain, PLIST)
    if r.returncode != 0:
        r = launchctl("load", "-w", PLIST)
    if r.returncode == 0:
        print("✓ 서버 자동 실행 등록 (맥 로그인 때마다 켜짐, 지금도 켜짐)")
    else:
        print("! 자동 실행 등록에 실패했어요. 대신 `python3 server.py` 로 직접 켜 주세요.\n ", r.stderr.strip())


def uninstall_autostart():
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("✓ 서버 자동 실행 해제")


def main():
    os.makedirs(os.path.join(BASE, "inbox"), exist_ok=True)
    if "--uninstall" in sys.argv:
        uninstall_hooks()
        uninstall_autostart()
        return
    install_hooks()
    if sys.platform == "darwin" and "--no-autostart" not in sys.argv:
        install_autostart()
        time.sleep(4)                         # 처음엔 https 인증서를 만드느라 몇 초 걸림
        try:
            with open(os.path.join(BASE, "server.log"), encoding="utf-8") as f:
                lines = f.read().splitlines()
            starts = [i for i, l in enumerate(lines) if "실행 중" in l]
            if starts:
                print()
                for l in lines[starts[-1]:]:
                    print(l)
        except OSError:
            pass
    print("\n이미 열려 있던 Claude Code 세션은 다시 시작해야 훅이 적용돼요.")


if __name__ == "__main__":
    main()
