import axios from 'axios'

let bridgeToken = ''

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

async function token(): Promise<string> {
  if (bridgeToken) return bridgeToken
  const response = await axios.get('/market/bridge-token', { timeout: 3000 })
  bridgeToken = String(response.data?.bridge_token || '')
  if (!bridgeToken) throw new Error('knowledge bridge token unavailable')
  return bridgeToken
}

async function request<T extends KnowledgeEnvelope>(
  path: string,
  options: { method?: 'GET' | 'POST'; params?: any; data?: any } = {},
): Promise<T> {
  const value = await token()
  const response = await axios.request<T>({
    url: `/market/knowledge/${path}`,
    method: options.method || 'GET',
    params: { ...(options.params || {}), token: value },
    data: options.data,
    timeout: 15000,
  })
  const data = response.data
  if (data?.ok === false) {
    throw new KnowledgeApiError(
      String(data.reason || 'operation_failed'),
      data.error_type ? String(data.error_type) : undefined,
    )
  }
  return data
}

export interface KnowledgeCollection {
  collection_id: string
  name: string
  entries?: number
  integrity_ok: boolean
  status: 'ready' | 'degraded'
  auto_context: boolean
  disabled_entries?: number
  packs?: number
  sources?: Array<{ tag: string; entries: number }>
  error_type?: string
}

export interface KnowledgeEntrySummary {
  collection_id: string
  title: string
  terms: Record<string, string[]>
  tags: string[]
  summary: string
  content?: string
  disabled: boolean
  score?: number
  source: { tag: string; name: string; homepage: string; license: string }
}

export const knowledgeApi = {
  collections: () => request<{ ok: boolean; collections: KnowledgeCollection[] }>('collections'),
  entries: (params: any) => request<any>('entries', { params }),
  entry: (params: any) => request<any>('entry', { params }),
  setEntryDisabled: (data: any) => request<any>('entry/disabled', { method: 'POST', data }),
  setCollectionAutoContext: (data: any) => request<any>('collection/auto-context', { method: 'POST', data }),
  packs: (collection: string) => request<any>('packs', { params: { collection } }),
  importPack: (pack: any) => request<any>('packs/import', { method: 'POST', data: { pack } }),
  setPackAutoContext: (data: any) => request<any>('packs/auto-context', { method: 'POST', data }),
  removePack: (data: any) => request<any>('packs/remove', { method: 'POST', data }),
  diagnostics: () => request<any>('diagnostics/recent'),
}
