import asyncio, os
from playwright.async_api import async_playwright
C="https://console-openshift-console.apps.bgrr25lx.eastus.aroapp.io"
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(); ctx=await b.new_context(viewport={"width":1920,"height":1080}, ignore_https_errors=True)
        pg=await ctx.new_page(); await pg.goto(C); await pg.wait_for_timeout(3000)
        if await pg.get_by_text("kube:admin").count(): await pg.get_by_text("kube:admin").first.click(); await pg.wait_for_timeout(2000)
        await pg.fill("#inputUsername","kubeadmin"); await pg.fill("#inputPassword", os.environ["KUBEADMIN_PASSWORD"])
        await pg.click("button[type=submit]"); await pg.wait_for_timeout(8000)
        await ctx.storage_state(path="/tmp/nasa-video/console-state.json")
        await pg.goto(C+"/k8s/ns/nasa-inference/pods"); await pg.wait_for_timeout(6000)
        await pg.screenshot(path="/tmp/nasa-video/explore/console-pods.png")
        await pg.goto(C+"/topology/ns/nasa-app?view=graph"); await pg.wait_for_timeout(7000)
        await pg.screenshot(path="/tmp/nasa-video/explore/console-topo.png")
        await b.close()
asyncio.run(main())
