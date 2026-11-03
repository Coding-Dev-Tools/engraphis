"""Execute Library reorder and cursor paging together in the shipped Ledger UI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import shutil

import pytest

from tests.test_dashboard_auth_placement import _HARNESS_PRELUDE, _ledger_declaration


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const elements = new Map();
let focused = null;
const makeElement = document.createElement;
document.createElement = tag => {
  const item = makeElement(tag);
  item.focus = () => { focused = item; };
  item.closest = () => item;
  item.querySelectorAll = selector => item.children.filter(child =>
    selector === '.memory-card' ? child.className === 'memory-card'
      : child.getAttribute?.('role') === 'option');
  item.querySelector = selector => item.children.find(child =>
    selector.includes(`data-memory-id="${child.dataset?.memoryId}"`)) || null;
  return item;
};
document.getElementById = id => {
  if (!elements.has(id)) elements.set(id, document.createElement('div'));
  return elements.get(id);
};
const window = { clearTimeout() {} }, CSS = { escape: value => value };
const notices = [], calls = [], gates = new Map();
const showNotice = message => notices.push(message);
const memoryTitle = memory => memory.title;
const renderMoveSelection = () => {};
const resetMoveSelection = () => state.moveMemoryIds.clear();
const renderFirstMemoryJourney = () => {};
const memoryCard = memory => {
  const card = document.createElement('button');
  card.className = 'memory-card';
  card.dataset.memoryId = memory.id;
  card.setAttribute('role', 'option');
  card.setAttribute('aria-selected', String(state.selectedMemory === memory.id));
  card.textContent = memory.title;
  return card;
};
let librarySearchTimer = null, revision = 1, postCount = 0, failBrowse = false;
let stored = Array.from({ length: 102 }, (_, index) => ({
  id: `m${String(index).padStart(3, '0')}`, title: `Memory ${index}`,
  content: `Memory ${index}`, mtype: index % 2 ? 'procedural' : 'semantic',
}));
Object.assign(state, {
  workspace: 'fixture', project: '', refreshEpoch: 1, scopedControllers: {}, scopedRequests: {},
  memories: stored.slice(0, 100), libraryTotal: 102, libraryPage: 0,
  libraryCursors: [null, '1:100'], libraryNextCursor: '1:100', libraryLoading: false,
  librarySelecting: false, selectedMemory: '', moveMemoryIds: new Set(),
});
document.getElementById('library-filter').value = '';
document.getElementById('library-type').value = '';
const api = async (path, options) => {
  calls.push({ path, body: options?.body });
  if (path === '/memories/reorder') {
    const count = ++postCount;
    if (gates.has(count)) await gates.get(count).promise;
    const byId = new Map(stored.map(item => [item.id, item]));
    stored = [...options.body.ids.map(id => byId.get(id)),
      ...stored.filter(item => !options.body.ids.includes(item.id))];
    revision++;
    return { reordered: options.body.ids.length };
  }
  if (failBrowse) throw new Error('Synthetic refresh failure');
  const params = new URL(path, 'http://fixture.invalid').searchParams;
  const cursor = params.get('cursor');
  if (cursor && Number(cursor.split(':')[0]) !== revision) {
    throw Object.assign(new Error('stale cursor'), { status: 409, code: 'cursor_stale' });
  }
  const offset = cursor ? Number(cursor.split(':')[1]) : 0;
  const filtered = stored.filter(item => (!params.get('q') || item.content.includes(params.get('q')))
    && (!params.get('mtype') || item.mtype === params.get('mtype')));
  return { memories: filtered.slice(offset, offset + 100), total_count: filtered.length,
    next_cursor: offset + 100 < filtered.length ? `${revision}:${offset + 100}` : null };
};
const hold = count => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  gates.set(count, { promise, release });
  return release;
};
const flush = async () => { for (let step = 0; step < 8; step++) await new Promise(setImmediate); };
const cards = () => byId('library-list').querySelectorAll('[role="option"]');
const keyboardMove = () => byId('library-list').listeners.keydown[0]({
  target: focused || cards()[0], altKey: true, key: 'ArrowDown', preventDefault() {},
});
"""


