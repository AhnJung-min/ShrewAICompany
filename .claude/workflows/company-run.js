export const meta = {
  name: 'company-run',
  description: 'Shrew AI Company 생산 라인 (팀 체계): CEO·기획팀장 킥오프 → 팀원 작업 + 팀장 결재(G1~G4) → CEO 출고 판정. 팀원 Sonnet, 팀장·CEO Opus',
  whenToUse: '새 상품 배치를 만들 때. args: { batch: "batch-002", briefs?: [...], count?: 3, maxFixRounds?: 2, root?: ".", python?: "python"(리눅스는 "python3"), staffOffset?: 0 }',
  phases: [
    { title: '대표실', detail: 'G0: 기획팀장 검토 → CEO 킥오프', model: 'opus' },
    { title: '기획팀', detail: '기획자 spec.md → G1 기획팀장 승인', model: 'sonnet' },
    { title: '제작팀', detail: '디자이너 design.md, 제작자 산출물 → G2 제작팀장 승인', model: 'sonnet' },
    { title: '품질팀', detail: '검수자 보고 → G3 품질팀장 판정 → 수정 루프', model: 'sonnet' },
    { title: '사업운영팀', detail: '마케터 listing.md, 재무·법무 compliance.md → G4 사업운영팀장 승인', model: 'sonnet' },
    { title: '출고 판정', detail: 'G5: CEO SHIP/HOLD + 배치 보고서', model: 'opus' },
  ],
}

// 팀 체계: docs/11_팀_체계.md. 팀원 = Sonnet, 팀장·CEO = Opus. 관문마다 팀장 Opus 호출은 최대 2회(검토 → 1회 수정 후 확정).

