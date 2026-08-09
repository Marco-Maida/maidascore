#!/usr/bin/env python3
"""
MaidaScore — SVG validator for accessible music scores.
Copyright (C) 2026  Marco Maida

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

---

Validatore standalone per spartiti MaidaScore.

Indipendente dal generatore: usa lxml per parsare l'SVG finale (non regex),
e verifica le proprietà geometriche end-to-end. Se il generatore ha un bug
che i controlli interni non vedono (correlazione degli errori), questo
validatore lo rileva.

Uso: python3 validate_maidascore.py <svg_maidascore_file> [--mscz <source.mscz>]
     python3 validate_maidascore.py Esercizio_1_SolM_F3_maidascore_svg-1_maidascore.svg
     python3 validate_maidascore.py Esercizio_1_SolM_F3_maidascore_svg-1_maidascore.svg --mscz Esercizio_1_SolM_F3.mscz

Exit code: 0 = tutto OK, 1 = errori trovati
"""

import sys
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

# Try lxml for robust XML parsing; fall back to ElementTree
try:
    from lxml import etree
    HAS_LXML = True
except ImportError:
    HAS_LXML = False

# ==============================================================================
# CONFIG — must match generate_maidascore.py constants
# ==============================================================================

DISC_R = 110  # must match DISC_R_OVERRIDE in generate_maidascore.py
BEAT_WIDTH = 825  # UNIFORM_MEASURE_WIDTH / 4
UNIFORM_MUSIC_START = 2500
UNIFORM_MEASURE_WIDTH = 3300
STAFF_END_X = 9215

# Color scheme (MaidaScore) — must match generate_maidascore.py NOTE_COLORS
NOTE_COLORS = {
    'E53935': 'Do', 'FB8C00': 'Re', 'FDD835': 'Mi', '64DD17': 'Fa',
    '00695C': 'Sol', '1E88E5': 'La', '8E24AA': 'Si',
}
# Also check with # prefix
NOTE_COLORS_HEX = {
    '#E53935': 'Do', '#FB8C00': 'Re', '#FDD835': 'Mi', '#64DD17': 'Fa',
    '#00695C': 'Sol', '#1E88E5': 'La', '#8E24AA': 'Si',
}
# Dark variants for open notes (whole/half) — see NOTE_COLORS_DARK in generator
NOTE_COLORS_DARK = {
    'E53935': 'Do', 'E65100': 'Re', 'FDD835': 'Mi', '558B2F': 'Fa',
    '00695C': 'Sol', '1E88E5': 'La', '8E24AA': 'Si',
}


class ValidationResult:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.info = []

    def error(self, msg):
        self.errors.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def info_msg(self, msg):
        self.info.append(msg)

    @property
    def ok(self):
        return len(self.errors) == 0

    def report(self):
        lines = []
        if self.info:
            lines.append("=== INFO ===")
            for m in self.info:
                lines.append(f"  ℹ {m}")
        if self.warnings:
            lines.append(f"=== WARNING ({len(self.warnings)}) ===")
            for m in self.warnings:
                lines.append(f"  ⚠ {m}")
        if self.errors:
            lines.append(f"=== ERRORI ({len(self.errors)}) ===")
            for m in self.errors:
                lines.append(f"  ✗ {m}")
        else:
            lines.append("=== TUTTO OK ===")
        return '\n'.join(lines)


# ==============================================================================
# PARSING — independent from the generator (lxml preferred)
# ==============================================================================

