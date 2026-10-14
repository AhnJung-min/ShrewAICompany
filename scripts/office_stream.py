"""오피스 실시간 스트림 (Server-Sent Events).

GET /api/stream 으로 연결하면, 직원 상태 파일·이벤트 로그·트래커 CSV 가 바뀌는 즉시
`event: state` 로 /api/state 와 같은 JSON 을 보낸다. 2초 폴링 대신 쓰며, 끊기면 브라우저
EventSource 가 알아서 다시 붙는다(화면은 그동안 폴링으로 버틴다).

- 변화 감지는 파일 목록·크기·수정 시각의 지문(signature)을 POLL_S 간격으로 비교한다(표준 라이브러리만).
- 변화가 없어도 KEEPALIVE_S 마다 주석 줄을 보내 연결이 끊기지 않게 한다.
- 동시에 붙는 화면 수는 MAX_CLIENTS 로 제한한다(스레드 하나씩 쓰므로).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import _common as C

POLL_S = 0.2          # 변화 감지 간격 (초)
KEEPALIVE_S = 15.0    # 아무 변화 없을 때 연결 유지용 주석 간격
MAX_CLIENTS = 8
RETRY_MS = 1500       # 끊겼을 때 브라우저가 다시 붙는 간격

_clients = 0
_clients_lock = threading.Lock()


def signature(root: Path | str | None = None) -> tuple:
    """상태를 바꿀 수 있는 파일들의 (이름, 크기, 수정시각) 묶음. 하나라도 바뀌면 값이 달라진다."""
    root = Path(root) if root is not None else C.get_root()
    parts: list[tuple] = []
    targets: list[Path] = []
    try:
        targets.extend(sorted(C.status_dir(root).glob("*.json")))
    except OSError:
        pass
    targets.append(C.events_path(root))
    targets.extend([C.products_csv(root), C.ledger_csv(root), C.approvals_csv(root)])
    for p in targets:
        try:
            st = p.stat()
            parts.append((p.name, st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append((p.name, -1, -1))
    return tuple(parts)


def _acquire() -> bool:
    global _clients
    with _clients_lock:
        if _clients >= MAX_CLIENTS:
            return False
        _clients += 1
        return True


def _release() -> None:
    global _clients
    with _clients_lock:
        _clients = max(0, _clients - 1)


def serve(handler: Any, root: Path, build_state: Callable[[Path], dict], stop: threading.Event | None = None) -> None:
    """BaseHTTPRequestHandler 안에서 호출한다. 연결이 끊기거나 서버가 멈출 때까지 돈다."""
    if not _acquire():
        body = json.dumps({"error": "실시간 연결이 너무 많습니다. 잠시 후 다시 시도하세요."}, ensure_ascii=False).encode("utf-8")
        handler.send_response(503)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")   # Cache-Control 은 서버 end_headers 가 붙인다
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        handler.wfile.write(f"retry: {RETRY_MS}\n\n".encode("utf-8"))
        handler.wfile.flush()

        last_sig = None
        last_send = 0.0
        while not (stop and stop.is_set()):
            sig = signature(root)
            now = time.monotonic()
            if sig != last_sig:
                try:
                    data = json.dumps(build_state(root), ensure_ascii=False, separators=(",", ":"))
                except Exception as exc:  # noqa: BLE001 - 상태를 못 읽어도 연결은 유지
                    data = json.dumps({"error": f"상태를 읽지 못했습니다: {type(exc).__name__}"}, ensure_ascii=False)
                handler.wfile.write(("event: state\ndata: " + data + "\n\n").encode("utf-8"))
                handler.wfile.flush()
                last_sig, last_send = sig, now
            elif now - last_send >= KEEPALIVE_S:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                last_send = now
            time.sleep(POLL_S)
    except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
        pass  # 화면을 닫거나 새로고침하면 연결이 끊긴다 — 정상
    finally:
        _release()
