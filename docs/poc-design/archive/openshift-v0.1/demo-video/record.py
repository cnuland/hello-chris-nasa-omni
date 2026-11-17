"""Record the NASA omni inventory demo as separate segments (headless Chromium, isolated profile).

Each segment is its own browser context with video recording; segments.json records where the usable part of each
recording starts (after page load) so the assembler can trim the blank lead-in.
"""
import asyncio, json, os, subprocess, sys, time, html
from pathlib import Path
from playwright.async_api import async_playwright

DASH = "https://dashboard-nasa-app.apps.bgrr25lx.eastus.aroapp.io"
CONSOLE = "https://console-openshift-console.apps.bgrr25lx.eastus.aroapp.io"
OUT = Path("/tmp/nasa-video/rec"); OUT.mkdir(parents=True, exist_ok=True)
W, H = 1920, 1080
KUBE = os.path.expanduser("~/.config/aro-nasa/kubeconfig")
ONLY = set(sys.argv[1:])

OVERLAY_JS = r"""
(() => {
  const install = () => {
    if (document.getElementById('__cap')) return;
    const st = document.createElement('style');
    st.textContent = `
      #__cap{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);max-width:1500px;z-index:2147483647;
        background:rgba(8,14,28,.88);color:#fff;font:600 27px/1.35 "Helvetica Neue",Arial,sans-serif;padding:14px 26px;
        border-radius:10px;border-left:6px solid #ee0000;box-shadow:0 6px 24px rgba(0,0,0,.45);opacity:0;transition:opacity .35s;
        pointer-events:none;text-align:left}
      #__cap small{display:block;font-weight:400;font-size:21px;color:#c9d3e3;margin-top:4px}
      #__cur{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483646;pointer-events:none;
        transition:transform .55s cubic-bezier(.4,.1,.2,1);transform:translate(960px,540px)}
      #__cur svg{filter:drop-shadow(0 2px 3px rgba(0,0,0,.6))}
      .__ripple{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;border:3px solid #ffcc00;
        z-index:2147483645;pointer-events:none;animation:__rp .5s ease-out forwards}
      @keyframes __rp{from{transform:scale(.3);opacity:1}to{transform:scale(1.6);opacity:0}}`;
    document.documentElement.appendChild(st);
    const c = document.createElement('div'); c.id='__cap'; document.documentElement.appendChild(c);
    const k = document.createElement('div'); k.id='__cur';
    k.innerHTML='<svg width="26" height="26" viewBox="0 0 24 24"><path d="M3 2l7 19 2.5-7.5L20 11z" fill="#fff" stroke="#000" stroke-width="1.4"/></svg>';
    document.documentElement.appendChild(k);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
  window.__capSet = (t, s) => { install(); const c=document.getElementById('__cap');
    if(!t){c.style.opacity=0;return;} c.innerHTML = t + (s?('<small>'+s+'</small>'):''); c.style.opacity=1; };
  window.__capTop = () => { install(); const c=document.getElementById('__cap'); c.style.bottom='auto'; c.style.top='64px'; };
  window.__curTo = (x,y) => { install(); document.getElementById('__cur').style.transform=`translate(${x}px,${y}px)`; };
  window.__ripple = (x,y) => { const r=document.createElement('div'); r.className='__ripple'; r.style.left=x+'px'; r.style.top=y+'px';
    document.documentElement.appendChild(r); setTimeout(()=>r.remove(),600); };
})();
"""

segments = {}


class Seg:
    def __init__(self, browser, name, storage=None):
        self.browser, self.name, self.storage = browser, name, storage

    async def __aenter__(self):
        d = OUT / self.name
        if d.exists():
            for f in d.iterdir(): f.unlink()
        kw = dict(viewport={"width": W, "height": H}, record_video_dir=str(d), record_video_size={"width": W, "height": H},
                  ignore_https_errors=True, color_scheme="dark", bypass_csp=True)
        if self.storage: kw["storage_state"] = self.storage
        self.ctx = await self.browser.new_context(**kw)
        await self.ctx.add_init_script(OVERLAY_JS)
        self.t0 = time.monotonic()
        self.page = await self.ctx.new_page()
        self.start = None
        return self

    def mark(self):  # usable footage starts here
        self.start = time.monotonic() - self.t0

    async def __aexit__(self, *a):
        end = time.monotonic() - self.t0
        await self.ctx.close()
        vids = list((OUT / self.name).glob("*.webm"))
        segments[self.name] = {"file": str(vids[0]), "start": self.start or 0, "end": end}
        print(f"[{self.name}] start={self.start:.1f}s end={end:.1f}s", flush=True)


