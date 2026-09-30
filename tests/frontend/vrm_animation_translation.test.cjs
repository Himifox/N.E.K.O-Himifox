const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

global.window = global;
global.THREE = {};
vm.runInThisContext(
    fs.readFileSync(path.resolve(__dirname, '../../static/vrm/vrm-animation.js'), 'utf8'),
    { filename: 'static/vrm/vrm-animation.js' }
);

async function playFixture(vrmVersion, options) {
    const rotationNames = ['Normalized_Hips', 'Normalized_Spine', 'Normalized_Head'];
    const hipsPosition = { name: 'Normalized_Hips.position', values: [0, 1, 0, 0, 0.5, 0] };
    const rootPositions = [hipsPosition, { name: 'Reference.position' }, { name: 'Root.position' }];
    const handPosition = { name: 'Normalized_LeftHand.position' };
    const rotations = rotationNames.map(name => ({ name: `${name}.quaternion` }));
    const tracks = [...rootPositions, handPosition, ...rotations];
    const clip = { tracks: [...tracks] };
    const boneNames = new Set(tracks.map(track => track.name.split('.')[0]));
    const scene = {
        uuid: `translation-${vrmVersion}`,
        traverse() {},
        getObjectByName(name) { return boneNames.has(name) ? { name } : null; }
    };
    const vrm = { scene, humanoid: { autoUpdateHumanBones: true } };
    const animation = new global.VRMAnimation({ currentModel: { vrm }, core: { vrmVersion } });
    animation._initLoader = async () => ({
        loadAsync: async () => ({ userData: { vrmAnimations: [{}] } })
    });
    global.VRMAnimation._animationModuleCache = { createVRMAnimationClip: () => clip };
    let configuredClip;
    let playedAction;
    const action = {};
    animation._createAndConfigureAction = (nextClip, mixerRoot) => {
        assert.equal(mixerRoot, scene);
        configuredClip = nextClip;
        return action;
    };
    animation._playAction = nextAction => { playedAction = nextAction; };

    assert.equal(await animation.playVRMAAnimation('/fixture.vrma', options), true);
    assert.equal(playedAction, action);
    return { configuredClip, tracks, rootPositions, handPosition, rotations, hipsPosition };
}

(async () => {
    for (const vrmVersion of ['0.0', '1.0']) {
        // Ordinary sitting/rest playback must preserve authored hip height.
        for (const options of [undefined, { movement: false }, { isIdle: true }]) {
            const fixture = await playFixture(vrmVersion, options);
            assert.deepEqual(fixture.configuredClip.tracks, fixture.tracks);
            assert.deepEqual(fixture.hipsPosition.values, [0, 1, 0, 0, 0.5, 0]);
        }
        const fixture = await playFixture(vrmVersion, { movement: true });
        assert.deepEqual(fixture.configuredClip.tracks, [fixture.handPosition, ...fixture.rotations]);
        assert.equal(fixture.rootPositions.some(track => fixture.configuredClip.tracks.includes(track)), false);
    }
    console.log('VRM animation translation: OK (normal poses and movement in VRM 0/1)');
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
