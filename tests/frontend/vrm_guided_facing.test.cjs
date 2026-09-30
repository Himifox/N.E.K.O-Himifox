const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
const root = path.resolve(__dirname, '../..');
// Use the shipped Three.js implementation, including quaternion/Euler coupling.
const THREE = await import('data:text/javascript;base64,' + fs.readFileSync(path.join(root, 'static/libs/three.core.js')).toString('base64'));
const { Vector3, Quaternion, Object3D } = THREE;
let frameCallback = null;
const context = vm.createContext({ window: { THREE }, console, performance: { now: () => 0 },
    requestAnimationFrame: callback => { frameCallback = callback; return 1; },
    cancelAnimationFrame: () => { frameCallback = null; } });
for (const file of ['vrm-orientation.js', 'vrm-interaction.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'static/vrm', file), 'utf8'), context);
}
const { VRMOrientationDetector: detector, VRMInteraction: Interaction } = context.window;
assert.equal(detector.getMovementFacingProfile({ meta: { metaVersion: '1.0' } }, '0.0').vrmVersion, '0.0');
assert.equal(detector.getMovementFacingProfile({ meta: { metaVersion: 'broken' } }, '1.0').vrmVersion, '1.0');
assert.equal(detector.getMovementFacingProfile({ meta: { metaVersion: '1.0' } }, 'unknown').vrmVersion, '1.0');
assert.equal(detector.getMovementFacingProfile({ meta: { metaVersion: '0.0' } }).vrmVersion, '0.0');

for (const version of ['0.0', '1.0']) {
    const authoredFront = version === '1.0' ? 1 : -1;
    for (const cameraYaw of [0, 0.7, -1.2]) {
        const camera = {
            quaternion: new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), cameraYaw),
            position: new Vector3(5 * Math.sin(cameraYaw), 0, 5 * Math.cos(cameraYaw)),
            getWorldDirection(v) { Object.assign(v, new Vector3(0, 0, -1).applyQuaternion(this.quaternion)); }
        };
        const right = new Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
        const forward = new Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
        for (const [x, y] of [[0, 1], [0, -1], [1, 0], [-1, 0], [1, 1], [-1, 1], [1, -1], [-1, -1]]) {
            const scene = new Object3D();
            const interaction = Object.create(Interaction.prototype);
            Object.assign(interaction, {
                manager: { camera, currentModel: { scene, vrm: {} }, core: { vrmVersion: version } },
                isMoving: true, moveTarget: right.clone().multiplyScalar(x).addScaledVector(new Vector3(0, 1, 0), y),
                movementArrivalThreshold: 0.01, movementVelocity: 0,
                movementMaxSpeed: 0.5, movementAcceleration: 1, movementDeceleration: 1
            });
            interaction._updateGuidedMovement(0.1);
            const actual = new Vector3(0, 0, authoredFront).applyQuaternion(scene.quaternion);
            const expected = right.clone().multiplyScalar(x).addScaledVector(forward, y).normalize();
            assert.ok(actual.dot(expected) > 1 - 1e-8, `${version}: camera ${cameraYaw}, direction ${x},${y}`);

            const restYaw = interaction._getCameraFacingRotationY(scene);
            const restFront = new Vector3(authoredFront * Math.sin(restYaw), 0, authoredFront * Math.cos(restYaw));
            const toCamera = camera.position.clone().sub(scene.position); toCamera.y = 0;
            assert.ok(restFront.dot(toCamera.normalize()) > 1 - 1e-8, `${version}: arrival faces camera`);
        }
    }
}
console.log('VRM guided movement facing: OK');

// Real frame intervals must also work when the previous trip left the model
// facing the opposite direction. A large single delta hides backward steps.
for (const version of ['0.0', '1.0']) {
    const authoredFront = version === '1.0' ? 1 : -1;
    for (const rotation of [[0, 0, 0], [-3.1354805766406257, 0.05088144987006485, -3.128094060958441]]) {
    const scene = new Object3D();
    scene.rotation.set(...rotation);
    const camera = {
        quaternion: new Quaternion(), position: new Vector3(0, 0, 5),
        getWorldDirection(v) { Object.assign(v, new Vector3(0, 0, -1)); }
    };
    const interaction = Object.create(Interaction.prototype);
    Object.assign(interaction, {
        manager: { camera, currentModel: { scene, vrm: {} }, core: { vrmVersion: version } },
        isMoving: true, movementArrivalThreshold: 0.01, movementVelocity: 0,
        movementMaxSpeed: 0.5, movementAcceleration: 1, movementDeceleration: 1,
        enableFaceCamera: true, _smoothFacingFrame: null
    });
    for (const y of [1, -1, 1, -1, 1, -1]) {
        interaction.moveTarget = scene.position.clone().addScaledVector(new Vector3(0, 1, 0), y * 10);
        let moved = false;
        for (let frame = 0; frame < 90; frame++) {
            const previous = scene.position.clone();
            interaction.update(1 / 60);
            if (scene.position.clone().sub(previous).lengthSq() > 1e-12) {
                moved = true;
                const frontZ = new Vector3(0, 0, authoredFront).applyQuaternion(scene.quaternion).z;
                assert.ok(frontZ * -y > 0.9, `${version}: trip ${y}, frame ${frame} moves while facing backward`);
            }
        }
        assert.ok(moved, 'turning must eventually allow movement');
        // Arrival and the next trip must retain the same physical heading,
        // even when Three.js rewrites the equivalent XYZ Euler representation.
        const upBefore = new Vector3(0, 1, 0).applyQuaternion(scene.quaternion).y;
        interaction._smoothTurnToCamera(scene);
        assert.ok(frameCallback);
        frameCallback(360);
        assert.equal(interaction._smoothFacingFrame, null);
        const front = new Vector3(0, 0, authoredFront).applyQuaternion(scene.quaternion);
        const toCamera = camera.position.clone().sub(scene.position); toCamera.y = 0;
        front.y = 0;
        assert.ok(front.normalize().dot(toCamera.normalize()) > 1 - 1e-8);
        assert.ok(Math.abs(new Vector3(0, 1, 0).applyQuaternion(scene.quaternion).y - upBefore) < 1e-8, 'turning preserves tilt');
    }
    }
}
console.log('VRM repeated movement at 60 FPS: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