async def cap(pg, text, sub=None):
    await pg.evaluate("([t,s]) => window.__capSet(t,s)", [text, sub])


async def wait(pg, s):
    await pg.wait_for_timeout(int(s * 1000))


async def point(pg, loc, click=True, pause=0.6):
    await loc.scroll_into_view_if_needed()
    box = await loc.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await pg.evaluate("([x,y]) => window.__curTo(x,y)", [x, y])
    await wait(pg, pause)
    if click:
        await pg.evaluate("([x,y]) => window.__ripple(x,y)", [x, y])
        await pg.mouse.click(x, y)


async def type_slow(pg, loc, text, delay=55):
    await point(pg, loc)
    await loc.press_sequentially(text, delay=delay)


# ----------------------------------------------------------------------------- title / closing cards
CARD = """<html><body style="margin:0;background:radial-gradient(circle at 30% 20%,#13284a,#060b16 70%);color:#fff;
font-family:'Helvetica Neue',Arial,sans-serif;height:1080px;display:flex;flex-direction:column;justify-content:center;padding:0 150px">
<div style="font-size:26px;letter-spacing:6px;color:#ee0000;font-weight:700">{kicker}</div>
<div style="font-size:76px;font-weight:700;margin:18px 0 10px">{title}</div>
<div style="font-size:34px;color:#c9d3e3;line-height:1.45">{sub}</div>
<div style="margin-top:46px;font-size:24px;color:#8fa1bd;line-height:1.6">{foot}</div></body></html>"""


async def card(browser, name, kicker, title, sub, foot, secs):
    async with Seg(browser, name) as s:
        await s.page.set_content(CARD.format(kicker=kicker, title=title, sub=sub, foot=foot))
        await s.page.evaluate("() => { const k=document.getElementById('__cur'); if(k) k.style.display='none'; }")
        await wait(s.page, 0.4); s.mark(); await wait(s.page, secs)


# ----------------------------------------------------------------------------- terminal
TERM = """<html><body style="margin:0;background:#0b0f17;height:1080px">
<div style="margin:40px 60px;border-radius:12px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.6);border:1px solid #273042">
<div style="background:#1b2230;padding:12px 18px;color:#9aa7bd;font:500 18px 'Helvetica Neue',Arial">
<span style="color:#ff5f56">●</span> <span style="color:#ffbd2e">●</span> <span style="color:#27c93f">●</span>
&nbsp;&nbsp; oc — Red Hat OpenShift on Azure (ARO) · NASA omni inventory POC</div>
<pre id="t" style="margin:0;padding:22px 26px;height:880px;overflow:hidden;color:#d7e0ee;
font:21px/1.42 Menlo,'SF Mono',monospace;white-space:pre-wrap"></pre></div></body></html>"""

TERM_JS = """
window.__p = () => { const s=document.createElement('span'); s.style.color='#5fd7a7'; s.textContent='demo@poc ~ $ ';
  document.getElementById('t').appendChild(s); };
window.__c = (ch) => { const t=document.getElementById('t'); t.appendChild(document.createTextNode(ch)); };
window.__o = (txt) => { const t=document.getElementById('t'); const s=document.createElement('span'); s.style.color='#aeb9cc';
  s.textContent = txt; t.appendChild(s); while (t.scrollHeight > t.clientHeight + 2 && t.childNodes.length) t.removeChild(t.firstChild); };
"""


