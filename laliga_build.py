#!/usr/bin/env python3
"""
Builds the La Liga dashboard (one self-contained HTML file) from public data.

    python laliga_build.py --raw raw --out docs/index.html

Match data (results, half-time scores, shots, corners, fouls, cards, xG and pre-match odds) comes from
football-data.co.uk. Matchday numbers and the remaining fixture list come from openfootball on GitHub.
Player data (every shot with its location and xG, plus minutes, assists and key passes) comes from Understat,
with an archived copy of Understat's shots on GitHub as a fallback for seasons up to 2024-25. ESPN's public match
feeds add lineups, minutes, shots on target, fouls, cards and saves for every player, and fill in results that
football-data.co.uk has not posted yet.
Raw files are cached in --raw; the season in progress is downloaded again on every run.
"""
import argparse, base64, datetime as dt, gzip, io, json, math, os, re, sys, time, unicodedata, urllib.request
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FD_URL = 'https://www.football-data.co.uk/mmz4281/{code}/SP1.csv'
MIRROR_URL = 'https://raw.githubusercontent.com/datasets/football-datasets/main/datasets/la-liga/season-{code}.csv'
OF_URL = 'https://raw.githubusercontent.com/openfootball/football.json/master/{label}/es.1.json'
N_SEASONS = 5
W_XG = 0.6          # weight on xG (vs goals) when rating teams
PRIOR_GOALS = 8.0   # shrinkage: pseudo-goals at league average added to each team's attack and defence
HOME_ROUNDS = 38
DRIFT = 0.06        # extra spread in team strength per simulated season (form, injuries, transfers)

# match row layout shared with template.html (keep in sync with COLS in the page script)
COLS = ['date', 'time', 'round', 'h', 'a', 'hg', 'ag', 'hhg', 'hag', 'hxg', 'axg', 'hs', 'as', 'hst', 'ast',
        'hf', 'af', 'hc', 'ac', 'hy', 'ay', 'hr', 'ar', 'oh', 'od', 'oa', 'src']
SRC = {'hg': 'FTHG', 'ag': 'FTAG', 'hhg': 'HTHG', 'hag': 'HTAG', 'hxg': 'HxG', 'axg': 'AxG', 'hs': 'HS', 'as': 'AS',
       'hst': 'HST', 'ast': 'AST', 'hf': 'HF', 'af': 'AF', 'hc': 'HC', 'ac': 'AC', 'hy': 'HY', 'ay': 'AY', 'hr': 'HR', 'ar': 'AR'}
ODDS = [('B365H', 'B365D', 'B365A'), ('AvgH', 'AvgD', 'AvgA'), ('PSH', 'PSD', 'PSA'), ('BWH', 'BWD', 'BWA')]


def log(*a):
    print(*a, file=sys.stderr)


def code_of(end_year):            # 2027 -> '2627'
    return f'{(end_year - 1) % 100:02d}{end_year % 100:02d}'


def label_of(end_year):           # 2027 -> '2026-27'
    return f'{end_year - 1}-{end_year % 100:02d}'


def default_current(today=None):
    today = today or dt.date.today()
    return today.year + 1 if today.month >= 8 else today.year


