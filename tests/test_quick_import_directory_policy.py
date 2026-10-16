"""Execute browser import selection with synthetic names and unread-entry tripwires."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_browser_allows_ordinary_directory_substrings_but_keeps_file_exclusions(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node is unavailable")
    script = tmp_path / "directory-policy.cjs"
    script.write_text(r"""
const assert = require('node:assert/strict');
const elements = new Map();
function element() {
  return {
    checked: false, listeners: {}, classList: { toggle() {}, remove() {}, add() {} },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {}, replaceChildren() {}, append() {},
  };
}
global.window = {};
global.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: element,
};
require(process.argv[2]);
const calls = [];
const imports = window.EngraphisImports.create({
  api: async (url, options) => {
    calls.push(options.body.getAll('files').map(file => file.name));
    return { imported: 3 };
  },
  workspace: () => 'synthetic', notify() {}, onImported() {},
  confirmDerive: async () => { throw new Error('unexpected derivation'); },
});
const benign = ['tokenizer-project', 'secretary-notes', 'credentialing'];
const file = name => ({ name, file: new File(['Synthetic notes.'], name.split('/').pop()) });
(async () => {
  const names = benign.map(name => `${name}/readme.md`);
  await imports.upload(names.map(file));
  assert.deepEqual(calls, [names]);
  const entries = benign.map(name => ({
    name, isDirectory: true,
    createReader() {
      let first = true;
      return { readEntries(done) {
        done(first ? [{ name: 'readme.md', isFile: true,
          file(done) { done(new File(['Synthetic notes.'], 'readme.md')); } }] : []);
        first = false;
      } };
    },
  }));
  entries.push(...['credentials', 'token', 'recoverycodes', 'client.key', '.ssh'].map(name => ({
    name, isDirectory: true,
    createReader() { throw new Error('known sensitive directory was inspected'); },
  })));
  entries.push({ name: 'tokenizer.md', isFile: true,
    file() { throw new Error('sensitive file basename was inspected'); } });
  elements.get('quick-import-drop').listeners.drop({
    preventDefault() {}, dataTransfer: {
      items: entries.map(entry => ({ webkitGetAsEntry: () => entry })), files: [],
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, [names, names]);
})().catch(error => { console.error(error); process.exitCode = 1; });
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "engraphis/dashboard_assets/import-sources.js"
    result = subprocess.run(["node", str(script), str(source)], text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
