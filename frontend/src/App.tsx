import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  ArrowRight,
  Check,
  CircleAlert,
  Clock3,
  FileCheck2,
  Fingerprint,
  Gauge,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MemoryStick,
  MessagesSquare,
  PackageSearch,
  PanelRightClose,
  PanelRightOpen,
  Route,
  Send,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import {
  acceptMemoryCandidate,
  confirmRun,
  continueRun,
  getEvalSummary,
  listMemoryCandidates,
  rejectMemoryCandidate,
  startRun,
} from './api'
import type {
  Candidate,
  EvalSummary,
  MemoryCandidate,
  OfferMatchAssessment,
  Run,
  RunResponse,
} from './types'

const SAMPLE_REQUEST = '给印尼研发团队采购20台开发笔记本，单台不超过8000元，两周内到货，需要稳定运行Docker'

const stages = [
  ['COLLECTING', '理解需求'],
  ['WAITING_USER', '补齐约束'],
  ['SOURCING', '并行寻源'],
  ['REVIEWING', '制度复核'],
  ['WAITING_CONFIRMATION', '等待确认'],
  ['DRAFT_CREATED', '草稿已创建'],
]

function money(amount: string, currency = 'CNY') {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(Number(amount))
}

function providerName(value: string) {
  return {
    internal_catalog: '内部协议目录',
    marketplace_alpha: '授权商城 Alpha',
    marketplace_beta: '授权商城 Beta',
  }[value] ?? value
}

function conditionName(value?: string) {
  return { NEW: '全新', REFURBISHED: '翻新', USED: '二手' }[value ?? ''] ?? '成色待核验'
}

function bundleName(value?: string) {
  return { DEVICE_ONLY: '单机', DOCK_BUNDLE: '扩展坞套装' }[value ?? ''] ?? '套装待核验'
}

function CandidateRow({ candidate, label, accent }: { candidate: Candidate; label: string; accent?: boolean }) {
  return (
    <article className={`candidate ${accent ? 'candidate--accent' : ''}`}>
      <div className="candidate__flag">{label}</div>
      <div className="candidate__main">
        <p className="eyebrow">{providerName(candidate.offer.provider)} · {candidate.offer.variant.variant_id}</p>
        <h3>{candidate.offer.product.brand} {candidate.offer.product.model}</h3>
        <p className="spec-line">
          {candidate.offer.variant.cpu} · {candidate.offer.variant.ram_gb}GB · {candidate.offer.variant.storage_gb / 1024}TB · {candidate.offer.variant.warranty_months}个月保修
        </p>
        <div className="match-strip">
          <span className="match-badge">MATCH · {(candidate.offer.match_decision.confidence * 100).toFixed(0)}%</span>
          <span>{conditionName(candidate.offer.variant.condition)}</span>
          <span>{bundleName(candidate.offer.variant.bundle)}</span>
        </div>
        <div className="evidence-line"><Fingerprint size={14} /> {candidate.evidence_refs.length} 条证据引用 · {candidate.offer.match_decision.matched_fields.length} 个身份字段已核验</div>
      </div>
      <div className="candidate__cost">
        <span>可信到手总价</span>
        <strong>{money(candidate.cost.unconditional_total.amount)}</strong>
        <small>{candidate.offer.delivery_days} 天交付 · {candidate.policy_decision}</small>
      </div>
    </article>
  )
}

function NormalizationIssue({ assessment }: { assessment: OfferMatchAssessment }) {
  const decision = assessment.match_decision
  return (
    <article className="normalization-issue">
      <div>
        <span className={`decision-badge decision-badge--${decision.decision.toLowerCase()}`}>{decision.decision}</span>
        <strong>{providerName(assessment.provider)} · {assessment.raw_offer_id}</strong>
      </div>
      <p>{assessment.title}</p>
      <div className="issue-meta">
        <span>置信度 {(decision.confidence * 100).toFixed(0)}%</span>
        <span>{conditionName(assessment.condition)}</span>
        <span>{bundleName(assessment.bundle)}</span>
        {decision.conflict_fields.length > 0 && <span>冲突：{decision.conflict_fields.join('、')}</span>}
      </div>
      <small>{decision.reasons.join('；')}。该报价不参与最低价、最快交付和综合最优排序。</small>
    </article>
  )
}

