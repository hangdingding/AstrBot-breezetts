const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const UTM = "utm_source=deepseek&utm_medium=plugin&utm_campaign=deepseek_harness";
const GIFT_URL = `https://breezeblue.ai/zh/app/subscription?gift=BREEZEBLUE&code=GH3HVA&${UTM}&utm_content=astrbot_subscription`;
const KEY_URL = `https://breezeblue.ai/zh/app/developer/api-keys?code=GH3HVA&${UTM}&utm_content=astrbot_api_keys`;

const $ = (id) => document.getElementById(id);
const state = {
  settings: null,
  voices: [],
  search: "",
  playing: null,
  design: { previews: [], selectedId: "", playing: null, audio: null },
};

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2000);
}

async function copyText(text, message) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      toast(message);
      return;
    }
  } catch {}
  // 沙箱 iframe 里 clipboard API 常被拦截，退回 execCommand
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.top = "-1000px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.focus();
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {}
  area.remove();
  toast(ok ? message : "复制失败，请手动选择");
}

function initials(name) {
  const value = String(name || "?").trim();
  return value ? Array.from(value)[0] : "?";
}

function formatBalance(value) {
  if (value === null || value === undefined) return "—";
  return Math.round(Number(value)).toLocaleString();
}

async function apiGet(endpoint, params) {
  return bridge.apiGet(endpoint, params);
}
async function apiPost(endpoint, body) {
  return bridge.apiPost(endpoint, body);
}

/* ---------------- settings ---------------- */

async function loadSettings() {
  state.settings = await apiGet("settings");
  renderSettings();
}

function updateProbabilityState() {
  const smart = !!state.settings?.smartVoice;
  const slider = $("voiceProbability");
  if (!slider) return;
  slider.disabled = smart;
  const hint = $("probHint");
  if (hint) {
    hint.textContent = smart
      ? "智能判断已开启，概率不生效（两者互斥）"
      : "0 = 全部文字 · 100 = 全部语音";
  }
}

function renderSettings() {
  const s = state.settings;
  if (!s) return;

  $("balance").textContent = formatBalance(s.balance);
  $("keyHint").textContent = s.hasApiKey
    ? `已保存 ${s.apiKeyHint} · 留空表示不修改`
    : "还没有填写，粘贴 brz_ 开头的密钥";
  $("getKey").classList.toggle("hidden", !!s.hasApiKey);
  $("search").disabled = !s.hasApiKey;
  $("search").placeholder = s.hasApiKey ? "搜索音色（名称或描述）" : "请先设置 API Key";
  $("voiceProbability").value = String(s.voiceProbability ?? 100);
  $("probVal").textContent = `${Number(s.voiceProbability ?? 100)}%`;
  $("smartVoice").checked = !!s.smartVoice;
  updateProbabilityState();
  $("speed").value = String(s.speed ?? 1);
  $("speedVal").textContent = `${Number(s.speed ?? 1).toFixed(2)}×`;

  const current = $("currentVoice");
  current.innerHTML = "";
  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.textContent = initials(s.voiceName || s.voiceId);
  const body = document.createElement("div");
  body.className = "currentBody";
  const name = document.createElement("span");
  name.className = "currentName";
  name.textContent = s.voiceName || s.voiceId || "未选择音色";
  const desc = document.createElement("span");
  desc.className = "currentDesc";
  desc.textContent = s.hasApiKey ? s.voiceId : "请先设置 API Key";
  body.append(name, desc);
  current.append(avatar, body);
}

async function saveSettings(patch, message = "已保存") {
  state.settings = await apiPost("settings/save", patch);
  renderSettings();
  if (message) toast(message);
}

/* ---------------- voices ---------------- */

async function loadVoices() {
  if (!state.settings?.hasApiKey) {
    $("voiceList").innerHTML = '<p class="hint">请先设置 API Key</p>';
    return;
  }
  $("voiceList").innerHTML = '<p class="hint">加载中…</p>';
  try {
    const data = await apiGet("voices", { search: state.search, limit: 8 });
    state.voices = data.voices || [];
    renderVoices();
  } catch (error) {
    $("voiceList").innerHTML = `<p class="hint">${error.message}</p>`;
  }
}

