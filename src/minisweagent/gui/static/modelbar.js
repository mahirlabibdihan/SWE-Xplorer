/* Model picker under the chat box: provider (only those with an API key) → model. Writes into the loaded config. */
"use strict";

const PROVIDERS = [
  { id: "openrouter", label: "OpenRouter", key: "OPENROUTER_API_KEY", cls: "openrouter", prefix: "",
    models: ["openrouter/free", "poolside/laguna-s-2.1:free", "nvidia/nemotron-3-super-120b-a12b:free",
      "nvidia/nemotron-3-ultra-550b-a55b:free", "cohere/north-mini-code:free", "deepseek/deepseek-v4-flash", "openai/gpt-5-mini", "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-opus-5-5",
      "qwen/qwen3.5-flash-02-23", "z-ai/glm-4.7-flash", "google/gemini-2.5-flash", "xiaomi/mimo-v2.5"] },
  { id: "openai", label: "OpenAI", key: "OPENAI_API_KEY", cls: "litellm", prefix: "openai/",
    models: ["openai/gpt-5-mini", "openai/gpt-5", "openai/gpt-4.1-mini"] },
  { id: "anthropic", label: "Anthropic", key: "ANTHROPIC_API_KEY", cls: "litellm", prefix: "anthropic/",
    models: ["anthropic/claude-sonnet-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-4-5-20251001"] },
  { id: "gemini", label: "Google Gemini", key: "GEMINI_API_KEY", cls: "litellm", prefix: "gemini/",
    models: ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"] },
  { id: "deepseek", label: "DeepSeek", key: "DEEPSEEK_API_KEY", cls: "litellm", prefix: "deepseek/",
    models: ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"] },
  { id: "vllm", label: "vLLM / OpenAI-compatible", key: "HOSTED_VLLM_API_KEY", cls: "litellm", prefix: "hosted_vllm/", needsBase: true,
    models: ["hosted_vllm/Qwen/Qwen2.5-7B-Instruct", "hosted_vllm/Qwen/Qwen3.5-4B"] },
];

S.keys = null;

function keySet(name) { return !!(S.keys || S.info?.keys || []).find((k) => k.name === name && k.set); }
/** Local OpenAI-compatible servers need a URL rather than a key, so they are always offered. */
function providerReady(p) { return p.needsBase || keySet(p.key); }

/** Which provider a model section of the config uses. */
function providerOf(m) {
  if (!m) return null;
  const cls = m.model_class || "", name = m.model_name || "";
  if (cls === "openrouter") return PROVIDERS[0];
  if (cls && !["litellm", "litellm_response", ""].includes(cls)) return null; // anthropic/portkey/custom class paths
  return PROVIDERS.find((p) => p.prefix && name.startsWith(p.prefix)) || null;
}

function renderModelBar() {
  const d = S.cfg.data;
  const bar = $("#model-pop");
  if (!d || !bar) return;
  d.model ??= {};
  const current = providerOf(d.model);
  const available = PROVIDERS.filter((p) => providerReady(p) || p === current);
  const sel = $("#mb-provider");
  sel.innerHTML = "";
  for (const p of available) sel.append(h("option", { value: p.id, text: p.label + (providerReady(p) ? "" : "  ⚠ key missing") }));
  if (!current) sel.append(h("option", { value: "config", text: d.model.model_class ? `from config (${d.model.model_class})` : "from config" }));
  sel.value = current ? current.id : "config";
  const none = !PROVIDERS.some((p) => keySet(p.key)) && !current;
  $("#mb-nokeys").classList.toggle("hidden", !none || !!d.model.model_class);
  sel.classList.toggle("warn", !!current && !providerReady(current));
  sel.title = current && !providerReady(current) ? `${current.key} is not set: add it in the Keys tab` : "Provider (only those whose API key is set)";

  const prov = current;
  const dl = $("#mb-models"); dl.innerHTML = "";
  for (const m of (prov?.models || [])) dl.append(h("option", { value: m }));
  $("#mb-model").value = d.model.model_name || "";
  const base = prov?.needsBase;
  $("#mb-base-field").classList.toggle("hidden", !base);
  if (base) $("#mb-base").value = d.model.model_kwargs?.api_base || "";
  $("#mb-reward").checked = $("#reward-same").checked;
  const name = d.model.model_name || "no model";
  const warn = current && !providerReady(current);
  $("#model-label").textContent = (warn ? "⚠ " : "") + name.split("/").pop();
  $("#btn-model").title = `${current ? current.label : d.model.model_class || "from config"} · ${name}${warn ? `
${current.key} is not set` : ""}` +
    `
Reward model: ${$("#reward-same").checked ? "same as policy" : d.reward_model?.model_name || "same as policy"}`;
  const rm = d.reward_model?.model_name;
  $("#mb-reward-label").title = $("#reward-same").checked ? "The reward model is the same as the policy model"
    : `Reward model from the config: ${rm || "(not set, uses the policy model)"}${d.reward_model?.model_kwargs?.api_base ? " @ " + d.reward_model.model_kwargs.api_base : ""}`;
}

function setProvider(id) {
  const d = S.cfg.data; const p = PROVIDERS.find((x) => x.id === id);
  if (!p) return;
  d.model ??= {};
  d.model.model_class = p.cls;
  if (!(d.model.model_name || "").startsWith(p.prefix) || (p.id === "openrouter" && providerOf(d.model) !== p) || !d.model.model_name) {
    d.model.model_name = p.models[0];
  }
  if (!p.needsBase && d.model.model_kwargs?.api_base) delete d.model.model_kwargs.api_base;
  modelChanged();
}

function modelChanged() { markDirty(); renderAllForms(); }

function initModelBar() {
  $("#mb-provider").onchange = (e) => { if (e.target.value !== "config") setProvider(e.target.value); };
  $("#mb-model").onchange = (e) => {
    const v = e.target.value.trim(); if (!v) return;
    S.cfg.data.model.model_name = v; modelChanged();
  };
  $("#mb-base").onchange = (e) => {
    const m = S.cfg.data.model; m.model_kwargs ??= {};
    if (e.target.value.trim()) m.model_kwargs.api_base = e.target.value.trim(); else delete m.model_kwargs.api_base;
    modelChanged();
  };
  $("#mb-reward").onchange = (e) => { $("#reward-same").checked = e.target.checked; syncRewardSame(); renderModelBar(); };
  $("#mb-nokeys").onclick = (e) => { e.preventDefault(); $("#model-pop").classList.add("hidden"); showTab("#left", "keys"); };
  // keep the bar in sync with the YAML forms and the key list
  const forms = renderAllForms;
  renderAllForms = function (...a) { forms(...a); renderModelBar(); };
  const keys = renderKeys;
  renderKeys = function (k) { S.keys = k; keys(k); renderModelBar(); };
  const same = syncRewardSame;
  syncRewardSame = function (...a) { same(...a); if ($("#mb-reward")) $("#mb-reward").checked = $("#reward-same").checked; };
  renderModelBar();
}
initModelBar();
