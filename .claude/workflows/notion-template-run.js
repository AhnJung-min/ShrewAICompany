export const meta = {
  name: 'notion-template-run',
  description: 'Shrew AI Company 노션 템플릿 생산 라인: CEO 킥오프 → 템플릿별 기획·디자인·노션 제작·검수 루프·마케팅·법무 → CEO 출고 판정 (직원 전원 Sonnet)',
  whenToUse: '판매용 노션 템플릿을 만들 때. args: { batch, workshopPageUrl, briefs: [...], maxFixRounds?, root?, python? } — 리눅스 클라우드에서는 python: "python3"',
  phases: [
    { title: '대표실', detail: 'CEO 킥오프: 배치 목표, 미적 기준 확정', model: 'sonnet' },
    { title: '기획·디자인', detail: '템플릿 구조 사양서 + 노션 미적 디자인 가이드', model: 'sonnet' },
    { title: '제작', detail: '노션 작업실 안에 템플릿을 실제로 구축', model: 'sonnet' },
    { title: '검수', detail: '노션을 직접 열어 적대적 검수 → 수정 루프', model: 'sonnet' },
    { title: '마케팅·법무', detail: '판매 문구, 라이선스·규정 점검, 장부 등록', model: 'sonnet' },
    { title: '출고 판정', detail: 'CEO 최종 게이트 + 배치 보고서', model: 'sonnet' },
  ],
}

const A = args || {}
const ROOT = A.root || '.'                 // 로컬: 바탕화면 폴더 / 클라우드: 저장소 체크아웃 루트
const PY = A.python || 'python'            // 리눅스 클라우드라면 'python3'
const BATCH = A.batch || 'notion-001'
const WORKSHOP = A.workshopPageUrl
const MAX_FIX = A.maxFixRounds === undefined ? 2 : A.maxFixRounds
const M = 'sonnet'
const NOTION = 'mcp__4e22fdb6-6c1b-4813-b6f3-be008f96b591__'
const NOTION_TOOLS = ['notion-fetch', 'notion-search', 'notion-create-pages', 'notion-update-page', 'notion-create-database', 'notion-update-data-source', 'notion-create-view', 'notion-update-view', 'notion-query-data-sources', 'notion-move-pages'].map(t => NOTION + t).join(',')

const AESTHETIC = `## 미적 기준 (오너 지시: "깔끔하지만 미적인 느낌은 살린다")
- 첫 화면 3초 규칙: 템플릿 홈을 열면 무엇을 하는 곳인지 바로 보인다. 맨 위에 짧은 소개 callout 하나, 그 아래 columns로 "오늘/이번 주"에 쓰는 핵심 뷰. 설명문은 짧게, 긴 안내는 토글 안으로.
- 아이콘은 한 가지 스타일로 통일한다. Notion 기본 단색 아이콘(예: "icons/book_gray" 형식, notion-fetch 결과에서 보이는 식별자)을 우선 시도하고, 안 되면 한 계열의 이모지로 통일한다. 스타일 섞기 금지.
- 색은 accent 1개 + 회색. callout 배경은 기본 gray_bg, 강조가 꼭 필요한 한 곳만 accent 배경. 무지개 색 금지.
- 상태(Status) 색 의미를 모든 DB에서 같게: 시작 전=gray, 진행=blue, 막힘=red, 완료=green.
- DB 속성은 꼭 필요한 5~8개. 이름은 짧은 한국어 명사. 각 DB에 목적이 다른 뷰 2~3개(보드/캘린더/갤러리/표), 뷰 이름은 쓰임새로("이번 주", "진행 중", "전체").
- 헤딩은 H2/H3만, divider 남발 금지, 빈 줄로 공간을 만들지 않는다(empty-block 최소).
- 샘플 데이터는 그럴듯한 가상 예시 3~6행. 실존 인물·회사·개인정보 금지. 오탈자 0.
- "처음 시작하기" 가이드: 토글 안에 3단계, 샘플 데이터 지우는 법 포함.
- 브랜딩은 최소: 홈 맨 아래 회색 작은 글씨로 "Made by Shrew AI".
- 다크 모드에서도 읽히도록 글자색은 기본/회색 위주, 배경색 블록 위에 같은 계열 글자색 금지.`

