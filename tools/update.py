#!/usr/bin/env python3
"""Daily updater for the Tuyisenge home search page.

Pulls active single family homes from Redfin near Terramar Academy, keeps the
ones that fit the family's rule (4+ bedrooms, no private pool, under the price
cap, within the drive radius), assigns Deer Valley school zones from the
official DVUSD boundary KML files, computes the FHA 3-2-1 buydown payment, and
writes index.html plus changes.md (adds / drops / price changes since last run).

Run from the repo root:  python3 tools/update.py
Only needs Python 3 standard library and curl.
"""
import json, math, os, re, subprocess, sys, urllib.parse, datetime, html as H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

# ---------------- rule ----------------
MAX_PRICE = 500000
MIN_BEDS = 4
RADIUS_MI = 4.0            # about a 10 minute drive from Terramar
TERRAMAR = (33.7109, -112.2118)      # 7000 W Happy Valley Rd
MTN_RIDGE = (33.6909, -112.2032)     # 22800 N 67th Ave
ZIPS = {'85083': 41768, '85383': 36414, '85310': 36349, '85308': 36347,
        '85085': 36248, '85382': 36413, '85027': 36200, '85053': 36223}
# Redfin skPoolType: 0 = private pool, 2 = private + community, 1 = community only, 5 = none
POOL_CODES = (0, 2)
DANNI = "14802383665"

# ---------------- payment (same setup as the pre-approval) ----------------
MI_RATE = 207.67 * 12 / 471306
def pi(rate, amt):
    r = rate / 1200
    return amt * r / (1 - (1 + r) ** -360)
def calc(P):
    base = P * 0.965; uf = round(base * 0.0175); tot = base + uf
    esc = 115 + P * 0.0045 / 12 + tot * MI_RATE / 12
    return dict(down=P * 0.035, y1=pi(3.875, tot) + esc, conc=P * 0.045)

# ---------------- geo ----------------
def hav(a, b):
    R = 3958.8
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def load_kml(path):
    s = open(path, encoding="utf-8").read()
    polys = []
    for pm in re.findall(r"<Placemark>(.*?)</Placemark>", s, re.S):
        name = re.search(r"<name>(.*?)</name>", pm, re.S)
        name = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", name.group(1)).strip() if name else "?"
        for coords in re.findall(r"<coordinates>(.*?)</coordinates>", pm, re.S):
            pts = []
            for tok in coords.split():
                p = tok.split(",")
                if len(p) >= 2:
                    pts.append((float(p[1]), float(p[0])))  # lat, lon
            if len(pts) >= 3:
                polys.append((name, pts))
    return polys

def inside(pt, poly):
    lat, lon = pt; n = len(poly); c = False; j = n - 1
    for i in range(n):
        yi, xi = poly[i]; yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            c = not c
        j = i
    return c

def zone(pt, polys):
    for name, poly in polys:
        if inside(pt, poly):
            return name
    return None

# ---------------- redfin ----------------
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
def gis(params):
    url = "https://www.redfin.com/stingray/api/gis?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-sS", "-A", UA, "-H", "Accept: */*", "-H", "Referer: https://www.redfin.com/", url],
                         capture_output=True, text=True, timeout=60).stdout
    if not out.startswith("{}&&"):
        raise RuntimeError("Redfin did not answer normally for region " + str(params.get("region_id")))
    return json.loads(out[4:])["payload"]["homes"]

def pull():
    homes = {}
    for z, rid in ZIPS.items():
        for x in gis({'al': 1, 'market': 'phoenix', 'num_homes': 350, 'ord': 'redfin-recommended-asc', 'page_number': 1,
                      'region_id': rid, 'region_type': 2, 'sf': '1,2,3,5,6,7', 'status': 9, 'uipt': 1, 'v': 8}):
            try:
                if x.get('mlsStatus') != 'Active': continue
                if (x.get('beds') or 0) < MIN_BEDS: continue
                price = x['price']['value']
                if price > MAX_PRICE: continue
                if x.get('skPoolType') in POOL_CODES: continue
                if x.get('propertyType') != 6: continue   # single family only (13 = townhouse)
                ll = x['latLong']['value']; p = (ll['latitude'], ll['longitude'])
                dT = hav(p, TERRAMAR)
                if dT > RADIUS_MI: continue
                mls = str(x['mlsId']['value'])
                homes[str(x['propertyId'])] = dict(
                    addr=x['streetLine']['value'], city=x['city'], zip=x['zip'], price=price,
                    beds=x['beds'], baths=x.get('baths'), sqft=(x.get('sqFt') or {}).get('value'),
                    lat=p[0], lon=p[1], url='https://www.redfin.com' + x['url'], mls=mls,
                    pool=x.get('skPoolType'), year=(x.get('yearBuilt') or {}).get('value'),
                    dom=(x.get('dom') or {}).get('value'), dT=dT, dM=hav(p, MTN_RIDGE),
                    photo=f"https://ssl.cdn-redfin.com/photo/{x.get('dataSourceId', 86)}/islphoto/{mls[-3:]}/genIslnoResize.{mls}_0.webp")
            except KeyError:
                continue
    return homes

