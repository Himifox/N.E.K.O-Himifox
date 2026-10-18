import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { Script, createContext } from 'node:vm'
import ts from 'typescript'

// Run from frontend/plugin-manager: node --test scripts/yui-stardew-panel.test.mjs
const panelPath = new URL('../../../plugin/plugins/yui_stardew/ui/panel.tsx', import.meta.url)
const compiled = ts.transpileModule(readFileSync(panelPath, 'utf8'), {
  fileName: 'panel.tsx',
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.React,
    jsxFactory: '__jsx',
  },
}).outputText

function mountPanel(refresh = async () => undefined, state = {}) {
  const timers = new Map()
  const clearedTimers = []
  const effects = []
  const calls = []
  let nextTimer = 1
  let effectIndex = 0
  const api = {
    refresh: (...args) => {
      calls.push(args)
      return refresh(...args)
    },
    call: () => assert.fail('Automatic polling must only refresh state'),
  }
  const sdk = new Proxy({
    useState: (initial) => [initial, () => undefined],
    useToast: () => ({}),
    useEffect: (effect, dependencies) => {
      const index = effectIndex++
      const previous = effects[index]
      if (previous && dependencies.every((value, i) => Object.is(value, previous.dependencies[i]))) {
        return
      }
      previous?.cleanup?.()
      effects[index] = { dependencies, cleanup: effect() }
    },
  }, {
    get: (target, name) => name in target ? target[name] : String(name),
  })
  const exports = {}
  const context = createContext({
    exports,
    require: (name) => {
      assert.equal(name, '@neko/plugin-ui')
      return sdk
    },
    __jsx: () => undefined,
    window: {
      setInterval: (callback, delay) => {
        const id = nextTimer++
        timers.set(id, { callback, delay })
        return id
      },
      clearInterval: (id) => {
        clearedTimers.push(id)
        timers.delete(id)
      },
    },
  })
  new Script(compiled, { filename: 'yui-stardew-panel.js' }).runInContext(context)
  const render = (nextState) => {
    effectIndex = 0
    exports.default({ state: nextState, api, actions: [] })
  }
  render(state)
  return {
    calls,
    timers,
    clearedTimers,
    render,
    tick: () => {
      for (const { callback } of [...timers.values()]) callback()
    },
    unmount: () => {
      for (const { cleanup } of effects) cleanup?.()
    },
  }
}

const settleRefresh = () => new Promise((resolve) => setImmediate(resolve))

test('empty initial context polls every five seconds without invoking actions', async () => {
  const panel = mountPanel()
  assert.equal(panel.timers.size, 1)
  assert.equal([...panel.timers.values()][0].delay, 5000)
  assert.equal(panel.calls.length, 0)
  panel.tick()
  await settleRefresh()
  panel.tick()
  await settleRefresh()
  assert.equal(panel.calls.length, 2)
  panel.unmount()
})

test('polling continues when the bridge connects and a save becomes ready', async () => {
  const panel = mountPanel()
  for (const worldReady of [false, true]) {
    panel.render({
      discovery: { state: 'ready' },
      connection: { ok: true, reachable: true },
      avatar: { world_ready: worldReady },
    })
    panel.tick()
    await settleRefresh()
    assert.equal(panel.calls.length, worldReady ? 2 : 1)
    assert.equal(panel.timers.size, 1)
  }
  panel.unmount()
})

test('an unfinished refresh cannot overlap with the next polling interval', async () => {
  let complete
  const panel = mountPanel(() => new Promise((resolve) => { complete = resolve }))
  panel.tick()
  panel.tick()
  await settleRefresh()
  panel.tick()
  assert.equal(panel.calls.length, 1)
  complete()
  await settleRefresh()
  panel.tick()
  assert.equal(panel.calls.length, 2)
  complete()
  await settleRefresh()
  panel.unmount()
})

test('a rejected refresh releases the guard so a later interval can recover', async () => {
  let attempts = 0
  const panel = mountPanel(async () => {
    if (++attempts === 1) throw new Error('Plugin temporarily unavailable')
  })
  panel.tick()
  await settleRefresh()
  panel.tick()
  await settleRefresh()
  assert.equal(panel.calls.length, 2)
  panel.unmount()
})

test('unmount clears the polling timer even while a refresh is unfinished', async () => {
  let complete
  const panel = mountPanel(() => new Promise((resolve) => { complete = resolve }))
  const timer = [...panel.timers.keys()][0]
  panel.tick()
  panel.unmount()
  assert.equal(panel.timers.size, 0)
  assert.deepEqual(panel.clearedTimers, [timer])
  complete()
  await settleRefresh()
  panel.tick()
  assert.equal(panel.calls.length, 1)
})
