#!/usr/bin/env python3
"""Sanity-checks a built dashboard before it is published. Exits non-zero if something looks wrong."""
import json, re, sys
path = sys.argv[1] if len(sys.argv) > 1 else 'docs/index.html'
html = open(path, encoding='utf-8').read()
problems = []
if '/*DATA*/' in html:
    sys.exit('PROBLEMS:\n - the /*DATA*/ placeholder was not replaced')
m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
if not m:
    sys.exit('PROBLEMS:\n - missing data block')
d = json.loads(m.group(1))
cols = d['cols']; ix = {c: i for i, c in enumerate(cols)}
cur = d['seasons'].get(d['current'])
if not cur:
    sys.exit('PROBLEMS:\n - current season missing')
if len(d['seasons']) < 3: problems.append(f'only {len(d["seasons"])} seasons of data')
for k, s in d['seasons'].items():
    ms = s['m']; teams = set(s['teams'])
    if len(teams) != 20: problems.append(f'{s["label"]}: {len(teams)} teams instead of 20')
    pairs = [(r[ix['h']], r[ix['a']]) for r in ms]
    if len(set(pairs)) != len(pairs): problems.append(f'{s["label"]}: the same fixture appears twice')
    if any(h not in teams or a not in teams for h, a in pairs): problems.append(f'{s["label"]}: a match has a team outside the team list')
    if k != d['current'] and len(ms) != 380: problems.append(f'{s["label"]}: {len(ms)} matches instead of 380')
    bad = [r for r in ms if r[ix['hg']] is None or r[ix['ag']] is None or r[ix['hg']] < 0 or r[ix['ag']] < 0]
    if bad: problems.append(f'{s["label"]}: {len(bad)} matches without a valid score')
    noxg = [r for r in ms if r[ix['hxg']] is None]
    if s['has']['xg'] and any(r[ix.get('src', 0)] != 1 for r in noxg): problems.append(f'{s["label"]}: xG flagged as present but missing in some football-data matches')
    if s['has']['xg'] and len(noxg) > 12: problems.append(f'{s["label"]}: {len(noxg)} matches without xG')
    dates = [r[ix['date']] for r in ms]
    if dates != sorted(dates): problems.append(f'{s["label"]}: matches are not in date order')
if len(cur['m']) < 1: problems.append('no games played this season yet')
p = d['proj']
if len(p['rows']) != len(cur['teams']): problems.append('projection rows do not match the team list')
for r in p['rows']:
    if abs(sum(r['pos']) - 1) > 0.01: problems.append(f'{r["t"]}: finishing-position chances add up to {sum(r["pos"]):.3f}')
for k in range(len(p['rows'])):
    col = sum(r['pos'][k] for r in p['rows'])
    if abs(col - 1) > 0.01: problems.append(f'position {k + 1}: chances add up to {col:.3f}')
exp_rem = 20 * 19 - len(cur['m'])
if p['remaining'] != exp_rem: problems.append(f'{p["remaining"]} games simulated but {exp_rem} are left')
import base64
pl = d.get('players', {}); codes = d.get('shotCodes', {}); nf = len(codes.get('fields', [])) or 12; na = len(codes.get('appFields', [])) or 10
for k, ps in pl.items():
    sh = base64.b64decode(ps['shots']); ap = base64.b64decode(ps['apps'])
    if len(sh) != ps['nShots'] * nf * 2: problems.append(f'players {k}: shot data is the wrong length')
    if len(ap) != ps['nApps'] * na * 2: problems.append(f'players {k}: appearance data is the wrong length')
    n = len(ps['players']); m = len(ps['matches'])
    import struct
    vals = struct.unpack('<%dh' % (len(sh) // 2), sh)
    for i in range(0, len(vals), nf):
        if not (0 <= vals[i] < n) or not (0 <= vals[i + 1] < m) or not (-1 <= vals[i + 10] < n):
            problems.append(f'players {k}: a shot points at a player or match that does not exist'); break
    goals = sum(1 for i in range(0, len(vals), nf) if vals[i + 6] == 0)
    tot = sum((p[7] or 0) for p in ps['players'])
    if ps['nShots'] and abs(goals - tot) > max(3, goals * 0.03): problems.append(f'players {k}: goals from shots ({goals}) and player totals ({tot}) disagree')
    if k != d['current'] and ps['src'] not in ('seed', 'espn') and ps['nShots'] < 7000: problems.append(f'players {k}: only {ps["nShots"]} shots, which looks incomplete')
if d['current'] not in pl:
    print(f'note: no player data for the current season (Understat may have been unreachable); the rest of the page is fine')
size = len(html.encode('utf-8')) / 1e6
if size > 15: problems.append(f'page is {size:.1f} MB; the hosting limit is 16 MB')
print(f'{path}: {size:.2f} MB | seasons {sorted(d["seasons"])} | current {cur["label"]}: {len(cur["m"])} games through {d["asof"]} '
      f'| xG {cur["has"]["xg"]} odds {cur["has"]["odds"]} | {len(p.get("upcoming", []))} upcoming fixtures | {len(p.get("backtest", []))} backtest seasons '
      f'| player seasons {sorted(pl)}')
if problems:
    print('PROBLEMS:'); [print(' -', x) for x in problems]; sys.exit(1)
print('OK')
