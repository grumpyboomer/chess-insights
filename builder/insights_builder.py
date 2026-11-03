"""Chess Insights Builder.

Run it (double-click "Chess Insights Builder.bat" on Windows, or `python insights_builder.py`),
type any chess.com and/or Lichess username in the page that opens, and it downloads the games,
analyses every position with native Stockfish, and builds the interactive Insights report.

Standard library only. The official Stockfish build is downloaded from GitHub on first run.
"""
import concurrent.futures as cf
import io, json, math, os, platform, re, shutil, socketserver, subprocess, sys, tarfile, threading, time, traceback, urllib.parse, urllib.request, webbrowser, zipfile
from http.server import SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline  # noqa: E402

ENGINE_DIR = os.path.join(HERE, 'engine')
DATA_DIR = os.path.join(HERE, 'data')
REPORTS_DIR = os.path.join(HERE, 'reports')
PORT = 8765
UA = 'ChessInsightsBuilder/1.0 (personal game analysis)'
START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'
PRESETS = {  # depth for every position (with top-2 moves), depth for the verification pass
    'fast': (12, 300_000), 'standard': (14, 1_000_000), 'deep': (14, 3_000_000), 'max': (16, 8_000_000),
}
JOB = {'state': 'idle', 'log': [], 'progress': 0.0, 'stage': '', 'report': None, 'error': None}
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        JOB['log'].append(time.strftime('%H:%M:%S ') + msg)
        JOB['log'] = JOB['log'][-300:]
    print(msg, flush=True)


def stage(name, p=None):
    with LOCK:
        JOB['stage'] = name
        if p is not None:
            JOB['progress'] = p


