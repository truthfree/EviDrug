const ANALYSIS_INPUT_ENDPOINT = '/api/v1/analysis-inputs/validate'

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export type TargetMode = 'discover' | 'specified'
export type PotencyUnit = 'pM' | 'nM' | 'uM' | 'mM' | 'M'

export type PotencyCriterion = {
  endpoint: 'Kd'
  maximum_value: number
  unit: PotencyUnit
}

export const DEFAULT_POTENCY_CRITERION: PotencyCriterion = {
  endpoint: 'Kd',
  maximum_value: 100,
  unit: 'nM',
}

export type AnalysisInputRequest = {
  disease_id: string
  disease_name: string
  target_mode: TargetMode
  target_name?: string
  smiles: string
  potency_criterion: PotencyCriterion
}

export type AnalysisInputResponse = {
  disease_id: string
  disease_name: string
  target_mode: TargetMode
  target_name: string | null
  original_smiles: string
  canonical_smiles: string
  potency_criterion?: PotencyCriterion | null
}

export type AnalysisInputErrorCode =
  | 'invalid_smiles'
  | 'invalid_input'
  | 'invalid_session'
  | 'unexpected_response'

export class AnalysisInputRequestError extends Error {
  readonly code: AnalysisInputErrorCode

  constructor(code: AnalysisInputErrorCode, message: string) {
    super(message)
    this.name = 'AnalysisInputRequestError'
    this.code = code
  }
}

/** 타깃 방식과 SMILES를 백엔드에서 검증하고 정규화된 입력을 반환한다. */
export async function validateAnalysisInput(
  request: AnalysisInputRequest,
): Promise<AnalysisInputResponse> {
  const response = await fetch(`${apiBaseUrl}${ANALYSIS_INPUT_ENDPOINT}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  })

  const responseBody = await readResponseBody(response)
  if (!response.ok) {
    throw createAnalysisInputError(response.status, responseBody)
  }
  return parseAnalysisInputResponse(responseBody)
}

async function readResponseBody(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function createAnalysisInputError(status: number, value: unknown): AnalysisInputRequestError {
  const responseCode = readErrorCode(value)

  if (status === 401 || responseCode === 'invalid_session') {
    return new AnalysisInputRequestError('invalid_session', 'Authentication is required.')
  }
  if (responseCode === 'invalid_smiles') {
    return new AnalysisInputRequestError('invalid_smiles', 'The SMILES input is invalid.')
  }
  if (status === 422) {
    return new AnalysisInputRequestError('invalid_input', 'The analysis input is invalid.')
  }
  return new AnalysisInputRequestError(
    'unexpected_response',
    `Analysis input validation failed with status ${status}.`,
  )
}

function parseAnalysisInputResponse(value: unknown): AnalysisInputResponse {
  if (
    !isRecord(value) ||
    typeof value.disease_id !== 'string' ||
    typeof value.disease_name !== 'string' ||
    !isTargetMode(value.target_mode) ||
    (value.target_name !== null && typeof value.target_name !== 'string') ||
    typeof value.original_smiles !== 'string' ||
    typeof value.canonical_smiles !== 'string' ||
    !isPotencyCriterion(value.potency_criterion)
  ) {
    throw new AnalysisInputRequestError(
      'unexpected_response',
      'Analysis input response did not match the expected shape.',
    )
  }

  return {
    disease_id: value.disease_id,
    disease_name: value.disease_name,
    target_mode: value.target_mode,
    target_name: value.target_name,
    original_smiles: value.original_smiles,
    canonical_smiles: value.canonical_smiles,
    potency_criterion: value.potency_criterion,
  }
}

function readErrorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') {
    return null
  }
  return value.detail.code
}

function isTargetMode(value: unknown): value is TargetMode {
  return value === 'discover' || value === 'specified'
}

export function isPotencyCriterion(value: unknown): value is PotencyCriterion {
  return (
    isRecord(value) &&
    value.endpoint === 'Kd' &&
    typeof value.maximum_value === 'number' &&
    Number.isFinite(value.maximum_value) &&
    value.maximum_value > 0 &&
    ['pM', 'nM', 'uM', 'mM', 'M'].includes(String(value.unit))
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
