(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const FINGERS = ["thumb", "index", "middle", "ring"];
  const NAMES = { thumb: "拇指", index: "食指", middle: "中指", ring: "無名指" };
  const POLL_MS = 250;
  const SEND_MS = 120;
  const RELEASE_TEXT = {
    watchdog: "控制的頁面太久沒有回報，面板已自動關扭力。",
    hold_timeout: "閒置太久，面板已自動關扭力。要繼續就再啟用一次。",
    enable_timeout: "啟用扭力花太久，面板已關扭力。",
  };

  function randomId() {
    const a = new Uint8Array(12);
    crypto.getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  const CLIENT = (() => {
    try {
      let c = sessionStorage.getItem("hand_panel_client");
      if (!c) { c = randomId(); sessionStorage.setItem("hand_panel_client", c); }
      return c;
    } catch (e) { return randomId(); }
  })();

  let st = null;            // 最近一次狀態
  let fails = 0;
  let poses = [];
  const sliders = {};       // 手指 → {flex, side}
  const reads = {};         // 手指 → 顯示數字的節點
  let pending = {};         // 還沒送出的目標
  let sendTimer = null;
  let sending = false;
  let overwriteName = null;

  // ------------------------------------------------------------------ 小工具
  let toastTimer = null;
  function toast(text, bad) {
    const t = $("#toast");
    t.textContent = text;
    t.classList.toggle("bad", !!bad);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3000);
  }

  async function post(path, body) {
    try {
      const r = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ client: CLIENT }, body || {})), keepalive: path === "/api/stop",
      });
      return await r.json();
    } catch (e) {
      return { status: "error", reason_code: "NETWORK", detail: "連不到面板，這個動作沒有送出去。" };
    }
  }

  function failed(res) {
    if (res.status !== "rejected" && res.status !== "error") return false;
    toast(res.detail || res.reason_code, true);
    return true;
  }

  // 第一次按只換文字，幾秒內再按一次才算數。
  function confirmed(btn, text) {
    if (btn.classList.contains("confirm")) {
      clearTimeout(btn._timer);
      btn.classList.remove("confirm");
      btn.textContent = btn.dataset.label;
      return true;
    }
    btn.dataset.label = btn.textContent;
    btn.textContent = text;
    btn.classList.add("confirm");
    btn._timer = setTimeout(() => {
      btn.classList.remove("confirm");
      btn.textContent = btn.dataset.label;
    }, 4000);
    return false;
  }

  const deg = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "°");
  const inControl = () => !!(st && st.torque_on && st.you_control);

  // ------------------------------------------------------------------ 滑桿
  class Slider {
    constructor(node, opt) {
      Object.assign(this, { node, min: opt.min, max: opt.max, vertical: opt.vertical, onInput: opt.onInput });
      this.value = null;
      this.dragging = false;
      this.holdUntil = 0;
      node.setAttribute("role", "slider");
      node.setAttribute("aria-label", opt.label);
      node.setAttribute("aria-orientation", opt.vertical ? "vertical" : "horizontal");
      node.setAttribute("aria-valuemin", opt.min);
      node.setAttribute("aria-valuemax", opt.max);
      node.tabIndex = 0;
      for (const t of opt.ticks || []) {
        const tick = el("span", "tick");
        if (opt.vertical) tick.style.top = this.frac(t) * 100 + "%";
        if (t === 0) tick.style.width = "0.9rem";
        node.append(tick);
      }
      this.fill = node.appendChild(el("span", "fill"));
      this.meas = node.appendChild(el("span", "meas"));
      this.handle = node.appendChild(el("span", "handle"));
      this.meas.hidden = true;
      this.setDisabled(true);
      node.addEventListener("pointerdown", (e) => {
        if (this.disabled) { toast(st && st.torque_on ? "這隻手現在由另一個頁面控制。" : "扭力關著。先按「啟用扭力」。"); return; }
        try { node.setPointerCapture(e.pointerId); } catch (err) { /* 抓不到指標時仍可拖，只是離開長條就停 */ }
        this.dragging = true;
        node.classList.add("live");
        this.fromPointer(e);
      });
      node.addEventListener("pointermove", (e) => { if (this.dragging) this.fromPointer(e); });
      const end = () => {
        if (!this.dragging) return;
        this.dragging = false;
        this.holdUntil = performance.now() + 700;
        node.classList.remove("live");
      };
      node.addEventListener("pointerup", end);
      node.addEventListener("pointercancel", end);
      node.addEventListener("keydown", (e) => {
        const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1, PageDown: 10, PageUp: -10 }[e.key];
        let v = null;
        if (step !== undefined) v = (this.value === null ? 0 : this.value) + step;
        else if (e.key === "Home") v = this.min;
        else if (e.key === "End") v = this.max;
        else return;
        e.preventDefault();
        if (this.disabled) return;
        this.holdUntil = performance.now() + 700;
        this.user(Math.min(this.max, Math.max(this.min, v)));
      });
    }
    frac(v) { return Math.min(1, Math.max(0, (v - this.min) / (this.max - this.min))); }
    fromPointer(e) {
      const r = this.node.getBoundingClientRect();
      const f = this.vertical ? (e.clientY - r.top) / r.height : (e.clientX - r.left) / r.width;
      this.user(Math.round(this.min + Math.min(1, Math.max(0, f)) * (this.max - this.min)));
    }
    user(v) {
      if (v === this.value) return;
      this.show(v);
      this.onInput(v);
    }
    show(v) {
      this.value = v;
      const p = this.frac(v) * 100;
      this.node.setAttribute("aria-valuenow", Math.round(v));
      if (this.vertical) {
        this.handle.style.top = p + "%";
        this.fill.style.height = p + "%";
      } else {
        this.handle.style.left = p + "%";
        this.fill.style.left = Math.min(50, p) + "%";
        this.fill.style.width = Math.abs(p - 50) + "%";
      }
    }
    setTarget(v) {            // 伺服器回報的目標；使用者正在拖的時候不蓋掉
      if (v === null || this.dragging || performance.now() < this.holdUntil) return;
      this.show(v);
    }
    setMeasured(v) {
      this.meas.hidden = v === null;
      if (v === null) return;
      this.meas.style[this.vertical ? "top" : "left"] = this.frac(v) * 100 + "%";
    }
    setDisabled(d) {
      this.disabled = d;
      this.node.setAttribute("aria-disabled", d ? "true" : "false");
    }
  }

  function queue(finger, axis, v) {
    pending[finger] = Object.assign(pending[finger] || {}, { [axis]: v });
    reads[finger][axis].t.textContent = deg(v);
    if (!sendTimer) sendTimer = setTimeout(flush, SEND_MS);
  }

  async function flush() {
    sendTimer = null;
    if (sending) { sendTimer = setTimeout(flush, SEND_MS); return; }
    const body = pending;
    pending = {};
    if (!Object.keys(body).length) return;
    sending = true;
    const res = await post("/api/fingers", { fingers: body });
    sending = false;
    failed(res);
  }

  function buildFingers(limits) {
    const root = $("#fingers");
    for (const key of FINGERS) {
      const box = el("section", "finger");
      box.dataset.finger = key;
      box.append(el("h3", "", NAMES[key]));
      const ids = box.appendChild(el("p", "ids", ""));
      const v = box.appendChild(el("div", "track v"));
      const flexRead = readout(box, "彎曲");
      const h = box.appendChild(el("div", "track h"));
      const sideRead = readout(box, "側擺");
      const open = box.appendChild(el("button", "quiet", "張開"));
      open.type = "button";
      open.disabled = true;
      open.addEventListener("click", async () => {
        failed(await post("/api/fingers", { fingers: { [key]: { flex: limits.flex[0], side: 0 } } }));
      });
      sliders[key] = {
        flex: new Slider(v, { min: limits.flex[0], max: limits.flex[1], vertical: true, ticks: [0, 30, 60],
                              label: NAMES[key] + "彎曲（度）", onInput: (x) => queue(key, "flex", x) }),
        side: new Slider(h, { min: limits.side[0], max: limits.side[1], vertical: false, ticks: [0],
                              label: NAMES[key] + "側擺（度）", onInput: (x) => queue(key, "side", x) }),
      };
      reads[key] = { flex: flexRead, side: sideRead, ids, open };
      root.append(box);
    }
  }

  function readout(box, name) {
    const dl = box.appendChild(el("dl", "read"));
    dl.append(el("dt", "", name));
    const dd = dl.appendChild(el("dd"));
    const t = dd.appendChild(el("span", "t", "—"));
    dd.append(document.createElement("br"));
    const a = dd.appendChild(el("span", "a", "—"));
    return { t, a };
  }

  // ------------------------------------------------------------------ 畫面
  function rigText() {
    const kind = st.simulated ? "模擬" : "實機";
    if (st.bus.state === "unavailable") return kind + "：" + (st.bus.detail || "拿不到匯流排");
    if (st.bus.state !== "attached" || st.reading_age_ms === null) return kind + "：連線中…";
    const ok = st.servos.filter((s) => s.ok);
    if (!ok.length) return kind + "：8 顆都沒有回應。伺服機電源關著，或線沒接好。";
    if (ok.length < 8) return kind + "：ID " + st.servos.filter((s) => !s.ok).map((s) => s.id).join("、") + " 沒有回應。";
    const volts = ok.map((s) => s.voltage_v);
    const temp = Math.max(...ok.map((s) => s.temperature_c));
    const lo = Math.min(...volts).toFixed(1), hi = Math.max(...volts).toFixed(1);
    return kind + "：8 顆都有回應，" + (lo === hi ? lo : lo + "–" + hi) + " V，最高 " + temp.toFixed(0) + " °C";
  }

  function render() {
    if (!Object.keys(sliders).length) buildFingers(st.limits);
    $("#rig").textContent = rigText();
    const ready = st.bus.state === "attached" && st.servos.every((s) => s.ok);
    const mine = inControl();

    // 故障與自動關扭力的說明
    const notice = $("#notice");
    const key = st.fault ? "fault:" + st.fault.at : (!st.torque_on && st.last_release && RELEASE_TEXT[st.last_release.reason]
      ? "rel:" + st.last_release.at : "");
    if (notice.dataset.key !== key) {
      notice.dataset.key = key;
      notice.replaceChildren();
      notice.hidden = !key;
      notice.classList.toggle("info", !st.fault);
      if (st.fault) {
        notice.append(el("h2", "", "已停止並鎖住"));
        const p = notice.appendChild(el("p"));
        p.append(el("code", "", st.fault.reason_code), "　" + st.fault.detail);
        notice.append(el("p", "", "面板已送出關扭力。先看手的狀況、排除原因；不確定伺服機有沒有還在出力時，切斷伺服機電源。"));
        const b = notice.appendChild(el("button", "quiet", "解除鎖定"));
        b.type = "button";
        b.addEventListener("click", async () => { failed(await post("/api/clear_fault")); });
      } else if (key) {
        notice.append(el("p", "", RELEASE_TEXT[st.last_release.reason]));
      }
    }

    // 扭力
    const power = $(".power");
    power.classList.toggle("on", st.torque_on);
    let text;
    if (st.fault) text = "已鎖住。先處理上面的故障，再解除鎖定。";
    else if (mine && st.moving) text = "扭力開著，手指移動中。";
    else if (mine) text = "扭力開著，由這一頁控制。再 " + Math.ceil(st.hold_remaining_s || 0) + " 秒沒有新動作就自動關扭力。";
    else if (st.torque_on) text = "扭力開著，由另一個頁面控制。這一頁只能看，以及按停止。";
    else if (!ready) text = "扭力關著。8 顆伺服機都有回應之後才能啟用。";
    else text = "扭力關著，手指是鬆的。啟用後，滑桿從手指現在的位置開始，不會跳動。";
    $("#power-state").textContent = text;
    const enable = $("#enable");
    if (!enable.classList.contains("confirm")) {
      enable.textContent = mine ? "關扭力" : "啟用扭力";
      enable.classList.toggle("quiet", mine);
      enable.classList.toggle("go", !mine);
    }
    enable.disabled = mine ? false : (st.torque_on || !!st.fault || !ready);
    if (st.torque_on) {
      const radio = $('#speed input[value="' + st.speed + '"]');
      if (radio && !radio.checked && document.activeElement !== radio) radio.checked = true;
    }
    for (const r of document.querySelectorAll("#speed input")) r.disabled = st.torque_on && !mine;
    $("#open-all").disabled = !mine;

    // 手指
    for (const k of FINGERS) {
      const f = st.fingers[k];
      const s = sliders[k];
      const rd = reads[k];
      rd.ids.textContent = "ID " + f.servo_ids.join("、");
      rd.open.disabled = !mine;
      for (const axis of ["flex", "side"]) {
        s[axis].setDisabled(!mine);
        s[axis].setTarget(mine && f.target ? f.target[axis] : null);
        s[axis].setMeasured(f.actual ? f.actual[axis] : null);
        if (!s[axis].dragging && performance.now() >= s[axis].holdUntil) {
          rd[axis].t.textContent = st.torque_on && f.target ? deg(f.target[axis]) : "沒有目標";
          rd[axis].t.classList.toggle("none", !(st.torque_on && f.target));
        }
        rd[axis].a.textContent = f.actual ? deg(f.actual[axis]) : "讀不到";
      }
    }

    // 姿勢
    $("#save-hint").textContent = st.torque_on ? "會存下現在的目標角度。" : "扭力關著：會存下現在讀到的位置。";
    for (const b of document.querySelectorAll("#pose-list .play")) b.disabled = !mine || b.dataset.playable !== "1";

    // 伺服機表
    const rows = $("#servo-rows");
    rows.replaceChildren(...st.servos.map((s) => {
      const tr = el("tr");
      tr.append(el("td", "", String(s.id)));
      if (!s.ok) {
        const td = el("td", "bad", "沒有回應");
        td.colSpan = 4;
        tr.append(td);
      } else {
        tr.append(el("td", "", deg(s.position_deg === undefined ? null : s.position_deg)),
                  el("td", "", s.voltage_v.toFixed(1) + " V"), el("td", "", s.temperature_c.toFixed(0) + " °C"),
                  el("td", "", s.torque_on ? "開" : "關"));
      }
      return tr;
    }));
  }

  function offline(text) {
    $("#rig").textContent = text;
    $("#power-state").textContent = "面板沒有回應時，這一頁送不出任何指令。面板超過 3 秒收不到這一頁的回報，會自己關扭力。";
    $("#enable").disabled = true;
    $("#open-all").disabled = true;
    for (const k of Object.keys(sliders)) { sliders[k].flex.setDisabled(true); sliders[k].side.setDisabled(true); }
  }

  async function poll() {
    try {
      const r = await fetch("/api/status?client=" + CLIENT, { cache: "no-store" });
      if (r.status === 401) {
        offline("這個面板現在要存取碼。請用含存取碼的網址重新開啟這一頁。");
      } else {
        st = await r.json();
        fails = 0;
        render();
      }
    } catch (e) {
      fails += 1;
      if (fails >= 3) offline("連不到面板。");
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  // ------------------------------------------------------------------ 姿勢清單
  function poseItem(p) {
    const li = el("li", "pose");
    const head = li.appendChild(el("div", "pose-head"));
    head.append(el("strong", "", p.label || p.name), el("code", "", p.name));
    let meta = "手勢表（gestures.yaml）裡已校正的手勢";
    if (p.kind === "saved") {
      const when = p.saved_at ? new Date(p.saved_at).toLocaleString("zh-TW", { hour12: false }) : "";
      meta = (p.source === "measured" ? "讀到的位置" : "目標角度") + (when ? "，存於 " + when : "");
    }
    li.append(el("p", "pose-meta", p.note ? meta + "。" + p.note : meta));
    const dl = li.appendChild(el("dl", "pose-angles"));
    for (const k of FINGERS) {
      const cell = dl.appendChild(el("div"));
      cell.append(el("dt", "", NAMES[k]), el("dd", "", "彎 " + p.fingers[k].flex.toFixed(0) + "　側 " + p.fingers[k].side.toFixed(0)));
    }
    if (!p.playable) li.append(el("p", "pose-why", p.why_not));
    const act = li.appendChild(el("div", "pose-act"));
    const play = act.appendChild(el("button", "go play", "重現"));
    play.type = "button";
    play.dataset.playable = p.playable ? "1" : "0";
    play.disabled = !inControl() || !p.playable;
    play.addEventListener("click", async () => {
      const res = await post("/api/poses/play", { name: p.name });
      if (!failed(res)) toast("正在擺出「" + (p.label || p.name) + "」。");
    });
    if (p.kind === "saved") {
      if (p.reason_code === "CALIBRATION_REVISION_CHANGED") {
        const again = act.appendChild(el("button", "quiet", "重新確認"));
        again.type = "button";
        again.addEventListener("click", async () => {
          if (!confirmed(again, "確認：這個姿勢在新的校正下仍然正確")) return;
          if (!failed(await post("/api/poses/reconfirm", { name: p.name }))) loadPoses();
        });
      }
      const del = act.appendChild(el("button", "quiet danger", "刪除"));
      del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirmed(del, "確認刪除")) return;
        if (!failed(await post("/api/poses/delete", { name: p.name }))) { toast("已刪除 " + p.name + "。"); loadPoses(); }
      });
    }
    return li;
  }

  async function loadPoses() {
    let data;
    try {
      data = await (await fetch("/api/poses", { cache: "no-store" })).json();
    } catch (e) { return; }
    if (data.status !== "ok") return;
    const sig = JSON.stringify([data.poses, data.error]);
    if (sig === loadPoses.sig) return;
    loadPoses.sig = sig;
    poses = data.poses;
    const err = $("#poses-error");
    err.hidden = !data.error;
    err.textContent = data.error ? "姿勢檔讀不了，修好之前不能存也不能重現存下的姿勢：" + data.error : "";
    const list = $("#pose-list");
    list.replaceChildren(...poses.map(poseItem));
    if (!poses.some((p) => p.kind === "saved") && !data.error) {
      list.append(el("li", "empty", "還沒有存下的姿勢。把手指擺到想要的位置，取個名稱，按「存下目前姿勢」。"));
    }
  }

  // ------------------------------------------------------------------ 紀錄
  function logText(e) {
    const d = e.data || {};
    switch (e.type) {
      case "torque.on": return "啟用扭力（" + ({ slow: "慢", normal: "中", fast: "快" }[d.speed] || d.speed) + "）";
      case "torque.off": return "關扭力：" + ({ stop: "按了停止", watchdog: "頁面沒有回報", hold_timeout: "閒置太久", fault: "檢查沒過",
        shutdown: "面板結束", enable_timeout: "啟用逾時" }[d.reason] || d.reason) + (d.torque_off_failed && d.torque_off_failed.length ? "（ID " + d.torque_off_failed.join("、") + " 沒有成功）" : "");
      case "torque.rejected": return "啟用被拒：" + d.detail;
      case "move.finished": return d.ok ? "移動完成（" + d.rounds + " 輪，" + d.elapsed_s + " 秒）" : "移動失敗：" + d.reason_code + "　" + d.detail;
      case "pose.saved": return (d.overwrite ? "覆蓋姿勢 " : "存下姿勢 ") + d.name;
      case "pose.play": return "重現姿勢 " + d.name;
      case "pose.deleted": return "刪除姿勢 " + d.name;
      case "pose.reconfirmed": return "重新確認姿勢 " + d.name;
      case "fault.cleared": return "解除鎖定";
      case "panel.started": return "面板啟動（" + d.adapter + "）";
      case "panel.stopped": return "面板結束";
      default: return e.type;
    }
  }

  async function loadLog() {
    if (!$("#more").open) return;
    try {
      const data = await (await fetch("/api/log?n=15", { cache: "no-store" })).json();
      $("#log").replaceChildren(...data.events.reverse().map((e) => {
        const li = el("li");
        const t = el("time", "", new Date(e.at).toLocaleTimeString("zh-TW", { hour12: false }));
        li.append(t, logText(e));
        return li;
      }));
    } catch (e) { /* 下一輪再試 */ }
  }

  // ------------------------------------------------------------------ 事件
  async function stop() {
    pending = {};
    const res = await post("/api/stop");
    if (res.status === "accepted" && res.torque_off === "ok") toast("已關扭力。");
    else toast((res.detail || "送不出關扭力。") + " 請切斷伺服機電源。", true);
  }
  $("#stop").addEventListener("click", stop);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") stop(); });

  $("#enable").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (inControl()) { stop(); return; }
    if (!confirmed(btn, "確認啟用：手放在電源開關上")) return;
    const speed = $("#speed input:checked").value;
    const res = await post("/api/enable", { speed });
    if (!failed(res)) toast("扭力已啟用。");
  });

  $("#speed").addEventListener("change", async (e) => {
    if (inControl()) failed(await post("/api/speed", { speed: e.target.value }));
  });

  $("#open-all").addEventListener("click", async () => { failed(await post("/api/open")); });

  $("#pose-name").addEventListener("input", () => {
    overwriteName = null;
    $("#save-btn").textContent = "存下目前姿勢";
  });

  $("#save").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#pose-name").value.trim();
    const label = $("#pose-label").value.trim();
    const res = await post("/api/poses/save", { name, label, overwrite: overwriteName === name });
    if (res.status === "rejected" && res.reason_code === "NAME_TAKEN" && poses.some((p) => p.name === name && p.kind === "saved")) {
      overwriteName = name;
      $("#save-btn").textContent = "覆蓋「" + name + "」";
      toast("已經有 " + name + "。再按一次就覆蓋。", true);
      return;
    }
    overwriteName = null;
    $("#save-btn").textContent = "存下目前姿勢";
    if (failed(res)) return;
    toast("已存下 " + name + "。");
    $("#pose-name").value = "";
    $("#pose-label").value = "";
    loadPoses();
  });

  $("#more").addEventListener("toggle", loadLog);
  window.addEventListener("pagehide", () => { if (inControl()) post("/api/stop"); });

  poll();
  loadPoses();
  setInterval(loadPoses, 10000);
  setInterval(loadLog, 4000);
})();
