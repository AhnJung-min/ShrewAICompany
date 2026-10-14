export const meta = {
  name: 'company-run',
  description: 'Shrew AI Company 생산 라인: CEO 킥오프 → 상품별 기획·디자인·제작·검수·마케팅·법무 → CEO 출고 판정 (직원 전원 Sonnet)',
  whenToUse: '새 상품 배치를 만들 때. args: { batch: "batch-002", briefs?: [...], count?: 4, maxFixRounds?: 2, root?: ".", python?: "python" } — 리눅스 클라우드에서는 python: "python3"',
  phases: [
    { title: '대표실', detail: 'CEO 킥오프: 배치 목표와 상품 브리프 확정', model: 'sonnet' },
    { title: '기획·디자인', detail: '상품별 사양서(spec.md)와 디자인(design.md)', model: 'sonnet' },
    { title: '제작', detail: '실제 산출물 파일 제작', model: 'sonnet' },
    { title: '검수', detail: '적대적 QA → 제작자 수정 → 재검수 루프', model: 'sonnet' },
    { title: '마케팅·법무', detail: '판매 문구(listing.md), 컴플라이언스(compliance.md), 장부', model: 'sonnet' },
    { title: '출고 판정', detail: 'CEO 최종 게이트(READY_FOR_OWNER.md) + 배치 보고서', model: 'sonnet' },
  ],
}

// ───────── 설정 ─────────
const A = args || {}
const ROOT = A.root || '.'                 // 로컬: 바탕화면 폴더 / 클라우드: 저장소 체크아웃 루트
const PY = A.python || 'python'            // 리눅스 클라우드라면 'python3'
const BATCH = A.batch || 'batch-001'
const COUNT = A.count || 4
const MAX_FIX = A.maxFixRounds === undefined ? 2 : A.maxFixRounds
const OFFSET = A.staffOffset || 0          // 다른 배치와 동시에 돌 때 직원 번호가 겹치지 않게 (예: 3 → maker-4 부터)
const M = 'sonnet'

// ───────── 스키마 ─────────
const BRIEF = {
  type: 'object',
  properties: {
    id: { type: 'string', description: 'kebab-case slug' },
    title: { type: 'string' }, model: { type: 'string' }, format: { type: 'string' },
    target_platform: { type: 'string' }, price_krw: { type: 'string' }, target_buyer: { type: 'string' },
    deliverables: { type: 'array', items: { type: 'string' } },
    spec: { type: 'string' },
  },
  required: ['id', 'title', 'model', 'format', 'target_platform', 'price_krw', 'target_buyer', 'deliverables', 'spec'],
}
const IDEAS = {
  type: 'object',
  properties: { ideas: { type: 'array', items: BRIEF }, evidence: { type: 'array', items: { type: 'string' } } },
  required: ['ideas', 'evidence'],
}
const KICKOFF = {
  type: 'object',
  properties: {
    batch_goal: { type: 'string' },
    approved: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, notes_for_team: { type: 'string' } }, required: ['id', 'notes_for_team'] } },
    rejected: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] } },
    new_briefs: { type: 'array', items: BRIEF, description: '브리프가 주어지지 않은 경우 CEO가 확정한 브리프' },
  },
  required: ['batch_goal', 'approved', 'rejected'],
}
const MADE = {
  type: 'object',
  properties: { files: { type: 'array', items: { type: 'string' } }, how_to_verify: { type: 'string' }, summary: { type: 'string' } },
  required: ['files', 'how_to_verify', 'summary'],
}
const QA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    score_0_10: { type: 'number' },
    defects: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, file: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' },
    }, required: ['severity', 'issue', 'fix'] } },
    summary: { type: 'string' },
  },
  required: ['verdict', 'score_0_10', 'defects', 'summary'],
}
const CFO = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    issues: { type: 'array', items: { type: 'object', properties: { severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, issue: { type: 'string' }, fix: { type: 'string' } }, required: ['severity', 'issue', 'fix'] } },
    summary: { type: 'string' },
  },
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

// ───────── 직원 프롬프트 ─────────
function staffId(role, k) {
  if (role === 'ceo') return 'ceo'
  const n = k + OFFSET
  if (role === 'maker' || role === 'qa' || role === 'researcher') return `${role}-${n + 1}`
  return n === 0 ? role : `${role}-${n + 1}`
}

