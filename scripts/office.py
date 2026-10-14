#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""office.py - 오피스(대시보드) 상태 CLI.  규약: docs/90_시스템_인터페이스.md §2

  python office.py status <instance_id> <state> "<task>" [--bubble "💡"] [--product <id>] [--type status]
  python office.py event  <instance_id> <type>  "<msg>"  [--product <id>]
  python office.py reset  [--prune] [--no-event]     # 기본 직원 전원 idle
  python office.py show   [--json] [--events N]      # 현재 상태 표

state ∈ idle|working|meeting|blocked|done
type  ∈ status|handoff|decision|defect|approval_request|note
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


# --------------------------------------------------------------------------
# 검증
# --------------------------------------------------------------------------
def validate_instance_id(instance_id: str) -> str:
    iid = (instance_id or "").strip()
    m = C.INSTANCE_ID_RE.match(iid)
    if not m:
        raise C.UsageError(
            f"직원 ID '{instance_id}' 형식이 올바르지 않습니다. '역할', '역할-번호', 팀장은 '역할-lead' 형식(소문자)이어야 합니다. 예: ceo, maker-2, qa-1, qa-lead"
        )
    if m.group(1) not in C.ROLES:
        raise C.UsageError(
            f"알 수 없는 역할 '{m.group(1)}' (ID: {iid}). 가능한 역할: {', '.join(C.ROLES)}  (병렬 인스턴스는 maker-2 처럼 '-번호')"
        )
    if m.group(2) is not None and m.group(2).isdigit():  # qa-01 과 qa-1 이 서로 다른 직원(파일)이 되지 않게 번호 정규화
        iid = f"{m.group(1)}-{int(m.group(2))}"
    return iid


def validate_state(state: str) -> str:
    s = (state or "").strip().lower()
    if s not in C.STATES:
        raise C.UsageError(f"잘못된 state '{state}'. 가능한 값: {' | '.join(C.STATES)}")
    return s


def validate_type(type_: str) -> str:
    t = (type_ or "").strip().lower()
    if t not in C.EVENT_TYPES:
        raise C.UsageError(f"잘못된 type '{type_}'. 가능한 값: {' | '.join(C.EVENT_TYPES)}")
    return t


def validate_bubble(bubble: str | None, state: str, role: str) -> str:
    if bubble is None or not bubble.strip():
        if state == "working":
            return C.ROLES[role]["bubble"]
        return C.STATE_DEFAULT_BUBBLE[state]
    b = C.clean_text(bubble, 40)  # 개행·제어문자 제거
    if not b:
        return C.ROLES[role]["bubble"] if state == "working" else C.STATE_DEFAULT_BUBBLE[state]
    if C.bubble_len(b) > 6:
        raise C.UsageError(f"--bubble 은 이모지 1개 또는 6자 이하 짧은 텍스트여야 합니다 (받은 값: '{b}', {C.bubble_len(b)}자).")
    return b


def validate_product(product_id: str | None) -> str | None:
    if product_id is None or not str(product_id).strip():
        return None
    return C.validate_safe_id(str(product_id), "product_id")


# --------------------------------------------------------------------------
# 핵심 동작 (테스트에서도 직접 호출)
# --------------------------------------------------------------------------
def status_path(instance_id: str, root=None) -> Path:
    return C.status_dir(root) / f"{instance_id}.json"


def build_status(instance_id: str, state: str, task: str = "", bubble: str | None = None,
                 product_id: str | None = None) -> dict[str, Any]:
    iid = validate_instance_id(instance_id)
    st = validate_state(state)
    role = C.role_of(iid)
    record: dict[str, Any] = {
        "id": iid,
        "role": role,
        "state": st,
        "task": C.clean_text(task, 300) or C.STATE_DEFAULT_TASK[st],
        "bubble": validate_bubble(bubble, st, role),
    }
    pid = validate_product(product_id)
    if pid:
        record["product_id"] = pid
    record["updated_at"] = C.now_iso()
    return record


def write_status(record: dict[str, Any], root=None) -> Path:
    path = status_path(record["id"], root)
    C.atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return path


