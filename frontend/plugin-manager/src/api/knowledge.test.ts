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

  it('rejects a logical API failure with its reason and error type', async () => {
    axiosMocks.request.mockResolvedValue({
      data: {
        ok: false,
        reason: 'invalid_pack',
        error_type: 'ValueError',
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
    expect(failure).toMatchObject({
      reason: 'invalid_pack',
      errorType: 'ValueError',
    })
  })

  it('returns a successful response unchanged', async () => {
    const payload = { ok: true, status: { status: 'ready' } }
    axiosMocks.request.mockResolvedValue({ data: payload })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.status()).resolves.toEqual(payload)
  })

  it('preserves transport failures', async () => {
    const upstream = new Error('bad gateway')
    axiosMocks.request.mockRejectedValue(upstream)
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.status()).rejects.toBe(upstream)
  })
})
