import type { EvalSummary, RunResponse } from './types'

const headers = {
  'Content-Type': 'application/json',
  Authorization: 'Bearer fixture-session-cn-employee-001',
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, headers: { ...headers, ...init?.headers } })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.error?.message ?? '服务暂时不可用，请稍后重试。')
  }
  return payload as T
}

export function startRun(message: string, scenario?: string): Promise<RunResponse> {
  return request('/api/runs', {
    method: 'POST',
    body: JSON.stringify({ message, scenario: scenario || null }),
  })
}

export function continueRun(runId: string, message: string, scenario?: string): Promise<RunResponse> {
  return request(`/api/runs/${runId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message, scenario: scenario || null }),
  })
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