const NOTION_RULES = `## 노션 작업 규칙
- 노션 도구는 지연 로딩이다. 먼저 ToolSearch 로 불러온다: query "select:${NOTION_TOOLS}"
- 페이지 내용을 쓰기 전에 notion-fetch 로 "notion://docs/enhanced-markdown-spec" 을 읽고, 뷰를 만들기 전에 "notion://docs/view-dsl-spec" 을 읽는다. 문법을 추측하지 않는다.
- 작업 범위: 오직 작업실 페이지(${WORKSHOP}) 아래. 다른 페이지는 읽지도 고치지도 않는다. 삭제·이동은 자기 템플릿 안에서만.
- 웹 게시, 공유 설정, 마켓 등록, 복제 허용은 절대 하지 않는다(오너 전용).
- 데이터베이스는 notion-create-database(SQL DDL)로, 행은 notion-create-pages(parent data_source_id)로, 뷰는 notion-create-view 로 만든다. 만든 페이지/DB의 URL·ID는 상품 폴더의 build_log.md 에 기록한다.`

const QA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    score_0_10: { type: 'number' },
    aesthetic_score_0_10: { type: 'number' },
    defects: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, where: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' },
    }, required: ['severity', 'where', 'issue', 'fix'] } },
    summary: { type: 'string' },
  },
  required: ['verdict', 'score_0_10', 'aesthetic_score_0_10', 'defects', 'summary'],
}
const BUILT = {
  type: 'object',
  properties: {
    root_page_url: { type: 'string' },
    pages: { type: 'array', items: { type: 'string' } },
    databases: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, url: { type: 'string' }, views: { type: 'array', items: { type: 'string' } }, sample_rows: { type: 'number' } }, required: ['name', 'url', 'views', 'sample_rows'] } },
    summary: { type: 'string' },
  },
  required: ['root_page_url', 'pages', 'databases', 'summary'],
}
const CFO = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, issues: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' } },
  required: ['ok', 'issues', 'summary'],
}
const GATE = {
  type: 'object',
  properties: {
    decision: { type: 'string', enum: ['SHIP', 'HOLD'] },
    reasons: { type: 'array', items: { type: 'string' } },
    owner_actions: { type: 'array', items: { type: 'string' } },
    approval_id: { type: 'string' },
  },
  required: ['decision', 'reasons', 'owner_actions'],
}
const KICKOFF = {
  type: 'object',
  properties: {
    batch_goal: { type: 'string' },
    notes: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, notes_for_team: { type: 'string' } }, required: ['id', 'notes_for_team'] } },
  },
  required: ['batch_goal', 'notes'],
}

function staffId(role, k) {
  if (role === 'ceo') return 'ceo'
  if (role === 'maker' || role === 'qa') return `${role}-${k + 1}`
  return k === 0 ? role : `${role}-${k + 1}`
}

function staffPrompt(role, id, p, task) {
  const prod = p ? ` --product ${p.id}` : ''
  const dir = p ? `${ROOT}/products/${BATCH}/${p.id}` : `${ROOT}/products/${BATCH}`
  return [
    `너는 Shrew AI Company(땃쥐 AI 컴퍼니)의 직원이다. 역할: ${role} / 인스턴스 ID: ${id} / 배치: ${BATCH} (판매용 노션 템플릿).`,
    `프로젝트 루트: ${ROOT} (상대경로면 현재 작업 디렉터리 기준. 먼저 절대경로로 확인한 뒤 파일 도구에는 절대경로를 쓴다.)`,
    `1) ${ROOT}/.claude/agents/${role}.md 를 읽고 그 페르소나·품질 기준·절대 규칙을 따른다. 공통 규약: ${ROOT}/docs/90_시스템_인터페이스.md, 오너 결정(최우선): ${ROOT}/docs/01_오너_결정사항.md.`,
    `2) 오피스 상태 보고: 시작 즉시 ${PY} "${ROOT}/scripts/office.py" status ${id} working "<지금 하는 일, 20자 내외>"${prod} / 넘길 때 ${PY} "${ROOT}/scripts/office.py" event ${id} handoff "<누구에게 무엇을>"${prod} / 끝나기 전 ${PY} "${ROOT}/scripts/office.py" status ${id} done "<한 일>"${prod}. (실패해도 본업은 계속하고 보고에 적는다.)`,
    `3) 트래커는 ${PY} "${ROOT}/scripts/tracker.py" 로만 수정한다. 정확한 플래그는 먼저 --help 로 확인한다.`,
    `작업 폴더(문서 산출물): ${dir}`,
    `노션 작업실(템플릿을 짓는 곳): ${WORKSHOP}`,
    p ? `템플릿 브리프(JSON):\n${JSON.stringify(p, null, 1)}` : '',
    AESTHETIC,
    NOTION_RULES,
    `절대 규칙: 계정 생성·로그인·약관 동의·결제 정보 입력·금융 거래 금지. 게시·공유·발송 금지 — 오너 승인 요청만. 허위 효과 보장·가짜 후기·저작권/상표 침해·실존 개인정보 금지. AI 활용 사실은 판매 문구에 표시.`,
    `\n과제:\n${task}`,
  ].filter(Boolean).join('\n')
}

