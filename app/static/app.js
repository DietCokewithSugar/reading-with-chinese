"use strict";

// --------------------------------------------------------------------------- //
// Element handles
// --------------------------------------------------------------------------- //
const $ = (id) => document.getElementById(id);

const els = {
  settings: $("settings"),
  settingsToggle: $("settings-toggle"),
  apiKey: $("api-key"),
  modelName: $("model-name"),
  engine: $("engine"),
  translateFigures: $("translate-figures"),
  langIn: $("lang-in"),
  langOut: $("lang-out"),
  chunkSize: $("chunk-size"),
  concurrency: $("concurrency"),
  thread: $("thread"),

  dropzone: $("dropzone"),
  fileInput: $("file-input"),
  fileName: $("file-name"),
  translateBtn: $("translate-btn"),
  cancelBtn: $("cancel-btn"),

  progressWrap: $("progress-wrap"),
  progressBar: $("progress-bar"),
  progressLabel: $("progress-label"),

  srcFrame: $("src-frame"),
  srcEmpty: $("src-empty"),
  outFrame: $("out-frame"),
  outEmpty: $("out-empty"),
  viewSeg: $("view-seg"),
  downloadLink: $("download-link"),

  toast: $("toast"),
};

// --------------------------------------------------------------------------- //
// Settings persistence (localStorage)
// --------------------------------------------------------------------------- //
const SETTINGS_KEYS = [
  "apiKey", "modelName", "engine", "langIn", "langOut",
  "chunkSize", "concurrency", "thread",
];
const STORE_KEY = "rwc.settings.v1";

function loadSettings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (_) {}
  for (const key of SETTINGS_KEYS) {
    if (saved[key] !== undefined && els[key]) els[key].value = saved[key];
  }
  if (saved.translateFigures !== undefined && els.translateFigures) {
    els.translateFigures.checked = !!saved.translateFigures;
  }
}

function saveSettings() {
  const data = {};
  for (const key of SETTINGS_KEYS) if (els[key]) data[key] = els[key].value;
  if (els.translateFigures) data.translateFigures = els.translateFigures.checked;
  localStorage.setItem(STORE_KEY, JSON.stringify(data));
}

for (const key of SETTINGS_KEYS) {
  if (els[key]) els[key].addEventListener("change", saveSettings);
}
if (els.translateFigures) els.translateFigures.addEventListener("change", saveSettings);

els.settingsToggle.addEventListener("click", () => {
  els.settings.classList.toggle("hidden");
});

// --------------------------------------------------------------------------- //
// Toast helper
// --------------------------------------------------------------------------- //
let toastTimer = null;
function toast(message, kind = "") {
  els.toast.textContent = message;
  els.toast.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 4200);
}

// --------------------------------------------------------------------------- //
// File selection + original preview (rendered locally, no upload needed)
// --------------------------------------------------------------------------- //
let selectedFile = null;
let srcObjectUrl = null;

function setFile(file) {
  if (!file) return;
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    toast("请选择 PDF 文件", "err");
    return;
  }
  selectedFile = file;
  els.fileName.textContent = file.name;
  els.translateBtn.disabled = false;

  if (srcObjectUrl) URL.revokeObjectURL(srcObjectUrl);
  srcObjectUrl = URL.createObjectURL(file);
  els.srcFrame.src = srcObjectUrl;
  els.srcEmpty.classList.add("hidden");
}

els.dropzone.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", (e) => setFile(e.target.files[0]));

["dragover", "dragenter"].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("drag");
  })
);
els.dropzone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});

// --------------------------------------------------------------------------- //
// Translation job lifecycle
// --------------------------------------------------------------------------- //
let currentJobId = null;
let pollTimer = null;
let currentKind = "mono";
let resultReady = false;  // true once translated files exist on the server
const LAST_JOB_KEY = "rwc.lastJob";

function setBusy(busy) {
  els.translateBtn.disabled = busy || !selectedFile;
  els.cancelBtn.classList.toggle("hidden", !busy);
  els.progressWrap.classList.toggle("hidden", !busy);
  els.dropzone.style.pointerEvents = busy ? "none" : "";
}

function updateProgress(job) {
  els.progressBar.style.width = (job.percent || 0) + "%";
  let label = `${job.pages_done}/${job.pages_total || "?"} 页 · ${job.percent || 0}%`;
  if (job.status === "running" && !job.pages_total) label = "正在解析文档…";
  els.progressLabel.textContent = label;
}