function renderVoices() {
  const list = $("voiceList");
  list.innerHTML = "";
  if (!state.voices.length) {
    list.innerHTML = state.settings?.hasApiKey
      ? '<p class="hint">没有找到音色</p>'
      : '<p class="hint">请先设置 API Key</p>';
    return;
  }
  const currentId = state.settings?.voiceId || "";
  for (const voice of state.voices) {
    const row = document.createElement("div");
    row.className = `voiceRow${voice.id === currentId ? " selected" : ""}`;

    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = initials(voice.name);

    const body = document.createElement("div");
    body.className = "voiceBody";
    const name = document.createElement("span");
    name.className = "voiceName";
    name.textContent = voice.name;
    const meta = document.createElement("span");
    meta.className = "voiceMeta";
    meta.textContent = [voice.language, voice.description].filter(Boolean).join(" · ");
    body.append(name, meta);

    const preview = document.createElement("button");
    preview.className = "linkBtn";
    preview.textContent = "试听";
    preview.addEventListener("click", () => playPreview(voice.previewUrl));

    const use = document.createElement("button");
    use.className = "btnGhost";
    use.textContent = voice.id === currentId ? "已选" : "使用";
    use.disabled = voice.id === currentId;
    use.addEventListener("click", async () => {
      use.disabled = true;
      try {
        await saveSettings({ voiceId: voice.id }, "已切换音色");
        await loadVoices();
      } catch (error) {
        toast(error.message);
        use.disabled = false;
      }
    });

    row.append(avatar, body, preview, use);
    list.append(row);
  }
}

function playPreview(url) {
  if (!url) return;
  if (state.playing) {
    state.playing.pause();
    state.playing = null;
  }
  const audio = new Audio(url);
  state.playing = audio;
  audio.addEventListener("ended", () => {
    if (state.playing === audio) state.playing = null;
  });
  audio.play().catch(() => toast("试听失败"));
}

/* ---------------- design ---------------- */

const DESIGN_DEFAULT = {
  description: "温暖低沉的女声，语速从容，带一点轻微的沙哑",
  text: "你好，很高兴认识你。这是我用新音色读的第一句话。",
  name: "",
};

function openDesign() {
  $("modal").classList.remove("hidden");
  $("designDesc").value = DESIGN_DEFAULT.description;
  $("designText").value = DESIGN_DEFAULT.text;
  $("designName").value = "";
  $("designCount").textContent = `${DESIGN_DEFAULT.description.length}/500`;
  $("designResults").classList.add("hidden");
  $("previewList").innerHTML = "";
  $("designNote").textContent = "";
  state.design.previews = [];
  state.design.selectedId = "";
  stopDesignAudio();
}

function closeDesign() {
  stopDesignAudio();
  $("modal").classList.add("hidden");
}

function stopDesignAudio() {
  if (state.design.audio) {
    try {
      state.design.audio.pause();
    } catch {}
    state.design.audio = null;
  }
  state.design.playing = null;
  renderPreviews();
}

async function generateDesign() {
  const description = $("designDesc").value.trim();
  if (description.length < 3) {
    toast("音色描述至少 3 个字");
    return;
  }
  const button = $("generate");
  button.disabled = true;
  button.textContent = "生成中…";
  $("designNote").textContent = "";
  try {
    const data = await apiPost("design", {
      description,
      text: $("designText").value.trim(),
      language: state.settings?.language || "zh",
    });
    state.design.previews = data.previews || [];
    state.design.selectedId = state.design.previews[0]?.id || "";
    $("designResults").classList.remove("hidden");
    renderPreviews();
    if (state.design.selectedId) playDesign(state.design.selectedId);
  } catch (error) {
    $("designNote").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "生成 3 个预览";
  }
}

function renderPreviews() {
  const list = $("previewList");
  list.innerHTML = "";
  state.design.previews.forEach((preview, index) => {
    const row = document.createElement("div");
    row.className = `previewRow${preview.id === state.design.selectedId ? " selected" : ""}`;
    row.addEventListener("click", () => {
      state.design.selectedId = preview.id;
      renderPreviews();
      if (state.design.playing !== preview.id) playDesign(preview.id);
    });

    const radio = document.createElement("span");
    radio.className = "radio";
    const name = document.createElement("span");
    name.className = "voiceName";
    name.textContent = `音色 ${index + 1}`;
    name.style.flex = "1";
    const duration = document.createElement("span");
    duration.className = "voiceMeta";
    duration.textContent = preview.duration ? `${Number(preview.duration).toFixed(1)}s` : "";

    const play = document.createElement("button");
    play.className = "linkBtn";
    play.textContent = state.design.playing === preview.id ? "停止" : "试听";
    play.addEventListener("click", (event) => {
      event.stopPropagation();
      if (state.design.playing === preview.id) stopDesignAudio();
      else playDesign(preview.id);
    });

    row.append(radio, name, duration, play);
    list.append(row);
  });
}

