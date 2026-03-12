---
name: smooth-web-animation
description: Best practices for buttery-smooth web animations on desktop and mobile. Use when building interactive visualizations, scatter plots, zoomable maps, graph explorers, photo grids, Poincaré disks, node-based UIs, or any interface that needs smooth pan/zoom/pinch interactions. Also use when dealing with image loading issues on mobile Safari, canvas rendering performance, or DOM-based animation at scale. Covers camera lerp systems, mobile touch handling, image loading strategies, GPU compositing, hover interactions, Möbius fly-through animations, staggered transitions, and golden-angle node distribution.
---

# Smooth Web Animation Skill

Hard-won techniques for building fluid, 60fps web interfaces with hundreds of visual elements. These patterns come from extensive iteration across mobile Safari, Chrome, and Firefox — follow them to avoid days of troubleshooting.

---

## The Camera Lerp Pattern (Most Important)

### Never snap transforms directly from input

The single biggest difference between "feels like an app" and "feels like a webpage" is interpolated camera motion. Input events set a **target**; a rAF loop lerps the **rendered state** toward it.

```javascript
const target = { x: 0, y: 0, k: 1 };   // what input wants
const cam    = { x: 0, y: 0, k: 1 };   // what's rendered
const LERP = 0.12;

function tick() {
  const dx = target.x - cam.x;
  const dy = target.y - cam.y;
  const dk = target.k - cam.k;
  if (Math.abs(dx) > 0.05 || Math.abs(dy) > 0.05 || Math.abs(dk) > 0.0001) {
    cam.x += dx * LERP;
    cam.y += dy * LERP;
    cam.k += dk * LERP;
    applyTransform();
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function onDrag(dx, dy)    { target.x += dx; target.y += dy; }
function onZoom(sx, sy, f) {
  const oldK = target.k;
  const newK = clamp(oldK * f, 0.02, 40);
  target.x = sx - (sx - target.x) * (newK / oldK);
  target.y = sy - (sy - target.y) * (newK / oldK);
  target.k = newK;
}
```

### For initial positioning, snap (don't lerp)

```javascript
function autoFit(snap) {
  // ... compute target from data bounds ...
  if (snap) { cam.x = target.x; cam.y = target.y; cam.k = target.k; }
}
autoFit(true);  // first load: snap
// on resize: autoFit(false) — smooth glide
```

---

## Image Loading on Mobile Safari

### CRITICAL: Use CSS background-image, NOT JavaScript Image() or <img src>

| Method | Mobile Safari Result |
|--------|---------------------|
| `new Image(); img.src = url` | Stuck at loading forever |
| `img.crossOrigin = 'anonymous'` | Makes same-origin loads worse |
| `<img src>` all at once | Same stuck behavior |
| `<img loading="lazy">` in transformed container | Never triggers |
| **`<div style="background-image:url(...)">`** | **Works perfectly** |

```javascript
// WRONG
const img = new Image(); img.src = '/uploads/thumb.jpg';

// RIGHT
const el = document.createElement('div');
el.style.backgroundImage = `url(/uploads/thumb.jpg)`;
el.style.backgroundSize = 'cover';
el.style.backgroundPosition = 'center';
container.appendChild(el);
```

### Never set crossOrigin on same-origin images

---

## Minimum Thumbnail Size — Draw Outlines Below Threshold

At small zoom, thumbnails are unrecognizable. Draw outlined squares below ~16px:

```javascript
const screenSize = TILE_SIZE * cam.zoom;
if (screenSize < 16) {
  ctx.strokeStyle = 'rgba(100,140,200,0.3)';
  ctx.lineWidth = 0.5;
  ctx.strokeRect(sx, sy, screenSize, screenSize);
} else {
  ctx.drawImage(img, sx, sy, screenSize, screenSize);
}
```

---

## DOM vs Canvas for Image-Heavy Views

### Use DOM tiles when images come from URLs

```html
<div id="field" style="position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform">
  <div class="tile" style="left:120px;top:340px;background-image:url(...)"></div>
</div>
```