function hire(role, k, p, phaseTitle, task, schema) {
  const id = staffId(role, k)
  const opts = { label: `${id}${p ? ':' + p.id : ''}`, phase: phaseTitle, model: M }
  if (schema) opts.schema = schema
  return agent(staffPrompt(role, id, p, task), opts)
}

if (!WORKSHOP) return { error: 'args.workshopPageUrl is required' }
const briefs = Array.isArray(A.briefs) ? A.briefs : []
if (!briefs.length) return { error: 'args.briefs is required' }

// ───────── 대표실 ─────────
phase('대표실')
const kickoff = await hire('ceo', 0, null, '대표실',
  `이번 배치(${BATCH})는 판매용 노션 템플릿 ${briefs.length}종이다. 오너는 "기존 소재는 구조가 예쁘지 않으니, 깔끔하지만 미적인 느낌을 살린 템플릿"을 원한다. 각 템플릿에 팀이 지킬 핵심 지시(notes_for_team: 차별점, 반드시 넣을 것, 빼야 할 것)를 정하고, ${ROOT}/products/${BATCH}/BATCH_BRIEF.md 를 작성한다(배치 목표, 템플릿 목록, 담당 인스턴스, 미적 기준 요약, 완성 기준).\n브리프:\n${JSON.stringify(briefs, null, 1)}`,
  KICKOFF)
const notesById = {}
;(kickoff ? kickoff.notes : []).forEach(n => { notesById[n.id] = n.notes_for_team })
const products = briefs.map(b => ({ ...b, ceo_notes: notesById[b.id] || '' }))
log(`배치 목표: ${kickoff ? kickoff.batch_goal : '(킥오프 실패 — 브리프대로 진행)'}`)

