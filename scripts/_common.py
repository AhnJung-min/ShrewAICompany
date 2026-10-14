# -*- coding: utf-8 -*-
"""AI Revenue Engine - 공용 헬퍼 (Python 3.12 표준 라이브러리만 사용).

office.py / office_server.py / tracker.py 가 함께 쓰는 상수·경로·파일 입출력·
표 출력·장부 집계 로직을 한곳에 모았다. 규약: docs/90_시스템_인터페이스.md
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as _dt
import decimal
import io
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

# --------------------------------------------------------------------------
# 시간 / 경로
# --------------------------------------------------------------------------
KST = _dt.timezone(_dt.timedelta(hours=9), "KST")


def now_kst() -> _dt.datetime:
    return _dt.datetime.now(KST).replace(microsecond=0)


def now_iso() -> str:
    """예: 2026-10-15T14:03:22+09:00"""
    return now_kst().isoformat()


def today_kst() -> _dt.date:
    return now_kst().date()


def get_root() -> Path:
    """ROOT = 환경변수 AIRE_ROOT, 없으면 scripts/ 의 부모. (호출 시점에 평가)"""
    env = os.environ.get("AIRE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _root(root: Path | str | None) -> Path:
    return Path(root) if root is not None else get_root()


def office_dir(root=None) -> Path:
    return _root(root) / "office"


def status_dir(root=None) -> Path:
    return office_dir(root) / "status"


def events_path(root=None) -> Path:
    return office_dir(root) / "events.jsonl"


def tracker_dir(root=None) -> Path:
    """트래커 CSV 폴더.

    매출 장부는 공개 저장소에 두지 않는다(오너 결정 2026-10-15): 비공개 저장소인
    products/ 안의 _private/tracker 가 있으면 그곳을, 없으면(테스트·새 클론) tracker/ 를 쓴다.
    AIRE_TRACKER_DIR 로 직접 지정할 수도 있다.
    """
    env = os.environ.get("AIRE_TRACKER_DIR")
    if env:
        return Path(env).expanduser().resolve()
    r = _root(root)
    private = r / "products" / "_private" / "tracker"
    return private if private.is_dir() else r / "tracker"


def products_csv(root=None) -> Path:
    return tracker_dir(root) / "products.csv"


def ledger_csv(root=None) -> Path:
    return tracker_dir(root) / "ledger.csv"


def approvals_csv(root=None) -> Path:
    return tracker_dir(root) / "approvals.csv"


def force_utf8() -> None:
    """Windows 콘솔/파이프에서도 한글이 깨지지 않도록 stdout/stderr 를 UTF-8 로."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


# --------------------------------------------------------------------------
# 규약 상수
# --------------------------------------------------------------------------
ROLES: dict[str, dict[str, str]] = {
    "ceo": {"title": "대표", "room": "ceo", "room_label": "대표실 (CEO)", "bubble": "📋"},
    "researcher": {"title": "리서처", "room": "research", "room_label": "리서치 (Research)", "bubble": "🔍"},
    "pm": {"title": "기획자", "room": "designing", "room_label": "기획·디자인 (Designing)", "bubble": "📝"},
    "designer": {"title": "디자이너", "room": "designing", "room_label": "기획·디자인 (Designing)", "bubble": "🎨"},
    "maker": {"title": "제작자", "room": "coding", "room_label": "제작 (Coding)", "bubble": "💻"},
    "qa": {"title": "검수자", "room": "testing", "room_label": "검수 (Testing)", "bubble": "🧪"},
    "marketer": {"title": "마케터", "room": "marketing", "room_label": "마케팅 (Marketing)", "bubble": "📣"},
    "cfo": {"title": "재무·컴플라이언스", "room": "finance", "room_label": "재무·법무 (Finance)", "bubble": "💰"},
}
ROLE_ORDER = list(ROLES)