# ---------------------------------------------------------------- HTTP helpers
def http(url, data=None, headers=None, timeout=60, tries=5):
    h = {'User-Agent': UA}
    h.update(headers or {})
    if 'api.github.com' in url and os.environ.get('GITHUB_TOKEN'):  # online runs (GitHub Actions): avoid the anonymous rate limit
        h['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if e.code == 429 or e.code >= 500:
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            raise
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError('Gave up on ' + url)


def jget(url, **kw):
    return json.loads(http(url, **kw).decode('utf-8'))


# ---------------------------------------------------------------- engine
def engine_path():
    if os.path.isdir(ENGINE_DIR):
        for root, _, files in os.walk(ENGINE_DIR):
            for f in files:
                if f.lower().startswith('stockfish') and (f.lower().endswith('.exe') or (os.name != 'nt' and '.' not in f)):
                    return os.path.join(root, f)
    return None


def engine_works(path):
    try:
        p = subprocess.run([path], input='uci\nquit\n', capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return 'uciok' in p.stdout
    except Exception:
        return False


def install_engine():
    path = engine_path()
    if path and engine_works(path):
        return path
    stage('Downloading Stockfish (official release)')
    rel = jget('https://api.github.com/repos/official-stockfish/Stockfish/releases/latest')
    plats = ['windows'] if os.name == 'nt' else (['macos'] if sys.platform == 'darwin' else ['linux', 'ubuntu'])  # Stockfish 19 names Linux builds 'linux', older ones 'ubuntu'
    arm = platform.machine().lower() in ('arm64', 'aarch64')
    prefs = (['arm64-universal', 'arm64', 'm1-apple-silicon', 'apple-silicon'] if arm else []) + ['x86-64-universal', 'universal', 'x86-64-avx2', 'x86-64-bmi2', 'x86-64-sse41-popcnt', 'x86-64']
    assets = rel.get('assets', [])
    for pref in prefs:
        for plat, a in ((pl, a) for pl in plats for a in assets):
            n = a['name']
            if plat in n and n.split(plat + '-')[-1].split('.')[0] == pref and (n.endswith('.zip') or n.endswith('.tar') or n.endswith('.tar.gz')):
                log(f'Downloading {n} ({a["size"] // 1_000_000} MB) from the official Stockfish release {rel.get("tag_name")}')
                blob = http(a['browser_download_url'], timeout=300)
                shutil.rmtree(ENGINE_DIR, ignore_errors=True); os.makedirs(ENGINE_DIR, exist_ok=True)
                if n.endswith('.zip'):
                    zipfile.ZipFile(io.BytesIO(blob)).extractall(ENGINE_DIR)
                else:
                    tarfile.open(fileobj=io.BytesIO(blob)).extractall(ENGINE_DIR)
                path = engine_path()
                if path:
                    if os.name != 'nt':
                        os.chmod(path, 0o755)
                    if engine_works(path):
                        log('Stockfish ready: ' + os.path.basename(path))
                        return path
                log(f'{n} does not run on this CPU, trying the next build')
    raise RuntimeError('Could not install a Stockfish build that runs on this computer. Release assets: ' + ', '.join(a['name'] for a in assets))


def full_speed(handle=None):
    """Windows 11 puts windowless background processes into 'efficiency mode' (EcoQoS): lower clock
    speeds and the slower cores. Opt this process (or a child, by handle) out, so Stockfish runs at full speed."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes

        class PPTS(ctypes.Structure):  # PROCESS_POWER_THROTTLING_STATE
            _fields_ = [('Version', wintypes.ULONG), ('ControlMask', wintypes.ULONG), ('StateMask', wintypes.ULONG)]
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        st = PPTS(1, 0x1, 0x0)  # control EXECUTION_SPEED, state 0 = never throttle
        h = handle if handle is not None else k32.GetCurrentProcess()
        k32.SetProcessInformation(wintypes.HANDLE(int(h)), 4, ctypes.byref(st), ctypes.sizeof(st))  # 4 = ProcessPowerThrottling
    except Exception:
        pass


class Engine:
    def __init__(self, path, hash_mb=64):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        full_speed(getattr(self.p, '_handle', None)) if os.name == 'nt' else None
        self.send('uci'); self.wait('uciok')
        self.send('setoption name Threads value 1'); self.send(f'setoption name Hash value {hash_mb}'); self.send('setoption name MultiPV value 2')
        self.send('isready'); self.wait('readyok')

    def send(self, s):
        self.p.stdin.write(s + '\n'); self.p.stdin.flush()

    def wait(self, token):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError('Stockfish stopped unexpectedly')
            if line.strip() == token:
                return

    def new_game(self):
        self.send('ucinewgame'); self.send('isready'); self.wait('readyok')

    nodes_total = 0

    def analyse(self, moves, depth=None, nodes=None):
        """Returns (best_score, second_score, best_move) from the side to move's view."""
        self.send('position startpos' + (' moves ' + ' '.join(moves) if moves else ''))
        self.send(f'go nodes {int(nodes)}' if nodes else f'go depth {depth} movetime 60000')
        pv = {}; last_nodes = 0
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError('Stockfish stopped unexpectedly')
            if line.startswith('info') and ' score ' in line and 'bound' not in line:
                m = re.search(r' multipv (\d+)', line)
                pv[m.group(1) if m else '1'] = line
                mn = re.search(r' nodes (\d+)', line)
                if mn: last_nodes = int(mn.group(1))
            elif line.startswith('bestmove'):
                Engine.nodes_total += last_nodes
                return score_of(pv.get('1')), score_of(pv.get('2')), line.split()[1]

    def close(self):
        try:
            self.send('quit'); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def score_of(info):
    if not info:
        return None
    m = re.search(r'score (cp|mate) (-?\d+)', info)
    if not m:
        return None
    v = int(m.group(2))
    if m.group(1) == 'mate':
        v = -10000 if v == 0 else (10000 - v if v > 0 else -10000 - v)
    return v


def white_pov(v, k):
    return None if v is None else (v if k % 2 == 0 else -v)


def base_pass(eng, uci, depth):
    """Every position at a fixed depth, with the top 2 moves (needed for Great moves)."""
    eng.new_game()
    n = len(uci)
    e, b, mpv = [None] * (n + 1), [None] * (n + 1), {}
    for k in range(n + 1):
        s1, s2, bm = eng.analyse(uci[:k], depth=depth)
        e[k], b[k] = white_pov(s1, k), bm
        if k < n and s2 is not None:
            mpv[str(k)] = [e[k], white_pov(s2, k)]
    return {'e': e, 'b': b, 'uci': uci, 'mpv': mpv, 'bd': depth, 'refined': 0}


def borderline(rec):
    """Positions whose move class could change with a deeper search: near the
    Inaccuracy/Mistake/Blunder/Miss thresholds, or a possible only-move (Great)."""
    e, mpv, n = rec['e'], rec.get('mpv') or {}, len(rec['uci'])
    wp = pipeline.wp
    out = set()
    for k in range(n):
        if e[k] is None or e[k + 1] is None:
            continue
        sign = 1 if k % 2 == 0 else -1
        before, after = wp(sign * e[k]), wp(sign * e[k + 1])
        if before > 98.5 and after > 98.5 or before < 1.5 and after < 1.5:
            continue  # completely decided either way
        drop = before - after
        mp = mpv.get(str(k))
        gap = (wp(sign * mp[0]) - wp(sign * mp[1])) if mp and mp[0] is not None and mp[1] is not None else None
        if 3 <= drop <= 30 or (drop < 3 and gap is not None and 6 <= gap <= 18):
            out.update((k, k + 1))
    return sorted(out)


CACHE_LOCK = threading.Lock()  # held while the cache is saved or changed, so a save never sees it half-updated


def refine_positions(eng, rec, positions, nodes):
    eng.new_game()
    uci, n = rec['uci'], len(rec['uci'])
    upd = []
    for k in positions:
        s1, s2, bm = eng.analyse(uci[:k], nodes=nodes)
        if s1 is not None:
            upd.append((k, white_pov(s1, k), bm, white_pov(s2, k) if (k < n and s2 is not None) else None))
    with CACHE_LOCK:
        rec.setdefault('mpv', {})
        for k, ev, bm, ev2 in upd:
            rec['e'][k], rec['b'][k] = ev, bm
            if ev2 is not None:
                rec['mpv'][str(k)] = [ev, ev2]
        rec['refined'] = nodes
    return rec


def save_json(obj, path):
    """Write to a temporary file first, so a crash mid-save never leaves a broken file."""
    with CACHE_LOCK:
        text = json.dumps(obj)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def load_seed(jobs):
    """Reuse analysis already done in the browser (an *_engine_analysis_full.json export),
    found next to the builder folder or in Downloads."""
    found = {}
    cands = []
    for d in (os.path.dirname(HERE), os.path.join(os.path.expanduser('~'), 'Downloads')):
        try:
            cands += [os.path.join(d, f) for f in os.listdir(d) if f.endswith('_engine_analysis_full.json')]
        except Exception:
            pass
    want = dict(jobs)
    for c in cands:
        try:
            A = json.load(open(c))
        except Exception:
            continue
        mpv = A.get('mpv') or {}
        for gid, r in (A.get('analysis') or {}).items():
            k = 'cc:' + gid
            if k in want and r.get('uci') == want[k] and len(r.get('e') or []) == len(r['uci']) + 1:
                found[k] = {'e': r['e'], 'b': r['b'], 'uci': r['uci'], 'mpv': mpv.get(gid) or {}, 'bd': 14, 'refined': 0}
        for gid, r in ((A.get('lichess') or {}).get('analysis') or {}).items():
            k = 'li:' + gid
            if k in want and r.get('uci') == want[k] and len(r.get('e') or []) == len(r['uci']) + 1:
                found[k] = {'e': r['e'], 'b': r['b'], 'uci': r['uci'], 'mpv': r.get('mpv') or {}, 'bd': 14, 'refined': 0}
        if found:
            log(f'Reusing earlier browser analysis for {len(found)} games from {os.path.basename(c)}')
    return found


def run_pool(n_eng, path, items, fn, label, total_units, unit_of):
    """Run fn(eng, item) over items on n_eng engines, with an ETA from measured speed."""
    it = iter(items); qlock = threading.Lock(); done = [0]; t0 = time.time(); hist = [(t0, 0)]
    def worker():
        eng = Engine(path)
        try:
            while True:
                with qlock:
                    x = next(it, None)
                if x is None:
                    return
                fn(eng, x)
                with qlock:
                    done[0] += unit_of(x)
                    now = time.time(); el = now - t0
                    hist.append((now, done[0]))
                    while len(hist) > 2 and now - hist[1][0] > 600:
                        hist.pop(0)  # keep about the last 10 minutes
                    if el > 20:
                        t_a, d_a = hist[0]
                        rate = (done[0] - d_a) / max(now - t_a, 1e-9)
                        left = (total_units - done[0]) / max(rate, 1e-9)
                        stage(f'{label} · {done[0]:,}/{total_units:,} positions · about {int(left // 60)} min left', done[0] / max(1, total_units))
        finally:
            eng.close()
    with cf.ThreadPoolExecutor(n_eng) as ex:
        for f in [ex.submit(worker) for _ in range(n_eng)]:
            f.result()
    el = time.time() - t0
    log(f'{label}: {done[0]:,} positions in {int(el // 60)} min {int(el % 60)} s ({Engine.nodes_total / max(el, 1) / 1e6:.1f} M nodes/s total)')
    Engine.nodes_total = 0


# ---------------------------------------------------------------- fetching
DRAW = {'agreed', 'repetition', 'stalemate', 'insufficient', '50move', 'timevsinsufficient'}
STOP = {'Defense', 'Opening', 'Gambit', 'Game', 'Attack', 'System', 'Countergambit'}


def hdr(pgn, k):
    m = re.search(r'\[' + k + r' "([^"]*)"\]', pgn or '')
    return m.group(1) if m else ''


def fam(u):
    if not u:
        return 'Unknown'
    slug = urllib.parse.unquote(u.split('/openings/')[-1])
    words = [w for w in re.split(r'-(?=\d)|\.\.\.', slug)[0].split('-') if w]
    out = []
    for w in words:
        out.append(w)
        if w in STOP or len(out) >= 4:
            break
    return ' '.join(out) or 'Unknown'


def fetch_chesscom(user, since_ts, max_games, countries):
    u = user.strip().lower()
    stage('Downloading chess.com games')
    arch = jget(f'https://api.chess.com/pub/player/{urllib.parse.quote(u)}/games/archives')['archives']
    games = []
    for i, a in enumerate(reversed(arch)):
        month = jget(a)['games']
        games.extend(month)
        log(f'chess.com: {a.rsplit("/", 2)[-2]}/{a.rsplit("/", 1)[-1]} · {len(month)} games')
        if since_ts and month and min(g.get('end_time', 0) for g in month) < since_ts:
            break
        if max_games and len(games) >= max_games * 1.2:
            break
    games.sort(key=lambda g: g.get('end_time', 0))
    if since_ts:
        games = [g for g in games if g.get('end_time', 0) >= since_ts]
    if max_games:
        games = games[-max_games:]
    tuples, meta = [], []
    for g in games:
        me_w = g['white']['username'].lower() == u
        me, op = (g['white'], g['black']) if me_w else (g['black'], g['white'])
        res = 'W' if me['result'] == 'win' else 'D' if me['result'] in DRAW else 'L'
        acc = (g.get('accuracies') or {}).get('white' if me_w else 'black')
        gid = g['url'].rstrip('/').split('/')[-1]
        tuples.append([gid, g.get('tcn', ''), g['time_class'], g['rules'], 'w' if me_w else 'b', res, op['result'] if res == 'W' else me['result'],
                       me.get('rating'), op.get('rating'), g.get('end_time', 0), fam(hdr(g.get('pgn'), 'ECOUrl')), hdr(g.get('pgn'), 'TimeControl'),
                       acc, g.get('initial_setup', ''), g['url']])
        clocks = []
        for m in re.finditer(r'%clk (\d+):(\d+):(\d+(?:\.\d+)?)', g.get('pgn') or ''):
            clocks.append(round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 1))
        meta.append({'id': gid, 'rated': g.get('rated', True), 'opp': op['username'], 'eco': hdr(g.get('pgn'), 'ECO'), 'ecoUrl': hdr(g.get('pgn'), 'ECOUrl'),
                     'termination': hdr(g.get('pgn'), 'Termination'), 'clocks': clocks, 'tc': hdr(g.get('pgn'), 'TimeControl')})
    log(f'chess.com: {len(tuples)} games for {user}')
    try:
        stats = jget(f'https://api.chess.com/pub/player/{urllib.parse.quote(u)}/stats')
    except Exception:
        stats = {}
    cpath = os.path.join(DATA_DIR, 'chesscom_countries.json')
    try:
        known = json.load(open(cpath))
    except Exception:
        known = {}
    cty = {}
    if countries:
        allnames = sorted({m['opp'].lower() for m in meta})
        cty = {n: known[n] for n in allnames if n in known}
        names = [n for n in allnames if n not in known]
        stage(f'Looking up {len(names)} chess.com opponents\' countries')

        def one(n):
            try:
                j = jget(f'https://api.chess.com/pub/player/{urllib.parse.quote(n)}')
                return n, (j.get('country') or '').rsplit('/', 1)[-1] or None
            except Exception:
                return n, None
        with cf.ThreadPoolExecutor(6) as ex:
            for i, (n, c) in enumerate(ex.map(one, names)):
                cty[n] = c
                if i % 200 == 0:
                    log(f'countries: {i}/{len(names)}')
        known.update(cty)
        try:
            json.dump(known, open(cpath, 'w'))
        except Exception:
            pass
    return tuples, meta, cty, stats


def san_to_uci_list(sans):
    b = pipeline.Board(); out = []; col = 'w'
    for raw in sans:
        s = re.sub(r'[+#!?]', '', raw)
        if s in ('O-O', 'O-O-O'):
            r0 = 0 if col == 'w' else 7
            f, t, pr = r0 * 8 + 4, r0 * 8 + (6 if s == 'O-O' else 2), ''
        else:
            m = re.match(r'^([KQRBN])?([a-h])?([1-8])?x?([a-h][1-8])(?:=([QRBN]))?$', s)
            if not m:
                raise ValueError('bad SAN ' + raw)
            pc = m.group(1) or 'P'; t = pipeline.sq_index(m.group(4)); pr = (m.group(5) or '').lower()
            want = pc if col == 'w' else pc.lower()
            if pc == 'P':
                if m.group(2) and 'x' in s:
                    fr = t // 8 + (-1 if col == 'w' else 1)
                    cands = [fr * 8 + 'abcdefgh'.index(m.group(2))]
                else:
                    one = t + (-8 if col == 'w' else 8)
                    cands = [one if b.b[one] == want else t + (-16 if col == 'w' else 16)]
            else:
                cands = [q for q in range(64) if b.b[q] == want and t in b.attacks_from(q)]
                if m.group(2): cands = [q for q in cands if q % 8 == 'abcdefgh'.index(m.group(2))]
                if m.group(3): cands = [q for q in cands if q // 8 == int(m.group(3)) - 1]
                if len(cands) > 1:
                    keep = []
                    for q in cands:
                        bb = b.copy(); bb.apply(q, t, '')
                        if not pipeline.in_check(bb, col):
                            keep.append(q)
                    cands = keep
            if len(cands) != 1:
                raise ValueError('ambiguous ' + raw)
            f = cands[0]
        out.append(pipeline.sqname(f) + pipeline.sqname(t) + pr)
        b.apply(f, t, pr); col = 'b' if col == 'w' else 'w'
    return out


def fetch_lichess(user, since_ts, max_games, countries):
    u = user.strip()
    stage('Downloading Lichess games')
    q = {'moves': 'true', 'clocks': 'true', 'opening': 'true', 'evals': 'false', 'pgnInJson': 'false', 'tags': 'false'}
    if since_ts: q['since'] = str(int(since_ts * 1000))
    if max_games: q['max'] = str(int(max_games * 1.2) + 20)
    raw = http(f'https://lichess.org/api/games/user/{urllib.parse.quote(u)}?' + urllib.parse.urlencode(q), headers={'Accept': 'application/x-ndjson'}, timeout=600)
    allg = [json.loads(l) for l in raw.decode('utf-8').splitlines() if l.strip()]
    games = [g for g in allg if g.get('variant') == 'standard' and g.get('moves') and not g.get('initialFen') and g.get('source') not in ('ai', 'position', 'import')]
    games.sort(key=lambda g: g.get('lastMoveAt') or g['createdAt'])
    if max_games: games = games[-max_games:]
    out, bad = [], 0
    for g in games:
        try:
            g['uci'] = san_to_uci_list(g['moves'].split())
            out.append({k: g.get(k) for k in ('id', 'rated', 'speed', 'createdAt', 'lastMoveAt', 'status', 'winner', 'players', 'opening', 'clocks', 'clock', 'uci')})
        except Exception:
            bad += 1
    log(f'Lichess: {len(out)} games for {user}' + (f' ({bad} skipped)' if bad else ''))
    flags = {}
    if countries and out:
        me = u.lower()
        opps = sorted({(g['players']['white'].get('user') or {}).get('id') if (g['players']['black'].get('user') or {}).get('id') == me else (g['players']['black'].get('user') or {}).get('id') for g in out} - {None})
        for i in range(0, len(opps), 300):
            try:
                for p in json.loads(http('https://lichess.org/api/users', data=','.join(opps[i:i + 300]).encode(), headers={'Content-Type': 'text/plain'})):
                    flags[p['id']] = (p.get('profile') or {}).get('flag')
            except Exception as e:
                log('Lichess flags: ' + str(e))
    return out, flags


def map_data():
    p = os.path.join(DATA_DIR, 'mapdata.json')
    if not os.path.exists(p):
        topo = jget('https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json')
        codes = jget('https://cdn.jsdelivr.net/npm/i18n-iso-countries@7.11.3/codes.json')
        os.makedirs(DATA_DIR, exist_ok=True); json.dump({'topo': topo, 'codes': codes}, open(p, 'w'))
    return json.load(open(p))


# ---------------------------------------------------------------- the job
def slug(*names):
    return re.sub(r'[^a-z0-9_-]+', '-', '+'.join(n for n in names if n).lower()).strip('-') or 'report'


def build(opts):
    cc, li = (opts.get('chesscom') or '').strip(), (opts.get('lichess') or '').strip()
    preset = opts.get('preset', 'standard'); depth, budget = PRESETS.get(preset, PRESETS['standard'])
    if preset == 'custom':
        depth = max(8, min(30, int(opts.get('depth') or 14))); budget = max(100_000, min(100_000_000, int(float(opts.get('mnodes') or 1) * 1_000_000)))
    since_ts = None
    if opts.get('since'):
        since_ts = time.mktime(time.strptime(opts['since'], '%Y-%m-%d'))
    max_games = int(opts['max_games']) if opts.get('max_games') else None
    countries = bool(opts.get('countries', True))
    key = slug(cc and 'cc-' + cc, li and 'li-' + li)
    work = os.path.join(DATA_DIR, key); os.makedirs(work, exist_ok=True)
    cache_p = os.path.join(work, 'engine_cache.json')
    src_p = os.path.join(work, 'sources.json')
    src = None
    if opts.get('update') and os.path.exists(src_p):
        try:
            src = json.load(open(src_p))
        except Exception:
            src = None
    if src:  # update: keep the settings of the last full build, fetch only games played since
        preset, depth, budget = src['preset'], src['depth'], src['budget']
        since_ts, max_games = None, None
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}

    tuples, meta, cty, stats, lgames, flags = [], [], {}, {}, [], {}
    if src:
        tuples, meta, cty, stats = src['tuples'], src['meta'], src.get('cty') or {}, src.get('stats') or {}
        lgames, flags = src['lgames'], src.get('flags') or {}
        added = 0
        if cc:
            last = max((t[9] or 0 for t in tuples), default=0)
            nt, nm, ncty, nst = fetch_chesscom(cc, last + 1, None, countries)
            have = {t[0] for t in tuples}
            for t, m in zip(nt, nm):
                if t[0] not in have:
                    tuples.append(t); meta.append(m); added += 1
            cty.update(ncty)
            if nst: stats = nst
        if li:
            last = max((g.get('createdAt') or 0 for g in lgames), default=0)
            nl, nf = fetch_lichess(li, (last + 1) / 1000, None, countries)
            have = {g['id'] for g in lgames}
            for g in nl:
                if g['id'] not in have:
                    lgames.append(g); added += 1
            flags.update(nf)
        tuples.sort(key=lambda t: t[9] or 0); lgames.sort(key=lambda g: g.get('lastMoveAt') or g['createdAt'])
        rep_dir = os.path.join(REPORTS_DIR, key)
        if not added and os.path.exists(os.path.join(rep_dir, 'app_data.json')) and not opts.get('force'):
            log('No new games since the last update.')
            return key
        log(f'{added} new game{"s" if added != 1 else ""} since the last update')
    else:
        if cc:
            tuples, meta, cty, stats = fetch_chesscom(cc, since_ts, max_games, countries)
        if li:
            lgames, flags = fetch_lichess(li, since_ts, max_games, countries)
    if not tuples and not lgames:
        raise RuntimeError('No games found. Check the usernames.')
    try:
        json.dump({'preset': preset, 'depth': depth, 'budget': budget, 'tuples': tuples, 'meta': meta, 'cty': cty, 'stats': stats,
                   'lgames': lgames, 'flags': flags, 'saved': time.time()}, open(src_p, 'w'))
    except Exception as e:
        log('Could not save the game list: ' + str(e))

    jobs = []
    for t in tuples:
        if t[3] == 'chess' and (t[13] or START).startswith(START) and t[1]:
            mv = pipeline.decode_tcn(t[1])
            if mv:
                jobs.append(('cc:' + t[0], [pipeline.sqname(f) + pipeline.sqname(tt) + pr for f, tt, pr in mv]))
    for g in lgames:
        jobs.append(('li:' + g['id'], g['uci']))
    for k, v in load_seed([j for j in jobs if j[0] not in cache]).items():
        cache[k] = v
    path = install_engine()
    n_eng = max(1, (os.cpu_count() or 2) - 2) if not opts.get('engines') else int(opts['engines'])
    save = lambda: save_json(cache, cache_p)
    # phase 1: every position, for games not analysed yet (or analysed shallower)
    todo = [j for j in jobs if j[0] not in cache or cache[j[0]].get('bd', 0) < depth]
    if todo:
        log(f'Pass 1: {len(todo)} games, every position at depth {depth} with the top 2 moves, on {n_eng} Stockfish processes')
        cnt = [0]
        def p1(eng, j):
            rec = base_pass(eng, j[1], depth)
            with CACHE_LOCK:
                cache[j[0]] = rec; cnt[0] += 1; due = cnt[0] % 100 == 0
            if due: save()
        run_pool(n_eng, path, todo, p1, 'Pass 1', sum(len(j[1]) + 1 for j in todo), lambda j: len(j[1]) + 1)
        save()
    # phase 2: borderline positions re-checked with a much bigger search
    ref = []
    for gid, _ in jobs:
        r = cache.get(gid)
        if r and r.get('refined', 0) < budget:
            pos = borderline(r)
            if pos: ref.append((gid, pos))
            else: r['refined'] = budget
    if ref:
        tot = sum(len(p) for _, p in ref)
        log(f'Pass 2: re-checking {tot:,} borderline positions in {len(ref)} games with {budget / 1e6:g} million nodes each')
        cnt = [0]
        def p2(eng, x):
            refine_positions(eng, cache[x[0]], x[1], budget)
            with CACHE_LOCK:
                cnt[0] += 1; due = cnt[0] % 50 == 0
            if due: save()
        run_pool(n_eng, path, ref, p2, 'Pass 2', tot, lambda x: len(x[1]))
    save()

    stage('Building insights', 1.0)
    A = {'games': tuples, 'analysis': {k[3:]: v for k, v in cache.items() if k.startswith('cc:')},
         'mpv': {k[3:]: v['mpv'] for k, v in cache.items() if k.startswith('cc:')},
         'lichess': {'user': li, 'flags': flags, 'games': lgames, 'analysis': {k[3:]: v for k, v in cache.items() if k.startswith('li:')}}}
    M = {'meta': meta, 'countries': cty, 'stats': stats}
    ap, mp = os.path.join(work, 'analysis.json'), os.path.join(work, 'meta.json')
    json.dump(A, open(ap, 'w')); json.dump(M, open(mp, 'w'))
    import importlib; importlib.reload(pipeline)  # pick up pipeline.py changes without restarting the builder
    games, moves_rows, events, st, cat = pipeline.run(ap, mp, work)
    rep = os.path.join(REPORTS_DIR, key); os.makedirs(rep, exist_ok=True)
    pipeline.write_outputs(games, moves_rows, events, st, rep)
    ad = pipeline.app_data(games, events, st); ad['catalog'] = cat
    ad['player'] = {'chesscom': cc or None, 'lichess': li or None, 'engine': f'Stockfish 19: every position at depth {depth} with the top 2 moves, then every borderline position (where a move\'s class could change) re-searched with {budget / 1e6:g} million nodes', 'built': time.strftime('%Y-%m-%d %H:%M')}
    try:
        ad['map'] = map_data()
    except Exception as e:
        log('Map data unavailable: ' + str(e))
    json.dump(ad, open(os.path.join(rep, 'app_data.json'), 'w'), separators=(',', ':'))
    json.dump(pipeline.detail_data(games), open(os.path.join(rep, 'detail.json'), 'w'), separators=(',', ':'))
    shutil.copy(os.path.join(HERE, 'app_template.html'), os.path.join(rep, 'index.html'))
    if os.path.exists(os.path.join(HERE, 'ux_template.html')):  # the UX version, same data
        shutil.copy(os.path.join(HERE, 'ux_template.html'), os.path.join(rep, 'ux.html'))
    for f in os.listdir(rep):
        if f.startswith('abdh2005_'):
            os.replace(os.path.join(rep, f), os.path.join(rep, f.replace('abdh2005', key)))
    log(f'Report ready: {len(games)} games, {len(moves_rows)} moves')
    return key


def run_job(opts):
    with LOCK:
        JOB.update(state='running', log=[], progress=0.0, stage='Starting', report=None, error=None)
    try:
        key = build(opts)
        with LOCK:
            JOB.update(state='done', report=f'/reports/{key}/', stage='Done', progress=1.0)
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            JOB.update(state='error', error=str(e), stage='Stopped')
        log('Error: ' + str(e))


# ---------------------------------------------------------------- auto-update
AUTO_P = os.path.join(DATA_DIR, 'autoupdate.json')
AUTO_DEFAULT = {'enabled': False, 'chesscom': '', 'lichess': '', 'every': 30, 'last_check': None, 'last_result': '', 'next_ts': 0}
STARTUP_DIR = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
STARTUP_F = os.path.join(STARTUP_DIR, 'Chess Insights Builder (auto-update).bat')


def auto_get():
    a = dict(AUTO_DEFAULT)
    try:
        a.update(json.load(open(AUTO_P)))
    except Exception:
        pass
    a['startup'] = os.path.exists(STARTUP_F)
    return a


def auto_set(a):
    a = {k: v for k, v in a.items() if k in AUTO_DEFAULT}
    os.makedirs(DATA_DIR, exist_ok=True); json.dump(a, open(AUTO_P, 'w'))


def set_startup(on):
    """Start the builder silently (no window, no browser) when you sign in to Windows."""
    if not os.environ.get('APPDATA'):
        raise RuntimeError('Starting with Windows is only available on Windows.')
    if not on:
        if os.path.exists(STARTUP_F): os.remove(STARTUP_F)
        return
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    exe = pyw if os.path.exists(pyw) else exe
    with open(STARTUP_F, 'w', newline='\r\n') as f:
        f.write('@echo off\n')
        f.write(f'start "" "{exe}" "{os.path.join(HERE, "insights_builder.py")}" --background\n')


def auto_loop():
    a = auto_get(); a['next_ts'] = time.time() + 60; auto_set(a)  # first check a minute after starting
    while True:
        time.sleep(20)
        try:
            a = auto_get()
            if not a['enabled'] or not (a['chesscom'] or a['lichess']) or time.time() < (a.get('next_ts') or 0):
                continue
            with LOCK:
                busy = JOB['state'] == 'running'
            if busy:
                continue
            a['next_ts'] = time.time() + max(5, int(a.get('every') or 30)) * 60; auto_set(a)
            run_job({'chesscom': a['chesscom'], 'lichess': a['lichess'], 'update': True, 'preset': 'standard'})
            a = auto_get()
            with LOCK:
                a['last_result'] = JOB.get('error') or ([l for l in JOB['log'] if 'new game' in l or 'Report ready' in l] or [JOB.get('stage') or ''])[-1]
            a['last_check'] = time.strftime('%Y-%m-%d %H:%M'); auto_set(a)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- web UI
UI = open(os.path.join(HERE, 'builder_ui.html'), encoding='utf-8').read() if os.path.exists(os.path.join(HERE, 'builder_ui.html')) else '<h1>builder_ui.html missing</h1>'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        global LAST_REQ
        LAST_REQ = time.time()
        p = urllib.parse.urlparse(self.path).path
        if p in ('/', '/index.html'):
            b = UI.encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p == '/api/auto':
            return self._json(auto_get())
        if p == '/api/status':
            with LOCK:
                return self._json(dict(JOB))
        if p == '/api/reports':
            out = []
            if os.path.isdir(REPORTS_DIR):
                for k in sorted(os.listdir(REPORTS_DIR)):
                    f = os.path.join(REPORTS_DIR, k, 'app_data.json')
                    if os.path.exists(f):
                        out.append({'key': k, 'url': f'/reports/{k}/', 'built': time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(f)))})
            return self._json(out)
        if p.startswith('/reports/'):
            rel = urllib.parse.unquote(p[len('/reports/'):])
            if rel.endswith('/') or rel == '' or '.' not in rel.rsplit('/', 1)[-1]:
                rel = rel.rstrip('/') + '/index.html'
            full = os.path.realpath(os.path.join(REPORTS_DIR, rel))
            if not full.startswith(os.path.realpath(REPORTS_DIR)) or not os.path.isfile(full):
                return self.send_error(404)
            ctype = {'.html': 'text/html; charset=utf-8', '.json': 'application/json', '.csv': 'text/csv', '.zip': 'application/zip', '.db': 'application/octet-stream'}.get(os.path.splitext(full)[1], 'application/octet-stream')
            with open(full, 'rb') as fh:
                b = fh.read()
            self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        self.send_error(404)

    def do_POST(self):
        global LAST_REQ
        LAST_REQ = time.time()
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get('Content-Length', 0)); opts = json.loads(self.rfile.read(n) or b'{}')
        if path == '/api/auto':
            a = auto_get()
            for k in ('enabled', 'chesscom', 'lichess', 'every'):
                if k in opts: a[k] = opts[k]
            if opts.get('check_now'): a['next_ts'] = 0
            auto_set(a)
            if 'startup' in opts and bool(opts['startup']) != a['startup']:
                try:
                    set_startup(bool(opts['startup']))
                except Exception as e:
                    return self._json({'error': str(e)}, 400)
            return self._json(auto_get())
        if path == '/api/update':  # from the report's "Check for new games" button
            opts = {'chesscom': opts.get('chesscom') or '', 'lichess': opts.get('lichess') or '', 'update': True}
            a = auto_get(); a['chesscom'], a['lichess'] = opts['chesscom'], opts['lichess']; auto_set(a)
        elif path != '/api/build':
            return self.send_error(404)
        with LOCK:
            busy = JOB['state'] == 'running'
        if busy:
            return self._json({'error': 'A report is already being built.'}, 409)
        threading.Thread(target=run_job, args=(opts,), daemon=True).start()
        return self._json({'ok': True})


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------- remote requests
# The online report's Update button starts a Claude run that can only touch files in this folder.
# It drops data/requests/update.json; this loop starts the update and keeps data/requests/heartbeat.json
# current (alive + job state), so that run knows when the new report is ready to publish.
REQ_DIR = os.path.join(DATA_DIR, 'requests')
LAST_REQ = time.time()


