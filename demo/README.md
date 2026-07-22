# Engraphis memory-continuity screen demo

This produces a silent 56-second MP4 showing the three proof points requested:

1. A new `demo-agent` session boots from the previous repository handoff and recalls the v2 architecture context.
2. A same-subject fact changes; the resolver invalidates the old row while preserving it in history.
3. Retrieval evidence sits next to the Timeline chain, showing the retrieval arm, fused score, retention, provenance, and current/past validity.

The payload is generated from a real in-memory `MemoryService` run before recording. No credentials, live services, or external APIs are used.

Install the repository's Node dependencies and Chromium once, and ensure `ffmpeg` is on `PATH`:

```powershell
npm ci
npx playwright install chromium
ffmpeg -version
```

From the repository root:

```powershell
node demo/record_screen_demo.mjs
```

The finished video is written to `demo/output/engraphis-memory-demo.mp4`. To inspect only the data contract:

```powershell
python demo/prepare_screen_demo.py
```
