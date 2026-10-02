const SESSION_ENDPOINT = '/api/v1/auth/session'

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export type SessionResponse = {
  authenticated: true
  expires_at: string
}

export type AuthenticationErrorCode =
  | 'invalid_access_code'
  | 'too_many_auth_attempts'
  | 'auth_unavailable'
  | 'origin_not_allowed'
  | 'unexpected_response'

/**
 * 인증 API가 반환한 예상 가능한 실패를 상태 코드 및 오류 코드와 함께 전달한다.
 */
export class AuthenticationRequestError extends Error {
  readonly code: AuthenticationErrorCode
  readonly retryAfterSeconds: number | null

  constructor(
    code: AuthenticationErrorCode,
    message: string,
    retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.name = 'AuthenticationRequestError'
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
  }
}

/** 현재 브라우저의 인증 쿠키를 확인하며 미인증 상태는 null로 반환한다. */
export async function readCurrentSession(): Promise<SessionResponse | null> {
  const response = await fetch(buildApiUrl(SESSION_ENDPOINT), {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })

  if (response.status === 401) {
    return null
  }

  if (!response.ok) {
    throw await createAuthenticationError(response)
  }

  return parseSessionResponse(await readResponseBody(response))
}

/** 공용 접근 코드를 검증하고 HttpOnly 세션 쿠키를 발급받는다. */
export async function createAuthenticatedSession(accessCode: string): Promise<SessionResponse> {
  const response = await fetch(buildApiUrl(SESSION_ENDPOINT), {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ access_code: accessCode }),
  })

  if (!response.ok) {
    throw await createAuthenticationError(response)
  }

  return parseSessionResponse(await readResponseBody(response))
}

/** 서버 세션 쿠키를 만료시켜 현재 브라우저를 로그아웃한다. */
export async function deleteAuthenticatedSession(): Promise<void> {
  const response = await fetch(buildApiUrl(SESSION_ENDPOINT), {
    method: 'DELETE',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })

  if (!response.ok) {
    throw await createAuthenticationError(response)
  }
}

function buildApiUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

async function createAuthenticationError(response: Response): Promise<AuthenticationRequestError> {
  const responseBody = await readResponseBody(response)
  const responseCode = readErrorCode(responseBody)
  const code = normalizeErrorCode(responseCode, response.status)
  const retryAfterSeconds = readRetryAfterSeconds(response.headers.get('Retry-After'))

  return new AuthenticationRequestError(
    code,
    `Authentication request failed with status ${response.status}`,
    retryAfterSeconds,
  )
}

async function readResponseBody(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function parseSessionResponse(value: unknown): SessionResponse {
  if (!isRecord(value) || value.authenticated !== true || typeof value.expires_at !== 'string') {
    throw new AuthenticationRequestError(
      'unexpected_response',
      'Authentication response did not match the expected session shape',
    )
  }

  return {
    authenticated: true,
    expires_at: value.expires_at,
  }
}

function readErrorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') {
    return null
  }

  return value.detail.code
}

function normalizeErrorCode(code: string | null, status: number): AuthenticationErrorCode {
  if (code === 'invalid_access_code') return 'invalid_access_code'
  if (code === 'too_many_auth_attempts') return 'too_many_auth_attempts'
  if (code === 'auth_unavailable') return 'auth_unavailable'
  if (code === 'origin_not_allowed') return 'origin_not_allowed'

  if (status === 401) return 'invalid_access_code'
  if (status === 429) return 'too_many_auth_attempts'
  if (status === 503) return 'auth_unavailable'
  if (status === 403) return 'origin_not_allowed'
  return 'unexpected_response'
}

function readRetryAfterSeconds(value: string | null): number | null {
  if (value === null) return null

  const seconds = Number.parseInt(value, 10)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
