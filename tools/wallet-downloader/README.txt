POLY WALLET ARCHIVE

1. Extract this folder anywhere on your Windows PC.
2. Double-click START_DOWNLOAD.cmd (Python 3.10+ required).
3. The script creates D:\Phavi\PolyWalletResearch.
4. Keep the window open. To resume after interruption, run the same launcher again.
5. Upload D:\Phavi\PolyWalletResearch\Research_Summary.zip to this chat.

Public target wallet: 0x31393e2f8333d4a6e2e31ca6424defe8b6cb9e2e
No wallet credentials, private keys or trading access are requested.

Outputs: raw compressed response pages, SQLite records, saved cursors, summary.json,
REPORT.md and a small Research_Summary.zip. The full dataset remains on your PC.
Reports are refreshed every 100 pages and when the script exits normally or with Ctrl+C.
The downloader does not delete earlier records. Repeated pages/cursors stop the run.
API restrictions or unavailable endpoints are reported without pretending the download completed.
API history coverage is not proof of complete blockchain or maker/taker coverage.

Manual report refresh:
  py -3 wallet_download.py --out "D:\Phavi\PolyWalletResearch" --report-only

This archive has not yet downloaded data on your PC and is not a completed wallet study.
