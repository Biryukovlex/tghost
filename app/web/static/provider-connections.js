(function () {
  "use strict";
  function init() {
    var picker = document.getElementById("studio-provider");
    if (!picker || picker.dataset.modelsInitialized) return;
    picker.dataset.modelsInitialized = "true";
    var loaders = {};
    var error = document.querySelector("[data-model-connection-error]");
    document.querySelectorAll("[data-load-models]").forEach(function (button) {
      var provider = button.dataset.loadModels;
      var select = document.getElementById(provider === "ollama" ? "ollama-model" : "openai-model");
      var loading = false;
      var loaded = false;
      async function load() {
        if (loading) return;
        loading = true;
        button.disabled = true;
        if (picker.value === provider) error.hidden = true;
        try {
          var response = await fetch("/settings/providers/models?provider=" + encodeURIComponent(provider) + (provider === "ollama" ? "&ollama_url=" + encodeURIComponent(document.getElementById("ollama-url").value) : ""), { credentials: "same-origin" });
          var payload = await response.json();
          if (!response.ok) throw new Error(payload.error && payload.error.message || "Models could not load.");
          if (!Array.isArray(payload.models) || !payload.models.length) throw new Error("No models available. Install an Ollama model or check ChatGPT access.");
          // Read the current choice after the request: users may change it
          // while a refresh is in flight. Never silently replace a saved model.
          var selected = select.value;
          var models = payload.models.slice();
          if (selected && !models.some(function (model) { return model.id === selected; })) {
            models.unshift({ id: selected, name: selected + " (saved; not in catalog)" });
          }
          select.replaceChildren();
          models.forEach(function (model) {
            var option = document.createElement("option");
            option.value = model.id;
            option.textContent = model.name;
            option.selected = model.id === selected;
            select.appendChild(option);
          });
          loaded = true;
          select.dispatchEvent(new Event("tg:options"));
          select.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (reason) {
          if (picker.value === provider) {
            error.textContent = reason.message || "Models could not load.";
            error.hidden = false;
          }
        } finally { loading = false; button.disabled = false; }
      }
      button.addEventListener("click", load);
      loaders[provider] = function () {
        if (provider === "openai" && button.dataset.modelsConnected === "true" && !loaded) load();
      };
    });
    function update() {
      error.hidden = true;
      document.querySelectorAll("[data-provider-panel]").forEach(function (panel) {
        panel.hidden = panel.dataset.providerPanel !== picker.value;
        panel.querySelectorAll("input, select").forEach(function (input) { input.disabled = panel.hidden; input.dispatchEvent(new Event("change")); });
      });
      if (loaders[picker.value]) loaders[picker.value]();
    }
    picker.addEventListener("change", update);
    update();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
