import {
  DEFAULT_POTENCY_CRITERION,
  isPotencyCriterion,
  type AnalysisInputResponse,
} from '../analysis-input/api'
import type { AnalysisStageName, AnalysisStageStatus } from './contracts'
import { parseTargetPrioritization, type TargetPrioritization } from './targetPrioritization'

const ANALYSES_ENDPOINT = '/api/v1/analyses'
const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export type AnalysisStatus = 'queued' | 'running' | 'completed' | 'partial_failure' | 'failed'

export type AnalysisStage = {
  name: AnalysisStageName
  status: AnalysisStageStatus
  position: number
  updated_at: string
}

export type AnalysisEvent = {
  event_type: string
  status: AnalysisStatus
  reason_code: string | null
  created_at: string
}

export type Analysis = {
  target_prioritization?: TargetPrioritization | null
  analysis_id: string
  status: AnalysisStatus
  input: AnalysisInputResponse
  stages: AnalysisStage[]
  events: AnalysisEvent[]
  error_code: string | null
  created_at: string
  updated_at: string
}

export type AnalysisRequestErrorCode =
  | 'invalid_session'
  | 'invalid_input'
  | 'analysis_not_found'
  | 'rate_limited'
  | 'service_busy'
  | 'network_error'
  | 'unexpected_response'

export class AnalysisRequestError extends Error {
  readonly code: AnalysisRequestErrorCode
  readonly retryAfterSeconds: number | null

  constructor(
    code: AnalysisRequestErrorCode,
    message: string,
    retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.name = 'AnalysisRequestError'
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
  }
}

