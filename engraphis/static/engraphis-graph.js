/* Legacy v1 compatibility adapter.

   The force-graph renderer is a v2 dashboard capability and is served by
   ``/v2-assets/engraphis-graph.js``.  Keep this tiny marker for integrations that used the
   old static path; it deliberately does not install the renderer, so the legacy reference
   server continues to take its established classic-renderer fallback. */
(function () {
  window.EngraphisGraphCompat = { canonicalAsset: '/v2-assets/engraphis-graph.js' };
})();
