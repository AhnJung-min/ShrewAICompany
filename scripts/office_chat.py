#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""office_chat.py - 대표 명령창 백엔드 (표준 라이브러리만).

오피스 페이지의 '대표 명령창'에서 오너가 보낸 말을, 이 PC 의 Claude Code CLI 를 헤드리스(-p)로 돌려 처리하고
답을 페이지에 돌려준다. 대화는 --resume 으로 이어진다.

HTTP 계약 (office_server.py 가 연결):
  POST /api/chat          {"message": str(1..8000), "model": "sonnet"|"opus"} -> 202 {"job_id"} | 409 | 503
  GET  /api/chat/jobs/<id> -> {"job_id","status","text","tools","session_id","cost_usd","error","started_at","finished_at"}
  GET  /api/chat/history   -> {"messages":[{role,text,ts,job_id}], "cli":{found,path,logged_in}}  (최근 100개)
  POST /api/chat/reset     -> {"ok": true}
  모든 /api/chat* 요청: X-Shrew-Token 헤더 + Host(127.0.0.1|localhost:<port>) + Origin 동일 출처 필수.

저장: office/chat/history.jsonl (append-only), office/chat/session.json ({"session_id": ...}) - .gitignore 대상.
실행 파일(PyInstaller)로 묶여도 동작해야 하므로 ROOT 는 호출자가 넘기고, 자식 프로세스에는 AIRE_ROOT 로 전달한다.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402
import office  # noqa: E402

# --------------------------------------------------------------------------
# 상수
# --------------------------------------------------------------------------
TOKEN_PLACEHOLDER = "__SHREW_CHAT_TOKEN__"
TOKEN_HEADER = "X-Shrew-Token"
MAX_MESSAGE_CHARS = 8000
MAX_BODY_BYTES = 64 * 1024
HISTORY_LIMIT = 100
TIMEOUT_SECONDS = 15 * 60
MODELS = ("sonnet", "opus")
DEFAULT_MODEL = "sonnet"
CEO_ID = "ceo"
CEO_TASK = "대표 명령 처리 중"
CEO_BUBBLE = "📨"
MAX_TEXT_CHARS = 400_000      # 한 답변이 메모리를 무한정 먹지 않게
MAX_TOOLS = 60                # 한 작업에서 기록하는 도구 호출 수 상한
KEEP_JOBS = 30                # 메모리에 남기는 작업 수
STDERR_KEEP = 6000
READER_GRACE_SECONDS = 5      # CLI 종료 뒤 남은 출력을 비우는 데 기다리는 최대 시간 (파이프를 쥔 고아가 있으면 여기서 포기)
SESSION_RESET_NOTE = "새 대화 시작"

# 허용 목록. Read/Glob/Grep/Edit/Write 는 일부러 '이름만' 넣지 않는다: 이름만 있는 허용 규칙은 경로를 가리지 않아서
# 프로젝트 폴더 밖(예: 사용자 폴더 전체)도 읽고 쓸 수 있게 된다(실제 CLI 로 확인). 규칙을 빼면 CLI 기본 동작 -
# 작업 폴더(=ROOT) 안의 읽기는 자동 허용, acceptEdits 로 폴더 안의 파일 수정은 자동 허용, 폴더 밖은 물어봐야 하는데
# 헤드리스(-p)에는 물어볼 사람이 없으므로 거부 - 이 된다.
ALLOWED_TOOLS = [
    "WebSearch", "WebFetch",
    "Bash(python scripts/office.py:*)",
    "Bash(python scripts/tracker.py list:*)",
    "Bash(python scripts/tracker.py report:*)",
    "Bash(python scripts/tracker.py add-product:*)",
    "Bash(python scripts/tracker.py set-status:*)",
    "Bash(python scripts/tracker.py add-ledger:*)",
    "Bash(python scripts/tracker.py request-approval:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
]
DISALLOWED_TOOLS = [
    "Bash(python scripts/tracker.py approve:*)",
    "Bash(python scripts/tracker.py reject:*)",
    "Bash(git push:*)", "Bash(git reset:*)", "Bash(rm:*)", "Bash(del:*)",
    # 대시보드 상태를 통째로 지우는 명령 (생산 워크플로가 돌아가는 중에 직원 상태 파일을 날릴 수 있다)
    "Bash(python scripts/office.py reset:*)",
    # 오너 전용 행위(게시·PR 병합·로그인)로 이어지는 git/gh 쓰기 명령. 사용자 설정이 허용해 둬도 여기서 막는다.
    "Bash(gh:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
    "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git pull:*)", "Bash(git clean:*)", "Bash(git rm:*)",
    "Bash(git config:*)", "Bash(git remote:*)", "Bash(git stash:*)", "Bash(git branch:*)", "Bash(git tag:*)",
    # 허용된 읽기 전용 git 명령이 파일을 쓰거나(--output) 프로젝트 밖 파일을 읽는(--no-index) 데 쓰이지 않게
    "Bash(git * --output*)", "Bash(git * --no-index*)", "Bash(git * --ext-diff*)", "Bash(git -c*)",
    # Edit/Write 는 폴더 안이면 자동 허용이므로, 승인 대기열을 파일로 직접 고쳐 approve 금지를 우회하거나
    # 승인 가드(tracker.py)·운영 지침·설정·대화 기록을 바꾸지 못하게 막는다 (경로는 작업 폴더=ROOT 기준).
    "Edit(tracker/approvals.csv)", "Write(tracker/approvals.csv)",
    "Edit(CLAUDE.md)", "Write(CLAUDE.md)",
    "Edit(.claude/**)", "Write(.claude/**)",
    "Edit(scripts/**)", "Write(scripts/**)",
    "Edit(office/chat/**)", "Write(office/chat/**)",
    # 비밀값은 읽지도 않는다 (WebFetch 로 내보내는 길을 만들지 않게)
    "Read(.env)", "Read(.env.*)", "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.key)", "Read(secrets/**)",
]

