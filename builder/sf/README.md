# sf/ — what the analysis board loads in the browser

These files are third-party and unmodified. The site talks to the engine only through UCI text messages to a web worker.

| File | What | From | Licence |
|---|---|---|---|
| `stockfish-19-lite-single.js`, `stockfish-19-lite-single.wasm` | Stockfish 19, single-threaded "lite" WebAssembly build (Stockfish.js 19.0.1) | https://github.com/nmrugg/stockfish.js/releases/tag/v19.0.1 | GPL-3.0, see `COPYING-stockfish.txt`. Source: https://github.com/nmrugg/stockfish.js (based on https://github.com/official-stockfish/Stockfish) |
| `chess.js` | chess.js 0.10.3: legal moves and notation | https://github.com/jhlywa/chess.js/tree/v0.10.3 | BSD-2-Clause, see `LICENSE-chess.js.txt` |

SHA-256 as downloaded:

```
ccb130d7ff29408531c47d859a49f91d59d4f482d05a53eb1a3231a4f624c91a  stockfish-19-lite-single.js
ee4f115bdb2d32cadf96cada104a05b5ec257d7ad6f57582190b49bc71f096de  stockfish-19-lite-single.wasm
1a6803b787dc4a6f71aabdd0b010cee7bf0fc70ce032f5ed8d7738684d431bfc  chess.js
```

The single-threaded build is used because GitHub Pages cannot send the headers the multi-threaded one needs.
The games themselves are still analysed by the full desktop Stockfish 19 in the updater; this lite build is only for the live analysis board.
