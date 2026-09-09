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
    state.config = {promptEnhancer: {enabled: true, maxPromptLength: 12000}};
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
    app.run(`apiFetch = async () => ({prompt: "Кот смотрит в окно.\\nКамера приближается."});`);
    await app.run("enhancePrompt()");
    assert.equal(app.element("#promptEditor").textContent, "Кот смотрит в окно.\nКамера приближается.");
    assert.equal(app.element("#undoPromptBtn").hidden, false);
    app.run("undoPromptEnhancement()");
    assert.equal(app.element("#promptEditor").textContent, "Кот смотрит в окно");
    assert.equal(app.element("#undoPromptBtn").hidden, true);
  }
  {
    const app = editor();
    app.run(`apiFetch = async () => { throw new Error("BytePlus unavailable"); };`);
    await app.run("enhancePrompt()");
    assert.equal(app.element("#promptEditor").textContent, "Кот смотрит в окно");
    assert.equal(app.element("#enhancePromptStatus").textContent, "BytePlus unavailable");
    assert.equal(app.element("#enhancePromptBtn").disabled, false);
  }
  for (const change of [
    'promptEditor.textContent = "Мои новые правки"; promptRevision += 1;',
    'durationEl.value = "20";',
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
    app.run('promptEditor.textContent = "  "; updatePromptActions();');
    assert.equal(app.element("#enhancePromptBtn").disabled, true);
    app.run('promptEditor.textContent = "Кот"; state.config.promptEnhancer.enabled = false; updatePromptActions();');
    assert.equal(app.element("#enhancePromptBtn").disabled, true);
  }
  console.log("Editor checks passed: replacement, undo, errors, stale responses, duplicate requests and availability.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