def run(cmd):
    env = dict(os.environ, KUBECONFIG=KUBE)
    r = subprocess.run(["/bin/bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=90)
    return (r.stdout + r.stderr).rstrip("\n")


async def term_cmd(pg, shown, actual=None, after=2.2):
    await pg.evaluate("window.__p()")
    for ch in shown:
        await pg.evaluate("(c)=>window.__c(c)", ch); await pg.wait_for_timeout(28)
    await wait(pg, 0.35)
    out = run(actual or shown)
    await pg.evaluate("(o)=>window.__o(o)", "\n" + out + "\n\n")
    await wait(pg, after)


async def terminal(browser):
    async with Seg(browser, "s2_terminal") as s:
        pg = s.page
        await pg.set_content(TERM); await pg.evaluate(TERM_JS)
        await pg.evaluate("() => { const k=document.getElementById('__cur'); if(k) k.style.display='none'; }")
        await wait(pg, 0.3); s.mark()
        await cap(pg, "Live on the cluster with the oc CLI", "Red Hat OpenShift on Azure, NVIDIA T4 GPU nodes: the development stand-in for the Dell GB10")
        await wait(pg, 1.5)
        await term_cmd(pg, "oc get nodes -l nvidia.com/gpu.present=true -L node.kubernetes.io/instance-type", after=3)
        await cap(pg, "Model servers in nasa-inference",
                  "gemma4 = Gemma 4 12B omni perception · asr = Whisper · dgemma = DiffusionGemma decision model · agent-llm = chat")
        await term_cmd(pg, "oc get pods -n nasa-inference -o custom-columns=POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName", after=3.5)
        await term_cmd(pg, "oc exec -n nasa-inference deploy/gemma4 -- nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv",
                       after=3.5)
        await cap(pg, "Application and data tiers", "Perception, inventory API, agent + MCP server and dashboard; Postgres 16 + pgvector via CloudNativePG")
        await term_cmd(pg, "oc get deploy -n nasa-app", after=3)
        await term_cmd(pg, "oc get clusters.postgresql.cnpg.io -n nasa-data", after=2.5)
        await cap(pg, "What the pipeline has built from the ISS tour so far", "Observation windows, items by module and the open review queue, straight from the inventory API")
        await term_cmd(pg, f"curl -s {DASH}/api/stats | jq -c '{{windows, by_module, open_reviews_by_kind}}'", after=5)


# ----------------------------------------------------------------------------- console
async def console(browser):
    async with Seg(browser, "s3_console", storage="/tmp/nasa-video/console-state.json") as s:
        pg = s.page
        await pg.goto(CONSOLE + "/k8s/ns/nasa-inference/pods"); await pg.wait_for_selector("text=gemma4", timeout=60000)
        await wait(pg, 1.5); s.mark()
        await cap(pg, "The same stack in the OpenShift console", "Project nasa-inference: one pod per model server, each on a GPU node")
        await wait(pg, 4)
        await point(pg, pg.get_by_role("link", name="gemma4-").first, click=False); await wait(pg, 1.5)
        await pg.goto(CONSOLE + "/topology/ns/nasa-app?view=graph"); await pg.wait_for_selector("text=inventory-api", timeout=60000)
        await wait(pg, 1)
        await cap(pg, "Topology of nasa-app", "dashboard, inventory-api (the single writer), agent (chatbot + MCP server), perception (paused for the demo) and the agent-dreaming CronJob")
        await wait(pg, 3)
        await point(pg, pg.get_by_text("dashboard", exact=True).first); await wait(pg, 4)
        await pg.goto(CONSOLE + "/k8s/ns/nasa-data/pods"); await pg.wait_for_selector("text=inventory-db", timeout=60000)
        await cap(pg, "Project nasa-data", "Postgres 16 + pgvector (CloudNativePG, primary + replica) and RustFS S3 storage for evidence frames")
        await wait(pg, 5)


# ----------------------------------------------------------------------------- dashboard
async def dashboard(browser):
    async with Seg(browser, "s4_dashboard") as s:
        pg = s.page
        await pg.goto(DASH + "/"); await pg.wait_for_selector("text=items", timeout=60000); await wait(pg, 1.5); s.mark()
        await cap(pg, "Mission-control dashboard: the inventory", "Every item the pipeline has tracked from the ISS tour: status, confidence, last location and time (UTC)")
        await wait(pg, 4)
        await type_slow(pg, pg.get_by_placeholder("label, description…"), "laptop")
        await wait(pg, 1.5)
        await cap(pg, "Search and filter", "Search 'laptop', then filter by module")
        await point(pg, pg.get_by_label("Location").first)
        await pg.get_by_label("Location").first.select_option(label="Cupola (CUP1)"); await wait(pg, 2.5)
        await pg.get_by_label("Location").first.select_option(label="Anywhere"); await wait(pg, 1.5)
        # item detail
        await cap(pg, "Item detail: the evidence behind each record", "Frame with the detector box, location provenance, sighting history and audit trail")
        await point(pg, pg.get_by_role("link", name="Laptop").first); await wait(pg, 3.5)
        await pg.mouse.wheel(0, 500); await wait(pg, 3)
        await pg.mouse.wheel(0, -500); await wait(pg, 1)


async def review(browser):
    async with Seg(browser, "s5_review") as s:
        pg = s.page
        await pg.goto(DASH + "/review"); await pg.wait_for_selector("text=Review queue", timeout=60000); await wait(pg, 1.5); s.mark()
        await cap(pg, "Review queue: what the system is not sure about goes to a person", "Unknown objects, possible duplicates and low-confidence matches, each with image, description, place and time")
        await wait(pg, 3)
        await type_slow(pg, pg.get_by_label("Operator").first, "demo")
        await point(pg, pg.get_by_role("button", name="Unknown objects"))
        await point(pg, pg.get_by_label("Location").first)
        await pg.get_by_label("Location").first.select_option(label="Kibo JEM Pressurized Module (JPM1)"); await wait(pg, 2)
        card = pg.get_by_role("article").filter(has_text="orange sphere").first
        await card.scroll_into_view_if_needed()
        await cap(pg, "Define an unknown object", "The orange sphere from the Kibo footage is not in the catalog. The operator classifies it as 'Orange ball'")
        await wait(pg, 3.5)
        await point(pg, card.get_by_label("Catalog item"))
        await card.get_by_label("Catalog item").select_option(label="Orange ball"); await wait(pg, 1)
        await point(pg, card.get_by_role("button", name="Classify")); await wait(pg, 3)
        # possible duplicates: merge + undo
        await pg.get_by_label("Location").first.select_option(label="Anywhere")
        await point(pg, pg.get_by_role("button", name="Possible duplicates")); await wait(pg, 1.5)
        await cap(pg, "Possible duplicates: compare side by side", "Candidates are ranked by match score, with the reasons. Merging is audited and can be undone")
        await wait(pg, 3.5)
        dup = pg.get_by_role("article").first
        await point(pg, dup.get_by_role("button", name="Merge subject into this").first); await wait(pg, 1.2)
        confirm = pg.get_by_role("button", name="Confirm merge")
        if await confirm.count():
            await point(pg, confirm.first);
        await wait(pg, 2.5)
        undo = pg.get_by_role("button", name="Undo merge")
        if await undo.count():
            await cap(pg, "Changed your mind? Undo", "The merge is reversed from the notification; the audit log keeps both steps")
            await point(pg, undo.first); await wait(pg, 3)


async def additem(browser):
    async with Seg(browser, "s6_add") as s:
        pg = s.page
        await pg.goto(DASH + "/items/new"); await pg.wait_for_selector("text=Add item", timeout=60000); await wait(pg, 1); s.mark()
        await cap(pg, "Add an item no camera has seen yet", "For example a spare stowed before launch. A photo is required so NASA always sees what the record is")
        await wait(pg, 2.5)
        await point(pg, pg.get_by_label("Image"), click=False)
        await pg.get_by_label("Image").set_input_files("/tmp/nasa-video/frames/laptop.png"); await wait(pg, 1.5)
        await type_slow(pg, pg.get_by_label("Label"), "Spare SSC laptop")
        await point(pg, pg.get_by_label("Location")); await pg.get_by_label("Location").select_option(label="Kibo JEM Pressurized Module (JPM1)")
        await point(pg, pg.get_by_label("Catalog")); await pg.get_by_label("Catalog").select_option(label="SSC laptop")
        await type_slow(pg, pg.get_by_label("Description"), "Spare laptop stowed in Kibo, photo from ground records", delay=30)
        await type_slow(pg, pg.get_by_label("Operator"), "demo")
        await wait(pg, 0.8)
        await point(pg, pg.get_by_role("button", name="Add item")); await wait(pg, 3.5)
        await cap(pg, "Overview", "Counts by module, person tracks (appearance only, never identity) and recent events in plain English")
        await point(pg, pg.get_by_role("link", name="Overview")); await wait(pg, 4)
        await pg.mouse.wheel(0, 600); await wait(pg, 3)


async def ask(pg, q, wait_max=45):
    box = pg.get_by_label("Message")
    await type_slow(pg, box, q, delay=32)
    await point(pg, pg.get_by_role("button", name="Send"))
    await wait(pg, 1.2)
    for _ in range(int(wait_max * 2)):
        if not await pg.get_by_role("button", name="Stop").count() and await box.is_enabled():
            break
        await wait(pg, 0.5)
    await wait(pg, 1.0)
    await pg.get_by_role("log").evaluate("e => e.scrollTop = e.scrollHeight")


async def chat(browser):
    async with Seg(browser, "s7_chat") as s:
        pg = s.page
        await pg.goto(DASH + "/chat"); await pg.wait_for_selector("text=Ask the inventory", timeout=60000)
        await pg.evaluate("() => window.__capTop()"); await wait(pg, 1); s.mark()
        await cap(pg, "The mission-control chatbot", "A Gemma 4 agent that answers only from inventory records, cites them, and says what it checked")
        await type_slow(pg, pg.get_by_label("Operator"), "demo")
        await ask(pg, "Who used the drill?"); await wait(pg, 3)
        await cap(pg, "Catalog knowledge", "Home location and expected count come from the ISS catalog, not from the model")
        await ask(pg, "Where is the multimeter usually kept, and how many should there be?"); await wait(pg, 3)
        await cap(pg, "People, privacy-safe", "Exact counts from the database; person tracks are appearance-based and labelled approximate")
        await ask(pg, "How many people were seen on October 18, and in which modules?"); await wait(pg, 4)
        await cap(pg, "The orange ball we just classified", "The operator's decision is immediately part of the inventory the agent answers from")
        await ask(pg, "Where is the orange ball?"); await wait(pg, 3.5)
        await cap(pg, "Private areas stay private", "Crew quarters store no images, speech or descriptions, and the agent says so")
        await ask(pg, "What is in the crew quarters?"); await wait(pg, 3.5)
        await cap(pg, "Audit: what changed?", "The classification and the added item from this demo, in plain English")
        await ask(pg, "What changed in the last hour?"); await wait(pg, 6)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        steps = [
            ("s0_title", lambda: card(b, "s0_title", "RED HAT · NASA POC DEMO", "NASA Omni Inventory Tracking",
                                       "vLLM-Omni video + audio perception · on-device Postgres inventory ·<br>mission-control dashboard and chatbot",
                                       "Recorded live on Red Hat OpenShift (ARO, NVIDIA T4 GPUs), October 2026.<br>Input: NASA's public-domain tour video "
                                       "\"Step Inside the International Space Station\" (2022).", 7)),
            ("s2_terminal", lambda: terminal(b)),
            ("s3_console", lambda: console(b)),
            ("s4_dashboard", lambda: dashboard(b)),
            ("s5_review", lambda: review(b)),
            ("s6_add", lambda: additem(b)),
            ("s7_chat", lambda: chat(b)),
            ("s9_close", lambda: card(b, "s9_close", "WHAT YOU SAW", "Video and audio in, a trustworthy inventory out",
                                       "vLLM-Omni perception · closed-world tracking with a human in the loop ·<br>grounded mission-control chat · crew privacy by design",
                                       "Next: the same stack on a Dell GB10. POC starts November 2026, onsite 1 the week of November 17.", 8)),
        ]
        for name, fn in steps:
            if ONLY and name not in ONLY: continue
            await fn()
        await b.close()
    prev = json.loads(Path(OUT / "segments.json").read_text()) if (OUT / "segments.json").exists() else {}
    prev.update(segments)
    Path(OUT / "segments.json").write_text(json.dumps(prev, indent=1))


asyncio.run(main())