function staffPrompt(role, id, p, task) {
  const prod = p ? ` --product ${p.id}` : ''
  const dir = p ? `${ROOT}/products/${BATCH}/${p.id}` : `${ROOT}/products/${BATCH}`
  return [
    `너는 Shrew AI Company(땃쥐 AI 컴퍼니)의 직원이다. 역할: ${role} / 인스턴스 ID: ${id} / 배치: ${BATCH}.`,
    `프로젝트 루트: ${ROOT} (상대경로라면 현재 작업 디렉터리 기준이다. 먼저 절대경로로 확인한 뒤 파일 도구에는 절대경로를 쓴다.)`,
    `1) ${ROOT}/.claude/agents/${role}.md 를 읽고 그 페르소나·품질 기준·절대 규칙을 따른다. 공통 규약: ${ROOT}/docs/90_시스템_인터페이스.md, 오너 결정(최우선): ${ROOT}/docs/01_오너_결정사항.md, 회사 전략: ${ROOT}/docs/00_전략_Strategy.md.`,
    `2) 오피스 상태 보고 (대시보드에 표시됨):`,
    `   시작 즉시: ${PY} "${ROOT}/scripts/office.py" status ${id} working "<지금 하는 일, 20자 내외>"${prod}`,
    `   넘길 때:   ${PY} "${ROOT}/scripts/office.py" event ${id} handoff "<누구에게 무엇을>"${prod}`,
    `   끝나기 전: ${PY} "${ROOT}/scripts/office.py" status ${id} done "<한 일>"${prod}`,
    `   (스크립트가 없거나 실패해도 본업은 계속하고, 최종 보고에 적는다.)`,
    `3) 트래커는 ${PY} "${ROOT}/scripts/tracker.py" 로만 수정한다. 정확한 플래그는 먼저 --help 로 확인한다.`,
    `작업 폴더: ${dir}`,
    `Python: python-docx, openpyxl, Pillow, matplotlib, pandas 사용 가능 (없으면 ${PY} -m pip install --user <패키지>).`,
    p ? `상품 브리프(JSON):\n${JSON.stringify(p, null, 1)}` : '',
    `절대 규칙: 계정 생성·로그인·약관 동의·결제/계좌/신분증 정보 입력·금융 거래 금지. 게시·발송·제출 금지 — 오너 승인 요청(approvals)만 올린다. 허위 수익 보장·가짜 리뷰·스팸·저작권/상표 침해·무단 개인정보 수집 금지. AI 생성물 표시와 대가성 문구 규정 준수.`,
    `\n과제:\n${task}`,
  ].filter(Boolean).join('\n')
}

function hire(role, k, p, phaseTitle, task, schema) {
  const id = staffId(role, k)
  const opts = { label: `${id}${p ? ':' + p.id : ''}`, phase: phaseTitle, model: M }
  if (schema) opts.schema = schema
  return agent(staffPrompt(role, id, p, task), opts)
}

// ───────── 1. 대표실: 킥오프 ─────────
phase('대표실')
let briefs = Array.isArray(A.briefs) ? A.briefs : []
let researchNotes = ''

if (!briefs.length) {
  const LENSES = [
    '수요 스캔: 전략 문서의 선택 모델/플랫폼에서 지금 실제로 팔리는 것(베스트셀러, 리뷰 수, 검색 키워드)을 조사해 우리가 이길 수 있는 틈새 상품 아이디어를 낸다.',
    '경쟁·가격 스캔: 같은 플랫폼의 경쟁 상품 가격대·품질·리뷰 불만을 조사해, 불만을 해결하는 차별화 상품 아이디어를 낸다.',
  ]
  const scans = (await parallel(LENSES.map((lens, i) => () =>
    hire('researcher', i, null, '대표실',
      `${lens}\nWebSearch/WebFetch 를 쓴다(지연 로딩이면 ToolSearch "select:WebSearch,WebFetch"). 이미 만든 상품은 ${ROOT}/tracker/products.csv 와 ${ROOT}/products/ 에서 확인하고 중복을 피한다. 아이디어 ${COUNT + 2}개, 각각 한 세션 안에 파일로 완성 가능한 것만. spec 은 별도 작성자가 추가 조사 없이 만들 수 있을 만큼 구체적으로.`,
      IDEAS)))).filter(Boolean)
  researchNotes = JSON.stringify(scans)
  log(`리서치 완료: 아이디어 ${scans.reduce((n, s) => n + s.ideas.length, 0)}개`)
}

const kickoff = await hire('ceo', 0, null, '대표실',
  briefs.length
    ? `아래 상품 브리프들을 검토해 이번 배치(${BATCH})에서 만들 것을 승인/반려한다. 승인 기준: 전략 부합, 실제 수요 근거, 한 세션 내 파일로 완성 가능, 규정 위험 낮음. 승인한 각 상품에 팀에게 줄 지시(notes_for_team)를 적는다. ${ROOT}/products/${BATCH}/BATCH_BRIEF.md 를 작성한다(배치 목표, 상품 목록, 담당 인스턴스, 일정, 품질 기준).\n\n브리프:\n${JSON.stringify(briefs, null, 1)}`
    : `리서처들의 아이디어 중 최대 ${COUNT}개를 골라 new_briefs 로 확정하고(approved 에도 id 를 넣는다), 나머지는 rejected 에 이유와 함께 넣는다. ${ROOT}/products/${BATCH}/BATCH_BRIEF.md 를 작성한다.\n\n리서치 결과:\n${researchNotes}`,
  KICKOFF)

