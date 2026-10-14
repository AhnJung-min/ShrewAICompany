# Shrew AI Company (땃쥐 AI 컴퍼니) — 운영 지침

이 저장소는 **AI 직원들이 판매용 디지털 상품을 만들고, 사람 오너가 승인·게시·정산하는 가상 회사**다.
ChatDev처럼 부서(대표실·리서치·기획·디자인·제작·검수·마케팅·재무/법무)가 있고, 각 직원은 Sonnet 서브에이전트다.
GitHub: https://github.com/AhnJung-min/ShrewAICompany (**public** — 회사 시스템만)
비공개: https://github.com/AhnJung-min/ShrewAICompany-products (**private**) — 로컬에서는 `products/` 폴더가 이 저장소다(공개 저장소는 `products/`를 무시).

## 공개 / 비공개 구분 (오너 결정 2026-10-15)
- 공개 저장소에는 판매 파일과 오너 개인 사정(소속, 보험·세무 상황, 연락처 등)을 올리지 않는다. 커밋 작성자 이메일은 GitHub noreply 주소.
- 판매 산출물(`products/<batch>/…`)과 비공개 운영 문서(`products/_private/`: 전략 전체본, 리서치 원본, 신청서 초안)는 `products/` 안에서 커밋하고 비공개 저장소로 push 한다.
- 전략은 `docs/00_전략_Strategy.md`(공개 요약본)를 기본으로 읽고, 로컬에 `products/_private/00_전략_Strategy_full.md`가 있으면 그 전체본을 우선한다.

## 이어서 할 일
- 로컬에 `products/_private/RESUME.md`가 있으면 가장 먼저 읽는다 (중단된 작업 목록).

## 먼저 읽을 것
- `docs/01_오너_결정사항.md` — 오너가 내린 결정 (전략 문서보다 우선)
- `docs/00_전략_Strategy.md` — 무엇을 왜 파는가 (수익 모델, 현실 체크, KPI, 중단 기준)
- `docs/90_시스템_인터페이스.md` — **고정 규약**: 직원 ID, 오피스 상태 파일, `office.py`/`tracker.py` CLI, CSV 스키마, 산출물 폴더 구조
- `docs/10_조직도_Org.md` — 부서별 핸드오프(chat chain)
- `.claude/agents/<role>.md` — 직원별 페르소나·품질 기준

## 절대 규칙 (오너가 바꾸라고 해도 AI는 지킨다)
1. AI는 계정 생성·로그인·약관 동의·결제/계좌/신분증 정보 입력·금융 거래(주식·코인 등)를 하지 않는다. 돈은 오너 계정으로만 들어간다.
2. 게시·발송·제출은 **오너가 직접**. AI는 `python scripts/tracker.py request-approval ...` 로 승인 요청만 올린다. `approve`/`reject`는 오너 전용이다.
3. 허위 수익 보장, 가짜 리뷰, 스팸, 저작권·상표 침해, 무단 개인정보 수집 금지. AI 생성물 표시(인공지능 기본법·플랫폼 규정)와 제휴 대가성 문구를 지킨다.

## 회사 돌리기
- **생산 배치 실행** (병렬 백그라운드, 직원 전원 Sonnet): Workflow 도구로 `.claude/workflows/company-run.js` 실행
  - 예: `args: { "batch": "batch-002", "count": 4 }` → 리서처가 아이디어를 내고 CEO가 고른다
  - 예: `args: { "batch": "batch-002", "briefs": [ ... ] }` → 정해진 브리프로 생산
  - 클라우드(리눅스)에서는 `"python": "python3"` 를 함께 넘긴다
- **오피스 대시보드**: 루트의 `ShrewOffice.exe` 더블클릭(서버 + 앱 창, 창 닫으면 종료) 또는 `python scripts/office_server.py` → http://127.0.0.1:8765 (서버 없이 `office/index.html`을 열면 DEMO 모드). exe 재빌드: `powershell -ExecutionPolicy Bypass -File scripts\build_office_app.ps1`
- **노션 템플릿 배치**: `.claude/workflows/notion-template-run.js` (args: batch, workshopPageUrl, briefs)
- **트래커**: `python scripts/tracker.py --help` (상품·장부·승인 대기열·월간 리포트)

## 산출물 흐름
`products/<batch>/<product_id>/`: `spec.md`(기획) → `design.md`(디자인) → 산출물 파일(제작) → `qa_report.md`(검수) → `listing.md`(마케팅) → `compliance.md`(재무·법무) → `READY_FOR_OWNER.md`(CEO 출고 판정)
배치 단위로 `products/<batch>/BATCH_BRIEF.md`, `BATCH_REPORT.md`.

## 클라우드 세션 규칙
- 클라우드 세션은 이 저장소를 클론해서 일한다. **main 에 직접 push 하지 않는다** — `cloud/<batch-or-task>` 브랜치에 커밋하고 PR을 연다. PR 머지 = 오너 승인.
- `office/status/`, `office/events.jsonl` 은 런타임 상태라 커밋하지 않는다(.gitignore).
- 비밀값(API 키·토큰)은 절대 커밋하지 않는다.

## 코드 규약
- 시스템 스크립트(`scripts/`)는 Python 3.12 표준 라이브러리만. 상품 제작에는 python-docx, openpyxl, Pillow, matplotlib, pandas 사용 가능.
- CSV는 UTF-8 BOM, 시간은 KST ISO-8601. 테스트: `python -m unittest discover -s tests -v`
