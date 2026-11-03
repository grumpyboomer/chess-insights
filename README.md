# Chess Insights (online updater)

Analyses every chess.com (abdh2005) and Lichess (abdulrahmanhendawy20) game with Stockfish and publishes the Insights site on GitHub Pages.

- **Runs by itself every 3 hours.** Only new games are fetched and analysed.
- **Update now:** Actions → *Update Chess Insights* → **Run workflow** (or the ↻ Update button on the site).
- **Site:** `https://<username>.github.io/chess-insights/` (UX version at `ux.html`).
- Saved engine analysis lives on the `data` branch (one commit, overwritten each run). `seed/` is the starting copy used only if that branch is missing.
- `builder/` is the same builder that runs on the laptop (`python insights_builder.py`).
