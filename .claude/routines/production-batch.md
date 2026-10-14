# 클라우드 정기 실행: 생산 배치

- 상태: **가동 중** (2026-10-15 생성). 관리: https://claude.ai/code/routines (삭제·일시정지는 여기서 오너가)
- 첫 실행: 2026-10-16(금) 09:00 KST 무렵. 이후 월·수·금 09:00 KST.
- 종료: cron 에는 종료일이 없으므로 프롬프트 0단계에서 KST 날짜가 2026-11-05 이후면 아무것도 하지 않고 끝낸다. 11/5 이후 오너가 루틴을 끈다.

## 설정
- cron (UTC): `0 0 * * 1,3,5` = 월·수·금 09:00 Asia/Seoul
- 모델: `claude-sonnet-5-5`
- 저장소(둘 다 체크아웃됨): 공개 `ShrewAICompany` → `/home/user/ShrewAICompany`, 비공개 `ShrewAICompany-products` → `/home/user/ShrewAICompany-products`. 실행 첫 단계에서 비공개 저장소를 `products/` 로 심볼릭 링크한다.
- 허용 도구: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Agent, Workflow, ToolSearch
- 외부 연동(MCP): **없음** — 생산에 필요 없고 메일·노션 내용이 섞이는 위험을 줄이기 위해 뺐다. 노션 동기화는 데스크톱 세션에서 한다.
- 결과: 비공개 저장소 `cloud/cloud-YYYYMMDD` 브랜치 → PR. PR 머지 = 오너 승인.

## 클라우드 환경에서 확인된 사실 (2026-10-15 연결 시험)
- 두 저장소 체크아웃 위치는 위와 같다. `cloud/*` 브랜치 push 는 허용, 원격 브랜치 삭제는 403 으로 막힌다(시도하지 않는다).
- 도구: Python 3.11, Node 22, gh 2.89 (로컬 Windows 는 Python 3.12).

## 프롬프트 요약
0) 날짜 확인(11/5 이후면 종료) → 1) 두 저장소 연결(products 심볼릭 링크) → 2) 맥락 읽기(CLAUDE.md, 오너 결정, 전략 전체본, 규약, 조직도, 트래커, 이전 cloud 브랜치) → 3) 비공개 저장소에 `cloud/<배치>` 브랜치 → 4) `company-run.js` 실행(count 3, python3; Workflow 도구가 없으면 같은 순서를 직접, 2개로 축소) → 5) 테스트 → 6) 커밋·push·PR, 오너 알림.
절대 규칙은 CLAUDE.md 와 같다(계정·결제·게시·발송·승인 금지, 판매 파일은 공개 저장소에 커밋 금지).
전문은 claude.ai/code/routines 의 루틴 상세에서 볼 수 있다.
