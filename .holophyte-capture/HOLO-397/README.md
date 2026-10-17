# General settings capture evidence

Captured from `68175a5c68fa0debbcf140103583e80c2b4a439c` on 2026-10-17,
using the seeded host daemon and Chromium. These repository copies replace
the inaccessible media-server links in PR #690. Open the links below directly
from the branch; they require no separate media-server access.

| Capture | SHA-256 |
| --- | --- |
| [Dawn, 1440 × 900](01-general-dawn.png) | `3ff81f8eeb0e884c6f114a2f95fcc889a2bfe49458fde1ca53a16ff16d1f875b` |
| [Night, 1440 × 900](02-general-night.png) | `58e2d041f6de22e2c3ca325f3221e243201333286097b00b0fd0af07ac44d71c` |
| [Workers changed from 2 to 3, 1440 × 900](03-general-changed.png) | `83dfba64ab4015a3c562539f69dabd66de6a14ce0a2423fa22602d5e98cfbbe3` |
| [Stacked rows, 390 px wide (full page)](04-general-390.png) | `1ca95d35e7e2fff2fcd3f17baa95862dbfaaddb84333917a049e5a5485295691` |

Reproduce from the repository root (choose an empty output directory):

```sh
HOLOPHYTE_TICKET=HOLO-397 python3 holophyte/capture_playwright.py --dir console/e2e --config console/playwright.config.ts --boot "python3 -m tests.console_fixture --build 'bun --cwd=console run build' bun --cwd=console run capture" /tmp/holo-397-evidence
```

The capture opens `/next.html#/settings/general` with demo's merge mode `pr`,
approval `auto` and workers `2`. It captures Dawn and Night, raises workers
to `3`, then captures the changed row and the same page at 390 px. The spec
checks that the unsaved region appears and the mobile page has no horizontal
overflow. Appearance remains available alongside the project settings.
