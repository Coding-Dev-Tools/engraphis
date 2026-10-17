/* Engraphis safe Markdown renderer.

   Memory content is untrusted input (SECURITY.md §1): it may come from imported files,
   connected agents or a synced peer. This renderer therefore builds DOM nodes with
   createElement and textContent only. It never parses HTML, never assigns innerHTML,
   never sets inline styles, ids or names, and only makes absolute http(s) and mailto
   URLs clickable. Raw HTML in a memory is shown as text. That keeps rendered content
   inert under the dashboard's strict Content-Security-Policy and safe from DOM clobbering.

   It is also bounded: every scan is linear in the input or capped, tables are limited in
   size, and the number of elements one render may create is capped, so adversarial content
   cannot freeze the page that is meant to let someone review and retire it.

   Supported syntax: ATX headings, paragraphs (single newlines are kept as line breaks),
   emphasis, strong, strikethrough, inline code, fenced and indented code blocks,
   block quotes, ordered, unordered and task lists, horizontal rules, pipe tables,
   [text](url) links, <autolinks>, bare http(s) URLs and Obsidian [[wikilinks]]. Images
   are shown as links so a memory cannot load remote content into the page. */
(function () {
  'use strict';

  const MAX_SOURCE_CHARS = 200000;
  const MAX_NESTING = 8;
  const MAX_LINK_SCAN = 2000;
  /* Characters that failed link scans may examine in one inline run before `[` is text. */
  const MAX_FAILED_LINK_SCAN = 200000;
  const MAX_WIKILINK_CHARS = 300;
  const MAX_TABLE_COLUMNS = 64;
  const MAX_TABLE_ROWS = 500;
  const MAX_ELEMENTS = 50000;
  const ESCAPABLE = /[!-/:-@[-`{-~]/;

  /* Elements the current render may still create, and whether it had to stop early; both
     are reset by render(). Nested lists and quotes share them with the top level. */
  let budget = MAX_ELEMENTS;
  let truncated = false;
  const exhausted = () => budget <= 0;

  function element(tag, className, content) {
    budget -= 1;
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (content !== undefined && content !== '') item.textContent = String(content);
    return item;
  }

  const isSpace = character => character === ' ' || character === '\t';
  const isWhitespace = character => character !== undefined && /\s/.test(character);

  function trimSpaces(line) {
    let start = 0;
    let end = line.length;
    while (start < end && isSpace(line[start])) start += 1;
    while (end > start && isSpace(line[end - 1])) end -= 1;
    return line.slice(start, end);
  }

  function safeHref(raw) {
    const value = String(raw || '').trim().replace(/^<|>$/g, '');
    if (!/^(https?:\/\/|mailto:)/i.test(value)) return '';
    try {
      const url = new URL(value);
      if (url.protocol !== 'http:' && url.protocol !== 'https:' && url.protocol !== 'mailto:') return '';
      return url.href;
    } catch (_) {
      return '';
    }
  }

  function appendLink(parent, href, label, depth, image) {
    const safe = safeHref(href);
    if (!safe) {
      parent.append(document.createTextNode(image ? `[image: ${label || href}]` : (label || href)));
      return;
    }
    const link = element('a', image ? 'md-image-link' : '');
    link.href = safe;
    link.target = '_blank';
    link.rel = 'noopener noreferrer nofollow';
    link.title = safe;
    if (image) link.textContent = `Image: ${label || safe}`;
    else renderInline(label || safe, link, depth + 1, true);
    parent.append(link);
  }

  /* `[label](destination "title")`, starting at the opening bracket. Failed attempts are
     charged to `scan` so a run of unmatched brackets cannot cost more than a fixed budget. */
  function parseLink(source, start, scan) {
    if (scan.remaining <= 0) return null;
    let depth = 0;
    let index = start;
    const limit = Math.min(source.length, start + MAX_LINK_SCAN);
    const fail = () => {
      scan.remaining -= Math.max(1, index - start);
      return null;
    };
    for (; index < limit; index += 1) {
      const character = source[index];
      if (character === '\\') { index += 1; continue; }
      if (character === '[') depth += 1;
      else if (character === ']') {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    if (depth !== 0 || source[index] !== ']' || source[index + 1] !== '(') return fail();
    const label = source.slice(start + 1, index);
    let cursor = index + 2;
    let parens = 0;
    let destination = '';
    while (cursor < limit) {
      const character = source[cursor];
      if (character === '\\' && cursor + 1 < limit) {
        destination += source[cursor + 1];
        cursor += 2;
        continue;
      }
      if (character === '(') parens += 1;
      if (character === ')') {
        if (parens === 0) break;
        parens -= 1;
      }
      if (isWhitespace(character)) break;
      destination += character;
      cursor += 1;
    }
    index = cursor;
    if (isWhitespace(source[cursor])) {
      /* Optional title: whitespace, then "…", '…' or (…), then whitespace and `)`. */
      while (cursor < limit && isWhitespace(source[cursor])) cursor += 1;
      const open = source[cursor];
      if (open === '"' || open === "'" || open === '(') {
        const close = source.indexOf(open === '(' ? ')' : open, cursor + 1);
        if (close < 0 || close >= limit) {
          index = limit;
          return fail();
        }
        cursor = close + 1;
        while (cursor < limit && isWhitespace(source[cursor])) cursor += 1;
      }
      index = cursor;
    }
    if (source[cursor] !== ')') return fail();
    return { label, href: destination, end: cursor + 1 };
  }

  /* Every maximal run of backticks in `source`, grouped by exact length. A code span closes
     only on a run of the same length (CommonMark), so finding a closer is a binary search
     here instead of a substring scan, which stays fast however the runs are arranged. */
  function backtickRuns(source) {
    const runs = new Map();
    for (let index = source.indexOf('`'); index > -1;) {
      let end = index + 1;
      while (source[end] === '`') end += 1;
      const starts = runs.get(end - index);
      if (starts) starts.push(index);
      else runs.set(end - index, [index]);
      index = source.indexOf('`', end);
    }
    return runs;
  }

  /* Start of the first run of exactly `length` backticks at or after `from`, or -1. */
  function nextRun(runs, length, from) {
    const starts = runs.get(length);
    if (!starts) return -1;
    let low = 0;
    let high = starts.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (starts[middle] < from) low = middle + 1;
      else high = middle;
    }
    return low < starts.length ? starts[low] : -1;
  }

  /* Index of the closing delimiter, or -1. Runs of the delimiter character that cannot
     close this span (for example `**` while looking for `*`) are skipped as a whole, and so
     are code spans, which emphasis cannot cross. */
  function findCloser(source, from, delimiter, runs) {
    const mark = delimiter[0];
    let index = from;
    while (index < source.length) {
      const character = source[index];
      if (character === '\\') { index += 2; continue; }
      if (character === '`') {
        let length = 1;
        while (source[index + length] === '`') length += 1;
        const close = nextRun(runs, length, index + length);
        index = close > -1 ? close + length : index + length;
        continue;
      }
      if (character === mark) {
        let run = 1;
        while (source[index + run] === mark) run += 1;
        /* In a run of three, the outer delimiter closes last: `*a **b***` and `***c***`
           both nest correctly when the closer is taken from the end of the run. */
        const offset = run === delimiter.length ? 0 : run === 3 ? 3 - delimiter.length : -1;
        if (offset >= 0 && index > from && !isWhitespace(source[index - 1])) return index + offset;
        index += run;
        continue;
      }
      index += 1;
    }
    return -1;
  }

  function renderInline(source, parent, depth = 0, insideLink = false) {
    let buffer = '';
    const failed = Object.create(null);
    const runs = backtickRuns(source);
    const linkScan = { remaining: MAX_FAILED_LINK_SCAN };
    const flush = () => {
      if (buffer) parent.append(document.createTextNode(buffer));
      buffer = '';
    };
    let index = 0;
    while (index < source.length) {
      if (exhausted()) {
        buffer += source.slice(index);
        break;
      }
      const character = source[index];
      const rest = source.slice(index, index + 12);
      if (character === '\\' && index + 1 < source.length && ESCAPABLE.test(source[index + 1])) {
        buffer += source[index + 1];
        index += 2;
        continue;
      }
      if (character === '\n') {
        flush();
        parent.append(element('br'));
        index += 1;
        continue;
      }
      if (character === '`') {
        let length = 1;
        while (source[index + length] === '`') length += 1;
        const close = nextRun(runs, length, index + length);
        if (close > -1) {
          flush();
          let code = source.slice(index + length, close).replace(/\n/g, ' ');
          if (code.length > 1 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim()) {
            code = code.slice(1, -1);
          }
          parent.append(element('code', '', code));
          index = close + length;
          continue;
        }
        buffer += source.slice(index, index + length);
        index += length;
        continue;
      }
      if (!insideLink && depth < MAX_NESTING) {
        if (rest.startsWith('[[')) {
          const window = source.slice(index + 2, index + 2 + MAX_WIKILINK_CHARS + 2);
          const close = window.indexOf(']]');
          const inner = close > -1 ? window.slice(0, close) : '';
          if (close > 0 && !/[\n[\]]/.test(inner) && inner.length <= MAX_WIKILINK_CHARS) {
            flush();
            const parts = inner.split('|');
            parent.append(element('span', 'md-wikilink', (parts[1] || parts[0]).trim()));
            index += 2 + close + 2;
            continue;
          }
        }
        if ((character === '[' || rest.startsWith('![')) && !rest.startsWith('[[')) {
          const image = character === '!';
          const link = parseLink(source, image ? index + 1 : index, linkScan);
          if (link) {
            flush();
            appendLink(parent, link.href, link.label, depth, image);
            index = link.end;
            continue;
          }
        }
        if (character === '<') {
          const auto = /^<((?:https?:\/\/|mailto:)[^\s<>]+)>/i.exec(source.slice(index, index + MAX_LINK_SCAN));
          if (auto) {
            flush();
            appendLink(parent, auto[1], auto[1], depth, false);
            index += auto[0].length;
            continue;
          }
        }
        if ((character === 'h' || character === 'H') && /^https?:\/\//i.test(rest)
          && (index === 0 || /[\s(*_~]/.test(source[index - 1]))) {
          const bare = /^https?:\/\/[^\s<]*[^\s<.,:;"')\]!?*_~]/i.exec(source.slice(index, index + MAX_LINK_SCAN));
          if (bare) {
            flush();
            appendLink(parent, bare[0], bare[0], depth, false);
            index += bare[0].length;
            continue;
          }
        }
      }
      if ((character === '*' || character === '_' || character === '~') && depth < MAX_NESTING) {
        const match = /^(\*\*|__|~~|\*|_)/.exec(source.slice(index, index + 2));
        const delimiter = match ? match[1] : '';
        const intraword = delimiter[0] === '_' && index > 0 && /[\p{L}\p{N}]/u.test(source[index - 1]);
        const next = source[index + delimiter.length];
        if (delimiter && delimiter !== '~' && !intraword && next && !isWhitespace(next) && !failed[delimiter]) {
          const close = findCloser(source, index + delimiter.length, delimiter, runs);
          if (close > -1) {
            flush();
            const tag = delimiter === '~~' ? 'del' : delimiter.length === 2 ? 'strong' : 'em';
            const span = element(tag);
            renderInline(source.slice(index + delimiter.length, close), span, depth + 1, insideLink);
            parent.append(span);
            index = close + delimiter.length;
            continue;
          }
          failed[delimiter] = true;
        }
        buffer += delimiter || character;
        index += (delimiter || character).length;
        continue;
      }
      buffer += character;
      index += 1;
    }
    flush();
  }

  /* Line terminators are normalised before splitting, so `.` matches every character of a
     line and these patterns cannot backtrack across an embedded U+2028 or U+2029. */
  const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  const FENCE_CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
  const HEADING_OPEN = /^ {0,3}(#{1,6})(?=[ \t]|$)/;
  const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const QUOTE = /^ {0,3}>[ ]?/;
  const LIST_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)(.*)$/;
  const blank = line => !line.trim();

  /* An ATX heading without regex backtracking: `# Title ##` → level 1, "Title". */
  function parseHeading(line) {
    const open = HEADING_OPEN.exec(line);
    if (!open) return null;
    let title = trimSpaces(line.slice(open[0].length));
    let end = title.length;
    while (end > 0 && title[end - 1] === '#') end -= 1;
    if (end === 0) title = '';
    else if (end < title.length && isSpace(title[end - 1])) title = trimSpaces(title.slice(0, end));
    return { level: open[1].length, title };
  }

  function tableCells(line) {
    let value = line.trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|') && !value.endsWith('\\|')) value = value.slice(0, -1);
    const cells = [];
    let current = '';
    for (let index = 0; index < value.length; index += 1) {
      if (value[index] === '\\' && value[index + 1] === '|') {
        current += '|';
        index += 1;
      } else if (value[index] === '|') {
        cells.push(current.trim());
        current = '';
      } else current += value[index];
    }
    cells.push(current.trim());
    return cells;
  }

  /* Number of columns a delimiter row such as `| :-- | --: |` declares, or 0. */
  function dividerColumns(line) {
    let value = line.trim();
    if (!value || !/^[|:\- \t]+$/.test(value) || !value.includes('-')) return 0;
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    const cells = value.split('|');
    return cells.every(cell => /^:?-+:?$/.test(cell.trim())) ? cells.length : 0;
  }

  function startsTable(lines, index) {
    const line = lines[index];
    if (!line.includes('|') || index + 1 >= lines.length) return false;
    const columns = dividerColumns(lines[index + 1]);
    return columns > 0 && columns <= MAX_TABLE_COLUMNS && tableCells(line).length === columns;
  }

  function startsBlock(lines, index) {
    const line = lines[index];
    return FENCE.test(line) || HEADING_OPEN.test(line) || RULE.test(line) || QUOTE.test(line)
      || LIST_ITEM.test(line) || startsTable(lines, index);
  }

  function renderTable(lines, index, parent, depth) {
    const header = tableCells(lines[index]);
    const wrap = element('div', 'md-table-wrap');
    const table = element('table');
    const head = element('thead');
    const headRow = element('tr');
    header.forEach(cell => {
      const th = element('th');
      th.scope = 'col';
      renderInline(cell, th, depth + 1);
      headRow.append(th);
    });
    head.append(headRow);
    table.append(head);
    const body = element('tbody');
    let cursor = index + 2;
    let rows = 0;
    while (cursor < lines.length && !blank(lines[cursor]) && lines[cursor].includes('|')
      && rows < MAX_TABLE_ROWS && !exhausted()) {
      const row = element('tr');
      const cells = tableCells(lines[cursor]);
      header.forEach((_, column) => {
        const td = element('td');
        renderInline(cells[column] || '', td, depth + 1);
        row.append(td);
      });
      body.append(row);
      cursor += 1;
      rows += 1;
    }
    if (rows) table.append(body);
    wrap.append(table);
    parent.append(wrap);
    return cursor;
  }

  function renderList(lines, index, parent, depth, options) {
    const first = LIST_ITEM.exec(lines[index]);
    const ordered = /\d/.test(first[2]);
    const list = element(ordered ? 'ol' : 'ul');
    if (ordered) {
      const start = parseInt(first[2], 10);
      if (Number.isFinite(start) && start !== 1) list.start = start;
    }
    const markerKind = ordered ? first[2].slice(-1) : first[2];
    let cursor = index;
    while (cursor < lines.length && !exhausted()) {
      const match = LIST_ITEM.exec(lines[cursor]);
      if (!match) break;
      const sameKind = ordered ? /\d/.test(match[2]) && match[2].slice(-1) === markerKind
        : match[2] === markerKind;
      if (!sameKind || match[1].length > first[1].length + 1) break;
      const contentIndent = match[1].length + match[2].length + Math.max(1, Math.min(4, match[3].length));
      const itemLines = [match[4]];
      cursor += 1;
      while (cursor < lines.length) {
        const line = lines[cursor];
        if (blank(line)) {
          const following = lines[cursor + 1];
          if (following !== undefined && /^\s*/.exec(following)[0].length >= contentIndent) {
            itemLines.push('');
            cursor += 1;
            continue;
          }
          break;
        }
        const indent = /^\s*/.exec(line)[0].length;
        if (indent >= contentIndent) {
          itemLines.push(line.slice(Math.min(indent, contentIndent)));
          cursor += 1;
          continue;
        }
        if (LIST_ITEM.test(line) || startsBlock(lines, cursor)) break;
        itemLines.push(line.trim());
        cursor += 1;
      }
      const item = element('li');
      const task = /^\[([ xX])\][ \t]+/.exec(itemLines[0]);
      if (task) {
        const checked = task[1].toLowerCase() === 'x';
        item.className = 'md-task';
        const box = element('span', 'md-task-box', checked ? '☑' : '☐');
        box.setAttribute('role', 'img');
        box.setAttribute('aria-label', checked ? 'Done' : 'Not done');
        item.append(box, document.createTextNode(' '));
        itemLines[0] = itemLines[0].slice(task[0].length);
      }
      const simple = itemLines.length === 1 || itemLines.every(line => line && !startsBlock([line], 0));
      if (simple) renderInline(itemLines.join('\n'), item, depth + 1);
      else renderBlocks(itemLines, item, depth + 1, { ...options, tight: true });
      list.append(item);
      if (cursor < lines.length && blank(lines[cursor])) {
        const next = lines[cursor + 1];
        const nextMatch = next !== undefined ? LIST_ITEM.exec(next) : null;
        if (nextMatch && nextMatch[1].length <= first[1].length + 1) cursor += 1;
      }
    }
    parent.append(list);
    return cursor;
  }

  function renderBlocks(lines, parent, depth, options) {
    if (depth > MAX_NESTING) {
      const paragraph = element('p');
      paragraph.textContent = lines.join('\n');
      parent.append(paragraph);
      return;
    }
    let index = 0;
    while (index < lines.length) {
      if (exhausted()) {
        /* Out of element budget: the rest stays readable as one block of plain text. */
        const rest = lines.slice(index).join('\n');
        if (rest.trim()) parent.append(element('p', 'md-plain', rest));
        truncated = true;
        return;
      }
      const line = lines[index];
      if (blank(line)) { index += 1; continue; }

      const fence = FENCE.exec(line);
      if (fence) {
        const marker = fence[1];
        const body = [];
        index += 1;
        while (index < lines.length) {
          const close = FENCE_CLOSE.exec(lines[index]);
          if (close && close[1][0] === marker[0] && close[1].length >= marker.length) {
            index += 1;
            break;
          }
          body.push(lines[index]);
          index += 1;
        }
        const pre = element('pre', 'md-code');
        const code = element('code', '', body.join('\n'));
        const language = fence[2].trim().split(/\s+/)[0];
        if (language && /^[\w+#.-]{1,30}$/.test(language)) pre.dataset.language = language;
        pre.append(code);
        parent.append(pre);
        continue;
      }

      const heading = parseHeading(line);
      if (heading) {
        const level = Math.min(6, heading.level + (options.headingOffset || 0));
        const title = element(`h${level}`, 'md-heading');
        renderInline(heading.title, title, depth + 1);
        parent.append(title);
        index += 1;
        continue;
      }

      if (RULE.test(line)) {
        parent.append(element('hr'));
        index += 1;
        continue;
      }

      if (QUOTE.test(line)) {
        const quoted = [];
        while (index < lines.length && !blank(lines[index])) {
          const current = lines[index];
          if (QUOTE.test(current)) quoted.push(current.replace(QUOTE, ''));
          else if (quoted.length && !startsBlock(lines, index)) quoted.push(current);
          else break;
          index += 1;
        }
        const quote = element('blockquote');
        renderBlocks(quoted, quote, depth + 1, options);
        parent.append(quote);
        continue;
      }

      if (LIST_ITEM.test(line)) {
        index = renderList(lines, index, parent, depth, options);
        continue;
      }

      if (startsTable(lines, index)) {
        index = renderTable(lines, index, parent, depth);
        continue;
      }

      if (/^( {4}|\t)/.test(line) && !options.tight) {
        const body = [];
        while (index < lines.length && (/^( {4}|\t)/.test(lines[index]) || blank(lines[index]))) {
          body.push(lines[index].replace(/^( {4}|\t)/, ''));
          index += 1;
        }
        while (body.length && blank(body[body.length - 1])) body.pop();
        const pre = element('pre', 'md-code');
        pre.append(element('code', '', body.join('\n')));
        parent.append(pre);
        continue;
      }

      const paragraphLines = [];
      while (index < lines.length && !blank(lines[index])
        && (!paragraphLines.length || !startsBlock(lines, index))) {
        paragraphLines.push(trimSpaces(lines[index]));
        index += 1;
      }
      const paragraph = element('p');
      renderInline(paragraphLines.join('\n'), paragraph, depth + 1);
      parent.append(paragraph);
    }
  }

  /* Render Markdown source into a DocumentFragment of inert DOM nodes.
     `headingOffset` shifts heading levels so `#` inside a memory nests under the
     page's own headings (the default turns `#` into an h3). */
  function render(source, { headingOffset = 2 } = {}) {
    budget = MAX_ELEMENTS;
    truncated = false;
    const fragment = document.createDocumentFragment();
    let value = String(source == null ? '' : source);
    const options = { headingOffset, tight: false };
    if (value.length > MAX_SOURCE_CHARS) {
      value = value.slice(0, MAX_SOURCE_CHARS);
      truncated = true;
    }
    const lines = value.replace(/\r\n?|[\u2028\u2029]/g, '\n').replace(/\u0000/g, '\uFFFD').split('\n');
    renderBlocks(lines, fragment, 0, options);
    if (truncated || exhausted()) {
      fragment.append(element('p', 'md-truncated', 'Preview truncated.'));
    }
    return fragment;
  }

  function renderInto(target, source, options) {
    if (!target) return;
    target.replaceChildren(render(source, options));
  }

  window.EngraphisMarkdown = Object.freeze({ render, renderInto, safeHref });
})();
