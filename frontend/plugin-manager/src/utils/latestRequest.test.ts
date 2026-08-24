import { describe, expect, it } from 'vitest'

import { createLatestRequestGate } from './latestRequest'

describe('latest request gate', () => {
  it('rejects an older response after a newer request starts', () => {
    const gate = createLatestRequestGate()
    const older = gate.begin()
    const newer = gate.begin()

    expect(gate.isLatest(older)).toBe(false)
    expect(gate.isLatest(newer)).toBe(true)
  })
})