DEFAULT_STAFF = [
    "ceo", "researcher", "pm", "designer",
    "maker-1", "maker-2", "maker-3",
    "qa-1", "qa-2", "marketer", "cfo",
    "pm-lead", "maker-lead", "qa-lead", "marketer-lead",   # 팀장 (Opus)
]

STATES = ("idle", "working", "meeting", "blocked", "done")
EVENT_TYPES = ("status", "handoff", "decision", "defect", "approval_request", "note")

STATE_DEFAULT_TASK = {
    "idle": "대기 중",
    "working": "작업 중",
    "meeting": "회의 중",
    "blocked": "막힘 - 확인 필요",
    "done": "작업 완료",
}
STATE_DEFAULT_BUBBLE = {"idle": "💤", "meeting": "💬", "blocked": "⛔", "done": "✅"}

PRODUCT_STATUSES = ("draft", "in_production", "qa_failed", "qa_passed", "awaiting_owner", "listed", "retired")
LEDGER_TYPES = ("sale", "fee", "refund", "expense", "payout")
APPROVAL_STATUSES = ("pending", "approved", "rejected")

PRODUCT_COLS = ["id", "batch", "title", "model", "platform", "price_krw", "status", "created_at", "listed_url", "notes"]
LEDGER_COLS = ["date", "product_id", "platform", "type", "amount_krw", "note"]
APPROVAL_COLS = ["id", "product_id", "action", "requested_at", "status", "decided_at", "note"]

# '역할', '역할-번호', 팀장은 '역할-lead' (docs/11_팀_체계.md)
INSTANCE_ID_RE = re.compile(r"^([a-z][a-z0-9_]*)(?:-(\d{1,3}|lead))?$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class CliError(Exception):
    """사용자에게 보여줄 메시지를 가진 오류. code 는 프로세스 종료 코드."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class UsageError(CliError):
    def __init__(self, message: str):
        super().__init__(message, 2)


def role_of(instance_id: str) -> str:
    """maker-2 -> maker (대시보드가 방을 정하는 prefix)."""
    return instance_id.split("-", 1)[0]


def instance_sort_key(instance_id: str) -> tuple[int, int, str]:
    role = role_of(instance_id)
    order = ROLE_ORDER.index(role) if role in ROLES else len(ROLE_ORDER)
    m = INSTANCE_ID_RE.match(instance_id)
    suffix = m.group(2) if m else None
    num = -1 if suffix == "lead" else int(suffix) if suffix else 0   # 팀장이 같은 역할의 팀원보다 앞
    return (order, num, instance_id)


# --------------------------------------------------------------------------
# 안전한 파일 쓰기
# --------------------------------------------------------------------------
_RETRY_WINERRORS = {5, 32, 33}  # ACCESS_DENIED / SHARING_VIOLATION / LOCK_VIOLATION
_win_state: dict[str, Any] = {"k32": None, "broken": False}


def _is_transient(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _RETRY_WINERRORS


def _kernel32():
    k = _win_state["k32"]
    if k is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.WriteFile.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        k.WriteFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _win_state["k32"] = k
    return k


def _win_append(path: str, data: bytes) -> None:
    """FILE_APPEND_DATA 전용 핸들로 열어 쓰기 -> OS 가 EOF 위치 지정까지 원자적으로 처리.

    (CRT 의 O_APPEND 는 'EOF 로 seek 후 write' 두 단계라 병렬 프로세스 사이에서 줄이 덮어써질 수 있다.)
    """
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    FILE_APPEND_DATA = 0x0004
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_NORMAL = 0x80
    invalid = ctypes.c_void_p(-1).value
    h = k.CreateFileW(path, FILE_APPEND_DATA, SHARE_ALL, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if h in (None, 0, invalid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            chunk = bytes(view[offset:])
            written = wintypes.DWORD(0)
            if not k.WriteFile(h, chunk, len(chunk), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            offset += written.value
    finally:
        k.CloseHandle(h)


def _posix_append(path: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o666)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            offset += os.write(fd, view[offset:])
    finally:
        os.close(fd)


def append_bytes(path: Path | str, data: bytes, retries: int = 25) -> None:
    """여러 프로세스가 동시에 호출해도 안전한 한 번의 append (+ PermissionError 재시도)."""
    path = os.path.abspath(os.fspath(path))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    delay = 0.01
    for attempt in range(retries):
        try:
            if os.name == "nt" and not _win_state["broken"]:
                try:
                    _win_append(path, data)
                    return
                except OSError:
                    raise
                except Exception:  # ctypes 초기화 문제 등 -> 이식 가능한 방식으로 폴백
                    _win_state["broken"] = True
            _posix_append(path, data)
            return
        except OSError as exc:
            if not _is_transient(exc) or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, 0.25)


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8", retries: int = 60) -> None:
    """임시 파일에 쓴 뒤 os.replace. 읽는 쪽이 반쯤 쓰인 파일을 볼 수 없다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        delay = 0.01
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:  # Windows: 누군가 대상 파일을 열고 있으면 PermissionError
                if not _is_transient(exc) or attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def file_lock(path: Path | str, timeout: float = 15.0, stale: float = 30.0) -> Iterator[None]:
    """<path>.lock 파일 기반의 단순 상호배제 (read-modify-write 구간 보호)."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise CliError(f"파일 잠금을 얻지 못했습니다(다른 프로세스가 사용 중): {lock}")
            time.sleep(0.03)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        for _ in range(20):
            try:
                lock.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.02)


# --------------------------------------------------------------------------
# 값 파싱
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
# 날짜 뒤에는 끝/공백/T 만 허용: '2026-10-1599', '2026-10-15abc' 같은 오타를 조용히 받아들이지 않는다.
_DATE_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?=$|[T\s])")
_DATE8_RE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")
# 제어문자(탭·개행·\x1c-\x1f 는 제외: 공백으로 접힘)
_CTRL_RE = re.compile("[\x00-\x08\x0e-\x1b\x7f]")


def parse_int(value: Any) -> int | None:
    """'12,000' / '₩12000' / '12000원' / '1500.0' -> int (큰 수도 정확히). 실패하면 None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().replace(",", "").replace("₩", "").replace("원", "").replace(" ", "")
    if not _NUM_RE.match(s):
        return None
    try:
        if "." not in s:
            return int(s)
        return int(decimal.Decimal(s).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))
    except (ValueError, ArithmeticError):
        return None


