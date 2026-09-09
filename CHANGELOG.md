# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.4] - 2026-08-13

### Fixed
- **Half rest (pausa di minima) invisible on staff**: MuseScore 4 renders
  half rests (2-quarter / half-measure rests) with an SVG path starting
  with `M0,-3.3125`, which the code mistook for a 16th rest (also starting
  with `M0,`) and shrank to 33% scale — making the rest glyph invisible on
  the staff. The type-aware rest matching also assumed half rests are never
  rendered in the SVG (an outdated assumption from an earlier MuseScore 4
  bug). Fix applied in three points: (1) `enlarge_rest` now identifies half
  rests via `M0,-3.3125` before the 16th-rest check and applies the correct
  scale; (2) type-aware matching matches `M0,-3.3125` SVG paths as half
  rests instead of always returning `type_match=False`; (3) the clone
  step only clones unmatched rests (matched half rests are no longer
  duplicated). Result: 20/20 rests visible on the staff (was 16/20), zero
  false clones.

## [1.0.3] - 2026-08-12

### Fixed

#### Key signature (armatura) — detection & rendering

- **Key signature detection for C-major scores**: `extract_key_sig_changes`
  no longer registers `None` for every measure when a score has no explicit
  `<KeySig>` elements (C-major / no key signature). Previously, `prev_ks`
  stayed `None` forever, causing every measure to be recorded as a key-signature
  change — which forced `make_accessible_mscz` to insert a LayoutBreak after
  every measure, producing 1 measure per system instead of 2. Now, when no
  `<KeySig>` is found, the key is treated as 0 (C major) and only genuine
  changes are registered. A `first_ks_seen` flag distinguishes "no KeySig
  found yet" from "KeySig found and persists".
- **Per-measure key signature tracking**: `extract_notes_via_music21` now
  returns `key_sigs_per_measure` (key signature active in each measure),
  enabling correct accidental rendering across key changes mid-score.
- **Intermediate key signature reconstruction**: `extract_single_part_mscz`
  now accepts a `key_sig_changes` dict and inserts `key.KeySignature` at the
  start of each measure where the key changes (measure > 0). Previously,
  music21 did not detect intermediate KeySignatures (offset=0.0 bug), so the
  reconstructed single-part score lost all mid-score key changes.
- **Cancel naturals for key changes (MusicXML injection)**: music21 does not
  export `<cancel>` tags in MusicXML, so MuseScore 4 did not render
  cancellation naturals when switching from sharps to flats (or vice versa) —
  residual sharps stayed visible. Now `<cancel location="left">` tags with
  the previous fifths value are injected manually into every non-first `<key>`
  block in the exported MusicXML.
- **KeySig break on system change**: `make_accessible_mscz` now forces a
  LayoutBreak when the key signature changes mid-system, in addition to time
  signature changes. The key changes are read from the original file via
  `extract_key_sig_changes` (the reconstructed .mscx loses intermediate
  changes).
- **MuseScore 4 flats rendering bug (workaround)**: MuseScore 4 renders all
  KeySig accidentals as sharps in the SVG when the .mscz is reconstructed via
  music21, and does not render cancellation naturals at all. Workaround:
  all KeySig elements are removed from the SVG and replaced with correct
  path data (sharp/flat/natural glyphs extracted from the original MuseScore
  SVG) for each system based on the correct key signature.
- **Per-system key signature labels**: key signature labels in the tavola
  sonora are now computed per-system (not just the initial key), using
  `key_sig_changes_dict` and system-to-measure mapping. Each system shows
  the accidentals for its active key.
- **KeySig on all staves of a system**: key signatures are now rendered on
  all staves (pentagrams) of a system, not just the first. StaffLines are
  grouped into pentagrams (groups of 5 lines, gap < 400px) and then into
  systems (gap < 1000px), supporting multi-staff instruments (e.g. piano
  with brace).
- **Cancellation natural ordering**: when the key changes, the order is now
  correct: accidentals that REMAIN (in their original position) are drawn
  first, then cancellation naturals for the removed accidentals. Covers all
  4 cases: sharps→sharps (fewer), sharps→flats, flats→sharps, flats→flats
  (fewer).
- **KeySig overlap with TimeSig (cancellation naturals)**: the width
  calculation for scaling the key signature now includes cancellation
  naturals for the current system (not just the initial key), preventing
  overlap between the TimeSig and cancellation naturals.
- **KeySig-to-system Y mapping (pre-stretch)**: KeySig original Y
  coordinates are pre-stretch and do not match post-stretch system_tops.
  KeySigs are now grouped by Y (pre-stretch) and assigned to systems by
  order of appearance, not by nearest post-stretch Y. Each system can have
  a variable number of KeySigs (0, 1, 2, 3).

#### System layout — architecture (Single Source of Truth)