def parse_svg_elements(svg_path):
    """Parse SVG and extract circles, text elements, and structural elements.
    Uses lxml if available, falls back to regex (with warnings about fragility)."""
    with open(svg_path, 'r') as f:
        content = f.read()

    circles = []  # (cx, cy, r, fill, stroke)
    texts = []    # (x, y, content)
    barlines = []  # x positions
    staff_lines = []  # (x1, y1, x2, y2)
    rests = []  # (tx, ty) — rest positions from transform matrix
    text_paths = 0  # path class="Text" (should be 0 — MuseScore titles removed)
    note_paths = 0  # path class="Note"

    if HAS_LXML:
        tree = etree.fromstring(content.encode())
        ns = {'svg': 'http://www.w3.org/2000/svg'}

        for circle in tree.iter('{http://www.w3.org/2000/svg}circle'):
            cx = float(circle.get('cx', 0))
            cy = float(circle.get('cy', 0))
            r = float(circle.get('r', 0))
            fill = circle.get('fill', '')
            stroke = circle.get('stroke', '')
            circles.append((cx, cy, r, fill, stroke))

        for text in tree.iter('{http://www.w3.org/2000/svg}text'):
            x = float(text.get('x', 0))
            y = float(text.get('y', 0))
            txt = text.text or ''
            texts.append((x, y, txt))

        for pl in tree.iter('{http://www.w3.org/2000/svg}polyline'):
            cls = pl.get('class', '')
            points = pl.get('points', '')
            if 'BarLine' in cls:
                # points="x1,y1 x2,y2"
                parts = points.replace(',', ' ').split()
                if len(parts) >= 2:
                    barlines.append(float(parts[0]))
            elif 'StaffLines' in cls:
                parts = points.replace(',', ' ').split()
                if len(parts) >= 4:
                    staff_lines.append((float(parts[0]), float(parts[1]),
                                       float(parts[2]), float(parts[3])))

        for path in tree.iter('{http://www.w3.org/2000/svg}path'):
            cls = path.get('class', '')
            if 'Text' in cls and 'Note' not in cls:
                text_paths += 1
            if 'Note' in cls:
                note_paths += 1
            if 'Rest' in cls:
                transform = path.get('transform', '')
                m = re.search(r'matrix\([\d.\-]+,[\d.\-]+,[\d.\-]+,[\d.\-]+,([\d.\-]+),([\d.\-]+)\)', transform)
                if m:
                    rests.append((float(m.group(1)), float(m.group(2))))
    else:
        # Regex fallback (less robust — point 4)
        for m in re.finditer(r'<circle\s+cx="([\d.]+)"\s+cy="([\d.]+)"\s+r="([\d.]+)"'
                            r'(?:\s+fill="([^"]*)")?(?:\s+stroke="([^"]*)")?', content):
            circles.append((float(m.group(1)), float(m.group(2)), float(m.group(3)),
                          m.group(4) or '', m.group(5) or ''))

        for m in re.finditer(r'<text\s+x="([\d.]+)"\s+y="([\d.]+)"[^>]*>([^<]*)</text>', content):
            texts.append((float(m.group(1)), float(m.group(2)), m.group(3)))

        for m in re.finditer(r'<polyline class="BarLine"[^>]*points="([\d.]+),', content):
            barlines.append(float(m.group(1)))

        for m in re.finditer(r'<polyline class="StaffLines"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"', content):
            staff_lines.append((float(m.group(1)), float(m.group(2)),
                               float(m.group(3)), float(m.group(4))))

        text_paths = len(re.findall(r'<path class="Text"', content))
        note_paths = len(re.findall(r'<path class="Note"', content))

        for m in re.finditer(r'<path class="Rest" transform="matrix\([\d.\-]+,[\d.\-]+,[\d.\-]+,[\d.\-]+,([\d.\-]+),([\d.\-]+)\)"', content):
            rests.append((float(m.group(1)), float(m.group(2))))

    return {
        'circles': circles,
        'texts': texts,
        'barlines': sorted(set(barlines)),
        'staff_lines': staff_lines,
        'rests': rests,
        'text_paths': text_paths,
        'note_paths': note_paths,
        'content': content,
        'svg_text': content,
        'parser': 'lxml' if HAS_LXML else 'regex',
    }


def count_notes_in_mscz(mscz_path):
    """Count notes in the source .mscz (independent verification)."""
    with zipfile.ZipFile(mscz_path, 'r') as zf:
        mscx_name = [f for f in zf.namelist() if f.endswith('.mscx')][0]
        with zf.open(mscx_name) as f:
            content = f.read().decode()
    # Count <Chord> elements (each = 1+ notes)
    tree = ET.fromstring(content)
    count = 0
    for chord in tree.iter('Chord'):
        count += len(chord.findall('Note'))
    return count


# ==============================================================================
# CHECKS
# ==============================================================================

