const RENDERER_VERSION = "0.0.9-alpha.2";
const RENDERER_MODULE =
  `https://cdn.jsdelivr.net/npm/gaussian-splat-renderer-for-lam@${RENDERER_VERSION}`
  + "/build/gaussian-splat-renderer-for-lam.module.js";

/**
 * A small jitter buffer for the versioned ARKit motion packets published by
 * the worker.  The worker timestamps motion in the canonical audio sample
 * clock; the renderer samples that clock through the hidden LiveKit media
 * element supplied by the application.
 */
class ExpressionBuffer {
  constructor() {
    this.frames = [];
    this.lastSample = -1;
  }

  push(packet) {
    if (!packet || packet.schema !== "arkit52-v1" || !Array.isArray(packet.values)) return;
    const sample = Number(packet.pts_sample);
    const values = packet.values.map(Number);
    if (!Number.isFinite(sample) || values.length !== 52) return;
    this.frames.push({ sample, duration: Number(packet.duration_samples) || 1600, values });
    this.frames.sort((left, right) => left.sample - right.sample);
    // Keep a bounded jitter buffer. A late packet is harmless and should not
    // make the browser retain an entire long-running session.
    if (this.frames.length > 180) this.frames.splice(0, this.frames.length - 180);
  }

  sample(audioSample) {
    if (!this.frames.length || !Number.isFinite(audioSample)) return undefined;
    this.lastSample = audioSample;
    while (this.frames.length > 2 && this.frames[1].sample + this.frames[1].duration <= audioSample) {
      this.frames.shift();
    }
    const current = this.frames[0];
    const next = this.frames[1];
    if (!current) return undefined;
    if (!next || audioSample < current.sample) return current.values;
    const span = Math.max(1, next.sample - current.sample);
    const ratio = Math.max(0, Math.min(1, (audioSample - current.sample) / span));
    return current.values.map((value, index) => value + (next.values[index] - value) * ratio);
  }

  clear() {
    this.frames = [];
    this.lastSample = -1;
  }
}

/** Client adapter for the pinned LAM WebRender package. */
export class LamWebRendererAdapter {
  constructor(container) {
    this.container = container;
    this.renderer = undefined;
    this.expression = undefined;
    this.motion = new ExpressionBuffer();
    this.audioElement = undefined;
    this.startedAt = 0;
    this.state = "Idle";
  }

  async start({ assetUrl = "/assets/lam/default.zip", expressionUrl = "/assets/lam/test_expression_1s.json" } = {}) {
    if (this.renderer) return;
    const [{ GaussianSplatRenderer }, expression] = await Promise.all([
      import(RENDERER_MODULE),
      fetch(expressionUrl).then((response) => {
        if (!response.ok) throw new Error("Không tải được LAM expression asset");
        return response.json();
      }),
    ]);
    this.expression = expression;
    this.startedAt = performance.now();
    this.renderer = await GaussianSplatRenderer.getInstance(
      this.container,
      assetUrl,
      {
        getChatState: () => this.state,
        getExpressionData: () => this.getExpressionData(),
        // Match the upstream WebRender defaults. A transparent canvas lets
        // the avatar's Gaussian colors remain visible instead of presenting
        // an opaque black surface when the asset is still initializing.
        backgroundColor: "0xff0000",
        alpha: 0.2,
      },
    );
    // A successful adapter must have a render surface. Without this guard a
    // failed asset load is mistaken for a working renderer and the UI hides
    // the fallback video, resulting in a black panel.
    for (let attempt = 0; attempt < 20 && !this.container.querySelector("canvas"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!this.container.querySelector("canvas")) {
      throw new Error("LAM WebRender did not create a canvas");
    }
  }

  setAudioElement(element) {
    this.audioElement = element;
  }

  pushMotion(packet) {
    this.motion.push(packet);
  }

  setState(state) {
    this.state = ({ idle: "Idle", thinking: "Thinking", speaking: "Responding" })[state] || "Idle";
  }

  getExpressionData() {
    const frames = this.expression?.frames || [];
    const names = this.expression?.names || [];
    if (!frames.length) return {};
    const audioSample = this.audioElement && Number.isFinite(this.audioElement.currentTime)
      ? this.audioElement.currentTime * 48_000
      : ((performance.now() - this.startedAt) / 1000) * 48_000;
    const liveWeights = this.motion.sample(audioSample);
    const frameIndex = Math.floor(((performance.now() - this.startedAt) / 1000) * 30) % frames.length;
    const weights = liveWeights || frames[frameIndex]?.weights || [];
    return Object.fromEntries(names.map((name, index) => [name, weights[index] || 0]));
  }

  async close() {
    const renderer = this.renderer;
    this.renderer = undefined;
    this.expression = undefined;
    this.motion.clear();
    this.audioElement = undefined;
    if (renderer?.dispose) await renderer.dispose();
    this.container.replaceChildren();
  }
}

export function isLamRendererRequested() {
  // LAM WebGL is the default presentation for newly opened sessions. Use
  // ?renderer=video (or ?renderer=recorded) to explicitly select the legacy
  // server-published recorded video track.
  const renderer = new URLSearchParams(window.location.search).get("renderer");
  return renderer !== "video" && renderer !== "recorded";
}