LOGIN_MESSAGE = ("Claude CLI에 로그인되어 있지 않거나 로그인이 만료되었습니다. "
                 "터미널(PowerShell)에서 claude 를 한 번 실행하고 /login 으로 로그인한 뒤 다시 보내 주세요.")
NOT_FOUND_MESSAGE = "Claude CLI(claude.exe)를 찾을 수 없습니다."
NOT_FOUND_HINT = ("Claude 데스크톱 앱 또는 Claude Code CLI 를 설치하고 한 번 로그인하세요. "
                  "다른 위치에 있다면 환경변수 SHREW_CLAUDE_BIN 에 claude.exe 전체 경로를 지정하세요.")

_LOGIN_RE = re.compile(
    r"(not logged in|please run /login|run /login|invalid api key|oauth token (?:has )?(?:expired|revoked)|"
    r"token (?:has )?(?:expired|been revoked)|authentication[_ ](?:error|failed)|failed to authenticate|"
    r"invalid authentication credentials|credentials? (?:have |has )?expired|no api key|missing api key)",
    re.IGNORECASE)
_NO_SESSION_RE = re.compile(r"no conversation found|session .*not found|could not find session", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 부모 Claude 세션을 가리키는 환경변수는 자식 헤드리스 세션에 넘기지 않는다
# (오너가 Claude Code 안에서 이 앱을 띄웠을 때 중첩 세션으로 오인되는 것을 막는다. 로그인 관련 변수는 그대로 둔다).
_SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class ChatError(Exception):
    """HTTP 응답으로 바로 바꿀 수 있는 오류. message 는 한국어 문장."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.message, **self.extra}


# --------------------------------------------------------------------------
# Claude CLI 찾기
# --------------------------------------------------------------------------
def _version_key(name: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) or (0,)


def _usable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if path.lower().endswith(".py") and getattr(sys, "frozen", False):
        return False  # 실행 파일 안에서는 sys.executable 이 앱 자신이라 .py 를 돌릴 수 없다
    return True


def _cli_bases() -> list[Path]:
    """claude-code 버전 폴더들이 있는 곳.

    Claude 데스크톱 앱은 Windows 스토어(MSIX) 앱이라, 앱이 %APPDATA%\\Claude 에 쓴 파일이 실제로는
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude 에 있다. 앱 밖에서 도는
    ShrewOffice.exe 에는 두 번째 경로만 보인다.
    """
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata) / "Claude" / "claude-code")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
                bases.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude-code")
        except OSError:
            pass
    return bases


def _appdata_cli() -> str | None:
    versions: list[Path] = []
    for base in _cli_bases():
        try:
            versions.extend(d for d in base.iterdir() if d.is_dir())
        except OSError:
            continue
    if not versions:
        return None
    for d in sorted(versions, key=lambda p: _version_key(p.name), reverse=True):
        try:
            exes = sorted(d.rglob("claude.exe"), key=lambda p: (len(p.parts), str(p)))
        except OSError:
            continue
        for exe in exes:
            if exe.is_file():
                return str(exe)
    return None


def discover_cli() -> str | None:
    """SHREW_CLAUDE_BIN -> PATH 의 claude -> (%APPDATA% 또는 스토어 앱 LocalCache)/Claude/claude-code/<최신버전>/**/claude.exe -> ~/.local/bin."""
    env = os.environ.get("SHREW_CLAUDE_BIN", "").strip().strip('"')
    if env:
        cand = os.path.expanduser(env)
        if _usable(cand):
            return cand
    found = shutil.which("claude")
    if found and _usable(found):
        return found
    via_appdata = _appdata_cli()
    if via_appdata:
        return via_appdata
    try:
        local = Path.home() / ".local" / "bin"
    except RuntimeError:  # 홈 폴더를 알 수 없는 환경
        return None
    for name in ("claude.exe", "claude"):
        if _usable(str(local / name)):
            return str(local / name)
    return None


def valid_session_id(value: Any) -> bool:
    """--resume 뒤에 붙는 값. 파일(session.json)이 변조돼 '--옵션' 이 끼어드는 일이 없도록 안전한 글자만 허용."""
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def setting_sources() -> str:
    """CLI 가 읽을 설정 범위. 기본은 프로젝트 설정만. 로그인을 사용자 설정(apiKeyHelper 등)으로 하는 PC 만
    환경변수 SHREW_CLAUDE_USER_SETTINGS=1 로 user 를 다시 켠다."""
    if os.environ.get("SHREW_CLAUDE_USER_SETTINGS", "").strip() == "1":
        return "user,project,local"
    return "project,local"


def _is_batch(cli: str) -> bool:
    return cli.lower().endswith((".cmd", ".bat"))


def _single_line(text: str) -> str:
    """cmd.exe 를 거치는 .cmd/.bat 래퍼용: 줄바꿈·따옴표·%가 인자를 깨뜨리지 않게 한 줄로."""
    return " ".join(text.split()).replace('"', "'").replace("%", "％")


def build_command(cli: str, prompt: str, model: str, system_prompt: str,
                  resume_id: str | None = None) -> tuple[list[str], bytes | None]:
    """(인자 리스트, stdin 으로 보낼 바이트|None). shell 은 쓰지 않는다.

    일반 claude.exe 는 프롬프트를 -p 뒤 인자로 넘긴다. npm 의 claude.cmd 같은 배치 래퍼는 cmd.exe 가 줄바꿈에서 인자를
    자르고 줄 길이도 8191자로 제한되므로, 그때만 프롬프트를 stdin 으로 보내고 시스템 프롬프트는 한 줄로 만든다.
    """
    prefix = [sys.executable, cli] if cli.lower().endswith(".py") else [cli]
    batch = _is_batch(cli)
    args: list[str] = ["-p"]
    stdin_data: bytes | None = None
    if batch:
        stdin_data = prompt.encode("utf-8")
    else:
        args.append(" " + prompt if prompt.startswith("-") else prompt)  # '-로 시작하는 글'이 옵션으로 읽히지 않게
    args += [
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--append-system-prompt", _single_line(system_prompt) if batch else system_prompt,
        # 오너의 전역 설정(~/.claude/settings.json)에 들어 있는 허용 규칙(git push, gh pr 등)이 이 세션에 섞이지 않게
        # 프로젝트 설정만 읽고, claude.ai 커넥터(Gmail·Notion 등)와 MCP 서버는 아예 붙이지 않는다.
        "--setting-sources", setting_sources(),
        "--strict-mcp-config",
    ]
    if resume_id:
        if not valid_session_id(resume_id):
            raise ValueError("세션 ID 형식이 올바르지 않습니다.")
        args += ["--resume", resume_id]
    return prefix + args, stdin_data


# --------------------------------------------------------------------------
# 시스템 프롬프트 (+ 오피스 스냅샷)
# --------------------------------------------------------------------------
_SYSTEM_HEAD = """당신은 'Shrew AI Company(땃쥐 AI 컴퍼니)' 오피스의 대표 명령 비서입니다. 지금 오너(대표)가 오피스 화면의 '대표 명령창'에서 말을 걸고 있고, 작업 폴더는 이 프로젝트의 루트입니다.

반드시 지킬 절대 규칙(CLAUDE.md):
1. 계정 생성·로그인·약관 동의·결제/계좌/신분증 정보 입력·금융 거래(주식·코인 등)를 하지 않는다. 돈은 오너 계정으로만 들어간다.
2. 게시·발송·제출은 오너가 직접 한다. 승인이 필요한 일은 python scripts/tracker.py request-approval 로 요청만 올린다. approve/reject 는 절대 실행하지 않고 tracker/approvals.csv 도 직접 고치지 않는다. scripts/·CLAUDE.md·.claude/ 도 고치지 않고, git push·gh(PR 병합 포함)도 쓰지 않으며, 프로젝트 폴더 밖의 파일은 읽거나 쓰지 않는다.
3. 허위 수익 보장, 가짜 리뷰, 스팸, 저작권·상표 침해, 무단 개인정보 수집을 하지 않는다. API 키·토큰 같은 비밀값은 읽어서 말하거나 파일에 쓰지 않는다.

일하는 방식:
- 이 창에서는 워크플로(여러 직원이 병렬로 도는 배치 실행)를 돌릴 수 없다. 대표가 새 배치 생산 같은 큰 멀티 에이전트 작업을 시키면 그렇게 설명하고, Claude 데스크톱 세션에 그대로 붙여넣을 요청문을 구체적으로(배치 이름·개수·주제 포함) 제안한다.
- 현황 요약, 승인 대기 정리, 리포트, 트래커 조회·기록, 문서 읽기 같은 일은 직접 처리한다. 오피스 상태는 python scripts/office.py, 트래커는 python scripts/tracker.py 로만 다룬다.
- 답변은 한국어로 짧고 분명하게. 표나 긴 목록은 꼭 필요할 때만 쓴다.
"""


def _won(n: Any) -> str:
    try:
        return f"{int(n):,}원"
    except (TypeError, ValueError):
        return "0원"


def snapshot_text(state: dict[str, Any] | None) -> str:
    """build_state() 결과를 시스템 프롬프트용 몇 줄로 압축한다 (누락·이상 값에 안전)."""
    if not isinstance(state, dict):
        return "[오피스 스냅샷을 읽지 못했습니다]"
    lines = [f"[현재 오피스 스냅샷 - {C.clean_text(state.get('now', ''), 40)}]"]
    agents = [a for a in (state.get("agents") or []) if isinstance(a, dict) and a.get("state") not in (None, "idle")]
    if agents:
        parts = [f"{C.clean_text(a.get('id', ''), 20)}({a.get('state')}: {C.clean_text(a.get('task', ''), 50)})"
                 for a in agents[:12]]
        more = f" 외 {len(agents) - 12}명" if len(agents) > 12 else ""
        lines.append("- 일하는/움직이는 직원: " + ", ".join(parts) + more)
    else:
        lines.append("- 직원: 모두 대기 중")
    approvals = [a for a in (state.get("approvals") or []) if isinstance(a, dict)]
    if approvals:
        ids = ", ".join(f"{C.clean_text(a.get('id', ''), 20)}[{C.clean_text(a.get('action', ''), 24)}]" for a in approvals[:10])
        more = f" 외 {len(approvals) - 10}건" if len(approvals) > 10 else ""
        lines.append(f"- 승인 대기 {len(approvals)}건: {ids}{more}")
    else:
        lines.append("- 승인 대기 0건")
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    by_status = kpis.get("by_status") if isinstance(kpis.get("by_status"), dict) else {}
    counts = ", ".join(f"{k} {v}" for k, v in by_status.items() if v)
    lines.append(f"- 상품 총 {kpis.get('products_total', 0)}개" + (f" ({counts})" if counts else ""))
    lines.append(f"- 매출: 누적 {_won(kpis.get('revenue_krw_total'))} / 이번 달 {_won(kpis.get('revenue_krw_this_month'))}"
                 f" / 지출 누적 {_won(kpis.get('expenses_krw_total'))} / 순이익 누적 {_won(kpis.get('net_krw_total'))}")
    return "\n".join(lines)


def build_system_prompt(state: dict[str, Any] | None) -> str:
    return _SYSTEM_HEAD + "\n" + snapshot_text(state)


# --------------------------------------------------------------------------
# stream-json 해석 보조
# --------------------------------------------------------------------------
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|"
    r"Bearer\s+[A-Za-z0-9._\-]{16,})")
_KV_SECRET_RE = re.compile(
    r"(?i)([\w.\-]*(?:api[_\-]?key|token|secret|passw(?:or)?d|pwd|authorization)[\w.\-]*\s*[=:]\s*)(\S+)")
_SUMMARY_KEYS = ("file_path", "path", "notebook_path", "pattern", "command", "url", "query", "description", "prompt")


def redact(text: str) -> str:
    return _KV_SECRET_RE.sub(r"\1***", _SECRET_RE.sub("***", text))


def _rel_path(value: str, root: Path) -> str:
    try:
        rel = os.path.relpath(value, str(root))
    except ValueError:  # 다른 드라이브
        return value
    return value if rel.startswith("..") else rel.replace("\\", "/")


def tool_summary(name: str, tool_input: Any, root: Path) -> str:
    """'Read · docs/00_전략_Strategy.md' 칩에 쓸 한 줄 요약 (최대 120자, 비밀값 가림)."""
    if not isinstance(tool_input, dict):
        return C.clean_text(redact(str(tool_input)), 120) if tool_input else ""
    keys = ("pattern",) + _SUMMARY_KEYS if name in ("Grep", "Glob") else _SUMMARY_KEYS  # 검색은 무엇을 찾는지가 먼저
    for key in keys:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            if key in ("file_path", "path", "notebook_path") and os.path.isabs(val):
                val = _rel_path(val, root)
            if key == "pattern" and name in ("Grep", "Glob") and isinstance(tool_input.get("path"), str):
                val = f"{val} ({tool_input['path']})"
            return C.clean_text(redact(val), 120)
    try:
        return C.clean_text(redact(json.dumps(tool_input, ensure_ascii=False)), 120)
    except (TypeError, ValueError):
        return ""


class _Stream:
    """한 번의 CLI 실행에서 모은 스트림 상태."""

    def __init__(self) -> None:
        self.committed: list[str] = []   # 확정된 assistant 텍스트 덩어리
        self.partial = ""                # stream_event 로 자라는 중인 덩어리
        self.result: dict[str, Any] | None = None
        self.login_error = False
        self.saw_init = False

    def text(self) -> str:
        parts = self.committed + ([self.partial] if self.partial else [])
        return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# 작업(Job)
# --------------------------------------------------------------------------
class Job:
    def __init__(self, message: str, model: str):
        self.job_id = uuid.uuid4().hex[:16]
        self.message = message
        self.model = model
        self.status = "queued"
        self.text = ""
        self.tools: list[dict[str, str]] = []
        self.session_id: str | None = None
        self.cost_usd: float | None = None
        self.error: str | None = None
        self.started_at = C.now_iso()
        self.finished_at: str | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id, "status": self.status, "text": self.text,
                "tools": [dict(t) for t in self.tools], "session_id": self.session_id,
                "cost_usd": self.cost_usd, "error": self.error,
                "started_at": self.started_at, "finished_at": self.finished_at,
            }

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

    def set_text(self, text: str) -> None:
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n…(너무 길어 여기서 잘렸습니다)"
        with self._lock:
            self.text = text

    def add_tool(self, name: str, summary: str) -> None:
        with self._lock:
            if len(self.tools) < MAX_TOOLS:
                self.tools.append({"name": name, "summary": summary})

    def set_session(self, session_id: str | None) -> None:
        with self._lock:
            self.session_id = session_id if session_id is None or valid_session_id(session_id) else None

    def set_cost(self, cost: float | None) -> None:
        with self._lock:
            self.cost_usd = cost

    def reset_output(self) -> None:
        with self._lock:
            self.text, self.tools, self.cost_usd = "", [], None

    def finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.error = error
            self.finished_at = C.now_iso()
            self.status = status  # 마지막에 바꾼다: 'done/error' 를 본 쪽은 모든 필드가 확정된 것을 본다


def _kill_tree(proc: subprocess.Popen) -> None:
    """자식과 그 자손까지 전부 종료 (Windows: taskkill /T, 그 외: 프로세스 그룹)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
                           timeout=20, check=False)
        else:
            os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 서비스
