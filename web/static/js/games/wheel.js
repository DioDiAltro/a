/* =============================================================================
   Ruota della fortuna
   =============================================================================
   The server has already decided which wedge wins and has published the hash of
   the seed that decided it. This module's only job is to make the wheel arrive
   there in a way that feels earned.

   The whole craft of a fortune wheel is in the deceleration curve. A linear
   stop reads as a cheap animation; what sells it is a long quintic ease-out
   with a tiny elastic settle at the end, plus a pointer that physically clicks
   off each pin. Below ~0.6 rad/s the ticks become individually audible and that
   is where the tension lives — so the curve is tuned to spend real time there
   rather than rushing to a stop.
   ========================================================================== */

export class FortuneWheel {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {Array<{label: string, multiplier_bp: number, weight: number}>} wedges
   */
  constructor(canvas, wedges, { size = 340 } = {}) {
    this.canvas = canvas;
    this.wedges = wedges;
    this.size = size;
    this.ctx = window.fitCanvas(canvas, size, size);
    this.rotation = -Math.PI / 2; // pointer sits at 12 o'clock
    this.spinning = false;

    // Wedge arcs are proportional to weight, so the wheel *looks* like its own
    // odds. A wheel with equal-width wedges and unequal weights is a lie told
    // in pictures, whichever way the maths works out.
    const totalWeight = wedges.reduce((sum, w) => sum + w.weight, 0);
    let cursor = 0;
    this.arcs = wedges.map((w) => {
      const span = (w.weight / totalWeight) * Math.PI * 2;
      const arc = { start: cursor, end: cursor + span, mid: cursor + span / 2, ...w };
      cursor += span;
      return arc;
    });

    this.draw();
  }

  colorFor(multiplierBp) {
    const styles = getComputedStyle(document.documentElement);
    if (multiplierBp === 0) return styles.getPropertyValue("--ink-700").trim() || "#1a1e2e";
    if (multiplierBp >= 50000) return styles.getPropertyValue("--gold").trim() || "#ffc24b";
    if (multiplierBp >= 20000) return styles.getPropertyValue("--marble-glow").trim() || "#64e9ff";
    if (multiplierBp >= 10000) return styles.getPropertyValue("--marble-core").trim() || "#3d5afe";
    return styles.getPropertyValue("--ink-600").trim() || "#262b40";
  }

  draw() {
    const { ctx, size } = this;
    const r = size / 2;
    const radius = r - 14;

    ctx.clearRect(0, 0, size, size);
    ctx.save();
    ctx.translate(r, r);
    ctx.rotate(this.rotation);

    for (const arc of this.arcs) {
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, radius, arc.start, arc.end);
      ctx.closePath();
      ctx.fillStyle = this.colorFor(arc.multiplier_bp);
      ctx.fill();
      ctx.strokeStyle = "rgba(0,0,0,0.35)";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Labels ride the radius, flipped on the left half so none read upside
      // down — the detail people don't notice unless it's missing.
      const flipped = arc.mid > Math.PI / 2 && arc.mid < (3 * Math.PI) / 2;
      ctx.save();
      ctx.rotate(arc.mid);
      ctx.translate(radius * 0.66, 0);
      if (flipped) ctx.rotate(Math.PI);
      ctx.fillStyle = arc.multiplier_bp >= 20000 ? "#05060b" : "#e4e8f2";
      ctx.font = "600 13px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(arc.label, 0, 0);
      ctx.restore();
    }

    // Hub: a marble seen from directly above.
    const hub = ctx.createRadialGradient(-4, -5, 1, 0, 0, 26);
    hub.addColorStop(0, "#ffffff");
    hub.addColorStop(0.18, "#64e9ff");
    hub.addColorStop(0.6, "#3d5afe");
    hub.addColorStop(1, "#1a2bb8");
    ctx.beginPath();
    ctx.arc(0, 0, 26, 0, Math.PI * 2);
    ctx.fillStyle = hub;
    ctx.fill();

    ctx.restore();

    // Pointer, drawn unrotated at 12 o'clock.
    ctx.beginPath();
    ctx.moveTo(r, 4);
    ctx.lineTo(r - 11, 26);
    ctx.lineTo(r + 11, 26);
    ctx.closePath();
    ctx.fillStyle = "#ffc24b";
    ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.5)";
    ctx.stroke();
  }

  /**
   * Spin to a wedge the server already chose.
   * @param {number} winningIndex index into the wedge array
   * @param {number} duration ms
   * @returns {Promise<object>} resolves with the winning wedge
   */
  spinTo(winningIndex, { duration = 5200 } = {}) {
    if (this.spinning) return Promise.reject(new Error("already spinning"));
    this.spinning = true;

    const target = this.arcs[winningIndex];

    // Land at a random point *within* the wedge rather than dead centre.
    // Always stopping mid-wedge makes a wheel feel scripted, and occasionally
    // stopping near a boundary is what produces the held breath.
    const inset = (target.end - target.start) * 0.16;
    const landing = target.start + inset + Math.random() * (target.end - target.start - inset * 2);

    const from = this.rotation;
    const turns = 6 + Math.floor(Math.random() * 3);
    const to = -Math.PI / 2 - landing + Math.PI * 2 * turns;

    let lastTickAngle = from;

    return new Promise((resolve) => {
      window.animate(
        duration,
        (t) => {
          const eased = window.BiglieEase.outQuint(t);
          this.rotation = from + (to - from) * eased;

          // One click per pin crossed. Because the easing is quintic, these
          // naturally crowd at the start and space out at the end — the sound
          // of the wheel slowing down comes free from the geometry.
          const pinSpacing = (Math.PI * 2) / Math.max(this.arcs.length, 12);
          if (Math.abs(this.rotation - lastTickAngle) >= pinSpacing) {
            lastTickAngle = this.rotation;
            if (t > 0.35) window.BiglieSound.tick();
          }

          this.draw();
        },
        () => {
          this.spinning = false;
          target.multiplier_bp > 0 ? window.BiglieSound.win() : window.BiglieSound.loss();
          resolve(target);
        }
      );
    });
  }
}
