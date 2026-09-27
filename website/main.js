// JARVIS website: the hero's rings, reveal-on-scroll fallback, the level dial story,
// the nav background and the cursor glow on feature tiles. No dependencies, no tracking.
(() => {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // nav gets a background once the page scrolls
  const nav = document.querySelector("[data-nav]");
  const onScroll = () => nav && nav.classList.toggle("scrolled", scrollY > 24);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // staggered hero entrance
  document.querySelectorAll(".hero .reveal").forEach((el, i) => el.style.setProperty("--d", `${0.12 * i}s`));

  // reveal fallback where scroll-driven animations aren't supported
  if (!CSS.supports("animation-timeline: view()")) {
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && (e.target.classList.add("in"), io.unobserve(e.target))),
      { rootMargin: "0px 0px -10% 0px" },
    );
    document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
  }

  // the level story: the step in the middle of the screen drives the dial
  const dial = document.querySelector("[data-dial]");
  const arc = document.querySelector("[data-arc]");
  const levelText = document.querySelector("[data-level]");
  const steps = [...document.querySelectorAll("[data-step]")];
  if (dial && arc && steps.length) {
    const C = 2 * Math.PI * 84;
    arc.style.strokeDasharray = String(C);
    const show = (level) => {
      dial.dataset.l = String(level);
      levelText.textContent = String(level);
      arc.style.strokeDashoffset = String(C * (1 - Math.max(level, 0.12) / 3));
      steps.forEach((s) => s.classList.toggle("active", Number(s.dataset.step) === level));
    };
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && show(Number(e.target.dataset.step))),
      { rootMargin: "-45% 0px -45% 0px" },
    );
    steps.forEach((s) => io.observe(s));
    show(0);
  }

  // cursor glow that follows the pointer on feature tiles
  document.querySelectorAll(".tile").forEach((tile) =>
    tile.addEventListener("pointermove", (e) => {
      const r = tile.getBoundingClientRect();
      tile.style.setProperty("--mx", `${e.clientX - r.left}px`);
      tile.style.setProperty("--my", `${e.clientY - r.top}px`);
    }),
  );

  // hero: slow concentric rings and orbiting points, drawn on a canvas; they lean toward the pointer
  const canvas = document.querySelector("[data-rings]");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = 1, px = 0.5, py = 0.5, tx = 0.5, ty = 0.5, running = true;
  const resize = () => {
    dpr = Math.min(devicePixelRatio || 1, 2);
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  addEventListener("resize", resize);
  addEventListener("pointermove", (e) => { tx = e.clientX / innerWidth; ty = e.clientY / innerHeight; }, { passive: true });
  new IntersectionObserver(([e]) => { running = e.isIntersecting; if (running && !reduced) requestAnimationFrame(draw); }).observe(canvas);
  resize();

  const rings = Array.from({ length: 7 }, (_, i) => ({ r: 0.12 + i * 0.075, speed: (i % 2 ? -1 : 1) * (0.05 + i * 0.012), dots: 2 + (i % 3) }));
  function draw(t = 0) {
    const s = t / 1000;
    px += (tx - px) * 0.04; py += (ty - py) * 0.04;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2 + (px - 0.5) * 40, cy = h * 0.5 + (py - 0.5) * 30, base = Math.min(w, h);
    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, base * 0.55);
    glow.addColorStop(0, "rgba(63, 208, 255, 0.16)");
    glow.addColorStop(1, "rgba(7, 11, 18, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);
    rings.forEach((ring, i) => {
      const r = ring.r * base, a0 = s * ring.speed;
      ctx.beginPath();
      ctx.arc(cx, cy, r, a0, a0 + Math.PI * (1.2 + 0.1 * i));
      ctx.strokeStyle = `rgba(${i % 2 ? "63, 208, 255" : "92, 242, 193"}, ${0.22 - i * 0.022})`;
      ctx.lineWidth = i === 0 ? 2 : 1;
      ctx.stroke();
      for (let d = 0; d < ring.dots; d++) {
        const a = a0 * 1.6 + (d / ring.dots) * Math.PI * 2;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 1.8, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(238, 244, 251, 0.7)";
        ctx.fill();
      }
    });
    if (running && !reduced) requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();
