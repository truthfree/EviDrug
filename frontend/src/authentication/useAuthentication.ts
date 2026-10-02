import { useCallback, useEffect, useState } from 'react'
import {
  AuthenticationRequestError,
  createAuthenticatedSession,
  deleteAuthenticatedSession,
  readCurrentSession,
  type SessionResponse,
} from './api'

export type AuthenticationState =
  | { status: 'checking' }
  | { status: 'signed_out' }
  | { status: 'signed_in'; session: SessionResponse }
  | { status: 'unavailable'; message: string }

type AuthenticationActions = {
  state: AuthenticationState
  isSigningIn: boolean
  isSigningOut: boolean
  signInError: string | null
  signOutError: string | null
  clearSignInError: () => void
  retrySessionCheck: () => void
  signIn: (accessCode: string) => Promise<boolean>
  signOut: () => Promise<void>
}

/**
 * 앱이 사용하는 인증 상태와 세션 생성·확인·삭제 흐름을 한곳에서 관리한다.
 * 접근 코드는 API 호출 직후 보관하지 않으며 세션은 HttpOnly 쿠키에만 유지된다.
 */
export function useAuthentication(): AuthenticationActions {
  const [state, setState] = useState<AuthenticationState>({ status: 'checking' })
  const [sessionCheckSequence, setSessionCheckSequence] = useState(0)
  const [isSigningIn, setIsSigningIn] = useState(false)
  const [isSigningOut, setIsSigningOut] = useState(false)
  const [signInError, setSignInError] = useState<string | null>(null)
  const [signOutError, setSignOutError] = useState<string | null>(null)

  useEffect(() => {
    let isCurrentCheck = true

    readCurrentSession().then(
      (session) => {
        if (!isCurrentCheck) return
        setState(session ? { status: 'signed_in', session } : { status: 'signed_out' })
      },
      () => {
        if (!isCurrentCheck) return
        setState({
          status: 'unavailable',
          message: '인증 서버에 연결할 수 없습니다. 공개 안내는 계속 확인할 수 있습니다.',
        })
      },
    )

    return () => {
      isCurrentCheck = false
    }
  }, [sessionCheckSequence])

  const retrySessionCheck = useCallback(() => {
    setState({ status: 'checking' })
    setSessionCheckSequence((currentSequence) => currentSequence + 1)
  }, [])

  const clearSignInError = useCallback(() => {
    setSignInError(null)
  }, [])

  const signIn = useCallback(async (accessCode: string): Promise<boolean> => {
    setIsSigningIn(true)
    setSignInError(null)

    try {
      const session = await createAuthenticatedSession(accessCode)
      setState({ status: 'signed_in', session })
      return true
    } catch (error: unknown) {
      setSignInError(createSignInMessage(error))
      return false
    } finally {
      setIsSigningIn(false)
    }
  }, [])

  const signOut = useCallback(async (): Promise<void> => {
    setIsSigningOut(true)
    setSignOutError(null)

    try {
      await deleteAuthenticatedSession()
      setState({ status: 'signed_out' })
    } catch {
      setSignOutError('로그아웃 요청을 완료하지 못했습니다. 연결을 확인하고 다시 시도해 주세요.')
    } finally {
      setIsSigningOut(false)
    }
  }, [])

  return {
    state,
    isSigningIn,
    isSigningOut,
    signInError,
    signOutError,
    clearSignInError,
    retrySessionCheck,
    signIn,
    signOut,
  }
}

function createSignInMessage(error: unknown): string {
  if (!(error instanceof AuthenticationRequestError)) {
    return '인증 서버에 연결할 수 없습니다. 네트워크 상태를 확인하고 다시 시도해 주세요.'
  }

  if (error.code === 'invalid_access_code') {
    return '접근 코드가 올바르지 않습니다. 전달받은 코드를 다시 확인해 주세요.'
  }

  if (error.code === 'too_many_auth_attempts') {
    if (error.retryAfterSeconds !== null) {
      const retryAfterMinutes = Math.max(1, Math.ceil(error.retryAfterSeconds / 60))
      return `인증 시도가 잠시 제한되었습니다. 약 ${retryAfterMinutes}분 뒤 다시 시도해 주세요.`
    }
    return '인증 시도가 잠시 제한되었습니다. 잠시 후 다시 시도해 주세요.'
  }

  if (error.code === 'origin_not_allowed') {
    return '현재 접속 주소에서는 인증할 수 없습니다. 서비스 관리자에게 접속 주소를 확인해 주세요.'
  }

  if (error.code === 'auth_unavailable') {
    return '인증 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.'
  }

  return '예상하지 못한 응답을 받았습니다. 잠시 후 다시 시도해 주세요.'
}