def exit_when_done(started):
    """--exit-when-done (used by "Update Chess Insights.bat"): quit once the update has finished and the
    report page has had time to reload (no page requests for 90 s). Also quits if nothing was asked of it
    for 10 minutes."""
    ran = False
    while True:
        time.sleep(10)
        with LOCK:
            st = JOB['state']
        if st == 'running':
            ran = True
            continue
        idle = time.time() - LAST_REQ
        if (ran and idle > 90) or (not ran and time.time() - started > 600 and idle > 90):
            print('Update finished: closing the builder.', flush=True)
            try:
                os.remove(os.path.join(REQ_DIR, 'heartbeat.json'))
            except OSError:
                pass
            os._exit(0)


def request_loop():
    os.makedirs(REQ_DIR, exist_ok=True)
    req_p, hb_p = os.path.join(REQ_DIR, 'update.json'), os.path.join(REQ_DIR, 'heartbeat.json')
    last = {}
    while True:
        try:
            if os.path.exists(req_p):
                try:
                    r = json.load(open(req_p))
                except Exception:
                    r = {}
                os.remove(req_p)
                with LOCK:
                    busy = JOB['state'] == 'running'
                if not busy:
                    a = auto_get()
                    opts = {'chesscom': r.get('chesscom') or a.get('chesscom') or '', 'lichess': r.get('lichess') or a.get('lichess') or '', 'update': True}
                    last = {'request_id': r.get('id'), 'started': time.time()}
                    log('Update requested from the online report')
                    threading.Thread(target=run_job, args=(opts,), daemon=True).start()
                    time.sleep(1)
            with LOCK:
                snap = {'ts': time.time(), 'state': JOB['state'], 'stage': JOB['stage'], 'progress': JOB['progress'],
                        'report': JOB['report'], 'error': JOB['error'], 'log': JOB['log'][-8:]}
            snap.update(last)
            tmp = hb_p + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(snap, f)
            os.replace(tmp, hb_p)
        except Exception as e:
            print('request loop: ' + str(e), flush=True)
        time.sleep(5)


