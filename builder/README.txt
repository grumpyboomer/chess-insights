Chess Insights Builder
======================

1. Double-click "Chess Insights Builder.bat".
   A black window opens (keep it open) and your browser opens the builder page.
2. Type a chess.com username, a Lichess username, or both. Pick a depth and press "Build report".
3. When it finishes, click "Open the report". Every report you build is listed on the builder page.

First run: the builder downloads the official Stockfish engine from GitHub
(github.com/official-stockfish/Stockfish) into the "engine" folder.

How the engine works: pass 1 analyses every position at a fixed depth with the top 2 moves
(so "Great" moves can be detected). Pass 2 re-searches only the borderline positions, where a
move sits near a class threshold (inaccuracy, mistake, blunder, miss, great), with a much bigger search.
  Fast      depth 12, re-checks with 0.3 million nodes
  Standard  depth 14, re-checks with 1 million nodes    (default)
  Deep      depth 14, re-checks with 3 million nodes    (about 3x Standard)
  Max       depth 16, re-checks with 8 million nodes    (overnight)
  Custom    any depth up to 30, re-checks up to 100 million nodes
Analysis already done in the browser (a *_engine_analysis_full.json file next to this folder or in
Downloads) is reused for pass 1 automatically.

New games (when you want them):
  Press "Check for new games" on the builder page, or the same button at the top of an open report.
  It downloads only games played since the last build, analyses just those with the same engine
  settings, and refreshes the report (an open report reloads itself). Nothing runs in the background.

Speed: the builder tells Windows not to put Stockfish into "efficiency mode", so it runs at full speed
even when its window isn't in front. Keep the laptop plugged in and set Windows power mode to
"Best performance" for the fastest builds.

Where things go:
  reports\<name>\index.html   the report (open it through the builder page, not by double-clicking)
  reports\<name>\*.db, *.csv  SQLite database and CSVs for that account
  data\<name>\                downloaded games and the engine cache (rebuilding only analyses new games)

Command line, no browser:
  python insights_builder.py --cli chesscom=NAME lichess=NAME preset=deep
  python insights_builder.py --cli chesscom=NAME lichess=NAME update=1      (just new games)
