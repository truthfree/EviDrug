import { useEffect, useState } from 'react'
import {
  DiseaseSearchRequestError,
  searchDiseases,
  type DiseaseCandidate,
} from './api'

const SEARCH_DELAY_MS = 350
const MINIMUM_QUERY_LENGTH = 2

type DiseaseSearchState =
  | { status: 'idle'; candidates: DiseaseCandidate[]; message: null }
  | { status: 'loading'; candidates: DiseaseCandidate[]; message: null }
  | { status: 'ready'; candidates: DiseaseCandidate[]; message: null }
  | { status: 'empty'; candidates: DiseaseCandidate[]; message: string }
  | { status: 'error'; candidates: DiseaseCandidate[]; message: string }

const initialState: DiseaseSearchState = {
  status: 'idle',
  candidates: [],
  message: null,
}

/** 입력 중 요청을 줄이고 오래된 응답이 새 검색을 덮어쓰지 않게 관리한다. */
export function useDiseaseSearch(query: string): DiseaseSearchState {
  const normalizedQuery = query.trim()
  const [storedSearch, setStoredSearch] = useState<{
    query: string
    state: DiseaseSearchState
  }>({ query: '', state: initialState })

  useEffect(() => {
    if (normalizedQuery.length < MINIMUM_QUERY_LENGTH) {
      return
    }

    const abortController = new AbortController()
    const timeoutId = window.setTimeout(() => {
      setStoredSearch({
        query: normalizedQuery,
        state: { status: 'loading', candidates: [], message: null },
      })
      void searchDiseases(normalizedQuery, abortController.signal)
        .then((response) => {
          setStoredSearch({
            query: normalizedQuery,
            state: { status: 'ready', candidates: response.candidates, message: null },
          })
        })
        .catch((error: unknown) => {
          if (abortController.signal.aborted) return
          setStoredSearch({ query: normalizedQuery, state: toErrorState(error) })
        })
    }, SEARCH_DELAY_MS)

    return () => {
      window.clearTimeout(timeoutId)
      abortController.abort()
    }
  }, [normalizedQuery])

  if (normalizedQuery.length < MINIMUM_QUERY_LENGTH || storedSearch.query !== normalizedQuery) {
    return initialState
  }
  return storedSearch.state
}

function toErrorState(error: unknown): DiseaseSearchState {
  if (error instanceof DiseaseSearchRequestError) {
    if (error.code === 'disease_candidates_not_found') {
      return {
        status: 'empty',
        candidates: [],
        message: '일치하는 질환을 찾지 못했습니다. 철자나 더 짧은 표현을 확인해 주세요.',
      }
    }
    if (error.code === 'invalid_session') {
      return {
        status: 'error',
        candidates: [],
        message: '세션이 만료되었습니다. 로그아웃 후 다시 로그인해 주세요.',
      }
    }
    if (error.code === 'invalid_query') {
      return {
        status: 'error',
        candidates: [],
        message: '질환명 또는 표현형을 영어로 두 글자 이상 입력해 주세요.',
      }
    }
  }

  return {
    status: 'error',
    candidates: [],
    message: '질환 검색에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',
  }
}
