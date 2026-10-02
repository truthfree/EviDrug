import { useEffect, useMemo, useState } from 'react'
import type { MockScenarioId } from './contracts'
import { mockScenarios } from './mockScenarios'

const frameDurationMs = 900

/** 실제 API 대신 선택한 시나리오의 영속 상태 snapshot을 순서대로 재생한다. */
export function useMockAnalysis() {
  const [scenarioId, setScenarioId] = useState<MockScenarioId>('success')
  const [frameIndex, setFrameIndex] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const scenario = mockScenarios[scenarioId]
  const isTerminal = frameIndex === scenario.frames.length - 1

  useEffect(() => {
    if (!isPlaying || isTerminal) return
    const timer = window.setTimeout(() => {
      const nextFrameIndex = Math.min(frameIndex + 1, scenario.frames.length - 1)
      setFrameIndex(nextFrameIndex)
      if (nextFrameIndex === scenario.frames.length - 1) setIsPlaying(false)
    }, frameDurationMs)
    return () => window.clearTimeout(timer)
  }, [isPlaying, isTerminal, scenario.frames.length, frameIndex])

  return useMemo(
    () => ({
      scenario,
      frame: scenario.frames[frameIndex],
      frameIndex,
      isPlaying,
      isTerminal,
      selectScenario(nextScenarioId: MockScenarioId) {
        setScenarioId(nextScenarioId)
        setFrameIndex(0)
        setIsPlaying(false)
      },
      play() {
        if (isTerminal) setFrameIndex(0)
        setIsPlaying(true)
      },
      reset() {
        setFrameIndex(0)
        setIsPlaying(false)
      },
    }),
    [frameIndex, isPlaying, isTerminal, scenario],
  )
}
