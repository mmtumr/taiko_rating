# taiko_rating

太鼓の達人 player rating prototype.

This is a static browser app for:

- fetching score data from the Kinoko API
- joining score records with local chart constants
- calculating a CHUNITHM-like Taiko Rating
- rendering chart previews generated from local ESE/TJA files
- rendering and exporting a share image

The user-facing score is the classic comprehensive B20 Rating. A monotonic
piecewise curve calibrated from historical red-pass Dan requirements converts
that Rating into a recommended traditional chart constant. Different
difficulties of the same song are independent B20 candidates.

Run a local static server before opening the app, because the browser needs to
fetch `data/chart_data.json` and `data/local_chart_previews.json`:

```bash
python -m http.server 8765
```

Then open `http://127.0.0.1:8765/`.

GitHub Pages can serve this repo directly as a static site. In the repository
settings, enable Pages with source `Deploy from a branch`, branch `main`, folder
`/ (root)`.

## V4 ability profile

The comprehensive Rating remains the absolute strength indicator. The six numeric
abilities use the local v4 encoder catalog embedded in `chart_data.json`:
stamina, reading, burst, accuracy, rhythm, and complex. Reading models native
BPM/#SCROLL visual flow, screen information load, abrupt speed changes, and
simultaneous mixed-speed notes. The catalog covers
all Oni/Edit/Hard charts plus generated Normal reference charts
`DARK EX MACHINA♡` and `幽玄之乱`; the Easy/Normal rows for these two songs
remain excluded from Rating while their Hard and higher rows are included.
Sakura v2 is retained only as a compatibility fallback. Each player ability is
an independent simple-average B20: the top twenty single-chart values for that
specific axis are averaged without additional decay weights.

The radar polygon is a relative profile centered at 50. Its baseline is the
median and MAD of v4 charts within `main constant +/- 0.5`; it is an ability
tendency, not a player percentile. Lower difficulties retain a feature-based
fallback. Historical Nijiiro 2020-2024 dojo medians provide the displayed Dan
reference.

The v4 catalog is generated in the sibling encoder project and then embedded
when chart data is rebuilt:

```bash
python ../encoder/scripts/generate_v3_full_catalog.py --checkpoint ../encoder/checkpoints/encoder_custom_abilities_v4_reading_density/best.pt --output data/v4_reading_abilities.json --model-name encoder_custom_abilities_v4_reading_density --overwrite
python scripts/build_chart_data.py
```

For charts without spreadsheet/community constants, `build_chart_data.py`
currently retains `encoder_chart_stats_ordinal.json`, the established ordinal
const checkpoint chosen before the next const-model tuning round.

Regenerate the vendored v2 constants with:

```bash
python scripts/build_v2_constants.py
```
