const DISEASE_SEARCH_ENDPOINT = '/api/v1/diseases/search'

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export type DiseaseCandidate = {
  id: string
  name: string
  description: string | null
  relevance_score: number
}

export type DiseaseSearchResponse = {
  query: string
  candidates: DiseaseCandidate[]
  requires_confirmation: true
  source: 'open_targets'
}

export type DiseaseSearchErrorCode =
  | 'disease_candidates_not_found'
  | 'disease_search_unavailable'
  | 'invalid_session'
  | 'invalid_query'
  | 'unexpected_response'

export class DiseaseSearchRequestError extends Error {
  readonly code: DiseaseSearchErrorCode

  constructor(code: DiseaseSearchErrorCode, message: string) {
    super(message)
    this.name = 'DiseaseSearchRequestError'
    this.code = code
  }
}

/** 영어 질환 검색어를 Open Targets 기반 후보 목록으로 변환한다. */
export async function searchDiseases(
  query: string,
  signal?: AbortSignal,
): Promise<DiseaseSearchResponse> {
  const response = await fetch(`${apiBaseUrl}${DISEASE_SEARCH_ENDPOINT}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ query }),
    signal,
  })

  const responseBody = await readResponseBody(response)
  if (!response.ok) {
    throw createDiseaseSearchError(response.status, responseBody)
  }
  return parseDiseaseSearchResponse(responseBody)
}

async function readResponseBody(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function createDiseaseSearchError(status: number, value: unknown): DiseaseSearchRequestError {
  const responseCode = readErrorCode(value)

  if (status === 404 || responseCode === 'disease_candidates_not_found') {
    return new DiseaseSearchRequestError(
      'disease_candidates_not_found',
      'No matching disease was found.',
    )
  }
  if (status === 401 || responseCode === 'invalid_session') {
    return new DiseaseSearchRequestError('invalid_session', 'Authentication is required.')
  }
  if (status === 422) {
    return new DiseaseSearchRequestError('invalid_query', 'The disease query is invalid.')
  }
  if (status === 503 || responseCode === 'disease_search_unavailable') {
    return new DiseaseSearchRequestError(
      'disease_search_unavailable',
      'Disease search is unavailable.',
    )
  }
  return new DiseaseSearchRequestError(
    'unexpected_response',
    `Disease search failed with status ${status}.`,
  )
}

function parseDiseaseSearchResponse(value: unknown): DiseaseSearchResponse {
  if (
    !isRecord(value) ||
    typeof value.query !== 'string' ||
    value.requires_confirmation !== true ||
    value.source !== 'open_targets' ||
    !Array.isArray(value.candidates)
  ) {
    throw new DiseaseSearchRequestError(
      'unexpected_response',
      'Disease search response did not match the expected shape.',
    )
  }

  return {
    query: value.query,
    candidates: value.candidates.map(parseDiseaseCandidate),
    requires_confirmation: true,
    source: 'open_targets',
  }
}

function parseDiseaseCandidate(value: unknown): DiseaseCandidate {
  if (
    !isRecord(value) ||
    typeof value.id !== 'string' ||
    typeof value.name !== 'string' ||
    (value.description !== null && typeof value.description !== 'string') ||
    typeof value.relevance_score !== 'number'
  ) {
    throw new DiseaseSearchRequestError(
      'unexpected_response',
      'Disease candidate did not match the expected shape.',
    )
  }

  return {
    id: value.id,
    name: value.name,
    description: value.description,
    relevance_score: value.relevance_score,
  }
}

function readErrorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') {
    return null
  }
  return value.detail.code
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