async function startTranslation() {
  const apiKey = els.apiKey.value.trim();
  if (!apiKey) {
    els.settings.classList.remove("hidden");
    toast("请先在设置中填写 DeepSeek API Key", "err");
    els.apiKey.focus();
    return;
  }
  if (!selectedFile) return;

  saveSettings();

  const form = new FormData();
  form.append("file", selectedFile);
  form.append("api_key", apiKey);
  form.append("model_name", els.modelName.value);
  form.append("lang_in", els.langIn.value);
  form.append("lang_out", els.langOut.value);
  form.append("chunk_size", els.chunkSize.value || "8");
  form.append("concurrency", els.concurrency.value || "6");
  form.append("thread", els.thread.value || "4");
  form.append("engine", els.engine.value || "pdf2zh");
  form.append("translate_figures", els.translateFigures.checked ? "true" : "false");

  setBusy(true);
  els.progressBar.style.width = "0%";
  els.progressLabel.textContent = "上传中…";
  els.outFrame.removeAttribute("src");
  els.outEmpty.classList.remove("hidden");
  els.downloadLink.classList.add("hidden");

  try {
    const resp = await fetch("/api/translate", { method: "POST", body: form });
    if (resp.status === 401) { location.href = "/login"; return; }
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `服务器错误 (${resp.status})`);
    }
    const { job_id } = await resp.json();
    currentJobId = job_id;
    resultReady = false;
    try { localStorage.setItem(LAST_JOB_KEY, job_id); } catch (_) {}
    pollStatus();
  } catch (err) {
    setBusy(false);
    toast("提交失败：" + err.message, "err");
  }
}

function pollStatus() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!currentJobId) return;
    try {
      const resp = await fetch(`/api/jobs/${currentJobId}`);
      if (resp.status === 401) { location.href = "/login"; return; }
      if (!resp.ok) throw new Error("任务丢失");
      const job = await resp.json();
      updateProgress(job);

      if (job.status === "done") {
        onDone(job);
      } else if (job.status === "error") {
        setBusy(false);
        toast("翻译失败：" + (job.error || "未知错误"), "err");
      } else if (job.status === "cancelled") {
        setBusy(false);
        toast("已取消", "");
      } else {
        pollStatus();
      }
    } catch (err) {
      setBusy(false);
      toast(err.message, "err");
    }
  }, 900);
}

function onDone(job, opts = {}) {
  setBusy(false);
  resultReady = true;
  if (!opts.silent) toast("翻译完成 🎉", "ok");
  showResult(currentKind);
}

function showResult(kind) {
  if (!currentJobId) return;
  currentKind = kind;
  const url = `/api/jobs/${currentJobId}/file/${kind}`;
  els.outFrame.src = url + "#t=" + Date.now();
  els.outEmpty.classList.add("hidden");
  els.downloadLink.href = url + "?download=1";
  els.downloadLink.classList.remove("hidden");
}

els.viewSeg.addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn) return;
  els.viewSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  currentKind = btn.dataset.kind;
  // Only fetch the file once the translation has actually finished, otherwise
  // we'd hit the not-yet-written file and show "File not ready".
  if (currentJobId && resultReady) showResult(currentKind);
});

els.translateBtn.addEventListener("click", startTranslation);

els.cancelBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }).catch(() => {});
  clearTimeout(pollTimer);
  setBusy(false);
  toast("正在取消…", "");
});

// Restore the last job after a reload / reopening the page, so a finished
// translation can be viewed without staying on the page during processing.
async function restoreLastJob() {
  let jobId = null;
  try { jobId = localStorage.getItem(LAST_JOB_KEY); } catch (_) {}
  if (!jobId) return;
  try {
    const resp = await fetch(`/api/jobs/${jobId}`);
    if (resp.status === 401) { location.href = "/login"; return; }
    if (!resp.ok) { localStorage.removeItem(LAST_JOB_KEY); return; }
    const job = await resp.json();
    currentJobId = jobId;
    if (job.status === "done") {
      onDone(job, { silent: true });
    } else if (job.status === "running" || job.status === "pending") {
      setBusy(true);
      pollStatus();
    } else {
      // error / cancelled -> nothing to restore
      localStorage.removeItem(LAST_JOB_KEY);
      currentJobId = null;
    }
  } catch (_) {
    /* ignore restore failures */
  }
}

// --------------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------------- //
loadSettings();
restoreLastJob();
fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    if (!h.model_ready) {
      toast("首次使用需联网下载排版模型，初次翻译可能稍慢", "");
    }
  })
  .catch(() => {});
