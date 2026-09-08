/* Own renderer lifetime without changing its layout, camera, or physics settings. */
(function () {
  'use strict';

  function create({ isVisible, onStatus = () => {}, onError = () => {} }) {
    const entries = new Map();
    const disposed = new WeakSet();
    let active = null;
    let destroyed = false;

    function dispose(resource) {
      if (!resource || disposed.has(resource)) return;
      disposed.add(resource);
      if (typeof resource.destroy === 'function') {
        try { resource.destroy(); } catch (error) { onError(error); }
      }
    }

    function syncEntry(entry) {
      const visible = !document.hidden && isVisible();
      const { engine, overlay } = entry;
      let capability = 'unsupported';
      try {
        if (typeof engine.pause === 'function' && typeof engine.resume === 'function') {
          capability = 'drawing';
          if (entry.visible !== visible) engine[visible ? 'resume' : 'pause']();
        } else if (typeof engine.freeze === 'function' && typeof engine.state === 'function') {
          // Compatibility renderers may expose only physics freeze. Never label that as a
          // complete drawing pause, or replace the person's existing freeze preference.
          const state = engine.state();
          const frozen = state && (typeof state.frozen === 'boolean'
            ? state.frozen : state.settings && state.settings.frozen);
          if (typeof frozen === 'boolean') {
            capability = 'physics';
            if (!visible && entry.visible !== false) entry.wasFrozen = frozen;
            if (!visible) engine.freeze(true);
            else if (entry.visible === false) engine.freeze(entry.wasFrozen);
          }
        }
        if (overlay && typeof overlay.setEnabled === 'function') {
          overlay.setEnabled(visible && entry.overlayEnabled());
        }
        entry.visible = visible;
      } catch (error) {
        capability = 'unsupported';
        onError(error);
      }
      return { visible, capability };
    }

    function sync() {
      if (destroyed) return;
      let status = { visible: false, capability: 'unloaded' };
      entries.forEach(entry => {
        const result = syncEntry(entry);
        if (entry.engine === active) status = result;
      });
      onStatus(status);
    }

    function track(engine, overlay, overlayEnabled = () => false) {
      if (destroyed || disposed.has(engine)) return;
      const entry = entries.get(engine) || { engine, visible: null };
      entry.overlay = overlay;
      entry.overlayEnabled = overlayEnabled;
      entries.set(engine, entry);
      syncEntry(entry);
    }

    function release(engine, overlay) {
      const entry = entries.get(engine);
      entries.delete(engine);
      if (active === engine) active = null;
      dispose(entry ? entry.overlay : overlay);
      dispose(engine);
    }

    function replace(engine, overlay, overlayEnabled) {
      const previous = active;
      track(engine, overlay, overlayEnabled);
      active = engine;
      if (previous && previous !== engine) release(previous);
      sync();
    }

    function clear() {
      Array.from(entries.keys()).forEach(engine => release(engine));
      active = null;
      if (!destroyed) sync();
    }

    document.addEventListener('visibilitychange', sync);
    return {
      track, replace, release, sync, clear,
      destroy() {
        if (destroyed) return;
        destroyed = true;
        document.removeEventListener('visibilitychange', sync);
        clear();
      },
    };
  }

  window.EngraphisGraphLifecycle = { create };
}());
