# Chess Insights (online updater)

Analyses every chess.com (abdh2005) and Lichess (abdulrahmanhendawy20) game with Stockfish and publishes the Insights site on GitHub Pages.

- **Runs by itself every 30 minutes.** Only new games are fetched and analysed.
- **Update now:** Actions → *Update Chess Insights* → **Run workflow** (or the ↻ Update button on the site).
- **Site:** `https://<username>.github.io/chess-insights/` (UX version at `ux.html`).
- Saved engine analysis lives on the `data` branch (one commit, overwritten each run). `seed/` is the starting copy used only if that branch is missing.
- `builder/` is the same builder that runs on the laptop (`python insights_builder.py`).

## In the game review

- **Performance**: each game gets a game rating ("played like") for both players, phase accuracies, and where the game ranks among your games. The game rating is your own rating moved by how much more accurately you played than your opponent, compared with what the two ratings predict; the scale is fitted per time control so the gap between the two game ratings predicts the result like a real rating gap (constants `PERF_K` in the templates).
- **Analysis board**: "Analyse here", or just pick up a piece on the review board. Stockfish 19 runs in the browser (`builder/sf/`, see its README for sources and licences), shows its top three lines and grades every move you try with the review's rules.
