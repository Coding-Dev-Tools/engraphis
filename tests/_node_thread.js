'use strict';
/*
 * One fresh worker_thread per request. A worker thread starts with its own isolate
 * and `globalThis`, so `window`/`globalThis`/timers/mock DOM and `Math` patches are
 * private to the request while hot numeric loops keep normal V8 performance (a
 * contextified `vm` global deoptimizes property access by an order of magnitude).
 * The request is compiled with `vm.Script` and run in this worker's own context so
 * the supplied filename appears in stack traces.
 */

const { parentPort, workerData } = require('worker_threads');
const fs = require('fs');
const util = require('util');
const vm = require('vm');

const request = workerData.request || {};
const realProcess = process;
const realSetTimeout = setTimeout;
const realClearTimeout = clearTimeout;
const realSetInterval = setInterval;
const realClearInterval = clearInterval;
const realSetImmediate = setImmediate;
const realClearImmediate = clearImmediate;

const outputs = [];
const errors = [];
const timers = new Map();
const events = new Map();
let exitCode = 0;
let exited = false;

/*
 * `process.exit(code)` in real Node stops the current turn immediately: nothing after
 * the call runs. A plain `return` would let the rest of the script continue, so the
 * exit is signalled by a throw that the timer/emitter wrappers below unwind cleanly.
 */
class ExitSignal {
  constructor(code) {
    this.code = code;
  }
}

const format = args => util.format(...args);
const formatError = error => (error && (error.stack || error.message)) || String(error);

// Once `process.exit` has run, real Node executes nothing else: later console output
// belongs to an unwinding handler and must not be reported as the request's output.
const captureConsole = {
  ...console,
  log: (...args) => { if (!exited) outputs.push(format(args) + '\n'); },
  info: (...args) => { if (!exited) outputs.push(format(args) + '\n'); },
  debug: (...args) => { if (!exited) outputs.push(format(args) + '\n'); },
  warn: (...args) => { if (!exited) errors.push(format(args) + '\n'); },
  error: (...args) => { if (!exited) errors.push(format(args) + '\n'); },
};

const proc = {
  argv: ['node'].concat(request.args || []),
  env: Object.assign({}, realProcess.env, request.env || {}),
  platform: realProcess.platform,
  arch: realProcess.arch,
  versions: realProcess.versions,
  pid: realProcess.pid,
  cwd: () => realProcess.cwd(),
  nextTick: (callback, ...args) => realProcess.nextTick(() => runCallback(callback, args)),
  hrtime: (...args) => realProcess.hrtime(...args),
  memoryUsage: () => realProcess.memoryUsage(),
  on(type, handler) {
    const list = events.get(type) || [];
    list.push(handler);
    events.set(type, list);
    return proc;
  },
  once(type, handler) {
    const wrapped = (...args) => {
      proc.off(type, wrapped);
      handler(...args);
    };
    return proc.on(type, wrapped);
  },
  off(type, handler) {
    const list = events.get(type);
    if (list) events.set(type, list.filter(item => item !== handler));
    return proc;
  },
  removeListener(type, handler) {
    return proc.off(type, handler);
  },
  emit(type, ...args) {
    const list = (events.get(type) || []).slice();
    for (const handler of list) {
      try {
        handler(...args);
      } catch (error) {
        if (error instanceof ExitSignal) return true;
        throw error;
      }
    }
    return true;
  },
  exit(code) {
    if (!exited) {
      exitCode = code === undefined ? 0 : Number(code);
      exited = true;
      scheduleDrainCheck();
    }
    // Unwind every caller, including a handler reacting to the first sentinel.
    throw new ExitSignal(exitCode);
  },
  get exitCode() {
    return exitCode;
  },
  set exitCode(value) {
    if (value === undefined || value === null) {
      exitCode = 0;
      return;
    }
    const numeric = Number(value);
    if (Number.isFinite(numeric)) exitCode = numeric;
  },
  abort() {},
  stdout: {
    write(chunk, callback) {
      if (exited) return false;
      outputs.push(String(chunk));
      if (typeof callback === 'function') callback();
      return true;
    },
  },
  stderr: {
    write(chunk, callback) {
      if (exited) return false;
      errors.push(String(chunk));
      if (typeof callback === 'function') callback();
      return true;
    },
  },
};

function forwardEventHandlers(type, value) {
  const handlers = events.get(type);
  if (!handlers || !handlers.length) return false;
  for (const handler of handlers.slice()) {
    try {
      handler(value);
    } catch (error) {
      if (error instanceof ExitSignal) {
        exited = true;
        scheduleDrainCheck();
        return true;
      }
      // A throwing handler fails the request, exactly as Node does: an
      // `uncaughtException` handler that throws exits with code 7, and any other
      // handler that throws exits non-zero. Swallowing it here would answer
      // `code: 0` for a request that really failed.
      errors.push(formatError(error) + '\n');
      exitCode = type === 'uncaughtException' ? (exitCode || 7) : (exitCode || 1);
      exited = true;
      scheduleDrainCheck();
      return true;
    }
  }
  return true;
}

realProcess.on('unhandledRejection', reason => {
  // A rejection carrying the exit sentinel is the unwinding of `process.exit`, not a
  // failure of the request. Nothing reported after an exit happened, either.
  if (reason instanceof ExitSignal || exited) return;
  if (forwardEventHandlers('unhandledRejection', reason)) return;
  // Node exits non-zero when a rejection has no listener. Without this the broker
  // would answer `code: 0` and the failure would vanish, so fail the request loudly.
  errors.push(formatError(reason) + '\n');
  exitCode = 1;
  exited = true;
  scheduleDrainCheck();
});

