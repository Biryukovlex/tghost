import { readFileSync } from 'node:fs';
import { afterEach, expect, test, vi } from 'vitest';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

test('provider switch and model refresh use the same accessible dropdown', async () => {
  document.body.innerHTML = `<select data-ui-dropdown id="studio-provider"><option value="openrouter">OpenRouter</option><option value="ollama">Ollama</option></select>
    <div data-provider-panel="openrouter"><input name="model"></div>
    <div data-provider-panel="ollama" hidden><input id="ollama-url" value="http://127.0.0.1:11434">
      <select data-ui-dropdown id="ollama-model"><option value="">Load installed models</option></select><button data-load-models="ollama">Refresh models</button></div>
    <p data-model-connection-error hidden></p>`;
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:true,json:async()=>({models:[{id:'synthetic-local',name:'Synthetic local model'}]})}));
  window.eval(readFileSync('../app/web/static/ui-controls.js','utf8'));
  window.eval(readFileSync('../app/web/static/provider-connections.js','utf8'));
  document.dispatchEvent(new Event('DOMContentLoaded'));
  const provider = document.querySelector<HTMLSelectElement>('#studio-provider')!;
  provider.value = 'ollama';
  provider.dispatchEvent(new Event('change'));
  expect(document.querySelector<HTMLInputElement>('[name="model"]')!.disabled).toBe(true);
  expect(document.querySelector<HTMLInputElement>('#ollama-url')!.disabled).toBe(false);
  document.querySelector<HTMLButtonElement>('[data-load-models]')!.click();
  await vi.waitFor(() => expect(document.querySelector<HTMLSelectElement>('#ollama-model')!.value).toBe('synthetic-local'));
  const native = document.querySelector('#ollama-model')!;
  const trigger = native.parentElement!.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')!;
  expect(trigger.textContent).toContain('Synthetic local model');
  trigger.click();
  const options = [...native.parentElement!.querySelectorAll('[role="option"]')];
  expect(options.map(option=>option.textContent)).toEqual(['Synthetic local model']);
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('ollama_url=http%3A%2F%2F127.0.0.1%3A11434'), expect.any(Object));
});


function chatgptPage(provider = 'openai', connected = true) {
  document.body.innerHTML = `<select data-ui-dropdown id="studio-provider"><option value="openrouter">OpenRouter</option><option value="openai">ChatGPT</option></select>
    <div data-provider-panel="openrouter"><input name="model"></div>
    <div data-provider-panel="openai"><select data-ui-dropdown id="openai-model"><option value="saved-model">Saved model</option><option value="other-model">Other model</option></select>
      <button data-load-models="openai" data-models-connected="${connected}">Refresh models</button></div>
    <p data-model-connection-error hidden></p>`;
  document.querySelector<HTMLSelectElement>('#studio-provider')!.value = provider;
  window.eval(readFileSync('../app/web/static/ui-controls.js', 'utf8'));
  window.eval(readFileSync('../app/web/static/provider-connections.js', 'utf8'));
  document.dispatchEvent(new Event('DOMContentLoaded'));
}

function switchProvider(value: string) {
  const picker = document.querySelector<HTMLSelectElement>('#studio-provider')!;
  picker.value = value;
  picker.dispatchEvent(new Event('change'));
}

const catalogResponse = (models = [{id: 'saved-model', name: 'Saved model'}, {id: 'other-model', name: 'Other model'}]) =>
  ({ok: true, json: async () => ({models})});

test.each(['openai', 'openrouter'])('ChatGPT loads all options on activation from %s once', async provider => {
  const models = Array.from({length: 251}, (_, i) => ({id: `synthetic-${i}`, name: `Synthetic ${i}`}));
  models.unshift({id: 'saved-model', name: 'Saved model'});
  const fetchMock = vi.fn().mockResolvedValue(catalogResponse(models));
  vi.stubGlobal('fetch', fetchMock);
  chatgptPage(provider);
  if (provider === 'openrouter') {
    expect(fetchMock).not.toHaveBeenCalled();
    switchProvider('openai');
  }
  await vi.waitFor(() => expect(document.querySelector<HTMLSelectElement>('#openai-model')!.options).toHaveLength(252));
  const select = document.querySelector<HTMLSelectElement>('#openai-model')!;
  expect(select.value).toBe('saved-model');
  const trigger = select.parentElement!.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')!;
  trigger.click();
  expect(select.parentElement!.querySelectorAll('[role="option"]')).toHaveLength(252);
  switchProvider('openrouter');
  switchProvider('openai');
  expect(fetchMock).toHaveBeenCalledTimes(1);
  document.querySelector<HTMLButtonElement>('[data-load-models]')!.click();
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

test('refresh preserves the choice made while loading and retains a missing saved model', async () => {
  let resolve!: (value: ReturnType<typeof catalogResponse>) => void;
  vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(done => { resolve = done; })));
  chatgptPage();
  const select = document.querySelector<HTMLSelectElement>('#openai-model')!;
  select.value = 'other-model';
  switchProvider('openrouter');
  switchProvider('openai');
  expect(fetch).toHaveBeenCalledTimes(1);
  resolve(catalogResponse([{id: 'new-model', name: 'New model'}]));
  await vi.waitFor(() => expect(select.selectedOptions[0].textContent).toContain('not in catalog'));
  expect(select.value).toBe('other-model');
  expect(select.selectedOptions[0].textContent).toContain('not in catalog');
});

test.each(['failed', 'empty'])('a %s catalog keeps the saved choice and can be retried', async failure => {
  const fetchMock = vi.fn().mockResolvedValueOnce(failure === 'empty' ? catalogResponse([]) :
    {ok: false, json: async () => ({error: {message: 'Check the connection'}})}).mockResolvedValue(catalogResponse());
  vi.stubGlobal('fetch', fetchMock);
  chatgptPage();
  const error = document.querySelector<HTMLElement>('[data-model-connection-error]')!;
  await vi.waitFor(() => expect(error.hidden).toBe(false));
  expect(document.querySelector<HTMLSelectElement>('#openai-model')!.value).toBe('saved-model');
  document.querySelector<HTMLButtonElement>('[data-load-models]')!.click();
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(error.hidden).toBe(true);
});

test('disconnected ChatGPT does not auto-load or show a connection error', () => {
  vi.stubGlobal('fetch', vi.fn());
  chatgptPage('openai', false);
  expect(fetch).not.toHaveBeenCalled();
  expect(document.querySelector<HTMLElement>('[data-model-connection-error]')!.hidden).toBe(true);
});