def download(url, dest, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'laliga-dashboard/1.0 (+github actions)'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) < 200:
                raise ValueError(f'only {len(data)} bytes')
            with open(dest + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(dest + '.tmp', dest)
            return True
        except Exception as e:  # noqa: BLE001 - report and retry
            last = e
            time.sleep(2 * (i + 1))
    log(f'  could not download {url}: {last}')
    return False


def fetch_season(raw, end_year, current, offline):
    """Returns the path of a cached SP1 csv for the season, downloading it if needed."""
    code = code_of(end_year)
    local = os.path.join(raw, f'SP1_{code}.csv')
    fresh = end_year == current
    if offline or (os.path.exists(local) and not fresh):
        return local if os.path.exists(local) else None
    log('downloading', FD_URL.format(code=code))
    if download(FD_URL.format(code=code), local):
        return local
    if not fresh:
        log('  trying the GitHub mirror instead (results and match stats, no xG or odds)')
        if download(MIRROR_URL.format(code=code), local):
            return local
    if os.path.exists(local):
        log('  using the cached copy from an earlier run')
        return local
    return None


def fetch_schedule(raw, end_year, current, offline):
    label = label_of(end_year)
    local = os.path.join(raw, f'of_{label}.json')
    fresh = end_year == current
    if not offline and (fresh or not os.path.exists(local)):
        log('downloading', OF_URL.format(label=label))
        download(OF_URL.format(label=label), local)
    return local if os.path.exists(local) else None


def read_csv(path):
    b = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            txt = b.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(io.StringIO(txt), on_bad_lines='skip')
    df.columns = [c.strip() for c in df.columns]
    df = df[df['HomeTeam'].notna() & df['AwayTeam'].notna()].copy()
    df['HomeTeam'] = df['HomeTeam'].astype(str).str.strip()
    df['AwayTeam'] = df['AwayTeam'].astype(str).str.strip()
    df = df[df['FTHG'].notna() & df['FTAG'].notna()]
    return df


def parse_dates(s):
    s = s.astype(str).str.strip()
    iso = s.str.match(r'^\d{4}-\d{2}-\d{2}')
    out = pd.Series(pd.NaT, index=s.index)
    out[iso] = pd.to_datetime(s[iso].str[:10], format='%Y-%m-%d', errors='coerce')
    long_ = ~iso & s.str.match(r'^\d{1,2}/\d{1,2}/\d{4}$')
    out[long_] = pd.to_datetime(s[long_], format='%d/%m/%Y', errors='coerce')
    short = ~iso & ~long_
    out[short] = pd.to_datetime(s[short], format='%d/%m/%y', errors='coerce')
    return out


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def load_schedule(path, of2fd):
    """openfootball json -> {(home, away): (round, date)}; also returns the unknown names."""
    if not path:
        return {}, set()
    try:
        js = json.load(open(path, encoding='utf-8'))
    except Exception as e:  # noqa: BLE001
        log('  schedule unreadable:', e)
        return {}, set()
    out, unknown = {}, set()
    for m in js.get('matches', []):
        h, a = of2fd.get(m.get('team1')), of2fd.get(m.get('team2'))
        if not h: unknown.add(m.get('team1'))
        if not a: unknown.add(m.get('team2'))
        r = re.search(r'(\d+)', str(m.get('round', '')))
        if h and a and r:
            out[(h, a)] = (int(r.group(1)), m.get('date'))
    return out, unknown


def season_matches(df, sched):
    df = df.copy()
    df['_d'] = parse_dates(df['Date'])
    df = df[df['_d'].notna()]
    df['_t'] = df['Time'].astype(str).str.slice(0, 5) if 'Time' in df.columns else ''
    df = df.sort_values(['_d', '_t'], kind='stable')
    odds = next((o for o in ODDS if all(c in df.columns and df[c].notna().mean() > 0.8 for c in o)), None)
    has_xg = bool('HxG' in df.columns and df['HxG'].notna().mean() > 0.8)
    rows, gp = [], {}
    for _, r in df.iterrows():
        h, a = r['HomeTeam'], r['AwayTeam']
        rd = sched.get((h, a), (None, None))[0]
        if rd is None:  # no schedule: use the later of the two teams' next game numbers
            rd = max(gp.get(h, 0), gp.get(a, 0)) + 1
        gp[h] = gp.get(h, 0) + 1; gp[a] = gp.get(a, 0) + 1
        row = {'date': r['_d'].strftime('%Y-%m-%d'), 'time': (r['_t'] if re.match(r'^\d\d:\d\d$', str(r['_t'])) else ''),
               'round': int(rd), 'h': h, 'a': a, 'src': 1 if ('Src' in df.columns and r['Src'] == 'espn') else 0}
        for k, c in SRC.items():
            v = num(r[c]) if c in df.columns else None
            if k in ('hxg', 'axg'):
                row[k] = round(v, 2) if (v is not None and has_xg) else None
            else:
                row[k] = int(v) if v is not None else None
        if odds:
            o = [num(r[c]) for c in odds]
            row['oh'], row['od'], row['oa'] = o if all(x and x > 1 for x in o) else (None, None, None)
        else:
            row['oh'] = row['od'] = row['oa'] = None
        rows.append(row)
    return rows, {'xg': has_xg, 'odds': odds is not None,
                  'shots': bool(rows) and all(r['hs'] is not None for r in rows),
                  'oddsSource': {'B365': 'Bet365', 'Avg': 'average', 'PS': 'Pinnacle', 'BW': 'bwin'}.get(odds[0][:-1]) if odds else None}


# ----------------------------------------------------------------------------- projection model
def fit_ratings(matches, teams, w_xg=W_XG, prior=PRIOR_GOALS, start=None):
    """Multiplicative Poisson ratings fitted to a blend of xG and goals, shrunk toward a starting rating
    (last season's, pulled toward average, or league average for clubs without one).
    Expected home goals = mu_h * att[home] * dfn[away]; expected away goals = mu_a * att[away] * dfn[home]."""
    idx = {t: i for i, t in enumerate(teams)}
    H = np.array([idx[m['h']] for m in matches]); A = np.array([idx[m['a']] for m in matches])
    def blend(g, x):
        return (w_xg * x + (1 - w_xg) * g) if x is not None else g
    bh = np.array([blend(m['hg'], m['hxg']) for m in matches], float)
    ba = np.array([blend(m['ag'], m['axg']) for m in matches], float)
    n = len(teams)
    # league scoring rates, lightly shrunk toward long-run La Liga values
    k0 = 60.0
    mu_h = (bh.sum() + 1.45 * k0) / (len(bh) + k0)
    mu_a = (ba.sum() + 1.10 * k0) / (len(ba) + k0)
    pa = np.array([start[t][0] if start and t in start else 1.0 for t in teams])
    pdf = np.array([start[t][1] if start and t in start else 1.0 for t in teams])
    att = pa.copy(); dfn = pdf.copy()
    for _ in range(200):
        s = np.bincount(H, bh, n) + np.bincount(A, ba, n)
        e = np.bincount(H, mu_h * dfn[A], n) + np.bincount(A, mu_a * dfn[H], n)
        att_new = (s + prior * pa) / (e + prior)
        c = np.bincount(H, ba, n) + np.bincount(A, bh, n)
        e2 = np.bincount(H, mu_a * att_new[A], n) + np.bincount(A, mu_h * att_new[H], n)
        dfn_new = (c + prior * pdf) / (e2 + prior)
        att_new /= np.exp(np.log(att_new).mean()); dfn_new /= np.exp(np.log(dfn_new).mean())
        done = np.abs(att_new - att).max() < 1e-9 and np.abs(dfn_new - dfn).max() < 1e-9
        att, dfn = att_new, dfn_new
        if done:
            break
    # uncertainty of each rating on the log scale (Poisson counting error), plus a little drift over the season
    s = np.bincount(H, bh, n) + np.bincount(A, ba, n); c = np.bincount(H, ba, n) + np.bincount(A, bh, n)
    sd = (np.sqrt(1.0 / (s + prior) + DRIFT ** 2), np.sqrt(1.0 / (c + prior) + DRIFT ** 2))
    return att, dfn, mu_h, mu_a, sd


def starting_ratings(prev, teams):
    """Last season's full-season ratings, pulled 40% of the way back to average. Promoted clubs start at the
    average of last season's relegated clubs, which is roughly how promoted sides do."""
    if not prev:
        return None
    pt = prev['teams']; ms = prev['matches']
    a, d, *_ = fit_ratings(ms, pt, W_XG if prev['has']['xg'] else 0.0)
    r = {t: (float(np.exp(.6 * np.log(a[i]))), float(np.exp(.6 * np.log(d[i])))) for i, t in enumerate(pt)}
    pts = final_points(ms); down = sorted(pt, key=lambda t: pts.get(t, 0))[:3]
    new = (float(np.mean([r[t][0] for t in down])), float(np.mean([r[t][1] for t in down])))
    return {t: r.get(t, new) for t in teams}


def outcome_probs(lh, la, kmax=12):
    k = np.arange(kmax + 1)
    fact = np.array([math.factorial(int(i)) for i in k], float)
    ph = np.exp(-lh) * lh ** k / fact; pa = np.exp(-la) * la ** k / fact
    M = np.outer(ph, pa)
    return float(np.tril(M, -1).sum()), float(np.trace(M)), float(np.triu(M, 1).sum())


def simulate(matches, teams, att, dfn, mu_h, mu_a, sims, seed=7, sd=None):
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    pts = np.zeros(n); gd = np.zeros(n); gf = np.zeros(n)
    played = set()
    for m in matches:
        i, j = idx[m['h']], idx[m['a']]
        played.add((i, j))
        gf[i] += m['hg']; gf[j] += m['ag']; gd[i] += m['hg'] - m['ag']; gd[j] += m['ag'] - m['hg']
        if m['hg'] > m['ag']: pts[i] += 3
        elif m['hg'] < m['ag']: pts[j] += 3
        else: pts[i] += 1; pts[j] += 1
    rem = [(i, j) for i in range(n) for j in range(n) if i != j and (i, j) not in played]
    rng = np.random.default_rng(seed)
    P = np.tile(pts, (sims, 1)); G = np.tile(gd, (sims, 1)); F = np.tile(gf, (sims, 1))
    if rem:
        hi = np.array([r[0] for r in rem]); ai = np.array([r[1] for r in rem])
        for chunk in np.array_split(np.arange(sims), max(1, sims // 5000)):
            k = len(chunk)
            if sd is not None:  # each simulated season draws its own version of every team's strength
                A_ = att[None, :] * np.exp(rng.normal(0, sd[0], (k, n)) - sd[0] ** 2 / 2)
                D_ = dfn[None, :] * np.exp(rng.normal(0, sd[1], (k, n)) - sd[1] ** 2 / 2)
            else:
                A_ = np.tile(att, (k, 1)); D_ = np.tile(dfn, (k, 1))
            lh = mu_h * A_[:, hi] * D_[:, ai]; la = mu_a * A_[:, ai] * D_[:, hi]
            gh = rng.poisson(lh); ga = rng.poisson(la)
            ph = np.where(gh > ga, 3, np.where(gh == ga, 1, 0)); pa = np.where(ga > gh, 3, np.where(gh == ga, 1, 0))
            for col, (i, j) in enumerate(rem):
                P[chunk, i] += ph[:, col]; P[chunk, j] += pa[:, col]
                d = gh[:, col] - ga[:, col]
                G[chunk, i] += d; G[chunk, j] -= d
                F[chunk, i] += gh[:, col]; F[chunk, j] += ga[:, col]
    key = P * 1e6 + (G + 1000) * 1e3 + F + rng.random(P.shape)
    order = np.argsort(-key, axis=1)
    pos = np.empty_like(order); rows = np.arange(sims)[:, None]
    pos[rows, order] = np.arange(n)[None, :]
    posprob = np.stack([(pos == k).mean(axis=0) for k in range(n)], axis=1)  # team x position
    return P, posprob, rem


def project(matches, teams, sims, has_xg, start=None):
    att, dfn, mu_h, mu_a, sd = fit_ratings(matches, teams, W_XG if has_xg else 0.0, start=start)
    P, posprob, rem = simulate(matches, teams, att, dfn, mu_h, mu_a, sims, sd=sd)
    n = len(teams)
    out = []
    lo, hi = int(P.min()), int(P.max())
    for t, i in zip(teams, range(n)):
        p = P[:, i]
        hist = np.bincount((p - lo).astype(int), minlength=hi - lo + 1)
        gfor = (mu_h + mu_a) / 2 * att[i]; gag = (mu_h + mu_a) / 2 * dfn[i]
        out.append({'t': t, 'att': round(float(gfor), 3), 'def': round(float(gag), 3), 'net': round(float(gfor - gag), 3),
                    'mean': round(float(p.mean()), 2), 'p10': int(np.percentile(p, 10)), 'p50': int(np.percentile(p, 50)),
                    'p90': int(np.percentile(p, 90)),
                    'title': round(float(posprob[i, 0]), 4), 'top4': round(float(posprob[i, :4].sum()), 4),
                    'top6': round(float(posprob[i, :6].sum()), 4), 'rel': round(float(posprob[i, -3:].sum()), 4),
                    'pos': [round(float(x), 4) for x in posprob[i]], 'hist': hist.tolist()})
    out.sort(key=lambda r: -r['mean'])
    return {'sims': sims, 'remaining': len(rem), 'histLo': lo, 'muH': round(float(mu_h), 3), 'muA': round(float(mu_a), 3),
            'wxg': W_XG if has_xg else 0.0, 'prior': PRIOR_GOALS, 'drift': DRIFT,
            'ratings': {t: [round(float(att[i]), 4), round(float(dfn[i]), 4)] for i, t in enumerate(teams)},
            'rows': out}


def final_points(matches):
    pts = {}
    for m in matches:
        for t in (m['h'], m['a']): pts.setdefault(t, 0)
        if m['hg'] > m['ag']: pts[m['h']] += 3
        elif m['hg'] < m['ag']: pts[m['a']] += 3
        else: pts[m['h']] += 1; pts[m['a']] += 1
    return pts


def backtest(hist, cut, sims=4000):
    """For finished seasons: fit on the first `cut` matches, project, compare with the real final table."""
    res = []
    for n_, (key, s) in enumerate(hist):
        ms = s['matches']
        teams = sorted({m['h'] for m in ms} | {m['a'] for m in ms})
        if len(ms) < 380 or cut >= len(ms) or cut < 20:
            continue
        early = ms[:cut]
        prev = hist[n_ - 1][1] if n_ > 0 and hist[n_ - 1][1]['year'] == s['year'] - 1 else None
        att, dfn, mu_h, mu_a, sd = fit_ratings(early, teams, W_XG if s['has']['xg'] else 0.0, start=starting_ratings(prev, teams))
        P, posprob, _ = simulate(early, teams, att, dfn, mu_h, mu_a, sims, seed=11, sd=sd)
        actual = final_points(ms); now = final_points(early)
        gp = {t: sum(1 for m in early if t in (m['h'], m['a'])) for t in teams}
        model = {t: float(P[:, i].mean()) for i, t in enumerate(teams)}
        naive = {t: now.get(t, 0) / max(gp[t], 1) * 38 for t in teams}
        champ = max(teams, key=lambda t: (actual[t]))
        ti = teams.index(champ)
        fav = teams[int(np.argmax(posprob[:, 0]))]
        rel_actual = sorted(teams, key=lambda t: actual[t])[:3]
        rel_p = [float(posprob[teams.index(t), -3:].sum()) for t in rel_actual]
        res.append({'season': key, 'cut': cut,
                    'maeModel': round(float(np.mean([abs(model[t] - actual[t]) for t in teams])), 2),
                    'maeNaive': round(float(np.mean([abs(naive[t] - actual[t]) for t in teams])), 2),
                    'champ': champ, 'champP': round(float(posprob[ti, 0]), 3), 'fav': fav,
                    'favP': round(float(posprob[:, 0].max()), 3),
                    'relP': round(float(np.mean(rel_p)), 3), 'xg': s['has']['xg'], 'prior': prev is not None})
    return res


def upcoming(sched_path, of2fd, played_pairs, ratings, mu_h, mu_a, today):
    if not sched_path:
        return []
    try:
        js = json.load(open(sched_path, encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in js.get('matches', []):
        h, a = of2fd.get(m.get('team1')), of2fd.get(m.get('team2'))
        if not h or not a or (h, a) in played_pairs or h not in ratings or a not in ratings:
            continue
        r = re.search(r'(\d+)', str(m.get('round', '')))
        lh = mu_h * ratings[h][0] * ratings[a][1]; la = mu_a * ratings[a][0] * ratings[h][1]
        pw, pd_, pl = outcome_probs(lh, la)
        tm = str(m.get('time') or '')[:5]
        out.append({'round': int(r.group(1)) if r else None, 'date': m.get('date'), 'time': tm if re.match(r'^\d\d:\d\d$', tm) else '', 'h': h, 'a': a,
                    'p': [round(pw, 3), round(pd_, 3), round(pl, 3)], 'xg': [round(lh, 2), round(la, 2)]})
    out.sort(key=lambda x: (x['round'] or 99, x['date'] or ''))
    return out



# ----------------------------------------------------------------------------- players (Understat)
US_BASE = 'https://understat.com/'
US_HEADERS = {'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://understat.com/',
              'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
              'Accept': 'application/json, text/javascript, */*; q=0.01', 'Accept-Encoding': 'gzip'}
ARCHIVE_URL = 'https://github.com/JaseZiv/worldfootballR_data/releases/download/understat_shots/la_liga_shot_data.rds'
ARCHIVE_LAST = 2024        # last season start year the archive has in full
AGG_URL = 'https://raw.githubusercontent.com/vibedatascience/understat_players_aggregated/main/understat_players_aggregated_2014_2024.csv'
SEED_DIR = os.path.join(HERE, 'static', 'players_seed')
RES = ['Goal', 'SavedShot', 'MissedShots', 'BlockedShot', 'ShotOnPost', 'OwnGoal']
SIT = ['OpenPlay', 'FromCorner', 'SetPiece', 'DirectFreekick', 'Penalty']
TYP = ['RightFoot', 'LeftFoot', 'Head', 'OtherBodyPart']
LAST = ['Pass', 'Cross', 'None', 'Aerial', 'Standard', 'Chipped', 'TakeOn', 'Rebound', 'BallRecovery', 'Throughball',
        'BallTouch', 'HeadPass', 'LayOff', 'Dispossessed', 'Other']
SHOT_FIELDS = ['p', 'm', 'min', 'x', 'y', 'xg', 'res', 'sit', 'typ', 'last', 'ast', 'side']   # int16 each, keep in sync with the page
APP_FIELDS = ['p', 'm', 'min', 'g', 'a', 'sh', 'xg', 'xa', 'kp', 'st']


def norm(t):
    t = unicodedata.normalize('NFKD', str(t)).encode('ascii', 'ignore').decode().lower()
    t = re.sub(r'\b(cf|fc|ud|cd|rcd|rc|ca|sd|club|de|real|deportivo|sad)\b', ' ', t)
    return re.sub(r'[^a-z]+', '', t)


def team_mapper(teams_meta):
    exact = {}
    for k, v in teams_meta.items():
        for alias in [k, v.get('name', '')] + v.get('us', []) + v.get('of', []):
            exact[alias] = k
    loose = {norm(a): k for a, k in exact.items() if norm(a)}
    unknown = set()
    def f(title):
        if title in exact: return exact[title]
        n = norm(title)
        if n in loose: return loose[n]
        for a, k in loose.items():
            if n and (n in a or a in n) and min(len(n), len(a)) >= 5: return k
        unknown.add(title)
        return title
    f.unknown = unknown
    return f


def us_get(path, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(US_BASE + path, headers=US_HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read(); enc = r.headers.get('Content-Encoding', '')
            if enc == 'gzip' or b[:2] == b'\x1f\x8b':
                b = gzip.decompress(b)
            return json.loads(b.decode('utf-8'))
        except Exception as e:  # noqa: BLE001
            last = e; time.sleep(1.5 * (i + 1))
    log(f'  understat {path}: {last}')
    return None


def pick_key(d, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return None


def fetch_understat(raw, end_year, current, offline, delay=0.35):
    """League data plus one file per finished match, cached in raw/understat/<start year>/."""
    start = end_year - 1
    d = os.path.join(raw, 'understat', str(start)); os.makedirs(d, exist_ok=True)
    lp = os.path.join(d, 'league.json')
    league = None
    if not offline and (end_year == current or not os.path.exists(lp)):
        log(f'understat: getLeagueData/La_liga/{start}')
        js = us_get(f'getLeagueData/La_liga/{start}')
        if js and pick_key(js, 'dates', 'datesData'):
            league = js; json.dump(js, open(lp, 'w'))
    if league is None and os.path.exists(lp):
        league = json.load(open(lp))
    if not league:
        return None
    done = [x for x in pick_key(league, 'dates', 'datesData') or [] if str(x.get('isResult')).lower() in ('true', '1')]
    matches, missing, fetched = {}, 0, 0
    for x in done:
        mid = str(x['id']); mp = os.path.join(d, f'm{mid}.json')
        if os.path.exists(mp):
            try:
                matches[mid] = json.load(open(mp)); continue
            except Exception:  # noqa: BLE001
                pass
        if offline:
            missing += 1; continue
        js = us_get(f'getMatchData/{mid}'); fetched += 1; time.sleep(delay)
        if js and pick_key(js, 'shots', 'shotsData') is not None:
            json.dump(js, open(mp, 'w')); matches[mid] = js
        else:
            missing += 1
    if fetched:
        log(f'  understat {start}: downloaded {fetched} match files')
    return {'league': league, 'matches': matches, 'missing': missing, 'played': len(done)}


def f_(v, default=0.0):
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def code(lst, v):
    v = 'None' if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
    return lst.index(v) if v in lst else len(lst) - 1


def new_player(pid, name):
    return {'id': pid, 'name': name, 'pos': '', 'teams': {}, 'min': None, 'gp': None, 'gs': None, 'g': 0, 'a': 0, 'sh': 0,
            'xg': 0.0, 'xa': 0.0, 'npg': 0, 'npxg': 0.0, 'kp': 0, 'yc': None, 'rc': None}


def add_shot(P, shots, pid, name, team, mi, s, side, ast_pid):
    res = code(RES, s.get('result'))
    if RES[res] == 'OwnGoal':
        return
    p = P.setdefault(pid, new_player(pid, name))
    p['teams'][team] = p['teams'].get(team, 0) + 1
    xg = f_(s.get('xG')); sit = code(SIT, s.get('situation'))
    p['sh'] += 1; p['xg'] += xg
    if SIT[sit] != 'Penalty': p['npxg'] += xg
    shots.append([pid, mi, int(f_(s.get('minute'))), round(f_(s.get('X')) * 10000), round(f_(s.get('Y')) * 10000),
                  round(xg * 10000), res, sit, code(TYP, s.get('shotType')), code(LAST, s.get('lastAction')), ast_pid, side])


def season_from_understat(us, tmap):
    league = us['league']
    meta = {str(p['id']): p for p in pick_key(league, 'players', 'playersData') or []}
    dates = sorted(pick_key(league, 'dates', 'datesData') or [], key=lambda x: x.get('datetime', ''))
    matches, midx = [], {}
    for x in dates:
        mid = str(x['id'])
        if mid not in us['matches']:
            continue
        midx[mid] = len(matches)
        matches.append([x.get('datetime', '')[:10], tmap(x['h']['title']), tmap(x['a']['title']), int(f_(x['goals']['h'])), int(f_(x['goals']['a']))])
    P, shots, apps = {}, [], []
    for mid, js in us['matches'].items():
        if mid not in midx:
            continue
        mi = midx[mid]; m = matches[mi]
        rost = pick_key(js, 'rosters', 'rostersData') or {}
        names = {}
        for si, side in enumerate(('h', 'a')):
            team = m[1 + si]
            for r in (rost.get(side) or {}).values():
                pid = str(r.get('player_id')); name = r.get('player', '')
                names[(side, name)] = pid
                minutes = int(f_(r.get('time')))
                if minutes <= 0:
                    continue
                p = P.setdefault(pid, new_player(pid, name))
                for k in ('min', 'gp', 'gs', 'yc', 'rc'):
                    if p[k] is None: p[k] = 0
                started = 0 if str(r.get('position', '')).lower() == 'sub' else 1
                g, a, kp = int(f_(r.get('goals'))), int(f_(r.get('assists'))), int(f_(r.get('key_passes')))
                p['min'] += minutes; p['gp'] += 1; p['gs'] += started
                p['g'] += g; p['a'] += a; p['kp'] += kp; p['xa'] += f_(r.get('xA'))
                p['yc'] += int(f_(r.get('yellow_card'))); p['rc'] += int(f_(r.get('red_card')))
                p['teams'][team] = p['teams'].get(team, 0) + minutes
                apps.append([pid, mi, minutes, g, a, int(f_(r.get('shots'))), round(f_(r.get('xG')) * 100), round(f_(r.get('xA')) * 100), kp, started])
        sh = pick_key(js, 'shots', 'shotsData') or {}
        for si, side in enumerate(('h', 'a')):
            team = m[1 + si]
            for s in sh.get(side) or []:
                pid = str(s.get('player_id'))
                ast = names.get((side, s.get('player_assisted'))) if s.get('player_assisted') else None
                add_shot(P, shots, pid, s.get('player', ''), team, mi, s, si, ast)
    for pid, p in P.items():
        mp = meta.get(pid, {})
        pos = [t for t in str(mp.get('position', '')).split() if t != 'S']
        p['pos'] = pos[0] if pos else ''
    pen_goals = {}
    for r in shots:
        if SIT[r[7]] == 'Penalty' and RES[r[6]] == 'Goal': pen_goals[r[0]] = pen_goals.get(r[0], 0) + 1
    for pid, p in P.items():
        p['npg'] = p['g'] - pen_goals.get(pid, 0)
    return P, matches, shots, apps, True


def season_from_archive(df, start, tmap):
    d = df[df['season'] == start]
    if d.empty:
        return None
    d = d.sort_values(['date', 'match_id', 'minute']).copy()
    # older seasons in the archive use h_a, newer ones home_away
    ha = d['h_a'] if 'h_a' in d.columns else None
    if 'home_away' in d.columns:
        ha = d['home_away'].where(d['home_away'].notna(), ha) if ha is not None else d['home_away']
    d['h_a'] = ha
    matches, midx = [], {}
    for mid, g in d.groupby('match_id', sort=False):
        r = g.iloc[0]
        midx[mid] = len(matches)
        matches.append([str(r['date'])[:10], tmap(r['home_team']), tmap(r['away_team']), int(f_(r['home_goals'])), int(f_(r['away_goals']))])
    P, shots = {}, []
    name_to_pid = {}
    for r in d.itertuples(index=False):
        side = 0 if r.h_a == 'h' else 1
        team = matches[midx[r.match_id]][1 + side]
        name_to_pid[(team, r.player)] = str(int(r.player_id))
    for r in d.itertuples(index=False):
        side = 0 if r.h_a == 'h' else 1
        mi = midx[r.match_id]; team = matches[mi][1 + side]
        pid = str(int(r.player_id))
        ast = None
        if isinstance(r.player_assisted, str) and r.player_assisted:
            ast = name_to_pid.get((team, r.player_assisted)) or ('n:' + r.player_assisted)
            a = P.setdefault(ast, new_player(ast, r.player_assisted))
            a['teams'].setdefault(team, 0)
        s = {'result': r.result, 'xG': r.xG, 'situation': r.situation, 'minute': r.minute, 'X': r.X, 'Y': r.Y,
             'shotType': r.shotType, 'lastAction': r.lastAction}
        add_shot(P, shots, pid, r.player, team, mi, s, side, ast)
    for row in shots:
        p = P[row[0]]
        if RES[row[6]] == 'Goal':
            p['g'] += 1
            if SIT[row[7]] != 'Penalty': p['npg'] += 1
        if row[10] is not None:
            a = P[row[10]]; a['kp'] += 1; a['xa'] += row[5] / 10000
            if RES[row[6]] == 'Goal': a['a'] += 1
    return P, matches, shots, [], False



# ----------------------------------------------------------------------------- ESPN (lineups, minutes, match sheets, same-night results)
ESPN_SB = 'https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard?dates={d0}-{d1}&limit=500'
ESPN_SUM = 'https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/summary?event={id}'
ESPN_HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
                'Accept': 'application/json'}
ESPN_POS = {'G': 'GK', 'GK': 'GK', 'D': 'D', 'M': 'M', 'F': 'F', 'A': 'F'}
PLAYER_EXTRA = ['sot', 'fc', 'fs', 'off', 'sv', 'gc']   # ESPN-only per-player fields


def espn_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=ESPN_HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read()
            if b[:2] == b'\x1f\x8b':
                b = gzip.decompress(b)
            return json.loads(b.decode('utf-8'))
        except Exception as e:  # noqa: BLE001
            last = e; time.sleep(1.5 * (i + 1))
    log(f'  espn {url[-60:]}: {last}')
    return None


def month_windows(start_year, end_year):
    out = []
    y, m = start_year, 7
    while (y, m) <= (end_year, 6):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = dt.date(ny, nm, 1) - dt.timedelta(days=1)
        out.append((dt.date(y, m, 1), last)); y, m = ny, nm
    return out


def fetch_espn(raw, end_year, current, offline, delay=0.25):
    """Scoreboard month by month plus one summary file per finished game, cached in raw/espn/<start year>/."""
    start = end_year - 1
    d = os.path.join(raw, 'espn', str(start)); os.makedirs(d, exist_ok=True)
    today = dt.date.today(); events = {}
    for d0, d1 in month_windows(start, end_year):
        if d0 > today:
            break
        mp = os.path.join(d, f'sb_{d0:%Y%m}.json')
        settled = d1 < today - dt.timedelta(days=10)
        js = None
        if offline or (settled and os.path.exists(mp)):
            js = json.load(open(mp)) if os.path.exists(mp) else None
        else:
            js = espn_get(ESPN_SB.format(d0=f'{d0:%Y%m%d}', d1=f'{min(d1, today):%Y%m%d}')); time.sleep(delay)
            if js and isinstance(js.get('events'), list):
                json.dump(js, open(mp, 'w'))
            elif os.path.exists(mp):
                js = json.load(open(mp))
        for ev in (js or {}).get('events') or []:
            try:
                comp = ev['competitions'][0]
                if not (comp.get('status') or ev.get('status') or {}).get('type', {}).get('completed'):
                    continue
                events[str(ev['id'])] = ev
            except (KeyError, IndexError, TypeError):
                continue
    out, missing, fetched = {}, 0, 0
    for eid, ev in events.items():
        sp = os.path.join(d, f'e{eid}.json')
        if os.path.exists(sp):
            try:
                out[eid] = {'event': ev, 'summary': json.load(open(sp))}; continue
            except Exception:  # noqa: BLE001
                pass
        if offline:
            missing += 1; continue
        js = espn_get(ESPN_SUM.format(id=eid)); fetched += 1; time.sleep(delay)
        if js and (js.get('rosters') or js.get('boxscore')):
            json.dump(js, open(sp, 'w')); out[eid] = {'event': ev, 'summary': js}
        else:
            missing += 1
    if fetched or missing:
        log(f'  espn {start}: {len(out)} finished games, downloaded {fetched}, missing {missing}')
    return out if out else None


def espn_sides(ev):
    """(home, away) competitor dicts from a scoreboard event, by homeAway."""
    comp = ev['competitions'][0]; c = {x.get('homeAway'): x for x in comp.get('competitors', [])}
    return c.get('home'), c.get('away')


def espn_local_datetime(iso):
    try:
        t = dt.datetime.strptime(iso[:16], '%Y-%m-%dT%H:%M').replace(tzinfo=dt.timezone.utc)
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            t = t.astimezone(ZoneInfo('Europe/Madrid'))
        except Exception:  # noqa: BLE001
            pass
        return t.strftime('%Y-%m-%d'), t.strftime('%H:%M')
    except (ValueError, TypeError):
        return None, ''


def stat_map(items):
    out = {}
    for it in items or []:
        n = it.get('name') or it.get('abbreviation')
        v = it.get('value')
        if v is None:
            m = re.match(r'-?\d+(\.\d+)?', str(it.get('displayValue', '')))
            v = float(m.group(0)) if m else None
        if n and v is not None:
            out[n] = f_(v)
    return out


def espn_match_rows(es, tmap, have_pairs):
    """football-data-shaped rows for finished games the CSV does not have yet."""
    rows = []
    for eid, rec in (es or {}).items():
        ev, sm = rec['event'], rec['summary']
        try:
            home, away = espn_sides(ev)
            h, a = tmap(home['team']['displayName']), tmap(away['team']['displayName'])
        except (KeyError, TypeError):
            continue
        if (h, a) in have_pairs:
            continue
        date, tm = espn_local_datetime(ev['competitions'][0].get('date') or ev.get('date', ''))
        if not date:
            continue
        hg, ag = int(f_(home.get('score'))), int(f_(away.get('score')))
        ls = {'home': home.get('linescores') or [], 'away': away.get('linescores') or []}
        hhg = int(f_(ls['home'][0].get('value'))) if ls['home'] else None
        hag = int(f_(ls['away'][0].get('value'))) if ls['away'] else None
        ts = {}
        for t in (sm.get('boxscore') or {}).get('teams') or []:
            side = t.get('homeAway') or ('home' if str((t.get('team') or {}).get('id')) == str(home['team']['id']) else 'away')
            ts[side] = stat_map(t.get('statistics'))
        th, ta = ts.get('home', {}), ts.get('away', {})
        g = lambda d, k: (int(d[k]) if k in d else None)  # noqa: E731
        rows.append({'Div': 'SP1', 'Date': date, 'Time': tm, 'HomeTeam': h, 'AwayTeam': a, 'FTHG': hg, 'FTAG': ag,
                     'FTR': 'H' if hg > ag else 'A' if hg < ag else 'D', 'HTHG': hhg, 'HTAG': hag,
                     'HTR': (None if hhg is None else 'H' if hhg > hag else 'A' if hhg < hag else 'D'),
                     'HS': g(th, 'totalShots'), 'AS': g(ta, 'totalShots'), 'HST': g(th, 'shotsOnTarget'), 'AST': g(ta, 'shotsOnTarget'),
                     'HF': g(th, 'foulsCommitted'), 'AF': g(ta, 'foulsCommitted'), 'HC': g(th, 'wonCorners'), 'AC': g(ta, 'wonCorners'),
                     'HY': g(th, 'yellowCards'), 'AY': g(ta, 'yellowCards'), 'HR': g(th, 'redCards'), 'AR': g(ta, 'redCards'), 'Src': 'espn'})
    return rows


def add_espn_rows(df, es, tmap):
    if not es:
        return df
    have = set(zip(df['HomeTeam'], df['AwayTeam']))
    rows = espn_match_rows(es, tmap, have)
    if not rows:
        return df
    df = df.copy(); df['Src'] = 'fd'
    extra = pd.DataFrame(rows)
    log(f'  espn: {len(rows)} finished game(s) not yet on football-data.co.uk, added without xG or odds')
    return pd.concat([df, extra], ignore_index=True, sort=False)


def clock_minute(play, full=90):
    c = play.get('clock') or {}
    m = re.match(r'(\d+)', str(c.get('displayValue', '')))
    if m:
        return min(full, int(m.group(1)))
    v = c.get('value')
    return min(full, int(round(f_(v) / 60))) if v else None


def espn_minutes(entry, red_minute=None, full=90):
    """Minutes on the pitch from the lineup flags and substitution clocks. Returns (minutes, started)."""
    starter = bool(entry.get('starter'))
    subs = sorted([p for p in entry.get('plays') or [] if 'ubstitut' in str((p.get('type') or {}).get('text', '')) or 'ubstitut' in str(p.get('text', ''))],
                  key=lambda p: f_((p.get('clock') or {}).get('value')))
    sin, sout = bool(entry.get('subbedIn')), bool(entry.get('subbedOut'))
    stats = stat_map(entry.get('stats'))
    if starter:
        start, end = 0, full
        if sout:
            end = clock_minute(subs[0]) if subs else 70
    else:
        if not sin and not subs and not stats.get('appearances') and not stats.get('subIns'):
            return 0, False
        start = (clock_minute(subs[0]) if subs else None)
        if start is None:
            start = 70
        end = full
        if sout:
            end = clock_minute(subs[1]) if len(subs) > 1 else full
    if red_minute is not None:
        end = min(end, red_minute)
    return max(0, end - start), starter


def red_cards(sm):
    out = {}
    for ev in sm.get('keyEvents') or []:
        txt = str((ev.get('type') or {}).get('text', '')).lower()
        if 'red' in txt:
            for pt in ev.get('participants') or []:
                pid = str((pt.get('athlete') or {}).get('id', ''))
                mm = clock_minute(ev)
                if pid and mm is not None:
                    out[pid] = mm
    return out


def name_tokens(n):
    return [t for t in re.split(r'[\s\-]+', unicodedata.normalize('NFKD', str(n)).encode('ascii', 'ignore').decode().lower()) if t]


def espn_player_match(by_team, team, name, cache):
    """Find the Understat player with this name on this team: exact name, then a unique surname, then shared tokens."""
    key = (team, name)
    if key in cache:
        return cache[key]
    toks = name_tokens(name); cands = by_team.get(team, []); hit = None
    if toks:
        full = ' '.join(toks)
        exact = [p for p in cands if ' '.join(name_tokens(p['name'])) == full]
        if len(exact) == 1: hit = exact[0]
        if hit is None:
            last = toks[-1]; same = [p for p in cands if name_tokens(p['name']) and name_tokens(p['name'])[-1] == last]
            if len(same) == 1: hit = same[0]
        if hit is None:
            st = set(toks); best, score = None, 0
            for p in cands:
                pt = set(name_tokens(p['name'])); j = len(st & pt) / len(st | pt) if st | pt else 0
                if j > score: best, score = p, j
            if best is not None and score >= 0.5: hit = best
    cache[key] = hit
    return hit


def merge_espn(P, matches, apps, es, tmap):
    """Adds minutes, appearances and match-sheet stats from ESPN to the player table, and ESPN-only players."""
    if not es:
        return apps, False
    pair = {(m[1], m[2]): i for i, m in enumerate(matches)}
    by_team = {}
    for p in P.values():
        for t in p['teams']: by_team.setdefault(t, []).append(p)
    cache, eapps, E = {}, [], {}
    def bucket(p):
        return E.setdefault(p['id'], {'min': 0, 'gp': 0, 'gs': 0, 'g': 0, 'a': 0, 'sh': 0, 'yc': 0, 'rc': 0, 'sot': 0, 'fc': 0, 'fs': 0, 'off': 0, 'sv': 0, 'gc': 0})
    for eid, rec in es.items():
        ev, sm = rec['event'], rec['summary']
        try:
            home, away = espn_sides(ev)
            h, a = tmap(home['team']['displayName']), tmap(away['team']['displayName'])
        except (KeyError, TypeError):
            continue
        if (h, a) not in pair:
            date, _ = espn_local_datetime(ev['competitions'][0].get('date') or ev.get('date', ''))
            if not date: continue
            pair[(h, a)] = len(matches); matches.append([date, h, a, int(f_(home.get('score'))), int(f_(away.get('score')))])
        mi = pair[(h, a)]; reds = red_cards(sm)
        for side in sm.get('rosters') or []:
            team = h if side.get('homeAway') == 'home' else a
            for entry in side.get('roster') or []:
                ath = entry.get('athlete') or {}; name = ath.get('displayName') or ath.get('shortName') or ''
                eid_p = str(ath.get('id', ''))
                if not name:
                    continue
                minutes, started = espn_minutes(entry, reds.get(eid_p))
                if minutes <= 0:
                    continue
                p = espn_player_match(by_team, team, name, cache)
                if p is None:
                    pid = 'e:' + eid_p
                    p = P.get(pid)
                    if p is None:
                        p = P[pid] = new_player(pid, name)
                        pos = ESPN_POS.get(str(((entry.get('position') or ath.get('position') or {}).get('abbreviation') or '')).upper()[:2], '')
                        p['pos'] = pos
                        by_team.setdefault(team, []).append(p); cache[(team, name)] = p
                st = stat_map(entry.get('stats')); b = bucket(p)
                b['min'] += minutes; b['gp'] += 1; b['gs'] += int(started)
                for k, src in (('g', 'totalGoals'), ('a', 'goalAssists'), ('sh', 'totalShots'), ('yc', 'yellowCards'), ('rc', 'redCards'), ('sot', 'shotsOnTarget'),
                                ('fc', 'foulsCommitted'), ('fs', 'foulsSuffered'), ('off', 'offsides'), ('sv', 'saves'), ('gc', 'goalsConceded')):
                    b[k] += int(st.get(src, 0))
                p['teams'][team] = p['teams'].get(team, 0) + minutes
                if not p['pos']:
                    p['pos'] = ESPN_POS.get(str(((entry.get('position') or ath.get('position') or {}).get('abbreviation') or '')).upper()[:2], '')
                eapps.append([p['id'], mi, minutes, int(st.get('totalGoals', 0)), int(st.get('goalAssists', 0)), int(st.get('totalShots', 0)), 0, 0, 0, int(started)])
    for pid, b in E.items():
        p = P[pid]
        if p['min'] is None:   # no minutes from Understat: take ESPN's
            p['min'], p['gp'], p['gs'], p['yc'], p['rc'] = b['min'], b['gp'], b['gs'], b['yc'], b['rc']
        if pid.startswith('e:') or (p['sh'] == 0 and p['g'] == 0 and p['a'] == 0):  # ESPN-only player: scoring lines from ESPN too
            p['g'], p['a'], p['sh'], p['npg'] = b['g'], b['a'], b['sh'], b['g']
        for k in PLAYER_EXTRA: p[k] = b[k]
    use_eapps = not apps
    return (eapps if use_eapps else apps), use_eapps


def load_aggregates(raw, offline):
    """Understat season totals (minutes, games, cards, position) for finished seasons, mirrored on GitHub."""
    path = os.path.join(raw, 'understat', 'players_aggregated_2014_2024.csv')
    if not os.path.exists(path) and not offline:
        log('downloading Understat season totals for finished seasons (7 MB, once)')
        download(AGG_URL, path)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception as e:  # noqa: BLE001
        log('  season totals unreadable:', e); return None
    return df[df['league'].astype(str).str.lower().str.replace('_', '') == 'laliga']


def apply_aggregates(P, agg, start):
    """Fills minutes, games, cards and positions for Understat player ids from the season totals file."""
    if agg is None:
        return False
    d = agg[(agg['year'] == start) | (agg['season'].astype(str) == f'{start}/{(start + 1) % 100:02d}')] if 'year' in agg.columns else agg[agg['season'].astype(str) == f'{start}/{(start + 1) % 100:02d}']
    hit = 0
    for r in d.itertuples(index=False):
        p = P.get(str(int(f_(r.id))))
        if p is None:
            continue
        p['min'] = int(f_(r.time)); p['gp'] = int(f_(r.games)); p['yc'] = int(f_(r.yellow_cards)); p['rc'] = int(f_(r.red_cards))
        pos = str(getattr(r, 'primary_position', '') or getattr(r, 'position', '') or '').split()
        pos = [t for t in pos if t not in ('S', 'nan')]
        if pos and not p['pos']: p['pos'] = pos[0]
        hit += 1
    return hit > 0


def season_from_seed(path, tmap):
    """A hand-collected season file (static/players_seed/<code>.json): season totals only, used when nothing else is available."""
    js = json.load(open(path, encoding='utf-8')); F = js['fields']; P = {}
    for i, row in enumerate(js['rows']):
        r = dict(zip(F, row)); pid = f's:{i}'; p = new_player(pid, r['name'])
        p['teams'][tmap(r['team'])] = r.get('gp') or 1
        p['pos'] = {'F': 'F', 'M': 'M', 'D': 'D', 'GK': 'GK'}.get(str(r.get('pos') or ''), '')
        p['gp'] = r.get('gp'); p['g'] = r.get('g'); p['a'] = r.get('a')
        p['npg'] = (r.get('g') or 0) - (r.get('pk') or 0); p['gc'] = r.get('gc')
        P[pid] = p
    return P, [], [], [], False, 'seed', js


def pack(rows, width):
    arr = np.array(rows, dtype=np.int16).reshape(-1) if rows else np.zeros(0, dtype=np.int16)
    assert arr.size == len(rows) * width
    return base64.b64encode(arr.astype('<i2').tobytes()).decode()


def encode_players(P, matches, shots, apps, has_min, src, espn=False, seed=None):
    keep = [p for p in P.values() if p['sh'] or p['a'] or p['kp'] or (p['min'] or 0) > 0 or src == 'seed']
    keep.sort(key=lambda p: (-(p['min'] or 0), -p['sh'], p['name']))
    idx = {p['id']: i for i, p in enumerate(keep)}
    rows = []
    for p in keep:
        teams = [t for t, _ in sorted(p['teams'].items(), key=lambda kv: -kv[1])]
        rows.append([p['id'], p['name'], p['pos'], teams, p['min'], p['gp'], p['gs'], p['g'], p['a'], p['sh'],
                     round(p['xg'], 2), round(p['xa'], 2), p['npg'], round(p['npxg'], 2), p['kp'], p['yc'], p['rc']] + [p.get(k) for k in PLAYER_EXTRA])
    srows = [[idx[r[0]]] + r[1:10] + [idx.get(r[10], -1) if r[10] is not None else -1, r[11]] for r in shots if r[0] in idx]
    arows = [[idx[r[0]]] + r[1:] for r in apps if r[0] in idx]
    return {'src': src, 'hasMin': has_min, 'espn': espn, 'matches': matches, 'players': rows,
            'note': (seed or {}).get('note'), 'source': (seed or {}).get('source'),
            'shots': pack(srows, len(SHOT_FIELDS)), 'nShots': len(srows), 'apps': pack(arows, len(APP_FIELDS)), 'nApps': len(arows)}


def build_players(raw, years, current, offline, tmap, live=True, espn_by_year=None):
    out = {}
    archive = {'df': None, 'tried': False}
    agg = {'df': None, 'tried': False}
    def load_agg():
        if not agg['tried']:
            agg['tried'] = True; agg['df'] = load_aggregates(raw, offline)
        return agg['df']
    def load_archive():
        if archive['tried']:
            return archive['df']
        archive['tried'] = True
        path = os.path.join(raw, 'understat', 'archive_la_liga_shots.rds')
        if not os.path.exists(path) and not offline:
            log('downloading the archived Understat shots (about 27 MB, once)')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            download(ARCHIVE_URL, path)
        if not os.path.exists(path):
            return None
        try:
            import pyreadr  # noqa: PLC0415 - optional dependency, only for the fallback
            archive['df'] = list(pyreadr.read_r(path).values())[0]
        except Exception as e:  # noqa: BLE001
            log('  archive unreadable:', e)
        return archive['df']
    for y in years:
        key, start = code_of(y), y - 1
        got = None
        if live:
            us = fetch_understat(raw, y, current, offline)
            if us and us['matches']:
                got = season_from_understat(us, tmap) + ('understat',)
                if us['missing']:
                    log(f'  understat {label_of(y)}: {us["missing"]} of {us["played"]} finished matches could not be downloaded; the next run retries them')
        if got is None and start <= ARCHIVE_LAST:
            df = load_archive()
            if df is not None:
                r = season_from_archive(df, start, tmap)
                if r: got = r + ('archive',)
        es = (espn_by_year or {}).get(y)
        seed = None
        if got is None and es:
            got = ({}, [], [], [], False, 'espn')
            log(f'players {label_of(y)}: no Understat data, so lineups and stats come from ESPN only (no shot maps)')
        if got is None and os.path.exists(os.path.join(SEED_DIR, f'{key}.json')):
            *got, seed = season_from_seed(os.path.join(SEED_DIR, f'{key}.json'), tmap); got = tuple(got)
            log(f'players {label_of(y)}: using the hand-collected season file ({seed.get("source", "")})')
        if got is None:
            log(f'players {label_of(y)}: no player data (Understat unreachable and not in the archive)')
            continue
        P, matches, shots, apps, has_min, src = got
        if src == 'archive' and not has_min and apply_aggregates(P, load_agg(), start):
            has_min = True; src = 'archive+totals'
        used_espn = False
        if es:
            apps, from_espn = merge_espn(P, matches, apps, es, tmap)
            used_espn = True; has_min = has_min or from_espn
        out[key] = encode_players(P, matches, shots, apps, has_min, src, used_espn, seed)
        log(f'players {label_of(y)}: {len(out[key]["players"])} players, {out[key]["nShots"]} shots, from {src}'
            f'{" plus ESPN lineups" if used_espn else ""}{"" if has_min else (" (season totals only)" if src == "seed" else " (shots only, no minutes)")}')
    if tmap.unknown:
        log(f'  Understat team names not matched to static/teams.json (add them under "us"): {sorted(tmap.unknown)}')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--raw', default=os.path.join(HERE, 'raw'))
    ap.add_argument('--out', default=os.path.join(HERE, 'docs', 'index.html'))
    ap.add_argument('--current', type=int, default=None, help='end year of the season in progress, e.g. 2027 for 2026-27')
    ap.add_argument('--seasons', type=int, default=N_SEASONS)
    ap.add_argument('--sims', type=int, default=20000)
    ap.add_argument('--offline', action='store_true', help='use only files already in --raw')
    ap.add_argument('--no-understat', action='store_true', help='skip live Understat requests (the archive is still used)')
    ap.add_argument('--no-players', action='store_true', help='leave out player data entirely')
    ap.add_argument('--no-espn', action='store_true', help='skip ESPN (no same-night results, minutes only from Understat)')
    ap.add_argument('--espn-seasons', type=int, default=2, help='how many of the most recent seasons to fetch from ESPN (default 2)')
    args = ap.parse_args()
    current = args.current or default_current()
    os.makedirs(args.raw, exist_ok=True)

    teams_path = next((c for c in (os.path.join(HERE, 'static', 'teams.json'), os.path.join(HERE, 'teams.json')) if os.path.exists(c)), None)
    if teams_path:
        teams_meta = {k: v for k, v in json.load(open(teams_path, encoding='utf-8')).items() if not k.startswith('_')}
    else:
        log('WARNING: static/teams.json is missing, so every club gets a default name and a grey colour. '
            'Add the file at static/teams.json (the folder matters) to fix this.')
        teams_meta = {}
    of2fd = {alias: k for k, v in teams_meta.items() for alias in v.get('of', [])}
    tmap = team_mapper(teams_meta)
    espn_by_year = {}

    seasons, hist = {}, []
    years = list(range(current - args.seasons + 1, current + 1))
    for y in years:
        path = fetch_season(args.raw, y, current, args.offline)
        if not path:
            if y == current:
                sys.exit(f'no data for the current season {label_of(y)}; is it published yet? (try --current {y - 1})')
            log(f'  skipping {label_of(y)}: no data'); continue
        df = read_csv(path)
        if not args.no_espn and y > current - args.espn_seasons:
            es = fetch_espn(args.raw, y, current, args.offline)
            if es:
                espn_by_year[y] = es
                df = add_espn_rows(df, es, tmap)
        sp = fetch_schedule(args.raw, y, current, args.offline)
        sched, unknown = load_schedule(sp, of2fd)
        if unknown:
            log(f'  {label_of(y)}: openfootball names not in static/teams.json (add them under "of"): {sorted(unknown)}')
        rows, has = season_matches(df, sched)
        if not rows:
            log(f'  skipping {label_of(y)}: file has no finished matches'); continue
        teams = sorted({m['h'] for m in rows} | {m['a'] for m in rows})
        if y == current and sp:  # promoted teams that have not played yet still appear in the fixture list
            teams = sorted(set(teams) | {t for pair in sched for t in pair})
        for t in teams:
            if t not in teams_meta:
                log(f'  note: {t} is not in static/teams.json, so it gets a default name and colour')
                teams_meta[t] = {'name': t, 'short': re.sub(r'[^A-Z]', '', t.upper())[:3] or t[:3].upper(),
                                 'c1': '#5B6B7A', 'c2': '#FFFFFF'}
        key = code_of(y)
        seasons[key] = {'label': label_of(y), 'year': y, 'teams': teams, 'has': has, 'complete': len(rows) >= 380,
                        'rounds': max(m['round'] for m in rows), 'matches': rows}
        if y != current:
            hist.append((key, seasons[key]))
        log(f'{label_of(y)}: {len(rows)} matches, {len(teams)} teams, xG {"yes" if has["xg"] else "no"}, '
            f'odds {"yes" if has["odds"] else "no"}, matchdays from {"openfootball" if sched else "game order"}')

    if code_of(current) not in seasons:
        if args.current:
            sys.exit(f'no finished matches yet for {label_of(current)}')
        log(f'{label_of(current)} has no finished matches yet, so the page shows {label_of(current - 1)} as the latest season')
        current -= 1
        hist = [h for h in hist if h[0] != code_of(current)]
    cur = seasons[code_of(current)]
    ms = cur['matches']
    prev = seasons.get(code_of(current - 1))
    log(f'fitting ratings and simulating {args.sims} seasons ...')
    proj = project(ms, cur['teams'], args.sims, cur['has']['xg'], start=starting_ratings(prev, cur['teams']))
    proj['startFrom'] = prev['label'] if prev else None
    proj['backtest'] = backtest(hist, len(ms))
    played = {(m['h'], m['a']) for m in ms}
    proj['upcoming'] = upcoming(fetch_schedule(args.raw, current, current, True), of2fd, played,
                                proj['ratings'], proj['muH'], proj['muA'], dt.date.today())

    players = {}
    if not args.no_players:
        players = build_players(args.raw, [seasons[k]['year'] for k in sorted(seasons)], current, args.offline, tmap,
                                live=not args.no_understat, espn_by_year=espn_by_year)

    payload = {
        'current': code_of(current), 'asof': ms[-1]['date'], 'built': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'cols': COLS, 'teams': {k: {kk: v[kk] for kk in ('name', 'short', 'c1', 'c2')} for k, v in teams_meta.items()},
        'seasons': {k: {**{kk: v[kk] for kk in ('label', 'year', 'teams', 'has', 'complete', 'rounds')},
                        'm': [[m[c] for c in COLS] for m in v['matches']]} for k, v in seasons.items()},
        'proj': proj, 'players': players,
        'shotCodes': {'res': RES, 'sit': SIT, 'typ': TYP, 'last': LAST, 'fields': SHOT_FIELDS, 'appFields': APP_FIELDS, 'extra': PLAYER_EXTRA},
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    if '/*DATA*/' not in tpl:
        sys.exit('template.html is missing the /*DATA*/ placeholder')
    html = tpl.replace('/*DATA*/', blob)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(html)
    log(f'wrote {args.out} ({len(html.encode("utf-8")) / 1e6:.2f} MB), data through {payload["asof"]}')


if __name__ == '__main__':
    main()
