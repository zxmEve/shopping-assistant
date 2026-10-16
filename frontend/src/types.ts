export type Money = { amount: string; currency: string }

export type TraceEvent = {
  event_id: string
  timestamp: string
  actor: string
  kind: string
  name: string
  status: string
  detail: Record<string, unknown>
  duration_ms?: number
}

export type MatchDecision = {
  canonical_product_id?: string
  canonical_variant_id?: string
  confidence: number
  matched_fields: string[]
  conflict_fields: string[]
  decision: 'MATCH' | 'REVIEW_REQUIRED' | 'REJECTED'
  reasons: string[]
  evidence_refs: string[]
}

export type OfferMatchAssessment = {
  raw_offer_id: string
  provider: string
  title: string
  condition?: 'NEW' | 'REFURBISHED' | 'USED'
  bundle?: 'DEVICE_ONLY' | 'DOCK_BUNDLE'
  comparable: boolean
  match_decision: MatchDecision
}

export type Candidate = {
  offer: {
    offer_id: string
    provider: string
    seller: string
    title: string
    unit_price: Money
    delivery_days: number
    match_confidence: number
    match_decision: MatchDecision
    variant: {
      variant_id: string
      cpu: string
      ram_gb: number
      storage_gb: number
      operating_system: string
      warranty_months: number
      condition: string
      bundle: 'DEVICE_ONLY' | 'DOCK_BUNDLE'
    }
    product: { brand: string; model: string }
    evidence: { evidence_id: string; source: string; fetched_at: string; expires_at: string }
  }
  cost: {
    subtotal: Money
    tax: Money
    shipping: Money
    unconditional_total: Money
    conditional_total?: Money
  }
  policy_decision: string
  approval_route: string[]
  policy_reasons: string[]
  evidence_refs: string[]
  warnings: string[]
  score: number
}

export type Recommendation = {
  lowest_cost: Candidate
  fastest_delivery: Candidate
  best_overall: Candidate
  compared_offer_count: number
  rejected_offer_count: number
  match_rejected_count: number
  review_required_count: number
  normalization_issues: OfferMatchAssessment[]
  summary: string
}

export type Message = { role: string; content: string; created_at: string }

export type ClarificationQuestion = {
  question_id: string
  field: string
  field_ids: string[]
  text: string
  resolves_conflict_id?: string | null
}

export type RequirementSpec = {
  schema_version: string
  version: number
  status: 'NEEDS_CLARIFICATION' | 'READY_FOR_SOURCING'
  field_resolutions: Record<string, {
    state: 'UNRESOLVED' | 'EXPLICIT' | 'NO_PREFERENCE' | 'AUTHORIZED_DEFAULT' | 'CONFIRMED_REFERENCE' | 'PROPOSED_REFERENCE'
    evidence_ids: string[]
    source_ref?: string | null
  } | null>
  clarification_plan: {
    questions: ClarificationQuestion[]
    covered_fields: string[]
    repaired_question_count: number
  }
}

export type Run = {
  run_id: string
  tenant_id: string
  user_id: string
  state: string
  scenario?: string
  clarification_rounds: number
  requirement_spec?: RequirementSpec | null
  wait_reason?: string | null
  message_routes: {request_id: string; message: string; scenario?: string; status: string; base_requirement_version: number; base_recommendation_version: number; expected_requirement_version?: number; expected_recommendation_version?: number; expected_resolution_draft_id?: string; expected_resolution_version?: number; proposal: {reason: string}; source: 'model' | 'deterministic' | 'fallback' | 'user'}[]
  resolution_draft?: {
    draft_id: string; version: number; content_hash: string; base_requirement_version: number
    status: 'HISTORY_SELECTION' | 'NEEDS_INPUT' | 'BLOCKED' | 'READY' | 'CONFIRMED'
    proposed_spec: RequirementSpec
    fields: {field: string; value: string | number | boolean; source: 'USER' | 'POLICY' | 'HISTORY'; source_ref: string; reason: string}[]
    changes: string[]; missing_fields: string[]; blockers: string[]
  } | null
  reference_policy?: {source_version: string; effective_from?: string | null; citations: {document_title: string; document_version: number; heading: string; clause_ref?: string | null; excerpt: string; source_ref: string}[]} | null
  reference_history?: {detail: string; orders: {order_id: string; purchased_at: string; status: string; model_text: string; simulated: boolean}[]} | null
  requirement_confirmations: {request_id: string; draft_id: string; draft_version: number; content_hash: string; status: string}[]
  recommendation_version: number
  negotiation_options: { option_id: string; base_requirement_version: number; expires_at: string; adjustments: {field: string; previous: string | number; proposed: string | number}[] }[]
  fact_evidence: {fact_id: string; provider: string; offer_id: string; kind: string; status: string; value: Record<string, string | number> | null; source_ref: string; fetched_at: string; expires_at: string}[]
  requirement: {
    version: number
    quantity?: number
    unit_budget?: Money
    budget_tax_included?: boolean
    delivery_city?: string
    delivery_days?: number
    minimum_ram_gb?: number
    minimum_storage_gb?: number
    assumptions: string[]
  }
  messages: Message[]
  trace: TraceEvent[]
  recommendation?: Recommendation
  quote_snapshot?: {
    offer_id: string
    provider: string
    unit_price: Money
    shipping: Money
    stock: number
    delivery_days: number
    fingerprint: string
  }
  revalidation_changes: {
    field: string
    previous: string
    current: string
    requires_confirmation: boolean
  }[]
  action_token?: string
  draft?: {
    draft_id: string
    status: string
    approval_route: string[]
  }
}

export type MessageRouteResult = {
  request_id: string
  route: 'CONTINUE' | 'NEW' | 'CLARIFY'
  origin_run_id: string
  target_run_id?: string | null
  reason: string
  source: 'model' | 'deterministic' | 'fallback' | 'user'
  choices: ('CONTINUE' | 'NEW')[]
}

export type RunResponse = {
  message_route?: MessageRouteResult | null
  run: Run
  assistant_message: string
  suggested_replies: string[]
}

export type EvalSummary = {
  status: string
  total: number
  passed: number
  task_success_rate?: number
  veto_failures: string[]
  metrics?: Record<string, number>
}

export type RuntimeStatus = {
  mode: 'deterministic' | 'openai' | string
  model: string
  model_provider: string
}

export type MemoryCandidate = {
  candidate_id: string
  origin_run_id: string
  scope: 'PROFILE' | 'TEAM' | 'EPISODIC'
  key: string
  key_definition_version: number
  category_id: string
  proposed_content: string
  proposed_value_json: Record<string, unknown>
  tags: string[]
  source_message_refs: string[]
  source_ref: string
  confidence: number
  conflict_type: 'NEW' | 'REPLACES_EXISTING'
  status: 'PENDING' | 'ACCEPTED' | 'REJECTED' | 'EXPIRED'
  expires_at: string
  accepted_memory_id?: string
}

export type LongTermMemory = {
  memory_id: string
  key: string
  content: string
  value_json: Record<string, unknown>
  source_ref: string
}