def append_event(record: dict[str, Any], root=None) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    C.append_bytes(C.events_path(root), line.encode("utf-8"))


def build_event(instance_id: str, type_: str, msg: str, state: str | None = None,
                product_id: str | None = None) -> dict[str, Any]:
    iid = validate_instance_id(instance_id)
    ev: dict[str, Any] = {
        "ts": C.now_iso(),
        "id": iid,
        "role": C.role_of(iid),
        "type": validate_type(type_),
    }
    if state:
        ev["state"] = state
    ev["msg"] = C.clean_text(msg, 1000)
    if not ev["msg"]:
        raise C.UsageError("이벤트 메시지(msg)가 비어 있습니다. 무엇이 일어났는지 한 줄로 적어 주세요.")
    pid = validate_product(product_id)
    if pid:
        ev["product_id"] = pid
    return ev


def set_status(instance_id: str, state: str, task: str = "", bubble: str | None = None,
               product_id: str | None = None, event_type: str = "status", root=None) -> dict[str, Any]:
    """상태 파일을 원자적으로 쓰고, 같은 내용을 events.jsonl 에 한 줄 남긴다."""
    etype = validate_type(event_type)
    record = build_status(instance_id, state, task, bubble, product_id)
    write_status(record, root)
    event = build_event(record["id"], etype, record["task"], state=record["state"], product_id=record.get("product_id"))
    event["ts"] = record["updated_at"]
    try:
        append_event(event, root)
    except OSError as exc:  # 상태 파일은 이미 갱신됨 -> 로그 실패는 경고만
        print(f"경고: events.jsonl 기록 실패 ({exc})", file=sys.stderr)
    return record


def log_event(instance_id: str, type_: str, msg: str, product_id: str | None = None, root=None) -> dict[str, Any]:
    event = build_event(instance_id, type_, msg, product_id=product_id)
    append_event(event, root)
    return event


def read_statuses(root=None) -> list[dict[str, Any]]:
    out = []
    sdir = C.status_dir(root)
    if not sdir.is_dir():
        return out
    for p in sdir.glob("*.json"):
        data = C.read_json_file(p)
        if isinstance(data, dict):
            data.setdefault("id", p.stem)
            out.append(data)
    out.sort(key=lambda d: C.instance_sort_key(str(d.get("id", ""))))
    return out


def reset_office(prune: bool = False, log: bool = True, root=None) -> list[str]:
    """기본 직원 전원 idle. 기본 직원이 아닌 기존 상태 파일은 idle 로 바꾸거나(--prune 이면 삭제)."""
    C.status_dir(root).mkdir(parents=True, exist_ok=True)
    ids = list(C.DEFAULT_STAFF)
    for p in sorted(C.status_dir(root).glob("*.json")):
        if p.stem in C.DEFAULT_STAFF:
            continue
        if prune:
            try:
                p.unlink()
            except OSError as exc:
                print(f"경고: {p.name} 삭제 실패 ({exc})", file=sys.stderr)
            continue
        if C.INSTANCE_ID_RE.match(p.stem) and C.role_of(p.stem) in C.ROLES:
            ids.append(p.stem)
    for iid in ids:
        write_status(build_status(iid, "idle"), root)
    if log:
        ev = build_event("ceo", "note", f"오피스 초기화: 직원 {len(ids)}명 대기 상태로 전환")
        try:
            append_event(ev, root)
        except OSError as exc:
            print(f"경고: events.jsonl 기록 실패 ({exc})", file=sys.stderr)
    return ids


# --------------------------------------------------------------------------
# 출력
# --------------------------------------------------------------------------
def _short_ts(ts: str) -> str:
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(ts).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts or "")