function App() {
  const [run, setRun] = useState<Run | null>(null)
  const [input, setInput] = useState(SAMPLE_REQUEST)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [scenario, setScenario] = useState('normal')
  const [evals, setEvals] = useState<EvalSummary | null>(null)
  const [traceOpen, setTraceOpen] = useState(true)
  const [memoryCandidates, setMemoryCandidates] = useState<MemoryCandidate[]>([])
  const [resolvingCandidate, setResolvingCandidate] = useState('')
  const [memoryNotice, setMemoryNotice] = useState('')

  useEffect(() => {
    getEvalSummary().then(setEvals).catch(() => setEvals(null))
  }, [])

  useEffect(() => {
    if (run?.state !== 'DRAFT_CREATED') return
    listMemoryCandidates(run.run_id).then(setMemoryCandidates).catch(() => setMemoryCandidates([]))
  }, [run?.run_id, run?.state])

  const activeStage = useMemo(() => stages.findIndex(([state]) => state === run?.state), [run?.state])

  async function submitMessage(event?: FormEvent) {
    event?.preventDefault()
    const value = input.trim()
    if (!value || loading) return
    setLoading(true)
    setError('')
    try {
      const response: RunResponse = run
        ? await continueRun(run.run_id, value, scenario === 'normal' ? undefined : scenario)
        : await startRun(value, scenario === 'normal' ? undefined : scenario)
      setRun(response.run)
      setInput('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '请求没有完成，请重试。')
    } finally {
      setLoading(false)
    }
  }

  async function createDraft() {
    if (!run?.action_token || loading) return
    setLoading(true)
    setError('')
    try {
      const response = await confirmRun(run.run_id, run.action_token)
      setRun(response.run)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '草稿没有创建，请重新核验。')
    } finally {
      setLoading(false)
    }
  }

  async function acceptCandidate(candidate: MemoryCandidate) {
    setResolvingCandidate(candidate.candidate_id)
    setMemoryNotice('')
    try {
      const memory = await acceptMemoryCandidate(candidate)
      setMemoryCandidates((items) => items.map((item) => (
        item.candidate_id === candidate.candidate_id
          ? { ...item, status: 'ACCEPTED', accepted_memory_id: memory.memory_id }
          : item
      )))
      setMemoryNotice('已保存为长期偏好；后续任务会按 Agent 权限选择使用。')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '偏好没有保存，请重试。')
    } finally {
      setResolvingCandidate('')
    }
  }

  async function rejectCandidate(candidate: MemoryCandidate) {
    setResolvingCandidate(candidate.candidate_id)
    setMemoryNotice('')
    try {
      const resolved = await rejectMemoryCandidate(candidate)
      setMemoryCandidates((items) => items.map((item) => (
        item.candidate_id === candidate.candidate_id ? resolved : item
      )))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '候选没有处理，请重试。')
    } finally {
      setResolvingCandidate('')
    }
  }

  function reset() {
    setRun(null)
    setInput(SAMPLE_REQUEST)
    setError('')
    setMemoryCandidates([])
    setMemoryNotice('')
  }

  return (
    <div className="app-shell">
      <aside className="rail" aria-label="采购任务导航">
        <div className="brand-lockup">
          <div className="brand-mark">P</div>
          <div><strong>ProcurePilot</strong><span>采购决策控制台</span></div>
        </div>
        <div className="fixture-stamp"><ShieldCheck size={16} /> SIMULATED ENVIRONMENT</div>

        <nav className="stage-nav" aria-label="采购阶段">
          {stages.map(([state, label], index) => (
            <div className={`stage ${index <= activeStage ? 'stage--done' : ''} ${index === activeStage ? 'stage--active' : ''}`} key={state}>
              <span>{index < activeStage ? <Check size={13} /> : String(index + 1).padStart(2, '0')}</span>
              <div><strong>{label}</strong><small>{state}</small></div>
            </div>
          ))}
        </nav>

        <div className="rail-footer">
          <div className="eval-mini">
            <div><Gauge size={17} /><span>Phase 1 Evals</span></div>
            <strong>{evals ? `${evals.passed}/${evals.total}` : '—'}</strong>
            <small>{evals?.status === 'passed' ? '全部门禁通过' : '等待评测报告'}</small>
          </div>
          <button className="text-button" type="button" onClick={reset}>开始新任务 <ArrowRight size={15} /></button>
        </div>
      </aside>

      <main id="main-content" className="workspace">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">PROCUREMENT RUN / {run?.run_id ?? 'NEW'}</p>
            <h1>{run ? '研发设备采购' : '把模糊需求，变成可审计的采购决策。'}</h1>
          </div>
          <div className="header-actions">
            <label className="scenario-picker">
              <span>演示场景</span>
              <select value={scenario} onChange={(event) => setScenario(event.target.value)} disabled={Boolean(run)}>
                <option value="normal">正常链路</option>
                <option value="provider_429">单平台 429</option>
                <option value="provider_timeout">单平台超时</option>
                <option value="quote_price_changed">确认前报价变化</option>
              </select>
            </label>
            <button className="icon-button" type="button" onClick={() => setTraceOpen((value) => !value)} aria-label={traceOpen ? '收起运行轨迹' : '展开运行轨迹'}>
              {traceOpen ? <PanelRightClose size={19} /> : <PanelRightOpen size={19} />}
            </button>
          </div>
        </header>

        <section className="conversation" aria-live="polite">
          {!run && (
            <div className="opening-note">
              <div className="opening-index">01</div>
              <div>
                <p className="eyebrow">DECISION BRIEF</p>
                <h2>先问真正影响结果的问题，再开始搜索。</h2>
                <p>Agent 会并行核验商品、预算、供应商和制度；模型负责语义判断，金额与副作用由确定性服务控制。</p>
                <div className="capability-strip">
                  <span><MessagesSquare size={16} /> 主动追问</span>
                  <span><Layers3 size={16} /> 三方比价</span>
                  <span><LockKeyhole size={16} /> HITL</span>
                </div>
              </div>
            </div>
          )}

          {run?.messages.map((message, index) => (
            <div className={`message message--${message.role}`} key={`${message.created_at}-${index}`}>
              <span>{message.role === 'user' ? '申请人' : 'ProcurePilot'}</span>
              <p>{message.content}</p>
            </div>
          ))}

          {run?.state === 'WAITING_USER' && (
            <button className="suggested-reply" type="button" onClick={() => setInput('预算含税，送到雅加达，最低32GB内存和1TB硬盘')}>
              建议回复：预算含税，送到雅加达，最低 32GB 内存和 1TB 硬盘
              <ArrowRight size={16} />
            </button>
          )}

          {run?.recommendation && (
            <section className="recommendations" aria-labelledby="recommendation-title">
              <div className="section-heading">
                <div><p className="eyebrow">VERIFIED SHORTLIST</p><h2 id="recommendation-title">候选决策面板</h2></div>
                <div className="result-count"><strong>{run.recommendation.compared_offer_count}</strong><span>报价已比较</span></div>
              </div>
              <CandidateRow candidate={run.recommendation.best_overall} label="综合最优" accent />
              <CandidateRow candidate={run.recommendation.lowest_cost} label="最低成本" />
              <CandidateRow candidate={run.recommendation.fastest_delivery} label="最快交付" />

              {run.recommendation.normalization_issues.length > 0 && (
                <section className="normalization-review" aria-labelledby="normalization-review-title">
                  <div className="normalization-review__heading">
                    <div>
                      <p className="eyebrow">MATCH SAFETY GATE</p>
                      <h3 id="normalization-review-title">未进入比价的原始报价</h3>
                    </div>
                    <div className="normalization-counts">
                      <span><strong>{run.recommendation.match_rejected_count}</strong> 冲突否决</span>
                      <span><strong>{run.recommendation.review_required_count}</strong> 待人工复核</span>
                    </div>
                  </div>
                  {run.recommendation.normalization_issues.map((assessment) => (
                    <NormalizationIssue key={assessment.raw_offer_id} assessment={assessment} />
                  ))}
                </section>
              )}

              {run.revalidation_changes.length > 0 && (
                <div className="error-banner" role="alert">
                  <CircleAlert size={18} />
                  <div>
                    <strong>关键报价已经变化，请重新确认</strong>
                    {run.revalidation_changes.map((change) => (
                      <p key={change.field}>{change.field}: {change.previous} → {change.current}</p>
                    ))}
                  </div>
                </div>
              )}

              <div className="decision-footer">
                <div>
                  <p className="eyebrow">APPROVAL PREVIEW</p>
                  <div className="route-list"><Route size={17} /> {run.recommendation.best_overall.approval_route.join(' → ')}</div>
                  <p>确认后会重新校验报价、预算、制度与权限，只创建未提交草稿。</p>
                </div>
                {run.state === 'DRAFT_CREATED' ? (
                  <div className="draft-success"><FileCheck2 size={20} /><div><strong>{run.draft?.draft_id}</strong><span>DRAFT_NOT_SUBMITTED</span></div></div>
                ) : (
                  <button className="primary-button" type="button" onClick={createDraft} disabled={loading}>
                    {loading ? <LoaderCircle className="spin" size={17} /> : <FileCheck2 size={17} />}
                    确认并创建采购草稿
                  </button>
                )}
              </div>
            </section>
          )}

          {run?.state === 'DRAFT_CREATED' && memoryCandidates.length > 0 && (
            <section className="memory-review" aria-labelledby="memory-review-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">OPTIONAL MEMORY REVIEW</p>
                  <h2 id="memory-review-title">要为下次记住这些偏好吗？</h2>
                </div>
                <MemoryStick size={24} />
              </div>
              <p className="memory-review__intro">
                这些内容只是从你的原话提取的候选。未接受的内容不会进入长期记忆，也不会提供给 Agent。
              </p>
              <div className="memory-candidate-list">
                {memoryCandidates.map((candidate) => (
                  <article className={`memory-candidate memory-candidate--${candidate.status.toLowerCase()}`} key={candidate.candidate_id}>
                    <div>
                      <span className="memory-candidate__status">{candidate.status}</span>
                      <strong>{candidate.proposed_content}</strong>
                      <small>
                        {candidate.conflict_type === 'REPLACES_EXISTING' ? '将替换已有偏好' : '新偏好'}
                        {' · '}来源：本次对话
                        {' · '}14 天内有效
                      </small>
                    </div>
                    {candidate.status === 'PENDING' && (
                      <div className="memory-candidate__actions">
                        <button type="button" onClick={() => rejectCandidate(candidate)} disabled={Boolean(resolvingCandidate)}>
                          <X size={15} /> 不保存
                        </button>
                        <button type="button" onClick={() => acceptCandidate(candidate)} disabled={Boolean(resolvingCandidate)}>
                          {resolvingCandidate === candidate.candidate_id ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
                          确认记住
                        </button>
                      </div>
                    )}
                  </article>
                ))}
              </div>
              {memoryNotice && <div className="memory-notice"><ShieldCheck size={16} /> {memoryNotice}</div>}
            </section>
          )}

          {error && <div className="error-banner" role="alert"><CircleAlert size={18} /><span>{error}</span></div>}
        </section>

        {run?.state !== 'DRAFT_CREATED' && (
          <form className="composer" onSubmit={submitMessage}>
            <label htmlFor="procurement-message">采购需求或补充信息</label>
            <div className="composer__row">
              <textarea id="procurement-message" value={input} onChange={(event) => setInput(event.target.value)} rows={2} placeholder="例如：采购 20 台开发笔记本……" disabled={loading || run?.state === 'WAITING_CONFIRMATION'} />
              <button type="submit" disabled={loading || !input.trim() || run?.state === 'WAITING_CONFIRMATION'} aria-label="发送采购需求">
                {loading ? <LoaderCircle className="spin" size={20} /> : <Send size={20} />}
              </button>
            </div>
            <div className="composer__meta"><span><Sparkles size={14} /> Fixture 模式 · 数据可复算</span><span>Enter 发送</span></div>
          </form>
        )}
      </main>

      <aside className={`inspector ${traceOpen ? 'inspector--open' : ''}`} aria-label="Agent 运行轨迹">
        <div className="inspector-header">
          <div><p className="eyebrow">AGENT OPS</p><h2>运行轨迹</h2></div>
          <span className="live-dot">LIVE</span>
        </div>
        <div className="agent-map">
          <div className="agent-map__manager"><PackageSearch size={17} /><span>Application Runtime</span></div>
          <div className="agent-map__line" />
          <div className="agent-map__specialists"><span>Requirement Agent</span><span>Sourcing Agent</span></div>
        </div>
        <div className="trace-list">
          {run?.trace.length ? run.trace.map((event, index) => (
            <div className="trace-event" style={{ '--i': Math.min(index, 8) } as React.CSSProperties} key={event.event_id}>
              <div className={`trace-event__icon trace-event__icon--${event.kind.toLowerCase()}`}>
                {event.kind === 'TOOL' ? <PackageSearch size={14} /> : event.kind === 'STATE' ? <Clock3 size={14} /> : <Sparkles size={14} />}
              </div>
              <div><p><strong>{event.name}</strong><span>{event.status}</span></p><small>{event.actor}</small><code>{JSON.stringify(event.detail)}</code></div>
            </div>
          )) : (
            <div className="trace-empty"><Fingerprint size={28} /><strong>等待任务开始</strong><p>每次 Agent 决策、工具调用和状态变化都会出现在这里。</p></div>
          )}
        </div>
        <div className="inspector-footer"><ShieldCheck size={16} /><span>所有外部内容均按不可信数据处理</span></div>
      </aside>
    </div>
  )
}

export default App
