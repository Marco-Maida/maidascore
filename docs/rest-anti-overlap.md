# Rest Anti-Overlap — Design Notes

How MaidaScore positions rests to avoid overlapping notes, removes unnecessary
accidentals, and extracts the correct part from multi-instrument scores.

---

## Rest positioning logic

MaidaScore uses gray sectors (one per beat, 825px wide) where notes and rests are placed.
Rests are positioned based on three cases:

### Case 1: Rest alone in the sector

- **Target position**: center of the sector (50%)
- Formula: `beat_num * 0.25 + 0.125`
- Applies to: quarter rests when no notes share the same beat

### Case 2: Rest sharing the sector with notes

- **Target position**: 8% of the sector (left side)
- Formula: `beat_num * 0.25 + 0.08 * 0.25`
- Notes are distributed at equal fractions: `(j+1)/(n_groups+1)` of the sector
- Minimum distance from rest to first note: 209px (> 165px threshold = circle radius
  110px + half-rest 65px)

### Case 3: Barline gap not applied when notes are present

The barline gap (beat width × 0.3 ≈ 247px) shifts elements right to avoid the left
edge. If applied to a rest at onset 0.0 when notes share the same beat, the rest would
overlap the first note. The condition `if not notes_in_same_beat` prevents this.

---

## Key parameters

| Parameter | Value | Description |
|---|---|---|
| Uniform measure width | 3300px | All measures stretched equally |
| Beat width | 825px | Gray sector width (3300 / 4) |
| Circle radius | 110px | Note circle radius |
| Half-rest width | ~65px | Eighth rest at scale 3.386 |
| Min rest-to-note distance | 195px | radius + half-rest |
| Rest fraction (with notes) | 0.08 | 8% of sector, fixed |
| Rest fraction (alone) | 0.50 | Center of sector |

---

## Part extraction from multi-instrument scores

MaidaScore extracts a single part from multi-instrument `.mscz` files:

1. `music21` parses the source and extracts the part at `part_index`
2. A new score with a single `stream.Part` is created
3. Exported to MusicXML → converted to `.mscz` via MuseScore 4
4. The original `.mscz` is **not** modified by removing staves (MuseScore 4 segfaults
   when staves are removed from an existing file)

**Caveat**: the intermediate `.mscz` created by `music21` has
`movementTitle="Music21 Fragment"`, which is replaced with a custom title.

---

## Accidental removal

`music21` adds ~71 redundant naturals when exporting to MusicXML (one for each natural
note following an alteration in the same measure). For dyslexic learners, accidentals
on the staff are visual clutter — the sound table already shows the correct note name.

Removal is done in two places:

- **In the intermediate `.mscx`**: after `music21` → `.mscz` conversion, all
  `<Accidental>` elements are removed via regex, then re-compressed
- **In the final SVG**: `re.sub` removes all `path class="Accidental"` elements

MuseScore may still render ~6 residual accidentals from `<pitch>`/`<tpc>` data even
without explicit `<Accidental>` elements — the SVG removal eliminates these definitively.

---

## Rest onset extraction

In `extract_notes_from_mscz()`:

1. Iterate `Measure → voice → Chord/Rest` (filtered by `part_index`)
2. For each `Rest`: collect `{onset, duration_type}` into a `rests` list
3. Accumulate duration in beats to calculate onset (same method as notes)
4. Reset onset to 0 for each voice (do not accumulate across voices)
5. Return `rests` in `note_info`

In the positioning loop:

1. For each `path class="Rest"` with a transform matrix: extract `tx, ty`
2. Determine `measure_idx` from the nearest note (X matching)
3. Assign onset from `rests_by_measure[measure_idx]` in order
4. Calculate `beat_num = int(rest_onset)`
5. Search for notes in the same beat: same `measure_idx` and same `int(onset)`

---

## Gotchas

1. **Barline gap**: applied to `target_pos < 0.1`, shifts 247px right. Rests at 8%
   (target_pos=0.02) would be pushed onto notes. Exclude with
   `not notes_in_same_beat`. This exclusion also applies to rests at onset 0.0, not
   just `target_pos < 0.1`.

2. **`notes_in_same_beat` scope**: defined only inside the Rest block. Initialize to
   `[]` before the block to avoid `NameError` in the fallback path.

3. **`rest_assignment_counter`**: one rest per system is assigned in order of
   appearance. If two rests are in the same measure, the first gets `onset[0]`, the
   second gets `onset[1]`.

4. **MuseScore renders rests as `path class="Rest"` with a transform matrix** (after
   `.mscx` modification with `spatium=2.0`). The original (pre-modification) has
   absolute coordinates in the `d` attribute.

5. **Always clean files before regenerating**: `rm -f prefix*_maidascore*.svg
   prefix*.pdf prefix*.mscz` — otherwise old `_maidascore.svg` files get re-processed.

6. **`music21` adds redundant naturals**: systematic, happens on every multi-instrument
   file converted via `music21` → MusicXML → MuseScore. The removal is permanent in the
   generator and applies automatically.

7. **Do not remove staves from `.mscx`**: MuseScore 4 segfaults. Use `music21` to
   create a single-part `.mscz` from scratch instead.