def render_show(statuses: list[dict[str, Any]], events: list[dict[str, Any]] | None = None) -> str:
    lines = [f"오피스 현황 - {C.now_kst().strftime('%Y-%m-%d %H:%M:%S')} KST  (직원 {len(statuses)}명)"]
    if not statuses:
        lines.append("(상태 파일이 없습니다. `python scripts/office.py reset` 으로 초기화하세요.)")
    else:
        rows = []
        for s in statuses:
            role = str(s.get("role") or C.role_of(str(s.get("id", ""))))
            room = C.ROLES.get(role, {}).get("room_label", "-")
            rows.append([s.get("id", ""), room, s.get("state", ""), s.get("bubble", ""),
                         s.get("task", ""), s.get("product_id", "") or "-", _short_ts(s.get("updated_at", ""))])
        lines.append(C.format_table(
            ["ID", "부서", "상태", "말풍선", "작업", "상품", "갱신"], rows,
            max_widths=[None, None, None, None, 44, 26, None]))
        counts = {st: sum(1 for s in statuses if s.get("state") == st) for st in C.STATES}
        lines.append("")
        lines.append("합계: " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    if events is not None:
        lines.append("")
        lines.append(f"최근 이벤트 {len(events)}건")
        if events:
            lines.append(C.format_table(
                ["시각", "ID", "유형", "내용"],
                [[_short_ts(e.get("ts", "")), e.get("id", ""), e.get("type", ""), e.get("msg", "")] for e in events],
                max_widths=[None, None, None, 70]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="office.py", description="오피스 상태/이벤트 CLI (직원의 유일한 공식 상태 변경 경로)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="직원 상태 변경 (상태 파일 + 이벤트 로그)")
    s.add_argument("id", help=f"직원 ID. 예: ceo, maker-2  (역할: {', '.join(C.ROLES)})")
    s.add_argument("state", help=" | ".join(C.STATES))
    s.add_argument("task", nargs="*", help="지금 하는 일(한 줄). 따옴표 없이 여러 단어를 줘도 한 문장으로 합친다")
    s.add_argument("--bubble", help="말풍선: 이모지 1개 또는 6자 이하 텍스트")
    s.add_argument("--product", help="관련 product_id")
    s.add_argument("--type", default="status", help="이벤트 유형 (기본 status): " + " | ".join(C.EVENT_TYPES))

    e = sub.add_parser("event", help="이벤트 로그 한 줄 추가")
    e.add_argument("id")
    e.add_argument("type", help=" | ".join(C.EVENT_TYPES))
    e.add_argument("msg", nargs="+", help="내용(한 줄). 따옴표 없이 여러 단어를 줘도 한 문장으로 합친다")
    e.add_argument("--product", help="관련 product_id")

    r = sub.add_parser("reset", help="기본 직원 전원 idle 로 초기화")
    r.add_argument("--prune", action="store_true", help="기본 직원이 아닌 상태 파일(maker-4 등)은 idle 대신 삭제")
    r.add_argument("--no-event", action="store_true", help="초기화 이벤트를 로그에 남기지 않음")

    w = sub.add_parser("show", help="현재 상태 표 출력")
    w.add_argument("--json", action="store_true", help="JSON 으로 출력")
    w.add_argument("--events", type=int, metavar="N", default=0, help="최근 이벤트 N건도 출력")
    return p


def run(args: argparse.Namespace) -> int:
    if args.cmd == "status":
        rec = set_status(args.id, args.state, " ".join(args.task), args.bubble, args.product, args.type)
        print(f"[{rec['id']}] {rec['state']} {rec['bubble']}  {rec['task']}")
    elif args.cmd == "event":
        ev = log_event(args.id, args.type, " ".join(args.msg), args.product)
        print(f"[{ev['id']}] {ev['type']}: {ev['msg']}")
    elif args.cmd == "reset":
        ids = reset_office(prune=args.prune, log=not args.no_event)
        print(f"오피스 초기화 완료: {len(ids)}명 idle ({', '.join(ids)})")
    elif args.cmd == "show":
        statuses = read_statuses()
        events = C.read_jsonl_tail(C.events_path(), args.events) if args.events > 0 else None
        if args.json:
            print(json.dumps({"agents": statuses, **({"events": events} if events is not None else {})},
                             ensure_ascii=False, indent=2))
        else:
            print(render_show(statuses, events))
    return 0


def main(argv: list[str] | None = None) -> int:
    C.force_utf8()
    try:
        args = C.parse_cli(build_parser(), argv)
        return run(args)
    except C.CliError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        C.quiet_broken_pipe()
        return 0
    except OSError as exc:
        print(f"오류: {C.describe_oserror(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
