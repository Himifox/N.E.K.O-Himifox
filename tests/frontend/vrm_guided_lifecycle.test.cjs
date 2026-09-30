const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
    const root = path.resolve(__dirname, '../..');
    const THREE = await import('data:text/javascript;base64,' + fs.readFileSync(path.join(root, 'static/libs/three.core.js')).toString('base64'));
    const frames = new Map();
    let frameId = 0;
    const events = { addEventListener() {}, removeEventListener() {} };
    const window = { THREE, ...events, NekoModelTouchGestures: { installThree: () => ({ dispose() {} }) } };
    const context = vm.createContext({ window, console, performance: { now: () => 0 },
        document: { ...events, body: { classList: { contains: () => false } } },
        requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
        cancelAnimationFrame(id) { frames.delete(id); }, clearTimeout, setTimeout });
    for (const file of ['vrm-orientation.js', 'vrm-interaction.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, 'static/vrm', file), 'utf8'), context);
    }
    const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
    const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
    const finishTurn = () => {
        const pending = [...frames.entries()];
        assert.ok(pending.length, 'arrival must schedule a turn');
        for (const [id, callback] of pending) { frames.delete(id); callback(360); }
    };
    function fixture() {
        assert.equal(frames.size, 0, 'previous case must not leave animation frames');
        const leases = new Map();
        const saves = [];
        let rests = 0;
        window.NekoMotion = {
            async holdExternalPlayback(owner, { token }) { leases.set(owner, token); },
            async releaseExternalPlayback(owner, { token }) {
                if (leases.get(owner) !== token) return false;
                leases.delete(owner); return true;
            },
            async rest() { rests++; }
        };
        const scene = new THREE.Object3D();
        const camera = new THREE.PerspectiveCamera(); camera.position.z = 5;
        const manager = { camera, currentModel: { scene, vrm: {} }, core: { vrmVersion: '1.0' },
            _isModelReadyForInteraction: true, isLocked: false,
            renderer: { domElement: { ...events, style: {} } },
            playVRMAAnimation: async () => true, stopVRMAAnimation() {} };
        const interaction = new window.VRMInteraction(manager);
        interaction._savePositionAfterInteraction = async () => { saves.push(scene.quaternion.clone()); };
        interaction._hitTestModel = () => true;
        interaction.initDragAndZoom();
        const select = target => {
            interaction._screenPointToMovementTarget = () => target;
            assert.equal(interaction._selectMovementTarget(0, 0), true);
        };
        const mouseDown = button => interaction.mouseDownHandler({ button, clientX: 0, clientY: 0,
            preventDefault() {}, stopPropagation() {} });
        return { interaction, manager, scene, leases, saves, select, mouseDown, rests: () => rests };
    }

    // Clicking the current position before the walk loads must release the lease,
    // even though no movement action has been assigned yet.
    for (const result of [true, false]) {
        const f = fixture();
        const loading = deferred(); let options;
        f.manager.playVRMAAnimation = (_path, settings) => { options = settings; return loading.promise; };
        f.select(new THREE.Vector3(0, 1, 0)); await flush();
        assert.equal(f.leases.size, 1);
        assert.equal(f.interaction._movementAction, null);
        f.select(f.scene.position.clone()); await flush();
        assert.equal(f.leases.size, 0);
        assert.equal(f.interaction.isMoving, false);
        assert.equal(options.shouldApply(), false, 'cancelled loading must not apply its clip');
        loading.resolve(result); await flush();
        assert.equal(f.interaction._movementAction, null);
        f.interaction.cleanupDragAndZoom();
    }

    // A failed clip leaves pure translation active; cleanup must release it.
    {
        const f = fixture(); f.manager.playVRMAAnimation = async () => false;
        f.select(new THREE.Vector3(0, 1, 0)); await flush();
        assert.equal(f.interaction.isMoving, true);
        assert.equal(f.interaction._movementAction, null);
        f.interaction.cleanupDragAndZoom(); await flush();
        assert.equal(f.leases.size, 0);
    }

    // Cleanup also covers ownership alone, and must survive a stop failure.
    {
        const f = fixture();
        f.interaction._movementOwnerToken = 'held-without-action';
        f.leases.set(f.interaction._movementRestOwner, 'held-without-action');
        const warnings = [];
        context.console = { ...console, warn: (...args) => warnings.push(args) };
        f.manager.stopVRMAAnimation = () => { throw new Error('stop fixture failure'); };
        f.interaction.cleanupDragAndZoom(); await flush();
        assert.equal(f.leases.size, 0);
        assert.equal(warnings.length, 1);
        context.console = console;
    }

    // A stale hold completion may release only its own token, never a new trip.
    {
        const f = fixture(); const held = deferred(); let holds = 0;
        window.NekoMotion.holdExternalPlayback = async (owner, { token }) => {
            f.leases.set(owner, token); if (++holds === 1) await held.promise;
        };
        f.select(new THREE.Vector3(0, 1, 0));
        f.select(new THREE.Vector3(0, -1, 0)); await flush();
        const newToken = f.interaction._movementOwnerToken;
        held.resolve(); await flush();
        assert.equal(f.leases.get(f.interaction._movementRestOwner), newToken);
        assert.equal(f.interaction._movementAction, 'walk');
        f.interaction.cleanupDragAndZoom(); await flush();
        assert.equal(f.leases.size, 0);
    }

    // Save the final arrival heading, after the actual turn has finished.
    {
        const f = fixture();
        f.select(new THREE.Vector3(0, 1, 0)); await flush(); f.saves.length = 0;
        f.scene.rotation.y = Math.PI / 2;
        f.interaction.moveTarget.copy(f.scene.position);
        f.interaction._updateGuidedMovement(1 / 60); await flush();
        assert.equal(f.saves.length, 0);
        finishTurn(); await flush();
        assert.equal(f.saves.length, 1);
        assert.ok(f.saves[0].angleTo(new THREE.Quaternion()) < 1e-8);
        f.interaction.cleanupDragAndZoom();
    }

    // Both manual controls cancel active motion and pending arrival turns.
    for (const button of [0, 1, 2]) {
        for (const arriving of [false, true]) {
            const f = fixture();
            f.select(new THREE.Vector3(0, 1, 0)); await flush(); f.saves.length = 0;
            if (arriving) { f.scene.rotation.y = Math.PI / 2; void f.interaction._finishMovement(); await flush(); }
            f.mouseDown(button); await flush();
            assert.equal(f.interaction.isMoving, false);
            assert.equal(f.leases.size, 0);
            assert.equal(frames.size, 0);
            assert.equal(f.interaction.dragMode, button === 2 ? 'orbit' : 'pan');
            assert.equal(f.saves.length, 0, 'cancelled arrival must not save a stale heading');
            f.interaction.cleanupDragAndZoom();
        }
    }

    // A missed pan hit preserves movement, while locking cancels it.
    {
        const f = fixture(); f.select(new THREE.Vector3(0, 1, 0)); await flush();
        f.interaction._hitTestModel = () => false; f.mouseDown(0);
        assert.equal(f.interaction.isMoving, true);
        f.interaction.setLocked(true); await flush();
        assert.equal(f.leases.size, 0); assert.equal(f.interaction.isMoving, false);
        f.interaction.cleanupDragAndZoom();
    }

    // Even after the final frame, a pending release cannot restore/save over a
    // newer manual interaction, target selection, or disposal.
    for (const takeover of ['orbit', 'cleanup', 'same-position']) {
        const f = fixture(); const releasing = deferred();
        f.select(new THREE.Vector3(0, 1, 0)); await flush(); f.saves.length = 0;
        const release = window.NekoMotion.releaseExternalPlayback;
        window.NekoMotion.releaseExternalPlayback = async (...args) => { await release(...args); await releasing.promise; };
        f.scene.rotation.y = Math.PI / 2;
        const finished = f.interaction._finishMovement();
        finishTurn(); await flush();
        if (takeover === 'orbit') f.mouseDown(2);
        else if (takeover === 'cleanup') f.interaction.cleanupDragAndZoom();
        else f.select(f.scene.position.clone());
        const expectedSaves = f.saves.length;
        releasing.resolve(); await finished;
        assert.equal(f.saves.length, expectedSaves);
        assert.equal(f.rests(), 0, 'superseded finish must not restart rest playback');
        f.interaction.cleanupDragAndZoom();
    }
    console.log('VRM guided lifecycle: OK (loading, retargeting, arrival, pan/orbit, locking and cleanup)');
})().catch(error => { console.error(error); process.exitCode = 1; });
