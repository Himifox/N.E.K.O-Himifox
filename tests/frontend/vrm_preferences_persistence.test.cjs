const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
    const root = path.resolve(__dirname, '../..');
    const THREE = await import('data:text/javascript;base64,' + fs.readFileSync(path.join(root, 'static/libs/three.core.js')).toString('base64'));
    const window = { THREE, screen: { width: 1920, height: 1080 }, addEventListener() {}, removeEventListener() {} };
    const warnings = [];
    const context = vm.createContext({ window, AbortController, setTimeout, clearTimeout,
        localStorage: { getItem: () => 'high' }, console: { ...console,
            warn: (...args) => warnings.push(args), error: (...args) => warnings.push(args) } });
    for (const file of ['vrm-orientation.js', 'vrm-core.js', 'vrm-interaction.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, 'static/vrm', file), 'utf8'), context);
    }
    const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
    const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
    function fixture() {
        const requests = []; const persisted = new Map();
        window.electronScreen = null; window.isViewerMode = false;
        window.screen = { width: 1920, height: 1080 };
        context.fetch = (url, options) => {
            assert.equal(url, '/api/config/preferences');
            const preferences = JSON.parse(options.body);
            return new Promise((resolve, reject) => {
                requests.push({ preferences, fail: () => reject(new Error('fixture network failure')),
                    complete(success = true) {
                        if (success) persisted.set(preferences.model_path, preferences);
                        resolve({ ok: true, json: async () => ({ success }) });
                    } });
            });
        };
        const scene = new THREE.Object3D(); const camera = new THREE.PerspectiveCamera(); camera.position.z = 5;
        const manager = { currentModel: { url: '/model-a.vrm', scene }, camera };
        manager.core = new window.VRMCore(manager);
        const interaction = new window.VRMInteraction(manager);
        return { scene, manager, interaction, requests, persisted };
    }

    // The departure request is already in flight when the model is locked.
    // Hold its server response and prove the lock request cannot overtake it.
    {
        const f = fixture(); f.scene.position.y = 1;
        const first = f.interaction._savePositionAfterInteraction(); await flush();
        assert.equal(f.requests.length, 1);
        f.scene.position.y = 2; f.interaction.setLocked(true); await flush();
        assert.equal(f.requests.length, 1, 'do not send the locked pose while an older write is in flight');
        f.requests[0].complete(); await first; await flush();
        assert.equal(f.requests.length, 2);
        f.requests[1].complete(); await flush();
        assert.equal(f.persisted.get('/model-a.vrm').position.y, 2);
    }

    // Completed interaction A survives cleanup/switch, including its camera and
    // viewport snapshot. B's faster display lookup must not change write order.
    {
        const f = fixture(); const lookups = [];
        window.electronScreen = { getCurrentDisplay() { const d = deferred(); lookups.push(d); return d.promise; } };
        f.scene.position.set(1, 2, 3); f.manager.camera.position.z = 7;
        const first = f.interaction._savePositionAfterInteraction();
        f.interaction.cleanupDragAndZoom();
        const sceneB = new THREE.Object3D(); sceneB.position.y = 9;
        f.manager.currentModel = { url: '/model-b.vrm', scene: sceneB };
        f.manager.camera.position.z = 11; window.screen.width = 1280;
        const second = f.interaction._savePositionAfterInteraction();
        assert.equal(lookups.length, 2);
        lookups[1].resolve({ screenX: 1920, screenY: 0 }); await flush();
        assert.equal(f.requests.length, 0);
        lookups[0].resolve({ bounds: { x: 0, y: 0 } }); await flush();
        assert.equal(f.requests.length, 1);
        assert.equal(f.requests[0].preferences.model_path, '/model-a.vrm');
        assert.deepEqual(f.requests[0].preferences.position, { x: 1, y: 2, z: 3 });
        assert.equal(f.requests[0].preferences.camera_position.z, 7);
        assert.equal(f.requests[0].preferences.viewport.width, 1920);
        f.requests[0].complete(); await first; await flush();
        assert.equal(f.requests.length, 2); assert.equal(f.requests[1].preferences.model_path, '/model-b.vrm');
        f.requests[1].complete(); await second;
        assert.equal(f.persisted.get('/model-a.vrm').position.y, 2);
        assert.equal(f.persisted.get('/model-b.vrm').position.y, 9);
    }

    // Network failure must not poison the queue or discard the subsequent pose.
    {
        const f = fixture();
        const first = f.interaction._savePositionAfterInteraction(); await flush();
        f.scene.position.y = 4; const second = f.interaction._savePositionAfterInteraction();
        f.requests[0].fail(); await first; await flush();
        assert.equal(f.requests.length, 2);
        f.requests[1].complete(); await second;
        assert.equal(f.persisted.get('/model-a.vrm').position.y, 4);
    }

    // A missing or failed display response falls back to saving the pose and
    // does not keep every later request waiting indefinitely.
    for (const displayFailure of ['timeout', 'rejected']) {
        const f = fixture(); const timers = new Map(); let timerId = 0;
        context.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; };
        context.clearTimeout = id => timers.delete(id);
        window.electronScreen = { getCurrentDisplay: () => displayFailure === 'timeout'
            ? new Promise(() => {}) : Promise.reject(new Error('display fixture failure')) };
        f.scene.position.y = 5;
        const first = f.interaction._savePositionAfterInteraction(); await flush();
        if (displayFailure === 'timeout') {
            assert.equal(f.requests.length, 0);
            const displayTimer = [...timers.values()].find(timer => timer.delay === 1000);
            assert.ok(displayTimer); displayTimer.callback(); await flush();
        }
        assert.equal(f.requests.length, 1);
        assert.equal(f.requests[0].preferences.display, undefined);
        window.electronScreen = null; f.scene.position.y = 6;
        const second = f.interaction._savePositionAfterInteraction();
        f.requests[0].complete(); await first; await flush();
        assert.equal(f.requests.length, 2);
        f.requests[1].complete(); await second;
        assert.equal(f.persisted.get('/model-a.vrm').position.y, 6);
        assert.equal(timers.size, 0);
        context.setTimeout = setTimeout; context.clearTimeout = clearTimeout;
    }

    // Shared core callers (initial orientation/reset) obey the same ordering.
    // Queued arguments are immutable even if callers later mutate their inputs.
    {
        const f = fixture(); const core = f.manager.core;
        const first = core.saveUserPreferences('/model-a.vrm', { x: 0, y: 1, z: 0 }, { x: 1, y: 1, z: 1 });
        await flush();
        const position = { x: 0, y: 8, z: 0 }; const rotation = { x: 0, y: 0.6, z: 0 };
        const second = core.saveUserPreferences('/model-a.vrm', position, { x: 1, y: 1, z: 1 }, rotation);
        position.y = 100; rotation.y = 2;
        assert.equal(f.requests.length, 1);
        f.requests[0].complete(false); assert.equal(await first, false); await flush();
        assert.equal(f.requests[1].preferences.position.y, 8);
        assert.equal(f.requests[1].preferences.rotation.y, 0.6);
        f.requests[1].complete(); assert.equal(await second, true);
        window.isViewerMode = true;
        assert.equal(await core.saveUserPreferences('/model-a.vrm', position, { x: 1, y: 1, z: 1 }), false);
        window.isViewerMode = false;
        assert.equal(await core.saveUserPreferences('/model-a.vrm', { x: NaN, y: 0, z: 0 }, { x: 1, y: 1, z: 1 }), false);
        assert.equal(f.requests.length, 2);
    }
    // Viewer mode entered while a save waits in the queue remains read-only.
    {
        const f = fixture();
        const first = f.interaction._savePositionAfterInteraction(); await flush();
        f.scene.position.y = 3; const second = f.interaction._savePositionAfterInteraction();
        window.isViewerMode = true;
        f.requests[0].complete(); await first;
        assert.equal(await second, false);
        assert.equal(f.requests.length, 1);
        window.isViewerMode = false;
    }
    assert.ok(warnings.length, 'failed saves retain their diagnostics');
    console.log('VRM preferences persistence: OK (in-flight ordering, model switching, immutable snapshots and failure recovery)');
})().catch(error => { console.error(error); process.exitCode = 1; });
