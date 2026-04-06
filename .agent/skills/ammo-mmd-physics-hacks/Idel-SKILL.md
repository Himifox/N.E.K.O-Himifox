# MMD 物理引擎跳舞模式（防穿模）状态机设计文档

## 1. 背景与痛点

在 MMD (MikuMikuDance) 的 WebGL 物理渲染中，当模型执行高强度、高速度的跳舞动作时，运动学刚体（Kinematic Bodies，由骨骼直接驱动的刚体，如手臂、腿部）在两帧之间的位移跨度极大。
底层物理引擎（Ammo.js/Bullet）默认将其视为“瞬移”（即无瞬时线速度的坐标覆盖），这会导致引擎的连续碰撞检测（CCD）失效，从而引发手臂穿透裙摆、头发等极其严重的“穿模”或“炸模”现象。

## 2. 核心解决思路：业务层驱动的状态机

放弃在底层物理引擎中盲目推算线速度的复杂方案，转而将控制权上浮至**业务层（点歌台）**。
通过监听点歌台的播放状态，显式地向物理引擎下发状态切换指令，并在切换瞬间强制重置物理状态，实现性能与效果的完美闭环。

### 核心策略分点说明：
* **按需开启 CCD（防穿模主力）：** 仅在“跳舞”状态下，为运动学刚体开启 CCD 连续碰撞扫描，并强制刚体不休眠。
* **日常模式降级（释放性能）：** 在音乐暂停或停止时，关闭 CCD 参数，并允许刚体自动休眠，大幅节省 CPU 算力。
* **切换防抖与强制同步（终极保险）：** 在状态发生改变的瞬间，调用 `this.reset()` 强制将所有物理刚体同步到当前骨骼位置，抹平状态切换可能带来的结构拉扯。

---

## 3. 详细代码实现

### 3.1 物理引擎底层状态机 (`MMDPhysics` 类)

在物理引擎内部维护 `_isDancing` 状态，并实现双向切换与同步逻辑：

```javascript
  setDancing(enable) {
    // 1. 防抖机制：避免点歌台重复派发相同状态引发不必要的物理重置
    if (this._isDancing === enable) return this;
    
    this._isDancing = enable;
    for (let i = 0, il = this.bodies.length; i < il; i++) {
      const body = this.bodies[i];
      
      // 仅针对运动学刚体（物理模式 0）进行控制
      if (body.params.physicsMode === 0) {
        if (enable) {
          // 【跳舞状态】：强制激活，开启 CCD 并动态计算扫描半径
          body.body.setActivationState(4); // DISABLE_DEACTIVATION
          body.body.setCcdMotionThreshold(0.1);
          const minDim = Math.min(body.params.shapeSize[0], body.params.shapeSize[1], body.params.shapeSize[2]);
          body.body.setCcdSweptSphereRadius(minDim * 0.3);
        } else {
          // 【日常状态】：允许休眠，关闭 CCD 释放性能
          body.body.setActivationState(1); // ACTIVE_TAG
          body.body.setCcdMotionThreshold(0);
          body.body.setCcdSweptSphereRadius(0);
        }
      }
    }
    
    // 2. 核心兜底：状态切换后立刻强制同步一次所有物理刚体，确保网格瞬间落位
    this.reset();
    
    return this;
  }