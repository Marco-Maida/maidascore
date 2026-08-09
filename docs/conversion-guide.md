# Conversion Guide

How to convert MuseScore scores (`.mscz`) or compressed MusicXML files (`.mxl`) into
accessible MaidaScore partitures. Passing a `.mxl` file directly skips the manual
export step in MuseScore 4, since MaidaScore converts it to `.mscz` automatically.

---

## Command

```bash
# Full notation (colored circles + note names + sound table)
python3 generate_maidascore.py input.mscz output_prefix [part_index]

# Rhythm mode (simplified, no staff lines)
python3 generate_maidascore.py input.mscz output_prefix [part_index] --rhythm
```

### Parameters

| Parameter | Description |
|---|---|
| `input.mscz` | MuseScore 4 source file (`.mscz`), or compressed MusicXML (`.mxl`, converted automatically) |
| `output_prefix` | Prefix for output files |
| `part_index` | Which part to extract (0 = first, 1 = second, etc.) |
| `--rhythm` | Enable rhythm mode (simplified notation) |

### Output

| File | Description |
|---|---|
| `output_prefix.mscz` | Accessible MuseScore file |
| `output_prefix.pdf` | Final PDF — send this to the student |
| `output_prefix_svg-N_maidascore.svg` | Intermediate SVG (one per page) |

### Instrument note

The layout is optimized for the **transverse flute** (tessitura, register, density
per system). Any other instrument in the source `.mscz` can be processed by setting
`part_index` accordingly. For instruments with a very different range, some constants
in the `CONFIG` section of `generate_maidascore.py` may need to be adapted.

### Language of note names

Note names are available in **Italian** (Do Re Mi Fa Sol La Si, default) and **English**
(C D E F G A B) via the `--lang it|en` flag. Adding a new language is just a matter of
adding a new entry to the three `NOTE_NAMES_*` dictionaries near the top of
`generate_maidascore.py`. The layout logic is language-agnostic.

---

## Pipeline (6 steps)

1. **Note extraction** — `music21` analyzes the source `.mscz` (tie-aware, key signature,
   step + accidental)
2. **Single part** — extracts the requested part, preserving key signature
3. **SVG export** — MuseScore 4 exports the SVG of the single part
4. **Post-processor** — draws colored circles, note names, gray backgrounds, duration bars,
   positioned rests, measure numbers, enlarged clef, sound table. Maps notes to logical
   measures (0-N); multi-measure rests count as N measures
5. **PDF** — `cairosvg` converts SVG → PDF (multi-page merged with `pypdf`)
6. **Validation** — `lxml` verifies geometric properties end-to-end (see
   [validator.md](validator.md))

---

## Color scheme

| Note | Color | Hex | Text |
|---|---|---|---|
| Do | Red | `#E53935` | white |
| Re | Orange | `#FB8C00` | black |
| Mi | Yellow | `#FDD835` | black |
| Fa | Lime | `#64DD17` | black |
| Sol | Teal | `#00695C` | white |
| La | Blue | `#1E88E5` | white |
| Si | Purple | `#8E24AA` | white |

Open notes (whole/half) use white circles with darker colored borders for WCAG AA
contrast on white backgrounds.

---

## Rhythm mode

In rhythm mode (`--rhythm`), the output is simplified for rhythmic reading practice:

- Staff lines are **physically removed** from the SVG (not just hidden)
- Notes become plain circles on a single line
- Beams and flags are repositioned for clarity
- Accidentals are enlarged and placed below the note name
- Octave indicators (triangles) appear in the sound table
- Systems are centered on the page with uniform spacing
- Maximum 8 systems per page

---

## Key parameters

| Parameter | Value | Description |
|---|---|---|
| Uniform measure width | 3300px | All measures stretched to equal width |
| Beat width | 825px | Gray sector width (3300 / 4) |
| Circle radius | 110px | Note circle radius (scale 3.386) |
| Sound table row height | 175px | Height of sound table blocks |
| Ledger line extension | 60px per side | Extended to contain colored circles |

---

## Known edge cases and design decisions

### Rest positioning

Rests are positioned based on context:

- **Rest alone in a beat**: centered at 50% of the gray sector
- **Rest sharing a beat with notes**: placed at 8% of the sector (left side), notes are
  distributed at equal fractions of the remaining space
- **Rest at onset 0.0 with notes in the same beat**: the barline gap is **not** applied,
  preventing the rest from overlapping the first note

### Accidental removal

All accidentals are removed from the staff for visual clarity. The pitch information is
preserved in the MIDI/sound data, and the correct note name (including sharps/flats) is
shown in the sound table via the `NOTE_NAMES_IT_TAVOLA_SPLIT` mapping.

### Multi-measure rests (MMRest)

MMRests are handled as N individual measures for the purpose of gray sector layout and
sound table mapping. The `system_measure_indices` array is reconstructed contiguously to
ensure rests in measures without notes are assigned to the correct measure.

### Enharmonic spelling

Note names are derived from `step + accidental` extracted by `music21` (key signature
aware), not from a fixed pitch-to-name map. In F major, B♭ is spelled as "Si" (not
"A♯"). This works correctly for all key signatures.

### Barline X-coordinate formats

MuseScore exports some barlines with integer X coordinates (e.g. `"5279"`) and others
with decimals (e.g. `"5242.74"`). The post-processor tries three formats when matching
and repositioning barlines to avoid silent failures.

---

## Troubleshooting

### Empty sound table on the first page

If the source score has a rest measure at the beginning (e.g. a whole-note rest in
measure 0), `measure_offset = 0` is correct — the system covers measures 0-1, and the
rest is visible in the first gray sector. Do not apply a fallback that aligns
`measure_offset` to the first note's `measure_idx`, as this desynchronizes the SVG notes
from the sound table.

### "?" appearing in the sound table

This happens when `note_name` is `None`. Use `n0.get('note_name') or n0.get('name') or ''`
to fall back gracefully.

### Old SVG files being re-processed

Clean output files before regenerating: `rm -f prefix*_maidascore*.svg prefix*.pdf
prefix*.mscz`. Otherwise, old `_maidascore.svg` files may be picked up and re-processed,
producing `_maidascore_maidascore.svg` with corrupted output.
