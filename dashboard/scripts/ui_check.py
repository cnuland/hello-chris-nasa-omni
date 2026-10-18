# Playwright layout + axe check of the dashboard (UI review round 2) against `vite preview` with mocked /api and /agent
# routes (page.route), at 1024 and 1440 px. Prints measurements as JSON, then the axe violations per page.
#   npm run build && npx vite preview --port 4179 --strictPort &
#   python3 -m venv /tmp/pw2 && /tmp/pw2/bin/pip install playwright && /tmp/pw2/bin/playwright install chromium
#   npm i --prefix /tmp/axe axe-core
#   UI_CHECK_BASE=http://localhost:4179 AXE_JS=/tmp/axe/node_modules/axe-core/axe.min.js /tmp/pw2/bin/python -I scripts/ui_check.py
import json, os, re, struct, sys, zlib
from playwright.sync_api import sync_playwright

BASE = os.environ.get('UI_CHECK_BASE', 'http://localhost:4179')
OUT = os.environ.get('UI_CHECK_SHOTS', '/tmp/ui2/shots')
os.makedirs(OUT, exist_ok=True)
AXE = open(os.environ.get('AXE_JS', '/tmp/axe/node_modules/axe-core/axe.min.js')).read()

def png(w=640, h=360):
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            g = 40 + (x * 60 // w) + (y * 40 // h)
            if 0.40 * w < x < 0.60 * w and 0.40 * h < y < 0.60 * h:
                row += bytes([200, 120, 30])
            else:
                row += bytes([g, g + 10, g + 25])
        rows.append(bytes(row))
    raw = b''.join(rows)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

IMG = png()
U = lambda n: f'{n:08x}-0000-4000-8000-{n:012x}'
LOCS = [
    {'location_id': 'USOS', 'parent_id': None, 'kind': 'segment', 'display_name': 'US Orbital Segment'},
    {'location_id': 'NOD1', 'parent_id': 'USOS', 'kind': 'module', 'display_name': 'Node 1 Unity'},
    {'location_id': 'NOD3', 'parent_id': 'USOS', 'kind': 'module', 'display_name': 'Node 3 Tranquility'},
    {'location_id': 'ZARYA', 'parent_id': None, 'kind': 'module', 'display_name': 'Zarya FGB'},
]
CATALOG = [{'catalog_id': 'camera-dslr', 'class_name': 'camera', 'display_name': 'Nikon Z9 camera', 'category': 'object'},
           {'catalog_id': 'ctb-half', 'class_name': 'cargo transfer bag', 'display_name': 'Cargo Transfer Bag (half)', 'category': 'container'}]
def item(n, label, **kw):
    d = {'entity_id': U(n), 'catalog_id': 'camera-dslr', 'catalog_name': 'Nikon Z9 camera', 'kind': 'object', 'status': 'provisional',
         'label': label, 'description': 'Black, handheld', 'attributes': {}, 'last_location': 'USOS', 'last_location_name': 'US Orbital Segment',
         'last_seen_at': '2026-10-17T22:57:00Z', 'confidence': 0.5, 'snapshot_url': f'/media/snap/{n}.png', 'needs_review': False}
    d.update(kw)
    return d
ITEMS = [item(i, f'camera {i}') for i in range(1, 13)]
ITEMS[0]['last_handled'] = {'event_type': 'moved', 'actor_desc': 'ui-review-2', 'actor_kind': 'operator', 'occurred_at': '2026-10-18T00:06:31Z'}
ITEMS[1]['last_handled'] = {'event_type': 'used', 'actor_desc': 'Blue shirt, black shorts, headset', 'occurred_at': '2026-10-17T22:50:05Z'}
REASONS = [['same module', 'similar description'], ['same class'], ['seen 2 s apart', 'same module'], []]
CANDS = [dict(item(100 + i, 'camera', last_location=['USOS', 'ZARYA', 'NOD1', 'NOD3'][i % 4]), thumbnail_url=f'/media/thumb/{i}.png',
              sighting_count=i + 1, score=round(0.95 - i * 0.08, 2), match_reasons=REASONS[i % 4],
              **({'snapshot_bbox': [0.4, 0.4, 0.6, 0.6], 'snapshot_bbox_source': 'detector'} if i == 0 else {})) for i in range(8)]
SUBJECT = item(200, 'camera', last_location='ZARYA', last_location_name='Zarya FGB', thumbnail_url='/media/thumb/s.png', sighting_count=1)
REVIEWS = [
    {'review_id': U(300), 'kind': 'possible_duplicate', 'entity_id': U(200), 'subject': SUBJECT, 'candidates': CANDS, 'candidates_source': 'suggested',
     'image_url': '/media/rv/1.png', 'image_bbox': [0.42, 0.42, 0.58, 0.58], 'image_bbox_source': 'omni', 'description': "camera: Black, handheld, with a lens.",
     'observed_at': '2026-10-17T23:01:00Z', 'created_at': '2026-10-17T23:01:00Z', 'location_id': 'ZARYA', 'model_guess': 'camera', 'status': 'open'},
    {'review_id': U(301), 'kind': 'possible_duplicate', 'entity_id': U(201), 'subject': dict(SUBJECT, entity_id=U(201)), 'candidates': CANDS[:3], 'candidates_source': 'stored',
     'image_url': '/media/rv/2.png', 'image_bbox': None, 'description': 'second duplicate', 'observed_at': '2026-10-17T23:00:00Z', 'created_at': '2026-10-17T23:00:00Z',
     'location_id': 'NOD1', 'model_guess': 'camera', 'status': 'open'},
    {'review_id': U(302), 'kind': 'unknown_object', 'entity_id': U(202), 'image_url': '/media/rv/3.png', 'image_bbox': [0.4, 0.4, 0.6, 0.6], 'image_bbox_source': 'detector',
     'description': 'tool: a black tool with a long handle', 'observed_at': '2026-10-17T22:59:00Z', 'created_at': '2026-10-17T22:59:00Z', 'location_id': 'ZARYA',
     'model_guess': 'tool', 'status': 'open', 'candidates': []},
]
STATS = {'by_status': {'confirmed': 3}, 'by_kind': {'object': 120, 'person': 4}, 'by_module': {}, 'open_reviews_by_kind': {'possible_duplicate': 2, 'unknown_object': 1}}
EVENTS = [
    {'event_id': U(400), 'event_type': 'added', 'entity_id': U(1), 'entity_label': 'cargo transfer bag', 'actor_desc': None, 'from_location': None, 'to_location': 'NOD3', 'to_location_name': 'Node 3 Tranquility', 'occurred_at': '2026-10-18T00:01:00Z', 'source': 'operator'},
    {'event_id': U(401), 'event_type': 'used', 'entity_id': U(2), 'entity_label': 'camera', 'actor_desc': 'Blue shirt, headset', 'from_location': 'USOS', 'from_location_name': 'US Orbital Segment', 'to_location': None, 'occurred_at': '2026-10-17T22:53:00Z', 'source': 'pipeline', 'location_role': 'at', 'actor_kind': 'person'},
    {'event_id': U(402), 'event_type': 'moved', 'entity_id': U(3), 'entity_label': 'laptop', 'actor_desc': 'ui-review-2', 'from_location': 'NOD3', 'from_location_name': 'Node 3 Tranquility', 'to_location': 'NOD1', 'to_location_name': 'Node 1 Unity', 'occurred_at': '2026-10-18T00:06:31Z', 'source': 'operator'},
]
MERGED = dict(item(200, 'camera', status='merged', merged_into={'entity_id': U(100), 'label': 'camera'}, merged_at='2026-10-18T00:07:10Z',
                   snapshot_bbox=[0.4, 0.4, 0.6, 0.6], snapshot_bbox_source='omni'), events=[EVENTS[1] | {'entity_id': U(200)}], sightings=[])
TARGET = dict(item(100, 'camera', merged_from=[{'entity_id': U(200), 'label': 'camera', 'merged_at': '2026-10-18T00:07:10Z'}]), events=[EVENTS[2]], sightings=[])
HISTORY = [
    {'at': '2026-10-18T00:06:31Z', 'kind': 'event', 'id': U(402), 'event_type': 'moved', 'source': 'operator', 'location_id': 'NOD1', 'summary': 'moved NOD3 -> NOD1 by ui-review-2', 'image_url': None},
    {'at': '2026-10-17T22:53:00Z', 'kind': 'event', 'id': 'x', 'event_type': 'used', 'source': 'pipeline', 'location_id': 'USOS', 'summary': 'used USOS -> ? by Blue shirt, headset', 'image_url': None},
    {'at': '2026-10-17T22:40:00Z', 'kind': 'audit', 'summary': '9 pipeline updates (sighting refresh: last_seen_at/confidence)', 'count': 9, 'first_at': '2026-10-17T22:00:00Z', 'image_url': None},
]

def handler(route):
    url = route.request.url
    path = re.sub(r'^https?://[^/]+', '', url).split('?')[0]
    m = route.request.method
    def j(body, status=200, headers=None):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body), headers=headers or {})
    if path.startswith('/api/media/'):
        return route.fulfill(status=200, content_type='image/png', body=IMG)
    if path == '/api/items' and m == 'GET':
        return j({'items': ITEMS, 'total': 124})
    if path == f'/api/items/{U(200)}':
        return j(MERGED)
    if path == f'/api/items/{U(100)}':
        return j(TARGET)
    if path.endswith('/history'):
        return j(HISTORY)
    if path.endswith('/unmerge'):
        return j({'detail': 'target has new sightings since the merge'}, 409)
    if path.startswith('/api/items/'):
        return j(dict(ITEMS[0], events=EVENTS, sightings=[]))
    if path == '/api/review':
        q = dict(x.split('=') for x in url.split('?', 1)[1].split('&')) if '?' in url else {}
        items = [r for r in REVIEWS if not q.get('kind') or r['kind'] == q['kind']]
        return j(items, headers={'X-Total-Count': str(len(items))})
    if re.match(r'^/api/review/.*/resolve$', path):
        return j({'status': 'resolved'})
    if path == '/api/catalog': return j(CATALOG)
    if path == '/api/locations': return j(LOCS)
    if path == '/api/events': return j(EVENTS)
    if path == '/api/stats': return j(STATS)
    if path.startswith('/agent/'):
        return j({'reply': '1. one\n   - sub\n2. two', 'citations': []})
    return j({'detail': 'not mocked'}, 404)