def check_circles_in_sectors(data, result):
    """Check 1: every circle must be inside a grey sector (beat width)."""
    circles = data['circles']
    if not circles:
        result.warning("Nessun cerchio trovato nel SVG")
        return

    # Detect grey sectors directly from the SVG (supports 4/4, 3/4, 6/8, etc.)
    # Grey rects have fill #E8E8E8 or #B8B8B8 with opacity 0.85
    import re
    svg_text = data.get('svg_text', '')
    grey_rects = []
    for m in re.finditer(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"[^>]*fill="(#[0-9A-Fa-f]+)"[^>]*opacity="0.85"', svg_text):
        x, y, w, color = float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4)
        if color in ('#E8E8E8', '#B8B8B8'):
            grey_rects.append((x, y, w))
    
    # If we found grey rects, use them as sector boundaries
    # Group by Y (system), then each rect is a sector
    sectors = []
    if grey_rects:
        sectors = sorted(grey_rects, key=lambda r: (round(r[1]/100)*100, r[0]))
    
    for i, (cx, cy, r, fill, stroke) in enumerate(circles):
        if r < 50:  # skip non-note circles (e.g. dots)
            continue

        left_edge = cx - r
        right_edge = cx + r

        if sectors:
            # Find the sector that contains this circle's center
            found_sector = False
            for sx, sy, sw in sectors:
                # Match by Y (same system, within staff height)
                if abs(sy - (cy - 350)) > 500:  # rough Y match (sector is above notes)
                    continue
                if sx <= cx < sx + sw:
                    if left_edge >= sx and right_edge <= sx + sw:
                        found_sector = True
                    else:
                        result.error(
                            f"Cerchio {i} (cx={cx:.0f}, r={r:.0f}) "
                            f"bordi [{left_edge:.0f}, {right_edge:.0f}] "
                            f"escono dal settore [{sx:.0f}, {sx+sw:.0f}]"
                        )
                        found_sector = True
                    break
            
            if not found_sector:
                result.warning(
                    f"Cerchio {i} (cx={cx:.0f}) non rientra in nessun settore conosciuto"
                )
        else:
            # Fallback: use fixed BEAT_WIDTH (4/4 assumption)
            measure_start = UNIFORM_MUSIC_START
            found_sector = False
            for m in range(10):
                m_start = measure_start + m * UNIFORM_MEASURE_WIDTH
                if m_start > STAFF_END_X:
                    break
                m_end = m_start + UNIFORM_MEASURE_WIDTH
                if m_start <= cx < m_end:
                    beat_idx = int((cx - m_start) / BEAT_WIDTH)
                    beat_start = m_start + beat_idx * BEAT_WIDTH
                    beat_end = beat_start + BEAT_WIDTH
                    if left_edge >= beat_start and right_edge <= beat_end:
                        found_sector = True
                        break
                    else:
                        result.error(
                            f"Cerchio {i} (cx={cx:.0f}, r={r:.0f}) nel beat {beat_idx} "
                            f"del misura {m}: bordi [{left_edge:.0f}, {right_edge:.0f}] "
                            f"escono dal settore [{beat_start:.0f}, {beat_end:.0f}]"
                        )
                        found_sector = True
                        break
            if not found_sector:
                result.warning(
                    f"Cerchio {i} (cx={cx:.0f}) non rientra in nessun settore conosciuto"
                )

    result.info_msg(f"Controllati {len(circles)} cerchi nei settori grigi")


def check_circle_overlaps(data, result):
    """Check 2: circles in the same beat should not overlap (2r apart)."""
    circles = data['circles']

    # Group circles by approximate Y (same system) and measure
    for i in range(len(circles)):
        for j in range(i + 1, len(circles)):
            cx1, cy1, r1, _, _ = circles[i]
            cx2, cy2, r2, _, _ = circles[j]

            # Same system (Y within 50px)
            if abs(cy1 - cy2) > 50:
                continue

            # Distance between centers
            dist = abs(cx1 - cx2)
            min_dist = r1 + r2

            if dist < min_dist - 5:  # 5px tolerance
                # Check if they're in the same beat (could be chord = OK)
                m1 = int((cx1 - UNIFORM_MUSIC_START) / UNIFORM_MEASURE_WIDTH)
                m2 = int((cx2 - UNIFORM_MUSIC_START) / UNIFORM_MEASURE_WIDTH)
                if m1 != m2:
                    continue  # different measures, skip
                b1 = int((cx1 - UNIFORM_MUSIC_START - m1 * UNIFORM_MEASURE_WIDTH) / BEAT_WIDTH)
                b2 = int((cx2 - UNIFORM_MUSIC_START - m2 * UNIFORM_MEASURE_WIDTH) / BEAT_WIDTH)
                if b1 != b2:
                    continue  # different beats, skip

                # Same beat — check if same onset (chord) or different
                # Chord notes SHOULD overlap (stacked at same X), so only warn
                result.warning(
                    f"Cerchi sovrapposti nello stesso beat: "
                    f"({cx1:.0f},{cy1:.0f}) e ({cx2:.0f},{cy2:.0f}), "
                    f"distanza={dist:.0f} < {min_dist:.0f} "
                    f"(se è un accordo, è corretto)"
                )

    result.info_msg(f"Controllate sovrapposizioni cerchi ({len(circles)} cerchi)")


def check_rests_not_overlapping_notes(data, result):
    """Check 2b: rests should not overlap with note circles (same system, close X)."""
    circles = data['circles']
    rests = data['rests']

    if not rests:
        result.info_msg("Nessuna pausa nel SVG")
        return

    overlap_count = 0
    for rx, ry in rests:
        for cx, cy, r, _, _ in circles:
            if r < 50:
                continue
            # Same system (Y within 300px — rest and note in same staff)
            if abs(ry - cy) > 300:
                continue
            # Check X overlap: rest center must be at least r + 65px from note center
            # (65px ≈ half width of rest glyph at scale 3.386)
            dist = abs(rx - cx)
            min_dist = r + 65
            if dist < min_dist:
                result.warning(
                    f"Pausa a ({rx:.0f},{ry:.0f}) sovrapposta a nota "
                    f"a ({cx:.0f},{cy:.0f}), distanza={dist:.0f} < {min_dist:.0f}"
                )
                overlap_count += 1

    if overlap_count == 0:
        result.info_msg(f"Controllate {len(rests)} pause: nessuna sovrapposizione con note ✓")
    else:
        result.warning(f"Trovate {overlap_count} sovrapposizioni pausa-nota")


def check_rests_in_sectors(data, result):
    """Check 2c: rests should be positioned within grey sectors (like notes)."""
    rests = data['rests']
    if not rests:
        return

    out_of_sector = 0
    for rx, ry in rests:
        # Find which measure this rest belongs to
        found = False
        for m in range(10):
            m_start = UNIFORM_MUSIC_START + m * UNIFORM_MEASURE_WIDTH
            if m_start > STAFF_END_X:
                break
            m_end = m_start + UNIFORM_MEASURE_WIDTH
            if m_start <= rx < m_end:
                # Check which beat
                beat_idx = int((rx - m_start) / BEAT_WIDTH)
                beat_start = m_start + beat_idx * BEAT_WIDTH
                beat_end = beat_start + BEAT_WIDTH
                # Rests don't need to be fully inside (they're smaller than circles)
                # but their center should be within the beat sector
                if not (beat_start - 50 <= rx <= beat_end + 50):
                    result.warning(
                        f"Pausa a ({rx:.0f},{ry:.0f}) fuori dal settore "
                        f"[{beat_start:.0f}, {beat_end:.0f}]"
                    )
                    out_of_sector += 1
                found = True
                break
        if not found:
            result.warning(f"Pausa a ({rx:.0f},{ry:.0f}) non in nessuna battuta nota")

    if out_of_sector == 0:
        result.info_msg(f"Controllate {len(rests)} pause nei settori: tutte allineate ✓")


def check_musescore_titles_removed(data, result):
    """Check 3: no path class='Text' should remain (MuseScore titles removed)."""
    if data['text_paths'] > 0:
        result.error(
            f"Trovati {data['text_paths']} path class='Text' — "
            f"i titoli MuseScore non sono stati rimossi"
        )
    else:
        result.info_msg("Nessun path class='Text' residuo (titoli MuseScore rimossi)")


def check_note_count(data, result, mscz_path=None, total_circles=None):
    """Check 4: number of circles matches number of notes in source .mscz.
    total_circles = sum across all pages (for multi-page validation)."""
    note_circles = sum(1 for _, _, r, _, _ in data['circles'] if r > 50)
    result.info_msg(f"Cerchi note in questa pagina: {note_circles}")

    if mscz_path:
        source_notes = count_notes_in_mscz(mscz_path)
        if total_circles is not None:
            result.info_msg(f"Note nel .mscz sorgente: {source_notes}")
            result.info_msg(f"Cerchi note totali (tutte le pagine): {total_circles}")
            if total_circles != source_notes:
                result.error(
                    f"DISCREPANZA: {total_circles} cerchi totali vs {source_notes} note nel .mscz. "
                    f"Alcune note potrebbero essere andate perse (bug silenzioso)."
                )
            else:
                result.info_msg("Conteggio cerchi totali == note sorgente ✓")
        else:
            result.info_msg(f"Note nel .mscz sorgente: {source_notes} (usa --all-pages per verifica totale)")


def check_staff_lines_cover_barlines(data, result):
    """Check 5: staff lines extend to or past the last barline."""
    if not data['staff_lines']:
        result.warning("Nessuna staff line trovata")
        return

    max_staff_x = max(sl[2] for sl in data['staff_lines'])
    result.info_msg(f"Staff lines si estendono fino a X={max_staff_x:.0f}")

    if data['barlines']:
        max_barline = max(data['barlines'])
        result.info_msg(f"Barline più a destra: X={max_barline:.0f}")
        if max_staff_x < max_barline - 10:
            result.error(
                f"Staff lines ({max_staff_x:.0f}) non coprono l'ultima barline ({max_barline:.0f})"
            )


def check_text_labels(data, result):
    """Check 6: text elements should be note names (Italian or English) or title text."""
    note_letters_en = set('ABCDEFG')
    note_names_it = {'Do', 'Re', 'Mi', 'Fa', 'Sol', 'La', 'Si'}
    title_texts = {'Esercizio', 'Flauto', 'Sol', 'Maggiore', '—'}
    unknown_texts = []

    for x, y, txt in data['texts']:
        txt = txt.strip()
        if not txt:
            continue
        # Single letter = English note name
        if len(txt) == 1 and txt in note_letters_en:
            continue
        # Italian note name (Do/Re/Mi/Fa/Sol/La/Si)
        if txt in note_names_it:
            continue
        # Title text
        if any(t in txt for t in title_texts):
            continue
        # Numbers (measure numbers)
        if txt.isdigit():
            continue
        unknown_texts.append((x, y, txt))

    if unknown_texts:
        for x, y, txt in unknown_texts:
            result.warning(f"Testo sconosciuto a ({x:.0f},{y:.0f}): '{txt}'")
    else:
        result.info_msg(f"Controllati {len(data['texts'])} elementi testo (tutti riconosciuti)")


def check_colors(data, result):
    """Check 7: circle colors should be from the MaidaScore palette."""
    valid_colors = set(NOTE_COLORS.keys()) | set(NOTE_COLORS_HEX.keys()) | set(NOTE_COLORS_DARK.keys()) | {'white', '#ffffff', 'none', ''}
    invalid = []

    for cx, cy, r, fill, stroke in data['circles']:
        if r < 50:
            continue
        # Check fill and stroke
        for color_attr in [fill, stroke]:
            if not color_attr:
                continue
            color_lower = color_attr.lower().lstrip('#')
            if color_attr not in valid_colors and color_lower not in {c.lower().lstrip('#') for c in valid_colors}:
                invalid.append((cx, cy, color_attr))

    if invalid:
        for cx, cy, color in invalid[:5]:  # show max 5
            result.warning(f"Colore non standard a ({cx:.0f},{cy:.0f}): {color}")
    else:
        result.info_msg("Tutti i colori dei cerchi sono nello schema MaidaScore")


# ==============================================================================
# MAIN
# ==============================================================================

def validate(svg_path, mscz_path=None, total_circles=None):
    result = ValidationResult()

    if not os.path.exists(svg_path):
        result.error(f"File non trovato: {svg_path}")
        return result

    result.info_msg(f"Parser: {'lxml' if HAS_LXML else 'regex (fallback — installa lxml per robustezza)'}")
    result.info_msg(f"File: {svg_path}")

    data = parse_svg_elements(svg_path)

    result.info_msg(f"Cerchi: {len(data['circles'])}, Testi: {len(data['texts'])}, "
                    f"Barlines: {len(data['barlines'])}, Staff lines: {len(data['staff_lines'])}, "
                    f"Pause: {len(data['rests'])}")

    # Run all checks
    check_musescore_titles_removed(data, result)
    check_circles_in_sectors(data, result)
    check_circle_overlaps(data, result)
    check_rests_not_overlapping_notes(data, result)
    check_rests_in_sectors(data, result)
    check_staff_lines_cover_barlines(data, result)
    check_text_labels(data, result)
    check_colors(data, result)
    check_note_count(data, result, mscz_path, total_circles)

    return result


def validate_all_pages(svg_pattern_dir, mscz_path=None, prefix=None):
    """Validate all SVG pages in a directory. Aggregates note count across pages.
    
    prefix: if provided, only match files starting with this prefix (e.g. 'my_score').
            This prevents matching SVG files from other scores in the same directory.
    """
    import glob as globmod
    result = ValidationResult()

    # Find all *_maidascore.svg files, sorted numerically
    # If prefix is given, filter to only that score's files
    pattern = os.path.join(svg_pattern_dir, '*_maidascore.svg')
    svg_files = globmod.glob(pattern)
    if prefix:
        # Match files like {prefix}_svg-N_maidascore.svg
        prefix_base = os.path.basename(prefix)
        svg_files = [f for f in svg_files if os.path.basename(f).startswith(prefix_base)]
    # Sort numerically by page number
    def page_num(path):
        m = re.search(r'-(\d+)_maidascore\.svg$', path)
        return int(m.group(1)) if m else 0
    svg_files.sort(key=page_num)

    if not svg_files:
        result.error(f"Nessun file *_maidascore.svg trovato in {svg_pattern_dir}")
        return result

    result.info_msg(f"Trovate {len(svg_files)} pagine SVG")

    # First pass: count total circles across all pages
    total_circles = 0
    for sf in svg_files:
        data = parse_svg_elements(sf)
        total_circles += sum(1 for _, _, r, _, _ in data['circles'] if r > 50)

    # Second pass: validate each page
    for sf in svg_files:
        result.info_msg(f"--- Pagina: {os.path.basename(sf)} ---")
        page_result = validate(sf, mscz_path, total_circles)
        result.errors.extend(page_result.errors)
        result.warnings.extend(page_result.warnings)
        result.info.extend(page_result.info)

    return result


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 validate_maidascore.py <svg_maidascore | dir> [--mscz <source.mscz>] [--all-pages]")
        print("Esempio singola pagina: python3 validate_maidascore.py Esercizio_*_maidascore.svg")
        print("Esempio tutte le pagine: python3 validate_maidascore.py . --mscz Esercizio_*.mscz --all-pages")
        sys.exit(2)

    target = sys.argv[1]
    mscz_path = None
    all_pages = '--all-pages' in sys.argv

    if '--mscz' in sys.argv:
        idx = sys.argv.index('--mscz')
        if idx + 1 < len(sys.argv):
            mscz_path = sys.argv[idx + 1]

    # If no --mscz, try to find it automatically
    if not mscz_path:
        d = target if os.path.isdir(target) else (os.path.dirname(target) or '.')
        if os.path.isdir(d):
            mscz_files = [f for f in os.listdir(d) if f.endswith('.mscz') and 'maidascore' not in f]
            if len(mscz_files) == 1:
                mscz_path = os.path.join(d, mscz_files[0])
                print(f"Trovato .mscz sorgente: {mscz_path}")
            elif len(mscz_files) > 1:
                print(f"Più file .mscz trovati, uso --mscz per specificare: {mscz_files}")

    if all_pages or os.path.isdir(target):
        d = target if os.path.isdir(target) else (os.path.dirname(target) or '.')
        result = validate_all_pages(d, mscz_path)
    else:
        result = validate(target, mscz_path)

    print(result.report())

    if not result.ok:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
