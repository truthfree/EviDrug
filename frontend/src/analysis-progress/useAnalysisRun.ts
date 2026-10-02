import { useEffect, useRef, useState } from 'react'
import type { AnalysisInputResponse } from '../analysis-input/api'
import {
  AnalysisRequestError,
  createAnalysis,
  readAnalysis,
  type Analysis,
} from './api'

const pollingIntervalMs = 2_500
const maximumPollingFailures = 3

export type AnalysisRunPhase = 'idle' | 'creating' | 'polling' | 'terminal' | 'error'

/** 분석 생성 요청과 영속 상태 polling을 하나의 사용자 실행으로 관리한다. */
export function useAnalysisRun(input: AnalysisInputResponse) {
  const [phase, setPhase] = useState<AnalysisRunPhase>('idle')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const idempotencyKey = useRef<string | null>(null)
  const analysisId = analysis?.analysis_id ?? null

  useEffect(() => {
    if (phase !== 'polling' || analysisId === null) return
    const pollingAnalysisId = analysisId

    let cancelled = false
    let timer: number | undefined
    let requestController: AbortController | undefined

    async function poll(failureCount: number) {
      requestController = new AbortController()
      try {
        const latest = await readAnalysis(pollingAnalysisId, requestController.signal)
        if (cancelled) return
        setAnalysis(latest)
        setMessage(null)
        if (isTerminalStatus(latest.status)) {
          setPhase('terminal')
          return
        }
        timer = window.setTimeout(() => void poll(0), pollingIntervalMs)
      } catch (error) {
        if (cancelled || isAbortError(error)) return
        const nextFailureCount = failureCount + 1
        if (nextFailureCount >= maximumPollingFailures) {
          setMessage(toUserMessage(error, true))
          setPhase('error')
          return
        }
        setMessage(`상태 연결이 불안정해 다시 확인하고 있습니다. (${nextFailureCount}/3)`)
        const retryDelay = Math.min(pollingIntervalMs * 2 ** nextFailureCount, 10_000)
        timer = window.setTimeout(() => void poll(nextFailureCount), retryDelay)
      }
    }

    timer = window.setTimeout(() => void poll(0), pollingIntervalMs)
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
      requestController?.abort()
    }
  }, [analysisId, phase])

  async function start() {
    setPhase('creating')
    setMessage(null)
    if (idempotencyKey.current === null) idempotencyKey.current = createIdempotencyKey()
    try {
      const created = await createAnalysis(input, idempotencyKey.current)
      setAnalysis(created)
      setPhase(isTerminalStatus(created.status) ? 'terminal' : 'polling')
    } catch (error) {
      if (isAbortError(error)) return
      setMessage(toUserMessage(error, false))
      setPhase('error')
    }
  }

  async function retry() {
    if (analysis === null) {
      await start()
      return
    }
    setPhase('polling')
    setMessage('분석 상태를 다시 확인합니다.')
  }

  return {
    phase,
    analysis,
    message,
    start: () => void start(),
    retry: () => void retry(),
  }
}

export function isTerminalStatus(status: Analysis['status']): boolean {
  return status === 'completed' || status === 'partial_failure' || status === 'failed'
}

function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return `frontend:${crypto.randomUUID()}`
  return `frontend:${Date.now()}:${Math.random().toString(36).slice(2)}`
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function toUserMessage(error: unknown, polling: boolean): string {
  if (error instanceof AnalysisRequestError) {
    if (error.code === 'invalid_session') {
      return '세션이 만료되었습니다. 로그아웃 후 다시 로그인해 주세요.'
    }
    if (error.code === 'invalid_input') {
      return '분석 입력이 변경되었거나 유효하지 않습니다. 입력을 다시 확인해 주세요.'
    }
    if (error.code === 'analysis_not_found') {
      return '저장된 분석을 찾지 못했습니다. 새 분석을 시작해 주세요.'
    }
    if (error.code === 'rate_limited') {
      const retry = error.retryAfterSeconds
        ? ` 약 ${Math.max(1, Math.ceil(error.retryAfterSeconds / 60))}분 뒤`
        : ' 잠시 후'
      return `분석 실행 요청이 잠시 제한됐습니다.${retry} 다시 시도해 주세요.`
    }
    if (error.code === 'service_busy') {
      return '분석 서비스가 다른 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.'
    }
  }
  return polling
    ? '분석 상태를 계속 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.'
    : '분석 작업을 만들 수 없습니다. 잠시 후 다시 시도해 주세요.'
}
