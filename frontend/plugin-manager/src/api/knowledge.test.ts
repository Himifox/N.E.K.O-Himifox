import { beforeEach, describe, expect, it, vi } from 'vitest'

const axiosMocks = vi.hoisted(() => ({
  get: vi.fn(),
  request: vi.fn(),
}))

vi.mock('axios', () => ({ default: axiosMocks }))

async function loadKnowledgeApi() {
  return import('./knowledge')
}

describe('knowledge API response handling', () => {
  beforeEach(() => {
    vi.resetModules()
    axiosMocks.get.mockReset()
    axiosMocks.request.mockReset()
    axiosMocks.get.mockResolvedValue({ data: { bridge_token: 'fixture-token' } })
  })

  it('rejects a logical API failure with stable issues', async () => {
    axiosMocks.request.mockResolvedValue({
      data: {
        ok: false,
        issues: [{ path: 'pack_id', code: 'invalid_identifier', message: 'invalid pack id' }],
      },
    })
    const { KnowledgeApiError, knowledgeApi } = await loadKnowledgeApi()

    let failure: unknown
    try {
      await knowledgeApi.importPack({})
    } catch (error) {
      failure = error
    }

    expect(failure).toBeInstanceOf(KnowledgeApiError)
    expect(failure).toMatchObject({ code: 'invalid_identifier' })
  })

  it('returns a successful response unchanged', async () => {
    const payload = { ok: true, collections: [] }
    axiosMocks.request.mockResolvedValue({ data: payload })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.collections()).resolves.toEqual(payload)
  })
})
