import { useId, useState, type ChangeEvent, type KeyboardEvent } from 'react'
import type { DiseaseCandidate } from './api'
import { useDiseaseSearch } from './useDiseaseSearch'
import './DiseaseSearchPanel.css'

type DiseaseSearchPanelProps = {
  onConfirmedDiseaseChange: (candidate: DiseaseCandidate | null) => void
}

/** 영어 질환명을 자동완성하고 사용자의 명시적인 후보 확인을 받는다. */
export function DiseaseSearchPanel({ onConfirmedDiseaseChange }: DiseaseSearchPanelProps) {
  const listboxId = useId()
  const [query, setQuery] = useState('')
  const [selectedCandidate, setSelectedCandidate] = useState<DiseaseCandidate | null>(null)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [isConfirmed, setIsConfirmed] = useState(false)
  const search = useDiseaseSearch(selectedCandidate === null ? query : '')
  const hasOptions = search.status === 'ready' && search.candidates.length > 0
  const activeOptionId = activeIndex >= 0 ? `${listboxId}-option-${activeIndex}` : undefined

  function handleQueryChange(event: ChangeEvent<HTMLInputElement>) {
    setQuery(event.target.value)
    setSelectedCandidate(null)
    setIsConfirmed(false)
    setActiveIndex(-1)
    onConfirmedDiseaseChange(null)
  }

  function selectCandidate(candidate: DiseaseCandidate) {
    setSelectedCandidate(candidate)
    setQuery(candidate.name)
    setActiveIndex(-1)
    setIsConfirmed(false)
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!hasOptions) return

    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => Math.min(current + 1, search.candidates.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => Math.max(current - 1, 0))
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault()
      selectCandidate(search.candidates[activeIndex])
    } else if (event.key === 'Escape') {
      setActiveIndex(-1)
    }
  }

  return (
    <section className="disease-search-card" aria-labelledby="disease-search-title">
      <div className="disease-search-card__heading">
        <span className="disease-search-card__number">01</span>
        <div>
          <p className="section-label">Disease context</p>
          <h2 id="disease-search-title">어떤 질환을 살펴볼까요?</h2>
          <p>영어 질환명 또는 표현형을 입력하고 Open Targets의 표준 후보를 확인해 주세요.</p>
        </div>
      </div>

      <div className="disease-search-field">
        <label htmlFor="disease-query">Disease or phenotype</label>
        <div className="disease-search-input-wrap">
          <input
            id="disease-query"
            type="text"
            role="combobox"
            value={query}
            placeholder="e.g. Alzheimer disease"
            autoComplete="off"
            aria-autocomplete="list"
            aria-controls={listboxId}
            aria-expanded={hasOptions}
            aria-activedescendant={activeOptionId}
            aria-describedby="disease-query-help"
            onChange={handleQueryChange}
            onKeyDown={handleInputKeyDown}
          />
          {search.status === 'loading' && (
            <span className="disease-search-spinner" aria-label="질환 후보 검색 중" />
          )}
        </div>
        <p className="disease-search-example">
          Demo Guide <code>Breast cancer</code>
        </p>
        <p id="disease-query-help" className="disease-search-help">
          두 글자 이상 입력하면 후보를 검색합니다. 현재 영어 입력만 지원합니다.
        </p>

        {hasOptions && selectedCandidate === null && (
          <div className="disease-search-results">
            <p>이 질환을 찾으신 건가요?</p>
            <ul id={listboxId} role="listbox" aria-label="질환 후보">
              {search.candidates.map((candidate, index) => (
                <li
                  id={`${listboxId}-option-${index}`}
                  key={candidate.id}
                  role="option"
                  aria-selected={activeIndex === index}
                  className={activeIndex === index ? 'disease-option--active' : undefined}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => selectCandidate(candidate)}
                >
                  <span className="disease-option__title">
                    <strong>{candidate.name}</strong>
                    <code>{candidate.id}</code>
                  </span>
                  <span className="disease-option__description">
                    {candidate.description ?? 'Open Targets에 등록된 질환 또는 표현형입니다.'}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {(search.status === 'empty' || search.status === 'error') && (
          <p className={`disease-search-message disease-search-message--${search.status}`} role="alert">
            {search.message}
          </p>
        )}
      </div>

      <div className="disease-search-selection" aria-live="polite">
        {selectedCandidate ? (
          <div>
            <span>선택한 질환</span>
            <strong>{selectedCandidate.name}</strong>
            <code>{selectedCandidate.id}</code>
          </div>
        ) : (
          <p>후보를 선택하면 다음 단계로 진행할 수 있습니다.</p>
        )}
        <button
          type="button"
          className="disease-confirm-button"
          disabled={selectedCandidate === null || isConfirmed}
          onClick={() => {
            setIsConfirmed(true)
            if (selectedCandidate) onConfirmedDiseaseChange(selectedCandidate)
          }}
        >
          {isConfirmed ? '질환 확인 완료' : '선택한 질환으로 계속'}
        </button>
      </div>

      {isConfirmed && selectedCandidate && (
        <p className="disease-confirmation" role="status">
          <span aria-hidden="true">✓</span>
          {selectedCandidate.name}을 분석 질환으로 확인했습니다. 아래에서 타깃 방식과 화합물
          입력을 이어가세요.
        </p>
      )}
    </section>
  )
}
