"use strict";
(() => {
  const POLL_MS = 150;
  const FINGERS = ["thumb", "index", "middle", "ring"];
  const $ = (id) => document.getElementById(id);
  let state = null;
  let confirmUntil = 0;
  let busy = false;
  let offline = false;
  const dirty = {};

  // ────────────────────────────────────────────────── 小工具
  function text(el, value) {
    if (typeof el === "string") el = $(el);
    const s = value == null ? "" : String(value);
    if (el.textContent !== s) el.textContent = s;
  }
  function attr(el, name, value) {
    if (typeof el === "string") el = $(el);
    const s = String(value);
    if (el.getAttribute(name) !== s) el.setAttribute(name, s);
  }
  function show(el, visible) {
    if (typeof el === "string") el = $(el);
    if (el.hidden === visible) el.hidden = !visible;
  }
  function enable(el, on) {
    if (typeof el === "string") el = $(el);
    if (el.disabled === on) el.disabled = !on;
  }
  const percent = (v) => (v == null || !isFinite(v) ? "—" : Math.round(v * 100) + "%");
  const clamp01 = (v) => Math.max(0, Math.min(1, v));

  let toastTimer = 0;
  function toast(message, bad) {
    const t = $("toast");
    t.textContent = message;
    t.classList.toggle("bad", !!bad);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 2800);
  }

  async function api(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body || {}) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* 沒有內容 */ }
    if (!r.ok) throw new Error(data.error || "操作失敗（" + r.status + "）");
    return data;
  }
  async function act(path, body, done) {
    if (busy) return;
    busy = true;
    try {
      const data = await api(path, body);
      if (done) done(data);
    } catch (e) {
      toast(e.message, true);
    } finally {
      busy = false;
      refresh();
    }
  }

  // ────────────────────────────────────────────────── 手指示意圖
  function fingerPoints(curl) {
    const c = clamp01(curl || 0);
    const lengths = [58, 38, 28];
    const bends = [78, 96, 72];
    const pts = [[70, 46]];
    let angle = 0;
    for (let i = 0; i < 3; i++) {
      angle += (bends[i] * c * Math.PI) / 180;
      const p = pts[pts.length - 1];
      pts.push([p[0] + lengths[i] * Math.cos(angle), p[1] + lengths[i] * Math.sin(angle)]);
    }
    return pts;
  }
  const f1 = (n) => n.toFixed(1);
  function fingerSvg(curl, kind, ghost, off) {
    const pts = fingerPoints(curl);
    const widths = [26, 23, 20];
    let out = '<g class="' + (off ? "f-off" : "") + '">';
    if (ghost != null) {
      const g = fingerPoints(ghost);
      out += '<polyline class="f-ghost" points="' + g.map((p) => f1(p[0]) + "," + f1(p[1])).join(" ") + '"/>';
    }
    out += '<rect class="f-palm" x="8" y="20" width="66" height="54" rx="14"/>';
    for (let i = 2; i >= 0; i--) {
      out += '<line class="f-seg ' + (kind === "robot" ? "f-bot" : "f-skin") + '" stroke-width="' + widths[i] +
             '" x1="' + f1(pts[i][0]) + '" y1="' + f1(pts[i][1]) + '" x2="' + f1(pts[i + 1][0]) + '" y2="' + f1(pts[i + 1][1]) + '"/>';
    }
    if (kind === "robot") {
      for (let i = 0; i < 3; i++) out += '<circle class="f-joint" cx="' + f1(pts[i][0]) + '" cy="' + f1(pts[i][1]) + '" r="8"/>';
    } else {
      const dx = pts[1][0] - pts[0][0], dy = pts[1][1] - pts[0][1], len = Math.hypot(dx, dy);
      const cx = pts[0][0] + dx * 0.5, cy = pts[0][1] + dy * 0.5, nx = -dy / len, ny = dx / len;
      out += '<line class="f-seg f-band" stroke-width="10" x1="' + f1(cx - nx * 15) + '" y1="' + f1(cy - ny * 15) +
             '" x2="' + f1(cx + nx * 15) + '" y2="' + f1(cy + ny * 15) + '"/>';
    }
    return out + "</g>";
  }
  const drawn = new WeakMap();
  function draw(svg, curl, kind, ghost, off) {
    const key = [curl == null ? "x" : curl.toFixed(3), kind, ghost == null ? "x" : ghost.toFixed(3), off].join("|");
    if (drawn.get(svg) === key) return;
    drawn.set(svg, key);
    svg.innerHTML = fingerSvg(curl, kind, ghost, off);
  }

  function axes(root, raw) {
    const rows = root.querySelectorAll(".axis");
    for (let i = 0; i < 3; i++) {
      const bar = rows[i].querySelector("i"), out = rows[i].querySelector("output");
      if (!raw) { bar.style.width = "0"; text(out, "—"); continue; }
      const g = raw[i] / 8192, w = Math.min(50, (Math.abs(g) / 1.2) * 50);
      bar.style.width = w + "%";
      bar.style.left = (g < 0 ? 50 - w : 50) + "%";
      text(out, (g >= 0 ? "+" : "") + g.toFixed(2));
    }
  }

  // ────────────────────────────────────────────────── 畫面
  function renderControl(s) {
    const ring = s.ring, hand = s.hand, panel = hand.panel, follow = s.follow, curl = follow.curl || {};
    const cal = s.calibration;

    // 頂列
    const chipRing = $("chip-ring"), chipHand = $("chip-hand");
    attr(chipRing, "data-state", ring.connected ? "ok" : ring.busy ? "busy" : "off");
    text(chipRing.lastElementChild, ring.connected ? (ring.battery == null ? "已連線" : ring.battery + "%") : ring.busy ? "連線中" : "未連線");
    let handState = "off", handText = "未連線";
    if (hand.linked && !hand.reachable) { handState = "bad"; handText = "連不到"; }
    else if (panel && panel.fault) { handState = "bad"; handText = "保護鎖定"; }
    else if (panel && panel.torque_on && panel.you_control) { handState = "live"; handText = "跟隨中"; }
    else if (panel && panel.torque_on) { handState = "busy"; handText = "別的頁面控制中"; }
    else if (panel) { handState = "ok"; handText = panel.simulated ? "模擬，就緒" : "就緒"; }
    else if (hand.linked) { handState = "busy"; handText = "連線中"; }
    attr(chipHand, "data-state", handState);
    text(chipHand.lastElementChild, handText);
    enable("stop", hand.linked);

    // 步驟
    const done = { ring: ring.connected, hand: hand.reachable, cal: !!cal.saved, go: follow.phase === "following" };
    let next = null;
    for (const li of $("steps").children) {
      const key = li.dataset.step;
      attr(li, "data-done", key !== "go" && done[key]);
      attr(li, "data-live", key === "go" && done.go);
      if (!next && !done[key]) next = key;
      attr(li, "data-next", next === key && !done[key]);
    }

    // 戒指
    text("ring-state", ring.connected ? ring.name + " 已連線" : ring.message);
    text("ring-battery", ring.battery == null ? "—" : ring.battery + "%" + (ring.charging ? " 充電中" : ""));
    text("ring-rate", !ring.connected ? "—" : ring.sampling ? ring.rate_hz.toFixed(1) + " Hz" : "暫停");
    text("ring-hw", ring.hardware || "—");
    text("ring-fw", ring.firmware || "—");
    axes($("axes"), ring.connected ? ring.raw : null);
    let touch = "觸控：—";
    if (s.studio && s.studio.ring_connected && !ring.connected) touch = "QRing Studio 正連著這顆戒指，先在那邊中斷連線";
    else if (ring.connected && !ring.touch) touch = "觸控：關閉";
    else if (ring.connected && ring.gesture) touch = "觸控：" + ring.gesture.name + "（" + new Date(ring.gesture.at * 1000).toLocaleTimeString("zh-TW", { hour12: false }) + "）";
    else if (ring.connected) touch = "觸控：開啟，還沒有事件";
    text("ring-touch", touch);
    text("ring-toggle", ring.connected ? "中斷戒指" : ring.busy ? "連線中…" : "連接戒指");
    enable("ring-toggle", !ring.busy);
    $("ring-toggle").classList.toggle("primary", !ring.connected);

    // 中間：兩根手指
    const value = ring.connected && curl.value != null ? curl.value : null;
    draw($("finger-you"), value, "you", null, value == null || !curl.valid);
    text("curl-you", percent(value));
    let robot = null, target = null;
    if (panel && panel.fingers) {
      const actual = FINGERS.filter((f) => s.options.fingers[f] && panel.fingers[f] && panel.fingers[f].actual)
        .map((f) => (panel.fingers[f].actual.flex + 30) / 120);
      if (actual.length) robot = clamp01(actual.reduce((a, b) => a + b, 0) / actual.length);
    }
    if (follow.phase === "following" && follow.targets) {
      const on = FINGERS.filter((f) => s.options.fingers[f]);
      target = clamp01(on.reduce((a, f) => a + (follow.targets[f].flex + 30) / 120, 0) / on.length);
    }
    draw($("finger-robot"), robot, "robot", target, robot == null);
    text("curl-robot", percent(robot));
    attr("flow", "data-live", follow.phase === "following" && !!curl.valid && !follow.stale);

    const reading = $("reading");
    let readingText = "", readingBad = false;
    if (!ring.connected) readingText = "先連接戒指";
    else if (follow.stale) { readingText = "戒指的資料中斷，手掌停在上一個位置"; readingBad = true; }
    else if (curl.issue) { readingText = curl.issue_text; readingBad = ["moving", "off_axis", "out_of_range", "invalid"].includes(curl.issue); }
    else if (curl.valid) readingText = "讀值正常，方向偏離 " + Math.abs(curl.off).toFixed(0) + "°";
    text(reading, readingText);
    attr(reading, "data-bad", readingBad);

    // 啟動開關
    const power = $("power");
    const now = Date.now();
    let pState = "off", title = "啟動跟隨", sub = "手掌會立刻跟著你的手動", pEnabled = true;
    if (follow.phase === "following") { pState = "on"; title = "跟隨中"; sub = "按一下停止，手掌會放鬆"; }
    else if (follow.phase !== "idle") { title = "切換中…"; sub = ""; pEnabled = false; }
    else if (follow.problem) { sub = follow.problem; pEnabled = false; confirmUntil = 0; }
    else if (now < confirmUntil) { pState = "confirm"; title = "再按一次確認"; sub = "手掌周圍淨空、一隻手放在伺服機電源開關旁"; }
    attr(power, "data-state", pState);
    attr(power, "aria-pressed", pState === "on");
    enable(power, pEnabled && !busy);
    text("power-title", title);
    text("power-sub", sub);
    const message = $("message");
    text(message, follow.message);
    attr(message, "data-kind", follow.message_kind);

    // 手掌
    let hs = "尚未連線";
    if (hand.linked && !hand.reachable) hs = hand.message === "正在連接手掌面板…" ? hand.message : "連不到手掌面板";
    else if (panel) hs = (panel.simulated ? "模擬手掌" : "實機") + " 已連線";
    text("hand-state", hs);
    text("hand-servos", panel ? panel.servos_ok + " / " + panel.servos_total : "—");
    text("hand-volt", panel && panel.voltage_min != null ? panel.voltage_min.toFixed(1) + " V" : "—");
    text("hand-temp", panel && panel.temperature_max != null ? panel.temperature_max.toFixed(0) + " °C" : "—");
    text("hand-torque", !panel ? "—" : panel.torque_on ? (panel.you_control ? "開，由這個 APP" : "開，由別的頁面") : "關");
    const box = $("fingers");
    if (!box.children.length) {
      for (const f of s.fingers) {
        const row = document.createElement("div");
        row.className = "finger";
        row.dataset.finger = f.key;
        row.innerHTML = "<span></span><div class=\"bar2\"><i></i><b hidden></b></div><output>—</output>";
        row.firstChild.textContent = f.label;
        box.appendChild(row);
      }
    }
    for (const row of box.children) {
      const f = panel && panel.fingers ? panel.fingers[row.dataset.finger] : null;
      const fill = row.querySelector("i"), mark = row.querySelector("b"), out = row.querySelector("output");
      if (f && f.actual) {
        fill.style.width = clamp01((f.actual.flex + 30) / 120) * 100 + "%";
        text(out, f.actual.flex.toFixed(0) + "°");
      } else { fill.style.width = "0"; text(out, "—"); }
      if (f && f.target) { mark.hidden = false; mark.style.left = clamp01((f.target.flex + 30) / 120) * 100 + "%"; }
      else mark.hidden = true;
    }
    const note = $("hand-note");
    let noteText = "", noteBad = false;
    if (hand.linked && !hand.reachable) { noteText = hand.message; noteBad = hand.message !== "正在連接手掌面板…"; }
    else if (panel && panel.fault) { noteText = "保護鎖定 " + panel.fault.reason_code + "：" + panel.fault.detail; noteBad = true; }
    else if (panel && panel.bus && panel.bus.state !== "attached") { noteText = panel.bus.detail || "面板還沒接上伺服機匯流排"; noteBad = panel.bus.state === "unavailable"; }
    else if (panel && panel.missing && panel.missing.length) { noteText = "沒有回應的伺服機：" + panel.missing.join("、"); noteBad = true; }
    else if (panel && panel.hold_remaining_s != null) noteText = "手掌停著不動，" + Math.ceil(panel.hold_remaining_s) + " 秒後面板會自動放鬆";
    else if (hand.url) noteText = hand.url.replace("http://", "");
    text(note, noteText);
    attr(note, "data-bad", noteBad);
    text("hand-toggle", hand.linked ? "中斷手掌" : "連接手掌");
    $("hand-toggle").classList.toggle("primary", !hand.linked);
    show("hand-clear", !!(panel && panel.fault));

    // 事件
    const list = $("events");
    const rows = s.events.slice(-12).reverse();
    const sig = rows.length ? rows[0].time + rows.length : "";
    if (list.dataset.sig !== sig) {
      list.dataset.sig = sig;
      list.textContent = "";
      for (const e of rows) {
        const li = document.createElement("li"), t = document.createElement("time");
        t.textContent = e.time.slice(11, 19);
        li.append(t, e.message);
        list.appendChild(li);
      }
    }
  }

  const vec = (v) => "(" + v.map((x) => (x >= 0 ? "+" : "") + x.toFixed(2)).join(", ") + ") g";
  function renderCalibration(s) {
    const cal = s.calibration, ring = s.ring, curl = s.follow.curl || {};
    const idle = s.follow.phase === "idle";
    show("cal-start", !cal.active);
    text("cal-start", cal.saved ? "重新校正" : "開始校正");
    enable("cal-start", ring.connected && idle && !busy);
    show("cal-cancel", cal.active);
    let message = cal.message;
    if (!cal.active) {
      if (!ring.connected) message = "先到「控制」連接戒指。";
      else if (!idle) message = "跟隨中不能校正；先停止。";
      else if (cal.saved) message = "已校正（" + cal.saved.saved_at.replace("T", " ").slice(0, 16) + "），張開與握拳相差 " + cal.saved.span_deg + "°。";
      else message = "還沒校正。戴好戒指，按「開始校正」。";
    }
    text("cal-message", message);

    const stable = cal.stable;
    for (const pose of ["open", "fist"]) {
      const card = $("pose-" + pose);
      const recorded = cal.active ? cal[pose] : cal.saved && cal.saved[pose];
      attr(card, "data-current", cal.active && cal.step === pose);
      attr(card, "data-done", !!recorded);
      const label = pose === "open" ? "張開" : "握拳";
      let st = "還沒記錄";
      if (recorded) st = "已記錄 " + vec(recorded);
      else if (cal.active && cal.step === pose) st = "擺好" + label + "的姿勢，停住";
      else if (cal.active) st = "等另一個姿勢記完";
      text(card.querySelector(".pose-state"), st);
      enable(card.querySelector(".capture"), cal.active && cal.step === pose && !!(stable && stable.stable) && !busy);
      show(card.querySelector(".capture"), !(cal.active && cal[pose]));
      show(card.querySelector(".retake"), cal.active && !!cal[pose]);
      draw(card.querySelector("svg"), pose === "open" ? 0 : 1, "you", null, false);
    }
    const steady = $("steady");
    let sState = "wait", sText = "等資料";
    if (!ring.connected) sText = "戒指未連線";
    else if (stable && stable.stable) { sState = "stable"; sText = "已停穩（散布 " + stable.spread + "°）"; }
    else if (stable && stable.issue === "moving") { sState = "moving"; sText = "還在動"; }
    else if (stable && stable.issue === "collecting") sText = "收集中…";
    attr(steady, "data-state", sState);
    text(steady.lastElementChild, sText);
    axes($("cal-axes"), ring.connected ? ring.raw : null);

    const meter = $("meter");
    const has = ring.connected && curl.value != null;
    meter.querySelector(".meter-fill").style.width = (has ? curl.value * 100 : 0) + "%";
    const rawMark = meter.querySelector(".meter-raw");
    rawMark.hidden = !(ring.connected && curl.progress != null);
    if (!rawMark.hidden) rawMark.style.left = clamp01(curl.progress) * 100 + "%";
    meter.querySelector(".lo").style.left = s.options.open_margin * 100 + "%";
    meter.querySelector(".hi").style.left = s.options.fist_margin * 100 + "%";
    text("meter-value", has ? percent(curl.value) : "—");
    let note = "校正後，這裡會即時顯示握拳程度。黑線是原始讀值，虛線是「設定」裡張開與握拳到底的界線。";
    if (cal.active && cal.step === "review") note = "張開、握拳幾次，確認兩端都到得了。沒問題就儲存；單擊戒指也可以。";
    else if (has && curl.issue) note = curl.issue_text;
    text("review-note", note);
    show("cal-save", cal.active && cal.step === "review");
    document.querySelector('nav [data-tab="calibration"]').classList.toggle("attention", ring.connected && !cal.saved && !cal.active);
  }

  // ────────────────────────────────────────────────── 設定
  const OPTION_IDS = ["auto_link", "speed", "fist_pose", "fist_scale", "mode", "smoothing", "open_margin", "fist_margin",
                      "deadband", "off_limit", "ring_touch", "tap_confirm", "long_press_stop", "long_press_start"];
  function labelFor(input) {
    const out = document.querySelector('output[data-for="' + input.id + '"]');
    if (!out) return;
    const v = parseFloat(input.value);
    text(out, "percent" in input.dataset ? Math.round(v * 100) + "%" : v + (input.dataset.unit || ""));
  }
  function setOption(key, value) {
    dirty[key] = Date.now();
    api("/api/options", { [key]: value }).catch((e) => { toast(e.message, true); delete dirty[key]; }).then(refresh);
  }
  function bindSettings() {
    for (const key of OPTION_IDS) {
      const el = $("opt-" + key);
      if (el.type === "range") el.addEventListener("input", () => { dirty[key] = Date.now(); labelFor(el); });
      el.addEventListener("change", () => setOption(key, el.type === "checkbox" ? el.checked : el.type === "range" ? parseFloat(el.value) : el.value));
    }
    $("hand-link").addEventListener("click", () => act("/api/hand/link", { url: $("opt-hand_url").value }, () => { delete $("opt-hand_url").dataset.touched; toast("正在連接手掌面板"); }));
    $("opt-hand_url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("hand-link").click(); });
  }
  function renderSettings(s) {
    const o = s.options, now = Date.now();
    const boxes = $("opt-fingers");
    if (!boxes.children.length) {
      for (const f of s.fingers) {
        const label = document.createElement("label"), input = document.createElement("input");
        input.type = "checkbox";
        input.dataset.finger = f.key;
        input.addEventListener("change", () => { dirty.fingers = Date.now(); api("/api/options", { fingers: { [f.key]: input.checked } }).catch((e) => toast(e.message, true)).then(() => { delete dirty.fingers; refresh(); }); });
        label.append(input, f.label);
        boxes.appendChild(label);
      }
    }
    if (!dirty.fingers) for (const input of boxes.querySelectorAll("input")) input.checked = !!o.fingers[input.dataset.finger];

    const select = $("opt-fist_pose");
    const names = (s.hand.poses || []).filter((p) => p.playable).map((p) => p.name);
    const sig = names.join(",") + "|" + o.fist_pose;
    if (select.dataset.sig !== sig && document.activeElement !== select) {
      select.dataset.sig = sig;
      select.textContent = "";
      select.add(new Option("內建（四指彎曲 85°）", "builtin"));
      for (const p of s.hand.poses || []) if (p.playable) select.add(new Option(p.name + (p.label ? "（" + p.label + "）" : ""), p.name));
      if (o.fist_pose !== "builtin" && !names.includes(o.fist_pose)) select.add(new Option(o.fist_pose + "（面板上找不到）", o.fist_pose));
    }
    for (const key of OPTION_IDS) {
      const el = $("opt-" + key);
      if (document.activeElement === el || (dirty[key] && now - dirty[key] < 1500)) continue;
      delete dirty[key];
      if (el.type === "checkbox") el.checked = !!o[key];
      else if (el.value !== String(o[key])) el.value = o[key];
      if (el.type === "range") labelFor(el);
    }
    const url = $("opt-hand_url");
    if (document.activeElement !== url && !url.dataset.touched) url.value = (o.hand_url || "").replace("http://", "");
    const locked = s.follow.phase !== "idle";
    for (const id of ["opt-speed", "opt-fist_pose", "opt-hand_url", "hand-link"]) enable(id, !locked);
    $("opt-tap_confirm").disabled = $("opt-long_press_stop").disabled = $("opt-long_press_start").disabled = !o.ring_touch;
  }

  function render(s) {
    state = s;
    text("version", s.app.version);
    renderControl(s);
    renderCalibration(s);
    renderSettings(s);
  }

  // ────────────────────────────────────────────────── 輪詢
  let timer = 0, loading = false;
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      render(await r.json());
      if (offline) { offline = false; $("toast").hidden = true; }
    } catch (e) {
      if (!offline) { offline = true; toast("本機服務沒有回應。若持續出現，請結束 QRing Hand 再重新開啟", true); }
    } finally {
      loading = false;
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (document.hidden) return;          // 視窗沒在看：不輪詢，戒指也就暫停讀三軸
    timer = setTimeout(async () => { await refresh(); schedule(); }, POLL_MS);
  }
  document.addEventListener("visibilitychange", schedule);

  // ────────────────────────────────────────────────── 操作
  window.switchTab = (name) => {
    for (const b of document.querySelectorAll("nav button")) attr(b, "aria-pressed", b.dataset.tab === name);
    for (const t of document.querySelectorAll(".tab")) t.hidden = t.id !== "tab-" + name;
  };
  function stop() {
    confirmUntil = 0;
    api("/api/follow/stop").then((d) => toast(d.confirmed ? "已停止，手掌扭力已關" : "停止已送出，但面板沒有確認。請確認手已放鬆", !d.confirmed))
      .catch((e) => toast(e.message, true)).then(refresh);
  }
  function bind() {
    for (const b of document.querySelectorAll("nav button")) b.addEventListener("click", () => window.switchTab(b.dataset.tab));
    $("stop").addEventListener("click", stop);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state && state.hand.linked) stop(); });
    $("ring-toggle").addEventListener("click", () => act(state.ring.connected ? "/api/ring/disconnect" : "/api/ring/connect"));
    $("hand-toggle").addEventListener("click", () => {
      if (state.hand.linked) return act("/api/hand/unlink");
      if (!state.options.hand_url) { window.switchTab("settings"); $("opt-hand_url").focus(); toast("先填手掌面板的位址"); return; }
      act("/api/hand/link", {});
    });
    $("hand-clear").addEventListener("click", () => act("/api/hand/clear-fault", {}, () => toast("已解除鎖定")));
    $("power").addEventListener("click", () => {
      if (!state) return;
      if (state.follow.phase === "following") return stop();
      if (Date.now() < confirmUntil) { confirmUntil = 0; return act("/api/follow/start"); }
      confirmUntil = Date.now() + 4000;
      render(state);
    });
    $("cal-start").addEventListener("click", () => act("/api/calibration/start"));
    $("cal-cancel").addEventListener("click", () => act("/api/calibration/cancel"));
    $("cal-save").addEventListener("click", () => act("/api/calibration/save", {}, () => toast("校正已儲存")));
    for (const b of document.querySelectorAll(".capture")) b.addEventListener("click", () => act("/api/calibration/capture", { pose: b.dataset.pose }));
    for (const b of document.querySelectorAll(".retake")) b.addEventListener("click", () => act("/api/calibration/retake", { pose: b.dataset.pose }));
    $("opt-hand_url").addEventListener("input", (e) => { e.target.dataset.touched = "1"; });
    bindSettings();
  }

  bind();
  refresh().then(schedule);
})();
