/* Engraphis graph explorer: keyboard and screen-reader access to the Explore graph.

   The canvas is a visual summary. This module lists the loaded entities (most connected
   first) and relations as ordinary buttons with a shared filter, and drives the canvas
   keyboard cursor: with the canvas focused, arrow keys move between entities in the same
   order, Enter opens the selected entity's connections and Escape clears the selection.
   Entity names come from memory content, so every label is set with textContent. */
(function () {
  'use strict';

  const PAGE = 40;

  function create({ onOpen, onFocus, onClear }) {
    const byId = id => document.getElementById(id);
    let nodes = [];
    let links = [];
    let names = new Map();
    let shown = { nodes: [], links: [] };
    let limits = { nodes: PAGE, links: PAGE };
    let filter = '';
    let cursor = -1;
    let timer = null;

    const label = item => String((item && (item.name || item.label || item.id)) || 'entity');
    /* The renderer's force layout replaces link endpoints with node objects in place. */
    const endpoint = value => (value && typeof value === 'object' ? value.id : value);
    const typeLabel = type => String(type || 'entity').replace(/_/g, ' ');
    const plural = (value, noun) => `${Number(value || 0).toLocaleString()} ${noun}${Number(value) === 1 ? '' : 's'}`;

    function element(tag, className, content) {
      const item = document.createElement(tag);
      if (className) item.className = className;
      if (content !== undefined) item.textContent = String(content);
      return item;
    }

    function entityButton(item) {
      const control = element('button', 'graph-explorer-item');
      control.type = 'button';
      control.dataset.entity = String(item.id);
      control.append(
        element('span', 'graph-explorer-name', label(item)),
        element('span', 'graph-explorer-meta', `${typeLabel(item.etype)} · ${plural(item.relations, 'relation')}`),
      );
      control.addEventListener('click', () => {
        select(item.id);
        onOpen(item);
      });
      return control;
    }

    function endpointButton(id) {
      const item = names.get(id);
      const control = element('button', 'graph-entity-link', item ? label(item) : String(id));
      control.type = 'button';
      control.dataset.entity = String(id);
      control.disabled = !item;
      if (item) control.addEventListener('click', () => {
        select(item.id);
        onOpen(item);
      });
      return control;
    }

    function relationRow(link) {
      const row = element('div', 'graph-explorer-item graph-relation-row');
      row.setAttribute('role', 'listitem');
      row.append(
        endpointButton(link.source),
        element('span', 'graph-relation-label', String(link.label || link.layer || 'related to').replace(/_/g, ' ')),
        endpointButton(link.target),
        element('span', 'graph-explorer-meta', String(link.layer || 'semantic')),
      );
      return row;
    }

    function moreButton(kind, remaining) {
      const control = element('button', 'graph-explorer-more text-button',
        `Show ${Math.min(PAGE, remaining).toLocaleString()} more ${kind === 'nodes' ? 'entities' : 'relations'}`);
      control.type = 'button';
      control.addEventListener('click', () => {
        limits[kind] += PAGE;
        render(false);
        const list = byId(kind === 'nodes' ? 'graph-entity-list' : 'graph-relation-list');
        const items = list.querySelectorAll(kind === 'nodes' ? '.graph-explorer-item' : '.graph-relation-row button');
        const next = items[limits[kind] - PAGE];
        if (next) next.focus();
      });
      return control;
    }

    function matches(text) {
      return String(text || '').toLowerCase().includes(filter);
    }

    function render(reset) {
      const entityList = byId('graph-entity-list');
      const relationList = byId('graph-relation-list');
      if (!entityList || !relationList) return;
      if (reset) {
        limits = { nodes: PAGE, links: PAGE };
        shown = filter ? {
          nodes: nodes.filter(item => matches(label(item)) || matches(item.etype)),
          links: links.filter(link => matches(label(names.get(link.source)))
            || matches(label(names.get(link.target))) || matches(link.label) || matches(link.layer)),
        } : { nodes, links };
      }
      const nodePage = shown.nodes.slice(0, limits.nodes);
      const linkPage = shown.links.slice(0, limits.links);
      byId('graph-explorer-node-count').textContent = `${nodePage.length.toLocaleString()} of ${shown.nodes.length.toLocaleString()}`;
      byId('graph-explorer-edge-count').textContent = `${linkPage.length.toLocaleString()} of ${shown.links.length.toLocaleString()}`;
      entityList.replaceChildren();
      relationList.replaceChildren();
      if (!nodes.length) {
        entityList.append(element('p', 'empty-state', 'Open Explore to load the graph.'));
        relationList.append(element('p', 'empty-state', 'Open Explore to load the graph.'));
        return;
      }
      if (!nodePage.length) entityList.append(element('p', 'empty-state', 'No entities match this filter.'));
      nodePage.forEach(item => {
        const row = element('div');
        row.setAttribute('role', 'listitem');
        row.append(entityButton(item));
        entityList.append(row);
      });
      if (nodePage.length < shown.nodes.length) entityList.append(moreButton('nodes', shown.nodes.length - nodePage.length));
      if (!linkPage.length) relationList.append(element('p', 'empty-state', 'No relations match this filter.'));
      linkPage.forEach(link => relationList.append(relationRow(link)));
      if (linkPage.length < shown.links.length) relationList.append(moreButton('links', shown.links.length - linkPage.length));
      highlight(cursor >= 0 && nodes[cursor] ? nodes[cursor].id : '');
    }

    function highlight(id) {
      const list = byId('graph-entity-list');
      if (!list) return;
      list.querySelectorAll('[data-entity]').forEach(control => {
        const active = id !== '' && control.dataset.entity === String(id);
        control.classList.toggle('active', active);
        if (active) control.setAttribute('aria-current', 'true');
        else control.removeAttribute('aria-current');
      });
    }

    function select(id) {
      cursor = nodes.findIndex(item => item.id === id);
      highlight(cursor >= 0 ? id : '');
    }

    function setData(data) {
      const source = data || { nodes: [], links: [] };
      const known = new Set((source.nodes || [])
        .filter(item => item && item.id !== undefined && item.id !== null)
        .map(item => item.id));
      links = (source.links || [])
        .filter(Boolean)
        .map(link => ({ ...link, source: endpoint(link.source), target: endpoint(link.target) }))
        .filter(link => known.has(link.source) || known.has(link.target));
      /* `degree` may be a weighted score; the count shown to people is real relations. */
      const relations = new Map();
      links.forEach(link => [link.source, link.target].forEach(id => {
        relations.set(id, (relations.get(id) || 0) + 1);
      }));
      nodes = (source.nodes || [])
        .filter(item => item && known.has(item.id))
        .map(item => ({ ...item, relations: relations.get(item.id) || 0 }))
        .sort((left, right) => (Number(right.degree) || 0) - (Number(left.degree) || 0)
          || right.relations - left.relations
          || label(left).localeCompare(label(right)));
      names = new Map(nodes.map(item => [item.id, item]));
      cursor = -1;
      render(true);
    }

    function setFilter(value) {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        filter = String(value || '').trim().toLowerCase();
        render(true);
      }, 120);
    }

    /* Canvas keyboard cursor. Returns true when the key was handled. */
    function handleKey(event) {
      if (!nodes.length) return false;
      const key = event.key;
      if (key === 'Escape') {
        cursor = -1;
        highlight('');
        onClear();
        return true;
      }
      if (key === 'Enter' || key === ' ') {
        const item = nodes[Math.max(0, cursor)];
        if (cursor < 0) {
          cursor = 0;
          onFocus(item, 1, nodes.length);
        }
        highlight(item.id);
        onOpen(item);
        return true;
      }
      const steps = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (key === 'Home') cursor = 0;
      else if (key === 'End') cursor = nodes.length - 1;
      else if (steps[key]) cursor = cursor < 0 ? (steps[key] > 0 ? 0 : nodes.length - 1)
        : (cursor + steps[key] + nodes.length) % nodes.length;
      else return false;
      const item = nodes[cursor];
      highlight(item.id);
      onFocus(item, cursor + 1, nodes.length);
      return true;
    }

    const filterInput = byId('graph-explorer-filter');
    if (filterInput) filterInput.addEventListener('input', event => setFilter(event.currentTarget.value));

    return Object.freeze({ setData, select, handleKey });
  }

  window.EngraphisGraphExplorer = Object.freeze({ create });
})();
