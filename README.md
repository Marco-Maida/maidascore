# MaidaScore

**Accessible music notation generator for dyslexic learners.**

MaidaScore converts standard MuseScore scores (`.mscz`) or compressed MusicXML files
(`.mxl`) into simplified, color-coded partitures designed for students with dyslexia
and other reading difficulties. Instead of traditional notation on a five-line staff,
notes are displayed as **colored circles with note names** — making pitch
identification intuitive and reducing visual crowding.

Note names are available in **Italian** (Do Re Mi Fa Sol La Si) and **English**
(C D E F G A B); pass `--lang it` (default) or `--lang en`.

---

## What it produces

Each output page contains:

- **Colored circles** — one color per pitch class (Do/Re/Mi/Fa/Sol/La/Si), with the
  note name printed inside each circle
- **Gray quarter-backgrounds** — alternating light/dark gray bands behind each beat,
  providing a visual rhythm grid
- **Duration bars** — colored rectangles showing how long each note lasts
- **Positioned rests** — dashed cells with "pausa" label, aligned under the rest symbol
- **Sound table** — a row of colored blocks beneath each system, mirroring the notes
  above with full note names and octave indicators (Italian or English)
- **Octave indicators** — small triangles showing register changes

### Rhythm mode (`--rhythm`)

A simplified view focused on **rhythm only**: staff lines are removed, notes become
plain circles on a single line, with beamed flags, enlarged accidentals, and the sound
table. Ideal for students who need to focus on rhythmic reading before pitch.

### Instrument support

MaidaScore is **optimized for the transverse flute**: layout parameters (register
range, notes-per-system density, octave-triangle placement) are tuned to the flute's
tessitura and to the reference didactic material.

The system is however **generic**: it works with any instrument contained in the
source `.mscz` — clarinet, violin, guitar, voice, etc. Use the `part_index` argument
to select which part to process. For instruments whose range differs substantially
from the flute, some layout constants (see the `CONFIG` section at the top of
`generate_maidascore.py`) may need adjustment.

### Language of note names

Note names are available in **Italian** (Do Re Mi Fa Sol La Si, default) and **English**
(C D E F G A B) via the `--lang it|en` flag. Adding a new language is just a matter of
adding a new entry to the three `NOTE_NAMES_*` dictionaries near the top of
`generate_maidascore.py` — the layout logic is language-agnostic.

---

## Quick start

### Prerequisites