function playDesign(id) {
  const preview = state.design.previews.find((item) => item.id === id);
  if (!preview || !preview.audio) return;
  stopDesignAudio();
  const audio = new Audio(`data:${preview.mime || "audio/mpeg"};base64,${preview.audio}`);
  state.design.audio = audio;
  state.design.playing = id;
  audio.addEventListener("ended", () => {
    if (state.design.audio === audio) {
      state.design.audio = null;
      state.design.playing = null;
      renderPreviews();
    }
  });
  audio.play().catch(() => toast("试听失败"));
  renderPreviews();
}

async function saveDesign() {
  if (!state.design.selectedId) {
    toast("先选一个音色");
    return;
  }
  const button = $("saveDesign");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await apiPost("design/save", {
      id: state.design.selectedId,
      name: $("designName").value.trim(),
      description: $("designDesc").value.trim(),
      language: state.settings?.language || "zh",
    });
    await loadSettings();
    await loadVoices();
    closeDesign();
    toast(`已保存并使用：${result.name}`);
  } catch (error) {
    $("designNote").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "保存并使用";
  }
}

/* ---------------- events ---------------- */

function bind() {
  $("saveKey").addEventListener("click", async () => {
    const value = $("apiKey").value.trim();
    if (!value) {
      toast("先填写 API Key");
      return;
    }
    try {
      await saveSettings({ apiKey: value }, "API Key 已保存");
      $("apiKey").value = "";
      await loadVoices();
    } catch (error) {
      toast(error.message);
    }
  });

  $("voiceProbability").addEventListener("input", (event) => {
    $("probVal").textContent = `${Number(event.target.value)}%`;
  });
  $("voiceProbability").addEventListener("change", async (event) => {
    try {
      await saveSettings({ voiceProbability: Number(event.target.value) }, "语音概率已保存");
    } catch (error) {
      toast(error.message);
    }
  });

  $("smartVoice").addEventListener("change", async (event) => {
    try {
      await saveSettings({ smartVoice: event.target.checked }, event.target.checked ? "已开启智能判断" : "已关闭智能判断");
      updateProbabilityState();
    } catch (error) {
      toast(error.message);
      event.target.checked = !event.target.checked;
    }
  });

  $("speed").addEventListener("input", (event) => {
    $("speedVal").textContent = `${Number(event.target.value).toFixed(2)}×`;
  });
  $("speed").addEventListener("change", async (event) => {
    try {
      await saveSettings({ speed: Number(event.target.value) }, "语速已保存");
    } catch (error) {
      toast(error.message);
    }
  });

  $("search").addEventListener("input", (event) => {
    state.search = event.target.value.trim();
    clearTimeout(bind.searchTimer);
    bind.searchTimer = setTimeout(loadVoices, 300);
  });

  $("copyCode").addEventListener("click", () => copyText("BREEZEBLUE", "口令已复制"));
  $("copyGift").addEventListener("click", () =>
    copyText(GIFT_URL, "领取链接已复制，去浏览器打开"),
  );
  $("copyInviteLink").addEventListener("click", () =>
    copyText(GIFT_URL, "邀请链接已复制，去浏览器打开"),
  );
  $("getKey").addEventListener("click", () =>
    copyText(KEY_URL, "获取 API Key 的链接已复制，去浏览器打开"),
  );
  $("copyInvite").addEventListener("click", () =>
    copyText(
      `我在用 Breeze 语音，注册就送 1000–10000 积分（口令 BREEZEBLUE）：${GIFT_URL}`,
      "邀请文案已复制",
    ),
  );

  $("openDesign").addEventListener("click", () => {
    if (!state.settings?.hasApiKey) {
      toast("请先设置 API Key");
      return;
    }
    openDesign();
  });
  $("closeDesign").addEventListener("click", closeDesign);
  $("modal").addEventListener("click", (event) => {
    if (event.target === $("modal")) closeDesign();
  });
  $("designDesc").addEventListener("input", (event) => {
    $("designCount").textContent = `${event.target.value.length}/500`;
  });
  $("generate").addEventListener("click", generateDesign);
  $("saveDesign").addEventListener("click", saveDesign);
}

async function main() {
  if (!bridge) {
    document.body.innerHTML = "<p style='padding:24px'>请从 AstrBot 插件页打开此视图。</p>";
    return;
  }
  await bridge.ready();
  bind();
  try {
    await loadSettings();
    await loadVoices();
  } catch (error) {
    toast(error.message);
  }
}

main();