def run_library(scenario: str) -> dict:
    names = (
        "byId", "number", "empty", "node", "beginScopedRequest", "isCurrentScopedRequest",
        "loadMemories", "renderLibraryPaging", "refreshLibrary", "libraryReorderEnabled",
        "libraryOrderWrite", "persistLibraryOrder", "moveLibraryMemory", "dropLibraryMemory",
        "filteredMemories", "renderLibrary",
    )
    declarations = "\n".join(_ledger_declaration(name) for name in names)
    ledger = (ROOT / "engraphis/dashboard_assets/ledger.js").read_text(encoding="utf-8")
    next_binding = next(line for line in ledger.splitlines() if "byId('library-next').addEventListener('click'" in line)
    program = _HARNESS_PRELUDE + HARNESS + declarations + next_binding + "\nrenderLibrary();\n"
    program += "(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
    driver = "require('vm').runInNewContext(" + json.dumps(program) + ", {console, URL, URLSearchParams, AbortController, setImmediate, process});"
    result = subprocess.run([NODE, "-e", driver], cwd=ROOT, check=True,
                            capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


def test_successful_keyboard_reorder_refreshes_cursor_then_opens_next_page():
    result = run_library(r"""
keyboardMove(); await flush();
const first = { ids: cards().slice(0, 3).map(card => card.dataset.memoryId),
  focused: focused?.dataset.memoryId, focusConnected: cards().includes(focused), next: state.libraryNextCursor,
  cursors: [...state.libraryCursors], status: byId('library-reorder-status').textContent };
byId('library-next').click(); await flush();
console.log(JSON.stringify({ first, page: state.libraryPage, ids: state.memories.map(item => item.id), notices, calls }));
""")
    assert result["first"]["ids"] == ["m001", "m000", "m002"]
    assert result["first"]["focused"] == "m000"
    assert result["first"]["focusConnected"]
    assert result["first"]["next"] == "2:100"
    assert result["first"]["cursors"] == [None, "2:100"]
    assert "position 2" in result["first"]["status"]
    assert result["page"] == 1
    assert result["ids"] == ["m100", "m101"]
    assert not result["notices"]
    assert "cursor=2%3A100" in result["calls"][-1]["path"]


def test_queued_gestures_reload_only_after_the_last_order_write():
    result = run_library(r"""
const release = hold(1);
keyboardMove(); await flush(); keyboardMove(); await flush();
const pendingPosts = postCount;
release(); await flush();
console.log(JSON.stringify({ pendingPosts, posts: calls.filter(item => item.body),
  reads: calls.filter(item => !item.body), ids: cards().slice(0, 3).map(card => card.dataset.memoryId),
  cursor: state.libraryNextCursor, focused: focused?.dataset.memoryId }));
""")
    assert result["pendingPosts"] == 1
    assert [row["body"]["ids"][:3] for row in result["posts"]] == [
        ["m001", "m000", "m002"], ["m001", "m002", "m000"],
    ]
    assert len(result["reads"]) == 1
    assert result["ids"] == ["m001", "m002", "m000"]
    assert result["cursor"] == "3:100"
    assert result["focused"] == "m000"


def test_reload_keeps_filters_changed_while_the_save_was_pending():
    result = run_library(r"""
const release = hold(1); keyboardMove(); await flush();
byId('library-filter').value = 'Memory 10'; byId('library-type').value = 'procedural';
release(); await flush();
console.log(JSON.stringify({ calls, search: byId('library-filter').value,
  type: byId('library-type').value, workspace: state.workspace, project: state.project,
  ids: state.memories.map(item => item.id), page: state.libraryPage }));
""")
    assert result["search"] == "Memory 10"
    assert result["type"] == "procedural"
    assert result["workspace"] == "fixture" and result["project"] == ""
    assert result["ids"] == ["m101"]
    assert "q=Memory+10" in result["calls"][-1]["path"]
    assert "mtype=procedural" in result["calls"][-1]["path"]


def test_old_workspace_save_does_not_reset_the_new_scope_pagination():
    result = run_library(r"""
const release = hold(1); keyboardMove(); await flush();
state.workspace = 'other'; state.project = 'project-b'; state.refreshEpoch++;
state.libraryCursors = [null, 'other:100']; state.libraryNextCursor = 'other:100';
release(); await flush();
console.log(JSON.stringify({ cursor: state.libraryNextCursor, cursors: state.libraryCursors,
  reads: calls.filter(item => !item.body), workspace: state.workspace, project: state.project }));
""")
    assert result == {"cursor": "other:100", "cursors": [None, "other:100"], "reads": [],
                      "workspace": "other", "project": "project-b"}


def test_refresh_failure_keeps_old_cursors_disabled_without_claiming_save_failed():
    result = run_library(r"""
failBrowse = true; keyboardMove(); await flush();
console.log(JSON.stringify({ notices, cursor: state.libraryNextCursor,
  nextDisabled: byId('library-next').disabled, status: byId('library-reorder-status').textContent }));
""")
    assert result["cursor"] is None
    assert result["nextDisabled"]
    assert any("Order saved" in item and "refresh" in item for item in result["notices"])
    assert all("Could not save" not in item for item in result["notices"])
