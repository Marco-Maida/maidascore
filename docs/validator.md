# SVG Validator

MaidaScore includes an independent SVG validator (`validate_maidascore.py`) that verifies
the generated output using `lxml`, separate from the generator's own internal checks.

---

## Why an independent validator?

The generator's internal checks use the **same assumptions, regexes, and data structures**
as the generation code itself. If a function has a bug (e.g. mishandling tuplets), it
makes the same mistake in both generation and verification — the check passes, but the
output is wrong. An independent validator that re-parses the final SVG with `lxml`
(a completely different code path) breaks this correlation and catches errors that
internal checks cannot.

---

## The 7 checks

1. **Circles in gray sectors** — every circle (r > 50) must fit inside a gray sector
   (beat width 825px). Verifies that `[cx-r, cx+r]` is within `[beat_start, beat_end]`.

2. **Circle overlaps** — circles in the same beat closer than 2r (220px) trigger a
   warning. Chords (same onset) are allowed.

3. **MuseScore titles removed** — no `path class="Text"` elements should remain (they
   are removed by the generator).

4. **Circle count matches source** — the total number of circles across all pages must
   equal the number of notes in the source `.mscz`. A discrepancy means notes were
   silently lost.

5. **Staff lines cover barlines** — staff lines extend to or beyond the last barline.

6. **Recognized text** — `<text>` elements must be note letters (A-G), titles, or
   measure numbers. Unknown text triggers a warning.

7. **Color scheme** — circle fill/stroke colors must be in the MaidaScore palette
   (Do=`#E53935`, Re=`#FB8C00`, etc.).

---

## Usage

```bash
# Single page
python3 validate_maidascore.py output_svg-1_maidascore.svg --mscz source.mscz

# All pages (recommended — verifies total circle count)
python3 validate_maidascore.py . --mscz source.mscz --all-pages

# Automatic (integrated as step 6 of the generator)
python3 generate_maidascore.py input.mscz output_prefix 0
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks passed (warnings may be present) |
| 1 | Errors found — PDF may be corrupted |
| 2 | Incorrect usage |

---

## Dependencies

- `lxml` (preferred — robust XML parsing). Falls back to regex with a warning if unavailable.
- `xml.etree.ElementTree` — for parsing `.mscx` inside `.mscz` archives

---

## Known acceptable warnings

### 3 notes per beat overlap

With 3 notes in a single 825px beat, equalized spacing (25%/50%/75% = 206px apart) is
less than the 220px diameter threshold. This is currently acceptable because the circles
are semi-transparent and the note letters remain readable. A future fix could reduce
the circle radius for dense beats or limit to 2 notes per beat.