// ───────── 템플릿별 생산 라인 ─────────
const results = await pipeline(
  products,

  (_, p, k) => hire('pm', k, p, '기획·디자인',
    `템플릿 구조 사양서 spec.md 를 작성한다: 대상 사용자와 그들이 매일/매주 하는 일, 페이지 트리(홈 → 하위 페이지), 각 데이터베이스(이름, 속성 이름·타입·옵션, 관계/롤업/수식), DB별 뷰(종류, 필터, 정렬, 그룹), 홈 화면 구성, 샘플 데이터 행(구체 값), "처음 시작하기" 가이드 문구, 완성 기준(Definition of Done), 검수 체크리스트. 브리프보다 더 적게 만들어도 된다 — 꼭 필요한 것만. CEO 지시: ${p.ceo_notes}`)
    .then(() => ({ p, k })),

  ctx => hire('designer', ctx.k, ctx.p, '기획·디자인',
    `spec.md 를 읽고 design.md 를 쓴다: 이 템플릿의 노션 미적 가이드 — 아이콘 세트(페이지·DB별로 정확한 아이콘 식별자 또는 이모지), accent 색 1개와 쓰는 곳, 커버(없음 또는 노션 기본 단색 커버 URL), 홈 화면 와이어프레임(어느 블록이 어느 column 에), callout/토글 사용 위치, 뷰별 표시 속성, 샘플 데이터 문체. 위 미적 기준을 이 템플릿에 구체 값으로 적용한다. spec 에서 미적으로 지저분해지는 부분(속성 과다, 뷰 과다)이 있으면 spec.md 끝에 "## 디자이너 코멘트"로 줄이자고 제안하고 design.md 에서는 줄인 안으로 설계한다. 판매 썸네일 브리프(문구, 구도)도 마지막에 적는다.`)
    .then(() => ctx),

  ctx => hire('maker', ctx.k, ctx.p, '제작',
    `spec.md 와 design.md 대로 노션 작업실 페이지 아래에 템플릿을 실제로 구축한다: 템플릿 루트 페이지(아이콘·커버 포함) → 데이터베이스(속성·옵션·관계) → 뷰 → 샘플 행 → 홈 화면 레이아웃(columns, 링크된 DB 뷰, callout) → "처음 시작하기" 토글 → 하단 "Made by Shrew AI". 만든 뒤 notion-fetch 로 루트와 각 DB를 다시 열어 사양과 맞는지 직접 확인하고, 어긋난 곳은 고친다. 모든 URL과 데이터 소스 ID를 build_log.md 에 기록한다. 끝나면 검수자에게 handoff.`,
    BUILT)
    .then(b => ({ ...ctx, built: b })),

  async ctx => {
    if (!ctx.built) return { ...ctx, passed: false, qa: null }
    const qaTask = round => `노션에 지어진 템플릿을 적대적으로 검수한다(라운드 ${round}). 루트: ${ctx.built.root_page_url}. notion-fetch 로 홈, 모든 하위 페이지, 모든 DB(데이터 소스 스키마와 뷰 포함)를 직접 연다. 두 축으로 본다:\n(1) 기능: spec 의 완성 기준·검수 체크리스트, 관계/롤업/수식 동작, 뷰 필터·정렬, 샘플 데이터, 가이드 문구, 깨진 링크·빈 블록·플레이스홀더.\n(2) 미적: 위 미적 기준 항목 하나하나 — 아이콘 통일, 색 절제, 첫 화면 3초 규칙, 속성/뷰 과다, 헤딩 계층, 여백, 다크모드 가독성, 오탈자.\n"돈 받고 팔 수 있다"를 반박하려고 시도하라. blocker/major 가 하나라도 있으면 FAIL, 미적 점수 7 미만이어도 FAIL. 확신이 없으면 FAIL. qa_report.md 에 라운드별로 누적 기록한다.\n제작자 보고: ${JSON.stringify(ctx.built)}`
    let round = 1
    let qa = await hire('qa', ctx.k, ctx.p, '검수', qaTask(round), QA)
    while (qa && qa.verdict === 'FAIL' && round <= MAX_FIX) {
      log(`${ctx.p.id}: 검수 FAIL (기능 ${qa.score_0_10}, 미적 ${qa.aesthetic_score_0_10}) → 수정 라운드 ${round}`)
      await hire('maker', ctx.k, ctx.p, '검수',
        `검수자가 찾은 결함을 노션에서 모두 고친다. 고친 뒤 notion-fetch 로 다시 열어 확인하고, qa_report.md 아래에 "## 수정 내역 (라운드 ${round})"을 덧붙인다. build_log.md 도 갱신한다.\n결함 목록:\n${JSON.stringify(qa.defects, null, 1)}`)
      round++
      qa = await hire('qa', ctx.k, ctx.p, '검수', qaTask(round), QA)
    }
    const passed = !!qa && qa.verdict === 'PASS'
    log(`${ctx.p.id}: 검수 ${passed ? 'PASS' : '최종 불합격'}${qa ? ` (기능 ${qa.score_0_10}, 미적 ${qa.aesthetic_score_0_10})` : ''}`)
    return { ...ctx, qa, passed }
  },

  async ctx => {
    if (!ctx.passed) return ctx
    await hire('marketer', ctx.k, ctx.p, '마케팅·법무',
      `listing.md 를 쓴다. 노션 템플릿 루트: ${ctx.built.root_page_url} (notion-fetch 로 직접 열어 실제 구성을 보고 쓴다). 섹션: (1) Notion 마켓플레이스용 — 영어 제목·한 줄 소개·설명·카테고리·태그 + 한국어 병기, (2) Gumroad용 영어/한국어, (3) 크몽 자료·템플릿 카테고리용 한국어(상품명 여러 안, 상세 설명, 구성, FAQ), (4) 쇼룸 카드 문구(제목, 한 줄, 대상), (5) 가격 제안과 근거(${ctx.p.price_krw}), (6) 미리보기 스크린샷으로 찍을 화면 목록. 과장·효과 보장 금지, AI 활용 표시 포함. 끝나면 재무·법무에게 handoff.`)
    const cfo = await hire('cfo', ctx.k, ctx.p, '마케팅·법무',
      `compliance.md 를 쓴다: 구매자 라이선스(개인·사내 사용 허용, 재판매·재배포 금지) 문안, Notion 상표 사용 주의(상품명에 Notion 을 브랜드처럼 쓰지 않기, "for Notion" 표기 정도), 샘플 데이터 가상 여부, 아이콘/커버 출처, AI 활용 표시, 플랫폼별 정책 체크, 오너 세무 할 일. listing.md 의 규정 위반 문구는 직접 고친다. 이상 없으면 tracker.py 로 상품을 products.csv 에 등록한다(배치 ${BATCH}, 모델 '노션 템플릿' 성격이 드러나게, 플랫폼·가격 포함, 상태 qa_passed).`,
      CFO)
    return { ...ctx, cfo, compliant: !!cfo && cfo.ok }
  },

  async ctx => {
    if (!ctx.passed || !ctx.compliant) {
      return { id: ctx.p.id, title: ctx.p.title, decision: 'HOLD', reasons: [!ctx.passed ? '검수 불합격' : '컴플라이언스 이슈'], root_page_url: ctx.built ? ctx.built.root_page_url : null, qa: ctx.qa }
    }
    const gate = await hire('ceo', ctx.k, ctx.p, '출고 판정',
      `이 템플릿의 출고 여부를 판정한다. 노션 루트(${ctx.built.root_page_url})를 직접 열어 보고, spec/design/qa_report/listing/compliance 를 훑는다. 기준: 오너가 쇼룸에 자신 있게 걸 수 있는가, 깔끔하면서 미적인가. SHIP 이면 READY_FOR_OWNER.md 를 쓴다: 오너가 노션에서 할 일(템플릿 검토 → 공유 → 웹에 게시 → "템플릿으로 복제 허용" 켜기 → 링크 복사), 판매처별 등록 순서와 붙여넣을 문구 위치, 미리보기 스크린샷 목록, 예상 소요 시간. 그리고 tracker.py 로 상태를 awaiting_owner 로 바꾸고 publish_listing 승인 요청을 올린다(approval_id 반환). 승인은 절대 직접 하지 않는다.\n검수 요약: ${ctx.qa.summary}\n법무 요약: ${ctx.cfo.summary}`,
      GATE)
    return { id: ctx.p.id, title: ctx.p.title, root_page_url: ctx.built.root_page_url, qa_score: ctx.qa.score_0_10, aesthetic_score: ctx.qa.aesthetic_score_0_10, ...(gate || { decision: 'HOLD', reasons: ['CEO 판정 실패'], owner_actions: [] }) }
  },
)

phase('출고 판정')
const done = results.filter(Boolean)
log(`출고 ${done.filter(r => r.decision === 'SHIP').length} / 보류 ${done.filter(r => r.decision !== 'SHIP').length}`)
const report = await hire('ceo', 0, null, '출고 판정',
  `배치 보고서 ${ROOT}/products/${BATCH}/BATCH_REPORT.md 를 쓴다: 배치 목표 대비 결과, 템플릿별 판정·검수 점수(기능/미적)·노션 링크, 오너가 지금 할 일(승인 대기 목록과 순서), 다음 템플릿 제안 3개. 마지막에 ${PY} "${ROOT}/scripts/office.py" status ceo idle "배치 ${BATCH} 마감" 실행.\n배치 목표: ${kickoff ? kickoff.batch_goal : ''}\n결과:\n${JSON.stringify(done, null, 1)}`)

return { batch: BATCH, results: done, report }
