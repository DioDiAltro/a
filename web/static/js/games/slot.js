/* =============================================================================
   Slot
   =============================================================================
   Three reels, staggered stops, server-decided symbols.

   A slot reel is a cylinder, and the thing that separates a convincing one from
   a scrolling list is that the strip is drawn with *perspective compression*:
   symbols near the top and bottom of the window are squashed and dimmed because
   they are curving away from the viewer. Without it the reel reads as a sliding
   texture. With it, it reads as a drum.

   The other half is the stagger. Reels stop left to right with a widening gap,
   so the last reel lands alone — that gap is where a near-miss becomes a
   feeling instead of a pixel arrangement.
   ========================================================================== */

const SYMBOLS = ["◍", "★", "◆", "▲", "●", "✦", "7"];

export class SlotMachine {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts {reels, rows, symbols}
   */
  constructor(canvas, { reels = 3, rows = 3, symbols = SYMBOLS, width = 420 } = {}) {
    this.reelCount = reels;
    this.rows = rows;
    this.symbols = symbols;
    this.spinning = false;

    this.cellW = Math.floor(width / reels);
    this.cellH = 96;
    this.width = this.cellW * reels;
    this.height = this.cellH * rows;

    this.ctx = window.fitCanvas(canvas, this.width, this.height);

    // Each reel holds a long strip so a fast spin never runs out of symbols.
    this.strips = Array.from({ length: reels }, () =>
      Array.from({ length: 48 }, () => symbols[Math.floor(Math.random() * symbols.length)])
    );
    this.offsets = new Array(reels).fill(0);

    this.draw();
  }

  drawSymbol(ctx, symbol, x, centreY, distanceFromCentre) {
    // 1 at the centre line, falling off toward the window edges. Drives both
    // the vertical squash and the fade, which is what makes the strip curve.
    const proximity = Math.max(0, 1 - Math.abs(distanceFromCentre) / (this.cellH * 1.6));
    const squash = 0.55 + 0.45 * proximity;
    const alpha = 0.28 + 0.72 * proximity;

    ctx.save();
    ctx.translate(x + this.cellW / 2, centreY);
    ctx.scale(1, squash);
    ctx.globalAlpha = alpha;
    ctx.font = `600 ${Math.round(this.cellH * 0.52)}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    if (symbol === "7") {
      ctx.fillStyle = "#ffc24b";
      ctx.shadowColor = "rgba(255,194,75,0.55)";
      ctx.shadowBlur = 18 * proximity;
    } else if (symbol === "◍") {
      ctx.fillStyle = "#64e9ff";
      ctx.shadowColor = "rgba(100,233,255,0.5)";
      ctx.shadowBlur = 14 * proximity;
    } else {
      ctx.fillStyle = "#b9c0d4";
    }

    ctx.fillText(symbol, 0, 0);
    ctx.restore();
  }

  draw() {
    const { ctx, width, height, cellH, cellW } = this;
    ctx.clearRect(0, 0, width, height);

    const centreY = height / 2;

    for (let reel = 0; reel < this.reelCount; reel++) {
      const strip = this.strips[reel];
      const offset = this.offsets[reel];
      const x = reel * cellW;

      ctx.save();
      ctx.beginPath();
      ctx.rect(x + 2, 0, cellW - 4, height);
      ctx.clip();

      // Draw a couple of cells past the window on each side so symbols slide
      // in already curving rather than popping into existence at the edge.
      const first = Math.floor(offset / cellH) - 1;
      for (let i = first; i < first + this.rows + 3; i++) {
        const symbol = strip[((i % strip.length) + strip.length) % strip.length];
        const y = i * cellH - offset + cellH / 2;
        this.drawSymbol(ctx, symbol, x, y, y - centreY);
      }
      ctx.restore();

      // Reel separator.
      if (reel > 0) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.strokeStyle = "rgba(255,255,255,0.06)";
        ctx.stroke();
      }
    }

    // Drum shading: the housing darkens toward the top and bottom lips.
    const shade = ctx.createLinearGradient(0, 0, 0, height);
    shade.addColorStop(0, "rgba(5,6,11,0.92)");
    shade.addColorStop(0.28, "rgba(5,6,11,0.12)");
    shade.addColorStop(0.72, "rgba(5,6,11,0.12)");
    shade.addColorStop(1, "rgba(5,6,11,0.92)");
    ctx.fillStyle = shade;
    ctx.fillRect(0, 0, width, height);

    // Payline.
    ctx.beginPath();
    ctx.moveTo(0, centreY);
    ctx.lineTo(width, centreY);
    ctx.strokeStyle = "rgba(255,194,75,0.35)";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  /**
   * Spin to a result the server already computed.
   * @param {string[]} result one symbol per reel, landing on the payline
   * @returns {Promise<void>}
   */
  async spin(result) {
    if (this.spinning) return;
    this.spinning = true;
    window.BiglieSound.click();

    const stops = result.map((symbol, reel) => {
      // Plant the winning symbol into the strip at a known index rather than
      // hunting for it: it guarantees the reel can always land where the server
      // said, whatever the strip happens to contain.
      const strip = this.strips[reel];
      const index = 20 + reel * 3;
      strip[index % strip.length] = symbol;
      return index;
    });

    await Promise.all(
      stops.map((stopIndex, reel) => this.spinReel(reel, stopIndex, 1500 + reel * 620))
    );

    this.spinning = false;

    const won = result.every((s) => s === result[0]);
    won ? window.BiglieSound.win() : window.BiglieSound.loss();
  }

  spinReel(reel, stopIndex, duration) {
    const cellH = this.cellH;
    const strip = this.strips[reel];
    const from = this.offsets[reel];

    // Always travel forward through several full revolutions so the direction
    // never visibly reverses on its way to a nearby stop.
    const revolutions = 3 + reel;
    const target = stopIndex * cellH - cellH; // payline sits on the middle row
    const to = target + strip.length * cellH * revolutions;

    let lastTickCell = Math.floor(from / cellH);

    return new Promise((resolve) => {
      window.animate(
        duration,
        (t) => {
          // Exponential rather than quintic: reels should hold speed longer
          // and then arrest hard, where a wheel coasts down gradually.
          const eased = window.BiglieEase.outExpo(t);
          this.offsets[reel] = from + (to - from) * eased;

          const cell = Math.floor(this.offsets[reel] / cellH);
          if (cell !== lastTickCell) {
            lastTickCell = cell;
            if (t > 0.55) window.BiglieSound.tick();
          }

          if (reel === 0) this.draw(); // one draw per frame for all reels
        },
        () => {
          this.offsets[reel] = ((to % (strip.length * cellH)) + strip.length * cellH) %
            (strip.length * cellH);
          this.draw();
          resolve();
        }
      );
    });
  }
}