// ───────── 설정 ─────────
const A = args || {}
const ROOT = A.root || '.'                 // 로컬: 바탕화면 폴더 / 클라우드: 저장소 체크아웃 루트
const PY = A.python || 'python'            // 리눅스 클라우드라면 'python3'
const BATCH = A.batch || 'batch-001'
const COUNT = A.count || 3
const MAX_FIX = A.maxFixRounds === undefined ? 2 : A.maxFixRounds
const OFFSET = A.staffOffset || 0          // 다른 배치와 동시에 돌 때 팀원 번호가 겹치지 않게 (예: 3 → maker-4 부터)
const MEMBER = 'sonnet'
const LEAD = 'opus'
const LEADS = { ceo: 1, 'pm-lead': 1, 'maker-lead': 1, 'qa-lead': 1, 'marketer-lead': 1 }

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
const LEAD_REVIEW = {
  type: 'object',
  properties: {
    items: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, recommend: { type: 'string', enum: ['GO', 'CHANGE', 'DROP'] }, notes: { type: 'string' },
    }, required: ['id', 'recommend', 'notes'] } },
    new_briefs: { type: 'array', items: BRIEF, description: '브리프 없이 시작한 경우 리서처 아이디어로 기획팀장이 확정한 브리프' },
  },
  required: ['items'],
}
const KICKOFF = {
  type: 'object',
  properties: {
    batch_goal: { type: 'string' },
    approved: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, notes_for_team: { type: 'string' } }, required: ['id', 'notes_for_team'] } },
    rejected: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] } },
  },
  required: ['batch_goal', 'approved', 'rejected'],
}
const GATE = {
  type: 'object',
  properties: {
    decision: { type: 'string', enum: ['APPROVED', 'REVISE', 'DROP'] },
    reasons: { type: 'array', items: { type: 'string' } },
    fixes_requested: { type: 'array', items: { type: 'string' }, description: 'REVISE 일 때 팀원이 고칠 것' },
    fixed_myself: { type: 'array', items: { type: 'string' }, description: '팀장이 직접 고친 작은 문제' },
  },
  required: ['decision', 'reasons', 'fixes_requested', 'fixed_myself'],
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
const SHIP = {
  type: 'object',
  properties: {
    decision: { type: 'string', enum: ['SHIP', 'HOLD'] },
    reasons: { type: 'array', items: { type: 'string' } },
    owner_actions: { type: 'array', items: { type: 'string' } },
    approval_id: { type: 'string' },
  },
  required: ['decision', 'reasons', 'owner_actions'],
}

// ───────── 직원 ─────────
function staffId(role, k) {
  if (LEADS[role]) return role
  const n = k + OFFSET
  if (role === 'maker' || role === 'qa' || role === 'researcher') return `${role}-${n + 1}`
  return n === 0 ? role : `${role}-${n + 1}`
}

function staffPrompt(role, id, p, task) {
  const prod = p ? ` --product ${p.id}` : ''
  const dir = p ? `${ROOT}/products/${BATCH}/${p.id}` : `${ROOT}/products/${BATCH}`
  const rank = LEADS[role] ? '팀장급(Opus). 지시·검토·결정이 일이고, 직접 고치는 것은 1회 수정 후 남은 작은 문제뿐이다' : '팀원(Sonnet). 팀장의 관문 결정이 최종이다'
  return [
    `너는 Shrew AI Company(땃쥐 AI 컴퍼니)의 직원이다. 역할: ${role} / ID: ${id} / ${rank} / 배치: ${BATCH}.`,
    `프로젝트 루트: ${ROOT} (상대경로라면 현재 작업 디렉터리 기준이다. 먼저 절대경로로 확인한 뒤 파일 도구에는 절대경로를 쓴다.)`,
    `1) ${ROOT}/.claude/agents/${role}.md 를 읽고 그 페르소나·기준·절대 규칙을 따른다. 팀 체계(최우선): ${ROOT}/docs/11_팀_체계.md, 오너 결정: ${ROOT}/docs/01_오너_결정사항.md, 공통 규약: ${ROOT}/docs/90_시스템_인터페이스.md, 전략: ${ROOT}/products/_private/00_전략_Strategy_full.md (없으면 ${ROOT}/docs/00_전략_Strategy.md), 최신 수요조사: ${ROOT}/products/_private/trends/ (있으면).`,
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
  const opts = { label: `${id}${p ? ':' + p.id : ''}`, phase: phaseTitle, model: LEADS[role] ? LEAD : MEMBER }
  if (schema) opts.schema = schema
  return agent(staffPrompt(role, id, p, task), opts)
}

// 팀장 관문: 검토 → (REVISE 면) 팀원 1회 수정 → 팀장 확정. 반환: { approved, gate }
async function leadGate(lead, member, k, p, phaseTitle, gateName, block, reviewTask, reviseTask) {
  let gate = await hire(lead, k, p, phaseTitle,
    `${gateName}: ${reviewTask}\n결정: APPROVED(통과) / REVISE(팀원이 고칠 것을 fixes_requested 에 구체적으로) / DROP(이 상품은 진행하지 않음).\n결정과 이유를 해당 파일 끝에 "${block}" 블록으로 남긴다.`, GATE)
  if (gate && gate.decision === 'REVISE') {
    await hire(member, k, p, phaseTitle, `${gateName}에서 팀장이 수정을 요청했다. 아래를 모두 반영하고 무엇을 고쳤는지 해당 파일에 짧게 적는다.\n${JSON.stringify(gate.fixes_requested, null, 1)}\n${reviseTask}`)
    gate = await hire(lead, k, p, phaseTitle,
      `${gateName} 재검토(마지막): 팀원 수정본을 확인한다. 남은 작은 문제는 직접 고치고(fixed_myself) APPROVED 로 확정하거나, 여전히 판매할 수 없으면 DROP. 이번에는 REVISE 를 쓰지 않는다. "${block}" 블록을 갱신한다.\n지난 요청: ${JSON.stringify(gate.fixes_requested)}`, GATE)
  }
  return { approved: !!gate && gate.decision === 'APPROVED', gate }
}

// ───────── G0: 대표실 ─────────
phase('대표실')
let briefs = Array.isArray(A.briefs) ? A.briefs : []
let ideasJson = ''

if (!briefs.length) {
  const LENSES = [
    '수요 스캔: 최신 수요조사(products/_private/trends/)와 전략의 선택 모델/플랫폼에서 지금 실제로 팔리는 것을 확인해 우리가 이길 수 있는 틈새 상품 아이디어를 낸다.',
    '경쟁·가격 스캔: 같은 플랫폼의 경쟁 상품 가격대·품질·리뷰 불만을 조사해, 불만을 해결하는 차별화 상품 아이디어를 낸다.',
  ]
  const scans = (await parallel(LENSES.map((lens, i) => () =>
    hire('researcher', i, null, '대표실',
      `${lens}\nWebSearch/WebFetch 를 쓴다(지연 로딩이면 ToolSearch "select:WebSearch,WebFetch"). 이미 만든 상품은 ${PY} "${ROOT}/scripts/tracker.py" list products 와 ${ROOT}/products/ 에서 확인하고 중복을 피한다. 아이디어 ${COUNT + 2}개, 각각 한 세션 안에 파일로 완성 가능한 것만. 수요 근거는 측정치와 추론을 구분해 URL 과 함께 적는다.`,
      IDEAS)))).filter(Boolean)
  ideasJson = JSON.stringify(scans)
}

const pmReview = await hire('pm-lead', 0, null, '대표실',
  briefs.length
    ? `G0 사전 검토: 아래 브리프마다 수요 근거, 기존 상품과의 중복, 한 배치 안에 만들 수 있는 범위, 규정 위험을 보고 GO/CHANGE/DROP 을 권고한다(notes 에 이유와 바꿀 점).\n브리프:\n${JSON.stringify(briefs, null, 1)}`
    : `G0 사전 검토: 리서처 아이디어 중 최대 ${COUNT}개를 골라 완전한 브리프로 확정해 new_briefs 에 넣고(spec 은 별도 작성자가 추가 조사 없이 만들 수 있을 만큼 구체적으로), items 에 고른 것 GO, 버린 것 DROP 과 이유를 적는다.\n리서치 결과:\n${ideasJson}`,
  LEAD_REVIEW)
if (!briefs.length) briefs = (pmReview && pmReview.new_briefs) || []

const kickoff = await hire('ceo', 0, null, '대표실',
  `G0 킥오프: 이번 배치(${BATCH})에서 만들 상품을 승인/반려한다. 기획팀장 권고를 근거로 하되 최종 결정은 너다. 승인한 상품마다 팀에게 줄 지시(notes_for_team: 차별점, 반드시 넣을 것, 빼야 할 것)를 적는다. ${ROOT}/products/${BATCH}/BATCH_BRIEF.md 를 작성한다(배치 목표, 상품 목록, 팀별 담당, 관문 일정, 품질 기준).\n브리프:\n${JSON.stringify(briefs, null, 1)}\n기획팀장 권고:\n${JSON.stringify(pmReview)}`,
  KICKOFF)

if (!kickoff) { log('CEO 킥오프 실패 — 중단'); return { batch: BATCH, error: 'kickoff failed' } }
const notesById = {}
kickoff.approved.forEach(a => { notesById[a.id] = a.notes_for_team })
const products = briefs.filter(b => notesById[b.id] !== undefined).map(b => ({ ...b, ceo_notes: notesById[b.id] }))
log(`배치 목표: ${kickoff.batch_goal} — 승인 ${products.length}개, 반려 ${kickoff.rejected.length}개`)
if (!products.length) return { batch: BATCH, kickoff, results: [] }

const hold = (ctx, why) => ({ ...ctx, dropped: why })

// ───────── 상품별 생산 라인 (상품끼리는 병렬, 상품 안에서는 순차) ─────────
const results = await pipeline(
  products,

  // 기획팀: 기획자 → G1 기획팀장
  async (_, p, k) => {
    await hire('pm', k, p, '기획팀',
      `상품 사양서 spec.md 를 작성한다: 목표 구매자와 그들의 문제, 수요 근거(측정/추론 구분), 구성(목차/시트/기능 목록), 각 파일별 상세 요구사항, 완성 기준(Definition of Done), 검수 체크리스트, 경쟁 대비 차별점, 금지 문구, 하지 말아야 할 것. CEO 지시: ${p.ceo_notes}. 끝나면 기획팀장에게 handoff 이벤트를 남긴다.`)
    const g1 = await leadGate('pm-lead', 'pm', k, p, '기획팀', 'G1 사양 승인', '## 기획팀장 승인',
      `spec.md 가 수요 근거, 한 배치 범위, 완성 기준, 검수 체크리스트, 금지 문구를 갖췄는지, 기존 상품과 겹치지 않는지 확인한다.`,
      `spec.md 를 고친다.`)
    return g1.approved ? { p, k, g1: g1.gate } : hold({ p, k }, 'G1 기획팀장 DROP')
  },

  // 제작팀: 디자이너 → 제작자 → G2 제작팀장
  async ctx => {
    if (ctx.dropped) return ctx
    await hire('designer', ctx.k, ctx.p, '제작팀',
      `spec.md 를 읽고 design.md 를 작성한다: 정보 구조, 레이아웃, 서체/색/아이콘 규칙, 각 산출물 파일의 화면/페이지 구성, 썸네일·대표 이미지 브리프(문구, 구도, 색). 썸네일은 오너 결정에 따라 Claude Design 캔버스에서 따로 만들므로, 브리프는 Shrew AI Company 디자인 시스템(docs/91_외부_연동.md 의 아티팩트 링크) 토큰 이름으로 쓰고 크몽 썸네일 규격(652×488)을 기준으로 한다. spec 에 모순·누락이 있으면 spec.md 끝에 "## 디자이너 코멘트"로 남기고 design.md 에서는 합리적으로 해결한다. 끝나면 제작자에게 handoff.`)
    const made = await hire('maker', ctx.k, ctx.p, '제작팀',
      `spec.md 와 design.md 대로 실제 판매 산출물 파일을 모두 만든다(브리프의 deliverables 포함). 문서는 완성된 본문이어야 하며 플레이스홀더·"추후 작성" 금지. 템플릿/코드는 실제로 열리고 동작해야 한다 — 만든 뒤 직접 열어보거나 실행해 확인한다(예: openpyxl 로 다시 로드, python 실행, HTML 은 구조 확인). 확인 과정은 build_notes.md 에 남긴다. 썸네일 이미지는 만들지 않는다(Claude Design 캔버스에서 따로 제작). 끝나면 제작팀장에게 handoff.`,
      MADE)
    const g2 = await leadGate('maker-lead', 'maker', ctx.k, ctx.p, '제작팀', 'G2 제작 승인', '## 제작팀장 승인',
      `산출물을 직접 열어/실행해 spec·design 과 맞는지, 파일 위생(매크로·외부 링크·개인정보·절대경로 없음), 사용 설명이 실제 파일과 맞는지 확인한다. 기록은 build_notes.md 끝에.\n제작자 보고: ${JSON.stringify(made)}`,
      `산출물과 build_notes.md 를 고치고 다시 열어/실행해 확인한다.`)
    return g2.approved ? { ...ctx, made, g2: g2.gate } : hold(ctx, 'G2 제작팀장 DROP')
  },

  // 품질팀: 검수자 → G3 품질팀장 (FAIL 이면 제작자 수정 → 재검수 → 재판정)
  async ctx => {
    if (ctx.dropped) return ctx
    const memberTask = round => `상품 폴더의 산출물을 적대적으로 검수한다(라운드 ${round}). "이대로 돈 받고 팔 수 있다"를 반박하려고 시도하라. spec.md 의 완성 기준과 검수 체크리스트를 하나씩 확인하고, 파일을 실제로 열거나 실행해 본다. 내용 오류, 빈약한 분량, 플레이스홀더, 깨진 수식/링크, 맞춤법, 저작권 의심, 과장 표현을 찾는다. 확신이 없으면 FAIL. qa_report.md 에 라운드별로 누적 기록한다. 네 판정은 의견이고 최종 판정은 품질팀장이 한다.`
    const leadTask = (round, rep) => `G3 품질 판정(라운드 ${round}): 검수자 보고를 읽고, 가장 심각한 결함은 직접 다시 확인한 뒤 최종 PASS/FAIL 을 낸다. blocker/major 가 남아 있거나 확신이 없으면 FAIL. defects 에는 제작자가 고칠 것만 남긴다. qa_report.md 끝에 "## 품질팀장 판정" 블록(라운드, 판정, 이유)을 남긴다.\n검수자 보고: ${JSON.stringify(rep)}`
    let round = 1
    let rep = await hire('qa', ctx.k, ctx.p, '품질팀', memberTask(round), QA)
    let verdict = await hire('qa-lead', ctx.k, ctx.p, '품질팀', leadTask(round, rep), QA)
    while (verdict && verdict.verdict === 'FAIL' && round <= MAX_FIX) {
      log(`${ctx.p.id}: G3 FAIL (점수 ${verdict.score_0_10}) → 수정 라운드 ${round}`)
      await hire('maker', ctx.k, ctx.p, '품질팀',
        `품질팀장이 FAIL 판정했다. 아래 결함을 모두 고치고 직접 다시 열어/실행해 확인한 뒤, qa_report.md 아래에 "## 수정 내역 (라운드 ${round})" 를 덧붙인다.\n결함 목록:\n${JSON.stringify(verdict.defects, null, 1)}`)
      round++
      rep = await hire('qa', ctx.k, ctx.p, '품질팀', memberTask(round), QA)
      verdict = await hire('qa-lead', ctx.k, ctx.p, '품질팀', leadTask(round, rep), QA)
    }
    const passed = !!verdict && verdict.verdict === 'PASS'
    if (!passed) log(`${ctx.p.id}: G3 최종 불합격 — 보류`)
    return passed ? { ...ctx, qa: verdict } : hold({ ...ctx, qa: verdict }, 'G3 품질팀장 FAIL')
  },

  // 사업운영팀: 마케터 → 재무·법무 → G4 사업운영팀장
  async ctx => {
    if (ctx.dropped) return ctx
    await hire('marketer', ctx.k, ctx.p, '사업운영팀',
      `listing.md 를 작성한다: 플랫폼(${ctx.p.target_platform}) 규격에 맞는 상품명(여러 안), 한 줄 소개, 상세 설명, 구성/스펙, 대상/비대상, FAQ, 검색 키워드/태그(최신 수요조사 반영), 가격 제안과 근거(경쟁가 비교), 썸네일 문구. 과장·수익 보장 금지, AI 활용 사실을 플랫폼 규정에 맞게 표시. 끝나면 재무·법무에게 handoff.`)
    await hire('cfo', ctx.k, ctx.p, '사업운영팀',
      `compliance.md 를 작성한다: 플랫폼 판매 정책·AI 생성물 규정, 인공지능 기본법 표시 의무, 표시광고법/추천·보증 심사지침, 저작권(사용한 폰트·이미지·데이터 출처 라이선스), 개인정보, 오너 할 일(사업자등록·통신판매업 등 일반 체크리스트) . listing.md 의 규정 위반 문구는 직접 고친다. 이상 없으면 tracker.py 로 상품을 products.csv 에 등록·갱신한다(상태 qa_passed, 배치 ${BATCH}, 가격·플랫폼 포함).`)
    const g4 = await leadGate('marketer-lead', 'marketer', ctx.k, ctx.p, '사업운영팀', 'G4 판매 승인', '## 사업운영팀장 승인',
      `listing.md 와 compliance.md 를 함께 본다: 크몽 규정, 정직성(과장·수익 보장 없음, AI 활용 표시, 플랫폼 기본 환불 문구), 키워드·제목 품질, 가격 근거, 트래커 등록 상태. 기록은 compliance.md 끝에.`,
      `listing.md 와 compliance.md 를 고친다(마케터와 재무·법무 몫을 모두 반영).`)
    return g4.approved ? { ...ctx, g4: g4.gate } : hold(ctx, 'G4 사업운영팀장 DROP')
  },

  // G5: CEO 출고 판정
  async ctx => {
    if (ctx.dropped) {
      return { id: ctx.p.id, title: ctx.p.title, decision: 'HOLD', reasons: [ctx.dropped], qa: ctx.qa || null }
    }
    const gate = await hire('ceo', ctx.k, ctx.p, '출고 판정',
      `G5 출고 판정: spec/design/산출물/qa_report/listing/compliance 와 네 팀장의 승인 블록(## 기획팀장 승인, ## 제작팀장 승인, ## 품질팀장 판정, ## 사업운영팀장 승인)을 모두 읽고, 오너가 이걸 올렸을 때 부끄럽지 않은지 본다. SHIP 이면: READY_FOR_OWNER.md 를 작성(맨 위: 게시 전 점검 체크 항목 — ${ROOT}/products/_private/02_게시전_점검_기록.md 의 미확인 항목; 그다음 업로드할 파일 목록, 붙여넣을 제목·설명·태그·가격, 썸네일 브리프 위치, 오너 체크리스트, 예상 소요 시간)하고, tracker.py 로 상품 상태를 awaiting_owner 로 바꾸고, publish_listing 승인 요청을 올린다(approval_id 반환). 승인(approve)은 절대 직접 하지 않는다. HOLD 면 이유와 필요한 후속 작업을 적는다.\n품질팀장 판정: ${ctx.qa.summary}`,
      SHIP)
    return { id: ctx.p.id, title: ctx.p.title, ...(gate || { decision: 'HOLD', reasons: ['CEO 판정 실패'], owner_actions: [] }), qa_score: ctx.qa.score_0_10 }
  },
)

// ───────── 배치 보고서 (전체 결과가 필요하므로 여기서만 배리어) ─────────
phase('출고 판정')
const done = results.filter(Boolean)
const shipped = done.filter(r => r.decision === 'SHIP')
log(`출고 ${shipped.length} / 보류 ${done.length - shipped.length}`)
const report = await hire('ceo', 0, null, '출고 판정',
  `배치 보고서 ${ROOT}/products/${BATCH}/BATCH_REPORT.md 를 작성한다: 배치 목표 대비 결과, 상품별 판정(SHIP/HOLD)과 이유, 관문별 결과(G1~G4 팀장 결정), 검수 점수, 오너가 지금 해야 할 일(승인 대기 목록과 순서), 다음 배치 제안 3개. 마지막에 ${PY} "${ROOT}/scripts/office.py" status ceo idle "배치 ${BATCH} 마감" 을 실행한다.\n배치 목표: ${kickoff.batch_goal}\n결과:\n${JSON.stringify(done, null, 1)}`)

return { batch: BATCH, goal: kickoff.batch_goal, rejected: kickoff.rejected, results: done, report }
