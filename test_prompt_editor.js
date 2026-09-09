// Exercise the shipped editor logic with deferred API responses, without a browser dependency.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(0, "utf8").replace(/boot\(\)\.catch\([\s\S]*$/, "");

function editor() {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      value: "", textContent: "", hidden: false, disabled: false,
      get innerText() { return this.textContent; },
      classList: { toggle() {} }
    });
    return elements.get(selector);
  };
  element("#generationForm").elements = { prompt: { value: "" } };
  element("#generationForm").generateAudio = { checked: false };
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [] },
    setTimeout, clearTimeout, AbortController, console
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.config = {promptEnhancer: {enabled: true, maxPromptLength: 12000, maxPreferencesLength: 4000}};
    durationEl.value = "10";
    ratioEl.value = "16:9";
    promptEditor.textContent = "Кот смотрит в окно";
    renderPromptEditor = () => syncPromptField();
    syncPromptImageUrls = () => {};
    renderImageReferences = () => {};
    updatePromptActions();
  `, context);
  return { run: (code) => vm.runInContext(code, context), element };
}

async function main() {
  {
    const app = editor();
    app.run(`promptPreferences.value = "Холодный свет, статичная камера";
      var submitted;
      apiFetch = async (url, options) => {
        submitted = JSON.parse(options.body);
        return {prompt: "A cat looks out of the window.\\nCool window light, locked camera."};
      };`);
    await app.run("enhancePrompt()");
    assert.equal(app.element("#promptEditor").textContent, "A cat looks out of the window.\nCool window light, locked camera.");
    assert.equal(app.run("submitted.preferences"), "Холодный свет, статичная камера");
    assert.equal(app.element("#promptPreferences").value, "Холодный свет, статичная камера");
    assert.equal(app.element("#enhancePromptStatus").hidden, true);
    assert.equal(app.element("#undoPromptBtn").hidden, false);
    app.run("undoPromptEnhancement()");
    assert.equal(app.element("#promptEditor").textContent, "Кот смотрит в окно");
    assert.equal(app.element("#undoPromptBtn").hidden, true);
    assert.equal(app.element("#promptPreferences").value, "Холодный свет, статичная камера");
  }
  {
    const app = editor();
    app.run(`apiFetch = async () => { throw new Error("BytePlus unavailable"); };`);
    await app.run("enhancePrompt()");
    assert.equal(app.element("#promptEditor").textContent, "Кот смотрит в окно");
    assert.equal(app.element("#enhancePromptStatus").textContent, "BytePlus unavailable");
    assert.equal(app.element("#enhancePromptStatus").hidden, false);
    assert.equal(app.element("#enhancePromptBtn").disabled, false);
  }
  for (const change of [
    'promptEditor.textContent = "Мои новые правки"; promptRevision += 1;',
    'durationEl.value = "20";',
    'promptPreferences.value = "Добавь плавный отъезд камеры";',
    'imageRefs.push({id: "new-reference", url: "asset://hero"});',
    'setMode("image"); setMode("video");'
  ]) {
    const app = editor();
    app.run("var resolveRequest; apiFetch = () => new Promise(resolve => { resolveRequest = resolve; });");
    const pending = app.run("enhancePrompt()");
    assert.equal(app.element("#enhancePromptBtn").disabled, true);
    app.run(change);
    const current = app.element("#promptEditor").textContent;
    app.run('resolveRequest({prompt: "Устаревший ответ"})');
    await pending;
    assert.equal(app.element("#promptEditor").textContent, current);
    assert.match(app.element("#enhancePromptStatus").textContent, /изменились/);
  }
  {
    const app = editor();
    app.run("var calls = 0; var resolveRequest; apiFetch = () => { calls += 1; return new Promise(resolve => { resolveRequest = resolve; }); };");
    const pending = app.run("enhancePrompt()");
    await app.run("enhancePrompt()");
    await app.run("submitGeneration({preventDefault() {}})");
    assert.equal(app.run("calls"), 1);
    assert.match(app.element("#enhancePromptStatus").textContent, /Дождитесь/);
    app.run('resolveRequest({prompt: "Готовый промпт"})');
    await pending;
    app.run('promptEditor.textContent = "Ручная правка после улучшения"; undoPromptEnhancement();');
    assert.equal(app.element("#promptEditor").textContent, "Ручная правка после улучшения");
  }
  {
    const app = editor();
    app.run('promptPreferences.value = "x".repeat(4001); var calls = 0; apiFetch = async () => { calls += 1; };');
    await app.run("enhancePrompt()");
    assert.equal(app.run("calls"), 0);
    assert.equal(app.element("#enhancePromptStatus").hidden, false);
    assert.match(app.element("#enhancePromptStatus").textContent, /4 000/);
  }
  {
    const app = editor();
    app.run(`var submitted;
      apiFetch = async (url, options) => {
        submitted = JSON.parse(options.body);
        return {prompt: "A cat watches the street. Locked camera."};
      };`);
    await app.run("enhancePrompt()");
    app.run('promptPreferences.value = "Теперь плавно приблизить камеру";');
    await app.run("enhancePrompt()");
    assert.equal(app.run("submitted.prompt"), "A cat watches the street. Locked camera.");
    assert.equal(app.run("submitted.preferences"), "Теперь плавно приблизить камеру");
  }
  {
    const app = editor();
    app.run('promptEditor.textContent = "  "; updatePromptActions();');
    assert.equal(app.element("#enhancePromptBtn").disabled, true);
    app.run('promptEditor.textContent = "Кот"; state.config.promptEnhancer.enabled = false; updatePromptActions();');
    assert.equal(app.element("#enhancePromptBtn").disabled, true);
  }
  console.log("Editor checks passed: replacement, undo, errors, stale responses, duplicate requests and availability.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
