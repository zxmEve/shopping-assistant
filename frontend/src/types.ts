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

export type Run = {
  run_id: string
  tenant_id: string
  user_id: string
  state: string
  scenario?: string
  clarification_rounds: number
  requirement: {
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

export type RunResponse = {
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