# --------------------------------------------------------------------------
class ChatService:
    def __init__(self, root, state_provider: Callable[[Any], dict[str, Any]] | None = None,
                 timeout_seconds: float = TIMEOUT_SECONDS):
        try:  # 윈도우 8.3 짧은 이름(C:\Users\DKSWJ_~1\...)이 섞인 경로는 CLI 가 안전하지 않다고 보고 쓰기를 막는다 -> 긴 경로로
            self.root = Path(root).resolve()
        except (OSError, RuntimeError):
            self.root = Path(root)
        self.token = secrets.token_urlsafe(32)
        self.timeout_seconds = timeout_seconds
        self._state_provider = state_provider
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: Job | None = None
        self._cli_path: str | None = None
        self._logged_in: bool | None = None
        self._epoch = 0
        self._proc: subprocess.Popen | None = None
        self._closed = False

    # -- 경로 / 저장소 -------------------------------------------------------
    @property
    def chat_dir(self) -> Path:
        return self.root / "office" / "chat"

    @property
    def history_path(self) -> Path:
        return self.chat_dir / "history.jsonl"

    @property
    def session_path(self) -> Path:
        return self.chat_dir / "session.json"

    def _append_history(self, role: str, text: str, job_id: str | None) -> None:
        rec = {"role": role, "text": text, "ts": C.now_iso(), "job_id": job_id}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8", "replace")
        try:
            with self._file_lock:
                C.append_bytes(self.history_path, line)
        except OSError:
            pass  # 기록 실패가 대화를 막지 않게

    def history(self, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec in C.read_jsonl_tail(self.history_path, limit * 2):  # 깨진 줄이 섞여도 창이 줄지 않게 여유를 둔다
            role, text = rec.get("role"), rec.get("text")
            if role not in ("owner", "claude", "system") or not isinstance(text, str):
                continue
            ts, jid = rec.get("ts"), rec.get("job_id")
            out.append({"role": role, "text": text, "ts": ts if isinstance(ts, str) else "",
                        "job_id": jid if isinstance(jid, str) else None})
        return out[-limit:]

    def _read_session(self) -> str | None:
        data = C.read_json_file(self.session_path)
        sid = data.get("session_id") if isinstance(data, dict) else None
        return sid if valid_session_id(sid) else None   # 형식이 이상하면 없는 것으로 친다 (인자 주입 방지)

    def _write_session(self, session_id: str | None) -> None:
        try:
            with self._file_lock:
                C.atomic_write_text(self.session_path, json.dumps({"session_id": session_id}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- CLI -----------------------------------------------------------------
    def get_cli(self) -> str | None:
        with self._lock:
            if self._cli_path and os.path.isfile(self._cli_path):
                return self._cli_path
            self._cli_path = discover_cli()  # 사라졌거나 처음이면 다시 찾는다
            return self._cli_path

    def cli_info(self) -> dict[str, Any]:
        path = self.get_cli()
        return {"found": path is not None, "path": path, "logged_in": self._logged_in}

    # -- 공개 동작 -----------------------------------------------------------
    def history_payload(self) -> dict[str, Any]:
        with self._lock:
            active = self._active.job_id if self._active and self._active.status in ("queued", "running") else None
        return {"messages": self.history(), "cli": self.cli_info(), "active_job_id": active}

    def job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def reset(self) -> None:
        with self._lock:
            self._epoch += 1  # 실행 중이던 작업이 끝나며 옛 세션을 되살리지 못하게
        self._write_session(None)
        self._append_history("system", SESSION_RESET_NOTE, None)

    def start_job(self, message: str, model: str = DEFAULT_MODEL) -> Job:
        with self._lock:
            if self._closed:
                raise ChatError(503, "오피스 서버가 종료되는 중입니다.")
            if model not in MODELS:
                raise ChatError(400, "model 은 sonnet 또는 opus 여야 합니다.")
            if self._active is not None and self._active.status in ("queued", "running"):
                raise ChatError(409, "이전 명령을 처리 중입니다. 끝난 뒤 다시 보내 주세요.", job_id=self._active.job_id)
            cli = self.get_cli()
            if not cli:
                raise ChatError(503, NOT_FOUND_MESSAGE, hint=NOT_FOUND_HINT)
            job = Job(message, model)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._active = job
            self._prune()
            epoch = self._epoch
        self._append_history("owner", message, job.job_id)
        try:
            threading.Thread(target=self._run_job, args=(job, cli, epoch), name=f"chat-{job.job_id}", daemon=True).start()
        except Exception:  # noqa: BLE001 - 스레드를 못 만들면 대기 상태로 남기지 않는다
            job.finish("error", "명령을 시작하지 못했습니다.")
            raise ChatError(500, "명령을 시작하지 못했습니다.")
        return job

    def close(self) -> None:
        """서버 종료 시: 돌고 있는 CLI 를 자손까지 정리한다."""
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)

    def _prune(self) -> None:
        while len(self._order) > KEEP_JOBS:
            old = self._order[0]
            job = self._jobs.get(old)
            if job is not None and job.status in ("queued", "running"):
                break
            self._order.pop(0)
            self._jobs.pop(old, None)

    # -- 오피스 책상(CEO) 상태 -----------------------------------------------------
    def _ceo_begin(self, job: Job) -> dict[str, Any] | None:
        prev = None
        try:
            data = C.read_json_file(office.status_path(CEO_ID, self.root))
            prev = data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            prev = None
        try:
            office.set_status(CEO_ID, "working", CEO_TASK, bubble=CEO_BUBBLE, root=self.root)
            office.log_event(CEO_ID, "note", f"대표 명령: {C.clean_text(job.message, 4000)[:40]}", root=self.root)
        except Exception:  # noqa: BLE001 - 대시보드 표시 실패가 명령 처리를 막지 않는다
            pass
        return prev

    def _ceo_end(self, prev: dict[str, Any] | None, ok: bool) -> None:
        try:
            cur = C.read_json_file(office.status_path(CEO_ID, self.root))
            ours = (isinstance(cur, dict) and cur.get("state") == "working" and cur.get("task") == CEO_TASK
                    and cur.get("bubble") == CEO_BUBBLE)
            if ours:  # 그 사이 다른 워크플로가 CEO 상태를 바꿨다면 건드리지 않는다
                record = None
                if prev and prev.get("task") != CEO_TASK:
                    pid = prev.get("product_id") if isinstance(prev.get("product_id"), str) else None
                    for bubble in (prev.get("bubble") if isinstance(prev.get("bubble"), str) else None, None):
                        try:
                            record = office.build_status(CEO_ID, str(prev.get("state") or "idle"),
                                                         str(prev.get("task") or ""), bubble, pid)
                            break
                        except Exception:  # noqa: BLE001 - 옛 말풍선이 규칙에 안 맞으면 기본 말풍선으로
                            record = None
                if record is None:
                    record = office.build_status(CEO_ID, "idle")
                office.write_status(record, self.root)
            office.log_event(CEO_ID, "note", "대표 명령 처리 완료" if ok else "대표 명령 처리 실패", root=self.root)
        except Exception:  # noqa: BLE001
            pass

    # -- 실행 ------------------------------------------------------------------
    def _snapshot_state(self) -> dict[str, Any] | None:
        try:
            provider = self._state_provider
            if provider is None:
                import office_server  # 지연 임포트: office_server 가 이 모듈을 먼저 불러온다
                provider = office_server.build_state
            return provider(self.root)
        except Exception:  # noqa: BLE001
            return None

    def _child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
        env["AIRE_ROOT"] = str(self.root)
        return env

    def _run_job(self, job: Job, cli: str, epoch: int) -> None:
        """스레드 진입점. 어떤 예외가 나도 작업이 'running' 에 갇혀 다음 명령을 막지 않게 마지막에 반드시 확정한다."""
        try:
            self._run_job_inner(job, cli, epoch)
        except BaseException:  # noqa: BLE001
            pass
        finally:
            if job.snapshot()["status"] in ("queued", "running"):
                job.finish("error", "명령 처리 중 내부 오류가 발생했습니다.")

    def _run_job_inner(self, job: Job, cli: str, epoch: int) -> None:
        prev = None
        ok = False
        try:
            job.set_status("running")
            system_prompt = build_system_prompt(self._snapshot_state())  # 책상 표시를 바꾸기 전의 실제 상태
            prev = self._ceo_begin(job)
            resume = self._read_session()
            outcome = self._execute(job, cli, system_prompt, resume, epoch)
            ok = outcome is None
            error = outcome
        except Exception as exc:  # noqa: BLE001 - 어떤 일이 있어도 서버/스레드는 살아남는다
            error = f"명령 처리 중 내부 오류가 발생했습니다 ({type(exc).__name__})."
        # 기록 -> 책상 복원 -> 상태 확정 순서: 'done' 이 보이면 history 도 이미 최신이다.
        snap = job.snapshot()
        try:
            if ok:
                self._append_history("claude", snap["text"] or "(답변이 비어 있습니다)", job.job_id)
            else:
                if snap["text"].strip():
                    self._append_history("claude", snap["text"], job.job_id)
                self._append_history("system", error or "알 수 없는 오류", job.job_id)
        except Exception:  # noqa: BLE001
            pass
        self._ceo_end(prev, ok)
        job.finish("done" if ok else "error", None if ok else (error or "알 수 없는 오류"))

    def _execute(self, job: Job, cli: str, system_prompt: str, resume: str | None, epoch: int) -> str | None:
        """None 이면 성공, 문자열이면 사용자에게 보여줄 오류."""
        result = self._stream_once(job, cli, system_prompt, resume)
        if result["no_session"] and resume:
            # 저장된 세션이 CLI 쪽에서 사라졌다 -> 잊고 새 대화로 한 번 더
            self._write_session(None)
            job.reset_output()
            job.set_session(None)
            result = self._stream_once(job, cli, system_prompt, None)
        sid = result["session_id"]
        with self._lock:
            if sid and epoch == self._epoch and not result["no_session"]:
                self._write_session(sid)
            if result["login_error"]:
                self._logged_in = False
            elif result["ok"]:
                self._logged_in = True
        return None if result["ok"] else result["error"]

    def _stream_once(self, job: Job, cli: str, system_prompt: str, resume_id: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": "알 수 없는 오류", "login_error": False,
                               "no_session": False, "session_id": None}
        prompt = job.message
        argv, stdin_data = build_command(cli, prompt, job.model, system_prompt, resume_id)
        kwargs: dict[str, Any] = dict(
            cwd=str(self.root), env=self._child_env(), shell=False,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except (OSError, ValueError) as exc:
            out["error"] = f"Claude CLI 를 실행하지 못했습니다: {C.clean_text(str(exc), 200)}"
            return out
        with self._lock:
            self._proc = proc
            closing = self._closed
        if closing:
            _kill_tree(proc)

        stderr_chunks: list[bytes] = []

        def drain_stderr() -> None:
            try:
                for chunk in iter(lambda: proc.stderr.read(4096), b""):  # type: ignore[union-attr]
                    stderr_chunks.append(chunk)
                    if sum(len(c) for c in stderr_chunks) > STDERR_KEEP * 4:
                        del stderr_chunks[: len(stderr_chunks) // 2]
            except (OSError, ValueError):
                pass

        def feed_stdin() -> None:
            try:
                proc.stdin.write(stdin_data or b"")  # type: ignore[union-attr]
                proc.stdin.close()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

        helpers = [threading.Thread(target=drain_stderr, daemon=True)]
        if stdin_data is not None:
            helpers.append(threading.Thread(target=feed_stdin, daemon=True))
        for t in helpers:
            t.start()

        timed_out = threading.Event()
        abandoned = threading.Event()   # 읽기 스레드를 포기한 뒤에는 늦게 온 줄을 작업에 반영하지 않는다

        def on_timeout() -> None:
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(self.timeout_seconds, on_timeout)
        timer.daemon = True
        timer.start()
        st = _Stream()

        def read_stdout() -> None:
            try:
                for raw in proc.stdout:  # type: ignore[union-attr]
                    if abandoned.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(ev, dict):
                        self._on_event(job, st, ev)
            except (OSError, ValueError):
                pass

        # 읽기를 별도 스레드로 돌린다: CLI 가 끝났거나 강제 종료됐는데도 고아가 된 자손 프로세스가 stdout 파이프를 쥐고 있으면
        # 'for raw in stdout' 이 영영 안 끝나 작업이 running 에 갇히고 다음 명령이 계속 409 가 되기 때문이다.
        reader = threading.Thread(target=read_stdout, name=f"chat-read-{job.job_id}", daemon=True)
        reader.start()
        try:
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if self._closed:
                        _kill_tree(proc)
            reader.join(timeout=READER_GRACE_SECONDS)       # 종료 직후 남은 출력을 비운다
            if reader.is_alive():
                abandoned.set()           # 파이프를 쥔 고아가 있다: 버리고 진행 (스레드는 데몬이라 앱 종료를 막지 않는다)
                _kill_tree(proc)
        except Exception:  # noqa: BLE001
            _kill_tree(proc)
            raise
        finally:
            timer.cancel()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
        for t in helpers:
            t.join(timeout=READER_GRACE_SECONDS)
        # 파이프 핸들을 남기지 않는다. 단, 아직 그 파이프를 읽거나 쓰는 중인 스레드가 있으면(고아 프로세스가 쥐고 있을 때)
        # close() 가 그 스레드의 잠금을 기다리며 영영 멈추므로 건드리지 않는다.
        busy = {id(proc.stdout): reader.is_alive(), id(proc.stderr): helpers[0].is_alive(),
                id(proc.stdin): any(h.is_alive() for h in helpers[1:])}
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if pipe is not None and not busy.get(id(pipe), False):
                    pipe.close()
            except (OSError, ValueError):
                pass
        stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace")[-STDERR_KEEP:]
        return self._outcome(job, st, rc, stderr_text, timed_out.is_set(), out)

    def _outcome(self, job: Job, st: _Stream, rc: int, stderr_text: str, timed_out: bool,
                 out: dict[str, Any]) -> dict[str, Any]:
        res = st.result or {}
        out["session_id"] = job.snapshot()["session_id"]
        if timed_out:
            out["error"] = f"처리 시간이 {int(self.timeout_seconds // 60) or 1}분을 넘어 중단했습니다."
            return out
        result_text = res.get("result") if isinstance(res.get("result"), str) else ""
        # 성공한 답변 안에 우연히 'not logged in' 같은 말이 있어도 오류로 오인하지 않도록, 실패로 보이는 때만 패턴을 본다
        errorish = st.result is None or bool(res.get("is_error")) or rc != 0
        haystack = "\n".join([result_text, stderr_text, st.text() if errorish else ""])
        out["login_error"] = st.login_error or (errorish and bool(_LOGIN_RE.search(haystack)))
        out["no_session"] = errorish and not out["login_error"] and bool(_NO_SESSION_RE.search(haystack))
        if out["login_error"]:
            job.set_text("")  # 'Invalid API key · Please run /login' 같은 CLI 문구를 Claude 의 답처럼 보이지 않게
            out["error"] = LOGIN_MESSAGE
            return out
        if st.result is not None:
            if res.get("is_error"):
                detail = result_text or str(res.get("subtype") or "")
                out["error"] = "Claude CLI 오류: " + (C.clean_text(redact(detail), 300) or "원인을 알 수 없습니다")
                return out
            if not job.snapshot()["text"].strip() and result_text:
                job.set_text(result_text)
            out["ok"] = True
            return out
        if rc != 0:
            tail = C.clean_text(redact(stderr_text), 300)
            out["error"] = f"Claude CLI 가 오류로 종료되었습니다 (종료 코드 {rc})." + (f" {tail}" if tail else "")
            return out
        if job.snapshot()["text"].strip():
            out["ok"] = True
            return out
        out["error"] = "Claude CLI 가 응답 없이 종료되었습니다."
        return out

    def _on_event(self, job: Job, st: _Stream, ev: dict[str, Any]) -> None:
        etype = ev.get("type")
        sid = ev.get("session_id")
        if etype == "system" and ev.get("subtype") == "init":
            st.saw_init = True
            if isinstance(sid, str) and sid:
                job.set_session(sid)
        elif etype == "stream_event":
            inner = ev.get("event")
            if isinstance(inner, dict):
                if inner.get("type") == "message_start":
                    st.partial = ""
                elif inner.get("type") == "content_block_delta":
                    delta = inner.get("delta")
                    if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                        st.partial += delta["text"]
                        job.set_text(st.text())
        elif etype == "assistant":
            if ev.get("parent_tool_use_id"):
                return  # 서브에이전트의 중간 말은 대표에게 보이지 않는다
            if ev.get("error") == "authentication_failed":
                st.login_error = True
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            texts: list[str] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name") if isinstance(block.get("name"), str) else "tool"
                    job.add_tool(name, tool_summary(name, block.get("input"), self.root))
            if texts:
                st.committed.append("".join(texts))
                st.partial = ""
                job.set_text(st.text())
        elif etype == "result":
            st.result = ev
            if isinstance(sid, str) and sid:
                job.set_session(sid)
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost):
                job.set_cost(float(cost))


# --------------------------------------------------------------------------
# HTTP 보조 (office_server.py 의 핸들러가 호출)
# --------------------------------------------------------------------------
def is_chat_path(path: str) -> bool:
    return path == "/api/chat" or path.startswith("/api/chat/")


def host_allowed(headers, port: int) -> bool:
    host = (headers.get("Host") or "").strip().lower()
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def origin_allowed(headers) -> bool:
    """Origin 헤더가 있으면 이 서버 자신(같은 Host)이어야 한다. 없으면(같은 출처 GET·주소창 이동) 통과."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    host = (headers.get("Host") or "").strip().lower()
    return origin.strip().lower() == f"http://{host}"


def may_receive_token(headers, port: int) -> bool:
    """'/' 페이지에 토큰을 실어 보내도 되는 요청인가. 다른 사이트가 <script>/<link>/fetch/iframe 으로 끌어가는 요청에는
    (읽지는 못하겠지만) 애초에 토큰을 싣지 않는다."""
    if not host_allowed(headers, port) or not origin_allowed(headers):
        return False
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
    dest = (headers.get("Sec-Fetch-Dest") or "").strip().lower()
    if site in ("cross-site", "same-site") and mode != "navigate":
        return False
    if dest in ("iframe", "frame", "embed", "object"):
        return False
    return True


def check_request(service: ChatService, headers, port: int) -> ChatError | None:
    """DNS 리바인딩·교차 출처·토큰 검사. 통과하면 None."""
    if not host_allowed(headers, port):
        return ChatError(403, "허용되지 않은 접속 주소(Host)입니다. 오피스 앱에서 직접 여세요.")
    if not origin_allowed(headers):
        return ChatError(403, "허용되지 않은 출처(Origin)입니다.")
    if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return ChatError(403, "다른 사이트에서 보낸 요청은 받을 수 없습니다.")
    supplied = (headers.get(TOKEN_HEADER) or "").encode("utf-8", "replace")
    if not secrets.compare_digest(supplied, service.token.encode("utf-8")):
        return ChatError(403, "인증 토큰이 없거나 올바르지 않습니다. 오피스 화면을 새로 고쳐 주세요.")
    return None


def read_post_body(headers, rfile) -> bytes:
    """POST 본문 검증·읽기: JSON 만, Content-Length 필수, 64KB 상한."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise ChatError(415, "Content-Type 은 application/json 이어야 합니다.")
    raw_len = headers.get("Content-Length")
    if raw_len is None or not raw_len.strip():
        raise ChatError(411, "Content-Length 가 필요합니다.")
    try:
        length = int(raw_len.strip())
    except ValueError:
        raise ChatError(400, "Content-Length 가 올바르지 않습니다.") from None
    if length < 0:
        raise ChatError(400, "Content-Length 가 올바르지 않습니다.")
    if length > MAX_BODY_BYTES:
        raise ChatError(413, f"요청이 너무 큽니다 (최대 {MAX_BODY_BYTES // 1024}KB).")
    body = rfile.read(length) if length else b""
    if len(body) < length:
        raise ChatError(400, "요청 본문이 끝까지 전달되지 않았습니다.")
    return body


def inject_token(html: bytes, token: str) -> bytes:
    return html.replace(TOKEN_PLACEHOLDER.encode("ascii"), token.encode("ascii"))


def normalize_message(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ChatError(400, "message 는 글자(문자열)여야 합니다.")
    text = raw.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8").strip()
    if not text:
        raise ChatError(400, "보낼 내용을 입력하세요.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ChatError(400, f"메시지는 {MAX_MESSAGE_CHARS:,}자 이하여야 합니다 (지금 {len(text):,}자).")
    return text


def dispatch(service: ChatService, method: str, path: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
    """인증이 끝난 /api/chat* 요청 -> (상태코드, JSON). 예외는 한국어 오류 JSON 으로 바꾼다."""
    try:
        p = path.rstrip("/")
        if p == "/api/chat":
            if method != "POST":
                return 405, {"error": "이 주소는 POST 만 받습니다."}
            data = _json_body(body)
            text = normalize_message(data.get("message"))
            model = data.get("model", DEFAULT_MODEL)
            if model is None:
                model = DEFAULT_MODEL
            if model not in MODELS:
                raise ChatError(400, "model 은 sonnet 또는 opus 여야 합니다.")
            job = service.start_job(text, model)
            return 202, {"job_id": job.job_id}
        if p == "/api/chat/history":
            if method != "GET":
                return 405, {"error": "이 주소는 GET 만 받습니다."}
            return 200, service.history_payload()
        if p == "/api/chat/reset":
            if method != "POST":
                return 405, {"error": "이 주소는 POST 만 받습니다."}
            service.reset()
            return 200, {"ok": True}
        if p.startswith("/api/chat/jobs/"):
            if method != "GET":
                return 405, {"error": "이 주소는 GET 만 받습니다."}
            job_id = p[len("/api/chat/jobs/"):]
            snap = service.job_snapshot(job_id) if _JOB_ID_RE.match(job_id) else None
            if snap is None:
                return 404, {"error": "해당 작업을 찾을 수 없습니다 (서버가 다시 시작됐을 수 있습니다)."}
            return 200, snap
        return 404, {"error": "찾을 수 없는 주소입니다."}
    except ChatError as exc:
        return exc.status, exc.payload()
    except Exception:  # noqa: BLE001 - 트레이스백은 절대 내보내지 않는다
        return 500, {"error": "내부 오류가 발생했습니다. 잠시 뒤 다시 시도하세요."}


def _json_body(body: bytes | None) -> dict[str, Any]:
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (ValueError, RecursionError):
        raise ChatError(400, "요청 본문이 올바른 JSON 이 아닙니다.") from None
    if not isinstance(data, dict):
        raise ChatError(400, "요청 본문은 JSON 객체여야 합니다.")
    return data
