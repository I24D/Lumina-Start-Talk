(() => {
  "use strict";
  const tokenKey = "jarvis_token";
  let token = sessionStorage.getItem(tokenKey) || "";
  let state = null;
  let socket = null;
  let reconnectTimer = null;
  let toastTimer = null;
  let reviewIndex = 0;

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[ch]);

  // ── The tutor's own Gemini Live session ──────────────────────────────────
  // The class runs here: this page's microphone, its own voice and its own
  // Gemini session, independent of Lumina's general assistant, whose
  // microphone Lumina pauses while a class is open. Lumina's server only mints
  // the single-use token and keeps the progress.
  const LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained";
  const MIC_RATE = 16000;
  const VOICE_RATE = 24000;
  const live = {
    ws: null, ready: false, closing: false, handle: "", retries: 0, retryTimer: null,
    micStream: null, micContext: null, micNode: null,
    voiceContext: null, voiceSources: new Set(), voiceClock: 0,
    heard: "", said: "", typed: "", pendingControls: [],
    recordedPcm: [], pronunciationTarget: "", queuedPronunciationTarget: "",
    lastPronunciation: null,
  };

  // 50 ms blocks of 16 kHz PCM16, the format the live model transcribes.
  const MIC_WORKLET = `
    class MicCapture extends AudioWorkletProcessor {
      constructor() { super(); this.block = new Int16Array(800); this.filled = 0; }
      process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (channel) {
          for (let i = 0; i < channel.length; i++) {
            const sample = Math.max(-1, Math.min(1, channel[i]));
            this.block[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
            if (this.filled === this.block.length) {
              this.port.postMessage(this.block.buffer.slice(0));
              this.filled = 0;
            }
          }
        }
        return true;
      }
    }
    registerProcessor("mic-capture", MicCapture);`;

  async function recoverAuth() {
    if (token) return true;
    const deviceToken = localStorage.getItem("jarvis_device_token");
    if (deviceToken) {
      try {
        const response = await fetch("/api/device-login", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({device_token: deviceToken}),
        });
        const data = await response.json();
        if (response.ok && data.token) {
          token = data.token;
          sessionStorage.setItem(tokenKey, token);
          sessionStorage.setItem("jarvis_key", data.key || "");
          return true;
        }
      } catch (_) {}
    }
    location.replace("/login?next=/learning-english");
    return false;
  }

  async function api(path, options = {}) {
    options.headers = Object.assign({}, options.headers, {Authorization: `Bearer ${token}`});
    const response = await fetch(path, options);
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      sessionStorage.removeItem(tokenKey); token = ""; await recoverAuth();
      throw new Error("La sesión web expiró.");
    }
    if (!response.ok) throw new Error(data.error || "No se pudo completar la acción.");
    return data;
  }

  function toast(message, error = false) {
    const node = $("#toast"); node.textContent = message; node.style.borderColor = error ? "rgba(255,110,123,.5)" : "";
    node.classList.add("show"); clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
  }

  function skillName(key) {
    return ({conversation:"Conversación",pronunciation:"Pronunciación",grammar:"Gramática",vocabulary:"Vocabulario",listening:"Comprensión"})[key] || key;
  }

  function render(snapshot) {
    if (!snapshot) return;
    state = snapshot;
    const profile = snapshot.profile || {};
    const currentSession = (snapshot.sessions || []).find(item => item.id === snapshot.current_session_id) || {};
    const progress = Number(currentSession.progress || snapshot.last_lesson?.progress || 0);
    const level = profile.level || "A1";
    const metrics = snapshot.metrics || {};
    $("#header-level").textContent = level; $("#level-ring").textContent = level;
    $("#header-goal").textContent = profile.goal || "Configurando perfil";
    $("#profile-goal").textContent = profile.goal || "Inglés práctico";
    $("#header-progress").textContent = `${progress}%`;
    $("#header-xp").textContent = Number(metrics.xp || 0).toLocaleString("es");
    $("#header-streak").textContent = `${Number(metrics.streak_days || 0)} días`;
    $("#lesson-label").textContent = profile.current_objective || "Primera lección";
    $("#lesson-objective").textContent = profile.current_objective || "Construyamos tu plan de aprendizaje";
    $("#onboarding-card").classList.toggle("hidden", snapshot.state !== "onboarding");
    $("#mic-chip").classList.toggle("online", !!live.micStream && snapshot.active && !snapshot.inputMuted && !snapshot.paused);
    $("#mic-chip").classList.toggle("warn", snapshot.inputMuted || snapshot.paused);
    $("#mic-chip").childNodes[$("#mic-chip").childNodes.length - 1].textContent = snapshot.inputMuted ? " Micrófono silenciado" : " Micrófono";
    $$("#mode-list button").forEach(button => button.classList.toggle("active", button.dataset.mode === snapshot.currentMode));
    $("#mute-button").classList.toggle("active", !snapshot.inputMuted);
    $("#mute-button").setAttribute("aria-pressed", String(!snapshot.inputMuted));
    $("#mute-button").lastChild.textContent = snapshot.inputMuted ? " Activar micrófono" : " Micrófono";
    $("#pause-button").textContent = snapshot.paused ? "▶ Continuar clase" : "Ⅱ Pausar clase";
    $("#translation-button").classList.toggle("active", snapshot.translationEnabled);
    $("#translation-button").setAttribute("aria-pressed", String(!!snapshot.translationEnabled));
    $("#words-count").textContent = `${(snapshot.vocabulary || []).length} palabras`;
    $("#vocabulary-total").textContent = (snapshot.vocabulary || []).length;
    $("#error-count").textContent = (snapshot.frequent_errors || []).length;
    $("#review-count").textContent = `${(snapshot.reviewQueue || []).length} pendientes`;
    $("#history-count").textContent = `${(snapshot.sessions || []).filter(item => item.status === "completed").length} clases`;
    $("#roadmap-current").textContent = `${snapshot.currentUnit?.level || level} · ${snapshot.currentUnit?.title || "Unidad actual"}`;
    $("#listening-current").textContent = snapshot.activeListeningActivity?.title || "Opción correcta";
    const scenario = snapshot.activeScenario;
    $("#scenario-pill").classList.toggle("hidden", !scenario);
    $("#scenario-pill").textContent = scenario ? `${scenario.icon} ${scenario.title}` : "";
    renderSkills(snapshot.skill_progress || {});
    renderErrors(snapshot.frequent_errors || []);
    renderVocabulary(snapshot.vocabulary || []);
    renderWeek(snapshot.weekly_activity || []);
    renderScenarios(snapshot.catalog?.scenarios || []);
    renderRoadmap(snapshot.catalog?.levels || []);
    renderListening(snapshot.catalog?.listeningActivities || []);
    renderReview(snapshot.reviewQueue || []);
    renderHistory(snapshot.sessions || []);
    renderProviders(snapshot.providers || []);
    if (snapshot.lastResponse) renderTutorResponse(snapshot.lastResponse);
    if (snapshot.storageStatus === "temporary") {
      $("#dock-detail").textContent = snapshot.storageError || "Progreso temporal; se reintentará el guardado";
    } else {
      $("#dock-detail").textContent = "Tu progreso se guarda automáticamente";
    }
    // The summary opens once, when a class ends (sessionCompleted). Reopening it
    // from every later snapshot left a stale modal blocking the next class.
    if (snapshot.active) closeSummary();
    // A class ended from Lumina closes this page's session too; a class opened
    // again while the page stayed open starts a fresh one.
    if (snapshot.active && live.closing) { live.closing = false; startClass(); }
    if (!snapshot.active && !live.closing && (live.ws || live.micStream)) shutdownLive();
  }

  function renderSkills(skills) {
    $("#skill-bars").innerHTML = Object.entries({conversation:0,pronunciation:0,grammar:0,vocabulary:0,listening:0}).map(([key]) => {
      const value = Math.max(0, Math.min(100, Number(skills[key] || 0)));
      return `<div class="skill-row"><header><span>${esc(skillName(key))}</span><b>${value}%</b></header><div class="skill-track"><i style="width:${value}%"></i></div></div>`;
    }).join("");
  }

  function renderErrors(errors) {
    const recent = errors.slice(0, 4);
    $("#error-list").innerHTML = recent.length ? recent.map(item => `<div class="mini-error"><strong>${esc(item.corrected)}</strong>${esc(item.category)} · ${Number(item.count || 1)} veces</div>`).join("") : '<p class="empty-copy">Las oportunidades de práctica aparecerán aquí.</p>';
  }

  function renderWeek(entries) {
    const map = new Map(entries.map(item => [item.date, Number(item.lessons || 0)]));
    const days = [];
    for (let i = 6; i >= 0; i--) { const d = new Date(); d.setDate(d.getDate() - i); days.push(d); }
    const total = days.reduce((sum,d) => sum + (map.get(d.toISOString().slice(0,10)) || 0), 0);
    $("#week-total").textContent = `${total} ${total === 1 ? "clase" : "clases"}`;
    $("#week-chart").innerHTML = days.map(d => { const n = map.get(d.toISOString().slice(0,10)) || 0; return `<div class="week-column" title="${n} clases"><i style="height:${Math.max(4,n*15)}px"></i><small>${d.toLocaleDateString("es",{weekday:"narrow"})}</small></div>`; }).join("");
  }

  function renderVocabulary(words) {
    const list = words.slice(-6).reverse();
    $("#vocabulary-list").innerHTML = list.length ? list.map(item => `<article class="word-card"><header><strong>${esc(item.word)}</strong><button type="button" data-save-word="${esc(item.word)}" data-meaning="${esc(item.meaning)}" data-example="${esc(item.example)}">${item.saved ? "✓ Guardada" : "+ Guardar"}</button></header><p>${esc(item.meaning)}</p><em>${esc(item.example)}</em></article>`).join("") : '<p class="empty-copy">Todavía no hay palabras nuevas.</p>';
  }

  function renderScenarios(scenarios) {
    const active = state?.active_scenario_id || "";
    $("#scenario-grid").innerHTML = scenarios.map(item => `<button type="button" class="scenario-card ${item.id === active ? "active" : ""}" data-scenario="${esc(item.id)}" ${item.available ? "" : "disabled"}>${item.recommended ? '<span class="recommended-tag">Recomendado</span>' : ""}<span class="scenario-icon">${esc(item.icon)}</span><h3>${esc(item.title)}</h3><p>${esc(item.objective)}</p><span class="scenario-meta"><span>${esc(item.category)}</span><span>${esc(item.minLevel)}</span><span>${Number(item.duration)} min</span></span></button>`).join("");
  }

  function renderRoadmap(levels) {
    $("#roadmap").innerHTML = levels.map(level => `<section class="level-block ${level.units.some(unit => unit.available) ? "" : "locked"}"><div class="level-summary"><strong>${esc(level.id)}</strong><h3>${esc(level.title)}</h3><p>${esc(level.canDo)}</p></div><div class="unit-list">${level.units.map(unit => `<button type="button" class="unit-card ${unit.current ? "current" : ""} ${unit.completed ? "completed" : ""}" data-unit="${esc(unit.id)}" ${unit.available ? "" : "disabled"}><span><b>${esc(unit.title)}</b><small>${esc(unit.objective)} · ${Number(unit.lessons)} lecciones</small></span><span class="unit-state">${unit.completed ? "✓" : unit.current ? "EN CURSO" : "○"}</span></button>`).join("")}</div></section>`).join("");
  }

  function renderListening(activities) {
    const active = state?.activeListeningActivity?.id || "";
    $("#listening-grid").innerHTML = activities.map(item => `<button type="button" class="scenario-card ${item.id === active ? "active" : ""}" data-listening="${esc(item.id)}"><span class="scenario-icon">${esc(item.icon)}</span><h3>${esc(item.title)}</h3><p>${esc(item.instruction)}</p></button>`).join("");
  }

  function renderReview(queue) {
    if (reviewIndex >= queue.length) reviewIndex = 0;
    const item = queue[reviewIndex];
    $("#rating-row").classList.toggle("hidden", !item);
    $("#review-card").innerHTML = item ? `<div><span class="section-kicker">${reviewIndex + 1} de ${queue.length}</span><h3>${esc(item.word)}</h3><p>${esc(item.meaning || "Recuerda su significado y úsala en voz alta.")}</p><p class="review-example">${esc(item.example || "")}</p><button type="button" class="ghost-button review-practice" data-review-practice="${esc(item.word)}">Practicar con Lumina</button></div>` : "<p>No hay palabras pendientes por ahora. Lumina las mostrará en el momento óptimo.</p>";
  }

  function renderHistory(sessions) {
    const completed = sessions.filter(item => item.status === "completed").slice().reverse();
    $("#history-list").innerHTML = completed.length ? completed.map(item => `<article class="history-card"><header><strong>${esc((item.mode || "conversation").replace("-", " "))}</strong><small>${esc(String(item.ended_at || item.started_at || "").slice(0,10))} · ${Number(item.progress || 0)}%</small></header><p>${esc(item.summary || `${(item.turns || []).length} intervenciones · ${Number(item.correction_count || 0)} correcciones`)}</p></article>`).join("") : '<p class="empty-copy">Tu primera clase aparecerá aquí cuando la termines.</p>';
  }

  function renderProviders(providers) {
    $("#provider-list").innerHTML = providers.map(item => `<div class="provider-row ${item.state === "ready" ? "ready" : ""}"><strong>${esc(item.label)}</strong><span>${esc(item.detail)}</span><i></i></div>`).join("");
  }

  function renderTutorResponse(response) {
    if (response.assistantText) $("#assistant-text").textContent = response.assistantText;
    $("#next-action").textContent = response.nextAction || "Responde cuando estés listo.";
    const corrections = response.corrections || [];
    $("#correction-list").innerHTML = corrections.length ? corrections.map((item,index) => `<article class="correction-card"><span class="category">${esc(item.category)} · ${esc(item.source || "Gemini")}</span><div class="compare"><div><small>Lo que dijiste</small><p class="original">${esc(item.original)}</p></div><div><small>Forma recomendada</small><p class="corrected">${esc(item.corrected)}</p></div></div><p class="explanation">${esc(item.explanation)}</p>${item.pronunciation ? `<p class="explanation"><b>Pronunciación aproximada:</b> ${esc(item.pronunciation)}</p>` : ""}${state?.translationEnabled && item.translation ? `<p class="explanation"><b>Traducción:</b> ${esc(item.translation)}</p>` : ""}<div class="correction-actions"><button type="button" data-practice="${index}">↻ Practicar de nuevo</button></div></article>`).join("") : '<div class="empty-state"><span>✓</span><h3>Buen turno</h3><p>No hay una corrección esencial en esta respuesta. Continuemos.</p></div>';
    if (live.lastPronunciation) {
      renderPronunciationResult(
        live.lastPronunciation.result,
        live.lastPronunciation.expected,
        false,
      );
    }
  }

  function setTutorStatus(kind, label) {
    const node = $("#tutor-state"); node.innerHTML = `<i></i>${esc(label)}`;
    $("#dock-state").textContent = label;
    $("#tutor-orb").classList.toggle("speaking", kind === "speaking");
    $(".sound-wave").classList.toggle("active", kind === "speaking" || kind === "listening");
    $("#stream-label").textContent = kind.toUpperCase();
  }

  // ── Live session plumbing ────────────────────────────────────────────────

  function toBase64(buffer) {
    const bytes = new Uint8Array(buffer); let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }

  function joinPcmBase64(parts) {
    const size = parts.reduce((total, part) => total + part.byteLength, 0);
    const joined = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
      joined.set(new Uint8Array(part), offset);
      offset += part.byteLength;
    }
    return toBase64(joined.buffer);
  }

  function renderPronunciationResult(result, expected, remember = true) {
    if (remember) live.lastPronunciation = {result, expected};
    const score = Math.max(0, Math.min(100, Number(result?.score || 0)));
    const transcript = result?.transcribe || result?.transcription || "";
    const errors = result?.differences?.errors || [];
    const details = errors.slice(0, 4).map(item =>
      `<li><b>${esc(item.word || "sonido")}</b>: ${esc(item.expected || "—")} → ${esc(item.actual || "omitido")}</li>`
    ).join("");
    $("#correction-list").insertAdjacentHTML("afterbegin", `<article class="correction-card pronunciation-result"><span class="category">OpenPronounce · análisis fonético local</span><div class="pronunciation-score"><strong>${Math.round(score)}</strong><span>/100</span></div><p class="explanation"><b>Frase objetivo:</b> ${esc(expected)}</p>${transcript ? `<p class="explanation"><b>Se reconoció:</b> ${esc(transcript)}</p>` : ""}${details ? `<ul class="phoneme-errors">${details}</ul>` : '<p class="explanation">No se detectaron diferencias fonéticas importantes.</p>'}</article>`);
  }

  async function analyzePronunciation(parts, expected) {
    if (!parts.length || !expected) return;
    const provider = (state?.providers || []).find(item => item.id === "openpronounce");
    if (provider?.state !== "ready") return;
    try {
      const data = await api("/api/learning/pronunciation", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          expected_text: expected,
          sample_rate: MIC_RATE,
          pcm_base64: joinPcmBase64(parts),
        }),
      });
      renderPronunciationResult(data.result || {}, expected);
    } catch (error) {
      toast(error.message, true);
    }
  }

  function fromBase64(data) {
    const binary = atob(data); const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function liveSend(message) {
    if (live.ws && live.ws.readyState === WebSocket.OPEN && live.ready) live.ws.send(JSON.stringify(message));
  }

  function sendTeacherControl(instruction) {
    if (!live.ready) {
      live.pendingControls.push(instruction);
      toast("Instrucción preparada; se aplicará al conectar");
      return;
    }
    liveSend({clientContent: {turns: [{role: "user", parts: [{text: `[TEACHER_CONTROL]\n${instruction}`}]}], turnComplete: true}});
  }

  async function ensureVoice() {
    if (!live.voiceContext) live.voiceContext = new AudioContext({sampleRate: VOICE_RATE});
    if (live.voiceContext.state === "suspended") { try { await live.voiceContext.resume(); } catch (_) {} }
    return live.voiceContext.state === "running";
  }

  async function startMicrophone() {
    if (live.micStream) return;
    live.micStream = await navigator.mediaDevices.getUserMedia({audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true,
      // Automatic gain can move the device's input level, and Lumina reads the
      // same microphone again once the class ends.
      autoGainControl: false,
    }});
    live.micContext = new AudioContext({sampleRate: MIC_RATE});
    const url = URL.createObjectURL(new Blob([MIC_WORKLET], {type: "application/javascript"}));
    try { await live.micContext.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
    const source = live.micContext.createMediaStreamSource(live.micStream);
    live.micNode = new AudioWorkletNode(live.micContext, "mic-capture");
    live.micNode.port.onmessage = event => {
      if (state?.paused || state?.inputMuted) return;
      // Keep only the current student's turn, and only when the optional
      // pronunciation engine has an explicit target to compare against.
      if (live.pronunciationTarget) {
        live.recordedPcm.push(event.data.slice(0));
        while (live.recordedPcm.length > 600) live.recordedPcm.shift();
      }
      liveSend({realtimeInput: {audio: {mimeType: `audio/pcm;rate=${MIC_RATE}`, data: toBase64(event.data)}}});
    };
    // Pulled through a silent output so the worklet keeps running; nothing is heard.
    const silent = live.micContext.createGain(); silent.gain.value = 0;
    source.connect(live.micNode).connect(silent).connect(live.micContext.destination);
    if (live.micContext.state === "suspended") await live.micContext.resume();
    $("#mic-chip").classList.add("online");
  }

  function stopMicrophone() {
    if (live.micStream) live.micStream.getTracks().forEach(track => track.stop());
    if (live.micContext) live.micContext.close().catch(() => {});
    live.micStream = null; live.micContext = null; live.micNode = null;
    $("#mic-chip").classList.remove("online");
  }

  function playVoice(base64) {
    const ctx = live.voiceContext; if (!ctx) return;
    const bytes = fromBase64(base64);
    const samples = new Int16Array(bytes.buffer, 0, Math.floor(bytes.byteLength / 2));
    const buffer = ctx.createBuffer(1, samples.length, VOICE_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000;
    const source = ctx.createBufferSource(); source.buffer = buffer; source.connect(ctx.destination);
    live.voiceClock = Math.max(live.voiceClock, ctx.currentTime);
    source.start(live.voiceClock); live.voiceClock += buffer.duration;
    live.voiceSources.add(source);
    setTutorStatus("speaking", "Hablando");
    source.onended = () => {
      live.voiceSources.delete(source);
      if (!live.voiceSources.size && live.ready) setTutorStatus("listening", "Escuchando");
    };
  }

  function stopVoice() {
    live.voiceSources.forEach(source => { try { source.stop(); } catch (_) {} });
    live.voiceSources.clear();
    live.voiceClock = 0;
  }

  async function connectLive() {
    clearTimeout(live.retryTimer);
    if (live.closing || (live.ws && live.ws.readyState <= WebSocket.OPEN)) return;
    setTutorStatus("connecting", "Conectando con la maestra");
    let session;
    try {
      session = await api("/api/learning/live-session", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({handle: live.handle})});
    } catch (error) { toast(error.message, true); scheduleLiveRetry(); return; }
    if (live.closing) return;
    const ws = new WebSocket(`${LIVE_URL}?access_token=${encodeURIComponent(session.token)}`);
    live.ws = ws; live.ready = false;
    ws.onopen = () => ws.send(JSON.stringify({setup: {model: session.model}}));
    ws.onmessage = async event => {
      const text = typeof event.data === "string" ? event.data : await event.data.text();
      let message; try { message = JSON.parse(text); } catch (_) { return; }
      if (live.ws === ws) handleLiveMessage(message, session);
    };
    ws.onclose = event => {
      if (live.ws !== ws) return;
      live.ws = null; live.ready = false;
      $("#gemini-chip").classList.remove("online");
      if (!live.closing) { setTutorStatus("reconnecting", "Reconectando con la maestra"); scheduleLiveRetry(event.reason); }
    };
  }

  function scheduleLiveRetry(reason) {
    if (live.closing || !state?.active) return;
    live.retries += 1;
    if (reason) console.warn("Tutor session closed:", reason);
    live.retryTimer = setTimeout(connectLive, Math.min(1000 * 2 ** Math.min(live.retries, 4), 15000));
  }

  function handleLiveMessage(message, session) {
    if (message.setupComplete) {
      live.ready = true; live.retries = 0;
      $("#gemini-chip").classList.add("online");
      setTutorStatus("listening", "Escuchando");
      if (session.opening) sendTeacherControl(session.opening);
      while (live.pendingControls.length) sendTeacherControl(live.pendingControls.shift());
      return;
    }
    const update = message.sessionResumptionUpdate;
    if (update?.resumable && update.newHandle) live.handle = update.newHandle;
    if (message.goAway) {
      // The service is rotating the session: reconnect now, resuming it.
      live.retries = 0;
      try { live.ws?.close(); } catch (_) {}
      return;
    }
    const content = message.serverContent; if (!content) return;
    if (content.interrupted) stopVoice();
    if (content.inputTranscription?.text) {
      live.heard += content.inputTranscription.text;
      $("#student-text").textContent = live.heard.trim();
    }
    if (content.outputTranscription?.text) {
      live.said += content.outputTranscription.text;
      $("#assistant-text").textContent = live.said.trim();
    }
    for (const part of content.modelTurn?.parts || []) {
      if (part.inlineData?.data) playVoice(part.inlineData.data);
    }
    if (content.turnComplete) finishTurn();
  }

  function finishTurn() {
    const user = (live.typed || live.heard).trim();
    const assistant = live.said.trim();
    const pronunciationAudio = live.recordedPcm.splice(0);
    const pronunciationTarget = live.pronunciationTarget;
    live.pronunciationTarget = "";
    live.heard = ""; live.said = ""; live.typed = "";
    if (live.queuedPronunciationTarget) {
      live.pronunciationTarget = live.queuedPronunciationTarget;
      live.queuedPronunciationTarget = "";
    }
    if (!user && !assistant) return;
    if (user && pronunciationTarget) {
      analyzePronunciation(pronunciationAudio, pronunciationTarget);
    }
    api("/api/learning/turn", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({user, assistant})})
      .then(data => { if (data.state) render(data.state); })
      .catch(error => toast(error.message, true));
  }

  async function startClass() {
    $("#start-button").classList.add("hidden");
    if (!(await ensureVoice())) {
      // The browser only lets a page play sound after the user touches it.
      $("#start-button").classList.remove("hidden");
      setTutorStatus("waiting", "Pulsa Empezar clase");
      return;
    }
    try { await startMicrophone(); }
    catch (_) { toast("Permite el micrófono en el navegador para hablar con la maestra.", true); $("#mic-chip").classList.add("warn"); }
    connectLive();
  }

  function shutdownLive() {
    live.closing = true; clearTimeout(live.retryTimer);
    stopVoice(); stopMicrophone();
    const ws = live.ws; live.ws = null; live.ready = false; live.handle = "";
    try { ws?.close(); } catch (_) {}
    live.heard = ""; live.said = ""; live.typed = "";
    live.recordedPcm = []; live.pronunciationTarget = "";
    live.queuedPronunciationTarget = "";
    live.lastPronunciation = null;
    $("#gemini-chip").classList.remove("online");
  }

  async function endClass() {
    shutdownLive();
    const data = await action("exit");
    if (!data) { live.closing = false; startClass(); }  // exit failed: stay in the class
  }

  async function sendMessage(text) {
    text = String(text || "").trim(); if (!text) return;
    if (!live.ready) { toast("La maestra todavía se está conectando", true); return; }
    stopVoice();
    live.typed = text; live.heard = "";
    $("#student-text").textContent = text; setTutorStatus("thinking", "Lumina está pensando");
    liveSend({clientContent: {turns: [{role: "user", parts: [{text}]}], turnComplete: true}});
    $("#message-input").value = "";
  }

  async function action(name, payload = {}) {
    try {
      const data = await api("/api/learning/action", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:name,...payload})});
      if (data.state) render(data.state);
      return data;
    } catch (error) { toast(error.message, true); return null; }
  }

  // Lumina's own socket: progress, corrections and the end of a class.
  function connectSocket() {
    if (!token) return;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${location.host}/ws?token=${encodeURIComponent(token)}`);
    socket.onclose = () => { clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connectSocket, 1800); };
    socket.onmessage = event => {
      let message; try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type === "learning.turn") { renderTutorResponse(message.response || {}); loadState(); }
      if (message.type === "learning.snapshot") render(message.state);
      if (message.type === "learning.error") toast(message.message || "Ocurrió un error", true);
      if (message.type === "learningEnglish.sessionCompleted") showSummary(message.summary || "Clase completada.");
      if (message.type === "learningEnglish.modeEntered") closeSummary();
      if (message.type === "learningEnglish.modeExited") setTutorStatus("inactive", "Clase finalizada");
    };
  }

  async function loadState() {
    try {
      const data = await api("/api/learning/state"); render(data);
      if (!data.active) { const entered = await action("enter"); if (entered?.state) render(entered.state); }
    } catch (error) { toast(error.message, true); setTutorStatus("error", "Lumina no disponible"); }
  }

  function showSummary(summary) {
    $("#summary-copy").textContent = summary || "Tu progreso quedó guardado.";
    const dialog = $("#summary-dialog"); if (!dialog.open) dialog.showModal();
  }

  function closeSummary() {
    const dialog = $("#summary-dialog"); if (dialog.open) dialog.close();
  }

  function bind() {
    $("#message-form").addEventListener("submit", event => { event.preventDefault(); sendMessage($("#message-input").value); });
    $("#message-input").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); } });
    $$("[data-message]").forEach(button => button.addEventListener("click", () => sendMessage(button.dataset.message)));
    $$("#mode-list button").forEach(button => button.addEventListener("click", async () => {
      const data = await action("mode", {mode:button.dataset.mode});
      if (!data) return;
      sendTeacherControl(`Cambia ahora al modo de aprendizaje '${button.dataset.mode}'. Presenta un solo ejercicio breve.`);
      toast(`${button.textContent.trim()} activado`);
    }));
    document.addEventListener("click", event => {
      const openDialog = event.target.closest("[data-open-dialog]");
      if (openDialog) {
        const dialog = document.getElementById(openDialog.dataset.openDialog);
        if (dialog && !dialog.open) dialog.showModal();
      }
      const closeDialog = event.target.closest("[data-close-dialog]");
      if (closeDialog) document.getElementById(closeDialog.dataset.closeDialog)?.close();

      const scenarioButton = event.target.closest("[data-scenario]");
      if (scenarioButton) action("scenario", {scenario_id:scenarioButton.dataset.scenario}).then(data => {
        const scenario = data?.state?.activeScenario;
        if (!scenario) return;
        $("#scenario-dialog").close();
        sendTeacherControl(`Inicia el escenario '${scenario.title}'. Tú eres ${scenario.roles.tutor} y el estudiante es ${scenario.roles.student}. Objetivo: ${scenario.objective}. Comienza exactamente con: ${scenario.starter}`);
        toast(`${scenario.title} iniciado`);
      });

      const unitButton = event.target.closest("[data-unit]");
      if (unitButton) action("unit", {unit_id:unitButton.dataset.unit}).then(data => {
        const unit = data?.state?.currentUnit;
        if (!unit) return;
        $("#roadmap-dialog").close();
        sendTeacherControl(`Comienza una lección de la unidad '${unit.title}', nivel ${unit.level}. Objetivo: ${unit.objective}. Haz primero una comprobación breve y luego un solo ejercicio.`);
        toast(`${unit.title} seleccionado`);
      });

      const listeningButton = event.target.closest("[data-listening]");
      if (listeningButton) action("listening-activity", {activity_id:listeningButton.dataset.listening}).then(data => {
        const activity = data?.state?.activeListeningActivity;
        if (!activity) return;
        $("#listening-dialog").close();
        sendTeacherControl(`Inicia Listening Lab con '${activity.title}'. Sigue esta mecánica: ${activity.instruction}`);
        toast(`${activity.title} iniciado`);
      });

      const ratingButton = event.target.closest("[data-rating]");
      if (ratingButton) {
        const item = state?.reviewQueue?.[reviewIndex];
        if (item) action("review-word", {word:item.word,rating:ratingButton.dataset.rating}).then(data => {
          if (data) { reviewIndex = 0; toast("Repaso registrado con FSRS"); }
        });
      }

      const reviewPractice = event.target.closest("[data-review-practice]");
      if (reviewPractice) {
        $("#review-dialog").close();
        live.recordedPcm = [];
        live.lastPronunciation = null;
        live.queuedPronunciationTarget = reviewPractice.dataset.reviewPractice;
        sendTeacherControl(`Practica la palabra '${reviewPractice.dataset.reviewPractice}' en una situación breve. No des la respuesta antes de que el estudiante lo intente.`);
      }

      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {
        const name = actionButton.dataset.action;
        if (name === "pause") action(state?.paused ? "resume" : "pause");
        else if (name === "mute") action(state?.inputMuted ? "unmute" : "mute");
        else if (name === "translation") action("translation", {enabled:!state?.translationEnabled});
        else if (name === "repeat") sendTeacherControl("Repite tu última pregunta o ejemplo, sin añadir otro ejercicio.");
        else if (name === "slower") sendTeacherControl("Repite tu última frase más despacio y con pausas naturales.");
        else if (name === "stop") stopVoice();
      }
      const practice = event.target.closest("[data-practice]");
      if (practice && state?.lastResponse?.corrections) {
        const correction = state.lastResponse.corrections[Number(practice.dataset.practice)];
        if (correction) {
          live.recordedPcm = [];
          live.lastPronunciation = null;
          live.queuedPronunciationTarget = correction.corrected;
          sendTeacherControl(`Pide al estudiante repetir exactamente: '${correction.corrected}'. Espera su voz antes de corregir.`);
        }
      }
      const save = event.target.closest("[data-save-word]");
      if (save) action("save-word", {word:save.dataset.saveWord,meaning:save.dataset.meaning,example:save.dataset.example}).then(() => toast("Palabra guardada"));
    });
    $("#start-button").addEventListener("click", () => startClass());
    $("#end-button").addEventListener("click", () => endClass());
    $("#back-button").addEventListener("click", () => endClass());
    $("#settings-button").addEventListener("click", () => $("#settings-dialog").showModal());
    $("#save-settings").addEventListener("click", event => { event.preventDefault(); action("settings", {preferred_speed:$("#speed-select").value,translation_enabled:$("#translation-select").value === "true"}); $("#settings-dialog").close(); toast("Preferencias guardadas"); });
    $("#summary-close").addEventListener("click", () => { $("#summary-dialog").close(); try { window.close(); } catch (_) {} });
    $("#input-mode-button").addEventListener("click", () => $("#message-input").focus());
    window.addEventListener("keydown", event => { if (event.key === "Escape" && !$("#settings-dialog").open && !$("#summary-dialog").open) stopVoice(); });
    window.addEventListener("pagehide", () => shutdownLive());
  }

  (async function init() {
    bind();
    if (!(await recoverAuth())) return;
    connectSocket();
    await loadState();
    if (state?.active) startClass();
  })();
})();
