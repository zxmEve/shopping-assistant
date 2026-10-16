import type { Run, EvalSummary, LongTermMemory, MemoryCandidate, RunResponse, RuntimeStatus } from './types'

const headers = {
  'Content-Type': 'application/json',
  Authorization: 'Bearer fixture-session-cn-employee-001',
}

class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, headers: { ...headers, ...init?.headers } })
  const payload = await response.json()
  if (!response.ok) {
    throw new ApiRequestError(
      payload?.error?.message ?? '服务暂时不可用，请稍后重试。',
      response.status,
      payload?.error?.code,
    )
  }
  return payload as T
}

export function startRun(message: string, scenario?: string): Promise<RunResponse> {
  return request('/api/runs', {
    method: 'POST',
    body: JSON.stringify({ message, scenario: scenario || null }),
  })
}

export function continueRun(
  runId: string, message: string, scenario?: string,
  routing?: { request_id: string; route_choice?: 'CONTINUE' | 'NEW'; expected_requirement_version?: number; expected_recommendation_version?: number; expected_resolution_draft_id?: string; expected_resolution_version?: number },
): Promise<RunResponse> {
  return request(`/api/runs/${runId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message, scenario: scenario || null, ...routing }),
  })
}

export function resumeRun(runId: string): Promise<RunResponse> {
  return request(`/api/runs/${runId}/workflow/resume`, { method: 'POST' })
}

export function isWorkflowRecoveryRequired(error: unknown): boolean {
  return error instanceof ApiRequestError
    && error.status === 409
    && error.code === 'INVALID_STATE'
    && error.message.startsWith('Workflow cannot resume from')
}

export function confirmRun(runId: string, actionToken: string): Promise<RunResponse> {
  return request(`/api/runs/${runId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ action_token: actionToken }),
  })
}

export function getEvalSummary(): Promise<EvalSummary> {
  return request('/api/evals/summary')
}

export function getRuntimeStatus(): Promise<RuntimeStatus> {
  return request('/api/agent/topology')
}

export function listMemoryCandidates(runId: string): Promise<MemoryCandidate[]> {
  const query = new URLSearchParams({ origin_run_id: runId })
  return request(`/api/memory-candidates?${query}`)
}

export function acceptMemoryCandidate(candidate: MemoryCandidate): Promise<LongTermMemory> {
  return request(`/api/memory-candidates/${candidate.candidate_id}/accept`, {
    method: 'POST',
    body: JSON.stringify({
      request_id: `accept-${candidate.candidate_id}`,
      displayed_text: candidate.proposed_content,
      consent_acknowledged: true,
    }),
  })
}

export function rejectMemoryCandidate(candidate: MemoryCandidate): Promise<MemoryCandidate> {
  return request(`/api/memory-candidates/${candidate.candidate_id}/reject`, {
    method: 'POST',
    body: JSON.stringify({ request_id: `reject-${candidate.candidate_id}` }),
  })
}

export function getRun(runId: string): Promise<Run> {
  return request(`/api/runs/${encodeURIComponent(runId)}`)
}

export function confirmRequirements(runId: string, draft: {draft_id: string; version: number; content_hash: string}, requestId?: string): Promise<RunResponse> {
  return request(`/api/runs/${runId}/requirement-confirmations`, {method: 'POST', body: JSON.stringify({request_id: requestId ?? `requirement-${draft.draft_id}-${draft.version}`, draft_id: draft.draft_id, draft_version: draft.version, content_hash: draft.content_hash})})
}