realProcess.on('uncaughtException', error => {
  if (error instanceof ExitSignal || exited) return;
  if (forwardEventHandlers('uncaughtException', error)) return;
  errors.push(formatError(error) + '\n');
  exitCode = 1;
  exited = true;
  scheduleDrainCheck();
});

let drainResolve = null;
let drainCheckScheduled = false;

function scheduleDrainCheck() {
  if (!drainResolve || drainCheckScheduled) return;
  drainCheckScheduled = true;
  realSetImmediate(() => {
    // One extra turn: an unhandled rejection raised in this check phase is reported
    // after the callback returns, so the decision has to wait for it.
    realSetImmediate(() => {
      drainCheckScheduled = false;
      if (drainResolve && (exited || timers.size === 0)) {
        const resolve = drainResolve;
        drainResolve = null;
        resolve();
      }
    });
  });
}

function waitForEventLoopDrain() {
  // Always wait at least one event-loop turn, even with no pending timers, so a
  // rejection settled in a microtask is delivered as unhandled before we answer.
  return new Promise(resolve => {
    drainResolve = resolve;
    scheduleDrainCheck();
  });
}

function runCallback(callback, args) {
  if (exited) return;
  try {
    callback(...args);
  } catch (error) {
    if (!(error instanceof ExitSignal)) throw error;
  } finally {
    scheduleDrainCheck();
  }
}

const virtualFiles = new Map(Object.entries(request.files || {}));
const sources = new Map(Object.entries(request.sources || {}));
const realReadFileSync = fs.readFileSync;
const filename = String(request.filename || 'engraphis-test-request.js');

function runScript(code) {
  return new vm.Script(code, { filename }).runInThisContext();
}

function readSource(target, options) {
  const key = String(target);
  const encoding = typeof options === 'string'
    ? options
    : (options && options.encoding) || null;
  let buffer;
  if (virtualFiles.has(key)) {
    buffer = Buffer.from(String(virtualFiles.get(key)), 'utf8');
  } else if (sources.has(key)) {
    const value = sources.get(key);
    buffer = Buffer.isBuffer(value) ? value : Buffer.from(value);
  } else {
    return realReadFileSync(target, options);
  }
  if (encoding && encoding !== 'buffer') {
    return buffer.toString(encoding === 'utf-8' ? 'utf8' : encoding);
  }
  return buffer;
}

fs.readFileSync = readSource;

globalThis.console = captureConsole;
globalThis.process = proc;
const customRequire = id => (id === 'process' || id === 'node:process' ? proc : require(id));
Object.assign(customRequire, require);
globalThis.require = customRequire;
globalThis.setTimeout = (callback, delay, ...args) => {
  const id = realSetTimeout(() => {
    timers.delete(id);
    runCallback(callback, args);
  }, delay);
  timers.set(id, () => realClearTimeout(id));
  return id;
};
globalThis.clearTimeout = id => {
  timers.delete(id);
  realClearTimeout(id);
  scheduleDrainCheck();
};
globalThis.setInterval = (callback, delay, ...args) => {
  const id = realSetInterval(() => runCallback(callback, args), delay);
  timers.set(id, () => realClearInterval(id));
  return id;
};
globalThis.clearInterval = id => {
  timers.delete(id);
  realClearInterval(id);
  scheduleDrainCheck();
};
globalThis.setImmediate = (callback, ...args) => {
  const id = realSetImmediate(() => {
    timers.delete(id);
    runCallback(callback, args);
  });
  timers.set(id, () => realClearImmediate(id));
  return id;
};
globalThis.clearImmediate = id => {
  timers.delete(id);
  realClearImmediate(id);
  scheduleDrainCheck();
};

async function main() {
  let failure = null;
  try {
    const code = String(request.prelude || '') + String(request.script || '');
    const result = runScript(code);
    if (result && typeof result.then === 'function') await result;
    await waitForEventLoopDrain();
  } catch (error) {
    if (!(error instanceof ExitSignal)) {
      failure = error;
      // A top-level throw (or a rejected top-level script promise) is an uncaught
      // exception: run the registered handlers, then make sure the request cannot
      // report success. Only a handler that explicitly exits may pick the code.
      if (!forwardEventHandlers('uncaughtException', error)) {
        errors.push(formatError(error) + '\n');
        exitCode = exitCode || 1;
      } else if (!exited && exitCode === 0) {
        if (!errors.length) errors.push(formatError(error) + '\n');
        exitCode = 1;
      }
    }
  } finally {
    for (const cancel of timers.values()) {
      try {
        cancel();
      } catch (ignored) {
        /* best-effort teardown */
      }
    }
    timers.clear();
  }
  const failed = exitCode !== 0 || (failure !== null && !exited);
  parentPort.postMessage({
    id: request.id,
    error: failed && failure
      ? formatError(failure)
      : (failed ? 'node request exited with code ' + exitCode : undefined),
    stdout: outputs.join(''),
    stderr: errors.join(''),
    code: exitCode,
  });
}

main().catch(error => {
  if (!(error instanceof ExitSignal)) {
    // The async shell itself failed; treat it like an unhandled rejection of the
    // request so the broker answers with a failure instead of dropping the line.
    if (!forwardEventHandlers('unhandledRejection', error)) {
      errors.push(formatError(error) + '\n');
      exitCode = exitCode || 1;
    } else if (!exited && exitCode === 0) {
      if (!errors.length) errors.push(formatError(error) + '\n');
      exitCode = 1;
    }
  }
  parentPort.postMessage({
    id: request.id,
    error: formatError(error),
    stdout: outputs.join(''),
    stderr: errors.join(''),
    code: exitCode,
  });
});
