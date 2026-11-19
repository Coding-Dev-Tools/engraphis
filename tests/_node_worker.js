'use strict';
/*
 * Persistent request broker for the dashboard graph tests.
 *
 * The broker process reads one JSON request per line, runs it on a fresh Node
 * `worker_thread` (its own isolate and `globalThis`, so no state leaks between
 * calls), and writes one JSON response per line. Asset sources are read from disk
 * once and handed to each thread, so the multi-thousand-line engines are never
 * re-read; each request still compiles its own `vm.Script` inside the short-lived
 * worker. A request timeout terminates a stuck thread without killing the broker.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { Worker } = require('worker_threads');

const THREAD = path.join(__dirname, '_node_thread.js');
const sourceCache = new Map();

function sourceFor(target) {
  const resolved = path.resolve(String(target));
  try {
    const stat = fs.statSync(resolved);
    if (!stat.isFile()) {
      sourceCache.delete(resolved);
      return null;
    }
    const cached = sourceCache.get(resolved);
    if (cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size) {
      return cached.buffer;
    }
    const buffer = fs.readFileSync(resolved);
    sourceCache.set(resolved, { buffer, mtimeMs: stat.mtimeMs, size: stat.size });
    return buffer;
  } catch (error) {
    // A miss is never cached: a file a test creates later must be read next time,
    // and a file that changed on disk must not serve the previous bytes.
    sourceCache.delete(resolved);
    return null;
  }
}

function runRequest(request) {
  return new Promise(resolve => {
    const timeoutMs = Number(request.timeoutMs) > 0 ? Number(request.timeoutMs) : 300000;
    const sources = {};
    for (const arg of request.args || []) {
      const buffer = sourceFor(arg);
      if (buffer) sources[String(arg)] = buffer;
    }

    let settled = false;
    let stderrText = '';
    let worker;
    const finish = response => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(response);
      if (worker) {
        try {
          worker.terminate();
        } catch (ignored) {
          /* already gone */
        }
      }
    };
    const timer = setTimeout(() => finish({
      id: request.id,
      error: 'node request timed out after ' + timeoutMs + 'ms',
      stdout: '',
      stderr: stderrText,
    }), timeoutMs);

    try {
      worker = new Worker(THREAD, {
        workerData: { request: Object.assign({}, request, { sources }) },
        stdout: true,
        stderr: true,
      });
    } catch (error) {
      finish(errorResponse(request.id, error, stderrText));
      return;
    }

    worker.stdout.on('data', () => {});
    worker.stderr.on('data', chunk => {
      if (stderrText.length < 8192) stderrText += String(chunk);
    });
    worker.on('message', finish);
    worker.on('error', error => finish(errorResponse(request.id, error, stderrText)));
    worker.on('exit', code => {
      if (!settled) {
        finish(errorResponse(
          request.id,
          'node worker exited with code ' + code
            + (stderrText ? '\n' + stderrText : ''),
          stderrText,
        ));
      }
    });
  });
}

function errorResponse(id, error, stderr) {
  return {
    id: id === undefined ? null : id,
    error: error && (error.stack || error.message || String(error)),
    stdout: '',
    stderr: stderr || '',
  };
}

function reportBrokerFailure(error) {
  try {
    process.stderr.write('node broker failure: '
      + (error && (error.stack || error.message || String(error))) + '\n');
  } catch (ignored) {
    /* no channel left to report it */
  }
}

let queue = Promise.resolve();
const reader = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
reader.on('line', line => {
  if (!line.trim()) return;
  let request = null;
  queue = queue.then(async () => {
    let response;
    try {
      request = JSON.parse(line);
      response = await runRequest(request);
    } catch (error) {
      response = errorResponse(request && request.id, error);
    }
    process.stdout.write(JSON.stringify(response) + '\n');
  }).catch(error => {
    // A rejected queue promise would skip every later request, so report the cause
    // and answer this request instead of wedging the broker.
    reportBrokerFailure(error);
    try {
      process.stdout.write(
        JSON.stringify(errorResponse(request && request.id, error)) + '\n',
      );
    } catch (ignored) {
      /* stdout is gone; stderr already carries the cause */
    }
  });
});
reader.on('error', error => {
  reportBrokerFailure(error);
  process.exit(1);
});
reader.on('close', () => {
  process.exit(0);
});
