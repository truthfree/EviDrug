import { requestAnalysisData, type AnalysisStatus } from './api'
import { isAnalysisId } from './specialistResults'

export type RecentAnalysis = {
  analysis_id: string
  status: AnalysisStatus
  disease_name: string
  target_mode: 'discover' | 'specified'
  target_name: string | null
  canonical_smiles: string
  created_at: string
}

export async function readRecentAnalyses(signal?: AbortSignal): Promise<RecentAnalysis[]> {
  const response = await requestAnalysisData('/api/v1/analyses', {
    method: 'GET',
    signal,
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  if (!isRecord(response) || !Array.isArray(response.items) || !response.items.every(isRecentAnalysis)) {
    throw new Error('Invalid recent analyses response')
  }
  return response.items
}

function isRecentAnalysis(value: unknown): value is RecentAnalysis {
  return isRecord(value)
    && typeof value.analysis_id === 'string'
    && isAnalysisId(value.analysis_id)
    && typeof value.status === 'string'
    && ['queued', 'running', 'completed', 'partial_failure', 'failed'].includes(value.status)
    && typeof value.disease_name === 'string'
    && (value.target_mode === 'discover' || value.target_mode === 'specified')
    && (value.target_name === null || typeof value.target_name === 'string')
    && typeof value.canonical_smiles === 'string'
    && typeof value.created_at === 'string'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
