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

  it('requests durable pack job state through the knowledge bridge', async () => {
    axiosMocks.request.mockResolvedValue({ data: { ok: true, jobs: [] } })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.packJobs()).resolves.toEqual({ ok: true, jobs: [] })
    expect(axiosMocks.request.mock.calls[0]![0].url).toBe(
      '/market/knowledge/packs/jobs',
    )
  })

  it('discards a quarantined pack job through the knowledge bridge', async () => {
    axiosMocks.request.mockResolvedValue({ data: { ok: true } })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(
      knowledgeApi.discardPackJob({ job_id: 'degraded-fixture' }),
    ).resolves.toEqual({ ok: true })
    expect(axiosMocks.request.mock.calls[0]![0]).toMatchObject({
      url: '/market/knowledge/packs/jobs/discard',
      method: 'POST',
      data: { job_id: 'degraded-fixture' },
    })
  })

  it('preserves transport failures', async () => {
    const upstream = new Error('bad gateway')
    axiosMocks.request.mockRejectedValue(upstream)
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.status()).rejects.toBe(upstream)
  })

  it('refreshes an invalid bridge token and retries once', async () => {
    axiosMocks.get
      .mockResolvedValueOnce({ data: { bridge_token: 'stale-token' } })
      .mockResolvedValueOnce({ data: { bridge_token: 'fresh-token' } })
    axiosMocks.request
      .mockRejectedValueOnce({
        response: {
          status: 403,
          data: {
            detail: {
              code: 'invalid_bridge_token',
              message: '无效的 bridge token',
            },
          },
        },
      })
      .mockResolvedValueOnce({ data: { ok: true } })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.status()).resolves.toEqual({ ok: true })

    expect(axiosMocks.get).toHaveBeenCalledTimes(2)
    expect(axiosMocks.request).toHaveBeenCalledTimes(2)
    expect(axiosMocks.request.mock.calls[0]![0].params.token).toBe('stale-token')
    expect(axiosMocks.request.mock.calls[1]![0].params.token).toBe('fresh-token')
  })

  it('keeps compatibility with the legacy localized bridge-token error', async () => {
    axiosMocks.get
      .mockResolvedValueOnce({ data: { bridge_token: 'stale-token' } })
      .mockResolvedValueOnce({ data: { bridge_token: 'fresh-token' } })
    axiosMocks.request
      .mockRejectedValueOnce({
        response: { status: 403, data: { detail: '无效的 bridge token' } },
      })
      .mockResolvedValueOnce({ data: { ok: true } })
    const { knowledgeApi } = await loadKnowledgeApi()

    await expect(knowledgeApi.status()).resolves.toEqual({ ok: true })
    expect(axiosMocks.get).toHaveBeenCalledTimes(2)
  })

  it('shares one bridge-token fetch between concurrent requests', async () => {
    let resolveToken!: (value: { data: { bridge_token: string } }) => void
    axiosMocks.get.mockReturnValue(
      new Promise((resolve) => {
        resolveToken = resolve
      }),
    )
    axiosMocks.request.mockResolvedValue({ data: { ok: true } })
    const { knowledgeApi } = await loadKnowledgeApi()

    const first = knowledgeApi.status()
    const second = knowledgeApi.packs()
    resolveToken({ data: { bridge_token: 'shared-token' } })
    await Promise.all([first, second])

    expect(axiosMocks.get).toHaveBeenCalledTimes(1)
    expect(axiosMocks.request).toHaveBeenCalledTimes(2)
  })
})
