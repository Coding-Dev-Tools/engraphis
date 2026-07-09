"""Quick test for the new features."""
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8700", timeout=300)

# 1. Dashboard
r = c.get("/")
html = r.text
print("Dashboard:", len(html), "bytes")
print("Has graph-stats div:", "graph-stats" in html)
print("Has auto-categorize toggle:", "auto-categorize-folder" in html)
print("Has graph click handler:", "network.on('click'" in html)
print("Has openEntityMem:", "openEntityMem" in html)

# 2. Create test memories
for mt, title, content in [
    ("semantic", "Python Fact", "Python is dynamically typed and supports multiple paradigms."),
    ("episodic", "Meeting Notes", "Met with Alice on June 28 to discuss the Hermes project roadmap."),
    ("procedural", "Deploy Steps", "Step 1: Build. Step 2: Test. Step 3: Deploy."),
]:
    r = c.post("/memory/files/create", json={"title": title, "content": content, "namespace": "default", "memory_type": mt})
    print(f"Create {mt}:", r.status_code)

# 3. Graph snapshot with documents
r = c.get("/memory/admin/graph-snapshot?namespace=default&limit=50")
d = r.json()["data"]
print(f"Graph: {d['entity_count']} entities, {d['edge_count']} edges")
if d["entities"]:
    e = d["entities"][0]
    print(f"  First entity: {e['name']} - docs: {e.get('documents', [])} - preview: {e.get('preview_title', '')[:30]}")

# 4. Health
r = c.get("/memory/health/overview")
print("Health:", r.json()["data"])

# 5. Auto-categorize
print("Auto-categorizing (calling LLM)...")
r = c.post("/memory/auto-categorize", json={"namespace": "default"}, timeout=120)
d = r.json()["data"]
print(f"Auto-categorize: {d['categorized']} categorized, {d['errors']} errors")
if d["details"]:
    det = d["details"][0]
    print(f"  Example: {det['title'][:30]} -> {det['new_type']} (conf: {det['confidence']})")

# 6. Conflict check
r = c.post("/memory/conflict-check", json={
    "content": "Python is statically typed.",
    "namespace": "default",
    "title": "Python Type System",
}, timeout=120)
print("Conflict check:", r.json()["data"])

# 7. Smart import endpoint exists
print()
print("Checking new routes...")
from engraphis.app import app  # noqa: E402
routes = [r.path for r in app.routes if hasattr(r, "path")]
for name in ["/memory/vaults/upload-folder-smart", "/memory/auto-categorize", "/memory/conflict-check"]:
    print(f"  {name}: {'OK' if name in routes else 'MISSING'}")

print()
print("ALL CHECKS DONE")
