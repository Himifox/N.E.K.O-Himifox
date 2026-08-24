export interface LatestRequestGate {
  begin: () => number
  isLatest: (requestId: number) => boolean
}

export function createLatestRequestGate(): LatestRequestGate {
  let latestRequestId = 0
  return {
    begin: () => ++latestRequestId,
    isLatest: (requestId) => requestId === latestRequestId,
  }
}