if (!kickoff) { log('CEO 킥오프 실패 — 중단'); return { batch: BATCH, error: 'kickoff failed' } }
if (!briefs.length) briefs = kickoff.new_briefs || []
const notesById = {}
kickoff.approved.forEach(a => { notesById[a.id] = a.notes_for_team })
const products = briefs.filter(b => notesById[b.id] !== undefined).map(b => ({ ...b, ceo_notes: notesById[b.id] }))
log(`배치 목표: ${kickoff.batch_goal} — 승인 ${products.length}개, 반려 ${kickoff.rejected.length}개`)
if (!products.length) return { batch: BATCH, kickoff, results: [] }

// ───────── 2~6. 상품별 생산 라인 (상품끼리는 병렬, 상품 안에서는 순차) ─────────
const results = await pipeline(
  products,

  // 기획
  (_, p, k) => hire('pm', k, p, '기획·디자인',
    `상품 사양서 spec.md 를 작성한다: 목표 구매자와 그들의 문제, 구성(목차/시트/기능 목록), 각 파일별 상세 요구사항, 완성 기준(Definition of Done), 검수 체크리스트, 경쟁 대비 차별점, 하지 말아야 할 것. CEO 지시: ${p.ceo_notes}. 끝나면 디자이너에게 handoff 이벤트를 남긴다.`)
    .then(r => ({ p, k, log: { pm: r } })),

  // 디자인 (기획자↔디자이너 리뷰 쌍)
  ctx => hire('designer', ctx.k, ctx.p, '기획·디자인',
    `spec.md 를 읽고 design.md 를 작성한다: 정보 구조, 레이아웃, 서체/색/아이콘 규칙, 각 산출물 파일의 화면/페이지 구성, 썸네일·대표 이미지 브리프(문구, 구도, 색). 썸네일은 오너 결정에 따라 Claude Design 캔버스에서 따로 만들므로, 브리프는 Shrew AI Company 디자인 시스템(docs/91_외부_연동.md 의 아티팩트 링크) 토큰 이름으로 쓰고 크몽 썸네일 규격(652×488)을 기준으로 한다. spec 에 모순·누락이 있으면 spec.md 끝에 "## 디자이너 코멘트" 섹션으로 수정 제안을 남기고 design.md 에서는 합리적으로 해결한다. 끝나면 제작자에게 handoff.`)
    .then(r => ({ ...ctx, log: { ...ctx.log, designer: r } })),

  // 제작
  ctx => hire('maker', ctx.k, ctx.p, '제작',
    `spec.md 와 design.md 대로 실제 판매 산출물 파일을 모두 만든다(브리프의 deliverables 포함). 문서는 완성된 본문이어야 하며 플레이스홀더·"추후 작성" 금지. 템플릿/코드는 실제로 열리고 동작해야 한다 — 만든 뒤 직접 열어보거나 실행해 확인한다(예: openpyxl 로 다시 로드, python 실행, HTML 은 구조 확인). 썸네일 이미지는 만들지 않는다(Claude Design 캔버스에서 따로 제작). 상세페이지용 미리보기 스크린샷이 필요하면 실제 산출물을 렌더링한 이미지만 쓴다. 끝나면 검수자에게 handoff.`,
    MADE)
    .then(r => ({ ...ctx, made: r, log: { ...ctx.log, maker: r && r.summary } })),

  // 검수 루프 (제작자↔검수자 리뷰 쌍)
  async ctx => {
    const qaTask = round => `상품 폴더의 산출물을 적대적으로 검수한다(라운드 ${round}). "이대로 돈 받고 팔 수 있다"를 반박하려고 시도하라. spec.md 의 완성 기준과 검수 체크리스트를 하나씩 확인하고, 파일을 실제로 열거나 실행해 본다. 내용 오류, 빈약한 분량, 플레이스홀더, 깨진 수식/링크, 맞춤법, 저작권 의심, 과장 표현을 찾는다. 확신이 없으면 FAIL. blocker/major 가 하나라도 있으면 FAIL. qa_report.md 에 라운드별로 누적 기록한다.\n제작자 보고: ${JSON.stringify(ctx.made)}`
    let round = 1
    let qa = await hire('qa', ctx.k, ctx.p, '검수', qaTask(round), QA)
    while (qa && qa.verdict === 'FAIL' && round <= MAX_FIX) {
      log(`${ctx.p.id}: 검수 FAIL (점수 ${qa.score_0_10}) → 수정 라운드 ${round}`)
      await hire('maker', ctx.k, ctx.p, '검수',
        `검수자가 찾은 결함을 모두 고친다. 고친 뒤 직접 다시 열어/실행해 확인하고, qa_report.md 아래에 "## 수정 내역 (라운드 ${round})" 를 덧붙인다.\n결함 목록:\n${JSON.stringify(qa.defects, null, 1)}`)
      round++
      qa = await hire('qa', ctx.k, ctx.p, '검수', qaTask(round), QA)
    }
    const passed = !!qa && qa.verdict === 'PASS'
    if (!passed) log(`${ctx.p.id}: 검수 최종 불합격 — 보류`)
    return { ...ctx, qa, passed }
  },

  // 마케팅 → 법무 (마케터↔재무·법무 리뷰 쌍)
  async ctx => {
    if (!ctx.passed) return ctx
    const mk = await hire('marketer', ctx.k, ctx.p, '마케팅·법무',
      `listing.md 를 작성한다: 플랫폼(${ctx.p.target_platform}) 규격에 맞는 상품명(여러 안), 한 줄 소개, 상세 설명, 구성/스펙, 대상/비대상, FAQ, 검색 키워드/태그, 가격 제안과 근거(경쟁가 비교), 썸네일 문구. 과장·수익 보장 금지, AI 활용 사실을 플랫폼 규정에 맞게 표시. 끝나면 재무·법무에게 handoff.`)
    const cfo = await hire('cfo', ctx.k, ctx.p, '마케팅·법무',
      `compliance.md 를 작성한다: 플랫폼 판매 정책·AI 생성물 규정, 인공지능 기본법 표시 의무, 표시광고법/추천·보증 심사지침, 저작권(사용한 폰트·이미지·데이터 출처 라이선스), 개인정보, 세무(사업자등록·통신판매업·부가세·종합소득세 관련 오너 할 일) 체크리스트. listing.md 의 규정 위반 문구는 직접 고친다. 이상 없으면 tracker.py 로 상품을 products.csv 에 등록(상태 qa_passed, 배치 ${BATCH}, 가격·플랫폼 포함)한다.`,
      CFO)
    return { ...ctx, marketer: mk, cfo, compliant: !!cfo && cfo.ok }
  },

  // CEO 출고 판정
  async ctx => {
    if (!ctx.passed || !ctx.compliant) {
      return { id: ctx.p.id, title: ctx.p.title, decision: 'HOLD', reasons: [!ctx.passed ? '검수 불합격' : '컴플라이언스 이슈'], qa: ctx.qa, cfo: ctx.cfo || null }
    }
    const gate = await hire('ceo', ctx.k, ctx.p, '출고 판정',
      `이 상품의 최종 출고 여부를 판정한다. spec/design/산출물/qa_report/listing/compliance 를 모두 훑고, 오너가 이걸 올렸을 때 부끄럽지 않은지 본다. SHIP 이면: READY_FOR_OWNER.md 를 작성(업로드할 파일 목록, 붙여넣을 제목·설명·태그·가격, 썸네일, 오너 체크리스트, 예상 소요 시간)하고, tracker.py 로 상품 상태를 awaiting_owner 로 바꾸고, publish_listing 승인 요청을 올린다(approval_id 반환). 승인(approve)은 절대 직접 하지 않는다. HOLD 면 이유와 필요한 후속 작업을 적는다.\n검수 요약: ${ctx.qa.summary}\n법무 요약: ${ctx.cfo.summary}`,
      GATE)
    return { id: ctx.p.id, title: ctx.p.title, ...(gate || { decision: 'HOLD', reasons: ['CEO 판정 실패'], owner_actions: [] }), qa_score: ctx.qa.score_0_10 }
  },
)

// ───────── 배치 보고서 (전체 결과가 필요하므로 여기서만 배리어) ─────────
phase('출고 판정')
const done = results.filter(Boolean)
const shipped = done.filter(r => r.decision === 'SHIP')
log(`출고 ${shipped.length} / 보류 ${done.length - shipped.length}`)
const report = await hire('ceo', 0, null, '출고 판정',
  `배치 보고서 ${ROOT}/products/${BATCH}/BATCH_REPORT.md 를 작성한다: 배치 목표 대비 결과, 상품별 판정(SHIP/HOLD)과 이유, 검수 점수, 오너가 지금 해야 할 일(승인 대기 목록과 순서), 다음 배치 제안 3개. 마지막에 ${PY} "${ROOT}/scripts/office.py" status ceo idle "배치 ${BATCH} 마감" 을 실행한다.\n배치 목표: ${kickoff.batch_goal}\n결과:\n${JSON.stringify(done, null, 1)}`)

return { batch: BATCH, goal: kickoff.batch_goal, rejected: kickoff.rejected, results: done, report }
