#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tracker.py - 상품·장부·승인 대기열 트래커 (CSV, UTF-8 with BOM).  규약: docs/90_시스템_인터페이스.md §3

  python tracker.py init
  python tracker.py add-product --id notion-job-hunt-kit --title "..." [--batch batch-001] [--model digital] [--platform kmong] [--price-krw 9900] [--status draft] [--notes "..."] [--upsert]
  python tracker.py set-status <product_id> <status> [--note "..."] [--listed-url URL] [--price-krw N]
  python tracker.py add-ledger --type sale --amount 9900 [--date 2026-10-15] [--product-id ID] [--platform kmong] [--note "..."]
  python tracker.py request-approval --product-id ID --action publish_listing [--note "..."]
  python tracker.py approve <approval_id> --i-am-owner [--note "..."]      # 오너 전용
  python tracker.py reject  <approval_id> --i-am-owner [--note "..."]      # 오너 전용
  python tracker.py list [products|approvals|ledger|all] [--status S] [--type T] [--product ID] [--limit N] [--full]
  python tracker.py report [--month YYYY-MM] [--md | --json]

AI 직원은 approve/reject 를 쓰지 않는다 (승인은 오너 본인만).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

OWNER_EXIT_CODE = 3
MAX_KRW = 100_000_000_000  # 1000억 원 - 이보다 큰 금액은 자릿수 오타로 보고 거절
NOTES_MAX = 1500           # notes 칸이 상태 변경마다 끝없이 길어지지 않게 (오래된 앞부분부터 버림)
URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

OWNER_ONLY_NOTICE = """\
[거부] '{verb}' 는 오너(대표님) 전용 작업입니다.
  - AI 직원은 승인 대기열에 'pending' 요청을 올리는 것(request-approval)까지만 할 수 있고,
    approved / rejected 로 바꾸는 것은 사람인 오너만 합니다.
  - 오너 본인이 터미널에서 직접 실행할 때만 --i-am-owner 플래그를 붙이세요:
      python scripts/tracker.py {verb} <승인ID> --i-am-owner
  - AI 에이전트는 이 플래그를 절대 붙이지 않습니다."""