def axe(page, label, results):
    page.add_script_tag(content=AXE)
    r = page.evaluate("async () => { const r = await axe.run(document); return r.violations.map(v => ({id: v.id, impact: v.impact, n: v.nodes.length, nodes: v.nodes.slice(0,3).map(n => n.target.join(' '))})); }")
    results[label] = r

def rect(page, sel):
    return page.eval_on_selector(sel, 'e => { const r = e.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height, b: r.bottom}; }')

def main():
    report = {}
    axe_results = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for width in (1024, 1440):
            ctx = b.new_context(viewport={'width': width, 'height': 1000})
            ctx.add_init_script("localStorage.setItem('nasa-omni.operator', 'ui-review-2')")
            page = ctx.new_page()
            page.route(re.compile(r'.*/(api|agent)/.*'), handler)
            errors = []
            page.on('console', lambda msg: msg.type == 'error' and errors.append(msg.text))
            # ---------------- inventory filters
            page.goto(f'{BASE}/')
            page.wait_for_selector('[data-testid^="item-"]')
            r = {k: rect(page, s) for k, s in {
                'search': '#f-q', 'kind': '#f-kind', 'loc': '#f-loc', 'conf': '#f-conf', 'after': '#f-after', 'before': '#f-before',
                'quick': '.quick-ranges button', 'sort': '#f-sort', 'reset': '.reset-field button', 'status': '.status-group',
                'needs': '.needs-review-check'}.items()}
            sort_text = page.eval_on_selector('#f-sort', """e => { const c = document.createElement('canvas').getContext('2d');
                const cs = getComputedStyle(e); c.font = cs.font; const t = e.options[e.selectedIndex].text;
                return {text: t, textW: c.measureText(t).width, inner: e.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)}; }""")
            rows = {}
            for k in ('search', 'kind', 'loc', 'after', 'before', 'sort', 'reset', 'quick'):
                rows.setdefault(round(r[k]['y']), []).append(k)
            report[f'{width} filters'] = {'rows (input top y -> fields)': rows, 'sort': sort_text, 'sort width': r['sort']['w'],
                                          'needs review inside status row': r['status']['y'] <= r['needs']['y'] <= r['status']['b'],
                                          'form overflow': page.eval_on_selector('form.filters', 'e => e.scrollWidth - e.clientWidth')}
            page.screenshot(path=f'{OUT}/01-inventory-{width}.png')
            axe(page, f'/ @{width}', axe_results)
            # ---------------- review
            page.goto(f'{BASE}/review')
            card = f'[data-testid="review-{U(300)}"]'
            page.wait_for_selector(card)
            head = rect(page, f'{card} header')
            keep = rect(page, f'{card} .review-footer .actions button:nth-child(3)')
            report[f'{width} review'] = {'flow distance header->actions (px)': page.evaluate(f"""() => {{
                const c = document.querySelector('{card}'); const f = c.querySelector('.review-footer');
                return f.offsetTop - c.querySelector('header').offsetTop; }}"""),
                'keep-both visible at load (bottom <= 1000)': keep['b'] <= 1000, 'keep-both bottom': keep['b'], 'header top': head['y'],
                'candidate tiles shown': page.locator(f'{card} ul.candidates > li').count(),
                'page overflow-x': page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')}
            page.screenshot(path=f'{OUT}/02-review-{width}.png')
            axe(page, f'/review @{width}', axe_results)
            # scroll the card so its header sits under the topbar: the sticky footer must still be in the viewport
            page.evaluate(f"document.querySelector('{card}').scrollIntoView({{block: 'start'}})")
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(200)
            keep2 = rect(page, f'{card} .review-footer .actions button:nth-child(3)')
            report[f'{width} review']['keep-both in viewport after scrolling 300px into card'] = 0 <= keep2['y'] and keep2['b'] <= 1000
            page.screenshot(path=f'{OUT}/03-review-scrolled-{width}.png')
            # merge confirm + toast
            page.locator(f'{card} ul.candidates > li').first.get_by_role('button', name=re.compile('Merge subject into this')).click()
            page.wait_for_selector(f'{card} .confirm-merge'); page.wait_for_function("[...document.querySelectorAll('.confirm-merge img')].every(i => i.complete && i.naturalWidth > 0)")
            cm = rect(page, f'{card} .confirm-merge')
            report[f'{width} review']['confirm visible'] = cm['y'] >= 0 and cm['y'] < 1000
            page.screenshot(path=f'{OUT}/04-merge-confirm-{width}.png')
            axe(page, f'/review merge-confirm @{width}', axe_results)
            page.get_by_role('button', name='Confirm merge').click()
            page.wait_for_selector('.toast-actions')
            report[f'{width} review']['toast'] = page.inner_text('.toasts')
            page.screenshot(path=f'{OUT}/05-merge-toast-{width}.png')
            page.get_by_role('button', name=re.compile('Undo')).click()
            page.wait_for_selector('.toast-error')
            report[f'{width} review']['undo 409 toast'] = page.inner_text('.toast-error')
            # filtered count
            page.goto(f'{BASE}/review?kind=unknown_object')
            page.wait_for_selector('.review-count')
            report[f'{width} review']['filtered count'] = page.inner_text('.review-count')
            axe(page, f'/review filtered @{width}', axe_results)
            # ---------------- detail (merged source, target)
            page.goto(f'{BASE}/items/{U(200)}')
            page.wait_for_selector('.merged-banner')
            report[f'{width} detail'] = {'banner': page.inner_text('.merged-banner .merged-text'), 'hero caption': page.inner_text('.detail-media .frame-caption'),
                                         'history': page.inner_text('ol.timeline'), 'activity': page.inner_text('ul.moves')}
            page.screenshot(path=f'{OUT}/06-detail-merged-{width}.png', full_page=True)
            axe(page, f'/items merged @{width}', axe_results)
            page.get_by_role('button', name='Unmerge').click()
            page.wait_for_selector('.merged-banner [role="alert"]')
            report[f'{width} detail']['unmerge 409'] = page.inner_text('.merged-banner [role="alert"]')
            page.goto(f'{BASE}/items/{U(100)}')
            page.wait_for_selector('.merged-from')
            report[f'{width} detail']['merged from'] = page.inner_text('.merged-from')
            axe(page, f'/items target @{width}', axe_results)
            # ---------------- overview
            page.goto(f'{BASE}/overview')
            page.wait_for_selector('ul.events li')
            report[f'{width} overview'] = page.inner_text('ul.events')
            page.screenshot(path=f'{OUT}/07-overview-{width}.png')
            axe(page, f'/overview @{width}', axe_results)
            report[f'{width} console errors'] = errors
            ctx.close()
        b.close()
    print(json.dumps(report, indent=1))
    print('AXE', json.dumps({k: v for k, v in axe_results.items()}, indent=1))

main()
