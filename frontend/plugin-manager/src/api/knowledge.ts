import axios from 'axios'

let bridgeToken = ''
let bridgeTokenRequest: Promise<string> | null = null

interface KnowledgeEnvelope {
  ok?: boolean
  reason?: string
  error_type?: string
}

export class KnowledgeApiError extends Error {
  readonly reason: string
  readonly errorType?: string

  constructor(reason = 'operation_failed', errorType?: string) {
    super(reason)
    this.name = 'KnowledgeApiError'
    this.reason = reason
    this.errorType = errorType
  }
}

async function token(forceRefresh = false): Promise<string> {
  if (forceRefresh) bridgeToken = ''
  if (bridgeToken) return bridgeToken
  if (!bridgeTokenRequest) {
    bridgeTokenRequest = axios
      .get('/market/bridge-token', { timeout: 3000 })
      .then((response) => {
        bridgeToken = String(response.data?.bridge_token || '')
        if (!bridgeToken) throw new Error('knowledge bridge token unavailable')
        return bridgeToken
      })
  }
  const pending = bridgeTokenRequest
  try {
    return await pending
  } finally {
    if (bridgeTokenRequest === pending) bridgeTokenRequest = null
  }
}

function isInvalidBridgeToken(error: unknown): boolean {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response
  const detail = response?.data?.detail
  const code = detail && typeof detail === 'object'
    ? String((detail as { code?: unknown }).code || '').trim().toLowerCase()
    : ''
  const legacyDetail = typeof detail === 'string' ? detail.trim().toLowerCase() : ''
  return (
    response?.status === 403 &&
    (
      code === 'invalid_bridge_token' ||
      legacyDetail === 'invalid bridge token' ||
      legacyDetail === '无效的 bridge token'
    )
  )
}

async function executeRequest<T extends KnowledgeEnvelope>(
  path: string,
  options: { method?: 'GET' | 'POST'; params?: any; data?: any },
  value: string,
): Promise<T> {
  const response = await axios.request<T>({
    url: `/market/knowledge/${path}`,
    method: options.method || 'GET',
    params: { ...(options.params || {}), token: value },
    data: options.data,
    timeout: 15000,
  })
  return response.data
}

async function request<T extends KnowledgeEnvelope>(
  path: string,
  options: { method?: 'GET' | 'POST'; params?: any; data?: any } = {},
): Promise<T> {
  let data: T
  try {
    data = await executeRequest<T>(path, options, await token())
  } catch (error) {
    if (!isInvalidBridgeToken(error)) throw error
    data = await executeRequest<T>(path, options, await token(true))
  }
  if (data?.ok === false) {
    throw new KnowledgeApiError(
      String(data.reason || 'operation_failed'),
      data.error_type ? String(data.error_type) : undefined,
    )
  }
  return data
}

export interface KnowledgeStatus {
  name: string
  entries?: number
  integrity_ok: boolean
  status: 'ready' | 'degraded'
  disabled_entries?: number
  packs?: number
  knowledge_packs?: number
  corpus_packs?: number
  knowledge_entries?: number
  corpus_entries?: number
  sources?: Array<{ tag: string; entries: number }>
  error_type?: string
}

export interface KnowledgeEntrySummary {
  title: string
  terms: Record<string, string[]>
  tags: string[]
  summary: string
  content_preview?: string
  content?: string
  disabled: boolean
  score?: number
  source: { tag: string; name: string; homepage: string; license: string }
}

export interface KnowledgePackSummary {
  pack_id: string
  effective_material_type?: 'knowledge' | 'corpus'
  entries?: number
  auto_context?: boolean
  subscription?: { provider: string; version: string }
  index_origin?: string
  index_trust?: string
  index_validation?: string
  index_fallback_reason?: string
  local_embedding_enabled?: boolean
}

export interface KnowledgePackIndexPolicy {
  pack_id: string
  local_embedding_enabled: boolean
}

export const knowledgeApi = {
  status: () => request<{ ok: boolean; status: KnowledgeStatus }>('status'),
  entries: (params: any) => request<any>('entries', { params }),
  entry: (params: any) => request<any>('entry', { params }),
  setEntryDisabled: (data: any) => request<any>('entry/disabled', { method: 'POST', data }),
  packs: () => request<{ ok: boolean; packs: KnowledgePackSummary[] }>('packs'),
  importPack: (pack: any) => request<any>('packs/import', { method: 'POST', data: { pack } }),
  setPackAutoContext: (data: any) => request<any>('packs/auto-context', { method: 'POST', data }),
  setPackMaterialType: (data: any) => request<any>('packs/material-type', { method: 'POST', data }),
  setPackIndexPolicy: (data: KnowledgePackIndexPolicy) => request<KnowledgeEnvelope>('packs/index-policy', { method: 'POST', data }),
  removePack: (data: any) => request<any>('packs/remove', { method: 'POST', data }),
  diagnostics: () => request<any>('diagnostics/recent'),
}