def warn(msg: str) -> None:
    print(f"경고: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# 파일 준비
# --------------------------------------------------------------------------
def _files(root=None):
    return [
        (C.products_csv(root), C.PRODUCT_COLS),
        (C.ledger_csv(root), C.LEDGER_COLS),
        (C.approvals_csv(root), C.APPROVAL_COLS),
    ]


def init_files(root=None) -> list[tuple[Path, bool]]:
    C.tracker_dir(root).mkdir(parents=True, exist_ok=True)
    result = []
    for path, header in _files(root):
        with C.file_lock(path):
            created = C.ensure_csv(path, header)
        result.append((path, created))
    return result


def _prepare(path: Path, header: list[str]) -> None:
    """변경 작업 직전: 파일이 없으면 만들고, 헤더가 계약과 다르면 중단."""
    C.ensure_csv(path, header)
    C.check_csv_header(path, header)


def _find(rows: list[dict[str, str]], key: str, value: str) -> int | None:
    v = value.strip().lower()
    for i, r in enumerate(rows):
        if (r.get(key) or "").strip().lower() == v:
            return i
    return None


def _append_note(old: str, new: str) -> str:
    new = C.clean_text(new, 500)
    if not new:
        return old
    merged = f"{old} | {new}" if old else new
    if len(merged) > NOTES_MAX:
        merged = "…" + merged[-(NOTES_MAX - 1):]
    return merged


def _check_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in C.PRODUCT_STATUSES:
        raise C.UsageError(f"잘못된 상품 status '{status}'. 가능한 값: {' | '.join(C.PRODUCT_STATUSES)}")
    return s


def _check_price(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    n = C.parse_int(value)
    if n is None or n < 0:
        raise C.UsageError(f"가격 '{value}' 은(는) 0 이상의 정수(원)여야 합니다.")
    if n > MAX_KRW:
        raise C.UsageError(f"가격 '{value}' 이(가) 너무 큽니다 (최대 {MAX_KRW:,}원). 자릿수를 확인하세요.")
    return n


def _check_url(value: str | None) -> str:
    """listed_url 은 비어 있거나 http(s) 주소여야 한다 (javascript: 같은 값이 대시보드 링크로 나가지 않게)."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) > 500 or not URL_RE.match(v):
        raise C.UsageError(f"URL '{v[:80]}' 은(는) http:// 또는 https:// 로 시작하는 주소(공백 없음, 500자 이하)여야 합니다.")
    return v


def _warn_if_unapproved_listing(pid: str, root=None) -> None:
    approved = [a for a in C.read_csv_rows(C.approvals_csv(root))
                if (a.get("product_id") or "").lower() == pid.lower()
                and a.get("action") == "publish_listing" and a.get("status") == "approved"]
    if not approved:
        warn(f"'{pid}' 를 listed 로 바꿨지만 승인된(approved) publish_listing 기록이 없습니다. 오너가 실제로 게시했는지 확인하세요.")


def _known_product(product_id: str, root=None) -> bool:
    return _find(C.read_csv_rows(C.products_csv(root)), "id", product_id) is not None


# --------------------------------------------------------------------------
# 상품
# --------------------------------------------------------------------------
def add_product(pid: str, title: str, batch: str = "", model: str = "", platform: str = "",
                price_krw: Any = None, status: str | None = None, listed_url: str = "", notes: str = "",
                upsert: bool = False, root=None) -> tuple[dict[str, str], bool]:
    """(행, 새로 만들었는지). 이미 있으면 upsert 일 때만 비어 있지 않은 값으로 갱신."""
    pid = C.validate_safe_id(pid, "상품 id")
    title = C.clean_text(title, 200)
    if not title:
        raise C.UsageError("--title 이 비어 있습니다.")
    st = _check_status(status) if status else None
    price = _check_price(price_krw)
    url = _check_url(listed_url)
    path = C.products_csv(root)
    with C.file_lock(path):
        _prepare(path, C.PRODUCT_COLS)
        rows = C.read_csv_rows(path)
        idx = _find(rows, "id", pid)
        if idx is None:
            row = {
                "id": pid, "batch": C.clean_text(batch, 80), "title": title,
                "model": C.clean_text(model, 80), "platform": C.clean_text(platform, 80),
                "price_krw": str(price if price is not None else 0), "status": st or "draft",
                "created_at": C.now_iso(), "listed_url": url, "notes": C.clean_text(notes, 500),
            }
            C.append_csv_row(path, C.PRODUCT_COLS, row)
            created = True
        else:
            if not upsert:
                raise C.CliError(f"상품 '{pid}' 는 이미 있습니다. 값을 바꾸려면 set-status 를 쓰거나 add-product --upsert 를 쓰세요.")
            row = rows[idx]
            for key, val in (("title", title), ("batch", C.clean_text(batch, 80)), ("model", C.clean_text(model, 80)),
                             ("platform", C.clean_text(platform, 80)), ("listed_url", url)):
                if val:
                    row[key] = val
            if price is not None:
                row["price_krw"] = str(price)
            if st:
                row["status"] = st
            row["notes"] = _append_note(row.get("notes", ""), notes)
            C.rewrite_csv(path, C.PRODUCT_COLS, rows)
            created = False
    if st == "listed":
        _warn_if_unapproved_listing(row["id"], root)
    return row, created


def set_product_status(pid: str, status: str, note: str = "", listed_url: str = "",
                       price_krw: Any = None, root=None) -> dict[str, str]:
    st = _check_status(status)
    price = _check_price(price_krw)
    url = _check_url(listed_url)
    path = C.products_csv(root)
    with C.file_lock(path):
        _prepare(path, C.PRODUCT_COLS)
        rows = C.read_csv_rows(path)
        idx = _find(rows, "id", pid)
        if idx is None:
            raise C.CliError(f"상품 '{pid}' 를 찾을 수 없습니다. `python scripts/tracker.py list products` 로 id 를 확인하세요.")
        row = rows[idx]
        row["status"] = st
        if url:
            row["listed_url"] = url
        if price is not None:
            row["price_krw"] = str(price)
        row["notes"] = _append_note(row.get("notes", ""), note)
        C.rewrite_csv(path, C.PRODUCT_COLS, rows)
    if st == "listed":
        _warn_if_unapproved_listing(row["id"], root)
    return row


# --------------------------------------------------------------------------
# 장부
# --------------------------------------------------------------------------
def add_ledger(type_: str, amount: Any, date: str | None = None, product_id: str = "",
               platform: str = "", note: str = "", root=None) -> dict[str, str]:
    t = (type_ or "").strip().lower()
    if t not in C.LEDGER_TYPES:
        raise C.UsageError(f"잘못된 장부 type '{type_}'. 가능한 값: {' | '.join(C.LEDGER_TYPES)}")
    amt = C.parse_int(amount)
    if amt is None or amt == 0:
        raise C.UsageError(f"금액 '{amount}' 은(는) 0 이 아닌 정수(원)여야 합니다.")
    if abs(amt) > MAX_KRW:
        raise C.UsageError(f"금액 '{amount}' 이(가) 너무 큽니다 (최대 {MAX_KRW:,}원). 자릿수를 확인하세요.")
    if t == "sale":
        if amt < 0:
            raise C.UsageError("sale(매출)은 양수여야 합니다. 환불은 --type refund 로 기록하세요.")
    elif t in ("fee", "refund", "expense"):
        amt = -abs(amt)  # 수수료·환불·비용은 음수로 저장
    else:  # payout: 정산 입금 - 양수로 저장, 손익 계산에서는 제외
        amt = abs(amt)
    if date:
        d = C.parse_date(date)
        if d is None:
            raise C.UsageError(f"날짜 '{date}' 형식이 올바르지 않습니다. 예: 2026-10-15")
    else:
        d = C.today_kst()
    pid = product_id.strip()
    if pid and not _known_product(pid, root):
        warn(f"상품 '{pid}' 는 products.csv 에 없습니다 (그래도 기록합니다).")
    row = {"date": d.isoformat(), "product_id": pid, "platform": C.clean_text(platform, 80),
           "type": t, "amount_krw": str(amt), "note": C.clean_text(note, 500)}
    path = C.ledger_csv(root)
    with C.file_lock(path):
        C.append_csv_row(path, C.LEDGER_COLS, row)
    return row


# --------------------------------------------------------------------------
# 승인 대기열
# --------------------------------------------------------------------------
def new_approval_id(rows: list[dict[str, str]], today=None) -> str:
    d = today or C.today_kst()
    prefix = f"A-{d:%Y%m%d}-"
    nums = []
    for r in rows:
        rid = r.get("id", "")
        if rid.startswith(prefix) and rid[len(prefix):].isdigit():
            nums.append(int(rid[len(prefix):]))
    return f"{prefix}{max(nums, default=0) + 1:03d}"


def request_approval(product_id: str, action: str, note: str = "", root=None) -> tuple[dict[str, str], bool]:
    """승인 요청을 pending 으로 올린다. 같은 (상품, 행동)의 pending 이 이미 있으면 그것을 돌려준다."""
    act = (action or "").strip().lower()
    if not ACTION_RE.match(act):
        raise C.UsageError(f"--action '{action}' 형식이 올바르지 않습니다. 소문자/숫자/_ 만. 예: publish_listing, price_change, send_proposal, create_account")
    pid = (product_id or "").strip()
    if pid:
        pid = C.validate_safe_id(pid, "product_id")
        if not _known_product(pid, root):
            warn(f"상품 '{pid}' 는 products.csv 에 없습니다 (그래도 요청을 올립니다).")
    path = C.approvals_csv(root)
    with C.file_lock(path):
        _prepare(path, C.APPROVAL_COLS)
        rows = C.read_csv_rows(path)
        for r in rows:
            if (r.get("status") == "pending" and r.get("action") == act
                    and (r.get("product_id") or "").lower() == pid.lower()):
                return r, False
        row = {"id": new_approval_id(rows), "product_id": pid, "action": act,
               "requested_at": C.now_iso(), "status": "pending", "decided_at": "", "note": C.clean_text(note, 500)}
        C.append_csv_row(path, C.APPROVAL_COLS, row)
        return row, True


def decide_approval(approval_id: str, decision: str, note: str = "", root=None) -> dict[str, str]:
    """pending -> approved|rejected.  호출 전에 CLI 에서 오너 확인(--i-am-owner)을 거친다."""
    if decision not in ("approved", "rejected"):
        raise C.UsageError("decision 은 approved | rejected")
    path = C.approvals_csv(root)
    with C.file_lock(path):
        _prepare(path, C.APPROVAL_COLS)
        rows = C.read_csv_rows(path)
        idx = _find(rows, "id", approval_id)
        if idx is None:
            raise C.CliError(f"승인 요청 '{approval_id}' 를 찾을 수 없습니다. `python scripts/tracker.py list approvals` 로 확인하세요.")
        row = rows[idx]
        if row.get("status") != "pending":
            raise C.CliError(f"'{row['id']}' 는 이미 {row.get('status')} 상태입니다 (결정은 한 번만 가능).")
        row["status"] = decision
        row["decided_at"] = C.now_iso()
        row["note"] = _append_note(row.get("note", ""), note)
        C.rewrite_csv(path, C.APPROVAL_COLS, rows)
        return row


# --------------------------------------------------------------------------
# 리포트
# --------------------------------------------------------------------------
def _bucket_row(label: str, bucket: dict[str, int], **extra: Any) -> dict[str, Any]:
    return {"key": label, **extra, **C.bucket_totals(bucket)}


def build_report(root=None, month: str | None = None) -> dict[str, Any]:
    entries = C.load_ledger(root)
    if month:
        entries = [e for e in entries if e["month"] == month]
    total = C.new_bucket()
    by_month: dict[str, dict[str, int]] = {}
    by_platform: dict[str, dict[str, int]] = {}
    by_product: dict[str, dict[str, int]] = {}
    for e in entries:
        C.bucket_add(total, e)
        C.bucket_add(by_month.setdefault(e["month"], C.new_bucket()), e)
        C.bucket_add(by_platform.setdefault(e["platform"], C.new_bucket()), e)
        C.bucket_add(by_product.setdefault(e["product_id"], C.new_bucket()), e)

    products = C.read_csv_rows(C.products_csv(root))
    titles = {p.get("id", ""): p.get("title", "") for p in products}
    by_status = {s: 0 for s in C.PRODUCT_STATUSES}
    for p in products:
        if p.get("id"):
            st = (p.get("status") or "").lower() or "unknown"
            by_status[st] = by_status.get(st, 0) + 1

    def ranked(d: dict[str, dict[str, int]], blank: str, with_title: bool = False) -> list[dict[str, Any]]:
        rows = []
        for k, b in d.items():
            extra = {"title": titles.get(k, "")} if with_title else {}
            rows.append(_bucket_row(k or blank, b, **extra))
        rows.sort(key=lambda r: (-r["revenue"], r["key"]))
        return rows

    months = [_bucket_row(k or "(날짜 없음)", by_month[k]) for k in sorted(by_month, key=lambda k: (k == "", k))]
    return {
        "generated_at": C.now_iso(),
        "month_filter": month or "",
        "totals": C.bucket_totals(total),
        "by_month": months,
        "by_platform": ranked(by_platform, "(미지정)"),
        "by_product": ranked(by_product, "(상품 미지정)", with_title=True),
        "products": {"total": sum(by_status.values()), "by_status": by_status},
    }


_REPORT_HEADERS = ["구분", "판매", "환불", "순매출", "수수료", "비용", "순이익", "정산입금"]
_FIRST_COL = {"월별": "월", "플랫폼별": "플랫폼", "상품별": "상품"}


def _headers(first: str = "구분") -> list[str]:
    return [first] + _REPORT_HEADERS[1:]
_REPORT_KEYS = ["sales", "refunds", "revenue", "fees", "expenses", "net", "payouts"]


def _report_cells(r: dict[str, Any], label: str | None = None) -> list[str]:
    return [label if label is not None else str(r["key"])] + [C.fmt_krw(r[k]) for k in _REPORT_KEYS]


def _product_label(r: dict[str, Any]) -> str:
    return f"{r['key']} ({r['title']})" if r.get("title") else str(r["key"])


def render_report_text(rep: dict[str, Any]) -> str:
    scope = rep["month_filter"] or "전체 기간"
    t = rep["totals"]
    out = [f"AI Revenue Engine 매출 리포트 - {scope}   (생성 {rep['generated_at'][:16].replace('T', ' ')} KST)", ""]
    out.append("[요약]  순매출 = 판매 - 환불,  순이익 = 순매출 - (수수료 + 비용),  정산입금은 손익에 포함하지 않음")
    out.append(C.format_table(_headers(), [_report_cells(t, "합계")], ["l"] + ["r"] * 7))
    aligns = ["l"] + ["r"] * 7
    sections = (
        ("월별", rep["by_month"], None),
        ("플랫폼별", rep["by_platform"], None),
        ("상품별", rep["by_product"], _product_label),
    )
    for title, rows, labeler in sections:
        out += ["", f"[{title}]"]
        if rows:
            out.append(C.format_table(_headers(_FIRST_COL[title]),
                                      [_report_cells(r, labeler(r) if labeler else None) for r in rows], aligns))
        else:
            out.append("(장부 기록 없음)")
    ps = rep["products"]
    out += ["", f"[상품 현황] 총 {ps['total']}개   " + "  ".join(f"{k} {v}" for k, v in ps["by_status"].items())]
    return "\n".join(out)


def _md_escape(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _md_table(rows: list[list[str]], first: str = "구분") -> list[str]:
    lines = ["| " + " | ".join(_md_escape(h) for h in _headers(first)) + " |",
             "|" + "|".join([" --- "] + [" ---: "] * 7) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(_md_escape(c) for c in r) + " |")
    return lines


def render_report_md(rep: dict[str, Any]) -> str:
    scope = rep["month_filter"] or "전체 기간"
    t = rep["totals"]
    out = [f"# AI Revenue Engine 매출 리포트 ({scope})", "",
           f"- 생성: {rep['generated_at'][:16].replace('T', ' ')} KST",
           "- 단위: 원(KRW). 순매출 = 판매 - 환불, 순이익 = 순매출 - (수수료 + 비용). 정산입금은 손익에 포함하지 않음.", "",
           "## 요약", ""]
    out += _md_table([_report_cells(t, "합계")])
    for title, rows, labeler in (("월별", rep["by_month"], None), ("플랫폼별", rep["by_platform"], None),
                                 ("상품별", rep["by_product"], _product_label)):
        out += ["", f"## {title}", ""]
        if rows:
            out += _md_table([_report_cells(r, labeler(r) if labeler else None) for r in rows], _FIRST_COL[title])
        else:
            out.append("_장부 기록 없음_")
    ps = rep["products"]
    out += ["", "## 상품 현황", "", f"총 {ps['total']}개", ""]
    out += ["| 상태 | 개수 |", "| --- | ---: |"] + [f"| {k} | {v} |" for k, v in ps["by_status"].items()]
    return "\n".join(out)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def _tail(rows: list, limit: int) -> tuple[list, int]:
    if limit and len(rows) > limit:
        return rows[-limit:], len(rows) - limit
    return rows, 0


def _list_section(name: str, rows: list[dict[str, str]], args: argparse.Namespace) -> str:
    if args.product:
        key = "id" if name == "products" else "product_id"
        rows = [r for r in rows if (r.get(key) or "").lower() == args.product.lower()]
    full = args.full
    if name == "products":
        if args.status:
            rows = [r for r in rows if r.get("status") == args.status]
        shown, hidden = _tail(rows, args.limit)
        headers = ["id", "batch", "title", "model", "platform", "price_krw", "status", "created_at", "listed_url", "notes"]
        table = [[r.get("id"), r.get("batch"), r.get("title"), r.get("model"), r.get("platform"),
                  C.fmt_krw(C.parse_int(r.get("price_krw")) or 0), r.get("status"),
                  (r.get("created_at") or "")[:16].replace("T", " "), r.get("listed_url"), r.get("notes")] for r in shown]
        widths = [None] * 10 if full else [30, 14, 28, 14, 12, None, None, None, 26, 28]
        aligns = ["l"] * 5 + ["r"] + ["l"] * 4
    elif name == "approvals":
        status = "pending" if args.pending else args.status
        if status:
            rows = [r for r in rows if r.get("status") == status]
        shown, hidden = _tail(rows, args.limit)
        headers = ["id", "product_id", "action", "requested_at", "status", "decided_at", "note"]
        table = [[r.get("id"), r.get("product_id"), r.get("action"), (r.get("requested_at") or "")[:16].replace("T", " "),
                  r.get("status"), (r.get("decided_at") or "")[:16].replace("T", " "), r.get("note")] for r in shown]
        widths = [None] * 7 if full else [None, 30, None, None, None, None, 40]
        aligns = ["l"] * 7
    else:
        if args.type:
            rows = [r for r in rows if r.get("type") == args.type]
        shown, hidden = _tail(rows, args.limit)
        headers = ["date", "product_id", "platform", "type", "amount_krw", "note"]
        table = [[r.get("date"), r.get("product_id"), r.get("platform"), r.get("type"),
                  C.fmt_krw(C.parse_int(r.get("amount_krw")) or 0), r.get("note")] for r in shown]
        widths = [None] * 6 if full else [None, 30, 14, None, None, 40]
        aligns = ["l", "l", "l", "l", "r", "l"]
    title = f"[{name}] {len(shown) + hidden}건"
    if hidden:
        title += f" (최근 {len(shown)}건만 표시 - 전체는 --limit 0)"
    if not shown:
        return f"{title}\n(없음)"
    return f"{title}\n{C.format_table(headers, table, aligns, widths)}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tracker.py",
        description="AI Revenue Engine 트래커: 상품(products.csv), 장부(ledger.csv), 승인 대기열(approvals.csv)",
        epilog="AI 직원은 approve/reject 를 쓰지 않습니다 (오너 전용, --i-am-owner 필요).",
        allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="CSV 3종을 헤더와 함께 생성 (이미 있으면 그대로 둠)", allow_abbrev=False)

    a = sub.add_parser("add-product", help="상품 등록 (products.csv)", allow_abbrev=False)
    a.add_argument("--id", required=True, help="상품 id (영문/숫자/-/_ . 예: notion-job-hunt-kit)")
    a.add_argument("--title", required=True)
    a.add_argument("--batch", default="", help="배치 이름. 예: batch-001")
    a.add_argument("--model", default="", help="판매 모델/형태. 예: template, ebook, service")
    a.add_argument("--platform", default="", help="판매 플랫폼. 예: kmong, smartstore")
    a.add_argument("--price-krw", "--price", dest="price_krw", help="가격(원)")
    a.add_argument("--status", help=f"기본 draft: {' | '.join(C.PRODUCT_STATUSES)}")
    a.add_argument("--listed-url", "--url", dest="listed_url", default="")
    a.add_argument("--notes", default="")
    a.add_argument("--upsert", action="store_true", help="이미 있으면 주어진 값으로 갱신")

    s = sub.add_parser("set-status", help="상품 상태 변경", allow_abbrev=False)
    s.add_argument("product_id")
    s.add_argument("status", help=" | ".join(C.PRODUCT_STATUSES))
    s.add_argument("--note", default="", help="notes 에 덧붙임")
    s.add_argument("--listed-url", "--url", dest="listed_url", default="")
    s.add_argument("--price-krw", "--price", dest="price_krw")

    g = sub.add_parser("add-ledger", help="장부 기록 (sale 양수 / fee·refund·expense 음수로 자동 저장 / payout 은 정산 입금)", allow_abbrev=False)
    g.add_argument("--type", required=True, help=" | ".join(C.LEDGER_TYPES))
    g.add_argument("--amount", required=True, help="금액(원). 부호는 type 에 맞게 자동 보정")
    g.add_argument("--date", help="YYYY-MM-DD (기본: 오늘, KST)")
    g.add_argument("--product-id", "--product", dest="product_id", default="")
    g.add_argument("--platform", default="")
    g.add_argument("--note", default="")

    r = sub.add_parser("request-approval", help="오너 승인 요청 등록 (pending). 승인은 오너만 한다", allow_abbrev=False)
    r.add_argument("--product-id", "--product", dest="product_id", default="")
    r.add_argument("--action", required=True, help="예: publish_listing, price_change, send_proposal, create_account")
    r.add_argument("--note", default="")

    for verb, text in (("approve", "승인 (오너 전용)"), ("reject", "반려 (오너 전용)")):
        d = sub.add_parser(verb, help=text, allow_abbrev=False)
        d.add_argument("approval_id", help="예: A-20261015-001")
        d.add_argument("--i-am-owner", action="store_true", help="오너 본인이 직접 실행할 때만. AI 는 사용 금지")
        d.add_argument("--note", default="")

    ls = sub.add_parser("list", help="표로 보기", allow_abbrev=False)
    ls.add_argument("what", nargs="?", default="all", choices=["products", "approvals", "ledger", "all"])
    ls.add_argument("--status", help="상품/승인 status 필터")
    ls.add_argument("--pending", action="store_true", help="승인 대기만 (approvals)")
    ls.add_argument("--type", help="장부 type 필터")
    ls.add_argument("--product", help="상품 id 필터")
    ls.add_argument("--limit", type=int, default=50, help="최근 N건 (0 = 전부, 기본 50)")
    ls.add_argument("--full", action="store_true", help="긴 칸 줄이지 않기")

    rp = sub.add_parser("report", help="월별·플랫폼별·상품별 매출/비용/순이익", allow_abbrev=False)
    rp.add_argument("--month", help="YYYY-MM 한 달만")
    fmt = rp.add_mutually_exclusive_group()
    fmt.add_argument("--md", action="store_true", help="마크다운으로 stdout 출력")
    fmt.add_argument("--json", action="store_true", help="JSON 으로 출력")
    return p


def run(args: argparse.Namespace) -> int:
    cmd = args.cmd
    if cmd == "init":
        for path, created in init_files():
            print(f"{'생성' if created else '유지'}: {path}")
    elif cmd == "add-product":
        row, created = add_product(args.id, args.title, args.batch, args.model, args.platform, args.price_krw,
                                   args.status, args.listed_url, args.notes, args.upsert)
        print(f"상품 {'등록' if created else '갱신'}: {row['id']}  [{row['status']}]  {row['title']}  {C.fmt_krw(C.parse_int(row['price_krw']))}원")
    elif cmd == "set-status":
        row = set_product_status(args.product_id, args.status, args.note, args.listed_url, args.price_krw)
        print(f"상태 변경: {row['id']} -> {row['status']}")
    elif cmd == "add-ledger":
        row = add_ledger(args.type, args.amount, args.date, args.product_id, args.platform, args.note)
        print(f"장부 기록: {row['date']}  {row['type']}  {C.fmt_krw(int(row['amount_krw']))}원  {row['product_id'] or '-'}  {row['platform'] or '-'}")
    elif cmd == "request-approval":
        row, created = request_approval(args.product_id, args.action, args.note)
        if created:
            print(f"승인 요청 등록(pending): {row['id']}  {row['action']}  {row['product_id'] or '-'}")
            print("  -> 오너가 `python scripts/tracker.py approve <ID> --i-am-owner` 로 직접 결정합니다. AI 는 승인할 수 없습니다.")
        else:
            print(f"이미 대기 중인 동일 요청이 있어 새로 만들지 않았습니다: {row['id']}  {row['action']}  {row['product_id'] or '-'}")
        print(f"approval_id: {row['id']}")
    elif cmd in ("approve", "reject"):
        if not args.i_am_owner:
            print(OWNER_ONLY_NOTICE.format(verb=cmd), file=sys.stderr)
            return OWNER_EXIT_CODE
        row = decide_approval(args.approval_id, "approved" if cmd == "approve" else "rejected", args.note)
        print(f"{'승인' if cmd == 'approve' else '반려'} 완료: {row['id']}  {row['action']}  {row['product_id'] or '-'}  ({row['decided_at']})")
    elif cmd == "list":
        if args.limit < 0:
            raise C.UsageError("--limit 은 0 이상이어야 합니다.")
        sources = {"products": C.products_csv, "approvals": C.approvals_csv, "ledger": C.ledger_csv}
        names = ["products", "approvals", "ledger"] if args.what == "all" else [args.what]
        sections = [_list_section(n, C.read_csv_rows(sources[n]()), args) for n in names]
        print("\n\n".join(sections))
    elif cmd == "report":
        if args.month and not MONTH_RE.match(args.month):
            raise C.UsageError(f"--month '{args.month}' 형식이 올바르지 않습니다. 예: 2026-10")
        rep = build_report(month=args.month)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        elif args.md:
            print(render_report_md(rep))
        else:
            print(render_report_text(rep))
    return 0


def main(argv: list[str] | None = None) -> int:
    C.force_utf8()
    try:
        return run(C.parse_cli(build_parser(), argv))
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