def parse_date(value: Any) -> _dt.date | None:
    if value is None:
        return None
    s = str(value)
    m = _DATE_RE.match(s) or _DATE8_RE.match(s)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def month_key(d: _dt.date | None) -> str:
    return f"{d.year:04d}-{d.month:02d}" if d else ""


def clean_text(value: Any, maxlen: int = 300) -> str:
    """개행·연속 공백을 한 칸으로, 제어문자·짝 없는 서로게이트는 제거, 너무 길면 말줄임."""
    s = str(value if value is not None else "")
    s = _CTRL_RE.sub("", s)
    s = s.encode("utf-8", "ignore").decode("utf-8")  # 짝 없는 서로게이트 제거(UTF-8 저장 시 예외 방지)
    s = " ".join(s.split())
    if len(s) > maxlen:
        s = s[: maxlen - 1].rstrip() + "…"
    return s


def validate_safe_id(value: str, what: str = "ID") -> str:
    v = (value or "").strip()
    if not SAFE_ID_RE.match(v):
        raise UsageError(f"{what} '{value}' 형식이 올바르지 않습니다. 영문/숫자/./_/- 만 쓰고 80자 이하, 영문·숫자로 시작해야 합니다. 예: notion-job-hunt-kit")
    return v


def bubble_len(text: str) -> int:
    """말풍선 길이: ZWJ·변형선택자·피부톤 같은 이모지 결합 문자는 세지 않는다."""
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0x200D or 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF or unicodedata.combining(ch):
            continue
        n += 1
    return n


