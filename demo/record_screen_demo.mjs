import { spawn, spawnSync } from "node:child_process";
import { createReadStream, mkdirSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const demoDir = resolve(fileURLToPath(new URL(".", import.meta.url)));
const repoRoot = resolve(demoDir, "..");
const generatedDir = join(demoDir, "generated");
const outputDir = join(demoDir, "output");
const payload = join(generatedDir, "screen_demo_payload.json");
const webm = join(outputDir, "engraphis-memory-demo.webm");
const mp4 = join(outputDir, "engraphis-memory-demo.mp4");
const port = 8790;
const demoAssets = new Map([
  ["/", join(demoDir, "engraphis_screen_demo.html")],
  ["/engraphis_screen_demo.html", join(demoDir, "engraphis_screen_demo.html")],
  ["/generated/screen_demo_payload.json", payload],
]);

mkdirSync(generatedDir, { recursive: true });
mkdirSync(outputDir, { recursive: true });
const prepared = spawnSync(process.env.PYTHON || "python", [
  "-m", "demo.prepare_screen_demo", "--output", payload,
], { cwd: repoRoot, stdio: "inherit" });
if (prepared.status !== 0) process.exit(prepared.status || 1);

const server = createServer((request, response) => {
  let pathname;
  try {
    pathname = new URL(request.url || "/", "http://127.0.0.1").pathname;
  } catch {
    response.writeHead(400); response.end("Bad request"); return;
  }
  // This recorder only needs these generated demo assets.  Map request paths to fixed
  // files instead of deriving a filesystem path from a URL.
  const file = demoAssets.get(pathname);
  if (!file) { response.writeHead(404); response.end("Not found"); return; }
  try {
    const stat = statSync(file);
    response.writeHead(200, { "Content-Length": stat.size, "Content-Type": file.endsWith(".json") ? "application/json" : "text/html" });
    createReadStream(file).pipe(response);
  } catch {
    response.writeHead(404); response.end("Not found");
  }
});
await new Promise((resolveServer) => server.listen(port, "127.0.0.1", resolveServer));

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  recordVideo: { dir: outputDir, size: { width: 1920, height: 1080 } },
  deviceScaleFactor: 1,
});
const page = await context.newPage();
await page.goto(`http://127.0.0.1:${port}/engraphis_screen_demo.html?autoplay=1`, { waitUntil: "networkidle" });
// Keep the capture clock independent from requestAnimationFrame throttling in
// headless environments; the page itself still stops its progress bar at 56s.
await page.waitForTimeout(56_500);
await context.close();
await browser.close();
server.close();

const recorded = await page.video().path();
const ffmpeg = process.env.FFMPEG || "ffmpeg";
const encoded = spawnSync(ffmpeg, [
  "-y", "-i", recorded,
  "-c:v", "libx264", "-preset", "medium", "-crf", "20",
  "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4,
], { stdio: "inherit" });
if (encoded.status !== 0) process.exit(encoded.status || 1);
if (recorded !== webm) {
  try { rmSync(recorded, { force: true }); } catch { /* the MP4 is the deliverable */ }
}
console.log(`Wrote ${mp4}`);