def main():
    os.makedirs(DATA_DIR, exist_ok=True); os.makedirs(REPORTS_DIR, exist_ok=True)
    if '--cli' in sys.argv:  # python insights_builder.py --cli chesscom=NAME lichess=NAME preset=deep
        opts = dict(a.split('=', 1) for a in sys.argv[1:] if '=' in a)
        run_job(opts); print(JOB['state'], JOB.get('report') or JOB.get('error')); return
    url = f'http://127.0.0.1:{PORT}/'
    background = '--background' in sys.argv
    # --open=reports/<key>/ux.html?update=1 : open that page (even in background mode); used by "Update Chess Insights.bat"
    target = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--open=')), None)
    if target:
        url = url + target.lstrip('/')
    if background or sys.stdout is None:  # no console window: keep a log file instead
        sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, 'builder.log'), 'a', buffering=1, encoding='utf-8')
    try:
        srv = Server(('127.0.0.1', PORT), Handler)
    except OSError:  # already running (for example started with Windows): just open it
        if not background or target:
            webbrowser.open(url)
        return
    full_speed()
    try:  # automatic checks were replaced by the "Check for new games" button
        a = auto_get(); a['enabled'] = False; auto_set(a)
    except Exception:
        pass
    threading.Thread(target=request_loop, daemon=True).start()
    if '--exit-when-done' in sys.argv:
        threading.Thread(target=exit_when_done, args=(time.time(),), daemon=True).start()
    print(f'Chess Insights Builder running at {url}  (close this window to stop)', flush=True)
    if not background or target:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
