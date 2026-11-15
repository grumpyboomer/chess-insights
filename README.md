# Chess Insights (online updater)

Analyses every chess.com (abdh2005) and Lichess (abdulrahmanhendawy20) game with Stockfish and publishes the Insights site on GitHub Pages.

- **Runs by itself every 30 minutes.** Only new games are fetched and analysed.
- **Update now:** Actions → *Update Chess Insights* → **Run workflow** (or the ↻ Update button on the site).
- **Site:** `https://<username>.github.io/chess-insights/` (UX version at `ux.html`).
- Saved engine analysis lives on the `data` branch (one commit, overwritten each run). `seed/` is the starting copy used only if that branch is missing.
- `builder/` is the same builder that runs on the laptop (`python insights_builder.py`).

## The game board

One board for review and analysis, built like the analysis boards on Lichess and chess.com (`ChessBoard` and `viewerCard` in the two templates).

- **Review**: step through the game with the keys, the move list or the evaluation graph. Each move has its class, the better move as a green arrow, clocks and a coach line.
- **Your own moves**: drag any piece (or tap it, then a square). The move starts a side line that is listed under the game move it replaces; lines can branch and be deleted. Right-drag draws arrows.
- **Stockfish 19 in the browser** (`builder/sf/`): a switch, evaluation, depth, one to five lines whose moves can be clicked or hovered for a preview, and a "Deeper" button. Every move you try is graded with the review's rules.
- **Play from here**: play the position out against Stockfish at four levels; your moves are graded as you go.
- **Fix your mistakes**: goes back to each of your mistakes in the game and asks for a better move.
- **Performance**: a game rating ("played like") for both players, phase accuracies, where the game ranks among your games, and the biggest swing. Constants: `PERF_K`.

Elsewhere: the Trainer uses the same board; Highlights has a checkmate gallery and a longest-think record.