# --------------------------------------------------------------------------
# JSON / JSONL 읽기
# --------------------------------------------------------------------------
def read_text_retry(path: Path | str, encoding: str = "utf-8-sig", attempts: int = 4) -> str | None:
    for i in range(attempts):
        try:
            with open(path, "r", encoding=encoding, errors="replace") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            if i == attempts - 1:
                return None
            time.sleep(0.01 * (i + 1))
    return None


def json_loads_safe(text: str | bytes) -> Any | None:
    """깨진 JSON 이나 지나치게 깊게 중첩된 JSON(RecursionError)도 예외 없이 None."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def read_json_file(path: Path | str) -> Any | None:
    text = read_text_retry(path)
    if text is None:
        return None
    return json_loads_safe(text)


def json_safe(obj: Any, _depth: int = 0) -> Any:
    """NaN/Infinity(표준 JSON 이 아님 -> 브라우저 JSON.parse 실패)를 None 으로 바꾼다."""
    if _depth > 50:
        return None
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v, _depth + 1) for v in obj]
    return obj


def read_jsonl_tail(path: Path | str, limit: int) -> list[dict]:
    """JSONL 마지막 limit 개의 dict 줄(손상된 줄은 건너뜀). 파일 순서 유지(최신이 마지막)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    chunk = 256 * 1024
    while True:
        start = max(0, size - chunk)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                raw = fh.read()
        except OSError:
            return []
        lines = raw.split(b"\n")
        if start > 0:
            lines = lines[1:]  # 잘린 첫 줄 버림
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            obj = json_loads_safe(line.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                out.append(obj)
        if len(out) >= limit or start == 0:
            return out[-limit:] if limit > 0 else []
        chunk *= 4


# --------------------------------------------------------------------------
# CSV (UTF-8 with BOM)
# --------------------------------------------------------------------------
_BOM = b"\xef\xbb\xbf"
_CSV_MAX_SPAN = 12  # 따옴표가 열린 채 이어지는 칸을 최대 몇 줄까지 한 레코드로 볼지


def read_bytes_retry(path: Path | str, attempts: int = 4) -> bytes | None:
    for i in range(attempts):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            if i == attempts - 1:
                return None
            time.sleep(0.01 * (i + 1))
    return None


def decode_csv_bytes(raw: bytes) -> str:
    """CSV 바이트 -> 문자열. UTF-8(BOM 유무 무관) 우선, 한국어 엑셀의 'CSV(쉼표로 분리)' 저장본(CP949)과 UTF-16 도 읽는다."""
    if raw.startswith(_BOM):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp949")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def read_csv_text(path: Path | str) -> str | None:
    raw = read_bytes_retry(path)
    return None if raw is None else decode_csv_bytes(raw)


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _iter_csv_records(text: str) -> Iterator[list[str]]:
    """CSV 레코드(셀 목록)를 차례로 낸다.

    표준 csv 모듈은 짝이 안 맞는 따옴표 하나가 뒤의 모든 행을 한 칸으로 삼켜 버린다.
    여기서는 strict 모드로 읽다가 (1) 따옴표가 끝내 닫히지 않거나 (2) 여러 줄에 걸친 칸이
    헤더의 열 개수와 맞지 않으면, 그 줄 하나만 따옴표 없이 읽고 다음 줄부터 다시 시작한다.
    """
    lines = io.StringIO(text, newline="").readlines()
    i, n = 0, len(lines)
    ncols: int | None = None
    while i < n:
        window = lines[i:i + _CSV_MAX_SPAN]
        cells: list[str] | None = None
        used = 1
        try:
            rd = csv.reader(window, strict=True)
            cells = next(rd)
            used = max(rd.line_num, 1)
        except StopIteration:
            cells = []
        except csv.Error:
            cells = None
        if cells is not None and used > 1 and (ncols is None or len(cells) != ncols):
            cells = None
        if cells is None:  # 복구: 이 줄만, 따옴표를 글자 그대로
            used = 1
            try:
                cells = next(csv.reader([lines[i]], quoting=csv.QUOTE_NONE))
                cells = [c[1:] if c.startswith('"') and c.count('"') == 1 else c for c in cells]
            except (csv.Error, StopIteration):
                cells = []
        i += used
        if ncols is None and any(c.strip() for c in cells):
            ncols = len(cells)
        yield cells


def read_csv_rows(path: Path | str) -> list[dict[str, str]]:
    """CSV -> dict 행 목록 (헤더 이름은 소문자로 정규화).

    파일 없음/빈 파일/깨진 행은 조용히 건너뛰고, 따옴표 오타 하나가 뒤의 행을 삼키지 않는다.
    BOM 유무와 UTF-8/CP949/UTF-16 모두 허용.
    """
    text = read_csv_text(path)
    if not text:
        return []
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for raw in _iter_csv_records(text):
        if not any(c.strip() for c in raw):
            continue
        if header is None:
            header = [c.strip().lower() for c in raw]
            continue
        cells = [c.strip() for c in raw]
        if len(cells) > len(header) and header:  # 따옴표 없이 쉼표가 들어간 마지막 열 복구
            cells = cells[: len(header) - 1] + [",".join(cells[len(header) - 1:])]
        row = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header) if h}
        rows.append(row)
    return rows