# ---------------- grouping ----------------
def classify(h, k8polys, hspolys):
    p = (h['lat'], h['lon'])
    k8 = zone(p, k8polys); hs = zone(p, hspolys)
    h['k8'] = k8; h['hs'] = hs
    inT = k8 == 'Terramar Academy of the Arts'; inM = hs == 'Mountain Ridge High School'
    if inT and inM: h['grp'] = 1
    elif inM: h['grp'] = 2
    else: h['grp'] = 3
    return h

def m(x): return f"${x:,.0f}"
def mins(mi): return max(3, round(mi * 2.6))   # rough neighborhood drive time

def card(h, notes, new_ids):
    a = h['addr']; city = h['city']; z = h['zip']
    pay = calc(h['price'])
    short_k8 = (h['k8'] or 'outside Deer Valley').replace(' Elementary', '').replace(' Academy of the Arts', '').replace(' STEAM Academy', '').replace(' School', '')
    if h['grp'] == 1:
        walkT = h['dT'] < 1.0
        banner = ('<div class="ban ok">🚌 Bus to both schools</div>' if not walkT else
                  '<div class="ban ok">🚶 Walk to Terramar · 🚌 Bus to Mountain Ridge</div>')
        dist = f'<div class="dist"><span class="big">{h["dT"]:.1f} mi</span> to Terramar · <span class="big">{h["dM"]:.1f} mi</span> to Mountain Ridge</div>'
    elif h['grp'] == 2:
        banner = f'<div class="ban drive">🚌 Bus to Mountain Ridge · 🚗 You drive to Terramar</div>'
        dist = f'<div class="dist"><span class="big">{h["dT"]:.1f} mi · about {mins(h["dT"])} min</span> drive to Terramar</div>'
    else:
        banner = f'<div class="ban far">🚗 You drive to both schools</div>'
        dist = (f'<div class="dist"><span class="big">{h["dT"]:.1f} mi · about {mins(h["dT"])} min</span> to Terramar<br>'
                f'<span class="big">{h["dM"]:.1f} mi · about {mins(h["dM"])} min</span> to Mountain Ridge</div>')
    note = notes.get(a)
    note_html = f'<div class="note-line">{H.escape(note)}</div>' if note else ''
    newtag = '<span class="new">New</span>' if h['id'] in new_ids else ''
    wa = urllib.parse.quote(f"Hi Danni, we would like to request a showing for {a}, {city}. When is a good time?")
    slug = re.sub(r'[^A-Za-z0-9]+', '-', f"{a} {city} AZ {z}").strip('-')
    gm = urllib.parse.quote(f"{a}, {city}, AZ {z}")
    baths = f"{h['baths']:g}" if h['baths'] else "?"
    sq = f"{h['sqft']:,}" if h['sqft'] else "?"
    return f'''<article class="card g{h['grp']}" data-id="{H.escape(h['id'])}" data-bd="{h['beds']}">
  {banner}
  <a class="photo" href="{h['url']}" target="_blank" rel="noopener"><img src="{h['photo']}" alt="{H.escape(a)}" loading="lazy"><span class="more">See all photos</span></a>
  <div class="body">
    <div class="row1"><div class="price">{m(h['price'])}</div>{newtag}</div>
    <div class="addr">{H.escape(a)}<span class="city">{H.escape(city)}, AZ {z}</span></div>
    <div class="stats"><span><b>{h['beds']}</b> bd</span><span><b>{baths}</b> ba</span><span><b>{sq}</b> sq ft</span></div>
    {dist}
    <div class="pay">Year 1 payment about <b>{m(pay['y1'])}</b> a month · K-8 zone: {H.escape(short_k8)}</div>
    {note_html}
    <div class="acts">
      <button class="btn fav" type="button" aria-pressed="false">♡ Save</button>
      <button class="btn hide" type="button">Hide</button>
      <a class="btn show" href="https://wa.me/{DANNI}?text={wa}" target="_blank" rel="noopener">Request showing</a>
    </div>
    <div class="links"><a href="https://www.zillow.com/homes/{slug}_rb/" target="_blank" rel="noopener">Zillow</a> · <a href="https://www.google.com/maps/dir/?api=1&destination={gm}" target="_blank" rel="noopener">Directions</a></div>
  </div>
</article>'''

