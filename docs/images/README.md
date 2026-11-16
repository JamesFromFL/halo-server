# Halo Server screenshots

These images show the unmodified production web interface bundled in this repository.
They were captured with Chromium on 2026-11-15 using synthetic names, device observations,
and configuration. They are examples of the interface, not evidence of a live installation
or physical device behavior. No real account, camera imagery, pairing code, credential,
or private network address is present.

The capture fixture serves the static bundle on loopback and supplies read-only sample
API responses. It rejects every non-GET request and blocks browser requests to other
origins. It does not start the Halo daemon, contact Ring, publish HomeKit accessories,
or issue any device command. Scrolling and the app's normal system appearance preference
are used to frame each view; application markup and styles are not altered.

| Image                           | View                                                        | Appearance | Size        |
| ------------------------------- | ----------------------------------------------------------- | ---------- | ----------- |
| `halo-server-overview.webp`     | Connection health and device counts                         | Dark       | 1280 × 900  |
| `halo-server-devices.webp`      | Alarm, sensors, keypad, smart lighting, and Chime inventory | Light      | 1280 × 800  |
| `halo-server-camera.webp`       | Camera privacy and HomeKit Secure Video settings            | Dark       | 1280 × 1210 |
| `halo-server-integrations.webp` | Halo Bridge pairing and separate camera selection           | Light      | 1280 × 1240 |

The images use lossless WebP encoding at the original screenshot dimensions. The Halo
logo comes directly from the web application's own assets.

## Regenerate

Use the pinned CI toolchain, Node 26.12.1 with npm 12.2.1, and a Python environment
with Pillow. From the repository root:

```bash
cd web-ui
npm ci --ignore-scripts --no-audit --no-fund
npm run build
npx playwright install chromium
cd ..

HALO_SCREENSHOT_OUTPUT=/tmp/halo-server-readme node docs/images/capture.mjs

python - <<'PY'
from pathlib import Path
from PIL import Image

source = Path('/tmp/halo-server-readme')
target = Path('docs/images')
for image in sorted(source.glob('halo-server-*.png')):
    with Image.open(image) as screenshot:
        screenshot.save(target / f'{image.stem}.webp', format='WEBP', lossless=True, method=6)
PY
```

If Playwright browsers are stored outside its default cache, set
`PLAYWRIGHT_BROWSERS_PATH` when running the capture script. Local fonts can change text
metrics; inspect the resulting images before committing them. `capture-report.json` in
the temporary output directory records browser errors and every fixture request. A
successful capture exits without errors, and all recorded requests use GET.