```javascript
function applyTransform() {
  field.style.transform = `translate(${cam.x}px, ${cam.y}px) scale(${cam.k})`;
}
```

### Use canvas for procedural graphics (lines, glows, labels)

### Hybrid: SVG geometry + HTML div overlay for images

---

## Hover Interactions — Grow, Glow, Z-Order

### Scatter tiles: CSS-driven hover

```css
.scatter-tile {
  transition: transform 0.18s ease, box-shadow 0.18s ease;
  transform-origin: center;
}
.scatter-tile:hover {
  transform: scale(2.2);
  z-index: 100;
  box-shadow: 0 0 20px rgba(212,168,83,0.4), 0 4px 16px rgba(0,0,0,0.6);
}
```

### Poincaré nodes: JS-driven hover with CSS transition

```javascript
// On hover:
const growR = Math.max(sp.r, Math.min(44, diskR * 0.14));
el.style.width = (growR * 2) + 'px';
el.style.height = (growR * 2) + 'px';
el.classList.add('hovered');
container.appendChild(el);  // bring to front

// On unhover:
el.classList.remove('hovered');
el.style.width = (sp.r * 2) + 'px';
el.style.height = (sp.r * 2) + 'px';
```

```css
.node {
  transition: width 0.25s ease, height 0.25s ease, box-shadow 0.2s ease;
  transform: translate(-50%, -50%);
}
.node.hovered {
  z-index: 50;
  box-shadow: 0 0 16px rgba(212,168,83,0.5), 0 4px 20px rgba(0,0,0,0.7);
}
```

### OpenMind's rAF hover animation (advanced)

```javascript
const _hoverSizes = new Map();
function _animateHoverSizes() {
  for (const [id, cur] of _hoverSizes) {
    const targetR = (id === hoveredId) ? expandedR : naturalR;
    const diff = targetR - cur;
    const next = Math.abs(diff) > 0.3 ? cur + 0.25 * diff : targetR;
    _hoverSizes.set(id, next);
    applySize(id, next);
    if (Math.abs(diff) <= 0.3 && id !== hoveredId) _hoverSizes.delete(id);
  }
  if (_hoverSizes.size > 0) requestAnimationFrame(_animateHoverSizes);
}
```

---

## Möbius Fly-Through Animation (Poincaré Navigation)

### The pattern: fly-through → recompute → settle

1. **Fly-through (600ms):** `atanh`/`tanh` interpolation with easeInOut. Transform ALL positions each frame.
2. **Recompute layout** for new center.
3. **Settle (300ms):** smoothstep lerp from Möbius end to clean positions.

```javascript
function animateToNode(targetId) {
  const target = positions.get(targetId);
  const atanhR = Math.atanh(Math.min(cAbs(target), 0.999));
  const dur = 600, t0 = performance.now();
  const oldPos = new Map(positions);

  // Disable CSS transitions during rAF
  nodes.forEach(n => n.el.style.transition = 'none');

  function frame(now) {
    const t = Math.min((now - t0) / dur, 1);
    const et = t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2, 2) / 2;
    const r = Math.tanh(et * atanhR);
    const a = cScale(target, r / cAbs(target));
    for (const [id, pos] of oldPos) updateNodePosition(id, mobiusTransform(pos, a));
    if (t < 1) requestAnimationFrame(frame);
    else startSettle();
  }

  function startSettle() {
    const settled = new Map();
    for (const [id, pos] of oldPos) settled.set(id, mobiusTransform(pos, target));
    const s0 = performance.now();
    function settle(now) {
      const st = Math.min((now - s0) / 300, 1);
      const se = st * st * (3 - 2 * st);  // smoothstep
      for (const [id, clean] of newPositions) {
        const end = settled.get(id);
        if (end) updateNodePosition(id, [end[0]+(clean[0]-end[0])*se, end[1]+(clean[1]-end[1])*se]);
      }
      if (st < 1) requestAnimationFrame(settle);
      else { nodes.forEach(n => n.el.style.transition = ''); fullRender(); }
    }
    requestAnimationFrame(settle);
  }
  requestAnimationFrame(frame);
}
```

### Key: disable CSS transitions during rAF animations