def _csv_line(cells: Sequence[Any]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(["" if c is None else c for c in cells])
    return buf.getvalue()


def csv_safe_cell(col: str, value: Any) -> Any:
    """엑셀 수식 주입 방지: 글 칸이 = @ 탭 으로(또는 +/- 뒤에 공백 없이 글자가) 시작하면 앞에 ' 를 붙인다.

    금액 열(*_krw)과 순수 숫자('-990')는 그대로 둔다.
    """
    if not isinstance(value, str) or not value or col.endswith("_krw"):
        return value
    if value[0] in "=@\t\r":
        return "'" + value
    if value[0] in "+-" and len(value) > 1 and not value[1].isspace() and parse_int(value) is None:
        return "'" + value
    return value


def _row_cells(header: Sequence[str], row: dict[str, Any]) -> list[Any]:
    return [csv_safe_cell(h, row.get(h, "")) for h in header]


def csv_text(header: Sequence[str], rows: Sequence[dict[str, Any]]) -> str:
    out = _csv_line(header)
    for r in rows:
        out += _csv_line(_row_cells(header, r))
    return out


def _csv_is_blank(path: Path | str) -> bool:
    """내용이 BOM/공백/빈 줄뿐인 파일(엑셀이 빈 시트를 저장한 경우 등)."""
    raw = read_bytes_retry(path)
    if raw is None:
        return True
    if len(raw) > 4096:
        return False
    return decode_csv_bytes(raw).strip() == ""


def ensure_csv(path: Path | str, header: Sequence[str]) -> bool:
    """없거나 비어 있으면(공백뿐인 파일 포함) BOM+헤더로 생성. 새로 만들었으면 True."""
    path = Path(path)
    if path.is_dir():
        raise CliError(f"{path} 는 폴더입니다. CSV 파일이어야 합니다.")
    if path.exists() and path.stat().st_size > 0 and not _csv_is_blank(path):
        return False
    atomic_write_text(path, _csv_line(header), "utf-8-sig")
    return True


def check_csv_header(path: Path | str, header: Sequence[str]) -> None:
    """기존 파일의 헤더가 계약과 같은지 확인(다르면 데이터가 밀리므로 중단). 대소문자는 무시."""
    text = read_csv_text(path) or ""
    first = ""
    for line in io.StringIO(text, newline="").readlines():
        if line.strip():
            first = line.strip("\r\n")
            break
    try:
        found = next(csv.reader([first]))
    except (StopIteration, csv.Error):
        found = []
    if [c.strip().lower() for c in found] != [h.lower() for h in header]:
        raise CliError(f"{path} 의 헤더가 계약과 다릅니다.\n  기대: {','.join(header)}\n  실제: {first[:200]}\n수동으로 맞춘 뒤 다시 실행하세요.")


def append_csv_row(path: Path | str, header: Sequence[str], row: dict[str, Any]) -> None:
    ensure_csv(path, header)
    check_csv_header(path, header)
    raw = read_bytes_retry(path) or b""
    if raw and (not raw.startswith(_BOM) or not _is_utf8(raw)):
        # BOM 없는 파일/CP949 로 저장된 파일에 UTF-8 줄을 이어 붙이면 인코딩이 섞인다 -> UTF-8 BOM 으로 통째로 변환
        rows = read_csv_rows(path)
        rows.append(row)
        rewrite_csv(path, header, rows)
        return
    prefix = b"" if (not raw or raw[-1:] in (b"\n", b"\r")) else b"\r\n"
    line = _csv_line(_row_cells(header, row)).encode("utf-8")
    append_bytes(path, prefix + line)


def rewrite_csv(path: Path | str, header: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    atomic_write_text(path, csv_text(header, rows), "utf-8-sig")


# --------------------------------------------------------------------------
# CLI 보조: 대시( - )로 시작하는 글 인자, 파일 잠김 안내, 파이프 종료
# --------------------------------------------------------------------------
_DASH_MARK = ""  # argparse 가 옵션으로 오해하지 않게 앞에 붙였다가 파싱 뒤에 제거하는 표식
_NEG_NUM_RE = re.compile(r"^-\d+(?:\.\d+)?$")


def _collect_option_strings(parser: argparse.ArgumentParser, out: set[str]) -> None:
    for action in parser._actions:  # noqa: SLF001 - argparse 공개 API 에는 목록 조회가 없다
        out.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for sub in action.choices.values():
                _collect_option_strings(sub, out)


def protect_dash_text(parser: argparse.ArgumentParser, argv: Sequence[str]) -> list[str]:
    """'-5%', '-3000원', '-긴급' 처럼 대시 하나로 시작하고 두 번째 글자가 ASCII 영문이 아닌 인자는
    옵션이 아니라 '글'로 취급한다 (--note -3000원 이 'expected one argument' 로 실패하지 않게).
    순수 음수('-9900')와 알려진 옵션, '--' 로 시작하는 인자는 그대로 둔다."""
    known: set[str] = set()
    _collect_option_strings(parser, known)
    out: list[str] = []
    for tok in argv:
        if (len(tok) > 1 and tok[0] == "-" and tok[1] != "-" and tok not in known
                and not (tok[1].isascii() and tok[1].isalpha()) and not _NEG_NUM_RE.match(tok)):
            tok = _DASH_MARK + tok
        out.append(tok)
    return out


def restore_dash_text(args: argparse.Namespace) -> argparse.Namespace:
    for key, val in list(vars(args).items()):
        if isinstance(val, str):
            setattr(args, key, val.replace(_DASH_MARK, ""))
        elif isinstance(val, list):
            setattr(args, key, [v.replace(_DASH_MARK, "") if isinstance(v, str) else v for v in val])
    return args


def parse_cli(parser: argparse.ArgumentParser, argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    return restore_dash_text(parser.parse_args(protect_dash_text(parser, raw)))


def describe_oserror(exc: OSError) -> str:
    """파일을 못 쓴 이유를 사용자가 바로 조치할 수 있게 설명."""
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _RETRY_WINERRORS:
        target = getattr(exc, "filename2", None) or getattr(exc, "filename", None) or ""  # replace 의 목적지 파일 우선
        return ("다른 프로그램(엑셀·메모장 등)이 파일을 열고 있어 쓸 수 없습니다. 그 프로그램에서 파일을 닫은 뒤 다시 실행하세요."
                + (f"\n  대상: {target}" if target else ""))
    return f"파일 입출력 실패 - {exc}"


def quiet_broken_pipe() -> None:
    """`... | head` 처럼 출력 파이프가 먼저 닫혀도 트레이스백 없이 끝낸다."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 표 출력 (한글/이모지 폭 보정)
# --------------------------------------------------------------------------
def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate_width(text: str, max_w: int) -> str:
    if disp_width(text) <= max_w:
        return text
    out, w = "", 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > max_w - 1:
            break
        out += ch
        w += cw
    return out + "…"


def pad_cell(text: str, width: int, align: str = "l") -> str:
    gap = width - disp_width(text)
    if gap <= 0:
        return text
    return text + " " * gap if align == "l" else " " * gap + text


def _cell(v: Any) -> str:
    return "" if v is None else str(v).replace("\r", " ").replace("\n", " ")


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    aligns: Sequence[str] | None = None,
    max_widths: Sequence[int | None] | None = None,
) -> str:
    n = len(headers)
    aligns = list(aligns or ["l"] * n)
    max_widths = list(max_widths or [None] * n)
    body: list[list[str]] = []
    for r in rows:
        cells = []
        for i in range(n):
            c = _cell(r[i] if i < len(r) else "")
            if max_widths[i]:
                c = truncate_width(c, max_widths[i])  # type: ignore[arg-type]
            cells.append(c)
        body.append(cells)
    widths = [max([disp_width(headers[i])] + [disp_width(r[i]) for r in body]) for i in range(n)]
    lines = ["  ".join(pad_cell(headers[i], widths[i], aligns[i]) for i in range(n)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(n)))
    for r in body:
        lines.append("  ".join(pad_cell(r[i], widths[i], aligns[i]) for i in range(n)).rstrip())
    return "\n".join(lines)


def fmt_krw(n: int | float | None) -> str:
    return f"{int(n or 0):,}"


# --------------------------------------------------------------------------
# 장부 집계 (office_server 의 KPI 와 tracker report 가 같은 계산을 쓴다)
# --------------------------------------------------------------------------
def load_ledger(root=None) -> list[dict[str, Any]]:
    """ledger.csv -> 유효 행 목록. type/금액이 잘못된 행은 건너뛴다."""
    out: list[dict[str, Any]] = []
    for r in read_csv_rows(ledger_csv(root)):
        t = (r.get("type") or "").strip().lower()
        if t not in LEDGER_TYPES:
            continue
        amount = parse_int(r.get("amount_krw"))
        if amount is None:
            continue
        d = parse_date(r.get("date"))
        out.append({
            "date": d.isoformat() if d else "",
            "month": month_key(d),
            "product_id": r.get("product_id", ""),
            "platform": r.get("platform", ""),
            "type": t,
            "amount": amount,
            "note": r.get("note", ""),
        })
    return out


def new_bucket() -> dict[str, int]:
    return {"sales": 0, "refunds": 0, "fees": 0, "expenses": 0, "payouts": 0, "count": 0}


def bucket_add(bucket: dict[str, int], entry: dict[str, Any]) -> None:
    """sale 은 부호 그대로(양수가 정상), refund/fee/expense/payout 은 크기(절댓값)로 합산 -> 입력 부호 관례에 둔감."""
    t, a = entry["type"], entry["amount"]
    if t == "sale":
        bucket["sales"] += a
    elif t == "refund":
        bucket["refunds"] += abs(a)
    elif t == "fee":
        bucket["fees"] += abs(a)
    elif t == "expense":
        bucket["expenses"] += abs(a)
    elif t == "payout":
        bucket["payouts"] += abs(a)
    bucket["count"] += 1


def bucket_totals(bucket: dict[str, int]) -> dict[str, int]:
    """revenue = 판매 - 환불, expenses = 수수료 + 비용, net = revenue - expenses (정산입금은 제외)."""
    revenue = bucket["sales"] - bucket["refunds"]
    expenses = bucket["fees"] + bucket["expenses"]
    return {**bucket, "revenue": revenue, "costs": expenses, "net": revenue - expenses}