def build(homes, notes, new_ids, today):
    g = {1: [], 2: [], 3: []}
    for h in sorted(homes.values(), key=lambda h: (h['grp'], h['price'])):
        g[h['grp']].append(card(h, notes, new_ids))
    n1, n2, n3 = len(g[1]), len(g[2]), len(g[3])
    tpl = open(os.path.join(TOOLS, "template.html"), encoding="utf-8").read()
    return (tpl.replace("{{DATE}}", today).replace("{{N1}}", str(n1)).replace("{{N2}}", str(n2)).replace("{{N3}}", str(n3))
               .replace("{{G1}}", "\n".join(g[1]) or '<p class="empty">No homes fit here right now.</p>')
               .replace("{{G2}}", "\n".join(g[2]) or '<p class="empty">No homes fit here right now.</p>')
               .replace("{{G3}}", "\n".join(g[3]) or '<p class="empty">No homes fit here right now.</p>')
               .replace("{{PRICE}}", m(MAX_PRICE)).replace("{{IDS}}", json.dumps(sorted(homes.keys()))))

def main():
    today = datetime.date.today().strftime("%B %-d, %Y")
    k8 = load_kml(os.path.join(TOOLS, "dvusd_k8.kml")); hs = load_kml(os.path.join(TOOLS, "dvusd_hs.kml"))
    notes = json.load(open(os.path.join(TOOLS, "notes.json"))) if os.path.exists(os.path.join(TOOLS, "notes.json")) else {}
    state_path = os.path.join(TOOLS, "state.json")
    prev = json.load(open(state_path)) if os.path.exists(state_path) else {}

    homes = pull()
    if len(homes) < 3:
        print("Too few homes came back; Redfin may be blocking. Not publishing."); sys.exit(2)
    for pid, h in homes.items():
        h['id'] = pid; classify(h, k8, hs)

    added = [h for pid, h in homes.items() if pid not in prev]
    dropped = [prev[pid] for pid in prev if pid not in homes]
    repriced = [(prev[pid], h) for pid, h in homes.items() if pid in prev and prev[pid]['price'] != h['price']]
    new_ids = {h['id'] for h in added} if prev else set()

    page = build(homes, notes, new_ids, today)
    open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(page)
    json.dump({pid: {k: h[k] for k in ('addr', 'city', 'zip', 'price', 'beds', 'grp', 'dT', 'dM')} for pid, h in homes.items()},
              open(state_path, "w"), indent=1)

    G = {1: "bus to both", 2: "bus to Mountain Ridge, drive to Terramar", 3: "drive to both"}
    lines = [f"# Changes {today}", "",
             f"{len(homes)} homes fit the rule: {sum(h['grp']==1 for h in homes.values())} bus to both, "
             f"{sum(h['grp']==2 for h in homes.values())} bus to Mountain Ridge only, {sum(h['grp']==3 for h in homes.values())} drive to both.", ""]
    if not prev: lines.append("First run, so everything is new.")
    for h in added: lines.append(f"ADDED  {h['addr']}, {h['city']}  {m(h['price'])}  {h['beds']} bd  ({G[h['grp']]}, {h['dT']:.1f} mi to Terramar)")
    for h in dropped: lines.append(f"GONE   {h['addr']}, {h['city']}  {m(h['price'])}  (sold, pending or withdrawn)")
    for o, h in repriced: lines.append(f"PRICE  {h['addr']}  {m(o['price'])} -> {m(h['price'])}")
    if prev and not (added or dropped or repriced): lines.append("No changes since last run.")
    open(os.path.join(ROOT, "changes.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