---

## Touch Event Handling

### Always { passive: false } for preventDefault

```javascript
container.addEventListener('touchmove', handler, { passive: false });
```

### Set touch-action: none

```css
#my-view { touch-action: none; }
```

### Handle 2-finger → 1-finger transition

```javascript
container.addEventListener('touchend', e => {
  if (e.touches.length < 2) { pinching = false; lastPinchDist = 0; }
  if (e.touches.length === 0) dragging = false;
  if (e.touches.length === 1) {
    dragging = true;
    lastX = e.touches[0].clientX;
    lastY = e.touches[0].clientY;
  }
});
```

### Zoom toward pinch midpoint

### Mobile touch hover: grow on touchstart, shrink on touchend

```javascript
container.addEventListener('touchstart', e => {
  const node = e.target.closest('.node');
  if (node) growNode(node);
}, { passive: true });
container.addEventListener('touchend', e => {
  const node = e.target.closest('.node');
  if (node) setTimeout(() => shrinkNode(node), 300);
}, { passive: false });
```

---

## GPU Compositing

### will-change: transform on animated container

### Cap DPR at 2 on phones

```javascript
const DPR = Math.min(window.devicePixelRatio || 1, isPhone ? 2 : 3);
```

---

## Staggered Entry Animations

```css
.tile { opacity: 0; transition: opacity 0.4s ease; }
.tile.loaded { opacity: 1; }
```

```javascript
nodes.forEach((n, i) => setTimeout(() => n.el.classList.add('loaded'), 30 + i * 8));
```

### Fade out before switching datasets

```javascript
document.querySelectorAll('.tile').forEach(t => t.classList.remove('loaded'));
setTimeout(() => loadNewData(), 300);
```

---

## Even Node Distribution (Golden Angle)

```javascript
const GOLDEN_ANGLE = 2.399963;  // ≈ 137.508°
nodes.forEach((node, rank) => {
  const r = 0.10 + (rank / (N-1)) * 0.62;
  const a = rank * GOLDEN_ANGLE;
  positions.set(node.id, [Math.cos(a) * r, Math.sin(a) * r]);
});
```

Never sort by PCA angle then distribute — creates C-arcs.

---

## The rAF Main Loop

```javascript
function loop() {
  const moving = Math.abs(target.x-cam.x) > 0.1 || Math.abs(target.y-cam.y) > 0.1 || Math.abs(target.k-cam.k) > 0.001;
  if (moving || dirty) {
    cam.x += (target.x - cam.x) * LERP;
    cam.y += (target.y - cam.y) * LERP;
    cam.k += (target.k - cam.k) * LERP;
    draw(); dirty = false;
  }
  requestAnimationFrame(loop);
}
```

---

## Server-Side: Cache PCA Basis

```python
_basis = None
def compute_layout(limit=100):
    global _basis
    embs = load_embeddings(limit)
    if _basis is None:
        mean = embs[:100].mean(axis=0)
        U, S, Vt = np.linalg.svd(embs[:100] - mean, full_matrices=False)
        basis_proj = (embs[:100] - mean) @ Vt[:2].T
        _basis = (mean, Vt[:2], basis_proj.min(0), basis_proj.ptp(0) + 1e-8)
    mean, Vt2, pmin, prange = _basis
    return np.clip(((embs - mean) @ Vt2.T - pmin) / prange, -0.1, 1.1)
```

---

## Anti-Patterns

| Anti-Pattern | Fix |
|---|---|
| `new Image()` for 100+ loads on mobile Safari | CSS `background-image` on `<div>` |
| `crossOrigin = 'anonymous'` on same-origin | Remove it |
| `loading="lazy"` in transformed container | Remove it |
| Direct transform snap from input | Camera lerp |
| Sort-then-distribute in circles | Golden angle |
| Canvas DPR 3 on iPhone | Cap at 2 |
| Touch without `{ passive: false }` | Always specify |
| CSS transitions during rAF | Disable, restore after |
| Tiny thumbnails at low zoom | Outlined squares below 16px |
| Recompute SVD per request | Cache basis, project |
