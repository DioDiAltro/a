/* =============================================================================
   biglie — client runtime
   =============================================================================
   Small on purpose. The server owns every outcome; this file owns presentation,
   sound and the socket that carries results back. Nothing here may decide
   anything a user could profit from.
   ========================================================================== */

(() => {
  "use strict";

  /* -- theme -------------------------------------------------------------- */

  const root = document.documentElement;

  window.toggleTheme = () => {
    const next = root.dataset.theme === "light" ? "dark" : "light";
    root.dataset.theme = next;
    document.cookie = `theme=${next};path=/;max-age=31536000;samesite=lax`;
  };

  /* -- sound -------------------------------------------------------------- */
  /* Synthesised rather than sampled: a marble click is 40 bytes of maths and
     zero network, and the pitch can track the game state (a rising ladder
     climbs in pitch) which sampled audio can't do without a sprite sheet. */

  let audio = null;
  const audioReady = () => {
    if (!audio) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      audio = new Ctx();
    }
    if (audio.state === "suspended") audio.resume();
    return audio;
  };

  const muted = () => localStorage.getItem("biglie:muted") === "1";

  window.toggleMute = () => {
    localStorage.setItem("biglie:muted", muted() ? "0" : "1");
    return !muted();
  };

  /**
   * @param {number} freq  base frequency in Hz
   * @param {object} opts  {dur, type, gain, sweep}
   */
  const tone = (freq, { dur = 0.08, type = "triangle", gain = 0.14, sweep = 0 } = {}) => {
    if (muted()) return;
    const ctx = audioReady();
    if (!ctx) return;

    const osc = ctx.createOscillator();
    const amp = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime);
    if (sweep) osc.frequency.exponentialRampToValueAtTime(freq * sweep, ctx.currentTime + dur);

    // Short attack, exponential release — the envelope of something small and
    // hard striking something hard. A linear release sounds like a beep.
    amp.gain.setValueAtTime(0.0001, ctx.currentTime);
    amp.gain.exponentialRampToValueAtTime(gain, ctx.currentTime + 0.006);
    amp.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + dur);

    osc.connect(amp).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + dur + 0.02);
  };

  const Sound = {
    tick: () => tone(880 + Math.random() * 120, { dur: 0.035, gain: 0.06 }),
    click: () => tone(1400, { dur: 0.05, type: "square", gain: 0.05 }),
    climb: (step) => tone(300 + step * 55, { dur: 0.1, gain: 0.1 }),
    win: () => {
      [523.25, 659.25, 783.99, 1046.5].forEach((f, i) =>
        setTimeout(() => tone(f, { dur: 0.22, gain: 0.13, type: "sine" }), i * 78)
      );
    },
    loss: () => tone(196, { dur: 0.45, type: "sawtooth", gain: 0.09, sweep: 0.5 }),
  };

  window.BiglieSound = Sound;

  /* -- easing ------------------------------------------------------------- */

  const Ease = {
    // Long, heavy settle. The marble is heavy; it should not stop politely.
    outQuint: (t) => 1 - Math.pow(1 - t, 5),
    outExpo: (t) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t)),
    // Overshoots then settles back — the reel bounces against its detent.
    outBack: (t) => 1 + 2.2 * Math.pow(t - 1, 3) + 1.35 * Math.pow(t - 1, 2),
  };

  window.BiglieEase = Ease;

  /* -- animation loop ----------------------------------------------------- */

  /**
   * Drives `onFrame(progress, elapsed)` for `duration` ms, then `onDone`.
   * Honours prefers-reduced-motion by jumping straight to the final state —
   * the outcome is server-decided, so skipping the animation costs nothing.
   */
  window.animate = (duration, onFrame, onDone) => {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      onFrame(1, duration);
      onDone && onDone();
      return () => {};
    }

    let raf = 0;
    const start = performance.now();
    const step = (now) => {
      const elapsed = now - start;
      const t = Math.min(elapsed / duration, 1);
      onFrame(t, elapsed);
      if (t < 1) raf = requestAnimationFrame(step);
      else onDone && onDone();
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  };

  /* -- canvas ------------------------------------------------------------- */

  /** Sizes a canvas for the device pixel ratio so nothing renders soft. */
  window.fitCanvas = (canvas, cssWidth, cssHeight) => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = cssWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  };

  /* -- sockets ------------------------------------------------------------ */

  /**
   * Opens a reconnecting WebSocket. The server pushes settled results; the
   * client never asks for an outcome it could then discard by disconnecting.
   */
  window.openSocket = (path, onMessage) => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    let socket = null;
    let backoff = 500;
    let closed = false;

    const connect = () => {
      socket = new WebSocket(`${scheme}://${location.host}${path}`);
      socket.onopen = () => { backoff = 500; };
      socket.onmessage = (event) => {
        try { onMessage(JSON.parse(event.data)); }
        catch (err) { console.error("socket payload", err); }
      };
      socket.onclose = () => {
        if (closed) return;
        // A dropped socket mid-round must not look like a lost round: the
        // result is already committed server-side and is re-sent on reconnect.
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 10_000);
      };
    };

    connect();
    return {
      send: (data) => socket?.readyState === 1 && socket.send(JSON.stringify(data)),
      close: () => { closed = true; socket?.close(); },
    };
  };

  /* -- countdowns --------------------------------------------------------- */
  /* Lottery deadlines. Rendered from a server timestamp, never from client
     arithmetic that could drift. */

  const pad = (n) => String(n).padStart(2, "0");

  document.querySelectorAll("[data-countdown]").forEach((el) => {
    const target = new Date(el.dataset.countdown).getTime();
    const tick = () => {
      const left = target - Date.now();
      if (left <= 0) { el.textContent = "scaduta"; return; }
      const d = Math.floor(left / 864e5);
      const h = Math.floor((left % 864e5) / 36e5);
      const m = Math.floor((left % 36e5) / 6e4);
      const s = Math.floor((left % 6e4) / 1000);
      el.textContent = d > 0 ? `${d}g ${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(h)}:${pad(m)}:${pad(s)}`;
      requestAnimationFrame(() => setTimeout(tick, 1000));
    };
    tick();
  });
})();
