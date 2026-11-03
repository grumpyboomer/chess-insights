"""Chess insights pipeline: Stockfish evals + chess.com metadata -> SQLite, CSVs, app data JSON.
Pure Python (no external chess library)."""
import json, math, sqlite3, csv, re, sys, os
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
try:
    import wintr
except ImportError:  # run from another folder
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import wintr

T = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!?{~}(^)[_]@#$,./&-*++="
VAL = {'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9, 'k': 100}
PNAME = {'p': 'Pawn', 'n': 'Knight', 'b': 'Bishop', 'r': 'Rook', 'q': 'Queen', 'k': 'King'}
PIECES = 'pnbrqk'
RIYADH = timezone(timedelta(hours=3))
MATE = 10000
# 'wintr': every move classified with WintrChess's rules (expected-points loss, no Miss class).
# 'chesscom': chess.com-style win-chance thresholds plus Miss.
CLASSIFIER = os.environ.get('CLASSIFIER', 'wintr')
EP_K, EP_SHIFT = 0.002, 400   # expected-points curve, calibrated to chess.com reviews (see classification)


def decode_tcn(n):
    mv = []
    for i in range(0, len(n) - 1, 2):
        f, t = T.index(n[i]), T.index(n[i + 1])
        pr = ''
        if f > 63:
            return None
        if t > 63:
            pr = "qnrbkp"[(t - 64) // 3]
            t = f + (-8 if f < 16 else 8) + ((t - 1) % 3) - 1
        mv.append((f, t, pr))
    return mv


def sqname(i):
    return 'abcdefgh'[i % 8] + str(i // 8 + 1)


def color_of(p):
    return None if p is None else ('w' if p.isupper() else 'b')


KN = [(1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)]
KG = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
DIAG = [(1, 1), (1, -1), (-1, 1), (-1, -1)]
ORTH = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def on(f, r):
    return 0 <= f < 8 and 0 <= r < 8


class Board:
    def __init__(self):
        b = [None] * 64
        back = 'RNBQKBNR'
        for f in range(8):
            b[f] = back[f]; b[8 + f] = 'P'; b[48 + f] = 'p'; b[56 + f] = back[f].lower()
        self.b = b

    def copy(self):
        c = Board.__new__(Board); c.b = self.b[:]; return c

    def apply(self, f, t, pr):
        b = self.b
        p = b[f]
        info = {'piece': p.lower() if p else None, 'capture': None, 'castle': None, 'promo': pr or None}
        if p is None:
            info['bad'] = True
            return info
        low = p.lower()
        cap = b[t]
        if low == 'p' and (f % 8) != (t % 8) and b[t] is None:
            ep = t + (-8 if p == 'P' else 8)
            cap = b[ep]; b[ep] = None
        if low == 'k' and abs(f - t) == 2:
            if t > f:
                b[f + 1] = b[f + 3]; b[f + 3] = None; info['castle'] = 'O-O'
            else:
                b[f - 1] = b[f - 4]; b[f - 4] = None; info['castle'] = 'O-O-O'
        b[t] = (pr.upper() if p == 'P' else pr) if pr else p
        b[f] = None
        info['capture'] = cap.lower() if cap else None
        return info

    def attacks_from(self, s):
        """Squares attacked by the piece on s."""
        b = self.b; p = b[s]
        if p is None:
            return []
        f, r = s % 8, s // 8
        low = p.lower(); out = []
        if low == 'p':
            dr = 1 if p == 'P' else -1
            for df in (-1, 1):
                if on(f + df, r + dr): out.append((r + dr) * 8 + f + df)
        elif low == 'n':
            for df, dr in KN:
                if on(f + df, r + dr): out.append((r + dr) * 8 + f + df)
        elif low == 'k':
            for df, dr in KG:
                if on(f + df, r + dr): out.append((r + dr) * 8 + f + df)
        else:
            dirs = (DIAG if low == 'b' else ORTH if low == 'r' else DIAG + ORTH)
            for df, dr in dirs:
                ff, rr = f + df, r + dr
                while on(ff, rr):
                    q = rr * 8 + ff; out.append(q)
                    if b[q] is not None: break
                    ff += df; rr += dr
        return out

    def attackers(self, s, col):
        res = []
        for q in range(64):
            p = self.b[q]
            if p is not None and color_of(p) == col and s in self.attacks_from(q):
                res.append(q)
        return res

    def king(self, col):
        k = 'K' if col == 'w' else 'k'
        return self.b.index(k) if k in self.b else -1

    def hanging(self, s):
        """Piece on s can be won: attacked and undefended, or attacked by a cheaper piece."""
        p = self.b[s]
        if p is None or p.lower() == 'k':
            return False
        col = color_of(p); opp = 'b' if col == 'w' else 'w'
        att = self.attackers(s, opp)
        if not att:
            return False
        defs = self.attackers(s, col)
        if not defs:
            return True
        return min(VAL[self.b[a].lower()] for a in att) < VAL[p.lower()]

    def fork_by(self, s):
        """Piece on s attacks >=2 enemy targets worth more than it, or undefended, or the king."""
        p = self.b[s]
        if p is None:
            return False
        col = color_of(p); opp = 'b' if col == 'w' else 'w'
        v = VAL[p.lower()]
        tg = 0
        for q in self.attacks_from(s):
            x = self.b[q]
            if x is None or color_of(x) != opp:
                continue
            xl = x.lower()
            if xl == 'k' or VAL[xl] > v or (VAL[xl] >= 3 and not self.attackers(q, opp)):
                tg += 1
        return tg >= 2

    def pin_by(self, s):
        """Sliding piece on s pins an enemy piece against a more valuable one (or the king)."""
        p = self.b[s]
        if p is None or p.lower() not in 'brq':
            return False
        col = color_of(p); opp = 'b' if col == 'w' else 'w'
        f, r = s % 8, s // 8
        low = p.lower()
        dirs = (DIAG if low == 'b' else ORTH if low == 'r' else DIAG + ORTH)
        for df, dr in dirs:
            ff, rr = f + df, r + dr; first = None
            while on(ff, rr):
                q = rr * 8 + ff; x = self.b[q]
                if x is not None:
                    if color_of(x) != opp: break
                    if first is None:
                        first = x
                    else:
                        if (x.lower() == 'k' or VAL[x.lower()] > VAL[first.lower()]) and first.lower() != 'k':
                            return True
                        break
                ff += df; rr += dr
        return False

    def npm(self):
        return sum(VAL[p.lower()] for p in self.b if p and p.lower() in 'nbrq')


def fen_board(b):
    rows = []
    for r in range(7, -1, -1):
        row = ''; e = 0
        for f in range(8):
            p = b.b[r * 8 + f]
            if p is None:
                e += 1
            else:
                if e: row += str(e); e = 0
                row += p
        if e: row += str(e)
        rows.append(row)
    return '/'.join(rows)


def in_check(b, col):
    k = b.king(col)
    return k >= 0 and bool(b.attackers(k, 'b' if col == 'w' else 'w'))


def to_san(b, f, t, pr, mate=False):
    """SAN for a legal move on board b (position before the move)."""
    p = b.b[f]
    if p is None:
        return '?'
    col = color_of(p); low = p.lower()
    if low == 'k' and abs(f - t) == 2:
        s = 'O-O' if t > f else 'O-O-O'
    else:
        cap = b.b[t] is not None or (low == 'p' and (f % 8) != (t % 8))
        if low == 'p':
            s = ('abcdefgh'[f % 8] + 'x' if cap else '') + sqname(t) + ('=' + pr.upper() if pr else '')
        else:
            others = []
            for q in range(64):
                if q != f and b.b[q] == p and t in b.attacks_from(q):
                    bb = b.copy(); bb.apply(q, t, '')
                    if not in_check(bb, col):
                        others.append(q)
            dis = ''
            if others:
                if all(q % 8 != f % 8 for q in others): dis = 'abcdefgh'[f % 8]
                elif all(q // 8 != f // 8 for q in others): dis = str(f // 8 + 1)
                else: dis = sqname(f)
            s = low.upper() + dis + ('x' if cap else '') + sqname(t)
    nb = b.copy(); nb.apply(f, t, pr)
    if mate:
        s += '#'
    elif in_check(nb, 'b' if col == 'w' else 'w'):
        s += '+'
    return s


def wp(cp):
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


# Per-move accuracy is Lichess's formula. Averaged over a game it runs ~14 points above chess.com's
# accuracy, so game, phase, piece and move-number accuracies are rescaled to chess.com's scale:
# fitted on this player's chess.com-reviewed games from 2022 on (348 games, refit on the Stockfish 19 desktop analysis).
ACC_SCALE, ACC_SHIFT = 1.6256, -67.53


def cal_acc(mean_acc):
    return None if mean_acc is None else round(max(0.0, min(100.0, ACC_SCALE * mean_acc + ACC_SHIFT)), 2)


def cal_sum(total, n):  # a sum of per-move accuracies over n moves, rescaled the same way
    return round(ACC_SCALE * total + ACC_SHIFT * n, 1)


def move_acc(drop):
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


def book_plies(eco_url):
    """Plies in chess.com's named opening line (moves encoded at the end of the ECO URL)."""
    slug = (eco_url or '').split('/openings/')[-1]
    toks = slug.split('-')
    ply = 0; seen = False
    for t in toks:
        m = re.match(r'^(\d+)(\.\.\.|\.)(.*)$', t)
        if m:
            n = int(m.group(1)); black = m.group(2) == '...'
            ply = 2 * n if black else 2 * n - 1
            seen = True
            if not m.group(3):
                ply -= 1  # number with no move after it
        elif seen and re.match(r'^(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?)[+#]?$', t):
            ply += 1
    return ply if seen else 0


def sq_index(s):
    return 'abcdefgh'.index(s[0]) + 8 * (int(s[1]) - 1)


def norm_open(name):
    """Group opening family names the same way on both sites."""
    n = (name or 'Unknown').replace("'", '').replace('-', ' ')
    n = re.sub(r'\s+', ' ', n).strip()
    return {'Queens Pawn Game': 'Queens Pawn Opening', 'Kings Pawn Game': 'Kings Pawn Opening', 'Kings Pawn': 'Kings Pawn Opening',
            'Queens Pawn': 'Queens Pawn Opening', 'Petrovs Defense': 'Russian Game', 'Petrov Defense': 'Russian Game',
            'Giuoco Piano Game': 'Italian Game', 'Ruy Lopez Opening': 'Ruy Lopez', 'Bishops Opening': 'Bishops Opening',
            'Van t Kruijs Opening': 'Van t Kruijs Opening'}.get(n, n)


def phase_of(k, npm):
    if npm <= 26:
        return 'Endgame'
    return 'Opening' if k < 24 else 'Middlegame'


def shape_of(W):
    """Classify a game's win% curve (White POV) into one of 7 shapes."""
    if len(W) < 12:
        return 'Sudden'
    w = W[4:]
    drops = []
    for k in range(len(W) - 1):
        s = 1 if k % 2 == 0 else -1
        before = W[k] if s == 1 else 100 - W[k]
        after = W[k + 1] if s == 1 else 100 - W[k + 1]
        drops.append(before - after)
    big = sum(1 for d in drops if d >= 15)
    # lead changes between clearly winning states
    state = 0; changes = 0
    for x in w:
        st = 1 if x >= 75 else -1 if x <= 25 else 0
        if st != 0:
            if state != 0 and st != state:
                changes += 1
            state = st
    final = W[-1]
    winner = 1 if final >= 70 else -1 if final <= 30 else 0
    max_w, min_w = max(w), min(w)
    if (max_w >= 95 and winner != 1) or (min_w <= 5 and winner != -1):
        return 'Giveaway'
    if changes >= 2 or (big >= 5 and changes >= 1):
        return 'Wild'
    if changes == 1:
        return 'Sharp'
    near = sum(1 for x in w if 35 <= x <= 65) / len(w)
    if near >= 0.85 and big == 0:
        return 'Balanced'
    first_big = next((k for k, d in enumerate(drops) if d >= 15), None)
    if first_big is not None and big <= 2:
        pre = W[4:first_big + 1] or [50]
        if all(35 <= x <= 65 for x in pre):
            return 'Sudden'
    # smooth: one side reached 70 and never fell back below 55 afterwards
    for k, x in enumerate(w):
        if x >= 70 and all(y >= 55 for y in w[k:]):
            return 'Smooth'
        if x <= 30 and all(y <= 45 for y in w[k:]):
            return 'Smooth'
    return 'Intense'


CATQ = {'Superbrilliant', 'Brilliant', 'Great', 'Miss', 'Blunder', 'Mistake'}
CAT_CAP = {'Superbrilliant': 100000, 'Brilliant': 100000, 'Great': 100000, 'Miss': 400, 'Blunder': 400, 'Mistake': 300}
QCODE = {'Brilliant': '!', 'Great': 'G', 'Best': 'S', 'Excellent': 'E', 'Good': 'D', 'Book': 'K', 'Inaccuracy': 'I', 'Mistake': 'M', 'Miss': 'X', 'Blunder': 'U', 'Superbrilliant': '$'}
# Superbrilliant is appended last so the older class positions in every q array stay the same
QUAL = ['Brilliant', 'Great', 'Best', 'Excellent', 'Good', 'Book', 'Inaccuracy', 'Mistake', 'Miss', 'Blunder', 'Superbrilliant']
TACT = ['fork', 'pin', 'mate', 'freepiece']


def run(analysis_path, meta_path, outdir):
    A = json.load(open(analysis_path))
    M = json.load(open(meta_path))
    meta = {m['id']: m for m in M['meta']}
    countries = {k.lower(): v for k, v in M['countries'].items()}
    games_raw = A['games']; analysis = A['analysis']
    games = []; moves_rows = []; events = []; catalog = []
    # Book depth for opening links with no moves in them: the shortest known
    # line of the same name with moves, minus one ply.
    name_of = lambda u: re.split(r'-\d', (u or '').split('/openings/')[-1])[0]
    base_ply = {}
    for mm in M['meta']:
        bpv = book_plies(mm.get('ecoUrl', ''))
        if bpv:
            nm = name_of(mm.get('ecoUrl', ''))
            base_ply[nm] = min(base_ply.get(nm, 99), bpv)
    def book_depth(u):
        bpv = book_plies(u)
        if bpv:
            return bpv
        nm = (u or '').split('/openings/')[-1]
        cands = [v for k2, v in base_ply.items() if k2.startswith(nm)]
        return max(1, min(cands) - 1) if cands else 0
    jobs = []
    for g in games_raw:
        gid, tcn, tc, rules, col, res, how, my, opp, et, fam, tcs, cc_acc, setup, url = g
        rec = analysis.get(gid)
        m = meta.get(gid, {})
        end = datetime.fromtimestamp(et, RIYADH)
        base = {
            'id': gid, 'site': 'Chess.com', 'url': url, 'end_ts': et, 'date': end.strftime('%Y-%m-%d'), 'hour': end.hour, 'weekday': end.strftime('%a'),
            'time_class': tc, 'time_control': tcs, 'rules': rules, 'rated': bool(m.get('rated', True)), 'color': 'White' if col == 'w' else 'Black',
            'result': res, 'how': how, 'my_rating': my, 'opp_rating': opp, 'opponent': m.get('opp', ''),
            'opp_country': countries.get((m.get('opp') or '').lower()), 'opening': norm_open(fam), 'eco': m.get('eco', ''), 'eco_url': m.get('ecoUrl', ''),
            'chesscom_accuracy': cc_acc, 'analyzed': False,
        }
        if not rec or rules != 'chess':
            games.append(base); continue
        mv = [tuple(x) for x in decode_tcn(tcn)] if tcn else []
        try:
            bt, inc = (tcs.split('+') + ['0'])[:2]; bt = float(bt); inc = float(inc)
        except Exception:
            bt, inc = None, 0.0
        jobs.append((base, mv, rec['e'], rec['b'], 0 if col == 'w' else 1, m.get('clocks') or [], bt, inc,
                     book_depth(m.get('ecoUrl', '')), (A.get('mpv') or {}).get(gid) or {}))
    # ---- Lichess games ----
    L = A.get('lichess') or {}
    lme = (L.get('user') or '').lower()
    lflags = L.get('flags') or {}
    SPEED = {'ultraBullet': 'bullet', 'bullet': 'bullet', 'blitz': 'blitz', 'rapid': 'rapid', 'classical': 'rapid', 'correspondence': 'daily'}
    HOW = {'mate': 'checkmated', 'resign': 'resigned', 'outoftime': 'timeout', 'timeout': 'abandoned', 'stalemate': 'stalemate', 'draw': 'draw'}
    for lg in L.get('games', []):
        rec = (L.get('analysis') or {}).get(lg['id'])
        pw, pb = lg['players']['white'], lg['players']['black']
        col = 'w' if (pw.get('user') or {}).get('id') == lme else 'b'
        mine, theirs = (pw, pb) if col == 'w' else (pb, pw)
        win = lg.get('winner')
        res = 'D' if not win else ('W' if win[0] == col else 'L')
        et = int((lg.get('lastMoveAt') or lg['createdAt']) / 1000)
        end = datetime.fromtimestamp(et, RIYADH)
        clk = lg.get('clock') or {}
        tcs = f"{clk.get('initial')}+{clk.get('increment')}" if clk else ''
        op = (lg.get('opening') or {})
        flag = (lflags.get((theirs.get('user') or {}).get('id') or '') or '')[:2].upper()
        base = {
            'id': 'li-' + lg['id'], 'site': 'Lichess', 'url': f"https://lichess.org/{lg['id']}" + ('/black' if col == 'b' else ''), 'end_ts': et,
            'date': end.strftime('%Y-%m-%d'), 'hour': end.hour, 'weekday': end.strftime('%a'),
            'time_class': SPEED.get(lg['speed'], lg['speed']), 'time_control': tcs, 'rules': 'chess', 'rated': bool(lg.get('rated')),
            'color': 'White' if col == 'w' else 'Black', 'result': res, 'how': HOW.get(lg['status'], lg['status']),
            # Lichess gives ratings before the game plus the change; chess.com gives them after the game. Keep "after"
            # for your rating (current rating, peaks, history) and "before" for comparing with the opponent.
            'my_rating': (mine.get('rating') or 0) + (mine.get('ratingDiff') or 0) if mine.get('rating') else None,
            'my_pre': mine.get('rating'), 'prov': bool(mine.get('provisional')),
            'opp_rating': theirs.get('rating'), 'opponent': (theirs.get('user') or {}).get('name', 'Anonymous'),
            'opp_country': flag if re.match(r'^[A-Z]{2}$', flag) else None, 'opening': norm_open((op.get('name') or 'Unknown').split(':')[0]),
            'eco': op.get('eco', ''), 'eco_url': f"https://lichess.org/opening/{(op.get('name') or '').replace(' ', '_')}" if op.get('name') else '',
            'chesscom_accuracy': None, 'analyzed': False,
        }
        if not rec:
            games.append(base); continue
        mv = [(sq_index(u[:2]), sq_index(u[2:4]), u[4:]) for u in lg['uci']]
        clocks = [c / 100 for c in (lg.get('clocks') or [])]
        jobs.append((base, mv, rec['e'], rec['b'], 0 if col == 'w' else 1, clocks, float(clk['initial']) if clk else None,
                     float(clk.get('increment', 0)) if clk else 0.0, int(op.get('ply') or 0), rec.get('mpv') or {}))
    for base, mv, Eraw, B, me, clocks, bt, inc, bp, mpv in jobs:
        gid = base['id']
        E = [0 if v is None else v for v in Eraw]
        n = len(mv)
        if n == 0 or len(E) != n + 1:
            games.append(base); continue
        W = [wp(e) for e in E]
        board = Board()
        d_san, d_cls, d_best = [], [], []
        stats = {s: {'acc': [], 'cpl': [], 'q': Counter(), 'piece_n': Counter(), 'piece_acc': defaultdict(float),
                     'phase_n': Counter(), 'phase_acc': defaultdict(float), 'phase_bl': Counter(),
                     't_opp': Counter(), 't_found': Counter(), 'hang': Counter(), 'free_opp': 0, 'free_taken': 0,
                     'time': [], 'castle': None, 'castle_ply': None, 'mn_acc': defaultdict(float), 'mn_n': Counter()} for s in (0, 1)}
        prev_drop = [0.0, 0.0]
        for k, (f, t, pr) in enumerate(mv):
            s = k % 2
            sign = 1 if s == 0 else -1
            S = stats[s]
            colS = 'w' if s == 0 else 'b'
            e_before = sign * E[k]; e_after = sign * E[k + 1]
            w_before = W[k] if s == 0 else 100 - W[k]
            w_after = W[k + 1] if s == 0 else 100 - W[k + 1]
            drop = max(0.0, w_before - w_after)
            acc = move_acc(drop)
            cpl = max(0, max(-1000, min(1000, e_before)) - max(-1000, min(1000, e_after)))
            npm_k = board.npm()
            ph = phase_of(k, npm_k)
            played = sqname(f) + sqname(t) + (pr or '')
            best = B[k] if k < len(B) else None
            if best and not re.match(r'^[a-h][1-8][a-h][1-8]', best):
                best = None
            is_best = best == played
            # --- tactics opportunities, judged on the position before the move ---
            flags = {}
            if best and re.match(r'^[a-h][1-8][a-h][1-8]', best):
                bf = 'abcdefgh'.index(best[0]) + 8 * (int(best[1]) - 1)
                bt_ = 'abcdefgh'.index(best[2]) + 8 * (int(best[3]) - 1)
                bb = board.copy(); bi = bb.apply(bf, bt_, best[4:] or '')
                best_fork = (not bi.get('bad')) and bb.fork_by(bt_)
                best_pin = (not bi.get('bad')) and bb.pin_by(bt_)
            else:
                best_fork = best_pin = False
            # free pieces of the opponent, capturable now
            opp_col = 'b' if colS == 'w' else 'w'
            free_sq = [q for q in range(64) if board.b[q] and color_of(board.b[q]) == opp_col and board.b[q].lower() in 'nbrq' and board.hanging(q)]
            best_takes_free = bool(best and re.match(r'^[a-h][1-8][a-h][1-8]', best)) and sq_index(best[2:4]) in free_sq
            mate_avail = e_before >= MATE - 50
            # --- play the move ---
            nb = board.copy(); info = nb.apply(f, t, pr)
            played_fork = nb.fork_by(t); played_pin = nb.pin_by(t)
            if best_fork:
                S['t_opp']['fork'] += 1
                ok = played_fork and drop < 5
                if ok: S['t_found']['fork'] += 1
                events.append((gid, k, s, 'fork', ok))
            if best_pin:
                S['t_opp']['pin'] += 1
                ok = played_pin and drop < 5
                if ok: S['t_found']['pin'] += 1
                events.append((gid, k, s, 'pin', ok))
            if mate_avail:
                S['t_opp']['mate'] += 1
                ok = e_after >= MATE - 50
                if ok: S['t_found']['mate'] += 1
                events.append((gid, k, s, 'mate', ok))
            if free_sq and best_takes_free:
                S['t_opp']['freepiece'] += 1
                ok = t in free_sq
                if ok: S['t_found']['freepiece'] += 1
                events.append((gid, k, s, 'freepiece', ok))
            # my own pieces left hanging (and it cost something)
            hung = None
            if drop >= 10:
                own = [q for q in range(64) if nb.b[q] and color_of(nb.b[q]) == colS and nb.b[q].lower() in 'pnbrq' and nb.hanging(q)]
                if own:
                    hung = max((nb.b[q].lower() for q in own), key=lambda x: VAL[x])
                    S['hang'][hung] += 1
            # sacrifice: after the move, one of my pieces (knight or better) is en prise that
            # wasn't before, and the move didn't win material of at least that value
            before_hang = {q2 for q2 in range(64) if board.b[q2] and color_of(board.b[q2]) == colS and board.b[q2].lower() in 'nbrq' and board.hanging(q2)}
            after_hang = [q2 for q2 in range(64) if nb.b[q2] and color_of(nb.b[q2]) == colS and nb.b[q2].lower() in 'nbrq' and nb.hanging(q2)
                          and not (q2 == t and f in before_hang) and q2 not in before_hang]
            sac_val = max((VAL[nb.b[q2].lower()] for q2 in after_hang), default=0)
            sac = sac_val >= 3 and VAL.get(info['capture'] or 'x', 0) < sac_val
            # only-move check from the MultiPV-2 pass: gap between best and second-best for the mover
            gap = None
            mp = mpv.get(str(k)) if mpv else None
            if mp and mp[0] is not None and mp[1] is not None:
                gap = (wp(sign * mp[0]) - wp(sign * mp[1]))
            opp_err = prev_drop[1 - s]
            # --- Great / Brilliant / Forced: WintrChess rules (ported in wintr.py) ---
            mate_before = abs(E[k]) >= MATE - 50
            mate_after_any = abs(E[k + 1]) >= MATE - 50
            second_cp = sign * mp[1] if mp and mp[1] is not None else None
            after_cp = sign * E[k + 1]
            forced = wintr.is_forced(board.b, colS)
            checkmate = mate_after_any and after_cp >= MATE - 1
            candidate = wintr.critical_candidate(board.b, colS, second_cp, after_cp, pr, not mate_after_any)
            cap_type = info['capture']
            # --- point loss, with WintrChess's handling of mate scores ---
            q = None
            if mate_before and mate_after_any:
                pb, pa = sign * E[k], after_cp          # mover's view; +ve = mover mates
                if pb > 0 and pa < 0:
                    q = 'Mistake' if (MATE - abs(pa)) > 3 else 'Blunder'
                else:
                    mb, ma = MATE - abs(pb), MATE - abs(pa)   # moves to mate
                    loss = (ma - mb) if pb > 0 else (mb - ma)
                    if pb < 0:   # being mated: chess.com flags letting the mate come 2+ moves sooner
                        q = 'Best' if loss <= 0 else 'Excellent' if loss < 2 else 'Inaccuracy'
                    else:
                        q = 'Best' if loss < 0 or pb > 0 and ma <= mb - 1 else 'Excellent' if loss < 2 else 'Good' if loss < 7 else 'Inaccuracy'
            elif mate_before and not mate_after_any:
                q = 'Excellent' if after_cp >= 800 else 'Good' if after_cp >= 400 else 'Inaccuracy' if after_cp >= 200 else 'Mistake' if after_cp >= 0 else 'Blunder'
            elif not mate_before and mate_after_any:
                mate_in = MATE - abs(after_cp)
                q = 'Best' if after_cp > 0 else 'Blunder' if mate_in <= 2 else 'Mistake' if mate_in <= 5 else 'Inaccuracy'
            # --- classification ---
            if CLASSIFIER == 'wintr':
                # WintrChess (shared/src/lib/reporter/classify.ts): forced, theory, checkmate, top move = best,
                # otherwise mate rules above or expected-points loss (gradient 0.0035). No "Miss" class.
                if k < bp:
                    q = 'Book'
                elif forced or checkmate or is_best:
                    q = 'Best'
                elif q is None:
                    # Expected-points curve calibrated to chess.com: WintrChess uses 1/(1+e^(-0.0035 cp)) with
                    # cut-offs 1 / 4.5 / 8 / 12 / 22%, which keeps 99% of chess.com's good moves good but flags only
                    # 62% of its bad ones. chess.com is stricter when you are already worse and more lenient when
                    # winning, so the curve is flatter (0.002) and centred 4 pawns towards the losing side. Fitted
                    # on 30 chess.com-reviewed games and checked game by game on held-out games:
                    # 92% of good moves stay good, 88% of bad moves are flagged.
                    ep = lambda cp: 1 / (1 + math.exp(-EP_K * max(-5000, min(5000, cp + EP_SHIFT))))
                    loss = max(0.0, ep(sign * E[k]) - ep(after_cp))
                    q = ('Best' if loss < 0.002 else 'Excellent' if loss < 0.015 else 'Good' if loss < 0.025
                         else 'Inaccuracy' if loss < 0.06 else 'Mistake' if loss < 0.24 else 'Blunder')
            elif k < bp:
                q = 'Book'
            elif forced or checkmate:
                q = 'Best'
            elif q is None or q in ('Mistake', 'Blunder', 'Inaccuracy'):
                if q is None:
                    if opp_err >= 10 and w_before >= 65 and drop >= 10:
                        q = 'Miss'      # opponent erred, you had a winning chance, you let it go
                    elif is_best:
                        q = 'Best'
                    elif drop >= 20:
                        q = 'Blunder'
                    elif drop >= 10:
                        q = 'Mistake'
                    elif drop >= 5:
                        q = 'Inaccuracy'
                    elif drop < 0.5:
                        q = 'Best'
                    elif drop < 2:
                        q = 'Excellent'
                    else:
                        q = 'Good'
            if q == 'Best' and k >= bp and not forced and not checkmate:
                second_loss = (wp(sign * mp[0]) - wp(sign * mp[1])) if mp and mp[0] is not None and mp[1] is not None and abs(mp[1]) < MATE - 50 else (
                    100.0 if mp and mp[1] is not None and sign * mp[1] <= -(MATE - 50) else None)
                if is_best and wintr.is_critical(board.b, (f, t, pr, cap_type), colS, candidate, after_cp >= MATE - 50, second_loss):
                    q = 'Great'
                # Brilliant: a sound piece sacrifice that is the best or nearly best move (WintrChess rule with
                # exchange-based safety). Superbrilliant: on top of that it is Stockfish's top move AND clearly the
                # best one: the second-best move in the position before it loses at least GREAT_GAP points of win chance.
                if wintr.is_brilliant(board.b, nb.b, (f, t, pr, cap_type), colS, candidate):
                    q = 'Superbrilliant' if (is_best and second_loss is not None and second_loss >= wintr.GREAT_GAP) else 'Brilliant'
            prev_drop[s] = drop
            if s == me and q in CATQ:
                mate_after = abs(E[k + 1]) == MATE
                try:
                    b_san = None
                    if best and re.match(r'^[a-h][1-8][a-h][1-8]', best):
                        b_san = to_san(board, sq_index(best[:2]), sq_index(best[2:4]), best[4:])
                    catalog.append({'g': gid, 'ts': base['end_ts'], 'ply': k + 1, 'q': q, 'fen': fen_board(board), 'turn': colS,
                                    'uci': played, 'san': to_san(board, f, t, pr, mate_after), 'best': best if not is_best else None, 'best_san': b_san if not is_best else None,
                                    'eb': E[k], 'ea': E[k + 1], 'wb': round(w_before, 1), 'wa': round(w_after, 1), 'phase': ph, 'mn': k // 2 + 1})
                except Exception:
                    pass
            S['q'][q] += 1
            S['acc'].append(acc); S['cpl'].append(cpl)
            pc = info['piece'] or 'p'
            S['piece_n'][pc] += 1; S['piece_acc'][pc] += acc
            S['phase_n'][ph] += 1; S['phase_acc'][ph] += acc
            if q == 'Blunder' or (q == 'Miss' and drop >= 20): S['phase_bl'][ph] += 1
            mn = k // 2 + 1
            mnb = min(mn, 60)
            S['mn_acc'][mnb] += acc; S['mn_n'][mnb] += 1
            if info['castle'] and S['castle'] is None:
                S['castle'] = info['castle']; S['castle_ply'] = k; S['castle_phase'] = ph
            spent = None
            if k < len(clocks) and bt is not None:
                prevc = clocks[k - 2] if k >= 2 else bt
                spent = max(0.0, round(prevc - clocks[k] + (inc if k >= 2 else 0), 1))
                S['time'].append(spent)
            moves_rows.append({'game_id': gid, 'ply': k + 1, 'move_number': mn, 'side': 'White' if s == 0 else 'Black', 'is_me': int(s == me),
                               'uci': played, 'piece': PNAME[pc], 'capture': PNAME[info['capture']] if info['capture'] else '', 'castle': info['castle'] or '',
                               'best_move': best or '', 'is_best': int(is_best), 'eval_before_cp': E[k], 'eval_after_cp': E[k + 1],
                               'winpct_before': round(w_before, 1), 'winpct_after': round(w_after, 1), 'winpct_drop': round(drop, 1),
                               'accuracy': round(acc, 1), 'cpl': cpl, 'classification': q, 'phase': ph,
                               'clock_after_s': clocks[k] if k < len(clocks) else None, 'time_spent_s': spent, 'left_hanging': PNAME[hung] if hung else ''})
            d_san.append(to_san(board, f, t, pr, abs(E[k + 1]) >= MATE - 50 and sign * E[k + 1] > 0 and not wintr.legal_moves(nb.b, opp_col)))
            d_cls.append(QCODE[q]); d_best.append(best or '')
            board = nb
        end_phase = phase_of(n - 1, board.npm())
        shape = shape_of(W)
        mineS, oppS = stats[me], stats[1 - me]
        mean = lambda a: round(sum(a) / len(a), 2) if a else None
        # first to leave book
        left_first = None; my_book = None
        if bp and bp < n:
            leaver = bp % 2
            left_first = 'me' if leaver == me else 'opp'
            my_book = sum(1 for k in range(bp) if k % 2 == me)

        def pack(S):
            return {
                'acc': cal_acc(mean(S['acc'])), 'acpl': mean(S['cpl']), 'n': len(S['acc']),
                'q': [S['q'][x] for x in QUAL],
                'pn': [S['piece_n'][x] for x in PIECES], 'pa': [cal_sum(S['piece_acc'][x], S['piece_n'][x]) for x in PIECES],
                'phn': [S['phase_n'][x] for x in ('Opening', 'Middlegame', 'Endgame')],
                'pha': [cal_sum(S['phase_acc'][x], S['phase_n'][x]) for x in ('Opening', 'Middlegame', 'Endgame')],
                'phb': [S['phase_bl'][x] for x in ('Opening', 'Middlegame', 'Endgame')],
                'to': [S['t_opp'][x] for x in TACT], 'tf': [S['t_found'][x] for x in TACT],
                'hang': [S['hang'][x] for x in 'pnbrq'],
                'castle': S['castle'], 'castle_phase': S.get('castle_phase'),
                'tpm': mean(S['time']),
                'mn': {str(k2): [cal_sum(S['mn_acc'][k2], S['mn_n'][k2]), S['mn_n'][k2]] for k2 in S['mn_n']},
            }
        mp, op_ = pack(mineS), pack(oppS)
        base['detail'] = {'san': ' '.join(d_san), 'uci': ' '.join(x[0] for x in [(sqname(f2) + sqname(t2) + p2,) for f2, t2, p2 in mv]),
                          'ev': [max(-MATE, min(MATE, int(v))) for v in E], 'cls': ''.join(d_cls), 'best': ' '.join(b2 or '-' for b2 in d_best)}
        if clocks:  # remaining clock after each move, for the game review's player clocks
            base['detail'].update({'clk': [round(float(c), 1) for c in clocks[:n]], 'clk0': bt, 'inc': inc})
        base.update({'analyzed': True, 'plies': n, 'shape': shape, 'end_phase': end_phase, 'book_plies': bp,
                     'left_book_first': left_first, 'my_book_moves': my_book, 'me': mp, 'opp': op_,
                     'best_winpct': round(max((W[k] if me == 0 else 100 - W[k]) for k in range(min(10, n), n + 1)), 1) if n > 10 else None,
                     'worst_winpct': round(min((W[k] if me == 0 else 100 - W[k]) for k in range(min(10, n), n + 1)), 1) if n > 10 else None})
        games.append(base)
    catalog.sort(key=lambda c: -c['ts'])
    keep = Counter(); cat = []
    for c in catalog:
        if keep[c['q']] < CAT_CAP[c['q']]:
            keep[c['q']] += 1; c.pop('ts'); cat.append(c)
    return games, moves_rows, events, M.get('stats', {}), cat


def write_outputs(games, moves_rows, events, stats, outdir):
    os.makedirs(outdir, exist_ok=True)
    # --- SQLite ---
    dbp = os.path.join(outdir, 'abdh2005_chess.db')
    if os.path.exists(dbp): os.remove(dbp)
    con = sqlite3.connect(dbp)
    gcols = ['id', 'site', 'url', 'date', 'hour', 'weekday', 'time_class', 'time_control', 'rules', 'rated', 'color', 'result', 'how', 'my_rating', 'opp_rating',
             'opponent', 'opp_country', 'opening', 'eco', 'chesscom_accuracy', 'analyzed', 'plies', 'shape', 'end_phase', 'book_plies', 'left_book_first',
             'my_book_moves', 'best_winpct', 'worst_winpct']
    extra = ['my_accuracy', 'opp_accuracy', 'my_acpl', 'opp_acpl', 'my_blunders', 'my_mistakes', 'my_inaccuracies', 'opp_blunders', 'my_castle', 'my_castle_phase', 'opp_castle', 'my_avg_time_per_move']
    con.execute('CREATE TABLE games (' + ','.join(c + (' PRIMARY KEY' if c == 'id' else '') for c in gcols + extra) + ')')
    grows = []
    for g in games:
        r = [g.get(c) for c in gcols]
        if g.get('analyzed'):
            me, op = g['me'], g['opp']
            qi = {x: i for i, x in enumerate(QUAL)}
            r += [me['acc'], op['acc'], me['acpl'], op['acpl'], me['q'][qi['Blunder']] + me['q'][qi['Miss']], me['q'][qi['Mistake']], me['q'][qi['Inaccuracy']],
                  op['q'][qi['Blunder']] + op['q'][qi['Miss']], me['castle'], me['castle_phase'], op['castle'], me['tpm']]
        else:
            r += [None] * len(extra)
        grows.append(r)
    con.executemany('INSERT INTO games VALUES (' + ','.join('?' * len(gcols + extra)) + ')', grows)
    mcols = list(moves_rows[0].keys()) if moves_rows else []
    if mcols:
        con.execute('CREATE TABLE moves (' + ','.join(mcols) + ')')
        con.executemany('INSERT INTO moves VALUES (' + ','.join('?' * len(mcols)) + ')', [[m[c] for c in mcols] for m in moves_rows])
        con.execute('CREATE INDEX moves_game ON moves(game_id)')
    con.execute('CREATE TABLE tactics (game_id, ply, side, motif, found)')
    con.executemany('INSERT INTO tactics VALUES (?,?,?,?,?)', [(g, k + 1, 'White' if s == 0 else 'Black', mo, int(ok)) for g, k, s, mo, ok in events])
    con.commit(); con.close()
    # --- CSVs ---
    with open(os.path.join(outdir, 'abdh2005_games.csv'), 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(gcols + extra); w.writerows(grows)
    if mcols:
        with open(os.path.join(outdir, 'abdh2005_moves.csv'), 'w', newline='') as fh:
            w = csv.DictWriter(fh, mcols); w.writeheader(); w.writerows(moves_rows)
    return dbp


def app_data(games, events, stats):
    """Compact per-game rows for the app; everything is aggregated client-side."""
    G = []
    for g in games:
        if g['rules'] != 'chess':
            continue
        row = {k: g.get(k) for k in ('id', 'site', 'url', 'end_ts', 'date', 'hour', 'weekday', 'time_class', 'rated', 'color', 'result', 'how', 'my_rating', 'opp_rating',
                                      'opponent', 'opp_country', 'opening', 'eco', 'eco_url', 'plies', 'shape', 'end_phase', 'book_plies', 'left_book_first',
                                      'my_book_moves', 'best_winpct', 'worst_winpct', 'analyzed', 'my_pre', 'prov')}
        if row.get('my_pre') is None: row.pop('my_pre', None)
        if not row.get('prov'): row.pop('prov', None)
        if g.get('analyzed'):
            row['me'] = g['me']; row['op'] = g['opp']
        G.append(row)
    # most recent 60 occurrences per motif & outcome, for the drill-down lists (my moves only)
    me_side = {g['id']: (0 if g['color'] == 'White' else 1) for g in games}
    ts = {g['id']: g['end_ts'] for g in games}
    ev = [e for e in events if e[2] == me_side.get(e[0])]
    ev.sort(key=lambda e: -ts.get(e[0], 0))
    keep = defaultdict(list)
    for gid, k, s, mo, ok in ev:
        key = (mo, ok)
        if len(keep[key]) < 400:
            keep[key].append([gid, k + 1])
    E = [{'motif': mo, 'found': ok, 'games': v} for (mo, ok), v in keep.items()]
    return {'games': G, 'events': E, 'stats': stats, 'qual': QUAL, 'tact': TACT}


def detail_data(games):
    return {g['id']: g['detail'] for g in games if g.get('detail')}


if __name__ == '__main__':
    a, m, out = sys.argv[1:4]
    games, moves_rows, events, stats, cat = run(a, m, out)
    dbp = write_outputs(games, moves_rows, events, stats, out)
    ad = app_data(games, events, stats)
    ad['catalog'] = cat
    if len(sys.argv) > 4:
        mp = json.load(open(sys.argv[4]))
        ad['map'] = {'topo': mp['topo'], 'codes': mp['codes']}
    json.dump(ad, open(os.path.join(out, 'app_data.json'), 'w'), separators=(',', ':'))
    json.dump(detail_data(games), open(os.path.join(out, 'detail.json'), 'w'), separators=(',', ':'))
    an = [g for g in games if g.get('analyzed')]
    print('games', len(games), 'analyzed', len(an), 'moves', len(moves_rows), 'events', len(events))
    print('shapes', Counter(g['shape'] for g in an).most_common())
    print('end phase', Counter(g['end_phase'] for g in an).most_common())