| Dependency | Purpose | License |
|---|---|---|
| [MuseScore 4](https://musescore.org/) | Export SVG from `.mscz` (required for `.mxl` input) | GPL-3.0 |
| `xvfb` | Virtual framebuffer for headless MuseScore | MIT |
| [Atkinson Hyperlegible](https://fonts.google.com/specimen/Atkinson+Hyperlegible) | Accessible font for output | OFL |
| Python ≥ 3.10 | Runtime | — |

### Install

```bash
# System dependencies (Debian/Ubuntu)
./install_system_deps.sh

# Python dependencies
pip install -r requirements.txt
```

### Usage

```bash
# Full notation, Italian note names (Do Re Mi Fa Sol La Si)
python3 generate_maidascore.py input.mscz output_prefix 0

# Full notation, English note names (C D E F G A B)
python3 generate_maidascore.py input.mscz output_prefix 0 --lang en

# Rhythm mode (simplified, no staff lines)
python3 generate_maidascore.py input.mscz output_prefix 0 --rhythm

# Rhythm mode + English note names
python3 generate_maidascore.py input.mscz output_prefix 0 --rhythm --lang en
```

**Arguments:**
- `input.mscz` — MuseScore source file, **or** a compressed MusicXML file (`.mxl`);
  `.mxl` files are converted to `.mscz` automatically (passing `.mxl` directly skips
  the manual export step in MuseScore 4)
- `output_prefix` — prefix for output files
- `part_index` — which part to extract (0 = first part, default 0)
- `--rhythm` — enable rhythm mode
- `--lang` — note-name language: `it` (default) or `en`

**Output:**
- `output_prefix.mscz` — accessible MuseScore file
- `output_prefix.pdf` — final PDF (send this to the student)
- `output_prefix_svg-N_maidascore.svg` — intermediate SVG files (one per page)

---

## Color scheme

| Note | Color | Text |
|---|---|---|
| Do | 🔴 `#E53935` red | white |
| Re | 🟠 `#FB8C00` orange | black |
| Mi | 🟡 `#FDD835` yellow | black |
| Fa | 🟢 `#64DD17` lime | black |
| Sol | 🩵 `#00695C` teal | white |
| La | 🔵 `#1E88E5` blue | white |
| Si | 🟣 `#8E24AA` purple | white |

Colors and text colors are chosen for **WCAG AA contrast** (≥ 3:1 for large text).
Open notes (whole/half notes) use white circles with darker colored borders for
readability on white backgrounds.

---

## Architecture

MaidaScore runs a 6-step pipeline:

1. **Note extraction** — `music21` analyzes the source `.mscz` (tie-aware, key
   signature, step + accidental)
2. **Single part** — extracts the requested part, preserving key signature
3. **SVG export** — MuseScore 4 exports the SVG of the single part
4. **Post-processor** — draws colored circles, note names, gray backgrounds, duration
   bars, positioned rests, sound table, logical measure mapping, and (in rhythm mode)
   removes staff lines
5. **PDF** — `cairosvg` converts SVG → PDF (multi-page merged with `pypdf`)
6. **Validator** — `lxml` verifies geometric properties end-to-end

For technical documentation, see the [`docs/`](docs/) directory.

---

## Examples

The [`examples/`](examples/) directory contains a sample input score and its
generated output in all four modes:

| File | Mode | Note names | Description |
|---|---|---|---|
| `example_score.mscz` | — | — | Source MuseScore file used to generate the examples |
| `example_full_notation_it.pdf` | Full notation | Italian (Do Re Mi) | Complete colored-circle partiture |
| `example_full_notation_en.pdf` | Full notation | English (C D E) | Complete colored-circle partiture |
| `example_rhythm_mode_it.pdf` | Rhythm mode | Italian (Do Re Mi) | Rhythm-only view, no staff lines |
| `example_rhythm_mode_en.pdf` | Rhythm mode | English (C D E) | Rhythm-only view, no staff lines |

Reproduce any example with:

```bash
cd examples
python3 ../generate_maidascore.py example_score.mscz example_full_notation_it
python3 ../generate_maidascore.py example_score.mscz example_full_notation_en --lang en
python3 ../generate_maidascore.py example_score.mscz example_rhythm_mode_it --rhythm
python3 ../generate_maidascore.py example_score.mscz example_rhythm_mode_en --rhythm --lang en
```

---

## Limitations

- Optimized for **4/4, 3/4, 2/4, and 6/8 time signatures**; other meters may produce suboptimal layout
- **Tuplets** are not supported
- Layout constants are tuned for **transverse flute**; other instruments may require
  adjusting parameters in the `CONFIG` section of `generate_maidascore.py`
- The generator is a single-file monolith (~5500 lines); modularization is planned for
  a future 2.0 release
- No automated test suite yet; validation relies on the SVG validator and manual review
- Tested on Linux (Debian 12); macOS/Windows support is untested

---

## Third-party licenses

MaidaScore uses the following open-source libraries. It **links to** or **invokes** them
at runtime but does not include their source code in this repository.

| Library | License | Compatible with GPL-3.0? |
|---|---|---|
| music21 | BSD-3-Clause | ✅ Yes |
| lxml | BSD-3-Clause | ✅ Yes |
| CairoSVG | LGPL-3.0 | ✅ Yes |
| pypdf | BSD-3-Clause | ✅ Yes |
| MuseScore 4 | GPL-3.0 | ✅ Yes |
| xvfb | MIT | ✅ Yes |
| Atkinson Hyperlegible | OFL | ✅ Yes |

The Atkinson Hyperlegible font is **not included** in this repository. It is downloaded
by `install_system_deps.sh` or can be installed manually from Google Fonts.

---

## License

Copyright © 2026 Marco Maida. Licensed under the **GNU General Public License v3.0 or
later** (GPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

---

## Disclaimer

MaidaScore is an independent project and is **not affiliated with, endorsed by, or
sponsored by** MuseScore, Braille Institute, or any commercial music education system.
"MuseScore" is a trademark of its respective owners. "Atkinson Hyperlegible" is a
trademark of the Braille Institute.