- **`build_system_layout` (new function, Fable 5 architecture)**: the
  measure-to-system partition is now computed ONCE from barline counting
  (primary source: direct observation of MuseScore's layout), not from
  time-signature-based prediction (which is not deterministic — MuseScore
  does not always force a break on time-sig change). This Single Source of
  Truth is consumed by `sys_measure_ranges`, `_sys_to_global_idx`, and
  `draw_tavola_sonora` instead of each recalculating with local heuristics.
  Fallback to the old logic (equalized_measures + MMRest + UNIFORM) if
  `system_layout` is unavailable.
- **`process_svg` refactored**: the SVG post-processor now builds
  `_system_layout` once at the start and passes it down to all consumers,
  eliminating 3 separate recalculations of the same partition.

#### Tavola sonora — rhythm mode time signature rendering

- **Time signature as fraction**: in rhythm mode, the time signature is now
  rendered as a proper fraction (numerator above, line, denominator below)
  using `font-family: Atkinson Hyperlegible`, instead of inline text
  ("4/4"). The fraction fills the full vertical height of the staff +
  tavola sonora block.
- **Accidentals in vertical layout (rhythm mode)**: accidentals are now
  arranged vertically (one below the other) to the left of the time
  signature fraction, matching standard music notation.
- **Per-system time signature changes**: subsequent systems show accidentals
  only if they changed from the previous system, and the time signature only
  if it changed. Previously, all systems showed the same combined text.
- **Intra-system time signature changes as small fraction**: when the time
  signature changes mid-system, the new TS is rendered as a small inline
  fraction (scale 0.25) instead of large text.

#### Layout & sizing

- **AVAILABLE_WIDTH uses UNIFORM_MUSIC_START**: the width available for
  clef + key signature + time signature is now measured from
  `UNIFORM_MUSIC_START` (start of grey sectors), not `MUSIC_START_X`
  (center of first note, 2650). This follows Marco's directive: do not
  move the grey sectors, shrink the symbols instead.
- **KeySig scale gap corrected**: the gap between clef, KeySig, and TimeSig
  is now 55px (was 40px, leaving 11px of overlap with the grey sector).
- **TIMESIG_WIDTH_REF corrected**: TimeSig reference width at scale 2.0 is
  now 334.0 (was 283, underestimated by 18%). STAGGER_REF is now 180.0
  (measured: 103.6 / 1.1429 × 2.0 = 181.3).

#### Validator

- **Validator prefix matching**: `validate_maidascore.py` now matches SVG
  files with `startswith(prefix_base + '_svg')` instead of
  `startswith(prefix_base)`, preventing false positives when one prefix is
  a substring of another (e.g. `Canzon_v24` matching `Canzon_v24_r`).

### Changed
- `extract_single_part_mscz` signature: now accepts `key_sig_changes`
  parameter (dict {measure_idx: n_sharps}) for intermediate key
  reconstruction.
- `make_accessible_mscz` signature: now accepts `key_sig_changes` parameter
  (set of measure indices) to force LayoutBreak on key changes.
- `process_svg` and `draw_tavola_sonora`: now accept `key_sig_changes_dict`
  for per-system key signature rendering.
- `main()`: extracts key signatures from the original file before
  `extract_single_part_mscz` and `make_accessible_mscz`, passing the dict
  to both functions.

## [1.0.2] - 2026-08-12

### Fixed
- **Secondary beams for dotted-eighth + sixteenth**: secondary beams (beam
  secondario) are now correctly created for the dotted-eighth + sixteenth figure
  (croma puntata + semicroma) in both standard and rhythm modes. The secondary
  beam is thin (31px, matching MuseScore's style) and spans from the midpoint
  between the two notes to the right edge of the primary beam. Previously the
  code failed to find the correct primary beam because it matched beams from
  other systems with similar X coordinates (missing Y-system filter), and the
  secondary beam was drawn with the same thickness as the primary (47px instead
  of 31px), making it invisible.
- **Secondary beams for eighth + sixteenth + sixteenth**: secondary beams are
  now created for the 8th + 16th + 16th figure (croma + semicroma + semicroma),
  connecting the two sixteenth notes. This is handled in a separate code block
  independent of the dotted-eighth logic, so measures without dotted-eighths are
  also processed. A filter ensures the secondary beam is only created when the
  two sixteenths are preceded by an eighth (not in runs of 4+ sixteenths).

## [1.0.1] - 2026-08-09

### Fixed
- **Rhythm mode hook direction**: eighth-note flags (uncini) now curve downward
  when the stem points up, following standard music-notation convention. Previously
  the flag inherited the MuseScore stem-down path, producing an upward-curving hook
  on upward stems.
- **Rhythm mode hook color**: the flag (uncino) now inherits the exact color of its
  stem, read directly from the stem's `stroke` attribute after the stem-coloring pass.
  Previously the color was matched by note X/Y coordinates, which could mismatch in
  rhythm mode (where note Y is remapped to the middle line) and assign the wrong
  color to the flag.
- **Rhythm mode stems forced upward**: in rhythm mode (`--rhythm`), all stems are
  now drawn upward regardless of the original MuseScore direction. This simplifies
  the layout and ensures consistent spacing for rhythmic reading practice.
- **Key-signature accidentals shown on the staff**: notes altered by the key
  signature (e.g. F♯, C♯ in G major) now display the accidental (♯/♭) on the
  staff next to the notehead, not only under the note name in the sound table.
  Previously only passing accidentals (not key-signature ones) were drawn on the
  staff.
- **Accidental positioning relative to stem direction**: accidentals on the staff
  are now placed ABOVE the circle when the stem points down, and BELOW the circle
  when the stem points up — following standard engraving convention. Accidentals
  are drawn after the Y-stretch with per-system circle matching, preventing them
  from landing on the wrong note or system. When a note sits directly below
  another, the accidental uses a reduced font-size and tighter offset to stay
  attached to the correct circle. In rhythm mode, accidentals on the staff are
  suppressed (they remain only under the note names in the blocks).
- **Enharmonic Y-correction**: notes whose MuseScore rendering uses a different
  enharmonic spelling (e.g. D♯ drawn as E♭ in the 4th space) are corrected to their
  true staff position based on the actual step+octave from music21, using the
  system's line geometry.

### Changed
- **Documented note-value range**: the layout is optimized for durations down to the
  sixteenth note (semicroma). Shorter values (biscrome, semibiscrome) are not
  guaranteed to render correctly. This is now stated in the README and the header
  docstring of `generate_maidascore.py`.

## [1.0.0] - 2026-08-09

First public release.

### Added
- **Full notation pipeline**: MuseScore `.mscz` (or compressed MusicXML `.mxl`) → accessible PDF with colored circles, note names, gray quarter-backgrounds, duration bars, positioned rests, and sound table. Passing `.mxl` directly skips the manual export step in MuseScore 4 — MaidaScore converts it to `.mscz` automatically.
- **Rhythm mode (`--rhythm`)**: simplified rhythmic notation without staff lines, with beamed flags, enlarged accidentals, and octave indicators. Ideal for rhythmic reading practice before pitch.
- **Multilingual note names (`--lang it|en`)**: note names available in Italian (Do Re Mi Fa Sol La Si, default) and English (C D E F G A B). The layout logic is fully language-agnostic; adding a new language is just a matter of adding a new entry to the three `NOTE_NAMES_*` dictionaries.
- **Copyright footer on every page**: each generated PDF page carries a footer centered at the bottom reading "generated by MaidaScore — © 2026 Marco Maida", in light gray (Atkinson Hyperlegible, 110px). Works in both standard and `--rhythm` modes.
- **SVG validator (`validate_maidascore.py`)**: independent lxml-based verification of 7 geometric properties end-to-end.
- **Color scheme optimized for WCAG AA contrast**: mixed black/white text on colored backgrounds (black on light colors Do/Re/Mi/Fa, white on dark colors Sol/La/Si).
- **Octave indicators (triangles)** in the sound table, showing register changes (1 triangle down/up for one-octave changes, 2 for two-octave changes).
- **Ledger lines extended** to contain colored circles.
- **Automatic key signature extraction** and enharmonic spelling (key-signature-aware: in F major, B♭ is spelled as "Si", not "A♯").
- **Multi-time-signature support**: scores with multiple time signatures in the same part (e.g. 4/4 → 3/4 → 4/4 → 2/4) are fully supported in both standard and rhythm modes, with per-measure grey sectors and time-signature labels.
- **Beam injection from `.mscx`**: reads `BeamMode` from the original `.mscx` and injects `<beam number="1/2">` tags into the MusicXML, since music21 does not export beam modes (MuseScore would otherwise auto-calculate them with hooks instead of beams).
- **Multi-page output** with per-system layout and uniform spacing.
- **7-pipeline architecture**: note extraction → single part → SVG export → post-processor → PDF → validation.

### Known Limitations
- Optimized for note values down to the sixteenth note (semicroma); shorter durations (biscrome, semibiscrome) are not fully supported.
- Optimized for 4/4, 3/4, 2/4, and 6/8 time signatures; other meters may produce suboptimal layout.
- Tuplets are not supported.
- The generator is a single-file monolith (~7000 lines); modularization is planned for a future 2.0 release.
- No automated test suite yet; validation relies on the SVG validator and manual review.
- MuseScore 4, librsvg2, and the Atkinson Hyperlegible font must be installed separately (system dependencies).
- Tested on Linux (Debian 12); macOS/Windows support is untested.

### Validated On
- Amen (40 measures, 4/4, F major) — 4 pages full notation, 3 pages rhythm.
- Holberg Suite, Flute 1 (72 measures, 4/4, D major) — 10 pages full notation, 5 pages rhythm.
- Canzon vigesimaottava (43 measures, multiple time signatures: 4/4 → 3/4 → 4/4 → 2/4 → 3/4 → 4/4).
- Prova (6 measures, 4/4, A minor) — 1 page, 16 circles.
