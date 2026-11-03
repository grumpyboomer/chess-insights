"""Online update (GitHub Actions): restore the saved analysis, fetch only new games, analyse them, save, build the site.

store/   = the `data` branch: engine_cache.json, sources.json, mapdata.json, app_data.json, detail.json
_site/   = what GitHub Pages serves: index.html, ux.html, app_data.json, detail.json
"""
import json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = os.path.join(ROOT, 'builder')
STORE = os.path.join(ROOT, 'store')
SITE = os.path.join(ROOT, '_site')
CC, LI = os.environ.get('CHESSCOM', 'abdh2005'), os.environ.get('LICHESS', 'abdulrahmanhendawy20')
sys.path.insert(0, B)
import insights_builder as ib  # noqa: E402

KEY = ib.slug(CC and 'cc-' + CC, LI and 'li-' + LI)
WORK, REP = os.path.join(ib.DATA_DIR, KEY), os.path.join(ib.REPORTS_DIR, KEY)


def cp(a, b):
    if os.path.exists(a):
        os.makedirs(os.path.dirname(b), exist_ok=True); shutil.copy(a, b)


def restore():
    for f in ('engine_cache.json', 'sources.json'): cp(os.path.join(STORE, f), os.path.join(WORK, f))
    cp(os.path.join(STORE, 'mapdata.json'), os.path.join(ib.DATA_DIR, 'mapdata.json'))
    for f in ('app_data.json', 'detail.json'): cp(os.path.join(STORE, f), os.path.join(REP, f))


def save():
    os.makedirs(STORE, exist_ok=True)
    for f in ('engine_cache.json', 'sources.json'): cp(os.path.join(WORK, f), os.path.join(STORE, f))
    cp(os.path.join(ib.DATA_DIR, 'mapdata.json'), os.path.join(STORE, 'mapdata.json'))
    for f in ('app_data.json', 'detail.json'): cp(os.path.join(REP, f), os.path.join(STORE, f))


def site():
    shutil.rmtree(SITE, ignore_errors=True); os.makedirs(SITE)
    shutil.copy(os.path.join(B, 'app_template.html'), os.path.join(SITE, 'index.html'))
    shutil.copy(os.path.join(B, 'ux_template.html'), os.path.join(SITE, 'ux.html'))
    for f in ('app_data.json', 'detail.json'): shutil.copy(os.path.join(REP, f), os.path.join(SITE, f))
    open(os.path.join(SITE, '.nojekyll'), 'w').close()


def main():
    restore()
    before = os.path.getmtime(os.path.join(REP, 'app_data.json')) if os.path.exists(os.path.join(REP, 'app_data.json')) else 0
    opts = {'chesscom': CC, 'lichess': LI, 'update': '1', 'engines': str(os.cpu_count() or 2)}
    if os.environ.get('FORCE') == 'true':
        opts['force'] = '1'
    t = time.time()
    ib.build(opts)  # raises on any error, so the run fails visibly
    rebuilt = os.path.getmtime(os.path.join(REP, 'app_data.json')) != before
    save(); site()
    ad = json.load(open(os.path.join(REP, 'app_data.json')))
    msg = (f"{'Rebuilt' if rebuilt else 'No new games'}: {len(ad['games'])} games · built {ad.get('player', {}).get('built')} · {time.time() - t:.0f}s")
    print(msg)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        open(os.environ['GITHUB_STEP_SUMMARY'], 'a').write(msg + '\n')
    if os.environ.get('GITHUB_OUTPUT'):
        open(os.environ['GITHUB_OUTPUT'], 'a').write(f"rebuilt={'true' if rebuilt else 'false'}\n")


if __name__ == '__main__':
    main()
