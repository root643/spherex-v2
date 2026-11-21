/* SPHEREx Blink — cliente. Vanilla JS, sin dependencias. */
(() => {
  "use strict";
  const API = "/api/v1";
  const $ = (id) => document.getElementById(id);
  const TARGETS = [
    { name: "Barnard", q: "Barnard's Star", desc: "Barnard's Star: largest known proper motion (10.3″/yr). Highly visible in time-series blinking.", ra: 269.4521, dec: 4.6934, fov: 240, band: "D2" },
    { name: "WISE 0855", q: "WISE 0855-0714", desc: "WISE 0855-0714: coldest sub-stellar brown dwarf (~250 K), observable in D5/D6 thermal bands.", ra: 133.7951, dec: -7.2451, fov: 240, band: "D5" },
    { name: "ε Indi B", q: "Epsilon Indi B", desc: "Epsilon Indi B: nearby brown dwarf binary system (12 ly) with high proper motion.", ra: 331.044, dec: -56.783, fov: 300, band: "D3" },
    { name: "RW Aur", q: "RW Aurigae", desc: "RW Aurigae: young variable T Tauri binary star with circumstellar accretion disk.", ra: 76.956, dec: 30.401, fov: 300, band: "D2" },
    { name: "R Dra", q: "R Draconis", desc: "R Draconis: pulsating Mira-type red giant variable with intense brightness changes across epochs.", ra: 248.168, dec: 66.755, fov: 300, band: "D2" },
  ];
  const DET = { 1: [0.75, 1.10], 2: [1.10, 1.63], 3: [1.63, 2.42], 4: [2.42, 3.83], 5: [3.83, 4.42], 6: [4.42, 5.0] };
  const DET_COLOR = ["#7aa2f7", "#7fd1b9", "#c3e88d", "#ffb454", "#ff8a65", "#f07178"];

  // ------------------------------------------------------------------ estado <-> URL
  const P = new URLSearchParams(location.search);
  const num = (k, d) => (P.has(k) && isFinite(parseFloat(P.get(k))) ? parseFloat(P.get(k)) : d);
  const state = {
    ra: num("ra", null), dec: num("dec", null),
    fov: Math.min(1800, Math.max(60, num("fov", 300))),
    band: /^(D[1-6]|ch\d{1,3})$/.test(P.get("band") || "") ? P.get("band") : "D4",
    mode: ["blink", "diff", "coadd"].includes(P.get("mode")) ? P.get("mode") : "blink",
    stretch: ["asinh", "log", "sqrt", "linear"].includes(P.get("stretch")) ? P.get("stretch") : "asinh",
    invert: P.get("invert") === "1",
    speed: Math.min(1500, Math.max(100, num("speed", 500))),
    ref: Math.max(0, Math.round(num("ref", 0))),
    common: P.get("common") !== "0",
    cross: P.get("cross") !== "0",
    gaia: P.get("gaia") === "1",
    wise: P.get("wise") === "1",
  };
  let epochs = [];          // epocas devueltas por /observations
  let frames = [];          // [{url, label, meta}]
  let idx = 0, timer = null, playing = true, loadToken = 0;
  let grid = { size: 0, scale: 0 };  // rejilla del fotograma actual (px, arcsec/px)
  let marker = null;        // {ra, dec} del ultimo clic
  let targetName = "";      // nombre del ultimo objetivo elegido o buscado (para el foro)
  let baseFov = state.fov;  // FOV de los fotogramas cargados en el visor
  let zoomDebounceTimer = null;
  let panOffset = { x: 0, y: 0 };
  let clickTimer = null;
  let spectrumAbortController = null;
  let gaiaSources = [];
  let wiseSources = [];
  const catalogCache = new Map();

  function abortSpectrum() {
    clearTimeout(clickTimer);
    if (spectrumAbortController) {
      spectrumAbortController.abort();
      spectrumAbortController = null;
      if (chartLoader) chartLoader.done("");
      $("sedInfo").textContent = "Click or tap an object to analyze its spectrum.";
    }
  }

  function applyVisualTransform() {
    const img = $("img");
    if (!img) return;
    const zoomRatio = (baseFov && state.fov) ? (baseFov / state.fov) : 1;
    const hasPan = Math.abs(panOffset.x) > 0.5 || Math.abs(panOffset.y) > 0.5;
    const hasZoom = Math.abs(zoomRatio - 1) >= 0.005;

    if (!hasPan && !hasZoom) {
      img.style.transform = "none";
    } else if (hasPan && hasZoom) {
      img.style.transform = `translate(${panOffset.x.toFixed(1)}px, ${panOffset.y.toFixed(1)}px) scale(${zoomRatio.toFixed(4)})`;
    } else if (hasPan) {
      img.style.transform = `translate(${panOffset.x.toFixed(1)}px, ${panOffset.y.toFixed(1)}px)`;
    } else {
      img.style.transform = `scale(${zoomRatio.toFixed(4)})`;
    }
    drawOverlay();
  }

  function applyVisualZoom(ratio) {
    applyVisualTransform();
  }

  function animateRecenter(targetTx, targetTy) {
    const img = $("img");
    if (!img) return;
    img.classList.add("panning");
    panOffset.x = targetTx;
    panOffset.y = targetTy;
    applyVisualTransform();
    setTimeout(() => {
      img.classList.remove("panning");
    }, 290);
  }

  function changeZoom(delta) {
    clearTimeout(clickTimer);
    abortSpectrum();
    const step = state.fov < 300 ? 30 : (state.fov < 900 ? 60 : 120);
    const nextFov = Math.max(30, Math.min(1800, state.fov + delta * step));
    if (nextFov !== state.fov) {
      state.fov = nextFov;
      if ($("fovExact")) $("fovExact").value = state.fov;
      $("fov").value = Math.min(1800, Math.max(30, state.fov));
      $("fovOut").textContent = state.fov + "″ (" + (state.fov / 60).toFixed(2) + "′)";
      syncUrl();
      applyVisualTransform();
      clearTimeout(zoomDebounceTimer);
      zoomDebounceTimer = setTimeout(() => reload({ zoomOnly: true }), 260);
    }
  }

  function syncUrl() {
    const q = new URLSearchParams();
    if (state.ra != null) { q.set("ra", state.ra.toFixed(5)); q.set("dec", state.dec.toFixed(5)); }
    q.set("fov", state.fov); q.set("band", state.band); q.set("mode", state.mode);
    q.set("stretch", state.stretch); q.set("speed", state.speed);
    if (state.invert) q.set("invert", "1");
    if (state.mode === "diff") q.set("ref", state.ref);
    if (!state.common) q.set("common", "0");
    if (!state.cross) q.set("cross", "0");
    if (state.gaia) q.set("gaia", "1");
    if (state.wise) q.set("wise", "1");
    if (marker) { q.set("mra", marker.ra.toFixed(5)); q.set("mdec", marker.dec.toFixed(5)); }
    history.replaceState(null, "", "?" + q.toString());
  }

  const detOf = (band) => band.startsWith("D") ? +band[1] : Math.ceil(+band.slice(2) / 17);
  function chRange(ch) {
    const d = Math.ceil(ch / 17), k = (ch - 1) % 17, [lo, hi] = DET[d], s = (hi - lo) / 17;
    return [lo + k * s, lo + (k + 1) * s];
  }

  // ------------------------------------------------------------------ buffer loader psicologico con ETA
  function createBufferLoader(wrapEl, textEl) {
    const fillEl = wrapEl ? wrapEl.querySelector(".buffer-fill") : null;
    let startTime = 0;
    let baseText = "";
    let raf = null;
    let finished = true;
    let currentPct = 0;

    function frame(now) {
      if (finished) return;
      const elapsed = now - startTime;

      // Curva psicologica:
      // 0 a 160ms: salta de 0% a 68% (arranque explosivo: percepcion de velocidad inmediata)
      // 160 a 550ms: sube suavemente de 68% a 84%
      // 550ms+: se frena asintoticamente (crawling) hacia el 92% mientras el servidor entrega los bytes
      if (elapsed < 160) {
        currentPct = (elapsed / 160) * 68;
      } else if (elapsed < 550) {
        currentPct = 68 + ((elapsed - 160) / 390) * 16;
      } else {
        const extra = 8 * (1 - Math.exp(-(elapsed - 550) / 900));
        currentPct = 84 + extra;
      }

      if (fillEl) fillEl.style.width = `${currentPct.toFixed(1)}%`;

      // ETA psicologico dinamico
      let eta = "";
      if (currentPct < 65) {
        eta = "~0.5s";
      } else if (currentPct < 80) {
        eta = "~0.2s";
      } else {
        eta = "~0.1s";
      }

      if (textEl && !finished) {
        textEl.innerHTML = `${baseText} <span class="buffer-eta mono">[${Math.round(currentPct)}% · ${eta}]</span>`;
      }

      raf = requestAnimationFrame(frame);
    }

    return {
      start(label) {
        finished = false;
        currentPct = 0;
        baseText = label || "";
        startTime = performance.now();
        if (fillEl) {
          fillEl.className = "buffer-fill";
          fillEl.style.width = "0%";
        }
        if (wrapEl) wrapEl.classList.add("active");
        if (textEl) textEl.classList.remove("err");
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(frame);
      },
      step(pct, label) {
        if (finished) return;
        if (label) baseText = label;
        if (pct > currentPct) currentPct = pct;
        if (fillEl) fillEl.style.width = `${currentPct.toFixed(1)}%`;
      },
      done(finalLabel) {
        finished = true;
        cancelAnimationFrame(raf);
        if (fillEl) {
          fillEl.style.width = "100%";
          fillEl.classList.add("done");
        }
        if (textEl && finalLabel !== undefined) {
          textEl.textContent = finalLabel;
        }
        setTimeout(() => {
          if (wrapEl) wrapEl.classList.remove("active");
          setTimeout(() => {
            if (finished && fillEl) fillEl.style.width = "0%";
          }, 300);
        }, 220);
      },
      fail(errLabel) {
        finished = true;
        cancelAnimationFrame(raf);
        if (fillEl) fillEl.classList.add("err");
        if (textEl && errLabel !== undefined) {
          textEl.textContent = errLabel;
          textEl.classList.add("err");
        }
        setTimeout(() => {
          if (wrapEl) wrapEl.classList.remove("active");
        }, 600);
      }
    };
  }

  let viewerLoader = null;
  let chartLoader = null;

  // ------------------------------------------------------------------ controles
  function setMsg(text, err) { const m = $("msg"); m.textContent = text || ""; m.classList.toggle("err", !!err); }

  function randomCelestialCoord() {
    const ra = +(Math.random() * 359.9999).toFixed(4);
    const u = Math.random() * 2 - 1;
    const dec = +(Math.asin(u) * 180 / Math.PI).toFixed(4);
    const band = "D" + (Math.floor(Math.random() * 6) + 1);
    return { name: `Celestial Sphere (${ra}° ${dec >= 0 ? "+" : ""}${dec}°)`, ra, dec, fov: 300, band, type: "Uniform distribution cos(Dec)" };
  }

  async function handleRandomCoordinates() {
    let t = null;
    try {
      t = await getJson(`${API}/targets/random`);
    } catch {
      t = randomCelestialCoord();
    }
    state.ra = t.ra;
    state.dec = t.dec;
    if (t.fov) state.fov = t.fov;
    if (t.band) state.band = t.band;
    marker = null; targetName = t.name || "";
    paintControls();
    syncUrl();
    setMsg(`🎲 Random coordinates: RA ${state.ra.toFixed(4)}°, Dec ${state.dec >= 0 ? "+" : ""}${state.dec.toFixed(4)}° (${state.band})`);
    reload();
  }

  function updateGaiaUI() {
    if ($("overlayGaia")) $("overlayGaia").checked = state.gaia;
    if ($("stageGaia")) $("stageGaia").classList.toggle("active-gaia", state.gaia);
    if ($("gaiaLegend")) $("gaiaLegend").classList.toggle("active", state.gaia);
    if ($("stageGaiaLeg")) $("stageGaiaLeg").hidden = !state.gaia;
    if (state.gaia && $("gaiaCount")) {
      $("gaiaCount").textContent = gaiaSources.length;
    } else if ($("gaiaCount")) {
      $("gaiaCount").textContent = "0";
    }
    const anyActive = state.gaia || state.wise;
    if ($("stageLegend")) $("stageLegend").hidden = !anyActive;
  }

  function updateWiseUI() {
    if ($("overlayWise")) $("overlayWise").checked = state.wise;
    if ($("stageWise")) $("stageWise").classList.toggle("active-wise", state.wise);
    if ($("wiseLegend")) $("wiseLegend").classList.toggle("active", state.wise);
    if ($("stageWiseLeg")) $("stageWiseLeg").hidden = !state.wise;
    if (state.wise && $("wiseCount")) {
      $("wiseCount").textContent = wiseSources.length;
    } else if ($("wiseCount")) {
      $("wiseCount").textContent = "0";
    }
    const anyActive = state.gaia || state.wise;
    if ($("stageLegend")) $("stageLegend").hidden = !anyActive;
  }

  let catalogReqId = 0;
  async function loadCatalogs(force = false) {
    if (state.ra == null) return;
    const reqId = ++catalogReqId;
    const currentRa = state.ra, currentDec = state.dec, currentFov = state.fov;
    const radius = Math.max(10, Math.min(1800, Math.round(currentFov * 0.7)));

    if (!state.gaia && !state.wise) {
      gaiaSources = [];
      wiseSources = [];
      updateGaiaUI();
      updateWiseUI();
      drawOverlay();
      return;
    }

    const gaiaKey = `gaia:${currentRa.toFixed(4)}:${currentDec.toFixed(4)}:${radius}`;
    const wiseKey = `wise:${currentRa.toFixed(4)}:${currentDec.toFixed(4)}:${radius}`;

    // Limpiamos fuentes previas si cambiamos de campo para no superponer fuentes antiguas
    if (state.gaia && !catalogCache.has(gaiaKey)) {
      gaiaSources = [];
      updateGaiaUI();
      if ($("gaiaLegend")) $("gaiaLegend").hidden = false;
      if ($("gaiaCount")) $("gaiaCount").textContent = "…";
    }
    if (state.wise && !catalogCache.has(wiseKey)) {
      wiseSources = [];
      updateWiseUI();
      if ($("wiseLegend")) $("wiseLegend").hidden = false;
      if ($("wiseCount")) $("wiseCount").textContent = "…";
    }
    drawOverlay();

    const fetchGaia = async () => {
      if (!state.gaia) {
        gaiaSources = [];
        updateGaiaUI();
        return;
      }
      if (!force && catalogCache.has(gaiaKey)) {
        if (reqId !== catalogReqId) return;
        gaiaSources = catalogCache.get(gaiaKey);
        updateGaiaUI();
        drawOverlay();
        return;
      }
      try {
        const res = await getJson(`${API}/catalogs/gaia?ra=${currentRa}&dec=${currentDec}&radius=${radius}&limit=150`);
        if (reqId !== catalogReqId) return;
        gaiaSources = res.sources || [];
        catalogCache.set(gaiaKey, gaiaSources);
        updateGaiaUI();
        drawOverlay();
      } catch {
        if (reqId !== catalogReqId) return;
        if ($("gaiaCount")) $("gaiaCount").textContent = "err";
      }
    };

    const fetchWise = async () => {
      if (!state.wise) {
        wiseSources = [];
        updateWiseUI();
        return;
      }
      if (!force && catalogCache.has(wiseKey)) {
        if (reqId !== catalogReqId) return;
        wiseSources = catalogCache.get(wiseKey);
        updateWiseUI();
        drawOverlay();
        return;
      }
      try {
        const res = await getJson(`${API}/catalogs/wise?ra=${currentRa}&dec=${currentDec}&radius=${radius}&limit=150`);
        if (reqId !== catalogReqId) return;
        wiseSources = res.sources || [];
        catalogCache.set(wiseKey, wiseSources);
        updateWiseUI();
        drawOverlay();
      } catch {
        if (reqId !== catalogReqId) return;
        if ($("wiseCount")) $("wiseCount").textContent = "err";
      }
    };

    await Promise.all([fetchGaia(), fetchWise()]);
  }

  function paintControls() {
    $("ra").value = state.ra ?? ""; $("dec").value = state.dec ?? "";
    if ($("fovExact")) $("fovExact").value = state.fov;
    if ($("fovEquiv")) $("fovEquiv").value = (state.fov / 60).toFixed(2) + "′";
    $("fov").value = Math.min(1800, Math.max(30, state.fov));
    $("fovOut").textContent = `${state.fov}″ (${(state.fov / 60).toFixed(2)}′)`;
    if ($("fovPresets")) {
      [...$("fovPresets").children].forEach((b) => b.setAttribute("aria-pressed", String(Math.abs(+b.dataset.fov - state.fov) < 1e-2)));
    }
    $("stretch").value = state.stretch; $("invert").checked = state.invert;
    $("speed").value = state.speed; $("speedOut").textContent = state.speed + " ms";
    $("common").checked = state.common; $("cross").checked = state.cross;
    updateGaiaUI();
    updateWiseUI();
    const fine = state.band.startsWith("ch");
    $("useCh").checked = fine; $("chWrap").hidden = !fine;
    if (fine) {
      const ch = +state.band.slice(2), [a, b] = chRange(ch);
      $("ch").value = ch; $("chOut").textContent = `${ch} · ${a.toFixed(2)}–${b.toFixed(2)} µm`;
    }
    const det = detOf(state.band);
    [...$("bands").children].forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.det === det)));
    [...$("modes").children].forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.mode)));
    $("refWrap").hidden = state.mode !== "diff";
    [...$("targets").children].forEach((b, i) => {
      const t = TARGETS[i];
      b.setAttribute("aria-pressed", String(state.ra != null && Math.abs(t.ra - state.ra) < 1e-3 && Math.abs(t.dec - state.dec) < 1e-3));
    });
  }

  function buildControls() {
    if ($("randCoord")) $("randCoord").onclick = handleRandomCoordinates;
    TARGETS.forEach((t) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = t.name;
      b.title = `${t.name} (${t.q})\n${t.desc}`;
      b.onclick = () => { Object.assign(state, { ra: t.ra, dec: t.dec, fov: t.fov, band: t.band }); marker = null; targetName = t.q; reload(); };
      $("targets").appendChild(b);
    });
    const DET_DESC = {
      1: "Detector 1 (0.75 – 1.10 µm)\nNear-infrared optical (NIR). Optimal for cool stars and M dwarfs.",
      2: "Detector 2 (1.10 – 1.63 µm)\nNear-infrared (J-band). Detects water and hydrocarbon absorption signatures.",
      3: "Detector 3 (1.63 – 2.42 µm)\nNear-infrared (H and K bands). Sensitive to interstellar ices and dust clouds.",
      4: "Detector 4 (2.42 – 3.83 µm)\nMid-infrared (MIR). Covers the deep water ice absorption band (3.05 µm).",
      5: "Detector 5 (3.83 – 4.42 µm)\nMid-infrared (MIR). Includes diagnostic CO2 ice band (4.27 µm).",
      6: "Detector 6 (4.42 – 5.00 µm)\nSPHEREx infrared limit. Maximum sensitivity to ultra-cool sources and Y dwarfs."
    };
    for (let d = 1; d <= 6; d++) {
      const b = document.createElement("button");
      b.type = "button"; b.dataset.det = d; b.textContent = "D" + d;
      b.title = DET_DESC[d];
      b.onclick = () => { state.band = "D" + d; reload(); };
      $("bands").appendChild(b);
    }
    const MODE_DESC = {
      blink: "Blink Mode\nCycles through SPHEREx epochs to reveal apparent motion or time variability.",
      diff: "Difference Mode (Subtraction)\nSubtracts each epoch from the reference. Fixed objects cancel out, highlighting moving or variable sources.",
      coadd: "Coaddition Mode (Sum/Average)\nAverages all observed epochs to maximize signal-to-noise and reveal very faint sources."
    };
    [...$("modes").children].forEach((b) => {
      b.title = MODE_DESC[b.dataset.mode] || b.textContent;
      b.onclick = () => { state.mode = b.dataset.mode; reload(); };
    });
    $("fov").oninput = () => {
      state.fov = +$("fov").value;
      if ($("fovExact")) $("fovExact").value = state.fov;
      $("fovOut").textContent = `${state.fov}″ (${(state.fov / 60).toFixed(2)}′)`;
      syncUrl();
      applyVisualZoom(baseFov / state.fov);
    };
    $("fov").onchange = () => {
      clearTimeout(zoomDebounceTimer);
      reload({ zoomOnly: true });
    };
    if ($("fovExact")) {
      const applyExactFov = (reloadNow) => {
        const v = parseFloat($("fovExact").value);
        if (!isNaN(v) && v >= 10 && v <= 3600) {
          state.fov = Math.round(v * 100) / 100;
          $("fov").value = Math.min(1800, Math.max(30, state.fov));
          $("fovOut").textContent = `${state.fov}″ (${(state.fov / 60).toFixed(2)}′)`;
          syncUrl();
          applyVisualZoom(baseFov / state.fov);
          if (reloadNow) {
            clearTimeout(zoomDebounceTimer);
            reload({ zoomOnly: true });
          }
        }
      };
      $("fovExact").oninput = () => applyExactFov(false);
      $("fovExact").onchange = () => applyExactFov(true);
      $("fovExact").onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          $("fovExact").blur();
          applyExactFov(true);
        }
      };
    }
    if ($("fovPresets")) {
      [...$("fovPresets").children].forEach((b) => {
        b.onclick = () => {
          state.fov = +b.dataset.fov;
          paintControls();
          reload({ zoomOnly: true });
        };
      });
    }
    $("useCh").onchange = () => {
      const det = detOf(state.band);
      state.band = $("useCh").checked ? "ch" + ((det - 1) * 17 + 9) : "D" + det;
      reload();
    };
    $("ch").oninput = () => { state.band = "ch" + $("ch").value; paintControls(); };
    $("ch").onchange = reload;
    $("stretch").onchange = () => { state.stretch = $("stretch").value; reload(); };
    $("invert").onchange = () => { state.invert = $("invert").checked; reload(); };
    $("common").onchange = () => { state.common = $("common").checked; reload(); };
    $("cross").onchange = () => { state.cross = $("cross").checked; syncUrl(); drawOverlay(); };
    if ($("overlayGaia")) {
      $("overlayGaia").onchange = () => {
        state.gaia = $("overlayGaia").checked;
        syncUrl();
        updateGaiaUI();
        loadCatalogs();
      };
    }
    if ($("overlayWise")) {
      $("overlayWise").onchange = () => {
        state.wise = $("overlayWise").checked;
        syncUrl();
        updateWiseUI();
        loadCatalogs();
      };
    }
    if ($("stageGaia")) {
      $("stageGaia").onclick = (e) => {
        e.stopPropagation();
        state.gaia = !state.gaia;
        syncUrl();
        updateGaiaUI();
        loadCatalogs();
      };
    }
    if ($("stageWise")) {
      $("stageWise").onclick = (e) => {
        e.stopPropagation();
        state.wise = !state.wise;
        syncUrl();
        updateWiseUI();
        loadCatalogs();
      };
    }
    $("ref").onchange = () => { state.ref = +$("ref").value; reload(); };
    $("speed").oninput = () => { state.speed = +$("speed").value; paintControls(); syncUrl(); if (playing) play(); };
    $("apply").onclick = () => {
      let ra = parseFloat($("ra").value), dec = parseFloat($("dec").value);
      if (isNaN(ra) || isNaN(dec) || !(ra >= 0 && ra <= 360 && dec >= -90 && dec <= 90)) return setMsg("Invalid coordinates (RA 0–360°, Dec ±90°).", true);
      if (ra >= 360) ra = 0;
      if ($("fovExact")) {
        const f = parseFloat($("fovExact").value);
        if (!isNaN(f) && f >= 10 && f <= 3600) state.fov = Math.round(f * 100) / 100;
      }
      state.ra = ra; state.dec = dec; marker = null; reload();
    };
    $("share").onclick = async () => {
      try { await navigator.clipboard.writeText(location.href); $("share").textContent = "Link copied"; }
      catch { prompt("Copy this link:", location.href); }
      setTimeout(() => ($("share").textContent = "Copy link"), 1500);
    };
    $("exportGif").onclick = async () => {
      if (state.ra == null || !frames.length) {
        setMsg("Load a SPHEREx field first to export the GIF.", true);
        return;
      }
      const btn = $("exportGif");
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Exporting…";
      if (viewerLoader) viewerLoader.start("Generating GIF animation across epochs…");
      try {
        const q = new URLSearchParams({
          ra: state.ra,
          dec: state.dec,
          fov: Math.round(state.fov),
          band: state.band,
          mode: state.mode === "diff" ? "diff" : "blink",
          stretch: state.stretch,
          speed: state.speed
        });
        if (state.invert) q.set("invert", "true");
        if (state.mode === "diff") q.set("ref", state.ref);
        if (state.common && frames.length) {
          const vmins = frames.map((f) => f.meta && f.meta.vmin).filter((v) => typeof v === "number" && !isNaN(v));
          const vmaxs = frames.map((f) => f.meta && f.meta.vmax).filter((v) => typeof v === "number" && !isNaN(v));
          if (vmins.length && vmaxs.length) {
            q.set("vmin", Math.min(...vmins));
            q.set("vmax", Math.max(...vmaxs));
          }
        }
        const res = await fetch(`${API}/cutout/gif?${q}`);
        if (!res.ok) throw new Error(await errText(res));
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const signDec = state.dec >= 0 ? `+${state.dec.toFixed(4)}` : state.dec.toFixed(4);
        a.download = `spherex_${state.band}_ra${state.ra.toFixed(4)}_dec${signDec}_fov${Math.round(state.fov)}s.gif`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        if (viewerLoader) viewerLoader.done("Animated GIF successfully downloaded!");
      } catch (e) {
        if (viewerLoader) viewerLoader.fail("Error exporting GIF: " + e.message);
        else setMsg("Error exporting GIF: " + e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    };
    $("search").onsubmit = async (ev) => {
      ev.preventDefault();
      const q = $("q").value.trim();
      if (!q) return;
      setMsg("Resolving «" + q + "»…");
      try {
        const r = await getJson(`${API}/targets/search?q=${encodeURIComponent(q)}`);
        if (!r.results.length) return setMsg("Could not find «" + q + "».", true);
        const t = r.results[0];
        state.ra = t.ra; state.dec = t.dec; marker = null; targetName = t.name || q;
        reload();
      } catch (e) { setMsg(e.message, true); }
    };
    $("prev").onclick = () => { pause(); show(idx - 1); };
    $("next").onclick = () => { pause(); show(idx + 1); };
    $("play").onclick = () => (playing ? pause() : play());
    $("scrub").oninput = () => { pause(); show(+$("scrub").value); };
    document.addEventListener("keydown", (ev) => {
      if (/INPUT|SELECT|TEXTAREA/.test(ev.target.tagName)) return;
      if (ev.code === "Space") { ev.preventDefault(); playing ? pause() : play(); }
      if (ev.code === "ArrowLeft") { pause(); show(idx - 1); }
      if (ev.code === "ArrowRight") { pause(); show(idx + 1); }
      if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); changeZoom(-1); }
      if (ev.key === "-" || ev.key === "_") { ev.preventDefault(); changeZoom(1); }
    });
    const stage = $("stage");
    let isTouching = false;
    let isDragging = false;
    let isPinching = false;
    let touchStartX = 0, touchStartY = 0;
    let startPanX = 0, startPanY = 0;
    let initialPinchDist = 0;
    let initialPinchFov = 0;
    let lastTapTime = 0;
    let lastTapPos = { x: 0, y: 0 };
    let dragDebounceTimer = null;
    let suppressClickUntil = 0;

    // Búsqueda de fuentes cercanas de catálogos (Gaia DR3 y AllWISE) para inspección
    function findNearCatalogSource(clientX, clientY, threshold = 14) {
      if (!grid.size || (!state.gaia && !state.wise)) return null;
      const r = stage.getBoundingClientRect();
      const mx = clientX - r.left;
      const my = clientY - r.top;
      let bestDist = threshold;
      let found = null;

      if (state.gaia && gaiaSources.length) {
        for (const src of gaiaSources) {
          const p = pixelOf(src.ra, src.dec);
          const sx = p.x * r.width + panOffset.x;
          const sy = p.y * r.height + panOffset.y;
          const dist = Math.hypot(mx - sx, my - sy);
          if (dist < bestDist) {
            bestDist = dist;
            found = { type: "gaia", source: src, x: sx, y: sy };
          }
        }
      }
      if (state.wise && wiseSources.length) {
        for (const src of wiseSources) {
          const p = pixelOf(src.ra, src.dec);
          const sx = p.x * r.width + panOffset.x;
          const sy = p.y * r.height + panOffset.y;
          const dist = Math.hypot(mx - sx, my - sy);
          if (dist < bestDist) {
            bestDist = dist;
            found = { type: "wise", source: src, x: sx, y: sy };
          }
        }
      }
      return found;
    }

    function renderAstroTooltip(item) {
      const tooltipEl = $("tooltip");
      if (!tooltipEl) return;
      if (!item) {
        tooltipEl.hidden = true;
        return;
      }
      tooltipEl.hidden = false;
      tooltipEl.style.left = `${Math.min(stage.clientWidth - 260, Math.max(10, item.x + 12))}px`;
      tooltipEl.style.top = `${Math.min(stage.clientHeight - 95, Math.max(20, item.y))}px`;
      if (item.type === "gaia") {
        const src = item.source;
        const isFast = src.pm_total != null && src.pm_total > 120;
        tooltipEl.innerHTML = `<span style="color:#c084fc;font-weight:600">✦ Gaia DR3</span> <span class="muted">${src.id.slice(-8)}</span><br>` +
          `G: <b>${src.gmag != null ? src.gmag.toFixed(2) : "—"}</b> mag · BP−RP: <b>${src.bp_rp != null ? (src.bp_rp >= 0 ? "+" : "") + src.bp_rp.toFixed(2) : "—"}</b><br>` +
          `PM: <b${isFast ? ' style="color:#f43f5e"' : ''}>${src.pm_total != null ? src.pm_total.toFixed(1) + " mas/yr" + (isFast ? " ★ (fast)" : "") : "—"}</b>` +
          (src.pmra != null ? ` <span class="muted">(${src.pmra}, ${src.pmdec})</span>` : "") +
          (src.parallax != null ? `<br>Parallax: <b>${src.parallax.toFixed(2)} mas</b>` : "");
      } else {
        const src = item.source;
        const redClass = src.w1_w2 != null && src.w1_w2 > 1.2 ? ` style="color:#ff4081;font-weight:bold"` : "";
        tooltipEl.innerHTML = `<span style="color:#fb923c;font-weight:600">■ AllWISE</span> <span class="muted">${src.id}</span><br>` +
          `W1 (3.4 µm): <b>${src.w1 != null ? src.w1.toFixed(2) : "—"}</b> mag<br>` +
          `W2 (4.6 µm): <b>${src.w2 != null ? src.w2.toFixed(2) : "—"}</b> mag` +
          (src.w1_w2 != null ? `<br><span${redClass}>W1−W2: <b>${src.w1_w2 >= 0 ? "+" : ""}${src.w1_w2.toFixed(2)}</b>${src.w1_w2 > 1.2 ? " ★ (cold candidate)" : ""}</span>` : "");
      }
    }

    // Ratón en desktop: arrastrar para desplazar
    let isMouseDown = false;
    let mouseStartX = 0, mouseStartY = 0;
    let mouseMoved = false;

    stage.addEventListener("mousedown", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;
      if (ev.button !== 0) return;
      if (performance.now() < suppressClickUntil) return;
      const img = $("img");
      if (img) img.classList.remove("panning");
      isMouseDown = true;
      mouseMoved = false;
      mouseStartX = ev.clientX;
      mouseStartY = ev.clientY;
      startPanX = panOffset.x;
      startPanY = panOffset.y;
    });

    stage.addEventListener("mousemove", (ev) => {
      const s = skyAt(ev);
      $("cursor").textContent = s ? `RA ${s.ra.toFixed(5)}°  Dec ${s.dec >= 0 ? "+" : ""}${s.dec.toFixed(5)}°` : "—";
      const tooltipEl = $("tooltip");

      if (isMouseDown) {
        if (tooltipEl) tooltipEl.hidden = true;
        const dx = ev.clientX - mouseStartX;
        const dy = ev.clientY - mouseStartY;
        if (!mouseMoved && Math.hypot(dx, dy) > 5) {
          mouseMoved = true;
          abortSpectrum();
          stage.style.cursor = "grabbing";
        }
        if (mouseMoved) {
          panOffset.x = startPanX + dx;
          panOffset.y = startPanY + dy;
          applyVisualTransform();
        }
        return;
      }

      // Inspección de fuentes de Gaia y WISE con tooltip astrométrico
      const hovered = findNearCatalogSource(ev.clientX, ev.clientY);
      renderAstroTooltip(hovered);
    });

    const finishMouseDrag = () => {
      const tip = $("tooltip");
      if (tip) tip.hidden = true;
      if (!isMouseDown) return;
      isMouseDown = false;
      stage.style.cursor = "crosshair";
      if (mouseMoved) {
        suppressClickUntil = performance.now() + 350;
        const r = stage.getBoundingClientRect();
        const centerEv = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
        const newCenter = skyAt(centerEv);
        if (newCenter) {
          state.ra = +newCenter.ra.toFixed(5);
          state.dec = +newCenter.dec.toFixed(5);
          paintControls();
          syncUrl();
          clearTimeout(dragDebounceTimer);
          dragDebounceTimer = setTimeout(() => reload({ recenterOnly: true }), 180);
        }
      }
    };
    stage.addEventListener("mouseup", finishMouseDrag);
    stage.addEventListener("mouseleave", finishMouseDrag);

    // Pantallas táctiles (móvil)
    stage.addEventListener("touchstart", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;
      const img = $("img");
      if (img) img.classList.remove("panning");

      if (ev.touches.length === 2) {
        isPinching = true;
        isDragging = false;
        isTouching = false;
        abortSpectrum();
        initialPinchDist = Math.hypot(
          ev.touches[0].clientX - ev.touches[1].clientX,
          ev.touches[0].clientY - ev.touches[1].clientY
        );
        initialPinchFov = state.fov;
        startPanX = panOffset.x;
        startPanY = panOffset.y;
        return;
      }
      if (ev.touches.length === 1) {
        isTouching = true;
        isDragging = false;
        isPinching = false;
        touchStartX = ev.touches[0].clientX;
        touchStartY = ev.touches[0].clientY;
        startPanX = panOffset.x;
        startPanY = panOffset.y;
      }
    }, { passive: false });

    stage.addEventListener("touchmove", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;

      if (isPinching && ev.touches.length >= 2) {
        ev.preventDefault();
        abortSpectrum();
        const dist = Math.hypot(
          ev.touches[0].clientX - ev.touches[1].clientX,
          ev.touches[0].clientY - ev.touches[1].clientY
        );
        if (dist > 10 && initialPinchDist > 0) {
          const factor = initialPinchDist / dist;
          const nextFov = Math.max(30, Math.min(1800, Math.round(initialPinchFov * factor)));
          if (nextFov !== state.fov) {
            state.fov = nextFov;
            paintControls();
            applyVisualTransform();
          }
        }
        return;
      }
      if (isTouching && ev.touches.length === 1) {
        const dx = ev.touches[0].clientX - touchStartX;
        const dy = ev.touches[0].clientY - touchStartY;
        const dist = Math.hypot(dx, dy);
        if (!isDragging && dist > 7) {
          isDragging = true;
          abortSpectrum();
        }
        if (isDragging) {
          ev.preventDefault();
          panOffset.x = startPanX + dx;
          panOffset.y = startPanY + dy;
          applyVisualTransform();
        }
      }
    }, { passive: false });

    stage.addEventListener("touchend", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;

      if (isPinching) {
        if (ev.touches.length < 2) {
          isPinching = false;
          suppressClickUntil = performance.now() + 350;
          syncUrl();
          clearTimeout(zoomDebounceTimer);
          zoomDebounceTimer = setTimeout(() => reload({ zoomOnly: true }), 180);
        }
        return;
      }
      if (isDragging) {
        isDragging = false;
        isTouching = false;
        suppressClickUntil = performance.now() + 350;
        const r = stage.getBoundingClientRect();
        const centerEv = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
        const newCenter = skyAt(centerEv);
        if (newCenter) {
          state.ra = +newCenter.ra.toFixed(5);
          state.dec = +newCenter.dec.toFixed(5);
          paintControls();
          syncUrl();
          clearTimeout(dragDebounceTimer);
          dragDebounceTimer = setTimeout(() => reload({ recenterOnly: true }), 180);
        }
        return;
      }
      if (isTouching) {
        isTouching = false;
        const touch = ev.changedTouches[0];
        if (!touch) return;
        const now = performance.now();
        const tapDist = Math.hypot(touch.clientX - lastTapPos.x, touch.clientY - lastTapPos.y);

        if (now - lastTapTime < 320 && tapDist < 30) {
          // Doble toque: centrado suave
          abortSpectrum();
          lastTapTime = 0;
          suppressClickUntil = performance.now() + 350;
          const s = skyAt({ clientX: touch.clientX, clientY: touch.clientY });
          if (s) {
            const r = stage.getBoundingClientRect();
            const clickX = touch.clientX - r.left;
            const clickY = touch.clientY - r.top;
            const targetTx = r.width / 2 - clickX;
            const targetTy = r.height / 2 - clickY;
            state.ra = +s.ra.toFixed(5);
            state.dec = +s.dec.toFixed(5);
            marker = s;
            paintControls();
            syncUrl();
            animateRecenter(targetTx, targetTy);
            reload({ recenterOnly: true });
          }
        } else {
          // Toque simple: seleccionar estrella para 102 bandas
          lastTapTime = now;
          lastTapPos = { x: touch.clientX, y: touch.clientY };
          const s = skyAt({ clientX: touch.clientX, clientY: touch.clientY });
          if (s) {
            suppressClickUntil = performance.now() + 350;
            abortSpectrum();
            clickTimer = setTimeout(() => {
              marker = s;
              drawOverlay();
              loadSpectrum(s.ra, s.dec);
            }, 240);
          }
        }
      }
    }, { passive: false });

    let tooltipTimer = null;
    stage.addEventListener("click", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;
      if (performance.now() < suppressClickUntil) return;
      const nearCat = findNearCatalogSource(ev.clientX, ev.clientY, 16);
      if (nearCat) {
        renderAstroTooltip(nearCat);
        clearTimeout(tooltipTimer);
        tooltipTimer = setTimeout(() => { if ($("tooltip")) $("tooltip").hidden = true; }, 4000);
      }
      let s = skyAt(ev);
      if (!s) return;
      if (nearCat) {
        s = { ra: nearCat.source.ra, dec: nearCat.source.dec };
      }
      abortSpectrum();
      clickTimer = setTimeout(() => {
        marker = s;
        drawOverlay();
        loadSpectrum(s.ra, s.dec);
      }, 240);
    });

    stage.addEventListener("dblclick", (ev) => {
      if (ev.target.closest(".stage-zoom, .stage-overlays, .buffer-wrap")) return;
      abortSpectrum();
      suppressClickUntil = performance.now() + 350;
      const s = skyAt(ev);
      if (!s) return;
      const r = stage.getBoundingClientRect();
      const clickX = ev.clientX - r.left;
      const clickY = ev.clientY - r.top;
      const targetTx = r.width / 2 - clickX;
      const targetTy = r.height / 2 - clickY;

      state.ra = +s.ra.toFixed(5);
      state.dec = +s.dec.toFixed(5);
      marker = s;
      paintControls();
      syncUrl();
      animateRecenter(targetTx, targetTy);
      reload({ recenterOnly: true });
    });

    stage.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      abortSpectrum();
      changeZoom(Math.sign(ev.deltaY));
    }, { passive: false });

    $("zoomIn").onclick = (e) => {
      e.stopPropagation();
      abortSpectrum();
      changeZoom(-1);
    };
    $("zoomOut").onclick = (e) => {
      e.stopPropagation();
      abortSpectrum();
      changeZoom(1);
    };

    new ResizeObserver(drawOverlay).observe(stage);
  }

  // ------------------------------------------------------------------ red
  async function getJson(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await errText(r));
    return r.json();
  }
  async function errText(r) {
    try { const j = await r.json(); return j.detail ? String(j.detail) : `Error ${r.status}`; }
    catch { return `Error ${r.status}`; }
  }
  function frameUrl(kind, extra) {
    const q = new URLSearchParams({ ra: state.ra, dec: state.dec, fov: state.fov, band: state.band, stretch: state.stretch });
    if (state.invert) q.set("invert", "true");
    Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v));
    return `${API}/cutout/${kind}?${q}`;
  }
  async function fetchFrame(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await errText(r));
    const h = (k) => r.headers.get("X-Spherex-" + k);
    const blob = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = blobUrl;
      if (img.decode) await img.decode();
    } catch { /* decodificacion diferida */ }
    return {
      url: blobUrl,
      meta: { vmin: +h("Vmin"), vmax: +h("Vmax"), n: +h("N"), date: h("Date"), size: +h("Size"), scale: +h("Scale"), wl: h("Wavelength") },
    };
  }
  async function mapLimit(items, limit, fn) {
    const out = new Array(items.length);
    let i = 0;
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (i < items.length) { const k = i++; out[k] = await fn(items[k], k).catch((e) => ({ error: e })); }
    }));
    return out;
  }

  // ------------------------------------------------------------------ carga principal
  let lastLoadedCenter = { ra: null, dec: null };
  async function reload(opts = {}) {
    const hasExisting = frames.length > 0;
    const isZoomOnly = Boolean(opts && opts.zoomOnly && hasExisting);
    const isRecenterOnly = Boolean(opts && opts.recenterOnly && hasExisting);
    const keepExisting = hasExisting;
    cancelPrefetch();
    paintControls(); syncUrl();
    if (state.ra == null) return;
    const token = ++loadToken;
    if (state.gaia || state.wise) loadCatalogs();

    const isSameCenter = lastLoadedCenter.ra != null &&
      Math.abs(lastLoadedCenter.ra - state.ra) < 1e-4 &&
      Math.abs(lastLoadedCenter.dec - state.dec) < 1e-4;

    if (!keepExisting) {
      panOffset = { x: 0, y: 0 };
      baseFov = state.fov;
      applyVisualTransform();
      pause(true);
      frames.forEach((f) => { if (f && f.url) URL.revokeObjectURL(f.url); });
      frames = []; $("img").removeAttribute("src"); $("frameLabel").textContent = ""; $("frameMeta").textContent = "";
      if (viewerLoader) viewerLoader.start("Querying SPHEREx archive…");
    } else {
      let msg = "Querying SPHEREx archive…";
      if (isRecenterOnly) msg = "Centering on new coordinates…";
      else if (isZoomOnly) msg = "Adjusting astrometric sharpness…";
      else if (state.mode === "diff") msg = "Calculating epoch differences…";
      else if (state.mode === "coadd") msg = "Generating deep coaddition…";
      else if (state.band) msg = `Loading band ${state.band}…`;
      if (viewerLoader) viewerLoader.start(msg);
    }

    try {
      if (!isZoomOnly || !epochs || !epochs.length) {
        const obs = await getJson(`${API}/observations?ra=${state.ra}&dec=${state.dec}`);
        if (token !== loadToken) return;
        epochs = obs.epochs;
        const det = "D" + detOf(state.band);
        [...$("bands").children].forEach((b) => (b.disabled = !(obs.detectors["D" + b.dataset.det] > 0)));
      }
      const det = "D" + detOf(state.band);
      const usable = epochs.filter((e) => e.detectors[det] > 0);
      paintEpochs(det);
      if (!usable.length) {
        if (viewerLoader) viewerLoader.fail(`No images in ${det} for this position.`);
        return setMsg(`No images in ${det} for this position. Try another detector.`, true);
      }

      let jobs;
      if (state.mode === "coadd") {
        jobs = [{ label: `Coaddition · ${usable.length} epochs`, kind: "coadd", extra: {} }];
      } else if (state.mode === "diff") {
        if (usable.length < 2) return setMsg("More than one epoch needed for difference mode.", true);
        if (!usable.some((e) => e.epoch === state.ref)) state.ref = usable[0].epoch;
        paintRef(usable);
        jobs = usable.filter((e) => e.epoch !== state.ref)
          .map((e) => ({ label: `${e.date} − ref`, kind: "frame", epoch: e.epoch, extra: { epoch: e.epoch, diff: "true", ref: state.ref } }));
      } else {
        jobs = usable.map((e) => ({ label: e.date, kind: "frame", epoch: e.epoch, extra: { epoch: e.epoch } }));
      }
      syncUrl();

      // ---- progressive load: low res first, high res later in background
      let done = 0, failed = 0, showing = keepExisting;
      const progress = () => setMsg(done < jobs.length - failed
        ? `Epochs ready: ${done}/${jobs.length - failed} (first load takes a moment; cached afterward)` : "");
      if (!keepExisting) progress();

      const byIdx = new Map();   // indice del job -> fotograma
      const rebuild = () => {
        frames = jobs.map((j, k) => {
          const f = byIdx.get(k);
          return f ? { ...j, idx0: k, ...f } : null;
        }).filter(Boolean);
        if (!frames.length) return;
        $("scrub").max = frames.length - 1;
      };
      const applyFrame = (i, f) => {
        if (token !== loadToken) { if (f && f.url) URL.revokeObjectURL(f.url); return; }
        if (f.error) failed++; else byIdx.set(i, f);
        done++;
        rebuild();
        progress();
        if (!showing) {
          showing = true;
          show(0);
          if (frames.length > 1) play(); else pause(true);
        } else if (frames.length > 1 && playing) {
          play();   // rearma el temporizador al llegar mas epocas
        }
      };
      const replaceFrame = (i, g) => {
        if (token !== loadToken) { if (g && g.url) URL.revokeObjectURL(g.url); return; }
        const old = byIdx.get(i);
        byIdx.set(i, g);
        rebuild();
        if (frames[idx] && frames[idx].idx0 === i) show(idx);
        if (old && old.url) {
          setTimeout(() => URL.revokeObjectURL(old.url), 120);
        }
      };

      // fase 1: baja resolucion (carga rapida para respuesta inmediata)
      const newByIdx = new Map();
      let newFailed = 0;
      await mapLimit(jobs, 4, async (j, i) => {
        try {
          const f = await fetchFrame(frameUrl(j.kind, { ...j.extra, res: "low" }));
          newByIdx.set(i, f);
          if (viewerLoader && keepExisting) {
            viewerLoader.step(20 + Math.round((newByIdx.size / jobs.length) * 60));
          }
          if (!keepExisting) {
            applyFrame(i, f);
          }
        } catch (e) {
          if (!keepExisting) applyFrame(i, { error: e }); else newFailed++;
        }
      });
      if (token !== loadToken) {
        newByIdx.forEach((f) => { if (f && f.url) URL.revokeObjectURL(f.url); });
        return;
      }

      if (keepExisting) {
        if (!newByIdx.size) {
          if (viewerLoader) viewerLoader.fail("No valid data for this selection.");
          return;
        }
        const oldFrames = frames;
        byIdx.clear();
        newByIdx.forEach((v, k) => byIdx.set(k, v));
        failed = newFailed;
        rebuild();
        if (isZoomOnly || isRecenterOnly || !isSameCenter) {
          panOffset = { x: 0, y: 0 };
        }
        baseFov = state.fov;
        if (frames.length) {
          show(Math.min(idx, frames.length - 1));
          if (frames.length > 1 && playing) play(); else pause(true);
        }
        applyVisualTransform();
        lastLoadedCenter = { ra: state.ra, dec: state.dec };
        setTimeout(() => {
          oldFrames.forEach((f) => { if (f && f.url) URL.revokeObjectURL(f.url); });
        }, 150);
        if (viewerLoader) viewerLoader.step(82, "Frames ready · Maximizing resolution…");
      } else {
        lastLoadedCenter = { ra: state.ra, dec: state.dec };
        if (viewerLoader) viewerLoader.step(82, "Frames ready · Maximizing resolution…");
      }

      const validJobs = [...byIdx.entries()].sort((a, b) => a[0] - b[0]).map(([k, f]) => ({ idx0: k, f, job: jobs[k] }));
      if (!validJobs.length) {
        if (viewerLoader) viewerLoader.fail("No valid data in this band for this position.");
        return;
      }

      // common greyscale for all epochs (applied when requesting high res)
      let lim = {};
      if (state.common && validJobs.length > 1) {
        lim = { vmin: Math.min(...validJobs.map((x) => x.f.meta.vmin)), vmax: Math.max(...validJobs.map((x) => x.f.meta.vmax)) };
      }

      // phase 2: high resolution with concurrency limit (2 threads)
      mapLimit(validJobs, 2, async (x) => {
        if (token !== loadToken) return;
        try {
          replaceFrame(x.idx0, await fetchFrame(frameUrl(x.job.kind, { ...x.job.extra, ...lim })));
        } catch { /* keep low res */ }
      }).then(() => {
        if (token === loadToken) {
          if (viewerLoader) {
            viewerLoader.done(failed ? `${failed} epoch(s) without valid data.` : "");
          }
          schedulePrefetch(state.ra, state.dec, state.fov, det);
        }
      });
    } catch (e) {
      if (token === loadToken) {
        if (viewerLoader) viewerLoader.fail(e.message); else setMsg(e.message, true);
      }
    }
  }

  let prefetchTimer = null;
  let prefetchAbort = null;

  function cancelPrefetch() {
    if (prefetchTimer) {
      clearTimeout(prefetchTimer);
      prefetchTimer = null;
    }
    if (prefetchAbort) {
      prefetchAbort.abort();
      prefetchAbort = null;
    }
  }

  function schedulePrefetch(ra, dec, fov, currentDet) {
    cancelPrefetch();
    prefetchTimer = setTimeout(async () => {
      if (state.ra !== ra || state.dec !== dec || state.fov !== fov) return;
      prefetchAbort = new AbortController();
      const signal = prefetchAbort.signal;

      try {
        // 1. Precalentar siguiente nivel de zoom hacia adentro (el paso mas habitual)
        const fovIn = Math.max(60, fov - (fov <= 120 ? 30 : 60));
        if (fovIn !== fov && !signal.aborted) {
          await fetch(`${API}/cutout/prewarm?ra=${ra}&dec=${dec}&fov=${fovIn}&band=${currentDet}`, { signal });
        }
        if (signal.aborted) return;

        // 2. Precalentar banda contigua mas cercana (ej: D1 -> D2; D2 -> D3; D6 -> D5)
        const detNum = parseInt(currentDet.replace(/\D/g, ""), 10) || 1;
        const nextDet = detNum < 6 ? `D${detNum + 1}` : `D${detNum - 1}`;
        if (!signal.aborted) {
          await fetch(`${API}/cutout/prewarm?ra=${ra}&dec=${dec}&fov=${fov}&band=${nextDet}`, { signal });
        }
      } catch {
        // Cancelacion intencionada o fallo silencioso
      }
    }, 2500);
  }

  function paintEpochs(det) {
    const ol = $("epochs"); ol.textContent = "";
    const nUse = epochs.filter((e) => (e.detectors[det] || 0) > 0).length;
    if ($("epochCount")) $("epochCount").textContent = `${nUse}/${epochs.length} with images`;
    epochs.forEach((e) => {
      const li = document.createElement("li");
      const n = e.detectors[det] || 0;
      li.dataset.epoch = e.epoch;
      li.className = n ? "" : "off";
      li.innerHTML = `<span>${e.date_start} → ${e.date_end}</span><span>${n} img</span>`;
      if (n) li.onclick = () => { const k = frames.findIndex((f) => f.epoch === e.epoch); if (k >= 0) { pause(); show(k); } };
      ol.appendChild(li);
    });
  }
  function paintRef(usable) {
    const s = $("ref"); s.textContent = "";
    usable.forEach((e) => { const o = document.createElement("option"); o.value = e.epoch; o.textContent = e.date; s.appendChild(o); });
    s.value = state.ref;
  }

  // ------------------------------------------------------------------ player
  function show(k) {
    if (!frames.length) return;
    idx = (k + frames.length) % frames.length;
    const f = frames[idx];
    grid = { size: f.meta.size, scale: f.meta.scale };
    $("img").src = f.url;
    $("scrub").value = idx;
    $("frameLabel").textContent = `${idx + 1}/${frames.length} · ${f.label}`;
    const fovExactSec = (f.meta.size * f.meta.scale).toFixed(1);
    const fovExactMin = (fovExactSec / 60).toFixed(2);
    $("frameMeta").textContent = `${f.meta.n} exp · Exact FOV: ${fovExactSec}″ (${fovExactMin}′)` + (f.meta.wl ? ` · λ≈${(+f.meta.wl).toFixed(2)} µm` : "") + ` · ${f.meta.scale}″/px`;
    [...$("epochs").children].forEach((li) => li.classList.toggle("on", +li.dataset.epoch === f.epoch));
    drawOverlay();
  }
  function play() {
    clearInterval(timer);
    playing = true; $("play").textContent = "❚❚";
    if (frames.length > 1) timer = setInterval(() => show(idx + 1), state.speed);
  }
  function pause(silent) {
    clearInterval(timer); timer = null;
    if (!silent) { playing = false; $("play").textContent = "▶"; }
  }

  // ------------------------------------------------------------------ geometria (TAN)
  const D2R = Math.PI / 180;
  function skyAt(ev) {
    if (!grid.size || state.ra == null || !ev) return null;
    const r = $("stage").getBoundingClientRect();
    const zoomRatio = (baseFov && state.fov) ? (baseFov / state.fov) : 1;
    const cx = ev.clientX != null ? ev.clientX : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : (ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0].clientX : 0));
    const cy = ev.clientY != null ? ev.clientY : (ev.touches && ev.touches[0] ? ev.touches[0].clientY : (ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0].clientY : 0));
    const fx = ((cx - r.left - panOffset.x) / r.width - 0.5) / zoomRatio;
    const fy = ((cy - r.top - panOffset.y) / r.height - 0.5) / zoomRatio;
    const span = grid.size * grid.scale / 3600 * D2R;       // ancho total en radianes
    const xi = -fx * span, eta = -fy * span;                // este a la izquierda, norte arriba
    const rho = Math.hypot(xi, eta), d0 = state.dec * D2R;
    if (rho === 0) return { ra: state.ra, dec: state.dec };
    const c = Math.atan(rho);
    const dec = Math.asin(Math.cos(c) * Math.sin(d0) + eta * Math.sin(c) * Math.cos(d0) / rho);
    const ra = state.ra * D2R + Math.atan2(xi * Math.sin(c), rho * Math.cos(d0) * Math.cos(c) - eta * Math.sin(d0) * Math.sin(c));
    return { ra: ((ra / D2R) % 360 + 360) % 360, dec: dec / D2R };
  }
  function pixelOf(ra, dec) {   // inversa: cielo -> fraccion [0,1] del visor
    const a = (ra - state.ra) * D2R, d = dec * D2R, d0 = state.dec * D2R;
    const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a);
    const xi = Math.cos(d) * Math.sin(a) / cosc, eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a)) / cosc;
    const span = grid.size * grid.scale / 3600 * D2R;
    const zoomRatio = (baseFov && state.fov) ? (baseFov / state.fov) : 1;
    return { x: 0.5 - (xi / span) * zoomRatio, y: 0.5 - (eta / span) * zoomRatio };
  }
  function drawOverlay() {
    const c = $("overlay"), r = $("stage").getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    c.width = Math.round(r.width * dpr); c.height = Math.round(r.height * dpr);
    const g = c.getContext("2d"); g.scale(dpr, dpr);
    const w = r.width, h = r.height;
    if (state.cross && frames.length) {
      g.strokeStyle = "rgba(94,200,242,.9)"; g.lineWidth = 1;
      const cx = w / 2, cy = h / 2, a = 8, b = 22;
      g.beginPath();
      [[-b, 0, -a, 0], [a, 0, b, 0], [0, -b, 0, -a], [0, a, 0, b]].forEach(([x1, y1, x2, y2]) => { g.moveTo(cx + x1, cy + y1); g.lineTo(cx + x2, cy + y2); });
      g.stroke();
    }
    if (marker && grid.size) {
      const p = pixelOf(marker.ra, marker.dec);
      g.strokeStyle = "#7fd1b9"; g.lineWidth = 1.5;
      g.beginPath(); g.arc(p.x * w + panOffset.x, p.y * h + panOffset.y, 9, 0, 2 * Math.PI); g.stroke();
    }

    // Superposición Gaia DR3 (estrellas, magnitudes y vectores de movimiento propio)
    if (state.gaia && gaiaSources.length && grid.size) {
      for (const s of gaiaSources) {
        const p = pixelOf(s.ra, s.dec);
        const sx = p.x * w + panOffset.x;
        const sy = p.y * h + panOffset.y;
        if (sx < -40 || sx > w + 40 || sy < -40 || sy > h + 40) continue;

        const gmag = s.gmag != null ? s.gmag : 15;
        const rad = Math.max(2.5, Math.min(6.5, (17 - gmag) * 0.45 + 2.5));
        const pmTot = s.pm_total != null ? s.pm_total : (s.pmra != null && s.pmdec != null ? Math.hypot(s.pmra, s.pmdec) : null);
        const isFast = pmTot != null && pmTot > 120; // Estrella rápida / candidato Planeta X / enana marrón

        // Marcador estelar de Gaia (anillo cian/rosa + punto astrométrico central)
        g.strokeStyle = isFast ? "#f43f5e" : "#c084fc";
        g.lineWidth = isFast ? 1.6 : 1.2;
        g.beginPath();
        g.arc(sx, sy, rad, 0, 2 * Math.PI);
        g.stroke();
        g.fillStyle = isFast ? "#f43f5e" : "#c084fc";
        g.fillRect(sx - 0.75, sy - 0.75, 1.5, 1.5);

        // Vector de movimiento propio (PM) proyectado exactamente en la proyección TAN
        if (s.pmra != null && s.pmdec != null && pmTot != null && pmTot > 15) {
          const dt = 25; // Intervalo de proyección temporal en años
          const cosDec = Math.cos(s.dec * D2R);
          const dra = (s.pmra * dt / 1000) / (3600 * (Math.abs(cosDec) > 1e-6 ? cosDec : 1));
          const ddec = (s.pmdec * dt / 1000) / 3600;
          const p1 = pixelOf(s.ra + dra, s.dec + ddec);
          let vx = (p1.x - p.x) * w;
          let vy = (p1.y - p.y) * h;
          let vlen = Math.hypot(vx, vy);

          // Control estético de longitud mínima/máxima del vector
          if (vlen > 70) { vx = (vx / vlen) * 70; vy = (vy / vlen) * 70; vlen = 70; }
          if (vlen < 7 && pmTot > 25) { vx = (vx / vlen) * 7; vy = (vy / vlen) * 7; vlen = 7; }

          g.strokeStyle = isFast ? "#f43f5e" : "rgba(192, 132, 252, 0.9)";
          g.lineWidth = isFast ? 2.0 : 1.3;
          g.beginPath();
          g.moveTo(sx, sy);
          g.lineTo(sx + vx, sy + vy);
          g.stroke();

          // Punta de flecha astrométrica orientada hacia la dirección de avance
          const ang = Math.atan2(vy, vx);
          const head = isFast ? 6 : 4.5;
          g.fillStyle = isFast ? "#f43f5e" : "rgba(192, 132, 252, 0.9)";
          g.beginPath();
          g.moveTo(sx + vx, sy + vy);
          g.lineTo(sx + vx - head * Math.cos(ang - Math.PI / 6), sy + vy - head * Math.sin(ang - Math.PI / 6));
          g.lineTo(sx + vx - head * Math.cos(ang + Math.PI / 6), sy + vy - head * Math.sin(ang + Math.PI / 6));
          g.closePath();
          g.fill();
        }

        // Etiqueta de magnitud G y/o PM para estrellas destacadas o rápidas
        if (isFast) {
          g.fillStyle = "#f43f5e";
          g.font = "bold 9px ui-monospace, monospace";
          g.textAlign = "left";
          g.textBaseline = "middle";
          const gText = s.gmag != null ? `G${s.gmag.toFixed(1)} ` : "";
          g.fillText(`${gText}PM:${pmTot.toFixed(0)}★`, sx + rad + 3, sy - 2);
        } else if (s.gmag != null && (s.gmag < 15.0 || gaiaSources.length <= 30)) {
          g.fillStyle = "rgba(192, 132, 252, 0.9)";
          g.font = "9px ui-monospace, monospace";
          g.textAlign = "left";
          g.textBaseline = "middle";
          g.fillText("G" + s.gmag.toFixed(1), sx + rad + 3, sy - 2);
        }
      }
    }

    // Superposición AllWISE (detecciones infrarrojas W1 3.4 µm, W2 4.6 µm, retículas)
    if (state.wise && wiseSources.length && grid.size) {
      for (const s of wiseSources) {
        const p = pixelOf(s.ra, s.dec);
        const sx = p.x * w + panOffset.x;
        const sy = p.y * h + panOffset.y;
        if (sx < -40 || sx > w + 40 || sy < -40 || sy > h + 40) continue;

        const w1 = s.w1 != null ? s.w1 : 14;
        const boxRad = Math.max(3.5, Math.min(7.5, (16 - w1) * 0.5 + 3.5));
        const isUltraRed = s.w1_w2 != null && s.w1_w2 > 1.2; // Firma térmica de enana Y o Planeta X

        g.strokeStyle = isUltraRed ? "#ff4081" : "#fb923c";
        g.lineWidth = isUltraRed ? 1.8 : 1.3;

        // Retícula cuadrada con mira de comparación infrarroja
        g.strokeRect(sx - boxRad, sy - boxRad, boxRad * 2, boxRad * 2);
        g.beginPath();
        g.moveTo(sx - boxRad - 2, sy); g.lineTo(sx - boxRad, sy);
        g.moveTo(sx + boxRad, sy); g.lineTo(sx + boxRad + 2, sy);
        g.moveTo(sx, sy - boxRad - 2); g.lineTo(sx, sy - boxRad);
        g.moveTo(sx, sy + boxRad); g.lineTo(sx, sy + boxRad + 2);
        g.stroke();

        // Etiquetas infrarrojas W1 / W2 / W1-W2
        if (isUltraRed) {
          g.fillStyle = "#ff4081";
          g.font = "bold 9px ui-monospace, monospace";
          g.textAlign = "left";
          g.textBaseline = "middle";
          const signStr = s.w1_w2 >= 0 ? "+" : "";
          const w1Str = s.w1 != null ? `W1:${s.w1.toFixed(1)} ` : "";
          g.fillText(`${w1Str}W1-W2:${signStr}${s.w1_w2.toFixed(1)}★`, sx + boxRad + 3, sy + 2);
        } else if (s.w1 != null || s.w2 != null) {
          if ((s.w1 != null && s.w1 < 14.5) || wiseSources.length <= 30) {
            g.fillStyle = "rgba(251, 146, 60, 0.9)";
            g.font = "9px ui-monospace, monospace";
            g.textAlign = "left";
            g.textBaseline = "middle";
            let label = "";
            if (s.w1 != null && s.w2 != null) label = `W1:${s.w1.toFixed(1)} W2:${s.w2.toFixed(1)}`;
            else if (s.w1 != null) label = `W1:${s.w1.toFixed(1)}`;
            else label = `W2:${s.w2.toFixed(1)}`;
            g.fillText(label, sx + boxRad + 3, sy + 2);
          }
        }
      }
    }
  }

  // ------------------------------------------------------------------ espectro
  async function loadSpectrum(ra, dec) {
    abortSpectrum();
    syncUrl();
    if ($("pubFromSed")) $("pubFromSed").hidden = true;
    const controller = new AbortController();
    spectrumAbortController = controller;
    if (chartLoader) chartLoader.start(`Measuring 102 channels at ${ra.toFixed(4)}°, ${dec.toFixed(4)}°`);
    drawChartLoading(`Measuring 102 channels at ${ra.toFixed(4)}°, ${dec.toFixed(4)}°…`);
    $("diag").textContent = "";
    try {
      const res = await fetch(`${API}/spectrum?ra=${ra.toFixed(5)}&dec=${dec.toFixed(5)}`, { signal: controller.signal });
      if (!res.ok) throw new Error(await errText(res));
      const s = await res.json();
      if (controller.signal.aborted) return;
      if (chartLoader) chartLoader.done(`${s.n_exposures} exposures · aperture r = ${s.aperture_radius_px} px · ${ra.toFixed(5)}°, ${dec.toFixed(5)}°`);
      drawChart(s);
      if ($("pubFromSed")) $("pubFromSed").hidden = false;
      const fmt = (v) => (v == null ? "no data" : (v * 100).toFixed(1) + " %");
      $("diag").innerHTML = `<dt>Water Ice (3.05 µm)</dt><dd>${fmt(s.diagnostics["h2o_ice_3.05um_depth"])}</dd>` +
        `<dt>CO₂ (4.27 µm)</dt><dd>${fmt(s.diagnostics["co2_4.27um_depth"])}</dd>`;
    } catch (e) {
      if (e.name === "AbortError" || controller.signal.aborted) {
        return; // Silent: user prioritized drag or zoom
      }
      if (chartLoader) chartLoader.fail(e.message); else $("sedInfo").textContent = e.message;
    } finally {
      if (spectrumAbortController === controller) {
        spectrumAbortController = null;
      }
    }
  }

  function drawChartLoading(msg) {
    const c = $("chart"), g = c.getContext("2d"), dpr = window.devicePixelRatio || 1;
    const W = Math.max(280, c.clientWidth), H = Math.round(W * 0.7);
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const L = 50, R = 10, T = 14, B = 34, x0 = 0.75, x1 = 5.0;
    g.clearRect(0, 0, W, H);
    const X = (v) => L + (v - x0) / (x1 - x0) * (W - L - R);
    for (let d = 1; d <= 6; d++) {
      g.fillStyle = d % 2 ? "rgba(255,255,255,.03)" : "rgba(255,255,255,0)";
      g.fillRect(X(DET[d][0]), T, X(DET[d][1]) - X(DET[d][0]), H - T - B);
      g.fillStyle = DET_COLOR[d - 1]; g.font = "10px ui-monospace, monospace"; g.textAlign = "center";
      g.fillText("D" + d, (X(DET[d][0]) + X(DET[d][1])) / 2, T + 10);
    }
    g.strokeStyle = "#252b33"; g.lineWidth = 1;
    g.strokeRect(L, T, W - L - R, H - T - B);
    g.fillStyle = "#8a94a1"; g.font = "12px system-ui, sans-serif"; g.textAlign = "center";
    g.fillText(msg || "Extracting 102-channel photometry…", (L + W - R) / 2, (T + H - B) / 2);
  }
  function drawChart(s) {
    window.SpherexChart.draw($("chart"), s);
  }

  // ------------------------------------------------------------------ publish to forum
  const MODE_NAME = { blink: "Blink", diff: "Difference", coadd: "Coaddition" };
  let pubTimer = null;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* sin almacenamiento */ } },
  };
  const fmtCoord = (ra, dec) => `${ra.toFixed(5)}°, ${dec >= 0 ? "+" : ""}${dec.toFixed(5)}°`;

  function commonLimitsOfFrames() {
    const vmins = frames.map((f) => f.meta && f.meta.vmin).filter((v) => typeof v === "number" && isFinite(v));
    const vmaxs = frames.map((f) => f.meta && f.meta.vmax).filter((v) => typeof v === "number" && isFinite(v));
    return vmins.length && vmaxs.length ? { vmin: Math.min(...vmins), vmax: Math.max(...vmaxs) } : {};
  }
  function guessTargetName() {
    const t = TARGETS.find((x) => Math.abs(x.ra - state.ra) < 1e-3 && Math.abs(x.dec - state.dec) < 1e-3);
    return t ? t.q : targetName;
  }
  function openPublish(preferMarker = true) {
    if (state.ra == null || !frames.length) {
      setMsg("Wait for the view to finish loading: it publishes exactly as currently viewed.", true);
      return;
    }
    const dlg = $("pubDlg");
    const fine = state.band.startsWith("ch");
    const chips = [
      fine ? `Channel ${state.band.slice(2)}` : `Detector ${state.band}`,
      MODE_NAME[state.mode],
      state.mode === "coadd" ? "1 frame" : `${frames.length} ${frames.length === 1 ? "epoch" : "epochs"}`,
      `Field ${Math.round(state.fov)}″`,
      `Stretch ${state.stretch}`,
      `${state.speed} ms per frame`,
    ];
    if (state.mode === "diff") chips.push("Reference epoch subtracted");
    if (state.invert) chips.push("Negative");
    chips.push(state.common ? "Same scale across all" : "Per-epoch scale");
    if (state.cross) chips.push("Reticle");
    const ul = $("pubChips"); ul.textContent = "";
    chips.forEach((c) => { const li = document.createElement("li"); li.textContent = c; ul.appendChild(li); });

    const hasMarker = !!marker;
    const rMarker = dlg.querySelector('input[name="pubObj"][value="marker"]');
    const rCenter = dlg.querySelector('input[name="pubObj"][value="center"]');
    rMarker.disabled = !hasMarker;
    (hasMarker && preferMarker ? rMarker : rCenter).checked = true;
    $("pubMarkerTxt").textContent = hasMarker ? fmtCoord(marker.ra, marker.dec) : "Click on the image to mark an object";
    $("pubCenterTxt").textContent = fmtCoord(state.ra, state.dec);
    $("pubObjHint").hidden = hasMarker;
    if (!$("pubTarget").value || $("pubTarget").dataset.auto === "1") {
      $("pubTarget").value = guessTargetName(); $("pubTarget").dataset.auto = "1";
    }
    $("pubAuthor").value = store.get("spherex.author", "");
    $("pubMsg").textContent = ""; $("pubMsg").classList.remove("err");
    $("pubSend").disabled = false; $("pubSend").textContent = "Publish";

    // live preview: same frames from the viewer at current speed
    let k = 0;
    const tick = () => {
      if (!frames.length) return;
      const f = frames[k % frames.length];
      $("pubImg").src = f.url;
      $("pubFrame").textContent = frames.length > 1 ? `${k % frames.length + 1}/${frames.length} · ${f.label}` : f.label;
      k++;
    };
    clearInterval(pubTimer); tick();
    if (frames.length > 1) pubTimer = setInterval(tick, state.speed);
    pause();
    dlg.showModal();
    $("pubTitle").focus();
  }
  async function sendPublish(ev) {
    if (ev && ev.preventDefault) ev.preventDefault();
    const title = $("pubTitle").value.trim();
    const msg = $("pubMsg");
    if (title.length < 4) { msg.textContent = "Please enter a title of at least 4 characters."; msg.classList.add("err"); $("pubTitle").focus(); return; }
    const useMarker = $("pubDlg").querySelector('input[name="pubObj"]:checked')?.value === "marker" && marker;
    const view = {
      ra: state.ra, dec: state.dec, fov: Math.round(state.fov), band: state.band, mode: state.mode,
      stretch: state.stretch, invert: state.invert, speed: state.speed, ref: state.ref,
      common: state.common, cross: state.cross, ...(state.common ? commonLimitsOfFrames() : {}),
    };
    const payload = {
      title, body: $("pubBody").value, author: $("pubAuthor").value.trim(), target: $("pubTarget").value.trim(),
      view, object: useMarker ? { ra: marker.ra, dec: marker.dec } : null,
    };
    $("pubSend").disabled = true; $("pubSend").textContent = "Publishing…";
    msg.classList.remove("err"); msg.textContent = "Sending…";
    try {
      const r = await fetch(`${API}/forum/posts`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await errText(r));
      const out = await r.json();
      const owned = store.get("spherex.owned", {});
      const safeOwned = (owned && typeof owned === "object" && !Array.isArray(owned)) ? owned : {};
      safeOwned[out.id] = out.owner_token;
      store.set("spherex.owned", safeOwned);
      if (payload.author) store.set("spherex.author", payload.author);
      msg.textContent = "Published! Opening forum…";
      setTimeout(() => {
        location.href = `/forum.html#p/${out.id}`;
      }, 350);
    } catch (e) {
      msg.textContent = (e && e.message) ? e.message : "Error publishing"; msg.classList.add("err");
      $("pubSend").disabled = false; $("pubSend").textContent = "Publish";
    }
  }
  function setupPublish() {
    if ($("publish")) $("publish").onclick = () => openPublish(true);
    if ($("pubFromSed")) $("pubFromSed").onclick = () => openPublish(true);
    if ($("pubSend")) {
      $("pubSend").onclick = (ev) => {
        if (ev && ev.preventDefault) ev.preventDefault();
        sendPublish(ev);
      };
    }
    if ($("pubForm")) {
      $("pubForm").onsubmit = (ev) => {
        if (ev && ev.preventDefault) ev.preventDefault();
        sendPublish(ev);
      };
    }
    const closePub = () => { try { $("pubDlg").close(); } catch {} };
    if ($("pubCancel")) $("pubCancel").onclick = closePub;
    if ($("pubClose")) $("pubClose").onclick = closePub;
    if ($("pubTarget")) $("pubTarget").oninput = () => { $("pubTarget").dataset.auto = "0"; };
    if ($("pubTitle")) {
      $("pubTitle").addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          sendPublish(ev);
        }
      });
    }
    if ($("pubDlg")) {
      $("pubDlg").addEventListener("close", () => { clearInterval(pubTimer); pubTimer = null; });
      $("pubDlg").addEventListener("click", (ev) => { if (ev.target === $("pubDlg")) closePub(); });
    }
  }

  // ------------------------------------------------------------------ estadisticas
  async function stats() {
    try {
      const s = await getJson(`${API}/cache/stats`), r = s.redis, d = s.disk || {};
      const gb = (b) => (b / 1073741824).toFixed(2) + " GB";
      let t = r.backend === "redis" && !r.error
        ? `Redis ${r.used_memory_human}${r.maxmemory_bytes ? " / " + gb(r.maxmemory_bytes) : ""} · ${r.keys} keys · hit rate ${r.hit_rate == null ? "—" : (r.hit_rate * 100).toFixed(0) + " %"} · ${r.ops_per_sec} ops/s`
        : "Redis unavailable (local cache)";
      if (d.fits_bytes != null) t += ` · NVMe ${gb(d.fits_bytes)} across ${d.fits_files} FITS`;
      $("stats").textContent = "cache: " + t;
    } catch { $("stats").textContent = "cache: no API connection"; }
  }

  // ------------------------------------------------------------------ tooltips universales en hover
  // ------------------------------------------------------------------ HiPS & Aladin Lite engine
  let aladinInstance = null;
  function initOrUpdateAladin() {
    if (typeof A === "undefined") {
      setMsg("HiPS all-sky navigation requires an active internet connection to load CDS Aladin Lite.");
      return;
    }
    const aladinDiv = $("aladinDiv");
    if (!aladinDiv) return;

    if (!aladinInstance) {
      try {
        aladinInstance = A.aladin("#aladinDiv", {
          survey: state.hipsSurvey || "CDS/P/allWISE/color",
          fov: (state.fov || 300) / 3600.0,
          target: `${state.ra} ${state.dec}`,
          cooFrame: "J2000",
          showReticle: state.cross,
          showZoomControl: false,
          showFullscreenControl: false,
          showLayersControl: false,
          showGotoControl: false,
          showShareControl: false,
          showFrame: false,
        });

        aladinInstance.on("positionChanged", (data) => {
          if (state.viewEngine !== "hips") return;
          state.ra = Number(data.ra.toFixed(5));
          state.dec = Number(data.dec.toFixed(5));
          if ($("ra")) $("ra").value = state.ra;
          if ($("dec")) $("dec").value = state.dec;
          if ($("cursor")) $("cursor").textContent = `${state.ra.toFixed(5)}°  ${state.dec >= 0 ? "+" : ""}${state.dec.toFixed(5)}°`;
          syncUrl();
        });

        aladinInstance.on("zoomChanged", (fovDeg) => {
          if (state.viewEngine !== "hips") return;
          state.fov = Math.max(30, Math.min(1800, Math.round(fovDeg * 3600)));
          if ($("fovExact")) $("fovExact").value = state.fov;
          if ($("fov")) $("fov").value = state.fov;
          if ($("fovOut")) $("fovOut").textContent = `${state.fov}″ (${(state.fov / 60).toFixed(2)}′)`;
          syncUrl();
        });

        aladinInstance.on("objectClicked", (obj) => {
          if (obj && obj.ra != null && obj.dec != null) {
            state.ra = Number(obj.ra.toFixed(5));
            state.dec = Number(obj.dec.toFixed(5));
            marker = { ra: state.ra, dec: state.dec };
            loadSpectrum(state.ra, state.dec);
          }
        });
      } catch (err) {
        console.warn("Error initializing Aladin Lite:", err);
      }
    } else {
      try {
        aladinInstance.gotoPosition(state.ra, state.dec);
        aladinInstance.setFoV((state.fov || 300) / 3600.0);
        if (state.hipsSurvey) aladinInstance.setImageSurvey(state.hipsSurvey);
      } catch (e) {}
    }
  }

  function switchEngine(engine) {
    state.viewEngine = engine;
    const isHips = engine === "hips";

    if ($("btnEngineBlink")) $("btnEngineBlink").classList.toggle("active", !isHips);
    if ($("btnEngineHips")) $("btnEngineHips").classList.toggle("active", isHips);
    if ($("stageHips")) $("stageHips").classList.toggle("active-hips", isHips);
    if ($("hipsPanel")) $("hipsPanel").style.display = isHips ? "block" : "none";
    if ($("aladinDiv")) $("aladinDiv").style.display = isHips ? "block" : "none";
    if ($("btnSyncBlink")) $("btnSyncBlink").style.display = isHips ? "inline-flex" : "none";

    const img = $("img");
    const overlay = $("overlay");
    if (img) img.style.display = isHips ? "none" : "block";
    if (overlay) overlay.style.display = isHips ? "none" : "block";

    if (isHips) {
      pause();
      initOrUpdateAladin();
    } else {
      reload();
    }
  }

  function setupHipsEngine() {
    state.viewEngine = "blink";
    state.hipsSurvey = "CDS/P/allWISE/color";

    if ($("btnEngineBlink")) $("btnEngineBlink").onclick = () => switchEngine("blink");
    if ($("btnEngineHips")) $("btnEngineHips").onclick = () => switchEngine("hips");
    if ($("stageHips")) $("stageHips").onclick = () => switchEngine(state.viewEngine === "hips" ? "blink" : "hips");
    if ($("btnSyncBlink")) $("btnSyncBlink").onclick = () => { switchEngine("blink"); };
    if ($("hipsSurvey")) {
      $("hipsSurvey").onchange = () => {
        state.hipsSurvey = $("hipsSurvey").value;
        if (aladinInstance) {
          try { aladinInstance.setImageSurvey(state.hipsSurvey); } catch {}
        }
      };
    }
  }

  function setupTooltips() {
    let tipEl = $("uiTooltip");
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.id = "uiTooltip";
      tipEl.className = "ui-tooltip";
      tipEl.hidden = true;
      document.body.appendChild(tipEl);
    }
    let hideTimer = null;
    let showTimer = null;
    let currentTarget = null;

    function renderTip(el) {
      const text = el.dataset.tip;
      if (!text || !text.trim()) return;
      currentTarget = el;
      tipEl.textContent = "";

      const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
      if (lines.length > 1) {
        const titleLine = document.createElement("div");
        titleLine.className = "tooltip-title";
        titleLine.textContent = lines[0];
        tipEl.appendChild(titleLine);
        for (let i = 1; i < lines.length; i++) {
          const p = document.createElement("div");
          p.className = "tooltip-line";
          p.textContent = lines[i];
          tipEl.appendChild(p);
        }
      } else {
        tipEl.textContent = text;
      }

      tipEl.hidden = false;
      tipEl.classList.remove("visible");

      const r = el.getBoundingClientRect();
      const tipRect = tipEl.getBoundingClientRect();
      let top = r.top - tipRect.height - 8;
      let left = r.left + (r.width - tipRect.width) / 2;

      if (top < 10) top = r.bottom + 8;
      if (left < 10) left = 10;
      if (left + tipRect.width > window.innerWidth - 10) {
        left = window.innerWidth - tipRect.width - 10;
      }

      tipEl.style.top = `${Math.round(top)}px`;
      tipEl.style.left = `${Math.round(left)}px`;
      requestAnimationFrame(() => {
        if (currentTarget === el) tipEl.classList.add("visible");
      });
    }

    function hideTip() {
      clearTimeout(showTimer);
      currentTarget = null;
      tipEl.classList.remove("visible");
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => {
        if (!currentTarget) tipEl.hidden = true;
      }, 150);
    }

    document.addEventListener("mouseover", (ev) => {
      const target = ev.target.closest("[data-tip], [title]");
      if (!target || target === currentTarget) return;
      if (target.hasAttribute("title") && !target.dataset.tip) {
        target.dataset.tip = target.getAttribute("title");
        target.removeAttribute("title");
      }
      clearTimeout(showTimer);
      showTimer = setTimeout(() => {
        renderTip(target);
      }, 70);
    });

    document.addEventListener("mouseout", (ev) => {
      if (!currentTarget) return;
      const target = ev.target.closest("[data-tip]");
      if (target === currentTarget) {
        if (ev.relatedTarget && currentTarget.contains(ev.relatedTarget)) return;
        hideTip();
      }
    });

    document.addEventListener("pointerdown", hideTip, { passive: true });
    window.addEventListener("scroll", hideTip, { passive: true });
  }

  viewerLoader = createBufferLoader($("viewerBuffer"), $("msg"));
  chartLoader = createBufferLoader($("chartBuffer"), $("sedInfo"));
  buildControls(); paintControls();
  if (state.ra == null) { Object.assign(state, { ra: TARGETS[0].ra, dec: TARGETS[0].dec, fov: TARGETS[0].fov, band: TARGETS[0].band }); }
  setupPublish();
  setupHipsEngine();
  setupTooltips();
  // secciones plegables: se recuerda si cada una estaba abierta o cerrada
  // la seccion "image" se mantiene cerrada de forma automatica al cargar
  document.querySelectorAll("details.fold").forEach((d) => {
    if (d.dataset.fold === "image") {
      d.open = false;
      try { localStorage.removeItem("spherex.fold.image"); } catch { /* sin almacenamiento */ }
      return;
    }
    const key = "spherex.fold." + d.dataset.fold;
    try { const v = localStorage.getItem(key); if (v != null) d.open = v === "1"; } catch { /* sin almacenamiento */ }
    d.addEventListener("toggle", () => { try { localStorage.setItem(key, d.open ? "1" : "0"); } catch { /* idem */ } });
  });
  // el boton Aleatorio vive en la cabecera plegable: que no la pliegue al pulsarlo
  $("randCoord").addEventListener("click", (ev) => ev.preventDefault());
  // enlaces desde el foro: ?mra=&mdec= marca el objeto y mide su espectro; ?publish=1 abre el formulario
  const startMarker = P.has("mra") && P.has("mdec") ? { ra: num("mra", NaN), dec: num("mdec", NaN) } : null;
  const startPublish = P.get("publish") === "1";
  Promise.resolve(reload()).then(() => {
    if (startMarker && isFinite(startMarker.ra) && isFinite(startMarker.dec) && Math.abs(startMarker.dec) <= 90) {
      marker = startMarker; drawOverlay(); loadSpectrum(marker.ra, marker.dec);
    }
    if (startPublish) {
      if (frames.length) openPublish(true);
      else setMsg("Choose a target, click on an object, and click «Publish to forum».");
    }
  });
  stats(); setInterval(stats, 20000);
})();