/** 검증된 입력으로 영속 분석 작업을 생성한다. */
export async function createAnalysis(
  input: AnalysisInputResponse,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Analysis> {
  return requestAnalysis(ANALYSES_ENDPOINT, {
    method: 'POST',
    signal,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify({
      disease_id: input.disease_id,
      disease_name: input.disease_name,
      target_mode: input.target_mode,
      ...(input.target_name === null ? {} : { target_name: input.target_name }),
      smiles: input.original_smiles,
      potency_criterion: DEFAULT_POTENCY_CRITERION,
    }),
  })
}

/** PostgreSQL에 저장된 분석과 단계의 최신 상태를 조회한다. */
export async function readAnalysis(analysisId: string, signal?: AbortSignal): Promise<Analysis> {
  return requestAnalysis(`${ANALYSES_ENDPOINT}/${encodeURIComponent(analysisId)}`, {
    method: 'GET',
    signal,
    headers: { Accept: 'application/json' },
  })
}

async function requestAnalysis(path: string, init: RequestInit): Promise<Analysis> {
  return parseAnalysis(await requestAnalysisData(path, init))
}

/** 공통 인증·HTTP 오류 처리. 반환된 unknown은 각 공개 계약 parser가 검증한다. */
export async function requestAnalysisData(path: string, init: RequestInit): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}${path}`, { ...init, credentials: 'include' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new AnalysisRequestError('network_error', 'The analysis service is unreachable.')
  }

  const body = await readResponseBody(response)
  if (!response.ok) throw createRequestError(response, body)
  return body
}

async function readResponseBody(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function createRequestError(response: Response, value: unknown): AnalysisRequestError {
  const responseCode = readErrorCode(value)
  if (response.status === 401 || responseCode === 'invalid_session') {
    return new AnalysisRequestError('invalid_session', 'Authentication is required.')
  }
  if (response.status === 404 || responseCode === 'analysis_not_found') {
    return new AnalysisRequestError('analysis_not_found', 'The analysis does not exist.')
  }
  if (response.status === 429 || responseCode === 'api_rate_limited') {
    return new AnalysisRequestError(
      'rate_limited',
      'The analysis request limit was reached.',
      readRetryAfter(response),
    )
  }
  if (response.status === 503 || responseCode === 'api_busy') {
    return new AnalysisRequestError('service_busy', 'The analysis service is busy.')
  }
  if (response.status === 422) {
    return new AnalysisRequestError('invalid_input', 'The analysis input is invalid.')
  }
  return new AnalysisRequestError(
    'unexpected_response',
    `Analysis request failed with status ${response.status}.`,
  )
}

function readRetryAfter(response: Response): number | null {
  const value = response.headers.get('Retry-After')
  if (value === null) return null
  const seconds = Number.parseInt(value, 10)
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null
}

function parseAnalysis(value: unknown): Analysis {
  if (
    !isRecord(value) ||
    typeof value.analysis_id !== 'string' ||
    !isAnalysisStatus(value.status) ||
    !isAnalysisInput(value.input) ||
    !Array.isArray(value.stages) ||
    !Array.isArray(value.events) ||
    (value.error_code !== null && typeof value.error_code !== 'string') ||
    typeof value.created_at !== 'string' ||
    typeof value.updated_at !== 'string'
  ) {
    throw invalidResponse()
  }

  let targetPrioritization: TargetPrioritization | null
  try {
    targetPrioritization = parseTargetPrioritization(value.target_prioritization)
  } catch {
    throw invalidResponse()
  }
  return {
    analysis_id: value.analysis_id,
    target_prioritization: targetPrioritization,
    status: value.status,
    input: value.input,
    stages: value.stages.map(parseStage),
    events: value.events.map(parseEvent),
    error_code: value.error_code,
    created_at: value.created_at,
    updated_at: value.updated_at,
  }
}

function parseStage(value: unknown): AnalysisStage {
  if (
    !isRecord(value) ||
    !isStageName(value.name) ||
    !isStageStatus(value.status) ||
    typeof value.position !== 'number' ||
    typeof value.updated_at !== 'string'
  ) {
    throw invalidResponse()
  }
  return {
    name: value.name,
    status: value.status,
    position: value.position,
    updated_at: value.updated_at,
  }
}

function parseEvent(value: unknown): AnalysisEvent {
  if (
    !isRecord(value) ||
    typeof value.event_type !== 'string' ||
    !isAnalysisStatus(value.status) ||
    (value.reason_code !== null && typeof value.reason_code !== 'string') ||
    typeof value.created_at !== 'string'
  ) {
    throw invalidResponse()
  }
  return {
    event_type: value.event_type,
    status: value.status,
    reason_code: value.reason_code,
    created_at: value.created_at,
  }
}

function isAnalysisInput(value: unknown): value is AnalysisInputResponse {
  return (
    isRecord(value) &&
    typeof value.disease_id === 'string' &&
    typeof value.disease_name === 'string' &&
    (value.target_mode === 'discover' || value.target_mode === 'specified') &&
    (value.target_name === null || typeof value.target_name === 'string') &&
    typeof value.original_smiles === 'string' &&
    typeof value.canonical_smiles === 'string' &&
    (value.potency_criterion === undefined ||
      value.potency_criterion === null ||
      isPotencyCriterion(value.potency_criterion))
  )
}

function isAnalysisStatus(value: unknown): value is AnalysisStatus {
  return (
    value === 'queued' ||
    value === 'running' ||
    value === 'completed' ||
    value === 'partial_failure' ||
    value === 'failed'
  )
}

function isStageName(value: unknown): value is AnalysisStageName {
  return (
    value === 'target_hypothesis' || value === 'admet' || value === 'dta' || value === 'decision'
  )
}

function isStageStatus(value: unknown): value is AnalysisStageStatus {
  return (
    value === 'pending' ||
    value === 'running' ||
    value === 'completed' ||
    value === 'failed' ||
    value === 'skipped'
  )
}

function readErrorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') {
    return null
  }
  return value.detail.code
}

function invalidResponse(): AnalysisRequestError {
  return new AnalysisRequestError(
    'unexpected_response',
    'Analysis response did not match the expected shape.',
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
