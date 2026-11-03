"""Brilliant / Great (critical) / Forced detection, ported to plain Python from WintrChess
(github.com/WintrCat/wintrchess, shared/src/lib/reporter, GPL-3.0). Board = list of 64
FEN chars (None for empty), square 0 = a1. Castling and en passant are ignored: they never
change whether a piece is safe."""

GREAT_GAP = 8  # points of win chance the second-best move must lose (WintrChess uses 10; 8 matched chess.com better on 30 reviewed games)
VAL = {'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9, 'k': float('inf')}
KN = [(1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)]
KG = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
DG = [(1, 1), (1, -1), (-1, 1), (-1, -1)]
OR = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def col(p):
    return None if p is None else ('w' if p.isupper() else 'b')


def opp(c):
    return 'b' if c == 'w' else 'w'


def on(f, r):
    return 0 <= f < 8 and 0 <= r < 8


def targets(b, s):
    """Squares the piece on s attacks (pawn: diagonal captures only)."""
    p = b[s]; f, r = s % 8, s // 8; lo = p.lower(); out = []
    if lo == 'p':
        dr = 1 if p == 'P' else -1
        for df in (-1, 1):
            if on(f + df, r + dr): out.append((r + dr) * 8 + f + df)
    elif lo == 'n' or lo == 'k':
        for df, dr in (KN if lo == 'n' else KG):
            if on(f + df, r + dr): out.append((r + dr) * 8 + f + df)
    else:
        for df, dr in (DG if lo == 'b' else OR if lo == 'r' else DG + OR):
            ff, rr = f + df, r + dr
            while on(ff, rr):
                q = rr * 8 + ff; out.append(q)
                if b[q] is not None: break
                ff += df; rr += dr
    return out


def attacked(b, s, by):
    for q in range(64):
        p = b[q]
        if p is not None and col(p) == by and s in targets(b, q):
            return True
    return False


def king_sq(b, c):
    k = 'K' if c == 'w' else 'k'
    try:
        return b.index(k)
    except ValueError:
        return -1


def in_check(b, c):
    k = king_sq(b, c)
    return k >= 0 and attacked(b, k, opp(c))


def make(b, m):
    """m = (from, to, promo). Returns new board."""
    f, t, pr = m
    nb = b[:]
    p = nb[f]
    if p.lower() == 'p' and (f % 8) != (t % 8) and nb[t] is None:  # en passant
        nb[t + (-8 if p == 'P' else 8)] = None
    nb[t] = (pr.upper() if p == 'P' else pr) if pr else p
    nb[f] = None
    return nb


def pseudo_moves(b, c, frm=None):
    out = []
    for s in ([frm] if frm is not None else range(64)):
        p = b[s]
        if p is None or col(p) != c:
            continue
        lo = p.lower(); f, r = s % 8, s // 8
        if lo == 'p':
            dr = 1 if c == 'w' else -1; last = 7 if c == 'w' else 0
            one = s + 8 * dr
            if 0 <= one < 64 and b[one] is None:
                out += [(s, one, x) for x in ('q', 'r', 'b', 'n')] if one // 8 == last else [(s, one, '')]
                start = 1 if c == 'w' else 6
                two = s + 16 * dr
                if r == start and b[two] is None:
                    out.append((s, two, ''))
            for t in targets(b, s):
                if b[t] is not None and col(b[t]) != c:
                    out += [(s, t, x) for x in ('q', 'r', 'b', 'n')] if t // 8 == last else [(s, t, '')]
        else:
            for t in targets(b, s):
                if b[t] is None or col(b[t]) != c:
                    out.append((s, t, ''))
    return out


def legal_moves(b, c, frm=None):
    return [m for m in pseudo_moves(b, c, frm) if (b[m[1]] is None or b[m[1]].lower() != 'k') and not in_check(make(b, m), c)]


def gives_mate(b, c):
    """Does colour c have a move that checkmates?"""
    for m in legal_moves(b, c):
        nb = make(b, m)
        if in_check(nb, opp(c)) and not legal_moves(nb, opp(c)):
            return True
    return False


# ---------------------------------------------------------------- attackers / defenders
def direct_attacking_moves(b, sq):
    """Moves by the other side that capture the piece on sq (legal), plus the king's attack."""
    piece = b[sq]; a = opp(col(piece))
    moves = []
    for q in range(64):
        p = b[q]
        if p is None or col(p) != a or sq not in targets(b, q):
            continue
        if p.lower() == 'k':
            continue
        if not in_check(make(b, (q, sq, '')), a):
            moves.append((q, sq, p.lower()))
    for q in range(64):
        p = b[q]
        if p is not None and col(p) == a and p.lower() == 'k' and sq in targets(b, q):
            moves.append((q, sq, 'k'))
    return moves


def attacking_moves(b, sq, transitive=True):
    moves = direct_attacking_moves(b, sq)
    if not transitive:
        return moves
    frontier = [(b, m[0], m[2]) for m in moves]
    while frontier:
        tb, fsq, ftype = frontier.pop()
        if ftype == 'k':
            continue
        old = [m for m in direct_attacking_moves(tb, sq) if m[0] != fsq]
        nb = tb[:]; nb[fsq] = None
        new = direct_attacking_moves(nb, sq)
        revealed = [m for m in old if m not in new] + [m for m in new if m not in old]
        moves += revealed
        frontier += [(nb, m[0], m[2]) for m in revealed]
    return moves


def defending_moves(b, sq, transitive=True):
    piece = b[sq]
    sets = []
    for (fq, tq, ptype) in attacking_moves(b, sq, False):
        nb = make(b, (fq, tq, ''))
        if in_check(nb, opp(col(piece))) and ptype != 'k':
            continue
        sets.append(attacking_moves(nb, tq, transitive))
    if sets:
        return min(sets, key=len)
    nb = b[:]; nb[sq] = piece.swapcase()
    return attacking_moves(nb, sq, transitive)


def see(b, sq):
    """Static exchange on sq: material the attacking side wins by capturing there with its cheapest
    legal attacker each time (either side may stop). Pins count: a pinned piece can't recapture."""
    piece = b[sq]
    if piece is None:
        return 0
    a = opp(col(piece)); best = None
    for (q, s2, pt) in direct_attacking_moves(b, sq):
        if pt == 'k' and in_check(make(b, (q, sq, '')), a):
            continue
        if best is None or VAL[pt] < VAL[best[2]]:
            best = (q, s2, pt)
    if best is None:
        return 0
    return max(0, VAL[piece.lower()] - see(make(b, (best[0], sq, '')), sq))


def piece_safe_see(b, sq, played=None):
    """Brilliant checks only. played = (from, to, captured_type or None). Unsafe = the opponent wins material by capturing it
    (static exchange), which also covers pieces only 'outnumbered' on paper but defended in practice."""
    piece = b[sq]; pt = piece.lower()
    if played and played[2] and pt == 'r' and VAL[played[2]] == VAL['n']:
        attackers = [m[2] for m in attacking_moves(b, sq)]
        defenders = [m[2] for m in defending_moves(b, sq)]
        if len(attackers) == 1 and defenders and VAL[attackers[0]] == VAL['n']:
            return True
    return see(b, sq) < 2   # losing a pawn's worth in the exchange isn't a sacrifice


def is_piece_safe(b, sq, played=None):
    """played = (from, to, captured_type or None)."""
    piece = b[sq]; pt = piece.lower()
    direct = [m[2] for m in attacking_moves(b, sq, False)]
    attackers = [m[2] for m in attacking_moves(b, sq)]
    defenders = [m[2] for m in defending_moves(b, sq)]
    if (played and played[2] and pt == 'r' and VAL[played[2]] == VAL['n'] and len(attackers) == 1
            and defenders and VAL[attackers[0]] == VAL['n']):
        return True
    if any(VAL[a] < VAL[pt] for a in direct):
        return False
    if len(attackers) <= len(defenders):
        return True
    if not direct:
        return True
    low = min(direct, key=lambda a: VAL[a])
    if VAL[pt] < VAL[low] and any(VAL[d] < VAL[low] for d in defenders):
        return True
    if 'p' in defenders:
        return True
    return False


def unsafe_pieces(b, c, played=None):
    capv = VAL[played[2]] if played and played[2] else 0
    return [q for q in range(64) if b[q] is not None and col(b[q]) == c and b[q].lower() not in 'pk'
            and VAL[b[q].lower()] > capv and attacked(b, q, opp(c)) and not piece_safe_see(b, q, played)]


# ---------------------------------------------------------------- danger levels / trapped
def _relative_unsafe_attacks(b, threatened_sq, threatened_type, c, played=None):
    out = []
    for q in unsafe_pieces(b, c, played):
        if q != threatened_sq and VAL[b[q].lower()] >= VAL[threatened_type]:
            out += attacking_moves(b, q, False)
    return out


def move_creates_greater_threat(b, threatened_sq, threatened_type, move, mover):
    prev = _relative_unsafe_attacks(b, threatened_sq, threatened_type, mover)
    f, t = move[0], move[1]
    if b[f] is None:
        return False
    cap = b[t].lower() if b[t] else None
    nb = make(b, (f, t, ''))
    if in_check(nb, mover):
        return False
    rel = _relative_unsafe_attacks(nb, threatened_sq, threatened_type, mover, (f, t, cap))
    if [m for m in rel if m not in prev]:
        return True
    return VAL[threatened_type] < VAL['q'] and gives_mate(nb, opp(mover))


def move_leaves_greater_threat(b, threatened_sq, threatened_type, move, mover):
    f, t = move[0], move[1]
    if b[f] is None:
        return False
    nb = make(b, (f, t, ''))
    if in_check(nb, mover):
        return False
    if _relative_unsafe_attacks(nb, threatened_sq, threatened_type, mover):
        return True
    return VAL[threatened_type] < VAL['q'] and gives_mate(nb, opp(mover))


def has_danger_levels(b, sq, acting_moves, mode='leaves'):
    t = b[sq].lower()
    fn = move_creates_greater_threat if mode == 'creates' else move_leaves_greater_threat
    return all(fn(b, sq, t, m, col(b[m[0]])) for m in acting_moves)


def is_piece_trapped(b, sq, danger=True):
    piece = b[sq]; c = col(piece)
    if piece_safe_see(b, sq):
        return False
    for m in legal_moves(b, c, sq):
        if b[m[1]] is not None and b[m[1]].lower() == 'k':
            return False
        if danger and move_creates_greater_threat(b, sq, piece.lower(), m, c):
            continue
        nb = make(b, m)
        cap = b[m[1]].lower() if b[m[1]] else None
        if piece_safe_see(nb, m[1], (m[0], m[1], cap)):
            return False
    return True


# ---------------------------------------------------------------- classifications
def is_forced(b, c):
    return len(legal_moves(b, c)) <= 1


def critical_candidate(prev_b, c, second_cp, after_cp, promo, after_is_cp=True):
    """WintrChess isMoveCriticalCandidate. second_cp / after_cp from the mover's view."""
    if second_cp is not None:
        if abs(second_cp) < 9000 and second_cp >= 700:
            return False
    elif after_is_cp and after_cp >= 700:
        return False
    if after_cp < 0:
        return False
    if promo == 'q':
        return False
    if in_check(prev_b, c):
        return False
    return True


def is_brilliant(prev_b, cur_b, move, c, candidate):
    """move = (from, to, promo, captured_type)."""
    if not candidate or move[2]:
        return False
    played = (move[0], move[1], move[3])
    unsafe = unsafe_pieces(cur_b, c, played)
    if not unsafe:
        return False
    prev_unsafe = unsafe_pieces(prev_b, c)
    if not in_check(cur_b, opp(c)) and len(unsafe) < len(prev_unsafe):
        return False
    # a retreat from an attack that leaves behind a piece that was already attacked (e.g. one side of a
    # fork) is damage control, not a sacrifice
    if move[0] in prev_unsafe and move[1] not in unsafe and all(attacked(prev_b, q, opp(c)) for q in unsafe):
        return False
    if all(has_danger_levels(cur_b, q, attacking_moves(cur_b, q, False)) for q in unsafe):
        return False
    prev_trapped = [q for q in prev_unsafe if is_piece_trapped(prev_b, q)]
    trapped = [q for q in unsafe if is_piece_trapped(cur_b, q)]
    if len(trapped) == len(unsafe) or move[0] in prev_trapped or len(trapped) < len(prev_trapped):
        return False
    return True


def is_critical(prev_b, move, c, candidate, mate_for_mover_after, second_loss_pts):
    """second_loss_pts: expected-points loss (0-100) of the second-best move vs the best."""
    if not candidate or mate_for_mover_after:
        return False
    if move[3]:
        cap_sq = move[1]
        if prev_b[cap_sq] is not None and not is_piece_safe(prev_b, cap_sq):
            return False
    if second_loss_pts is None:
        return False
    return second_loss_pts >= GREAT_GAP
