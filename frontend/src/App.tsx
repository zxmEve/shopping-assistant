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
  confirmRequirements,
  continueRun,
  getEvalSummary,
  getRun,
  getRuntimeStatus,
  isWorkflowRecoveryRequired,
  listMemoryCandidates,
  rejectMemoryCandidate,
  resumeRun,
  startRun,
} from './api'
import type {
  Candidate,
  EvalSummary,
  MemoryCandidate,
  MessageRouteResult,
  OfferMatchAssessment,
  Run,
  RunResponse,
  RuntimeStatus,
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

function storageName(value: number) {
  return value >= 1024 ? `${value / 1024}TB` : `${value}GB`
}

function policyName(value: string) {
  return {
    ALLOW: '符合采购制度',
    ALLOW_WITH_APPROVAL: '符合制度，需完成审批',
    DENY: '不符合采购制度',
  }[value] ?? '制度核验已完成'
}

function approvalName(value: string) {
  return {
    line_manager: '直属主管',
    cost_center_owner: '成本中心负责人',
    procurement_manager: '采购负责人',
    procurement_specialist: '采购专员',
    finance_approver: '财务审核人',
  }[value] ?? '指定审批人'
}

function conflictFieldName(value: string) {
  return {
    brand: '品牌',
    model: '型号',
    cpu: '处理器',
    ram_gb: '内存',
    storage_gb: '存储容量',
    operating_system: '操作系统',
    warranty_months: '保修期',
    condition: '商品成色',
    bundle: '套装内容',
  }[value] ?? '商品规格'
}

function matchDecisionName(value: string) {
  return {
    REJECTED: '规格不一致',
    REVIEW_REQUIRED: '需要人工核验',
    MATCH: '信息一致',
  }[value] ?? '已核验'
}

function modelProviderName(value?: string) {
  return {
    openai: 'OpenAI',
    deepseek: 'DeepSeek',
    fixture: '离线演示',
  }[value ?? ''] ?? '已配置模型'
}

function recordedRequirement(run: Run) {
  const requirement = run.requirement
  const resolutions = run.requirement_spec?.field_resolutions
  const cityNames: Record<string, string> = { Jakarta: '雅加达', Shanghai: '上海', Shenzhen: '深圳' }
  return [
    requirement.quantity ? `${requirement.quantity} 台` : null,
    requirement.unit_budget ? `单台上限 ${money(requirement.unit_budget.amount, requirement.unit_budget.currency)}` : null,
    typeof requirement.budget_tax_included === 'boolean' ? (requirement.budget_tax_included ? '预算含税' : '预算未税') : null,
    requirement.delivery_city ? `交付至${cityNames[requirement.delivery_city] ?? requirement.delivery_city}` : null,
    requirement.minimum_ram_gb ? `最低 ${requirement.minimum_ram_gb}GB 内存` : (resolutions?.minimum_ram_gb?.state === 'NO_PREFERENCE' ? '内存无额外要求' : null),
    requirement.minimum_storage_gb ? `最低 ${storageName(requirement.minimum_storage_gb)} 存储` : (resolutions?.minimum_storage_gb?.state === 'NO_PREFERENCE' ? '硬盘无额外要求' : null),
  ].filter(Boolean).join(' · ')
}

function CandidateRow({ candidate, label, accent }: { candidate: Candidate; label: string; accent?: boolean }) {
  const explanations = candidate.policy_reasons.filter((reason) => !reason.startsWith('按 '))
  return (
    <article className={`candidate ${accent ? 'candidate--accent' : ''}`}>
      <div className="candidate__flag">{label}</div>
      <div className="candidate__main">
        <p className="eyebrow">{providerName(candidate.offer.provider)} · {candidate.offer.seller}</p>
        <h3>{candidate.offer.product.brand} {candidate.offer.product.model}</h3>
        <p className="spec-line">
          {candidate.offer.variant.cpu} · {candidate.offer.variant.ram_gb}GB 内存 · {storageName(candidate.offer.variant.storage_gb)} 存储 · {candidate.offer.variant.warranty_months} 个月保修
        </p>
        <div className="match-strip">
          <span className="match-badge"><ShieldCheck size={13} /> 商品信息已核验</span>
          <span>{conditionName(candidate.offer.variant.condition)}</span>
          <span>{bundleName(candidate.offer.variant.bundle)}</span>
        </div>
        {explanations.length > 0 && (
          <details className="candidate__explanation">
            <summary>查看核验说明</summary>
            <ul>
              {explanations.map((reason) => <li key={reason}>{reason}</li>)}
            </ul>
          </details>
        )}
      </div>
      <div className="candidate__cost">
        <span>可信到手总价</span>
        <strong>{money(candidate.cost.unconditional_total.amount)}</strong>
        <small>{candidate.offer.delivery_days} 天交付 · {policyName(candidate.policy_decision)}</small>
      </div>
    </article>
  )
}

function NormalizationIssue({ assessment }: { assessment: OfferMatchAssessment }) {
  const decision = assessment.match_decision
  return (
    <article className="normalization-issue">
      <div>
        <span className={`decision-badge decision-badge--${decision.decision.toLowerCase()}`}>{matchDecisionName(decision.decision)}</span>
        <strong>{providerName(assessment.provider)}</strong>
      </div>
      <p>{assessment.title}</p>
      <div className="issue-meta">
        <span>{conditionName(assessment.condition)}</span>
        <span>{bundleName(assessment.bundle)}</span>
        {decision.conflict_fields.length > 0 && <span>不一致项：{decision.conflict_fields.map(conflictFieldName).join('、')}</span>}
      </div>
      <small>{decision.reasons.join('；')}。为避免误选，该报价未参与推荐排序。</small>
    </article>
  )
}

function App() {
  const [run, setRun] = useState<Run | null>(null)
  const [input, setInput] = useState(SAMPLE_REQUEST)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [recoveryNotice, setRecoveryNotice] = useState('')
  const [messageRoute, setMessageRoute] = useState<MessageRouteResult | null>(null)
  const [messageAttempt, setMessageAttempt] = useState<{
    runId: string; requestId: string; message: string; scenario?: string; requirementVersion?: number; recommendationVersion?: number; resolutionDraftId?: string; resolutionVersion?: number
  } | null>(null)
  const [scenario, setScenario] = useState('normal')
  const [evals, setEvals] = useState<EvalSummary | null>(null)
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const [memoryCandidates, setMemoryCandidates] = useState<MemoryCandidate[]>([])
  const [resolvingCandidate, setResolvingCandidate] = useState('')
  const [memoryNotice, setMemoryNotice] = useState('')

  useEffect(() => {
    const savedRun = sessionStorage.getItem('procure:last-run')
    if (savedRun) {
      getRun(savedRun).then(async (saved) => {
        setRun(saved)
        const dispatching = [...saved.message_routes].reverse().find((r) => r.status === 'DISPATCHING')
        if (dispatching) {
          const recovered = await continueRun(saved.run_id, dispatching.message, dispatching.scenario ?? undefined, {request_id: dispatching.request_id, expected_requirement_version: dispatching.expected_requirement_version ?? undefined, expected_recommendation_version: dispatching.expected_recommendation_version ?? undefined, expected_resolution_draft_id: dispatching.expected_resolution_draft_id ?? undefined, expected_resolution_version: dispatching.expected_resolution_version ?? undefined})
          saved = recovered.run
          setMessageRoute(recovered.message_route ?? null)
        }
        const requirementPending = saved.requirement_confirmations.find((c) => c.status === 'DISPATCHING')
        if (requirementPending) {
          saved = (await confirmRequirements(saved.run_id, {draft_id: requirementPending.draft_id, version: requirementPending.draft_version, content_hash: requirementPending.content_hash}, requirementPending.request_id)).run
        }
        if (['COLLECTING', 'SOURCING', 'REVIEWING', 'REVALIDATING', 'DRAFT_CREATING'].includes(saved.state)) {
          saved = (await resumeRun(saved.run_id)).run
        }
        setRun(saved)
        setInput('')
        const pending = [...saved.message_routes].reverse().find((r) => r.status === 'CLARIFY')
        if (pending) {
          setMessageAttempt({runId: saved.run_id, requestId: pending.request_id, message: pending.message, scenario: pending.scenario ?? undefined, requirementVersion: pending.expected_requirement_version ?? undefined, recommendationVersion: pending.expected_recommendation_version ?? undefined, resolutionDraftId: pending.expected_resolution_draft_id ?? undefined, resolutionVersion: pending.expected_resolution_version ?? undefined})
          setMessageRoute({request_id: pending.request_id, route: 'CLARIFY', origin_run_id: saved.run_id, reason: pending.proposal.reason, source: pending.source, choices: ['DRAFT_CREATED', 'FAILED'].includes(saved.state) ? ['NEW'] : ['CONTINUE', 'NEW']})
        }
        setRecoveryNotice('已恢复本次采购，请核对当前处理结果。')
      }).catch(() => setRecoveryNotice('已有采购暂未恢复，请稍后重试。'))
    }
    getEvalSummary().then(setEvals).catch(() => setEvals(null))
    getRuntimeStatus().then(setRuntimeStatus).catch(() => setRuntimeStatus(null))
  }, [])

  useEffect(() => {
    if (run) sessionStorage.setItem('procure:last-run', run.run_id)
  }, [run])

  const isLiveModel = runtimeStatus?.mode === 'openai'

  useEffect(() => {
    if (run?.state !== 'DRAFT_CREATED') return
    listMemoryCandidates(run.run_id).then(setMemoryCandidates).catch(() => setMemoryCandidates([]))
  }, [run?.run_id, run?.state])

  const clarificationSpec = run?.resolution_draft && run.resolution_draft.status !== 'CONFIRMED'
    ? run.resolution_draft.proposed_spec : run?.requirement_spec

  async function approveRequirements() {
    if (!run?.resolution_draft || loading) return
    setLoading(true)
    setError('')
    try {
      const response = await confirmRequirements(run.run_id, run.resolution_draft)
      setRun(response.run)
      setMessageRoute(null)
      setMessageAttempt(null)
      setRecoveryNotice('需求已确认，已开始寻源；创建采购草稿需要另行确认。')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '需求没有确认，请重试。')
      try { setRun(await getRun(run.run_id)) } catch { /* Keep the current draft for retry. */ }
    } finally { setLoading(false) }
  }

  const referenceLabels: Record<string, string> = {category: '品类', purpose: '用途', quantity: '数量', 'budget.maximum': '单件预算上限', 'budget.tax_included': '预算含税', delivery_city: '交付城市', minimum_ram_gb: '最低内存（GB）', minimum_storage_gb: '最低存储（GB）', minimum_warranty_months: '最低保修（月）', condition: '成色'}
  const sourceLabels = {USER: '用户要求', POLICY: '现行制度', HISTORY: '模拟历史采购单'}

  const activeStage = useMemo(() => stages.findIndex(([state]) => state === run?.state), [run?.state])

  async function submitMessage(event?: FormEvent, routeChoice?: 'CONTINUE' | 'NEW', messageOverride?: string) {
    event?.preventDefault()
    const typedChoice = messageRoute?.route === 'CLARIFY'
      ? (/^(?:新需求|新的|换需求|开始新的采购)[。！!]?$/u.test(input.trim()) ? 'NEW'
        : /^(?:继续|继续当前采购|继续这个需求|原来的)[。！!]?$/u.test(input.trim()) ? 'CONTINUE' : undefined)
      : undefined
    const choice = routeChoice ?? typedChoice
    const value = choice ? messageAttempt?.message ?? '' : messageOverride ?? input.trim()
    if (!value || loading) return
    setLoading(true)
    setError('')
    setRecoveryNotice('')
    try {
      const selectedScenario = scenario === 'normal' ? undefined : scenario
      const attempt = run ? (
        messageAttempt?.runId === run.run_id && messageAttempt.message === value
          ? messageAttempt
          : { runId: run.run_id, requestId: crypto.randomUUID(), message: value, scenario: selectedScenario, requirementVersion: run.requirement.version, recommendationVersion: run.recommendation_version, resolutionDraftId: run.resolution_draft?.draft_id, resolutionVersion: run.resolution_draft?.version }
      ) : null
      if (attempt) setMessageAttempt(attempt)
      const response: RunResponse = run && attempt
        ? await continueRun(attempt.runId, attempt.message, attempt.scenario, {
          request_id: attempt.requestId, route_choice: choice,
          expected_requirement_version: attempt.requirementVersion, expected_recommendation_version: attempt.recommendationVersion,
          expected_resolution_draft_id: attempt.resolutionDraftId, expected_resolution_version: attempt.resolutionVersion,
        })
        : await startRun(value, selectedScenario)
      setRun(response.run)
      setMessageRoute(response.message_route ?? null)
      if (response.message_route?.route === 'NEW') {
        setRecoveryNotice('已开始新的采购，之前的采购已保留；新需求不会继承原来的预算、数量和配置。')
        setMemoryCandidates([])
        setMemoryNotice('')
      }
      if (response.message_route?.route !== 'CLARIFY') setMessageAttempt(null)
      setInput('')
    } catch (caught) {
      if (run && isWorkflowRecoveryRequired(caught)) {
        try {
          const recovered = await resumeRun(run.run_id)
          const lastUserMessage = [...recovered.run.messages].reverse().find((message) => message.role === 'user')
          const inputWasAlreadyAccepted = lastUserMessage?.content.trim() === value
          setRun(recovered.run)
          setInput(inputWasAlreadyAccepted ? '' : value)
          setRecoveryNotice(
            inputWasAlreadyAccepted
              ? '上一次处理已自动恢复，你的补充信息没有重复提交。'
              : '上一次处理已自动恢复；当前输入尚未发送，请确认后再次发送。',
          )
        } catch {
          setError('上一次处理尚未完成，自动恢复失败。请稍后重试或开始新任务。')
        }
      } else {
        setError(caught instanceof Error ? caught.message : '请求没有完成，请重试。')
      }
    } finally {
      setLoading(false)
    }
  }

  async function createDraft() {
    if (!run?.action_token || loading || messageRoute?.route === 'CLARIFY') return
    setLoading(true)
    setError('')
    setRecoveryNotice('')
    try {
      const response = await confirmRun(run.run_id, run.action_token)
      setRun(response.run)
    } catch (caught) {
      if (isWorkflowRecoveryRequired(caught)) {
        try {
          const recovered = await resumeRun(run.run_id)
          setRun(recovered.run)
          setRecoveryNotice('上一次处理已自动恢复，请检查最新结果。')
        } catch {
          setError('草稿创建流程尚未完成，自动恢复失败。请稍后重试。')
        }
      } else {
        setError(caught instanceof Error ? caught.message : '草稿没有创建，请重新核验。')
      }
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
    sessionStorage.removeItem("procure:last-run")
    setRun(null)
    setMessageRoute(null)
    setMessageAttempt(null)
    setInput(SAMPLE_REQUEST)
    setError('')
    setRecoveryNotice('')
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
        <div className={`runtime-stamp ${isLiveModel ? 'runtime-stamp--live' : ''}`}>
          <ShieldCheck size={16} />
          <span>
            <strong>{isLiveModel ? '真实模型已启用' : '离线演示模式'}</strong>
            <small>{isLiveModel ? modelProviderName(runtimeStatus?.model_provider) : '固定数据，不产生模型费用'}</small>
          </span>
        </div>

        <nav className="stage-nav" aria-label="采购阶段">
          {stages.map(([state, label], index) => (
            <div className={`stage ${index <= activeStage ? 'stage--done' : ''} ${index === activeStage ? 'stage--active' : ''}`} key={state}>
              <span>{index < activeStage ? <Check size={13} /> : String(index + 1).padStart(2, '0')}</span>
              <div><strong>{label}</strong></div>
            </div>
          ))}
        </nav>

        <div className="rail-footer">
          <div className="eval-mini">
            <div><Gauge size={17} /><span>系统质量检查</span></div>
            <strong>{evals ? `${evals.passed}/${evals.total}` : '—'}</strong>
            <small>{evals?.status === 'passed' ? '全部门禁通过' : '等待评测报告'}</small>
          </div>
          <button className="text-button" type="button" onClick={reset}>开始新任务 <ArrowRight size={15} /></button>
        </div>
      </aside>

      <main id="main-content" className="workspace">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">企业采购助手 · {run ? '任务进行中' : '新任务'}</p>
            <h1>{run ? '研发设备采购' : '把模糊需求，变成可审计的采购决策。'}</h1>
          </div>
          <div className="header-actions">
            <label className="scenario-picker">
              <span>流程模拟</span>
              <select value={scenario} onChange={(event) => setScenario(event.target.value)} disabled={Boolean(run)}>
                <option value="normal">标准采购流程</option>
                <option value="provider_429">平台限流演示</option>
                <option value="provider_timeout">平台超时演示</option>
                <option value="quote_price_changed">报价变化演示</option>
              </select>
              <small>{scenario === 'normal' ? '不模拟异常情况' : '用于演示系统的异常处理能力'}</small>
            </label>
            <button className="detail-toggle" type="button" onClick={() => setTraceOpen((value) => !value)} aria-label={traceOpen ? '收起技术运行详情' : '展开技术运行详情'}>
              {traceOpen ? <PanelRightClose size={19} /> : <PanelRightOpen size={19} />}
              <span>技术详情</span>
            </button>
          </div>
        </header>

        <section className="conversation" aria-live="polite">
          {!run && (
            <div className="opening-note">
              <div className="opening-index">01</div>
              <div>
                <p className="eyebrow">采购决策助手</p>
                <h2>先问真正影响结果的问题，再开始搜索。</h2>
                <p>Agent 会并行核验商品、预算、供应商和制度；模型负责语义判断，金额与副作用由确定性服务控制。</p>
                <div className="capability-strip">
                  <span><MessagesSquare size={16} /> 主动追问</span>
                  <span><Layers3 size={16} /> 三方比价</span>
                  <span><LockKeyhole size={16} /> 人工确认</span>
                </div>
              </div>
            </div>
          )}

          {run?.messages.map((message, index) => (
            run.state === 'WAITING_USER'
              && clarificationSpec?.clarification_plan.questions.length
              && run.wait_reason === 'MISSING_REQUIREMENT'
              && message.role === 'assistant'
              && index === run.messages.length - 1
              ? null : <div className={`message message--${message.role}`} key={`${message.created_at}-${index}`}>
              <span>{message.role === 'user' ? '申请人' : 'ProcurePilot'}</span>
              <p style={{ whiteSpace: 'pre-line' }}>{message.content}</p>
            </div>
          ))}

          {run?.state === 'WAITING_USER' && run.wait_reason === 'MISSING_REQUIREMENT' && clarificationSpec?.clarification_plan && (
            <section className="clarification-list" aria-labelledby="clarification-title">
              <h2 id="clarification-title">待补充的信息</h2>
              {recordedRequirement(run) && <p>已记录：{recordedRequirement(run)}</p>}
              <ol>
                {clarificationSpec.clarification_plan.questions.map((question) => (
                  <li key={question.question_id}>{question.text}</li>
                ))}
              </ol>
              <p>可以一次回答，也可以先补充其中几项。配置没有额外要求时，请明确说明内存或硬盘无要求。</p>
            </section>
          )}

          {run?.state === 'WAITING_USER' && run.resolution_draft && run.wait_reason !== 'REFERENCE_INFORMATION' && (
            <section className="clarification-list requirement-review" aria-label="参考依据与需求确认">
              <h2>{run.wait_reason === 'HISTORY_SELECTION' ? '选择参考的历史采购单' : '需求草案 · 待确认'}</h2>
              <p>版本 {run.resolution_draft.version}；引用字段确认前不会用于寻源。</p>
              {run.wait_reason === 'HISTORY_SELECTION' && run.reference_history?.orders.map((order) => <div key={order.order_id} className="negotiation-option">
                <p>{order.order_id} · {new Date(order.purchased_at).toLocaleDateString()} · {order.model_text} · 模拟数据</p>
                <button type="button" disabled={loading} onClick={() => submitMessage(undefined, undefined, `参考订单 ${order.order_id}`)}>参考此单并生成需求草案</button>
              </div>)}
              {run.reference_policy && <p>制度版本 {run.reference_policy.source_version}{run.reference_policy.effective_from ? `，生效日期 ${new Date(run.reference_policy.effective_from).toLocaleDateString()}` : ''}</p>}
              {run.reference_policy?.citations.map((citation) => <details key={citation.source_ref}><summary>{citation.document_title} · {citation.clause_ref ?? citation.heading}</summary><p>{citation.excerpt}</p><small>{citation.source_ref}</small></details>)}
              {run.resolution_draft.fields.map((field) => <div key={field.field} className="reference-field">
                <strong>{referenceLabels[field.field] ?? field.field}：{typeof field.value === 'boolean' ? (field.value ? '是' : '否') : ({laptop: '笔记本', NEW: '全新', REFURBISHED: '翻新', USED: '二手', Jakarta: '雅加达', Shanghai: '上海', Shenzhen: '深圳'} as Record<string, string>)[String(field.value)] ?? String(field.value)}</strong>
                <span>{sourceLabels[field.source]}</span><small>{field.source_ref}</small>
              </div>)}
              {run.resolution_draft.changes.length > 0 && <details><summary>参考与调整说明</summary>{run.resolution_draft.changes.map((change, index) => <p key={index}>{change}</p>)}</details>}
              {run.resolution_draft.blockers.map((blocker, index) => <p role="alert" key={index}>{blocker}</p>)}
              {run.wait_reason === 'REQUIREMENT_CONFIRMATION' && <button type="button" className="primary-button" disabled={loading || run.resolution_draft.status !== 'READY'} onClick={approveRequirements}>确认需求并开始寻源</button>}
              <p>此确认只开始寻源；不会创建或提交采购草稿。也可以直接输入要修改的条件。</p>
            </section>
          )}
          {run?.state === 'WAITING_USER' && run.wait_reason === 'CONSTRAINT_NEGOTIATION' && (
            <section className="clarification-list" aria-label="条件协商">
              <h2>可调整的采购条件</h2>
              <p>方案只调整需求并重新寻源。生成草稿需要另行确认。</p>
              {run.negotiation_options.map((option, index) => (
                <div key={option.option_id} className="negotiation-option">
                  <p>{option.adjustments.map((a) => `${({ 'budget.maximum': '单台预算', delivery_days: '交期（天）', minimum_ram_gb: '最低内存（GB）', minimum_storage_gb: '最低存储（GB）' } as Record<string, string>)[a.field] ?? a.field}：${a.previous} → ${a.proposed}`).join('；')}</p>
                  <button type="button" className="primary-button" disabled={loading} onClick={() => submitMessage(undefined, undefined, `接受方案${index + 1}`)}>接受方案 {index + 1} 并重新寻源</button>
                </div>
              ))}
            </section>
          )}
          {run?.state === 'WAITING_USER' && run.wait_reason === 'FACTS_UNAVAILABLE' && (
            <section className="clarification-list" aria-label="查询等待">
              <h2>必需事实暂未确认</h2>
              <p>库存、交期或保修查询尚未完成，当前无法创建草稿。</p>
              <button type="button" disabled={loading} onClick={() => submitMessage(undefined, undefined, '重试查询')}>重试查询</button>
            </section>
          )}
          {run?.recommendation && (
            <section className="recommendations" aria-labelledby="recommendation-title">
              <div className="section-heading">
                <div><p className="eyebrow">采购建议</p><h2 id="recommendation-title">推荐方案</h2></div>
                <div className="result-count"><strong>{run.recommendation.compared_offer_count}</strong><span>份有效报价已核验</span></div>
              </div>
              <CandidateRow candidate={run.recommendation.best_overall} label="1 · 综合最优" accent />
              <CandidateRow candidate={run.recommendation.lowest_cost} label="2 · 最低成本" />
              <CandidateRow candidate={run.recommendation.fastest_delivery} label="3 · 最快交付" />

              {run.recommendation.normalization_issues.length > 0 && (
                <details className="normalization-review">
                  <summary>另有 {run.recommendation.normalization_issues.length} 份报价因信息不一致未纳入比较</summary>
                  <section aria-labelledby="normalization-review-title">
                  <div className="normalization-review__heading">
                    <div>
                      <p className="eyebrow">报价核验</p>
                      <h3 id="normalization-review-title">未采用的报价</h3>
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
                </details>
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

              {run.state === 'WAITING_CONFIRMATION' && <div className="dialogue-actions">
                {['解释一下推荐理由', '比较三款的区别', '更便宜一些', '快一点'].map((text) => <button type="button" key={text} disabled={loading} onClick={() => submitMessage(undefined, undefined, text)}>{text}</button>)}
                <span>推荐版本 {run.recommendation_version}；可用“选第二款”指定本轮候选。</span>
              </div>}
              <details className="normalization-review"><summary>库存、交期与保修查询证据（本地供应商模拟接口）</summary>
                {run.fact_evidence.filter((f) => f.provider === run.recommendation?.best_overall.offer.provider && f.offer_id === run.recommendation?.best_overall.offer.offer_id).map((fact) => <p key={fact.fact_id}>{fact.kind} · {fact.status} · {fact.value ? JSON.stringify(fact.value) : '尚未确认'} · 有效至 {new Date(fact.expires_at).toLocaleTimeString()}<br /><small>{fact.source_ref}</small></p>)}
              </details>
              <div className="decision-footer">
                <div>
                  <p className="eyebrow">预计审批流程</p>
                  <div className="route-list"><Route size={17} /> {run.recommendation.best_overall.approval_route.length > 0 ? run.recommendation.best_overall.approval_route.map(approvalName).join(' → ') : '无需额外审批'}</div>
                  <p>确认后会重新校验报价、预算、制度与权限，只创建未提交草稿。</p>
                </div>
                {run.state === 'DRAFT_CREATED' ? (
                  <div className="draft-success"><FileCheck2 size={20} /><div><strong>采购草稿已创建</strong><span>尚未提交，可继续检查</span></div></div>
                ) : (
                  <button className="primary-button" type="button" onClick={createDraft} disabled={loading || !run.action_token || run.state !== 'WAITING_CONFIRMATION' || messageRoute?.route === 'CLARIFY'}>
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
                  <p className="eyebrow">可选偏好设置</p>
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

          {messageRoute?.route === 'CLARIFY' && (
            <section className="message-route-choice" aria-label="确认采购需求归属">
              <p>{messageRoute.choices.includes('CONTINUE')
                ? '这条消息是补充或调整当前采购，还是开始新的采购？'
                : '当前采购已结束，是否开始新的采购？'}</p>
              <div>
                {messageRoute.choices.map((choice) => (
                  <button key={choice} type="button" disabled={loading}
                    onClick={() => void submitMessage(undefined, choice)}>
                    {choice === 'CONTINUE' ? '继续当前采购' : '开始新的采购'}
                  </button>
                ))}
              </div>
              <small>开始新的采购不会继承当前需求条件。</small>
            </section>
          )}
          {recoveryNotice && <div className="recovery-banner" role="status"><ShieldCheck size={18} /><span>{recoveryNotice}</span></div>}
          {error && <div className="error-banner" role="alert"><CircleAlert size={18} /><span>{error}</span></div>}
        </section>

        {(
          <form className="composer" onSubmit={submitMessage}>
            <label htmlFor="procurement-message">采购需求或补充信息</label>
            <div className="composer__row">
              <textarea id="procurement-message" value={input} onChange={(event) => setInput(event.target.value)} rows={2} placeholder="例如：采购 20 台开发笔记本……" disabled={loading} />
              <button type="submit" disabled={loading || !input.trim()} aria-label="发送采购需求">
                {loading ? <LoaderCircle className="spin" size={20} /> : <Send size={20} />}
              </button>
            </div>
            <div className="composer__meta"><span><Sparkles size={14} /> {isLiveModel ? `${modelProviderName(runtimeStatus?.model_provider)} 真实模型` : '离线演示 · 不产生模型费用'}</span><span>Enter 发送</span></div>
          </form>
        )}
      </main>

      <aside className={`inspector ${traceOpen ? 'inspector--open' : ''}`} aria-label="技术运行详情">
        <div className="inspector-header">
          <div><p className="eyebrow">技术信息</p><h2>运行轨迹</h2></div>
          <span className="live-dot">实时</span>
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
