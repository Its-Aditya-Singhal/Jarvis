// JARVIS website: loader, custom cursor, velocity marquee, colour zones, the pinned
// protocol and sentence, and the hologram particle field that morphs per section.
// No dependencies, no tracking, nothing loaded from other hosts.
document.documentElement.classList.replace("no-js", "js");
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const $ = (id) => document.getElementById(id);

  /* loader: a short "verification" countdown, skipped with reduced motion */
  const L = $("loader"), lc = $("lc");
  let assembled = reduce;
  const boot = $("boot"), bootTxt = "> Owner identified · access granted";
  function typeBoot() {
    if (reduce) { boot.textContent = bootTxt; return; }
    let i = 0;
    const iv = setInterval(() => { boot.textContent = bootTxt.slice(0, ++i); if (i >= bootTxt.length) clearInterval(iv); }, 35);
  }
  const finish = () => {
    L.classList.add("gone");
    document.body.classList.add("ready");
    assembled = true;
    document.querySelectorAll(".mega span").forEach((s, i) => (s.style.transitionDelay = 0.3 + i * 0.06 + "s"));
    setTimeout(() => document.querySelectorAll(".mega span").forEach((s) => (s.style.transitionDelay = "")), 1600);
    setTimeout(typeBoot, reduce ? 0 : 700);
  };
  if (reduce) finish();
  else {
    let n = 0;
    const iv = setInterval(() => {
      n = Math.min(100, n + Math.ceil(Math.random() * 8));
      lc.textContent = String(n).padStart(2, "0");
      if (n > 30) $("l1").textContent = "✓";
      if (n > 60) $("l2").textContent = "✓";
      if (n > 90) $("l3").textContent = "✓";
      if (n >= 100) { clearInterval(iv); setTimeout(finish, 200); }
    }, 30);
  }

  /* cursor */
  const cur = $("cur"), ring = $("ring");
  let cx = -100, cy = -100, rx = -100, ry = -100, tx = -100, ty = -100, mx = 0, my = 0;
  addEventListener("pointermove", (e) => { tx = e.clientX; ty = e.clientY; mx = tx / innerWidth - 0.5; my = ty / innerHeight - 0.5; }, { passive: true });
  document.querySelectorAll("a,.th,.ft,.mega span").forEach((el) => {
    el.addEventListener("pointerenter", () => cur.classList.add("big"));
    el.addEventListener("pointerleave", () => cur.classList.remove("big"));
  });

  /* reveals + the session log */
  const logLines = [
    ["", "17:42:06  wake word \"JARVIS\" · owner voice verified"],
    ["dim", "17:42:07  heard: \"kal subah saat baje alarm laga do\""],
    ["ok", "17:42:07  auth L2 ✓ · alarm set → 07:00 tomorrow"],
    ["co", "17:44:31  unknown voice · command refused · logged"],
    ["dim", "17:45:02  speech not addressed by name · dropped"],
    ["", "17:46:10  liveness re-check: \"blink twice\""],
    ["ok", "17:46:12  passed · session renewed"],
  ];
  const log = $("log");
  let logged = false;
  function runLog() {
    if (logged) return;
    logged = true;
    let i = 0;
    const add = () => {
      if (i >= logLines.length) return;
      const [c, t] = logLines[i++];
      const s = document.createElement("span");
      s.className = c;
      s.textContent = t + "\n";
      log.appendChild(s);
      setTimeout(add, reduce ? 0 : 450);
    };
    add();
  }
  const io = new IntersectionObserver((es) => es.forEach((e) => {
    if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); if (e.target.id === "logBox") runLog(); }
  }), { threshold: 0.12 });
  document.querySelectorAll("[data-r]").forEach((el) => io.observe(el));

  /* ---------- particles ---------- */
  const cv = $("fx"), x = cv.getContext("2d");
  let W, H, DPR;
  const size = () => { DPR = Math.min(devicePixelRatio || 1, 2); W = cv.width = innerWidth * DPR; H = cv.height = innerHeight * DPR; };
  size();
  addEventListener("resize", size);
  const N = innerWidth < 700 ? 1500 : 2800, rnd = () => Math.random() * 2 - 1;
  function head() {
    const a = [];
    while (a.length < N) {
      const u = Math.random() * 2 * Math.PI, v = Math.acos(rnd());
      const px = Math.sin(v) * Math.cos(u) * 0.78, py = Math.cos(v) * 1.02;
      let pz = Math.sin(v) * Math.sin(u) * 0.86;
      if (pz > 0) {
        if (Math.hypot(px + 0.28, py - 0.18) < 0.15 || Math.hypot(px - 0.28, py - 0.18) < 0.15) continue;
        if (Math.abs(px) < 0.07 && py < 0.15 && py > -0.2) pz += 0.12;
      }
      if (py < -0.75 && Math.abs(px) > 0.4) continue;
      a.push([px, py, pz]);
    }
    return a;
  }
  function wave() { const a = []; for (let i = 0; i < N; i++) { const t = i / N; a.push([t * 3.6 - 1.8, 0, rnd() * 0.12, t]); } return a; }
  function lock() {
    const a = [];
    for (let i = 0; i < N; i++) {
      if (i < N * 0.35) { const u = Math.PI * Math.random(), r = 0.42 + rnd() * 0.03; a.push([Math.cos(u) * r, 0.18 + Math.sin(u) * r * 1.1, rnd() * 0.08]); }
      else {
        const f = Math.floor(Math.random() * 6);
        const p = [rnd() * 0.62, -0.35 + rnd() * 0.5, rnd() * 0.18];
        if (f < 2) p[2] = f ? 0.18 : -0.18; else if (f < 4) p[0] = f === 2 ? 0.62 : -0.62; else p[1] = f === 4 ? 0.15 : -0.85;
        a.push(p);
      }
    }
    return a;
  }
  function burst() { const a = []; for (let i = 0; i < N; i++) { const u = Math.random() * 2 * Math.PI, v = Math.acos(rnd()), r = 0.5 + Math.pow(Math.random(), 0.5) * 1.4; a.push([Math.sin(v) * Math.cos(u) * r, Math.cos(v) * r, Math.sin(v) * Math.sin(u) * r]); } return a; }
  function ringShape() { const a = []; for (let i = 0; i < N; i++) { const u = Math.random() * 2 * Math.PI, r = 1.5 + rnd() * 0.06 + (i % 4 === 0 ? rnd() * 0.3 : 0); a.push([Math.cos(u) * r, rnd() * 0.05, Math.sin(u) * r]); } return a; }
  function core() { const a = []; for (let i = 0; i < N; i++) { const u = Math.random() * 2 * Math.PI, v = Math.acos(rnd()), r = i % 3 ? 1.25 + Math.random() * 0.05 : 0.3; a.push([Math.sin(v) * Math.cos(u) * r, Math.cos(v) * r * (i % 3 ? 0.1 : 1), Math.sin(v) * Math.sin(u) * r]); } return a; }
  const S = { head: head(), wave: wave(), lock: lock(), burst: burst(), ring: ringShape(), core: core() };
  const P = S.burst.map((p) => [p[0] * 2.5, p[1] * 2.5, p[2] * 2.5]);
  const zones = [...document.querySelectorAll("[data-bg]")];
  const proto = $("protocol"), phs = [...document.querySelectorAll(".ph")], rail = [...document.querySelectorAll("#rail div")];
  const pnum = $("pnum"), pbar = $("pbar"), auth = $("authState");
  const pin = document.querySelector(".pin"), sent = $("sentence"), sbar = $("sbar");
  const rows = [...document.querySelectorAll(".row")].map((r) => { r.innerHTML += r.innerHTML; return { el: r, dir: +r.dataset.v, x: 0 }; });
  let lastY = scrollY, vel = 0, phase = -1, running = true;
  const st = { ox: 0.66, oy: 0.42, a: 1, col: [244, 244, 241], light: false };
  const t0 = performance.now();
  const authNames = ["Scanning", "Challenge", "Listening", "L2 · Act"];
  document.addEventListener("visibilitychange", () => { running = !document.hidden; if (running) requestAnimationFrame(frame); });

  function frame(now) {
    if (!running) return;
    const tt = reduce ? 1 : (now - t0) / 1000;
    const y = scrollY, dv = y - lastY;
    lastY = y;
    vel += (dv - vel) * 0.12;
    // cursor
    cx += (tx - cx) * 0.35; cy += (ty - cy) * 0.35; rx += (tx - rx) * 0.12; ry += (ty - ry) * 0.12;
    cur.style.transform = `translate(${cx}px,${cy}px) translate(-50%,-50%)`;
    ring.style.transform = `translate(${rx}px,${ry}px) translate(-50%,-50%)`;
    // marquee (still with reduced motion)
    if (!reduce) rows.forEach((r) => {
      r.x += (1.2 + Math.abs(vel) * 0.5) * r.dir * (vel < 0 ? -1 : 1);
      const w = r.el.scrollWidth / 2;
      if (r.x < -w) r.x += w;
      if (r.x > 0) r.x -= w;
      r.el.style.transform = `translateX(${r.x}px) skewX(${clamp(-vel * 0.4, -14, 14)}deg)`;
    });
    // colour zone under the middle of the screen
    const mid = innerHeight * 0.5;
    let z = zones[0];
    for (const s of zones) { const b = s.getBoundingClientRect(); if (b.top <= mid && b.bottom > mid) { z = s; break; } }
    document.body.style.setProperty("--bg", z.dataset.bg);
    document.body.style.setProperty("--fg", z.dataset.fg);
    const [px0, py0] = z.dataset.pos.split(",").map(Number);
    const fg = hex(z.dataset.fg);
    const ease = reduce ? 1 : 0.05;
    st.ox += (px0 - st.ox) * ease; st.oy += (py0 - st.oy) * ease;
    st.a += (+z.dataset.pa - st.a) * (reduce ? 1 : 0.06);
    st.col = st.col.map((c, i) => c + (fg[i] - c) * (reduce ? 1 : 0.08));
    st.light = z.dataset.bg === "#f4f4f1" || z.dataset.bg === "#ff5a3c";
    // pinned protocol
    const pr = proto.getBoundingClientRect(), pp = clamp(-pr.top / (pr.height - innerHeight));
    const idx = Math.min(3, Math.floor(pp * 4.0001));
    if (idx !== phase) {
      phase = idx;
      phs.forEach((p, i) => p.classList.toggle("on", i === idx));
      rail.forEach((p, i) => p.classList.toggle("on", i === idx));
      pnum.innerHTML = `0${idx + 1}<small>/04</small>`;
    }
    pbar.style.transform = `scaleX(${pp})`;
    auth.textContent = !assembled ? "Locked" : pr.top > 0 ? "Owner verified" : pr.bottom < innerHeight * 0.5 ? "L2 · Act" : authNames[idx];
    let key = z.dataset.shape;
    if (key === "proto") key = ["head", "head", "wave", "lock"][idx];
    if (!assembled) key = "burst";
    const T = S[key];
    // pinned sentence
    const sr = pin.getBoundingClientRect(), sp = clamp(-sr.top / (sr.height - innerHeight));
    sent.style.transform = `translateX(${-sp * (sent.scrollWidth - innerWidth * 0.92)}px)`;
    sbar.style.transform = `scaleX(${sp})`;
    // particles
    x.clearRect(0, 0, W, H);
    const turn = key === "head" && z.dataset.shape === "proto" && idx === 1;
    const rotY = key === "wave" ? mx * 0.3 : tt * 0.25 + mx * 0.9 + (turn && !reduce ? Math.sin(tt * 1.6) * 0.9 : 0);
    const rotX = my * 0.45 + 0.05 + (key === "ring" ? 0.5 : 0);
    const cyy = Math.cos(rotY), syy = Math.sin(rotY), cxx = Math.cos(rotX), sxx = Math.sin(rotX);
    const narrow = innerWidth < 760;
    const scale = Math.min(W, H) * (narrow ? 0.3 : 0.34);
    const ox = W * (narrow ? 0.5 : st.ox), oy = H * (narrow ? 0.4 : st.oy);
    const jitter = reduce ? 0 : clamp(Math.abs(vel) * 0.004, 0, 0.25);
    const mpx = tx * DPR, mpy = ty * DPR, R = 140 * DPR;
    x.globalCompositeOperation = st.light ? "source-over" : "lighter";
    const [cr, cg, cb] = st.col.map(Math.round);
    const Lr = reduce ? 1 : key === "wave" ? 0.2 : 0.07;
    for (let k = 0; k < N; k++) {
      const a = T[k];
      let gx = a[0], gy = a[1];
      const gz = a[2];
      if (key === "wave") { const env = Math.sin(a[3] * Math.PI); gy += Math.sin(a[3] * 28 + tt * 4) * 0.38 * env * Math.sin(tt * 1.3 + a[3] * 6); }
      if (key === "burst" && !reduce) { gx += Math.sin(tt * 0.6 + k) * 0.03; gy += Math.cos(tt * 0.5 + k * 1.3) * 0.03; }
      const p = P[k];
      p[0] += (gx - p[0]) * Lr; p[1] += (gy - p[1]) * Lr; p[2] += (gz - p[2]) * Lr;
      let X = p[0] * cyy - p[2] * syy, Z = p[0] * syy + p[2] * cyy, Y = p[1] * cxx - Z * sxx;
      Z = p[1] * sxx + Z * cxx;
      if (jitter) { X += rnd() * jitter; Y += rnd() * jitter; }
      const d = 3 / (4.2 - Z);
      let sx = ox + X * scale * d, sy = oy - Y * scale * d;
      const dx = sx - mpx, dy = sy - mpy, dd = Math.hypot(dx, dy);
      if (dd < R && dd > 0 && !reduce) { const f = (1 - dd / R) * 40 * DPR; sx += (dx / dd) * f; sy += (dy / dd) * f; }
      const al = clamp(0.25 + (Z + 1) * 0.35, 0.08, 0.95) * st.a;
      x.fillStyle = `rgba(${cr},${cg},${cb},${al * (st.light ? 0.55 : 0.75)})`;
      const r = (1.1 + d * 0.6) * DPR;
      x.fillRect(sx, sy, r, r);
    }
    x.globalCompositeOperation = "source-over";
    // HUD rings
    x.strokeStyle = `rgba(${cr},${cg},${cb},${0.18 * st.a})`;
    x.lineWidth = DPR;
    for (let q = 0; q < 2; q++) {
      x.beginPath();
      const RR = scale * (1.45 + q * 0.16), off = tt * (q ? -0.25 : 0.18);
      for (let s = 0; s < 30; s++) { const a0 = off + (s * Math.PI) / 15; x.moveTo(ox + Math.cos(a0) * RR, oy + Math.sin(a0) * RR); x.arc(ox, oy, RR, a0, a0 + Math.PI / 34); }
      x.stroke();
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
