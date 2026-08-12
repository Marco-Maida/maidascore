#!/usr/bin/env python3
"""
MaidaScore — Accessible music notation generator for dyslexic learners.
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

Pipeline completa per generare spartiti accessibili per dislessici (notazione semplificata con cerchi colorati).

Optimizzato per flauto traverso: i parametri di layout (estensione, densità per rigo,
riconoscimento delle ottave, posizionamento dei triangolini di registro) sono tarati
sulla tessitura del flauto e sul sistema didattico di riferimento. Il sistema però è
generico: elaborando un .mscz che contiene altri strumenti (clarinetto, violino,
chitarra, voce, ecc.) e selezionando la parte con `part_index`, produce lo stesso
tipo di output. Per strumenti con estensione molto diversa dal flauto può essere
necessario adattare alcune costanti di layout (vedi CONFIG e la documentazione).

Note names default to **Italian** (Do Re Mi Fa Sol La Si) to match the target didactic
system. Pass `--lang en` on the command line for English note names (C D E F G A B).
The language selects three dictionaries — `NOTE_NAMES_PALLINI` (short labels inside
the colored circles), `NOTE_NAMES_TAVOLA` (full labels in the sound table) and
`NOTE_NAMES_TAVOLA_SPLIT` (label + accidental symbol) — without touching the layout
logic. Adding a new language is just a matter of adding a new entry to each
dictionary.

Input:  file .mscz (MuseScore 4) con la partitura originale
Output: 
  - file .mscz con layout accessibile (spatium grande, linee spesse)
  - file .pdf con note colorate + nomi note + sfondo quarti + rettangoli durata

Nota sui valori ritmici: il layout è ottimizzato per durate fino alla semicroma
(sedicesimo). Valori più brevi (biscrome, semibiscrome) non sono garantiti.

Pipeline:
  1. Estrae le note (pitch, durata) dal .mscz
  2. Modifica il .mscz per layout accessibile (spatium, linee spesse)
  3. Esporta SVG da MuseScore 4
  4. Post-processa SVG (colori, nomi, sfondi, rettangoli)
  5. Converte SVG → PDF

Uso: python3 generate_maidascore.py input.mscz [output_prefix]
"""

import sys
import os
import re
import shutil
import zipfile
import subprocess
import xml.etree.ElementTree as ET

# ==============================================================================
# CONFIG
# ==============================================================================

import shutil as _shutil
MUSESCORE_CMD = (
    os.environ.get('MAIDASCORE_MSCORE')
    or _shutil.which('mscore')
    or _shutil.which('musescore4')
    or _shutil.which('ms4')
    or 'mscore'
)
XVFB = os.environ.get('MAIDASCORE_XVFB', 'xvfb-run -a')

# Layout accessibile per dislessici
# spatium moderato: il post-processing SVG ingrandisce i cerchi colorati separatamente,
# quindi non serve spatium enorme nel .mscz. Usiamo spatium sufficiente per 4 battute/rigo.
SPATIUM = '2.0'
STAFF_LINE_WIDTH = '0.11'  # default — ispessito nel post-processing SVG
PAGE_WIDTH = '8.27'   # A4 portrait width (inches)
PAGE_HEIGHT = '11.69' # A4 portrait height (inches)

# Dynamic measures-per-system calculation
# Each colored disc needs ~300px horizontal space (disc_r=130 diameter=260 + gap)
# Printable width in SVG units ≈ 8510 (A4 portrait minus margins)
# Max notes per system = 8510 / 300 ≈ 28, use 26 conservatively
MIN_NOTE_SPACING = 300
MAX_NOTES_PER_SYSTEM = 26
# UNIFORM measure width: all measures same width (sized for densest measure).
# 2 mis/sistema: settori grigi ampi (~3300px/battuta = 825px/quarto).
UNIFORM_MEASURES_PER_SYSTEM = 2
UNIFORM_MEASURE_WIDTH = 3300  # SVG units; 2 meas/system × 3300 = 6600 < 6715 (9215-2500)
UNIFORM_MUSIC_START = 2500  # uniform music start X (after enlarged clef + keysig + timesig)
DEFAULT_MEASURES_PER_SYSTEM = UNIFORM_MEASURES_PER_SYSTEM

# Invariant assertions moved after DISC_R_OVERRIDE definition below.
STAFF_END_X = 9215  # staff lines end at this X coordinate
BEAT_WIDTH = UNIFORM_MEASURE_WIDTH / 4  # 825px per beat = grey sector width



# ==============================================================================
# 1. ESTRAI NOTE DAL .mscz
# ==============================================================================

def extract_notes_from_mscz(mscz_path, part_index=0):
    """Estrae pitch, duration_type, dots, onset (beat position) e time signature dal .mscz.
    
    part_index: quale parte/strumento estrarre (0=primo, 1=secondo, ecc.)
    Default = 0 (prima parte).
    
    Fix:
    - (a) onset resettato a 0 per OGNI voice (non accumulato tra voci)
    - (b) tuplet/tie/durate sconosciute → fail loudly (non fallback silenzioso a 1)
    - (c) time signature estratta dal .mscx (non hardcoded 4/4)
    - (f) temp dir con tempfile.TemporaryDirectory (no leak su eccezione)
    - (g) 2 Ago 2026: seleziona parte specifica (part_index) invece di iterare
          su tutti gli staff mescolando i strumenti
    """
    import tempfile
    
    DURATION_TO_BEATS = {
        'whole': 4, 'whole_dotted': 6,
        'half': 2, 'half_dotted': 3,
        'quarter': 1, 'quarter_dotted': 1.5,
        'eighth': 0.5, 'eighth_dotted': 0.75,
        '16th': 0.25, '16th_dotted': 0.375,
        '32nd': 0.125,
        'measure': None,  # special: fills entire measure (beats = time_sig numerator)
    }
    
    notes = []
    rests = []  # pauses with onset for proper positioning
    time_sig = (4, 4)  # default, overridden if found in .mscx
    time_sigs_per_measure = {}  # FIX #152: {measure_idx: (num, den)} per cambi di tempo
    
    with tempfile.TemporaryDirectory(prefix='mscz_extract_') as temp_dir:
        with zipfile.ZipFile(mscz_path, 'r') as zf:
            zf.extractall(temp_dir)
        
        mscx_path = None
        for f in os.listdir(temp_dir):
            if f.endswith('.mscx'):
                mscx_path = os.path.join(temp_dir, f)
                break
        
        if not mscx_path:
            raise ValueError("No .mscx found in .mscz")
        
        tree = ET.parse(mscx_path)
        root = tree.getroot()
        
        # Extract time signature from first measure (bug c — no more 4/4 hardcoded)
        # FIX #152: estrai TUTTI i time signature, non solo il primo.
        # time_sig = primo TS (per backward compat).
        # time_sigs_per_measure = dict per ogni battuta.
        first_ts_found = False
        for ts in root.iter('TimeSig'):
            sigN = ts.find('sigN')
            sigD = ts.find('sigD')
            if sigN is not None and sigD is not None:
                if not first_ts_found:
                    time_sig = (int(sigN.text), int(sigD.text))
                    first_ts_found = True
                break  # il primo basta per time_sig; i per-battuta li estraiamo sotto
        
        # seleziona lo staff corretto (part_index)
        # In MuseScore 4 .mscx, gli <Staff> con misure hanno id="1","2","3",...
        # I primi <Staff> senza misure sono metadata (strumenti).
        # Filtriamo solo gli staff che contengono <Measure>.
        all_staves = root.findall('.//Staff')
        staves_with_measures = [s for s in all_staves if s.find('Measure') is not None]
        
        if not staves_with_measures:
            raise ValueError("No staves with measures found in .mscx")
        
        if part_index >= len(staves_with_measures):
            raise ValueError(
                f"part_index={part_index} out of range. "
                f"Found {len(staves_with_measures)} staves with measures (0..{len(staves_with_measures)-1})."
            )
        
        selected_staff = staves_with_measures[part_index]
        measures = selected_staff.findall('Measure')
        
        # le battute di pausa iniziali NON vengono più saltate.
        # Il brano va suonato con altri, l'allievo deve vedere le
        # battute di attesa per contare le pause.
        
        m_idx = 0
        for measure in measures:
            # Fix (a): onset resettato a 0 per OGNI voice, non accumulato tra voci
            for elem in measure:
                if elem.tag == 'voice':
                    onset = 0.0  # <-- RESET per ogni voice
                    for voice_elem in elem:
                        if voice_elem.tag in ('Chord', 'Rest'):
                            dur_type_elem = voice_elem.find('durationType')
                            dtype = dur_type_elem.text if dur_type_elem is not None else 'quarter'
                            dots_elem = voice_elem.find('dots')
                            dots = int(dots_elem.text) if dots_elem is not None else 0
                            
                            # Fix (b): fail loudly on unknown duration types
                            if dtype not in DURATION_TO_BEATS:
                                raise ValueError(
                                    f"Unsupported duration type '{dtype}' in measure {m_idx}. "
                                    f"MaidaScore supports: {sorted(DURATION_TO_BEATS.keys())}. "
                                    f"Add support or simplify the score."
                                )
                            
                            # Fix (b): detect tuplets — not supported, fail loudly
                            tuplet = voice_elem.find('Tuplet')
                            if tuplet is not None:
                                raise ValueError(
                                    f"Tuplet found in measure {m_idx}. "
                                    f"MaidaScore does not support tuplets. "
                                    f"Convert to straight rhythms or add support."
                                )
                            
                            # Handle 'measure' duration type (whole-measure rest)
                            # and multi-measure rests
                            if dtype == 'measure':
                                # Check for multi-measure rest at MEASURE level (not voice)
                                mmr_elem = measure.find('multiMeasureRest')
                                # Compute quarter-beats for this time signature
                                # 4/4→4, 3/4→3, 6/8→3, 3/8→1.5
                                if time_sig[1] in (1, 2, 4):
                                    beats = time_sig[0] * (4 // time_sig[1])
                                elif time_sig[1] == 8:
                                    beats = time_sig[0] * 0.5
                                elif time_sig[1] == 16:
                                    beats = time_sig[0] * 0.25
                                else:
                                    beats = time_sig[0]
                                # salva la durata esatta in ql_beats
                                # per evitare che music21 usi ql=4.0 (whole) in 6/8 (3.0 beats)
                                if mmr_elem is not None:
                                    # Multi-measure rest covers N measures in one
                                    # Add as whole-measure rest for current measure
                                    rests.append({
                                        'duration_type': 'whole',
                                        'onset': onset,
                                        'measure_idx': m_idx,
                                        'is_measure_rest': True,
                                        'ql_beats': beats,
                                    })
                                    onset += beats
                                    continue
                                else:
                                    # Single whole-measure rest
                                    rests.append({
                                        'duration_type': 'whole',
                                        'onset': onset,
                                        'measure_idx': m_idx,
                                        'is_measure_rest': True,
                                        'ql_beats': beats,
                                    })
                                    onset += beats
                                    continue
                            
                            # Compute beats
                            base_beats = DURATION_TO_BEATS[dtype]
                            beats = base_beats * (1 + 0.5 * dots) if dots > 0 else base_beats
                            
                            if voice_elem.tag == 'Chord':
                                # Detect ties — warn but don't crash (tied notes share onset)
                                has_tie = voice_elem.find('.//Tie') is not None
                                note_count = len(voice_elem.findall('Note'))
                                for note in voice_elem.findall('Note'):
                                    pitch_elem = note.find('pitch')
                                    if pitch_elem is not None:
                                        pitch = int(pitch_elem.text)
                                        dur_key = dtype
                                        if dots > 0:
                                            dur_key = f"{dtype}_dotted"
                                        notes.append({
                                            'pitch': pitch,
                                            'duration_type': dtype,
                                            'dots': dots,
                                            'dur_key': dur_key,
                                            'onset': onset,
                                            'measure_idx': m_idx,
                                            'n_chord_notes': note_count,  # for chord detection
                                        })
                            elif voice_elem.tag == 'Rest':
                                # Extract rest with onset for proper positioning
                                dur_key = f"{dtype}_dotted" if dots > 0 else dtype
                                rests.append({
                                    'duration_type': dtype,
                                    'dur_key': dur_key,
                                    'dots': dots,
                                    'onset': onset,
                                    'measure_idx': m_idx,
                                })
                            
                            onset += beats
            m_idx += 1
    
    return {'notes': notes, 'rests': rests, 'time_sig': time_sig}


def extract_notes_via_music21(mscz_path, part_index=0):
    """Estrae note usando music21 (gestisce tie, tuplet, ecc. correttamente).
    
    4 Ago 2026: extract_notes_from_mscz legge il .mscx XML direttamente e NON gestisce
    i tie — estrae note legate come note separate, causando mismatch con il rendering
    SVG di MuseScore (che unisce le note legate). Questa funzione usa music21 che
    gestisce i tie correttamente, producendo dati coerenti con il SVG.
    
    Converte il .mscz in MusicXML via MuseScore 4, poi legge con music21.
    """
    import subprocess, tempfile, os
    from music21 import converter, note as m21note
    
    # Converti .mscz in MusicXML
    xml_path = '/tmp/m21_extract_' + str(os.getpid()) + '.musicxml'
    cmd = f'{XVFB} {MUSESCORE_CMD} -o "{xml_path}" "{mscz_path}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not os.path.exists(xml_path):
        raise RuntimeError(f"Failed to convert .mscz to MusicXML: {result.stderr[-200:]}")
    
    try:
        score = converter.parse(xml_path)
        if part_index >= len(score.parts):
            raise ValueError(f"part_index={part_index} out of range ({len(score.parts)} parts)")
        
        part = score.parts[part_index]
        measures = list(part.getElementsByClass('Measure'))
        
        # Estrai time signature — SUPPORTA CAMBI DI TEMPO MULTIPLI.
        # time_sig = primo time signature (per backward compat).
        # time_sigs_per_measure = dict {measure_idx: (num, den)} per ogni battuta.
        # FIX #152: prima si estraeva solo il primo TS e si usava globalmente,
        # ignorando i cambi intermedi (3/4→4/4→2/4→4/4).
        # FIX #152: estrai TUTTI i time signature con il loro offset (beat position).
        # getTimeSignatures() funziona sempre (getElementsByClass/recurse non trovano
        # i TS in alcuni MusicXML di MuseScore). Ogni TS ha un offset in quarter-beats.
        ts_list = []
        for ts in part.getTimeSignatures():
            ts_list.append((ts.offset, ts.numerator, ts.denominator))
        
        # time_sig = primo TS (per backward compat)
        if ts_list:
            time_sig = (ts_list[0][1], ts_list[0][2])
        else:
            time_sig = (4, 4)
        
        # Calcola il time_sig attivo per ogni battuta
        time_sigs_per_measure = {}
        current_ts = time_sig
        ts_idx = 0
        for m_idx, m in enumerate(measures):
            m_offset = m.offset
            # Avanza ts_list finché il TS è valido per questa battuta
            while ts_idx + 1 < len(ts_list) and ts_list[ts_idx + 1][0] <= m_offset + 0.001:
                ts_idx += 1
                current_ts = (ts_list[ts_idx][1], ts_list[ts_idx][2])
            time_sigs_per_measure[m_idx] = current_ts
        
        # 4 Ago 2026 (bug KS): estrai key signature reale dal file.
        # Senza questo, la ricostruzione usa KS hardcoded (Sol maggiore 1#)
        # invece di quella reale (es. Re maggiore 2#).
        key_sig = 0  # default: Do maggiore (0 diesis/bemolli)
        ks_elements = part.recurse().getElementsByClass('KeySignature')
        if ks_elements:
            key_sig = ks_elements[0].sharps  # positivo=diesis, negativo=bemolli

        # determina quali step sono alterati dall'armatura.
        # Serve per distinguere accidentals di passaggio da quelli dell'armatura.
        # Ordine diesis: F C G D A E B; ordine bemolli: B E A D G C F
        SHARP_ORDER = ['F', 'C', 'G', 'D', 'A', 'E', 'B']
        FLAT_ORDER = ['B', 'E', 'A', 'D', 'G', 'C', 'F']
        keysig_altered_steps = set()  # es. {'F', 'C'} per Re maggiore (2#)
        keysig_alteration = '#' if key_sig > 0 else ('b' if key_sig < 0 else '')
        if key_sig > 0:
            keysig_altered_steps = set(SHARP_ORDER[:key_sig])
        elif key_sig < 0:
            keysig_altered_steps = set(FLAT_ORDER[:abs(key_sig)])
        
        # Mappa ql → duration_type
        QL_TO_DUR = {}
        for dt, beats in [('whole', 4.0), ('half', 2.0), ('quarter', 1.0),
                          ('eighth', 0.5), ('16th', 0.25), ('32nd', 0.125)]:
            QL_TO_DUR[beats] = dt
            QL_TO_DUR[beats * 1.5] = f"{dt}"  # dotted
        
        notes = []
        rests = []
        
        for m_idx, m in enumerate(measures):
            for n in m.notesAndRests:
                ql = n.quarterLength
                offset = n.offset
                
                # Determina duration_type e dots dalla ql
                # dotted = ql = base * 1.5
                dur_type = n.duration.type
                dots = n.duration.dots
                
                if n.isRest:
                    # Verifica se è un measure rest (riempie l'intera battuta)
                    # FIX #152: usa il time_sig specifico di questa battuta
                    m_ts = time_sigs_per_measure.get(m_idx, time_sig)
                    measure_ql = m_ts[0] * (4.0 / m_ts[1]) if m_ts[1] in (1,2,4) else m_ts[0] * 0.5
                    is_measure_rest = abs(ql - measure_ql) < 0.01 or dur_type == 'measure'
                    rests.append({
                        'duration_type': dur_type if dur_type != 'measure' else 'whole',
                        'dur_key': f"{dur_type}_dotted" if dots > 0 else (dur_type if dur_type != 'measure' else 'whole'),
                        'dots': dots,
                        'onset': offset,
                        'measure_idx': m_idx,
                        'is_measure_rest': is_measure_rest,
                    })
                else:
                    dur_key = f"{dur_type}_dotted" if dots > 0 else dur_type
                    # 4 Ago 2026 (bug Si/La): estrai step e accidental da music21
                    # per ottenere il nome della nota corretto tenendo conto
                    # dell'armatura. Senza questo, pitch class 10 (A#/Bb) viene
                    # sempre mappato ad A (La) anche in Fa maggiore dove è Bb (Si).
                    step = n.pitch.step  # C, D, E, F, G, A, B
                    acc = n.pitch.accidental
                    acc_str = ''
                    if acc is not None:
                        if acc.name == 'flat':
                            acc_str = 'b'
                        elif acc.name == 'sharp':
                            acc_str = '#'
                        elif acc.name == 'natural':
                            acc_str = 'natural'
                    note_name_full = step + ('' if acc_str == 'natural' else acc_str)  # es. 'Bb', 'C#', 'A'

                    # determina se l'alterazione è di PASSAGGIO
                    # (non dall'armatura). Confronta l'alterazione della nota
                    # con quella dell'armatura per questo step.
                    passing_acc = ''  # '', '#', 'b', or 'natural' (bequadro)
                    if step in keysig_altered_steps:
                        # Lo step è alterato nell'armatura. Se la nota ha
                        # alterazione diversa → di passaggio.
                        if acc_str != keysig_alteration:
                            passing_acc = acc_str  # es. 'natural' (bequadro) o alterazione opposta
                    else:
                        # Lo step NON è alterato nell'armatura. Se la nota ha
                        # un'alterazione (# o b) → di passaggio.
                        if acc_str in ('#', 'b'):
                            passing_acc = acc_str

                    # 9 Ago 2026 (bug diesis pentagramma, Marco): le note
                    # alterate nell'armatura (es. F#, C# in Re maggiore) devono
                    # mostrare il diesis ANCHE sul pentagramma (sopra/sotto il
                    # pallino), non solo sotto il blocco nella tavola.
                    # staff_acc = alterazione da disegnare sul pentagramma:
                    # - passing_acc se è un'alterazione di passaggio
                    # - acc_str se è un'alterazione dell'armatura (# o b)
                    # - 'natural' (bequadro) solo se di passaggio
                    staff_acc = passing_acc
                    if not staff_acc and acc_str in ('#', 'b'):
                        # Alterazione dell'armatura → mostrala sul pentagramma
                        staff_acc = acc_str

                    notes.append({
                        'pitch': n.pitch.midi,
                        'step': step,          # lettera naturale: C/D/E/F/G/A/B
                        'octave': n.pitch.octave,  # 9 Ago: per correggere Y con enarmonici
                        'note_name': note_name_full,  # con alterazione: Bb, C#, A
                        'passing_acc': passing_acc,  # 7 Ago: '#', 'b', 'natural', o ''
                        'staff_acc': staff_acc,  # 9 Ago: alterazione da disegnare sul pentagramma
                        'duration_type': dur_type,
                        'dots': dots,
                        'dur_key': dur_key,
                        'onset': offset,
                        'measure_idx': m_idx,
                        'n_chord_notes': 1,
                    })
        
        return {'notes': notes, 'rests': rests, 'time_sig': time_sig,
                'time_sigs_per_measure': time_sigs_per_measure,
                'key_sig': key_sig,
                'keysig_altered_steps': keysig_altered_steps,
                'keysig_alteration': keysig_alteration}
    finally:
        if os.path.exists(xml_path):
            os.unlink(xml_path)


def count_notes_per_measure(mscx_content):
    """Estrae il numero di note per ogni battuta dal contenuto .mscx."""
    measures = re.split(r'<Measure[^>]*>', mscx_content)
    # First element is everything before the first <Measure>, skip it
    counts = []
    for m in measures[1:]:
        # Stop at end of measure
        end_idx = m.find('</Measure>')
        if end_idx >= 0:
            m = m[:end_idx]
        # Count <Note> tags (but NOT <NoteDot>, <NoteLine>, etc.)
        # Use word boundary to avoid matching <NoteX>
        notes = len(re.findall(r'<Note>', m))
        counts.append(notes)
    return counts


def compute_system_breaks(note_counts, max_notes=MAX_NOTES_PER_SYSTEM,
                          default_measures=UNIFORM_MEASURES_PER_SYSTEM,
                          initial_rest_measures=0, mmrest_groups=None):
    """
    Calcola dopo quali battute inserire un LayoutBreak (a capo).
    
    UNIFORM measure width: tutte le battute hanno la stessa
    larghezza. 2 battute per rigo di default.
    
    Ogni gruppo MMRest va in un sistema separato.
    Il primo gruppo può occupare più battute (es. 28), ma il sistema le
    contiene tutte perché le pause equalizzate sono strette.
    """
    breaks = []
    i = 0
    n = len(note_counts)
    
    # gli MMRest sono ora SINGOLE battute (non splittate).
    # Ogni MMRest conta come 1 battuta nel layout.
    # Break: metti l'MMRest in un sistema separato (1 battuta sola).
    mmrest_start_set = set()
    if mmrest_groups:
        for gs, gc in mmrest_groups:
            mmrest_start_set.add(gs)
    
    # Primo sistema: se la prima battuta è un MMRest, mettila da sola
    if initial_rest_measures >= 2:
        # La prima battuta è un MMRest → sistema da 1 battuta
        breaks.append(0)  # break dopo la battuta 0 (MMRest)
        i = 1
    
    while i < n:
        # Se questa battuta è un MMRest, mettila in un sistema da sola
        if i in mmrest_start_set and i > 0:
            # Break prima
            if breaks and breaks[-1] != i - 1:
                breaks.append(i - 1)
            # Break dopo (se non è l'ultima)
            if i + 1 < n:
                breaks.append(i)
            i += 1
        elif i + 1 < n and (i + 1) in mmrest_start_set:
            # La prossima battuta è un MMRest → sistema da 1 battuta
            if i + 1 < n:
                breaks.append(i)
            i += 1
        else:
            count = min(default_measures, n - i)
            if i + count < n:  # don't break after the last measure
                breaks.append(i + count - 1)
            i += count
    return breaks


# ==============================================================================
# 2. MODIFICA .mscz PER LAYOUT ACCESSIBILE
# ==============================================================================

def _add_mmrests_to_mscx(mscx_content, note_info):
    """Aggiunge MMRest (multi-measure rests) per le pause consecutive nel .mscx.
    
    4 Ago 2026 (bug 56 regressione): la ricostruzione con music21 produce battute
    di pausa singole. Questa funzione rileva gruppi di pause consecutive (≥2 battute)
    e le converte in MMRest nativi di MuseScore 4, in modo che vengano renderizzati
    come una singola battuta con il numero di battute di pausa sopra.
    
    Args:
        mscx_content: stringa XML del .mscx
        note_info: dict con 'notes' e 'rests' (da extract_notes_via_music21)
    
    Returns:
        stringa XML del .mscx con MMRest aggiunti
    """
    import re as _re
    
    # Trova gruppi di battute di pausa consecutive (≥2)
    note_measures = set(n['measure_idx'] for n in note_info.get('notes', []))
    max_m = max(note_measures) if note_measures else 0
    
    # Trova anche dalle rests
    rest_measures = set(r['measure_idx'] for r in note_info.get('rests', []))
    all_measures = note_measures | rest_measures
    if all_measures:
        max_m = max(max_m, max(all_measures))
    
    # Trova gruppi di pause consecutive (battute senza note)
    pause_groups = []
    current_group = []
    for m in range(max_m + 1):
        if m not in note_measures:
            current_group.append(m)
        else:
            if len(current_group) >= 2:
                pause_groups.append(current_group)
            current_group = []
    if len(current_group) >= 2:
        pause_groups.append(current_group)
    
    if not pause_groups:
        return mscx_content  # nessun gruppo da convertire
    
    # Trova il time signature dal .mscx
    ts_match = _re.search(r'<sigN>(\d+)</sigN>\s*<sigD>(\d+)</sigD>', mscx_content)
    if ts_match:
        ts_n, ts_d = int(ts_match.group(1)), int(ts_match.group(2))
    else:
        ts_n, ts_d = 4, 4
    
    # Calcola la durata di una battuta in /d formato (es. 6/8 → 12/8)
    if ts_d in (1, 2, 4):
        measure_len = ts_n * (4 // ts_d)
    elif ts_d == 8:
        measure_len = ts_n * 2
    elif ts_d == 16:
        measure_len = ts_n * 4
    else:
        measure_len = ts_n
    
    print(f"      Aggiunta MMRest per {len(pause_groups)} gruppi di pause:")
    for g in pause_groups:
        print(f"        Battute {g[0]+1}-{g[-1]+1}: {len(g)} battute")
    
    # Parsa le battute dal .mscx
    # Trova lo staff con le misure (il primo <Staff> con <Measure> figli)
    staff_match = None
    for sm in _re.finditer(r'(<Staff[^>]*>)(.*?)(</Staff>)', mscx_content, _re.DOTALL):
        if '<Measure' in sm.group(2):
            staff_match = sm
            break
    
    if not staff_match:
        return mscx_content
    
    staff_open = staff_match.group(1)
    staff_content = staff_match.group(2)
    staff_close = staff_match.group(3)
    
    # Estrai tutte le battute dallo staff
    measure_pattern = r'<Measure[^>]*>.*?</Measure>'
    measures = list(_re.finditer(measure_pattern, staff_content, _re.DOTALL))
    
    if len(measures) != max_m + 1:
        print(f"      ⚠ Numero battute .mscx ({len(measures)}) ≠ attese ({max_m + 1}), skip MMRest")
        return mscx_content
    
    # Per ogni gruppo di pause, converti la prima battuta in MMRest e rimuovi le altre
    # Lavora in ordine inverso per preservare gli offset
    measures_to_remove = set()  # indici delle battute da rimuovere
    measures_to_modify = {}  # idx → nuovo XML
    
    for group in pause_groups:
        first_idx = group[0]
        count = len(group)
        
        # La prima battuta diventa MMRest
        first_measure = measures[first_idx].group(0)
        
        # Verifica che la battuta sia una pausa (contiene Rest ma non Chord)
        if '<Chord>' in first_measure:
            continue  # non è una pausa, skip
        
        # Aggiungi multiMeasureRest e cambia len
        # Rimuovi l'attributo len esistente se presente
        new_measure = _re.sub(r'<Measure[^>]*>', f'<Measure len="{count * measure_len}/{ts_d}">', first_measure, count=1)
        
        # Aggiungi <multiMeasureRest>N</multiMeasureRest> dopo il tag <Measure>
        if '<multiMeasureRest>' not in new_measure:
            new_measure = _re.sub(
                r'(<Measure[^>]*>)',
                f'\\1\n        <multiMeasureRest>{count}</multiMeasureRest>',
                new_measure,
                count=1
            )
        
        measures_to_modify[first_idx] = new_measure
        
        # Rimuovi le battute successive del gruppo
        for i in range(1, count):
            measures_to_remove.add(group[i])
    
    # Ricostruisci il contenuto dello staff
    new_staff_content = staff_content
    
    # Applica le modifiche in ordine inverso
    # Prima rimuovi le battute, poi modifica la prima
    # Raccogli tutti i cambiamenti come (start, end, replacement)
    changes = []
    for idx in measures_to_remove:
        changes.append((measures[idx].start(), measures[idx].end(), ''))
    for idx, new_xml in measures_to_modify.items():
        changes.append((measures[idx].start(), measures[idx].end(), new_xml))
    
    # Ordina per start inverso
    changes.sort(key=lambda c: -c[0])
    
    for start, end, replacement in changes:
        new_staff_content = new_staff_content[:start] + replacement + new_staff_content[end:]
    
    # Ricostruisci il .mscx
    new_mscx = mscx_content[:staff_match.start()] + staff_open + new_staff_content + staff_close + mscx_content[staff_match.end():]
    
    return new_mscx


def extract_single_part_mscz(input_mscz, part_index=0):
    """Estrae una singola parte da un .mscz multi-strumento.
    
    Usa music21 per leggere la parte specifica e MuseScore 4 per convertire
    il MusicXML risultante in .mscz. Questo evita il segfault di MuseScore 4
    quando si rimuovono staff direttamente dal .mscx.
    
    Ritorna il path del .mscz con una sola parte, o il path originale se il
    file ha già una sola parte.
    """
    import zipfile
    
    # Verifica se il file ha più parti
    with zipfile.ZipFile(input_mscz, 'r') as zf:
        mscx_name = [n for n in zf.namelist() if n.endswith('.mscx') and 'Excerpts' not in n][0]
        mscx_bytes = zf.read(mscx_name)
    
    import xml.etree.ElementTree as ET
    root = ET.fromstring(mscx_bytes)
    staff_ids = [s.get('id') for s in root.findall('.//Score/Staff') if s.get('id')]
    
    if len(staff_ids) <= 1:
        # 4 Ago 2026 (bug 55 redux): ANCHE per file a parte singola, ricostruisci
        # con music21. Il .mscx originale può avere battute MMRest/duplicate che
        # confondono make_accessible_mscz (perde le battute finali: 140→83).
        # Ricostruendo da music21 (che legge il MusicXML con 140 battute corrette)
        # otteniamo un .mscz pulito che make_accessible_mscz può processare.
        print(f"  Parte singola: ricostruzione con music21 per pulizia battute...")
        pass  # Non ritornare, continua con la ricostruzione sotto
    else:
        print(f"  Multi-strumento ({len(staff_ids)} staff): estrazione parte {part_index} via music21...")
    
    # Estrai note dalla parte specifica
    # 4 Ago 2026 (bug 55): usa extract_notes_via_music21 (tie-aware + rest-aware)
    # invece di extract_notes_from_mscz (parser XML diretto che perdeva le battute
    # finali vuote 84-139, causando tavola assente e note sbagliate batt.113-116).
    note_info = extract_notes_via_music21(input_mscz, part_index=part_index)
    
    # Crea score music21
    from music21 import stream, note as m21note, meter, key, clef
    from collections import defaultdict
    
    DUR_TO_QL = {
        'whole': 4.0, 'half': 2.0, 'quarter': 1.0, 'eighth': 0.5, '16th': 0.25,
        'half_dotted': 3.0, 'quarter_dotted': 1.5, 'eighth_dotted': 0.75,
        'whole_dotted': 6.0, '16th_dotted': 0.375,
    }
    
    s = stream.Score()
    p = stream.Part()
    ts = note_info.get('time_sig', (4, 4))
    # FIX #147/#152: per-measure time signatures (cambi di tempo).
    # time_sigs_per_measure = {measure_idx: (num, den)}
    time_sigs_pm = note_info.get('time_sigs_per_measure', {})
    p.insert(0, clef.TrebleClef())
    p.insert(0, meter.TimeSignature(f"{ts[0]}/{ts[1]}"))
    # 4 Ago 2026 (bug KS): usa la key signature reale estratta dal file originale.
    # key_sig = numero di diesis (positivo) o bemolli (negativo).
    key_sig_sharps = note_info.get('key_sig', 0)
    p.insert(0, key.KeySignature(sharps=key_sig_sharps))
    
    # calcola ql per measure rests in base al time signature
    # (whole=4.0 in 4/4, ma 3.0 in 6/8, 1.5 in 3/8, ecc.)
    if ts[1] in (1, 2, 4):
        measure_ql = ts[0] * (4 // ts[1])
    elif ts[1] == 8:
        measure_ql = ts[0] * 0.5
    elif ts[1] == 16:
        measure_ql = ts[0] * 0.25
    else:
        measure_ql = ts[0]
    
    # FIX #147/#152: helper per measure_ql per-battuta
    def _measure_ql_for(m_idx):
        m_ts = time_sigs_pm.get(m_idx, ts)
        if m_ts[1] in (1, 2, 4):
            return m_ts[0] * (4 // m_ts[1])
        elif m_ts[1] == 8:
            return m_ts[0] * 0.5
        elif m_ts[1] == 16:
            return m_ts[0] * 0.25
        else:
            return float(m_ts[0])
    
    events_by_measure = defaultdict(list)
    for n in note_info['notes']:
        events_by_measure[n['measure_idx']].append(('N', n['onset'], n['duration_type'], n['pitch'], n.get('dots', 0)))
    for r in note_info['rests']:
        events_by_measure[r['measure_idx']].append(('R', r['onset'], r['duration_type'], 0, 0, r.get('is_measure_rest', False)))
    
    max_measure = max(events_by_measure.keys()) if events_by_measure else 0
    _prev_ts = ts
    for m_idx in range(max_measure + 1):
        m = stream.Measure()
        # FIX #147/#152: insert time signature change if this measure has a different TS
        _cur_ts = time_sigs_pm.get(m_idx, _prev_ts)
        if _cur_ts != _prev_ts:
            m.insert(0, meter.TimeSignature(f"{_cur_ts[0]}/{_cur_ts[1]}"))
            _prev_ts = _cur_ts
        _m_ql = _measure_ql_for(m_idx)
        events = sorted(events_by_measure.get(m_idx, []), key=lambda e: e[1])
        for ev in events:
            t = ev[0]
            onset = ev[1]
            dtype = ev[2]
            pitch = ev[3]
            dots = ev[4]
            is_measure_rest = ev[5] if len(ev) > 5 else False
            if is_measure_rest:
                # usa la durata esatta del time signature, non whole=4.0
                ql = _m_ql
            else:
                dur_key = f"{dtype}_dotted" if dots > 0 else dtype
                ql = DUR_TO_QL.get(dur_key, DUR_TO_QL.get(dtype, 1.0))
            if ql == 0:
                ql = 1.0
            if t == 'N':
                n = m21note.Note(pitch, quarterLength=ql)
                m.append(n)
            else:
                r = m21note.Rest(quarterLength=ql)
                m.append(r)
        m.number = m_idx + 1  # 1-based measure number for MusicXML export
        p.append(m)
    
    s.append(p)
    
    # Esporta in MusicXML
    xml_path = '/tmp/single_part_' + str(os.getpid()) + '.xml'
    s.write('musicxml', xml_path)
    
    # inject beam modes from the original .mscx into the
    # MusicXML. music21 does not export beam modes, so MuseScore auto-calculates
    # them, which groups eighth notes incorrectly (e.g., beaming crome+semicrome
    # as separate notes with hooks instead of a single beam group).
    # We read BeamMode from the original .mscx and inject <beam> tags into
    # the MusicXML so MuseScore preserves the original beaming.
    try:
        mscx_text = mscx_bytes.decode('utf-8')
        orig_measures = re.findall(r'<Measure[^>]*>.*?</Measure>', mscx_text, re.DOTALL)
        # Extract beam modes: list of (measure_idx, chord_idx, beam_mode, duration_type)
        orig_beam_modes = {}  # (measure_idx, chord_idx) -> beam_mode
        orig_chord_dots = {}  # (measure_idx, chord_idx) -> dots count
        for m_idx, meas in enumerate(orig_measures):
            chords = re.findall(r'<Chord[^>]*>(.*?)</Chord>', meas, re.DOTALL)
            for c_idx, chord in enumerate(chords):
                beam = re.search(r'<BeamMode>(\w+)</BeamMode>', chord)
                dur = re.search(r'<durationType>(\w+)</durationType>', chord)
                dots_m = re.search(r'<dots>(\d+)</dots>', chord)
                dots_val = int(dots_m.group(1)) if dots_m else 0
                orig_chord_dots[(m_idx, c_idx)] = dots_val
                bm = beam.group(1) if beam else None
                dt = dur.group(1) if dur else None
                if bm and bm != 'none':
                    orig_beam_modes[(m_idx, c_idx)] = (bm, dt)
        
        if orig_beam_modes:
            # Read the generated MusicXML and inject beam tags
            with open(xml_path, 'r') as f:
                xml_content = f.read()
            # FIX: remove existing beam tags (music21 may add some auto-calculated
            # beams that conflict with our injected ones)
            xml_content = re.sub(r'<beam[^>]*>.*?</beam>\s*', '', xml_content)
            # For each measure, find notes (non-rest) and inject beam tags
            # in the same order as the original .mscx chords
            def _inject_beams_in_measure(m):
                meas = m.group(0)
                num_match = re.search(r'number="(\d+)"', meas)
                if not num_match:
                    return meas
                m_idx = int(num_match.group(1)) - 1  # 0-based
                # Find all <note> elements that are NOT rests
                note_pattern = r'<note[^>]*>.*?</note>'
                notes_in_meas = list(re.finditer(note_pattern, meas, re.DOTALL))
                # First pass (forward): assign chord_idx to each non-rest note
                # and determine what beam tag to insert
                insertions = []  # (note_start, note_end, new_note_text)
                chord_idx = 0
                for nm in notes_in_meas:
                    note_text = nm.group(0)
                    if '<rest/>' in note_text or '<rest />' in note_text:
                        continue  # skip rests
                    bm_info = orig_beam_modes.get((m_idx, chord_idx))
                    # Determine if this note ends a beam group:
                    # it ends if the PREVIOUS chord had a beam mode (begin/mid)
                    # but this one doesn't (none), OR it's the last chord.
                    prev_bm_info = orig_beam_modes.get((m_idx, chord_idx - 1)) if chord_idx > 0 else None
                    prev_had_beam = prev_bm_info is not None
                    # Track if previous note was a dotted eighth (croma puntata)
                    # → next 16th needs beam number="2">begin (not end) for secondary beam
                    prev_was_dotted_eighth = (prev_bm_info is not None
                                             and prev_bm_info[1] == 'eighth'
                                             and prev_bm_info[0] == 'begin'
                                             and orig_chord_dots.get((m_idx, chord_idx - 1), 0) > 0)
                    if bm_info:
                        bm, dt = bm_info
                        if bm == 'begin':
                            beam_val = 'begin'
                        elif bm == 'mid':
                            beam_val = 'continue'
                        else:
                            beam_val = 'end'
                        type_match = re.search(r'<type>(\w+)</type>', note_text)
                        if type_match:
                            beam_tag = f'<beam number="1">{beam_val}</beam>'
                            if dt == '16th' and beam_val in ('begin', 'continue'):
                                beam_tag += f'<beam number="2">{beam_val}</beam>'
                            elif dt == '16th' and beam_val == 'end':
                                beam_tag += '<beam number="2">end</beam>'
                            new_note = note_text[:type_match.end()] + beam_tag + note_text[type_match.end():]
                            insertions.append((nm.start(), nm.end(), new_note))
                    elif prev_had_beam:
                        # This note has no beam mode but the previous one did → end beam
                        # Determine duration type from the note
                        type_match = re.search(r'<type>(\w+)</type>', note_text)
                        if type_match:
                            dt = type_match.group(1)
                            beam_tag = f'<beam number="1">end</beam>'
                            if dt == '16th':
                                # If previous was a dotted eighth (croma puntata),
                                # the 16th is beamed to it → secondary beam BEGINS here
                                # (the 16th has its own secondary beam, short and visible).
                                # Otherwise (e.g. last of 4 sixteenths), secondary beam ENDS.
                                if prev_was_dotted_eighth:
                                    beam_tag += '<beam number="2">begin</beam>'
                                else:
                                    beam_tag += '<beam number="2">end</beam>'
                            new_note = note_text[:type_match.end()] + beam_tag + note_text[type_match.end():]
                            insertions.append((nm.start(), nm.end(), new_note))
                    chord_idx += 1
                # Apply insertions in reverse order to preserve offsets
                new_meas = meas
                for start, end, new_note in reversed(insertions):
                    new_meas = new_meas[:start] + new_note + new_meas[end:]
                return new_meas
            xml_content = re.sub(r'<measure[^>]*>.*?</measure>', _inject_beams_in_measure, xml_content, flags=re.DOTALL)
            with open(xml_path, 'w') as f:
                f.write(xml_content)
            print(f"  Beam modes injected from .mscx ({len(orig_beam_modes)} beams)")
    except Exception as e:
        print(f"  Warning: beam injection failed: {e}")
    
    # inserisci <print new-system="yes"/> nel
    # MusicXML PRIMA della conversione MuseScore. Questo "congela" le beam info
    # nei Chord dell'.mscx. Senza questo, i LayoutBreak aggiunti dopo da
    # make_accessible_mscz causano il ricalcolo auto-beam di MuseScore, che
    # distrugge gli hook delle crome isolate (beaming le crome alle semicrome
    # adiacenti o raggruppandole erroneamente).
    # I break di sistema vengono calcolati con compute_system_breaks (stessa
    # funzione usata da make_accessible_mscz per i LayoutBreak).
    note_counts_by_measure = defaultdict(int)
    for n in note_info['notes']:
        note_counts_by_measure[n['measure_idx']] += 1
    total_measures = max_measure + 1
    note_counts = [note_counts_by_measure.get(i, 0) for i in range(total_measures)]
    initial_rest = note_info.get('initial_rest_measures', 0)
    mmrest_groups = note_info.get('mmrest_groups', [])
    break_after = compute_system_breaks(note_counts,
                                         initial_rest_measures=initial_rest,
                                         mmrest_groups=mmrest_groups)
    break_before = set(b + 1 for b in break_after if b + 1 < total_measures)
    if break_before:
        with open(xml_path, 'r') as f:
            xml_content = f.read()
        # Inserisci <print new-system="yes"/> all'inizio delle <measure number="N">
        # che seguono un break. MuseScore legge questo tag e crea un nuovo sistema
        # senza ricalcolare le beam.
        def _insert_print(m):
            meas = m.group(0)
            num_match = re.search(r'number="(\d+)"', meas)
            if num_match:
                meas_num = int(num_match.group(1))
                # measure number is 1-based; break_before is 0-based measure index
                if (meas_num - 1) in break_before:
                    # Insert right after the <measure ...> opening tag
                    close = meas.find('>')
                    if close > 0:
                        meas = meas[:close+1] + '<print new-system="yes"/>' + meas[close+1:]
            return meas
        xml_content = re.sub(r'<measure[^>]*>.*?</measure>', _insert_print, xml_content, flags=re.DOTALL)
        with open(xml_path, 'w') as f:
            f.write(xml_content)
    
    # Converti in .mscz con MuseScore 4
    mscz_path = '/tmp/single_part_' + str(os.getpid()) + '.mscz'
    cmd = f'{XVFB} {MUSESCORE_CMD} -o "{mscz_path}" "{xml_path}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    
    if result.returncode != 0 or not os.path.exists(mscz_path):
        raise RuntimeError(f"Failed to create single-part .mscz: {result.stderr[-200:]}")
    
    # rimuovi accidentals ridondanti dal .mscx intermedio.
    # music21 aggiunge ~71 bequadri non necessari (uno per ogni nota naturale
    # dopo un'alterazione nella stessa battuta). Per l'allievo (dislessico),
    # gli accidentals nel pentagramma sono solo confusione visiva —
    # la tavola sonora colorata mostra già il nome della nota.
    import tempfile, shutil
    tmp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(mscz_path, 'r') as zf:
            zf.extractall(tmp_dir)
        mscx_file = [f for f in os.listdir(tmp_dir) if f.endswith('.mscx')][0]
        mscx_path = os.path.join(tmp_dir, mscx_file)
        with open(mscx_path, 'r') as f:
            mscx_content = f.read()
        # Rimuovi tutti gli elementi <Accidental>...</Accidental>
        import re as _re
        cleaned = _re.sub(r'<Accidental>.*?</Accidental>', '', mscx_content, flags=_re.DOTALL)
        with open(mscx_path, 'w') as f:
            f.write(cleaned)
        
        # 4 Ago 2026 (bug 56 regressione): aggiungi MMRest per le pause consecutive.
        # La ricostruzione con music21 produce battute di pausa singole (nessun MMRest).
        # Le pause d'aspetto vanno raggruppate in una singola battuta con il
        # numero di battute sopra. Rileviamo le pause consecutive nel .mscx e le
        # convertiamo in MMRest nativi di MuseScore 4.
        cleaned = _add_mmrests_to_mscx(cleaned, note_info)
        with open(mscx_path, 'w') as f:
            f.write(cleaned)
        
        # Ri-comprimi in .mscz
        os.unlink(mscz_path)
        with zipfile.ZipFile(mscz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root_dir, dirs, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zf.write(file_path, arcname)
    finally:
        shutil.rmtree(tmp_dir)
    
    # Cleanup
    os.unlink(xml_path)
    
    print(f"  ✓ Parte {part_index} estratta in .mscz singolo ({os.path.getsize(mscz_path)} bytes)")
    return mscz_path


def make_accessible_mscz(input_mscz, output_mscz, part_index=0, rhythm_mode=False):
    """Crea una copia del .mscz con spatium grande, pagina larga e senza line break.
    
    part_index: quale parte/strumento mantenere (0=primo, 1=secondo, ecc.)
    Default = 0 (prima parte).
    
    2 Ago 2026: se il file ha più strumenti, estrae solo la parte richiesta
    usando music21 per creare un .mscz intermedio (MuseScore 4 va in segfault
    se modifichiamo il .mscx rimuovendo gli staff direttamente).
    """
    temp_dir = '/tmp/mscz_modify_' + str(os.getpid())
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)
    
    # Initialize defaults (may be overridden by MMRest detection below)
    initial_rest_measures = 0
    mmrest_groups = []
    mmrest_info = []
    
    with zipfile.ZipFile(input_mscz, 'r') as zf:
        zf.extractall(temp_dir)
    
    # 1. Dynamic line breaks: compute how many measures per system based on note density
    # 4 measures per line by default, but reduce when
    # there are many eighth/16th notes that would make the line too crowded.
    for fname in os.listdir(temp_dir):
        if fname.endswith('.mscx'):
            mscx_path = os.path.join(temp_dir, fname)
            with open(mscx_path, 'r') as f:
                mscx = f.read()
            
            # Remove existing LayoutBreak elements (we'll add our own)
            mscx = re.sub(r'<LayoutBreak>.*?</LayoutBreak>\s*', '', mscx, flags=re.DOTALL)
            
            # rimuovi battute iniziali che contengono SOLO pause
            # di intera battuta (durationType=measure). Queste sono intro/pickup
            # rests che non servono all'allievo e causano numeri battuta sbagliati.
            # Rimuove solo le battute INIZIALI consecutive (non in mezzo al brano).
            # NOTA: ci possono essere multipli <Staff> elementi — il primo è metadata
            # (senza misure), dobbiamo trovare quello CON <Measure> figli.
            all_staff_matches = list(re.finditer(r'<Staff[^>]*>(.*?)</Staff>', mscx, re.DOTALL))
            staff_match = None
            for sm in all_staff_matches:
                if '<Measure' in sm.group(1):
                    staff_match = sm
                    break
            if staff_match:
                staff_xml = staff_match.group(1)
                # le battute di pausa iniziali NON vengono più rimosse.
                # il brano va suonato con altri, l'allievo deve vedere
                # le battute di attesa per contare le pause.
                
                # Le prime 4 battute di pausa vanno raggruppate
                # in un MMRest con "4". Approccio: convertiamo TUTTE le pause
                # measure→whole (MuseScore le renderizza come pause di semibreve),
                # poi nel post-processore SVG sostituiamo le prime 4 con un
                # MMRest disegnato a mano (rettangolo verticale + numero "4").
                
                # gestisci MMRest nativo di MuseScore 4.
                # MuseScore 4 raggruppa le pause iniziali in una battuta con
                # len="N/4" e multiMeasureRest=N. Inoltre aggiunge una pausa
                # extra M1 prima del MMRest e battute duplicate M3-M5 dopo.
                mmrest_match = re.search(r'<multiMeasureRest>(\d+)</multiMeasureRest>', mscx)
                if mmrest_match:
                    mmrest_num = int(mmrest_match.group(1))
                    
                    # Extract global time signature for MMRest split fallback
                    # (only the first MMRest has TimeSig; others don't)
                    global_ts_match = re.search(r'<sigN>(\d+)</sigN>\s*<sigD>(\d+)</sigD>', mscx)
                    global_ts_n = global_ts_match.group(1) if global_ts_match else '4'
                    global_ts_d = global_ts_match.group(2) if global_ts_match else '4'
                    print(f"      MMRest nativo: {mmrest_num} battute di pausa")
                    
                    # Rimuovi M0 (pausa extra prima del MMRest) ma preserva KeySig
                    all_m = list(re.finditer(r'<Measure[^>]*>(.*?)</Measure>', mscx, re.DOTALL))
                    saved_keysig = None
                    if len(all_m) >= 2:
                        m0 = all_m[0].group(1)
                        m1 = all_m[1].group(1)
                        if ('<Rest>' in m0 and '<multiMeasureRest>' not in m0 and
                            '<multiMeasureRest>' in m1):
                            # Salva il KeySig da M0 prima di rimuoverla
                            ks_match = re.search(r'<KeySig>.*?</KeySig>', m0, re.DOTALL)
                            if ks_match:
                                saved_keysig = ks_match.group(0)
                            mscx = mscx[:all_m[0].start()] + mscx[all_m[0].end():]
                            print(f"      Rimossa M0 (pausa extra, KeySig={'preservato' if saved_keysig else 'perso'})")
                    
                    # Salva le lunghezze di TUTTI gli MMRest
                    # PRIMA dello split, per poter rimuovere le battute vuote duplicate dopo.
                    # Root cause: nel .mscz originale, ogni MMRest(N) è seguito da N-1
                    # battute di pausa vuote. Lo split crea N battute, ma le N-1 vuote
                    # rimangono → 2N-1 battute (dovrebbero essere N).
                    # Soluzione: rimuovi le N-1 battute vuote DOPO ogni MMRest PRIMA dello split.
                    _all_m_pre = list(re.finditer(r'<Measure[^>]*>(.*?)</Measure>', mscx, re.DOTALL))
                    _remove_offsets = []  # (start, end) da rimuovere
                    for _idx, _m in enumerate(_all_m_pre):
                        _mmr = re.search(r'<multiMeasureRest>(\d+)</multiMeasureRest>', _m.group(1))
                        if _mmr:
                            _n = int(_mmr.group(1))
                            # Rimuovi la pausa PRIMA dell'MMRest (se esiste ed è una pausa)
                            # Questa pausa è parte del gruppo di pause che l'MMRest rappresenta
                            if _idx > 0:
                                _prev_c = _all_m_pre[_idx - 1].group(1)
                                if ('<Rest>' in _prev_c and '<Chord>' not in _prev_c 
                                        and '<multiMeasureRest>' not in _prev_c):
                                    _remove_offsets.append((_all_m_pre[_idx - 1].start(), _all_m_pre[_idx - 1].end()))
                            # Rimuovi le N-1 battute di pausa vuote DOPO questo MMRest
                            _removed = 0
                            for _j in range(_idx + 1, len(_all_m_pre)):
                                _c = _all_m_pre[_j].group(1)
                                if ('<Rest>' in _c and '<Chord>' not in _c 
                                        and '<multiMeasureRest>' not in _c):
                                    _remove_offsets.append((_all_m_pre[_j].start(), _all_m_pre[_j].end()))
                                    _removed += 1
                                    if _removed >= _n - 1:
                                        break
                                else:
                                    break
                    # Rimuovi in ordine inverso per preservare gli offset
                    for _s, _e in sorted(_remove_offsets, reverse=True):
                        mscx = mscx[:_s] + mscx[_e:]
                    if _remove_offsets:
                        print(f"      Rimossi {len(_remove_offsets)} battute pausa duplicate (prima dello split)")
                    
                    # NON splittare gli MMRest. Mantenerli come battuta
                    # singola. Rimuovere il tag multiMeasureRest (MuseScore non può
                    # aprire il file se il count non corrisponde al numero di battute).
                    # Convertire la battuta in una pausa measure normale.
                    # Il post-processore SVG riconoscerà le battute MMRest dagli indici
                    # salvati in mmrest_info e disegnerà il box nero + numero.
                    
                    # Salva TUTTI gli MMRest: lista di (measure_idx, count)
                    _all_m_after = list(re.finditer(r'<Measure[^>]*>(.*?)</Measure>', mscx, re.DOTALL))
                    mmrest_info = []  # (measure_idx, count)
                    for _idx, _m in enumerate(_all_m_after):
                        _mmr = re.search(r'<multiMeasureRest>(\d+)</multiMeasureRest>', _m.group(1))
                        if _mmr:
                            mmrest_info.append((_idx, int(_mmr.group(1))))
                    print(f"      MMRest preservati (non splittati):")
                    for mi, count in mmrest_info:
                        print(f"        Battuta {mi+1}: MMRest({count})")
                    
                    # Rimuovi i tag multiMeasureRest e breakMultiMeasureRest
                    # (MuseScore non può aprire il file se multiMeasureRest=N ma c'è 1 battuta)
                    mscx = re.sub(r'<multiMeasureRest>\d*</multiMeasureRest>', '', mscx)
                    mscx = re.sub(r'<breakMultiMeasureRest>\d*</breakMultiMeasureRest>', '', mscx)
                    
                    # Converti <Measure len="N/M"> in <Measure> (rimuovi l'attributo len)
                    mscx = re.sub(r'<Measure len="[^"]*">', '<Measure>', mscx)
                    
                    # Correggi i <duration> delle pause measure nelle battute MMRest.
                    # Il duration originale era "N×ts" (es. 28×6/8=168/8), ma ora è 1 battuta.
                    # Sostituisci con la durata di 1 battuta (ts_n/ts_d).
                    _ts_fraction = f"{global_ts_n}/{global_ts_d}"
                    mscx = re.sub(r'<duration>\d+/\d+</duration>\s*</Rest>',
                                  f'<duration>{_ts_fraction}</duration></Rest>', mscx)
                    
                    # Assicurati che ogni battuta MMRest abbia una pausa measure
                    # (alcune battute MMRest potrebbero non avere un <Rest> esplicito)
                    all_m_final = list(re.finditer(r'<Measure>(.*?)</Measure>', mscx, re.DOTALL))
                    for _idx, _m in enumerate(all_m_final):
                        _content = _m.group(1)
                        _is_mmrest = any(mi == _idx for mi, _ in mmrest_info)
                        if _is_mmrest and '<Rest>' not in _content:
                            # Aggiungi una pausa measure
                            _ts = re.search(r'<sigN>(\d+)</sigN>\s*<sigD>(\d+)</sigD>', _content)
                            if _ts:
                                _dur = f"{_ts.group(1)}/{_ts.group(2)}"
                            else:
                                _dur = f"{global_ts_n}/{global_ts_d}"
                            _new_content = _content.replace('<voice>', 
                                f'<voice><Rest><durationType>measure</durationType><duration>{_dur}</duration></Rest>', 1)
                            mscx = mscx[:_m.start()] + f'<Measure>{_new_content}</Measure>' + mscx[_m.end():]
                    
                    initial_rest_measures = mmrest_info[0][1] if mmrest_info else 0
                else:
                    initial_rest_measures = 0
                    mscx = re.sub(r'<multiMeasureRest>\d*</multiMeasureRest>', '', mscx)
                    mscx = re.sub(r'<breakMultiMeasureRest>\d*</breakMultiMeasureRest>', '', mscx)
                
                if initial_rest_measures >= 2:
                    print(f"      MMRest: prime {min(initial_rest_measures, 4)} battute pausa → MMRest manuale nel SVG")
                else:
                    # No initial rest block — keep measure rests as-is
                    # NON convertire measure→whole: in 6/8 whole=4qb ≠ measure=3qb
                    mscx = re.sub(r'<duration>\d+/\d+</duration>\s*</Rest>',
                                 '</Rest>', mscx)
                    mscx = re.sub(r'<multiMeasureRest>\d*</multiMeasureRest>', '', mscx)
                    mscx = re.sub(r'<breakMultiMeasureRest>\d*</breakMultiMeasureRest>', '', mscx)
                
            
            # se il file ha più strumenti, estrai solo part_index.
            # Verifica se ci sono più <Staff id="N"> con misure.
            staff_ids = re.findall(r'<Staff id="(\d+)">', mscx)
            if len(staff_ids) > 1:
                # Multi-strumento: il .mscz intermedio è già stato creato con music21
                # (vedi extract_single_part_mscz nel main). Qui usiamo quel file.
                # Il .mscx in temp_dir è già stato estratto dal .mscz intermedio.
                print(f"      Multi-strumento: uso parte {part_index} (già estratta)")
            
            # estrai TUTTI i gruppi di battute di pausa consecutive
            # dal .mscx processato (dopo split MMRest). Ogni gruppo è (start_idx, count).
            # Questi verranno ricostruiti come MMRest nel post-processore SVG.
            # usa mmrest_info (già calcolato sopra) invece di rilevare
            # gruppi di pause consecutive. mmrest_info = [(measure_idx, count)]
            # Converti in mmrest_groups = [(start_idx, count)] (formato atteso dal resto)
            if mmrest_info:
                mmrest_groups = mmrest_info  # already [(idx, count)]
            else:
                mmrest_groups = []
            print(f"      Gruppi MMRest: {len(mmrest_groups)}")
            for gs, gc in mmrest_groups:
                print(f"        Battuta {gs+1}: MMRest({gc})")
            
            # Count notes per measure and compute where to break
            note_counts = count_notes_per_measure(mscx)
            break_indices = compute_system_breaks(note_counts, 
                                                    initial_rest_measures=initial_rest_measures,
                                                    mmrest_groups=mmrest_groups)
            
            # Print system layout (compute actual groups from breaks)
            sys_start = 0
            for bi in break_indices:
                group_notes = note_counts[sys_start:bi+1]
                group_range = f"M{sys_start+1}-M{bi+1}"
                print(f"      {group_range} ({len(group_notes)} meas, {sum(group_notes)} notes: {group_notes})")
                sys_start = bi + 1
            if sys_start < len(note_counts):
                group_notes = note_counts[sys_start:]
                print(f"      M{sys_start+1}-M{len(note_counts)} ({len(group_notes)} meas, {sum(group_notes)} notes: {group_notes})")
            
            # Insert LayoutBreak after the specified measures
            # Also insert a PAGE break after the 5th system to split across 2 pages
            measure_count = [0]  # mutable counter
            break_set = set(break_indices)
            # Page break: split systems across 2 pages.
            # With 2 measures per system: break_indices = [1,3,5,7,9,11,13,...]
            # Page break: split systems across pages (5 systems per page for ledger line space)
            # in rhythm mode, NON inseriamo page break — lasciamo che
            # MuseScore faccia il flow automatico. I sistemi rhythm sono compatti
            # (~1000px per sistema) e MuseScore ne mette ~10-12 per pagina A4.
            # Inserendo page break manuali, MuseScore crea pagine quasi vuote.
            page_break_set = set()
            n_systems = len(break_indices) + 1  # total systems
            if not rhythm_mode:
                systems_per_page = 5  # reduced from 7 to 5 for ledger line clearance
                # Add page break after every systems_per_page systems
                for page_end in range(systems_per_page, n_systems, systems_per_page):
                    if page_end - 1 < len(break_indices):
                        page_break_set.add(break_indices[page_end - 1])
            else:
                # In rhythm mode, page break ogni 8 sistemi.
                # Prima non li inserivamo (flow automatico MuseScore) ma metteva
                # 9-10 sistemi/pagina → ultimo sistema tagliato (overflow 772-2372px).
                # 8 sistemi × ~1625px gap = ~13000px, entra in pagina 14028px.
                systems_per_page = 8
                for page_end in range(systems_per_page, n_systems, systems_per_page):
                    if page_end - 1 < len(break_indices):
                        page_break_set.add(break_indices[page_end - 1])
            def add_linebreak(match):
                measure_count[0] += 1
                measure_xml = match.group(0)
                idx = measure_count[0] - 1
                if idx in page_break_set:
                    # PAGE break (new page)
                    insert_pos = measure_xml.rfind('</Measure>')
                    if insert_pos > 0:
                        lb = '<LayoutBreak><subtype>page</subtype><name></name></LayoutBreak>'
                        measure_xml = measure_xml[:insert_pos] + lb + measure_xml[insert_pos:]
                elif idx in break_set:
                    # LINE break (same page, next system)
                    insert_pos = measure_xml.rfind('</Measure>')
                    if insert_pos > 0:
                        lb = '<LayoutBreak><subtype>line</subtype><name></name></LayoutBreak>'
                        measure_xml = measure_xml[:insert_pos] + lb + measure_xml[insert_pos:]
                return measure_xml
            
            mscx = re.sub(r'<Measure>.*?</Measure>', add_linebreak, mscx, flags=re.DOTALL)
            
            with open(mscx_path, 'w') as f:
                f.write(mscx)
            break
    
    # 2. Modify style: spatium, page size
    mss_path = os.path.join(temp_dir, 'score_style.mss')
    if os.path.exists(mss_path):
        with open(mss_path, 'r') as f:
            mss = f.read()
        
        mss = re.sub(r'<spatium>[\d.]+</spatium>', f'<spatium>{SPATIUM}</spatium>', mss)
        mss = re.sub(r'<pageWidth>[\d.]+</pageWidth>', f'<pageWidth>{PAGE_WIDTH}</pageWidth>', mss)
        mss = re.sub(r'<pageHeight>[\d.]+</pageHeight>', f'<pageHeight>{PAGE_HEIGHT}</pageHeight>', mss)
        mss = re.sub(r'<staffLineWidth>[\d.]+</staffLineWidth>', 
                     f'<staffLineWidth>{STAFF_LINE_WIDTH}</staffLineWidth>', mss)
        
        # minMeasureWidth basso per permettere layout flessibile
        if '<minMeasureWidth>' in mss:
            mss = re.sub(r'<minMeasureWidth>[\d.]+</minMeasureWidth>',
                         '<minMeasureWidth>18</minMeasureWidth>', mss)
        else:
            mss = mss.rstrip()
            if mss.endswith('</Style>'):
                mss = mss[:-len('</Style>')] + '<minMeasureWidth>18</minMeasureWidth></Style>'
            else:
                mss += '<minMeasureWidth>18</minMeasureWidth>'
        
        # disabilita MMRest automatico di MuseScore.
        # Le battute MMRest sono ora singole pause measure (non splittate).
        # Il post-processore SVG le riconosce dagli indici mmrest_info e
        # disegna il box nero + numero manualmente.
        if '<createMultiMeasureRests>' in mss:
            mss = re.sub(r'<createMultiMeasureRests>\w*</createMultiMeasureRests>',
                         '<createMultiMeasureRests>0</createMultiMeasureRests>', mss)
        else:
            mss = mss.rstrip()
            if mss.endswith('</Style>'):
                mss = mss[:-len('</Style>')] + '<createMultiMeasureRests>0</createMultiMeasureRests></Style>'
            else:
                mss += '<createMultiMeasureRests>0</createMultiMeasureRests>'
        
        # minEmptyMeasures alto per evitare raggruppamento automatico
        if '<minEmptyMeasures>' in mss:
            mss = re.sub(r'<minEmptyMeasures>\w*</minEmptyMeasures>',
                         '<minEmptyMeasures>999</minEmptyMeasures>', mss)
        else:
            mss = mss.rstrip()
            if mss.endswith('</Style>'):
                mss = mss[:-len('</Style>')] + '<minEmptyMeasures>999</minEmptyMeasures></Style>'
            else:
                mss += '<minEmptyMeasures>999</minEmptyMeasures>'
        
        with open(mss_path, 'w') as f:
            f.write(mss)
    
    # Repack
    if os.path.exists(output_mscz):
        os.remove(output_mscz)
    
    with zipfile.ZipFile(output_mscz, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root_dir, dirs, files in os.walk(temp_dir):
            for file in files:
                file_path = os.path.join(root_dir, file)
                arcname = os.path.relpath(file_path, temp_dir)
                zf.write(file_path, arcname)
    
    shutil.rmtree(temp_dir)
    return output_mscz, initial_rest_measures, mmrest_groups


# ==============================================================================
# 3. ESPORTA SVG DA MUSESCORE 4
# ==============================================================================

def export_svg(mscz_path, output_prefix):
    """Esporta il .mscz in SVG usando MuseScore 4 CLI."""
    svg_pattern = output_prefix + '.svg'
    
    cmd = f'{XVFB} {MUSESCORE_CMD} -o "{svg_pattern}" "{mscz_path}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    
    if result.returncode != 0:
        print(f"MuseScore export error: {result.stderr[-300:]}")
        return None
    
    # MuseScore may create multiple pages: prefix-1.svg, prefix-2.svg, etc.
    # CRITICAL: sort by page NUMBER, not alphabetically (svg-10 before svg-2 is wrong)
    # Handle both relative and absolute output_prefix paths.
    search_dir = os.path.dirname(output_prefix) or '.'
    prefix_basename = os.path.basename(output_prefix)
    svg_files = [os.path.join(search_dir, f) if search_dir != '.' else f
                 for f in os.listdir(search_dir)
                 if f.startswith(prefix_basename + '-') and f.endswith('.svg')]
    # Extract page number from filename for correct sorting
    import re as _re
    def _page_num(fname):
        m = _re.search(r'-(\d+)\.svg$', fname)
        return int(m.group(1)) if m else 0
    svg_files.sort(key=_page_num)
    
    if not svg_files:
        # Try without number (single page)
        if os.path.exists(svg_pattern):
            return [svg_pattern]
        return None
    
    return svg_files


# ==============================================================================
# 4. POST-PROCESS SVG
# ==============================================================================

# Color scheme
NOTE_COLORS = {
    'C': '#E53935', 'D': '#FB8C00', 'E': '#FDD835', 'F': '#64DD17',
    'G': '#00695C', 'A': '#1E88E5', 'B': '#8E24AA',
}
NOTE_TEXT_COLOR = {
    # Regola mista accessibilita dislessici:
    # testo NERO su sfondi luminosi (Re arancione, Mi giallo, Fa lime)
    # testo BIANCO su sfondi scuri (Do rosso, Sol ottanio, La blu, Si viola)
    'C': '#FFFFFF', 'D': '#111111', 'E': '#000000', 'F': '#000000',
    'G': '#FFFFFF', 'A': '#FFFFFF', 'B': '#FFFFFF',
}
# Colori per note aperte (whole/half): il cerchio e' bianco con bordo colorato.
# Per Re/Fa usiamo versioni scure (i colori originali chiari non passano WCAG AA sul bianco).
# Mi mantiene il giallo originale #FDD835: il testo nero
# assicura il contrasto di lettura, il bordo giallo e' decorativo e coerente col gambo.
# Do/Sol/La/Si sono gia' abbastanza scuri → usiamo il colore originale.
NOTE_COLORS_DARK = {
    'C': '#E53935',  # rosso: 4.23:1 OK
    'D': '#E65100',  # arancione scuro: 3.79:1 (era #FB8C00 2.37:1 FAIL)
    'E': '#FDD835',  # giallo originale (bordo decorativo, testo nero)
    'F': '#558B2F',  # verde scuro: 4.10:1 (era #64DD17 1.77:1 FAIL)
    'G': '#00695C',  # ottanio: 6.61:1 OK
    'A': '#1E88E5',  # blu: 3.68:1 OK
    'B': '#8E24AA',  # viola: 7.04:1 OK
}
# ------------------------------------------------------------------------------
# NOTE NAMES — multilingual (selected by --lang at runtime)
# ------------------------------------------------------------------------------
# NOTE_NAMES_PALLINI: short labels inside the colored circles (pallini).
# NOTE_NAMES_TAVOLA:  full labels in the sound table (tavola sonora).
# NOTE_NAMES_TAVOLA_SPLIT: (label, accidental-symbol) for the tavola, where the
#   accidental is rendered as a separate '#' or 'b' BELOW the note name.
# Keys are pitch names as exported by music21 (C, C#, Db, D, ...).
# To add a language: add a new key (e.g. 'fr') to each dictionary.
# ------------------------------------------------------------------------------
NOTE_NAMES_PALLINI = {
    'it': {'C': 'Do', 'D': 'Re', 'E': 'Mi', 'F': 'Fa',
           'G': 'So', 'A': 'La', 'B': 'Si'},   # So nei pallini (abbreviato)
    'en': {'C': 'C',  'D': 'D',  'E': 'E',  'F': 'F',
           'G': 'G',  'A': 'A',  'B': 'B'},
}

NOTE_NAMES_TAVOLA = {
    'it': {'C': 'Do', 'C#': 'Do#', 'Db': 'Db', 'D': 'Re', 'D#': 'Re#', 'Eb': 'Mib',
           'E': 'Mi', 'F': 'Fa', 'F#': 'Fa#', 'Gb': 'Solb', 'G': 'Sol',
           'G#': 'Sol#', 'Ab': 'Lab', 'A': 'La', 'A#': 'La#', 'Bb': 'Sib', 'B': 'Si'},
    'en': {'C': 'C',  'C#': 'C#',  'Db': 'Db', 'D': 'D',  'D#': 'D#',  'Eb': 'Eb',
           'E': 'E',  'F': 'F',  'F#': 'F#',  'Gb': 'Gb',  'G': 'G',
           'G#': 'G#', 'Ab': 'Ab', 'A': 'A',  'A#': 'A#',  'Bb': 'Bb', 'B': 'B'},
}

NOTE_NAMES_TAVOLA_SPLIT = {
    'it': {'C': ('Do', ''),  'C#': ('Do', '#'),  'Db': ('Re', 'b'),
           'D': ('Re', ''),  'D#': ('Re', '#'),  'Eb': ('Mi', 'b'),
           'E': ('Mi', ''),  'F': ('Fa', ''),  'F#': ('Fa', '#'),  'Gb': ('Sol', 'b'),
           'G': ('Sol', ''), 'G#': ('Sol', '#'), 'Ab': ('La', 'b'),
           'A': ('La', ''),  'A#': ('La', '#'),  'Bb': ('Si', 'b'), 'B': ('Si', '')},
    'en': {'C': ('C', ''),  'C#': ('C', '#'),  'Db': ('Db', 'b'),
           'D': ('D', ''),  'D#': ('D', '#'),  'Eb': ('Eb', 'b'),
           'E': ('E', ''),  'F': ('F', ''),  'F#': ('F', '#'),  'Gb': ('Gb', 'b'),
           'G': ('G', ''), 'G#': ('G', '#'), 'Ab': ('Ab', 'b'),
           'A': ('A', ''),  'A#': ('A', '#'),  'Bb': ('Bb', 'b'), 'B': ('B', '')},
}

# Default language (overridden by --lang at runtime). 'it' = Italian (Do Re Mi ...).
LANG = 'it'

# Convenience aliases used throughout the code — point to the active language.
# These are reassigned in main() after parsing --lang.
NOTE_NAMES_EN = NOTE_NAMES_PALLINI[LANG]            # backward-compat name (pallini)
NOTE_NAMES_IT_TAVOLA = NOTE_NAMES_TAVOLA[LANG]       # backward-compat name (tavola)
NOTE_NAMES_IT_TAVOLA_SPLIT = NOTE_NAMES_TAVOLA_SPLIT[LANG]

NOTEHEAD_WIDTH = 176
NOTEHEAD_HEIGHT = 106
NOTEHEAD_CENTER_OFFSET = 88
FONT_SIZE_1CHAR = 85
FONT_SIZE_2CHAR = 65
# Stroke-width delle linee pentagramma nell'SVG
# Con spatium 2.2: ~11.43. Cerchiamo dinamicamente nel SVG.
STAFF_LINE_WIDTH_NEW = 18
BG_COLOR_LIGHT = '#E8E8E8'
BG_COLOR_DARK = '#B8B8B8'
BG_OPACITY = 0.85
DURATION_RECT_HEIGHT = 28
DURATION_RECT_Y_OFFSET = 30
DURATION_RECT_OPACITY = 0.75
DURATION_RECT_RADIUS = 6

# Disc radius override: when spatium is small (for 4-measures-per-line on A4 portrait),
# the auto-calculated disc_r is too small for readability. This override makes
# colored circles ~1.4 staff spaces regardless of spatium. Set to None for auto.
DISC_R_OVERRIDE = 110  # reduced from 130 for 3 meas/system (beat_w=550)

# Invariant assertions on layout constants.
# Checked at import time — if someone changes a constant and breaks the geometry,
# the script fails immediately instead of producing a corrupted PDF.
assert DISC_R_OVERRIDE * 2 < BEAT_WIDTH, (
    f"Disc diameter ({DISC_R_OVERRIDE*2}px) must fit in a beat sector ({BEAT_WIDTH}px). "
    f"Reduce DISC_R_OVERRIDE or increase UNIFORM_MEASURE_WIDTH."
)
assert UNIFORM_MUSIC_START + UNIFORM_MEASURE_WIDTH * UNIFORM_MEASURES_PER_SYSTEM <= STAFF_END_X, (
    f"Music area ({UNIFORM_MUSIC_START} + {UNIFORM_MEASURE_WIDTH}*{UNIFORM_MEASURES_PER_SYSTEM} "
    f"= {UNIFORM_MUSIC_START + UNIFORM_MEASURE_WIDTH * UNIFORM_MEASURES_PER_SYSTEM}) "
    f"exceeds staff end ({STAFF_END_X}). Reduce measures per system or measure width."
)
assert BEAT_WIDTH > 0, "Beat width must be positive"

# ==============================================================================
# TRIANGOLINI OTTAVA
# Nella tavola sonora, indicano quando il flauto sale/scende di ottava.
# Basato sulla POSIZIONE sul pentagramma in chiave di violino:
#   - Do4(60)/Re4(62) sotto il pentagramma     → 1 triangolino PUNTAA IN GIÙ, nero
#   - Mi4(64)→Re5(74) nel pentagramma          → nessun triangolino (suoni naturali)
#   - Mi5(76)→Do6(84) sopra (fino a 2 tagli)   → 1 triangolino PUNTA IN SU, colore nota
#   - Re6(86)+ sopra (3+ tagli)                → 2 triangolini PUNTA IN SU, neri
# Le note alterate (es. Fa#5=78) rientrano nel range della loro posizione.
# ==============================================================================
def get_octava_triangle(midi, note_color):
    """Restituisce (n_triangles, direction, color) per i triangolini ottava.
    
    Args:
        midi: MIDI number della nota
        note_color: colore hex della nota (per il triangolino singolo su)
    
    Returns:
        (count, direction, color) dove:
        - count = 0 (nessuno), 1 o 2
        - direction = 'up' o 'down'
        - color = colore del triangolino (colore nota per 1-su, nero per gli altri)
    """
    if midi is None:
        return (0, 'up', '#000000')
    
    # Re grave (62) e Do grave (60): sotto il pentagramma → 1 giù, nero
    if midi <= 62:
        return (1, 'down', '#000000')
    
    # Mi4(64) → Re5(74): nel pentagramma, suoni naturali → nessuno
    if 63 <= midi <= 75:
        return (0, 'up', '#000000')
    
    # Mi5(76) → Do6(84): sopra il pentagramma fino a 2 tagli → 1 su, colore nota
    if 76 <= midi <= 85:
        return (1, 'up', note_color)
    
    # Re6(86)+ : 3+ tagli sopra → 2 su, neri
    return (2, 'up', '#000000')


def build_step_map():
    # Steps relative to middle line (B in treble clef)
    # Positive = above middle line, negative = below
    # Extend range to cover notes well above and below the staff
    notes_up = ['B', 'C', 'D', 'E', 'F', 'G', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'A', 'B', 'C']
    result = {}
    for i, n in enumerate(notes_up):
        result[i] = n
    down = ['A', 'G', 'F', 'E', 'D', 'C', 'B', 'A', 'G', 'F', 'E', 'D', 'C', 'B', 'A', 'G', 'F', 'E', 'D']
    for i, n in enumerate(down):
        result[-(i + 1)] = n
    return result

STEP_MAP = build_step_map()

def y_to_note_name(y, middle_line_y, half_step):
    steps = round((middle_line_y - y) / half_step)
    return STEP_MAP.get(steps, '?')

def parse_svg(svg_content):
    # in modalità rhythm le StaffLines hanno stroke="transparent",
    # ma vanno ancora rilevate per posizionare tavola, gambi, etc.
    staff_pattern = r'<polyline class="StaffLines" fill="none" stroke="[^"]*" stroke-width="([\d.]+)"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
    staff_matches = list(re.finditer(staff_pattern, svg_content))
    
    # Group staff lines into systems: lines with the same x_start AND close Y values
    # belong to the same system. Multiple systems on the same page share x_start
    # but have a large Y gap between them.
    from collections import defaultdict
    by_x_start = defaultdict(list)
    for m in staff_matches:
        x_start = m.group(2)
        y = float(m.group(3))
        by_x_start[x_start].append((y, m))
    
    systems = {}
    for x_start, lines in by_x_start.items():
        lines.sort(key=lambda x: x[0])
        
        # Split into groups: a new system starts when there's a Y gap > 1.5x the expected spacing
        # First, estimate the spacing from consecutive lines
        if len(lines) <= 5:
            # Single system (or fewer lines)
            groups = [lines]
        else:
            # Multiple systems — split by large Y gaps
            groups = [[lines[0]]]
            for i in range(1, len(lines)):
                gap = lines[i][0] - lines[i-1][0]
                # Normal staff line spacing is ~212px (for spatium 4.5)
                # A gap > 500 means a new system
                if gap > 500:
                    groups.append([lines[i]])
                else:
                    groups[-1].append(lines[i])
        
        for group in groups:
            if len(group) < 2:
                continue
            group.sort(key=lambda x: x[0])
            top_y = group[0][0]
            bottom_y = group[-1][0]
            middle_line_y = group[len(group) // 2][0] if len(group) >= 5 else group[0][0]
            # For a 5-line staff, middle line is the 3rd (index 2)
            if len(group) >= 5:
                middle_line_y = group[2][0]
            half_step = (bottom_y - top_y) / 8
            x_end = float(group[0][1].group(4))
            # Use a unique key: x_start + top_y to distinguish multiple systems
            sys_key = f"{x_start}_{top_y:.0f}"
            systems[sys_key] = {
                'top': top_y, 'bottom': bottom_y,
                'middle_line_y': middle_line_y, 'half_step': half_step,
                'x_start': float(x_start), 'x_end': x_end,
            }
    
    barline_pattern = r'<polyline class="BarLine" fill="none" stroke="#000000" stroke-width="([\d.]+)"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
    barlines_raw = list(re.finditer(barline_pattern, svg_content))
    
    barlines_by_system = defaultdict(list)
    for m in barlines_raw:
        x = float(m.group(2))
        y1 = float(m.group(3))
        for x_start, info in systems.items():
            if info['top'] - 100 < y1 < info['bottom'] + 100:
                barlines_by_system[x_start].append(x)
                break
    
    note_pattern = r'(<path class="Note" transform="matrix\(([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)" d="([^"]+)"\s*/>)'
    note_matches = list(re.finditer(note_pattern, svg_content))
    
    # Parse stems to distinguish whole (no stem) from half (stem)
    # Stem = <polyline class="Stem" ... points="X,Y1 X,Y2" />
    stem_positions = []
    stem_pat = re.compile(
        r'<polyline class="Stem"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"\s*/>'
    )
    for sm in stem_pat.finditer(svg_content):
        stem_positions.append((float(sm.group(1)), float(sm.group(2)), float(sm.group(4))))
    
    # Parse all notes and group by system
    raw_notes = []
    for m in note_matches:
        full_match = m.group(1)
        scale = float(m.group(2))
        x = float(m.group(6))
        y = float(m.group(7))
        d_attr = m.group(8)
        
        # Detect open vs filled notehead:
        # Open (semibreve/minima) = 2+ subpath (M commands), no Z
        # Filled (semiminima/croma) = 1 subpath, may have Z
        m_count = d_attr.count('M')
        is_open = m_count >= 2
        
        # Determine duration_type:
        # - open + no stem nearby = whole
        # - open + stem nearby = half
        # - filled + stem nearby = quarter
        # - filled + no stem = error/very short
        has_stem = False
        stem_dir = None  # 'up' = gambo verso l'alto, 'down' = gambo verso il basso
        w = 123.453 * scale  # glyph width
        for sx, sy1, sy2 in stem_positions:
            if (x - 40 <= sx <= x + w + 40
                    and min(abs(y - sy1), abs(y - sy2)) < 100):
                has_stem = True
                # Direzione del gambo: sy2 < sy1 = stem UP, sy2 > sy1 = stem DOWN
                stem_dir = 'up' if sy2 < sy1 else 'down'
                break
        
        if is_open and not has_stem:
            duration_type = 'whole'
        elif is_open and has_stem:
            duration_type = 'half'
        elif not is_open and has_stem:
            duration_type = 'quarter'
        else:
            duration_type = 'quarter'  # fallback
        
        note_system_key = None
        note_system = None
        # 3 Ago: some notes with many ledger lines are rendered far below
        # the staff (up to ~1200px). Find the NEAREST system by Y distance,
        # not just the first one within a fixed threshold.
        best_dist = 99999
        for x_start, info in systems.items():
            if info['x_start'] - 50 <= x <= info['x_end'] + 50:
                # Distance from note Y to the system's Y range
                if y < info['top']:
                    dist = info['top'] - y
                elif y > info['bottom']:
                    dist = y - info['bottom']
                else:
                    dist = 0  # inside the system
                # Only consider if within a reasonable range (ledger lines)
                if dist < 1500 and dist < best_dist:
                    best_dist = dist
                    note_system = info
                    note_system_key = x_start
        
        if note_system is None:
            continue
        
        note_name = y_to_note_name(y, note_system['middle_line_y'], note_system['half_step'])
        
        raw_notes.append({
            'full_match': full_match,
            'scale': scale,
            'x': x, 'y': y,
            'center_x': x + NOTEHEAD_CENTER_OFFSET * (scale / 1.25714),
            'name': note_name,
            'color': NOTE_COLORS.get(note_name, '#000000'),
            'text_color': NOTE_TEXT_COLOR.get(note_name, '#000000'),
            'name_it': NOTE_NAMES_EN.get(note_name, '?'),
            'system': note_system,
            'system_key': note_system_key,
            'system_top': note_system['top'],  # for sorting
            'is_open': is_open,  # True = semibreve/minima (vuota), False = semiminima/croma (piena)
            'duration_type': duration_type,  # 'whole', 'half', 'quarter'
            'stem_dir': stem_dir,  # 'up', 'down', or None (whole note, no stem)
        })
    
    # Sort notes in score order: system top-to-bottom, then X left-to-right
    notes = sorted(raw_notes, key=lambda n: (n['system_top'], n['x']))
    
    return {
        'systems': systems,
        'barlines_by_system': barlines_by_system,
        'notes': notes,
    }

def compute_measure_boundaries(system_key, system_info, barlines, notes_in_system):
    bls = sorted(barlines)
    
    if notes_in_system:
        first_note_x = min(n['x'] for n in notes_in_system)
        if len(bls) >= 1 and len(notes_in_system) >= 2:
            note2_x = sorted(n['x'] for n in notes_in_system)[1]
            bl1_x = bls[0]
            note_offset = note2_x - bl1_x
            first_measure_start = first_note_x - abs(note_offset) if note_offset > 0 else first_note_x - 149
        else:
            first_measure_start = first_note_x - 149
    else:
        first_measure_start = system_info['x_start'] + 500
    
    boundaries = [first_measure_start] + bls
    if boundaries[-1] > system_info['x_end'] - 50:
        boundaries = boundaries[:-1]
    
    measures = []
    for i in range(len(boundaries) - 1):
        measures.append((boundaries[i], boundaries[i+1]))
    
    if bls and boundaries[-1] < bls[-1] - 10:
        measures.append((boundaries[-1], bls[-1]))
    
    return measures

def y_stretch_systems(svg_content, systems, target_line_spacing=280, extra_system_gap=100,
                      top_margin=0, bottom_margin=0, page_height=None, fixed_system_gap=None):
    """Widen vertical spacing between staff lines by stretching Y coordinates.
    
    Only Y coordinates are scaled (relative to each system's middle line).
    X coordinates, disc sizes, fonts — all unchanged.
    Each system's 5 staff lines go from current spacing to target_line_spacing.
    extra_system_gap: additional pixels added between each pair of adjacent systems.
    top_margin: pixels of top margin (shifts all systems down).
    bottom_margin: minimum pixels of bottom margin.
    page_height: total page height (viewBox height) for bottom margin calculation.
    fixed_system_gap: if set, overrides extra_system_gap to achieve EXACTLY this gap
        between adjacent systems (replaces the original gap). Used in rhythm mode.
    """
    if not systems:
        return svg_content
    
    # Build system Y-ranges: for each system, find top, bottom, middle, half_step
    sys_info = []
    for x_start, info in systems.items():
        top = info['top']
        bottom = info['bottom']
        middle = info['middle_line_y']
        half_step = info['half_step']
        # Current line spacing (adjacent lines) = 2 * half_step
        current_spacing = 2 * half_step
        if current_spacing <= 0:
            continue
        scale = target_line_spacing / current_spacing
        # Don't stretch if already wide enough
        if scale <= 1.01:
            continue
        # Y range for assignment: extend beyond staff to cover ledger lines AND beams.
        # Beams can be up to ~1000px above the staff (long up-stems after stretch).
        # Use a margin proportional to the gap between systems, capped to avoid overlap.
        # We'll set exact margins later after sorting by top (see below).
        margin = (bottom - top) * 1.5
        y_min = top - margin
        y_max = bottom + margin
        sys_info.append({
            'middle': middle,
            'scale': scale,
            'y_min': y_min,
            'y_max': y_max,
            'top': top,
            'bottom': bottom,
            'orig_middle': middle,  # keep original for gap calculation
        })
    
    if not sys_info:
        return svg_content
    
    # Sort systems by Y position (top to bottom)
    sys_info.sort(key=lambda s: s['top'])
    n_systems = len(sys_info)
    
    # Expand y_min/y_max to cover beams (which can be far above/below the staff).
    # Use half the distance to the adjacent system as the margin — this avoids
    # overlap between systems while covering beams and ledger lines.
    for i, si in enumerate(sys_info):
        if n_systems > 1:
            if i > 0:
                prev_bottom = sys_info[i-1]['bottom']
                gap_above = si['top'] - prev_bottom
            else:
                gap_above = si['top']  # first system: use top as gap
            if i < len(sys_info) - 1:
                next_top = sys_info[i+1]['top']
                gap_below = next_top - si['bottom']
            else:
                gap_below = si['bottom'] * 0.5  # last system
            # Use 45% of the gap (leave 10% buffer to avoid overlap at boundary)
            margin_up = max(margin, gap_above * 0.45)
            margin_down = max(margin, gap_below * 0.45)
            si['y_min'] = si['top'] - margin_up
            si['y_max'] = si['bottom'] + margin_down
    
    # Calculate the actual stretched system height (without extra gap)
    # Each system: top' = middle + (top - middle)*scale, bottom' = middle + (bottom - middle)*scale
    # System height (stretched) = bottom' - top' = (bottom - top) * scale
    for si in sys_info:
        si['stretched_height'] = (si['bottom'] - si['top']) * si['scale']
    
    # If page_height is given, calculate extra_system_gap to fit with margins
    # The ACTUAL gap between systems = (original_gap) + extra_system_gap
    # where original_gap = stretched_top[i+1] - stretched_bottom[i]
    # (the gap that already exists in the stretched layout without any extra shift)
    if page_height and n_systems > 0:
        total_systems_h = sum(si['stretched_height'] for si in sys_info)
        # Calculate original gaps between stretched systems
        original_gaps_total = 0
        for i in range(n_systems - 1):
            stretched_top_next = sys_info[i+1]['middle'] + (sys_info[i+1]['top'] - sys_info[i+1]['middle']) * sys_info[i+1]['scale']
            stretched_bot_curr = sys_info[i]['middle'] + (sys_info[i]['bottom'] - sys_info[i]['middle']) * sys_info[i]['scale']
            original_gaps_total += stretched_top_next - stretched_bot_curr
        available_for_extra = page_height - top_margin - bottom_margin - total_systems_h - original_gaps_total
        if n_systems > 1:
            computed_gap = available_for_extra / (n_systems - 1)
            # Use the computed gap if it's reasonable (positive and not too large)
            if computed_gap > 0:
                extra_system_gap = computed_gap
                print(f"  Auto-gap: {extra_system_gap:.0f}px (page_h={page_height}, top={top_margin}, bot={bottom_margin}, orig_gaps={original_gaps_total:.0f})")
    
    # fixed_system_gap — calcola extra_system_gap per ottenere un gap
    # ESATTO tra sistemi (sostituisce il gap originale). Usato in rhythm mode.
    if fixed_system_gap is not None and n_systems > 1:
        # Il gap attuale (senza extra) tra sistema i e i+1 è:
        # stretched_top[i+1] - stretched_bottom[i]
        # Vogliamo che diventi fixed_system_gap, quindi:
        # extra_system_gap = fixed_system_gap - current_gap
        gaps = []
        for i in range(n_systems - 1):
            st_next = sys_info[i+1]['middle'] + (sys_info[i+1]['top'] - sys_info[i+1]['middle']) * sys_info[i+1]['scale']
            sb_curr = sys_info[i]['middle'] + (sys_info[i]['bottom'] - sys_info[i]['middle']) * sys_info[i]['scale']
            gaps.append(st_next - sb_curr)
        avg_gap = sum(gaps) / len(gaps)
        extra_system_gap = fixed_system_gap - avg_gap
        print(f"  Fixed-gap: target={fixed_system_gap}px, orig_avg={avg_gap:.0f}px, extra={extra_system_gap:.0f}px")
    
    # Calculate cumulative Y shift for each system (extra gap between systems)
    # The first system's stretched top = middle + (top - middle)*scale.
    # We want the first system's new top to be exactly top_margin.
    # So y_shift[0] = top_margin - (sys_info[0]['middle'] + (sys_info[0]['top'] - sys_info[0]['middle']) * sys_info[0]['scale'])
    first_stretched_top = sys_info[0]['middle'] + (sys_info[0]['top'] - sys_info[0]['middle']) * sys_info[0]['scale']
    base_shift = top_margin - first_stretched_top
    for i, si in enumerate(sys_info):
        si['y_shift'] = base_shift + extra_system_gap * i
    
    # Update y_min/y_max to include the shift (for assignment)
    for si in sys_info:
        si['y_min'] = si['y_min']  # keep original ranges for assignment
        si['y_max'] = si['y_max']
    
    # Also update middle for stretch calculation (shift applied separately)
    print(f"  Y-stretch: {len(sys_info)} sistemi, scale={sys_info[0]['scale']:.2f}x (target={target_line_spacing}px), gap+{extra_system_gap}px")
    
    def remap_y(y):
        """Map a Y coordinate through the appropriate system's stretch + gap shift."""
        for si in sys_info:
            if si['y_min'] <= y <= si['y_max']:
                stretched = si['middle'] + (y - si['middle']) * si['scale']
                return stretched + si['y_shift']
        return y  # outside any system — leave unchanged
    
    def remap_y_if_in_system(y):
        """Return (new_y, found_system) for a Y coordinate."""
        for si in sys_info:
            if si['y_min'] <= y <= si['y_max']:
                stretched = si['middle'] + (y - si['middle']) * si['scale']
                return stretched + si['y_shift'], True
        return y, False
    
    modified = svg_content
    
    # 1. StaffLines and Stems: polyline points="x1,y1 x2,y2"
    # StaffLines have y1==y2 (horizontal), Stems have y1!=y2 (vertical). Handle both.
    def replace_polyline_y(match):
        prefix = match.group(1)  # includes 'points="'
        x1, y1, x2, y2 = float(match.group(2)), float(match.group(3)), float(match.group(4)), float(match.group(5))
        suffix = match.group(6)  # does NOT include closing quote
        new_y1, found1 = remap_y_if_in_system(y1)
        new_y2, found2 = remap_y_if_in_system(y2)
        if found1 or found2:
            return f'{prefix}{x1},{new_y1:.2f} {x2},{new_y2:.2f}"{suffix}'
        return match.group(0)
    
    modified = re.sub(
        r'(<polyline[^>]*points=")([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"([^>]*>)',
        replace_polyline_y,
        modified
    )
    
    # 2. Circles: cx="x" cy="y" r="r" — stretch cy only
    def replace_circle_cy(match):
        full = match.group(0)
        cy_m = re.search(r'cy="([\d.]+)"', full)
        if not cy_m:
            return full
        cy = float(cy_m.group(1))
        new_cy, found = remap_y_if_in_system(cy)
        if not found:
            return full
        return full.replace(f'cy="{cy_m.group(1)}"', f'cy="{new_cy:.2f}"')
    
    modified = re.sub(r'<circle\s[^>]*>', replace_circle_cy, modified)
    
    # 3. Rects: y="y" height="h" — stretch both y and height
    def replace_rect_y(match):
        full = match.group(0)
        y_m = re.search(r'y="([\d.]+)"', full)
        h_m = re.search(r'height="([\d.]+)"', full)
        if not y_m:
            return full
        y = float(y_m.group(1))
        new_y, found = remap_y_if_in_system(y)
        if not found:
            return full
        h = float(h_m.group(1)) if h_m else 0
        new_h = h * sys_info[0]['scale']  # approximate — use first system's scale
        # Actually, find the system for the bottom of the rect too
        bottom_y = y + h
        new_bottom, bot_found = remap_y_if_in_system(bottom_y)
        if bot_found:
            new_h = new_bottom - new_y
        result = full.replace(f'y="{y_m.group(1)}"', f'y="{new_y:.2f}"')
        if h_m:
            result = result.replace(f'height="{h_m.group(1)}"', f'height="{new_h:.2f}"')
        return result
    
    modified = re.sub(r'<rect\s[^>]*>', replace_rect_y, modified)
    
    # 4. Text elements: x="x" y="y" — stretch y
    def replace_text_y(match):
        full = match.group(0)
        y_m = re.search(r'\sy="([\d.]+)"', full)
        if not y_m:
            return full
        y = float(y_m.group(1))
        new_y, found = remap_y_if_in_system(y)
        if not found:
            return full
        return full.replace(f'y="{y_m.group(1)}"', f'y="{new_y:.2f}"')
    
    modified = re.sub(r'<text\s[^>]*>', replace_text_y, modified)
    
    # 5. Transform matrix: matrix(a,b,c,d,e,f) — f is Y offset
    def replace_transform_y(match):
        a, b, c, d, e, f = (float(match.group(i)) for i in range(1, 7))
        new_f, found = remap_y_if_in_system(f)
        if found:
            return f'matrix({a},{b},{c},{d},{e},{new_f:.2f})'
        return match.group(0)
    
    modified = re.sub(r'matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)', replace_transform_y, modified)
    
    # 6. Paths (beams = quadrilateri with M/L only). Skip paths with C (curves = clefs/symbols).
    # For beams: ALL Y coordinates must use the SAME system (the one closest to the beam's mean Y).
    # Otherwise a beam that straddles a system boundary gets its points mapped to different
    # systems, producing a deformed triangle instead of a quadrilateral.
    def replace_path_d(match):
        prefix = match.group(1)
        d = match.group(2)
        suffix = match.group(3)
        # Skip paths with Bezier curves (C command) — these are clefs/symbols, not beams
        if 'C' in d:
            return match.group(0)
        # Extract all Y coordinates
        all_ys = [float(y) for y in re.findall(r'[\d.\-]+,([\d.\-]+)', d)]
        if not all_ys:
            return match.group(0)
        mean_y = sum(all_ys) / len(all_ys)
        # Find which system the beam's mean Y belongs to
        beam_system = None
        for si in sys_info:
            if si['y_min'] <= mean_y <= si['y_max']:
                beam_system = si
                break
        if beam_system is None:
            # Try nearest system by middle
            beam_system = min(sys_info, key=lambda s: abs(s['middle'] - mean_y))
        # Map all Y using the SAME system
        def replace_coord(m):
            cmd = m.group(1)  # M or L
            x = m.group(2)
            y = float(m.group(3))
            new_y = beam_system['middle'] + (y - beam_system['middle']) * beam_system['scale'] + beam_system['y_shift']
            return f'{cmd}{x},{new_y:.2f}'
        new_d = re.sub(r'([ML])\s*([\d.\-]+),([\d.\-]+)', replace_coord, d)
        return f'{prefix}{new_d}{suffix}'
    
    modified = re.sub(r'(<path\s[^>]*d=")([^"]*)("[^>]*>)', replace_path_d, modified)
    
    return modified

# ==============================================================================
# TAVOLA SONORA — disegna celle colorate sotto ogni sistema (Orrico method)
# Altezza=colore (schema cromatico MaidaScore), Durata=larghezza cella,
# Pausa=cella bianca con bordo tratteggiato.
# ==============================================================================

NOTE_COLORS_TAVOLA = {
    'C': '#E53935', 'C#': '#E53935', 'Db': '#E53935',
    'D': '#FB8C00', 'D#': '#FB8C00', 'Eb': '#FB8C00',
    'E': '#FDD835',
    'F': '#64DD17', 'F#': '#64DD17', 'Gb': '#64DD17',
    'G': '#00695C', 'G#': '#00695C', 'Ab': '#00695C',
    'A': '#1E88E5', 'A#': '#1E88E5', 'Bb': '#1E88E5',
    'B': '#8E24AA',
}

NOTE_TEXT_COLOR_TAVOLA = {
    # Regola mista accessibilita dislessici:
    # testo NERO su sfondi luminosi (Re arancione, Mi giallo, Fa lime)
    # testo BIANCO su sfondi scuri (Do rosso, Sol ottanio, La blu, Si viola)
    'C': 'white', 'C#': 'white', 'Db': 'white',
    'D': '#111111', 'D#': '#111111', 'Eb': '#111111',
    'E': '#111111',
    'F': '#111111', 'F#': '#111111', 'Gb': '#111111',
    'G': 'white', 'G#': 'white', 'Ab': 'white',
    'A': 'white', 'A#': 'white', 'Bb': 'white',
    'B': 'white',
}

NOTE_NAMES_IT_TAVOLA = {
    'C': 'Do', 'C#': 'Do#', 'Db': 'Db', 'D': 'Re', 'D#': 'Re#', 'Eb': 'Mib',
    'E': 'Mi', 'F': 'Fa', 'F#': 'Fa#', 'Gb': 'Solb', 'G': 'Sol',  # tavola: nome completo
    'G#': 'Sol#', 'Ab': 'Lab', 'A': 'La', 'A#': 'La#', 'Bb': 'Sib', 'B': 'Si',
}

# Alterazioni come simbolo SEPARATO a sinistra del nome nella tavola.
# Mappa pitch name → (nome senza alterazione, simbolo alterazione)
# Simboli: ♯ = diesis, ♭ = bemolle, ♮ = bequadro
# Alterazione come TESTO comprensibile in nero, SOTTO il nome.
# Symbol '#' o 'b' (non ♯/♭ che non si leggono). Maiuscolo per chiarezza.
NOTE_NAMES_IT_TAVOLA_SPLIT = {
    'C': ('Do', ''), 'C#': ('Do', '#'), 'Db': ('Re', 'b'),
    'D': ('Re', ''), 'D#': ('Re', '#'), 'Eb': ('Mi', 'b'),
    'E': ('Mi', ''), 'F': ('Fa', ''), 'F#': ('Fa', '#'), 'Gb': ('Sol', 'b'),
    'G': ('Sol', ''), 'G#': ('Sol', '#'), 'Ab': ('La', 'b'),
    'A': ('La', ''), 'A#': ('La', '#'), 'Bb': ('Si', 'b'), 'B': ('Si', ''),
}

DURATION_BEATS = {
    'whole': 4.0, 'whole_dotted': 6.0,
    'half': 2.0, 'half_dotted': 3.0,
    'quarter': 1.0, 'quarter_dotted': 1.5,
    'eighth': 0.5, 'eighth_dotted': 0.75,
    '16th': 0.25, '16th_dotted': 0.375,
    '32nd': 0.125,
}


def draw_tavola_sonora(svg_content, systems_post, equalized_measures, note_info,
                       note_offset, tavola_row_height=500, tavola_gap=150,
                       processed_notes=None, initial_rest_measures=0,
                       measure_offset=0, mmrest_groups=None):
    """Disegna la riga della Tavola Sonora sotto ogni sistema.
    
    Per ogni battuta: celle colorate (suono) o bianche tratteggiate (pausa),
    larghezza proporzionale alla durata, allineate ai confini delle battute
    del pentagramma MaidaScore.
    
    Se processed_notes è fornito, usa le posizioni center_x calcolate dal
    posizionamento dei cerchi (allineamento perfetto pentagramma↔tavola).
    """
    all_notes = note_info.get('notes', [])
    all_rests = note_info.get('rests', [])
    ts = note_info.get('time_sig', (4, 4))
    time_sigs_pm = note_info.get('time_sigs_per_measure', {})
    # Compute quarter-beats per measure from time signature.
    # 4/4 → 4, 3/4 → 3, 2/4 → 2, 6/8 → 3, 3/8 → 1.5, 9/8 → 4.5, 12/8 → 6
    def _beats_for_ts(sig):
        if sig[1] in (1, 2, 4):
            return sig[0] * (4 // sig[1])
        elif sig[1] == 8:
            return sig[0] * 0.5
        elif sig[1] == 16:
            return sig[0] * 0.25
        return 4
    def _beats_for_measure(m_idx):
        """Return quarter-beats for a specific measure (handles time changes)."""
        ts_m = time_sigs_pm.get(m_idx)
        if ts_m:
            return _beats_for_ts(ts_m)
        return _beats_for_ts(ts)
    beats_per_measure = _beats_for_ts(ts)  # global fallback
    
    # Grey sectors per measure: for compound meters (6/8, 9/8, 12/8),
    # each sector = one beat group (3/8 = 1.5 quarter beats).
    # For simple meters (4/4, 3/4), each sector = 1 quarter beat.
    if ts[1] == 8 and ts[0] % 3 == 0:
        # Compound meter: groups of 3 eighths (6/8→2, 9/8→3, 12/8→4)
        sectors_per_measure = ts[0] // 3
    else:
        sectors_per_measure = int(beats_per_measure) if beats_per_measure == int(beats_per_measure) else int(beats_per_measure)
    
    # Use the measure_offset passed from the caller (global measure index of the
    # first measure on this page). For page 1 with MMRest, this is 0 (includes
    # the rest measures 0-3). Do NOT recalculate from note_offset — that would
    # skip the MMRest measures (first note is at M4, but page starts at M0).
    # fallback DISATTIVATO. measure_offset=0 è corretto quando
    # la battuta 0 è un sistema visibile (pausa semibreve in Amen). Il fallback a
    # measure_idx=1 (prima nota) sfasa tutto di 1: le note SVG vengono assegnate
    # a measure_idx=1 ma sono fisicamente nel sistema 0 (battuta 0-1), mentre la
    # tavola le cerca nel sistema 1 → tavole vuote prima pagina.
    # Il fallback era stato introdotto per Amen modalità normale, ma
    # era sbagliato: anche in modalità normale il sistema 0 = battute 0-1.
    # if measure_offset == 0 and note_offset < len(all_notes) and initial_rest_measures == 0:
    #     measure_offset = all_notes[note_offset].get('measure_idx', 0)
    
    tavola_svg = ''
    
    # Sort systems by Y (top to bottom)
    sorted_systems = sorted(systems_post.items(), key=lambda x: x[1]['top'])
    
    for sys_idx, (sys_key, sys_info) in enumerate(sorted_systems):
        top_y = sys_info['top']
        bottom_y = sys_info['bottom']
        x_start = sys_info['x_start']
        
        # Tavola row position: below the staff, but below any down-stems too.
        # Find the max Y of stems in this system (down-stems extend below bottom_y).
        max_stem_y = bottom_y
        stem_pat = r'<polyline class="Stem"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
        for sm in re.finditer(stem_pat, svg_content):
            sx1, sy1, sx2, sy2 = float(sm.group(1)), float(sm.group(2)), float(sm.group(3)), float(sm.group(4))
            stem_min_y = min(sy1, sy2)
            stem_max_y = max(sy1, sy2)
            # Stem belongs to this system if it's within the staff Y range + margin
            if top_y - 100 <= stem_min_y <= bottom_y + 100:
                if stem_max_y > max_stem_y:
                    max_stem_y = stem_max_y
        
        # Gap: at least tavola_gap, but more if stems extend further
        dynamic_gap = max(tavola_gap, max_stem_y - bottom_y + 50)
        tavola_top = bottom_y + dynamic_gap
        tavola_bottom = tavola_top + tavola_row_height
        
        # Get measure boundaries for this system
        # Matching per posizione (ordinamento Y) tra systems_post
        # e equalized_measures. Entrambi ordinati per Y → match per indice.
        sorted_sys_keys = sorted(systems_post.keys(), key=lambda k: systems_post[k]['top'])
        sys_index = sorted_sys_keys.index(sys_key) if sys_key in sorted_sys_keys else -1
        
        # Match per indice: entrambi ordinati per Y (originale per em, post-stretch per systems_post)
        em_sorted_keys = sorted(equalized_measures.keys(),
                                key=lambda k: float(k.split('_')[1]) if '_' in str(k) else 0)
        measures = None
        if sys_index >= 0 and sys_index < len(em_sorted_keys):
            measures = equalized_measures[em_sorted_keys[sys_index]]
        
        if not measures:
            # Se il sistema non ha entry in equalized_measures,
            # potrebbe essere un sistema MMRest. Crea measures artificiali.
            _mmrest_set_tav = set(gs for gs, gc in (mmrest_groups or []))
            # Calcola global_m_idx_start per verificare se è MMRest
            _g_start = measure_offset
            _sorted_sk = sorted(systems_post.keys(), key=lambda k: systems_post[k]['top'])
            _si = _sorted_sk.index(sys_key) if sys_key in _sorted_sk else -1
            for _pi in range(_si):
                if _g_start in _mmrest_set_tav:
                    _g_start += 1
                elif (_g_start + 1) in _mmrest_set_tav:
                    _g_start += 1
                else:
                    _g_start += 2
            if _g_start in _mmrest_set_tav:
                # Sistema MMRest: crea 1 misura che occupa tutto il sistema
                _sys_info = systems_post.get(sys_key, {})
                staff_start = _sys_info.get('x_start', 472)
                staff_end = _sys_info.get('x_end', 9215)
                measures = [(staff_start, staff_end)]
            else:
                continue
        
        # Label "Tavola Sonora" on the left
        # Dicitura "Tavola" eliminata (non necessaria)
        
        # calcola global_m_idx_start direttamente dal layout,
        # senza dipendere da equalized_measures (che non ha entry per MMRest).
        # 4 Ago 2026 (bug KS): usa la stessa logica di sys_measure_ranges —
        # gli MMRest contano N battute logiche, non 1.
        _mmrest_set_tav = set(gs for gs, gc in (mmrest_groups or []))
        _mmrest_count_map_tav = {gs: gc for gs, gc in (mmrest_groups or [])}
        global_m_idx_start = measure_offset
        for prev_idx in range(sys_index):
            if global_m_idx_start in _mmrest_set_tav:
                global_m_idx_start += _mmrest_count_map_tav.get(global_m_idx_start, 1)
            elif (global_m_idx_start + 1) in _mmrest_set_tav:
                global_m_idx_start += 1
            else:
                global_m_idx_start += UNIFORM_MEASURES_PER_SYSTEM
        

        
        # verifica se questo sistema contiene SOLO battute MMRest.
        # Con il nuovo formato, ogni MMRest è 1 battuta fisica.
        # Un sistema è "MMRest puro" se TUTTE le sue battute sono MMRest.
        mmrest_in_this_sys = None
        if mmrest_groups:
            mmrest_measure_indices = set(gs for gs, gc in mmrest_groups)
            n_meas_in_sys = len(measures)
            all_mmrest = all(
                (global_m_idx_start + m_i) in mmrest_measure_indices
                for m_i in range(n_meas_in_sys)
            )
            if all_mmrest and n_meas_in_sys > 0:
                # Trova il count dell'MMRest di questo sistema
                for gs, gc in mmrest_groups:
                    if gs == global_m_idx_start:
                        mmrest_in_this_sys = (gc, n_meas_in_sys)
                        break

        
        if mmrest_in_this_sys:
            total_count, visible_count = mmrest_in_this_sys
            # Disegna un'unica cella bianca tratteggiata con "N battute di pausa"
            first_meas = measures[0]
            last_meas = measures[-1]
            mmrest_start = first_meas[0]
            mmrest_end = last_meas[1]
            mmrest_width = mmrest_end - mmrest_start
            
            tavola_svg += (f'<rect x="{mmrest_start:.1f}" y="{tavola_top:.1f}" '
                          f'width="{mmrest_width:.1f}" height="{tavola_row_height}" '
                          f'fill="white" rx="8" '
                          f'stroke="#999" stroke-width="3" stroke-dasharray="20,12"/>')
            font_size = 100
            text_x = mmrest_start + mmrest_width / 2
            text_y = tavola_top + tavola_row_height / 2 + font_size * 0.35
            tavola_svg += (f'<text x="{text_x:.1f}" y="{text_y:.1f}" '
                          f'text-anchor="middle" font-family="Atkinson Hyperlegible" '
                          f'font-size="{font_size}" font-weight="600" '
                          f'fill="#999" font-style="italic">{total_count} battute di pausa</text>')
            continue  # salta il disegno delle celle per questo sistema
        
        # For each measure in this system, draw the cells
        # usa global_m_idx_start (calcolato sopra con em_sorted)
        # invece della vecchia formula mmrest_meas
        system_start_measure = global_m_idx_start
        
        for m_idx, (m_start, m_end) in enumerate(measures):
            global_measure_idx = system_start_measure + m_idx
            m_width = m_end - m_start
            bpm = _beats_for_measure(global_measure_idx)
            beat_width = m_width / bpm
            
            # FIX 2 Ago 2026: se abbiamo le note processate (con center_x),
            # usiamo quelle posizioni per l'allineamento perfetto con i cerchi.
            # Altrimenti fallback al calcolo equal-spacing.
            if processed_notes is not None:
                # Get rests for this measure
                measure_rests = [r for r in all_rests 
                               if r.get('measure_idx', -1) == global_measure_idx]
                
                # Find processed notes belonging to this measure
                measure_pnotes = []
                for n in processed_notes:
                    cx = n.get('center_x', n.get('x', 0))
                    if m_start - 50 <= cx <= m_end + 50:
                        if n.get('measure_idx', -1) == global_measure_idx:
                            measure_pnotes.append(n)
                
                # BUILD EVENT TIMELINE: notes + rests, sorted by onset.
                # Each event has (onset, duration, center_x, type, data).
                # Cells are proportional to duration (croma=2x semicroma).
                events_timeline = []
                
                for n in measure_pnotes:
                    onset = n.get('onset', 0.0)
                    dur = DURATION_BEATS.get(n.get('duration_type', 'quarter'), 1.0)
                    if n.get('dots', 0) > 0:
                        dur *= 1.5
                    # Sub-group chord notes (same onset) — share one cell
                    events_timeline.append({
                        'onset': onset,
                        'duration': dur,
                        'type': 'note',
                        'notes': [n],
                    })
                
                for r in measure_rests:
                    onset = r.get('onset', 0.0)
                    dur = DURATION_BEATS.get(r.get('duration_type', 'quarter'), 1.0)
                    if r.get('dots', 0) > 0:
                        dur *= 1.5
                    events_timeline.append({
                        'onset': onset,
                        'duration': dur,
                        'type': 'rest',
                        'rest': r,
                    })
                
                # Merge chord notes (same onset) into single events
                merged = []
                events_timeline.sort(key=lambda e: e['onset'])
                for e in events_timeline:
                    if merged and abs(merged[-1]['onset'] - e['onset']) < 0.01 and e['type'] == 'note' and merged[-1]['type'] == 'note':
                        merged[-1]['notes'].extend(e['notes'])
                        # Duration = max of chord notes
                        merged[-1]['duration'] = max(merged[-1]['duration'], e['duration'])
                    else:
                        merged.append(e)
                events_timeline = merged
                
                # DRAW CELLS: each cell spans [onset, onset+duration] in beat space.
                # Cell X = m_start + (onset / beats_per_measure) * m_width
                # Cell W = (duration / beats_per_measure) * m_width
                # Gap between adjacent cells = visual separator.
                CELL_GAP = 20  # px gap between cells
                MIN_CELL_W = 20  # minimum cell width
                # Nella tavola le pause sono proporzionali alla durata (NON rimpicciolite).
                # Il rimpicciolimento delle pause avviene solo nel pentagramma (glifo più piccolo).
                # Una pausa eighth deve essere PIÙ LARGA di una 16th
                # perché dura il doppio. Rimpicciolire la cella confonde la percezione temporale.
                
                for e in events_timeline:
                    onset = e['onset']
                    dur = e['duration']
                    cell_x = m_start + (onset / bpm) * m_width
                    cell_w = (dur / bpm) * m_width
                    cell_w_vis = cell_w
                    cell_x_vis = cell_x
                    
                    # Apply gap: shrink from right
                    cell_w_adj = cell_w_vis - CELL_GAP
                    if cell_w_adj < MIN_CELL_W:
                        cell_w_adj = max(MIN_CELL_W, cell_w_vis)
                    
                    # Clamp to measure bounds
                    if cell_x_vis < m_start:
                        cell_x_vis = m_start
                    if cell_x_vis + cell_w_adj > m_end - 2:
                        cell_w_adj = m_end - cell_x_vis - 2
                    
                    if cell_w_adj < MIN_CELL_W:
                        continue
                    
                    # Text position: per half/whole (dur >= 2.0), il nome va
                    # all'INIZIO del blocco (allineato al cerchio sul pentagramma).
                    # Per le altre note, al centro.
                    # Anche le PAUSE vanno all'inizio del blocco
                    # (sotto il simbolo pausa disegnato nel pentagramma ritmico, che è
                    # all'inizio del settore grigio), non al centro.
                    if dur >= 2.0 and e['type'] == 'note':
                        # Allineato a sinistra: padding proporzionale al font
                        font_size_est = min(140, max(60, cell_w_adj * 0.35))
                        text_x = cell_x_vis + font_size_est * 0.7
                        text_anchor = "start"
                    elif e['type'] == 'rest':
                        # Pausa: allineata a sinistra come minime/semibrevi
                        font_size_est = min(100, max(40, cell_w_adj * 0.25))
                        text_x = cell_x_vis + font_size_est * 0.5
                        text_anchor = "start"
                    else:
                        text_x = cell_x_vis + cell_w_adj / 2
                        text_anchor = "middle"
                    
                    if e['type'] == 'note':
                        n0 = e['notes'][0]
                        color = n0.get('color', '#888')
                        text_color = n0.get('text_color', 'white')
                        # Tavola usa nome completo (Sol, non So)
                        # alterazione come testo '#' o 'b' in NERO, SOTTO il nome.
                        # Usa note_name (con alterazione: 'C#', 'Bb') invece di name (naturale).
                        pc_name_tav = n0.get('note_name') or n0.get('name') or ''
                        split = NOTE_NAMES_IT_TAVOLA_SPLIT.get(pc_name_tav, None)
                        if split:
                            label, acc_sym = split
                        else:
                            # name può essere None (Amen) → usa pc_name_tav
                            label = NOTE_NAMES_IT_TAVOLA.get(pc_name_tav,
                                   NOTE_NAMES_IT_TAVOLA.get(n0.get('name') or '', n0.get('name_it', '?')))
                            acc_sym = ''
                        tavola_svg += (f'<rect x="{cell_x_vis:.1f}" y="{tavola_top:.1f}" '
                                      f'width="{cell_w_adj:.1f}" height="{tavola_row_height}" '
                                      f'fill="{color}" rx="8" '
                                      f'stroke="{color}" stroke-width="2"/>')
                        font_size = min(140, max(60, cell_w_adj * 0.35))
                        # Nome nota allineato SOTTO la figura (cerchio)
                        # sul pentagramma ritmico, non al centro della cella.
                        # Usa center_x della nota (= posizione del cerchio) se disponibile.
                        note_cx = n0.get('center_x', None)
                        if dur >= 2.0 and e['type'] == 'note':
                            # Minime/semibrevi: allineato a sinistra (invariato)
                            font_size_est = min(140, max(60, cell_w_adj * 0.35))
                            text_x = cell_x_vis + font_size_est * 0.7
                            text_anchor = "start"
                        elif note_cx is not None:
                            # Semiminime/crome: allinea al center_x del cerchio
                            text_x = note_cx
                            text_anchor = "middle"
                        else:
                            text_x = cell_x_vis + cell_w_adj / 2
                            text_anchor = "middle"
                        text_y_tav = tavola_top + tavola_row_height/2 + font_size*0.35
                        tavola_svg += (f'<text x="{text_x:.1f}" '
                                      f'y="{text_y_tav:.1f}" '
                                      f'text-anchor="{text_anchor}" font-family="Atkinson Hyperlegible" '
                                      f'font-size="{font_size:.0f}" font-weight="700" '
                                      f'fill="{text_color}">{label}</text>')
                        if acc_sym:
                            # Alterazione SOTTO il nome, in NERO.
                            # Dimensione FISSA per tutte le durate.
                            # font_size dipende dalla larghezza cella → semicrome (cella stretta)
                            # Prima avevano acc_fs=98 (semicrome) vs 206 (crome) → fisso 206.
                            acc_fs = 206
                            acc_y = text_y_tav + acc_fs * 1.1
                            tavola_svg += (f'<text x="{text_x:.1f}" y="{acc_y:.1f}" '
                                          f'text-anchor="{text_anchor}" font-family="Atkinson Hyperlegible" '
                                          f'font-size="{acc_fs:.0f}" font-weight="700" '
                                          f'fill="#111111">{acc_sym}</text>')
                        
                        # === Triangolini ottava ===
                        # Disegna triangolini/i sopra (punta su) o sotto (punta giù)
                        # il blocco della tavola, in base all'altezza della nota.
                        # triangoli 2x più grandi e attaccati al blocco.
                        # centro X = text_x (sotto/sopra la scritta, non al centro blocco)
                        # gap 15px tra blocco e base triangolo (non attaccato)
                        TRI_BLOCK_GAP = 30
                        tri_count, tri_dir, tri_color = get_octava_triangle(
                            n0.get('pitch'), color)
                        if tri_count > 0:
                            tri_size = min(100, max(50, cell_w_adj * 0.30))
                            tri_gap = tri_size * 0.25  # gap verticale tra triangoli sovrapposti
                            # Centro triangolo = X della scritta (text_x già calcolato sopra)
                            tri_center = text_x
                            if tri_dir == 'up':
                                # Triangoli punta in su, sopra il blocco (gap da blocco)
                                # 2 triangoli sovrapposti verticalmente (non affiancati)
                                tri_y_base = tavola_top - TRI_BLOCK_GAP  # base del triangolo più basso
                                for ti in range(tri_count):
                                    tri_cx = tri_center  # sempre stesso X (sovrapposti)
                                    # ti=0: triangolo più vicino al blocco; ti=1: sopra il primo
                                    tri_y = tri_y_base - ti * (tri_size + tri_gap)
                                    half = tri_size / 2
                                    pts = (f"{tri_cx:.1f},{(tri_y - tri_size):.1f} "
                                           f"{(tri_cx - half):.1f},{tri_y:.1f} "
                                           f"{(tri_cx + half):.1f},{tri_y:.1f}")
                                    tavola_svg += (f'<polygon points="{pts}" '
                                                  f'fill="{tri_color}" '
                                                  f'stroke="{tri_color}" stroke-width="2"/>')
                            else:  # down
                                # Triangolo punta in giù, sotto il blocco (gap da blocco)
                                # 2 triangoli sovrapposti verticalmente (non affiancati)
                                tri_y_base = tavola_top + tavola_row_height + TRI_BLOCK_GAP  # base triangolo più alto
                                for ti in range(tri_count):
                                    tri_cx = tri_center
                                    # ti=0: più vicino al blocco; ti=1: sotto il primo
                                    tri_y = tri_y_base + ti * (tri_size + tri_gap)
                                    half = tri_size / 2
                                    pts = (f"{tri_cx:.1f},{(tri_y + tri_size):.1f} "
                                           f"{(tri_cx - half):.1f},{tri_y:.1f} "
                                           f"{(tri_cx + half):.1f},{tri_y:.1f}")
                                    tavola_svg += (f'<polygon points="{pts}" '
                                                  f'fill="{tri_color}" '
                                                  f'stroke="{tri_color}" stroke-width="2"/>')
                    else:
                        tavola_svg += (f'<rect x="{cell_x_vis:.1f}" y="{tavola_top:.1f}" '
                                      f'width="{cell_w_adj:.1f}" height="{tavola_row_height}" '
                                      f'fill="white" rx="8" '
                                      f'stroke="#999" stroke-width="3" stroke-dasharray="20,12"/>')
                        font_size = min(100, max(40, cell_w_adj * 0.25))
                        tavola_svg += (f'<text x="{text_x:.1f}" '
                                      f'y="{tavola_top + tavola_row_height/2 + font_size*0.35:.1f}" '
                                      f'text-anchor="{text_anchor}" font-family="Atkinson Hyperlegible" '
                                      f'font-size="{font_size:.0f}" font-weight="500" '
                                      f'fill="#999" font-style="italic">pausa</text>')
                
                # If no events in this measure
                if not events_timeline:
                    tavola_svg += (f'<rect x="{m_start:.1f}" y="{tavola_top:.1f}" '
                                  f'width="{m_width:.1f}" height="{tavola_row_height}" '
                                  f'fill="white" rx="8" '
                                  f'stroke="#ccc" stroke-width="2" stroke-dasharray="20,12"/>')
                continue  # Skip the fallback code below
            
            # === FALLBACK: calculate positions from note_info (no processed_notes) ===
            # Collect notes and rests for this measure
            measure_notes = [n for n in all_notes 
                           if n.get('measure_idx', -1) == global_measure_idx]
            measure_rests = [r for r in all_rests 
                           if r.get('measure_idx', -1) == global_measure_idx]
            
            # Build events with pitch info
            events = []
            for n in measure_notes:
                onset = n.get('onset', 0.0)
                dur = DURATION_BEATS.get(n['duration_type'], 1.0)
                if n.get('dots', 0) > 0:
                    dur *= 1.5
                pitch_class = n['pitch'] % 12
                # 4 Ago 2026 (bug Si/La): usa note_name estratto da music21
                # (tiene conto dell'armatura) invece del pitch_names fisso.
                # Il pitch_names fisso mappava pitch class 10 a 'A#' invece di
                # 'Bb' in Fa maggiore, mostrando 'La#' invece di 'Sib' in tavola.
                if 'note_name' in n:
                    pc_name = n['note_name']  # es. 'Bb', 'C#', 'A'
                else:
                    pitch_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
                    pc_name = pitch_names[pitch_class]
                events.append({
                    'type': 'note',
                    'onset': onset,
                    'duration': dur,
                    'beat_num': int(onset),
                    'color': NOTE_COLORS_TAVOLA.get(pc_name, '#888'),
                    'text_color': NOTE_TEXT_COLOR_TAVOLA.get(pc_name, 'white'),
                    'label': NOTE_NAMES_IT_TAVOLA.get(pc_name, '?'),
                    'pc_name': pc_name,  # per split alterazione
                    'pitch': n['pitch'],
                })
            for r in measure_rests:
                onset = r.get('onset', 0.0)
                dur = DURATION_BEATS.get(r['duration_type'], 1.0)
                if r.get('dots', 0) > 0:
                    dur *= 1.5
                events.append({
                    'type': 'rest',
                    'onset': onset,
                    'duration': dur,
                    'beat_num': int(onset),
                    'color': 'white',
                    'text_color': '#999',
                    'label': 'pausa',
                })
            
            # Sort by onset
            events.sort(key=lambda e: e['onset'])
            
            # FIX 2 Ago 2026: posiziona le celle della tavola usando lo STESSO
            # algoritmo dei cerchi del pentagramma (equal spacing per beat).
            # Raggruppa eventi per beat, poi per onset. Per ogni beat con n onsets
            # distinti, posiziona a (j+1)/(n+1) del beat sector (come i cerchi).
            # Le pause che condividono il beat con note vanno a 8% del settore.
            
            # Group by beat_num
            from itertools import groupby
            events_by_beat = {}
            for beat_num, grp in groupby(events, key=lambda e: e['beat_num']):
                events_by_beat[beat_num] = list(grp)
            
            # For each beat, sub-group by onset (chord notes share same position)
            beat_positions = {}  # beat_num -> list of (onset, events_at_onset, frac_in_beat)
            for beat_num, beat_events in events_by_beat.items():
                # Separate notes and rests
                note_events = [e for e in beat_events if e['type'] == 'note']
                rest_events = [e for e in beat_events if e['type'] == 'rest']
                
                # Sub-group notes by onset
                note_events_sorted = sorted(note_events, key=lambda e: e['onset'])
                note_onset_groups = []
                for onset_key, onset_grp in groupby(note_events_sorted, key=lambda e: round(e['onset'], 3)):
                    note_onset_groups.append(list(onset_grp))
                
                n_note_onsets = len(note_onset_groups)
                beat_sector_width = m_width / bpm
                beat_start_x = m_start + beat_num * beat_sector_width
                
                positions = []  # list of (center_x, cell_width, event_list)
                
                if n_note_onsets > 0:
                    # Position notes with equal spacing (same as circles)
                    for j, grp_list in enumerate(note_onset_groups):
                        if n_note_onsets <= 1:
                            frac_in_beat = 0.5
                        else:
                            frac_in_beat = (j + 1) / (n_note_onsets + 1)
                        center_x = beat_start_x + frac_in_beat * beat_sector_width
                        # Cell width: from this position to next, or to beat end
                        positions.append((center_x, grp_list, 'note'))
                
                # Position rests in this beat
                if rest_events:
                    for r_evt in rest_events:
                        if n_note_onsets > 0:
                            # Rest shares beat with notes → 8% of sector (like circles)
                            rest_x = beat_start_x + 0.08 * beat_sector_width
                        else:
                            # Rest alone in beat → center
                            rest_x = beat_start_x + 0.5 * beat_sector_width
                        positions.append((rest_x, [r_evt], 'rest'))
                
                beat_positions[beat_num] = positions
            
            # Draw cells using computed positions
            # For cell width: each cell extends from its center to the next cell's center
            # (or to the beat/measure boundary if it's the last in the beat)
            for beat_num in sorted(beat_positions.keys()):
                positions = beat_positions[beat_num]
                # Sort by X
                positions.sort(key=lambda p: p[0])
                
                beat_sector_width = m_width / bpm
                beat_start_x = m_start + beat_num * beat_sector_width
                beat_end_x = beat_start_x + beat_sector_width
                
                for i, (center_x, evt_list, evt_type) in enumerate(positions):
                    # Cell width: from halfway to previous to halfway to next
                    if i < len(positions) - 1:
                        next_x = positions[i + 1][0]
                        cell_x = (center_x + (positions[i-1][0] if i > 0 else beat_start_x)) / 2 if i > 0 else center_x - (next_x - center_x) / 2
                        cell_w = next_x - cell_x
                    else:
                        # Last in beat: extend to beat end
                        if i > 0:
                            prev_x = positions[i-1][0]
                            cell_x = (center_x + prev_x) / 2
                        else:
                            cell_x = center_x - beat_sector_width * 0.25
                        cell_w = beat_end_x - cell_x
                    
                    # Clamp to beat bounds
                    if cell_x < beat_start_x:
                        cell_x = beat_start_x
                    if cell_x + cell_w > beat_end_x:
                        cell_w = beat_end_x - cell_x
                    
                    if cell_w < 5:
                        continue
                    
                    evt = evt_list[0]  # representative event
                    
                    if evt['type'] == 'note':
                        # If chord (multiple notes at same onset), use first
                        tavola_svg += (f'<rect x="{cell_x:.1f}" y="{tavola_top:.1f}" '
                                      f'width="{cell_w:.1f}" height="{tavola_row_height}" '
                                      f'fill="{evt["color"]}" rx="8" '
                                      f'stroke="{evt["color"]}" stroke-width="2"/>')
                        font_size = min(140, max(60, cell_w * 0.35))
                        # alterazione come testo '#' o 'b' in NERO, SOTTO il nome
                        pc_name_evt = evt.get('pc_name', '')
                        split_evt = NOTE_NAMES_IT_TAVOLA_SPLIT.get(pc_name_evt, None)
                        if split_evt:
                            label_evt, acc_sym_evt = split_evt
                        else:
                            label_evt = evt["label"]
                            acc_sym_evt = ''
                        # Nome allineato al center_x (sotto la figura)
                        text_x_evt = center_x
                        text_anchor_evt = "middle"
                        text_y_evt = tavola_top + tavola_row_height/2 + font_size*0.35
                        tavola_svg += (f'<text x="{text_x_evt:.1f}" '
                                      f'y="{text_y_evt:.1f}" '
                                      f'text-anchor="{text_anchor_evt}" font-family="Atkinson Hyperlegible" '
                                      f'font-size="{font_size:.0f}" font-weight="700" '
                                      f'fill="{evt["text_color"]}">{label_evt}</text>')
                        if acc_sym_evt:
                            # Alterazione SOTTO il nome, in NERO.
                            # Dimensione FISSA 206px per tutte le durate.
                            acc_fs_evt = 206
                            acc_y_evt = text_y_evt + acc_fs_evt * 1.1
                            tavola_svg += (f'<text x="{text_x_evt:.1f}" y="{acc_y_evt:.1f}" '
                                          f'text-anchor="{text_anchor_evt}" font-family="Atkinson Hyperlegible" '
                                          f'font-size="{acc_fs_evt:.0f}" font-weight="700" '
                                          f'fill="#111111">{acc_sym_evt}</text>')
                        
                        # === Triangolini ottava (fallback path, 5 Ago rev2: 2x + attaccati, rev3: sotto/sopra scritta) ===
                        tri_count, tri_dir, tri_color = get_octava_triangle(
                            evt.get('pitch'), evt['color'])
                        if tri_count > 0:
                            tri_size = min(100, max(50, cell_w * 0.30))
                            tri_gap = tri_size * 0.25
                            tri_center = cell_x + cell_w / 2  # = text_x (sempre centro nel fallback)
                            TRI_BLOCK_GAP = 30
                            if tri_dir == 'up':
                                # 2 triangoli sovrapposti verticalmente (non affiancati)
                                tri_y_base = tavola_top - TRI_BLOCK_GAP
                                for ti in range(tri_count):
                                    tri_cx = tri_center
                                    tri_y = tri_y_base - ti * (tri_size + tri_gap)
                                    half = tri_size / 2
                                    pts = (f"{tri_cx:.1f},{(tri_y - tri_size):.1f} "
                                           f"{(tri_cx - half):.1f},{tri_y:.1f} "
                                           f"{(tri_cx + half):.1f},{tri_y:.1f}")
                                    tavola_svg += (f'<polygon points="{pts}" '
                                                  f'fill="{tri_color}" '
                                                  f'stroke="{tri_color}" stroke-width="2"/>')
                            else:
                                # 2 triangoli sovrapposti verticalmente (non affiancati)
                                tri_y_base = tavola_top + tavola_row_height + TRI_BLOCK_GAP
                                for ti in range(tri_count):
                                    tri_cx = tri_center
                                    tri_y = tri_y_base + ti * (tri_size + tri_gap)
                                    half = tri_size / 2
                                    pts = (f"{tri_cx:.1f},{(tri_y + tri_size):.1f} "
                                           f"{(tri_cx - half):.1f},{tri_y:.1f} "
                                           f"{(tri_cx + half):.1f},{tri_y:.1f}")
                                    tavola_svg += (f'<polygon points="{pts}" '
                                                  f'fill="{tri_color}" '
                                                  f'stroke="{tri_color}" stroke-width="2"/>')
                    else:
                        tavola_svg += (f'<rect x="{cell_x:.1f}" y="{tavola_top:.1f}" '
                                      f'width="{cell_w:.1f}" height="{tavola_row_height}" '
                                      f'fill="white" rx="8" '
                                      f'stroke="#999" stroke-width="3" stroke-dasharray="20,12"/>')
                        font_size = min(100, max(40, cell_w * 0.25))
                        # "pausa" allineata a sinistra (sotto il
                        # simbolo pausa del pentagramma, che è all'inizio del settore)
                        text_x_rest = cell_x + font_size * 0.5
                        tavola_svg += (f'<text x="{text_x_rest:.1f}" '
                                      f'y="{tavola_top + tavola_row_height/2 + font_size*0.35:.1f}" '
                                      f'text-anchor="start" font-family="Atkinson Hyperlegible" '
                                      f'font-size="{font_size:.0f}" font-weight="500" '
                                      f'fill="#999" font-style="italic">pausa</text>')
            
            # If no events in this measure, draw a single white cell (whole rest)
            if not events:
                tavola_svg += (f'<rect x="{m_start:.1f}" y="{tavola_top:.1f}" '
                              f'width="{m_width:.1f}" height="{tavola_row_height}" '
                              f'fill="white" rx="8" '
                              f'stroke="#ccc" stroke-width="2" stroke-dasharray="20,12"/>')
    
    # Insert tavola SVG before the closing </svg>
    if tavola_svg:
        svg_content = svg_content.replace('</svg>', tavola_svg + '\n</svg>')
    
    return svg_content


def process_svg(svg_content, note_info=None, note_offset=0, is_first_page=False, title_text=None, part_text=None, measure_offset=0, initial_rest_measures=0, mmrest_groups=None, rhythm_mode=False):
    parsed = parse_svg(svg_content)
    systems = parsed['systems']
    barlines = parsed['barlines_by_system']
    notes = parsed['notes']
    
    print(f"  Trovati {len(systems)} sistemi, {len(notes)} note (offset={note_offset})")
    for n in notes:
        kind = 'OPEN (vuota)' if n['is_open'] else 'FILLED (piena)'
        print(f"    {n['name_it']} ({n['name']}) x={n['x']:.1f} y={n['y']:.1f} {kind} → {n['color']}")
    
    # Merge note_info for durations — position-based matching (3 Ago)
    # OLD: index-based (i + note_offset) — broken when SVG note order != mscz order
    # NEW: for each SVG note, determine its measure from the system layout,
    # then match by (measure_idx, onset) with the mscz notes.
    if note_info:
        all_notes = note_info.get('notes', [])
        all_rests = note_info.get('rests', [])
        
        # 4 Ago 2026 (bug KS): ricalcola mmrest_groups in battute LOGICHE
        # dai measure_idx delle note. mmrest_groups passato da make_accessible_mscz
        # usa indici del file accessibile (prima della rimozione delle battute
        # duplicate), che non corrispondono ai measure_idx logici delle note.
        # Calcoliamo direttamente: gruppi di battute consecutive senza note.
        _note_measures = set(n['measure_idx'] for n in all_notes)
        _max_m = max(_note_measures) if _note_measures else 0
        _logical_mmrest = []  # (start_idx, count) in logical measure_idx
        _current_group = []
        for _m in range(_max_m + 1):
            if _m not in _note_measures:
                _current_group.append(_m)
            else:
                if len(_current_group) >= 2:
                    _logical_mmrest.append((_current_group[0], len(_current_group)))
                _current_group = []
        if len(_current_group) >= 2:
            _logical_mmrest.append((_current_group[0], len(_current_group)))
        # Usa i gruppi logici invece di mmrest_groups
        mmrest_groups = _logical_mmrest
        if mmrest_groups:
            print(f"  MMRest logici: {[(gs+1, gc) for gs, gc in mmrest_groups]}")
        
        # Group mscz notes by measure_idx, sorted by onset
        from collections import defaultdict
        mscz_notes_by_measure = defaultdict(list)
        for n in all_notes:
            mscz_notes_by_measure[n['measure_idx']].append(n)
        for mi in mscz_notes_by_measure:
            mscz_notes_by_measure[mi].sort(key=lambda n: n.get('onset', 0))
        
        # fallback DISATTIVATO (vedi draw_tavola_sonora riga 1843).
        # measure_offset=0 è corretto: il sistema 0 copre le battute 0-1, anche se
        # la battuta 0 ha solo pause (pausa semibreve in Amen). Il fallback a
        # measure_idx=1 sfasava le note SVG di 1 rispetto alla tavola.
        # if measure_offset == 0 and note_offset < len(all_notes) and initial_rest_measures == 0:
        #     measure_offset = all_notes[note_offset].get('measure_idx', 0)
        
        # Determine which measures belong to each system
        # System 0: measures [measure_offset, measure_offset + measures_per_system - 1]
        # System 1: measures [measure_offset + mps, measure_offset + 2*mps - 1]
        # etc.
        # BUT: if there's an MMRest, system 0 has `initial_rest_measures` battute
        # (usually 4), not measures_per_system (2). All subsequent systems are shifted.
        measures_per_system = UNIFORM_MEASURES_PER_SYSTEM
        mmrest_meas = min(initial_rest_measures, 4) if initial_rest_measures >= 2 else 0
        
        # Group SVG notes by system (using system_top)
        svg_notes_by_system = defaultdict(list)
        for n in notes:
            svg_notes_by_system[n['system_top']].append(n)
        
        # Get ALL system tops (including MMRest system with no notes)
        all_system_tops = sorted([s['top'] for s in systems.values()])
        if not all_system_tops:
            all_system_tops = sorted(svg_notes_by_system.keys())
        
        # Track which mscz notes have been matched (to handle duplicate onsets)
        matched_mscz_indices = set()
        
        # calcola sys_start_measure scorrendo i sistemi in ordine.
        # Conta le battute per sistema dal numero di gruppi di barline.
        # Se non abbiamo barline info, usa UNIFORM_MEASURES_PER_SYSTEM.
        # 4 Ago 2026 (bug 55 redux): per i sistemi MMRest, conta quante battute
        # vuote consecutive ci sono in music21 a partire dalla battuta corrente,
        # invece di assumere sempre 1. MuseScore compatta N battute di pausa in
        # un solo sistema; se contiamo 1 invece di N, tutte le battute successive
        # vengono sfasate e le note vengono matchate alle battute sbagliate.
        mscz_notes_by_measure_set = set(mscz_notes_by_measure.keys())
        sys_measure_ranges = []  # list of (sys_top, start_measure, n_measures)
        cumulative_meas = measure_offset
        _mmrest_set_calc = set(gs for gs, gc in (mmrest_groups or []))
        _mmrest_count_map_calc = {gs: gc for gs, gc in (mmrest_groups or [])}
        for sys_i, sys_top in enumerate(all_system_tops):
            sys_notes = sorted(svg_notes_by_system.get(sys_top, []), key=lambda n: n['x'])
            # 4 Ago 2026 (bug KS/regressione): conta il numero corretto di battute
            # LOGICHE per sistema, ALLINEATO con total_meas_in_page.
            # Se questo sistema è un MMRest → conta tutte le battute rappresentate.
            # Se la prossima battuta è MMRest → 1 battuta (sistema pre-MMRest).
            # Altrimenti → 2 battute (UNIFORM_MEASURES_PER_SYSTEM).
            if cumulative_meas in _mmrest_set_calc:
                n_meas = _mmrest_count_map_calc.get(cumulative_meas, 1)
            elif (cumulative_meas + 1) in _mmrest_set_calc:
                n_meas = 1
            else:
                n_meas = UNIFORM_MEASURES_PER_SYSTEM
            sys_measure_ranges.append((sys_top, cumulative_meas, n_meas))
            cumulative_meas += n_meas
        
        for sys_i, (sys_top, sys_start_measure, sys_n_measures) in enumerate(sys_measure_ranges):
            sys_notes = sorted(svg_notes_by_system.get(sys_top, []), key=lambda n: n['x'])
            
            # Count notes per measure in the mscz for this system's measures
            meas_note_counts = []
            for mi in range(sys_start_measure, sys_start_measure + sys_n_measures):
                meas_note_counts.append(len(mscz_notes_by_measure.get(mi, [])))
            
            # Assign SVG notes to measures by splitting sorted notes into groups
            # matching the mscz note counts
            note_idx = 0
            for meas_offset_in_sys, mscz_count in enumerate(meas_note_counts):
                global_measure_idx = sys_start_measure + meas_offset_in_sys
                # Take the next mscz_count SVG notes for this measure
                meas_svg_notes = sys_notes[note_idx:note_idx + mscz_count]
                note_idx += mscz_count
                
                # Match these SVG notes to mscz notes by onset (X position → onset)
                # Sort SVG notes by X (already sorted) and mscz notes by onset
                mscz_candidates = mscz_notes_by_measure.get(global_measure_idx, [])
                mscz_sorted = sorted([(j, n) for j, n in enumerate(mscz_candidates) 
                                      if id(n) not in matched_mscz_indices],
                                     key=lambda x: x[1].get('onset', 0))
                
                for svg_idx, svg_n in enumerate(meas_svg_notes):
                    if svg_idx < len(mscz_sorted):
                        mscz_n = mscz_sorted[svg_idx][1]
                        svg_n['duration_type'] = mscz_n.get('duration_type', 'quarter')
                        svg_n['dots'] = mscz_n.get('dots', 0)
                        svg_n['onset'] = mscz_n.get('onset', 0.0)
                        svg_n['measure_idx'] = mscz_n.get('measure_idx', global_measure_idx)
                        # usa il pitch dal .mscz (AUTORITATIVO) invece
                        # della posizione Y del SVG (inaffidabile con MuseScore 4).
                        # Aggiorna nome, colore e colore testo in base al pitch reale.
                        if 'pitch' in mscz_n:
                            pitch = mscz_n['pitch']
                            # 4 Ago 2026 (bug Si/La): usa step estratto da music21
                            # (tiene conto dell'armatura) invece del natural_map fisso.
                            # Il natural_map fisso mappava pitch class 10 (A#/Bb) sempre
                            # ad A (La), anche in Fa maggiore dove è Bb (Si).
                            if 'step' in mscz_n:
                                note_name = mscz_n['step']  # C/D/E/F/G/A/B
                            else:
                                natural_map = {0: 'C', 1: 'C', 2: 'D', 3: 'D', 4: 'E',
                                              5: 'F', 6: 'F', 7: 'G', 8: 'G', 9: 'A',
                                              10: 'A', 11: 'B'}
                                note_name = natural_map[pitch % 12]
                            svg_n['name'] = note_name
                            svg_n['pitch'] = pitch
                            svg_n['color'] = NOTE_COLORS.get(note_name, '#000000')
                            svg_n['text_color'] = NOTE_TEXT_COLOR.get(note_name, '#000000')
                            svg_n['name_it'] = NOTE_NAMES_EN.get(note_name, '?')
                            # propaga passing_acc per modalità rhythm
                            svg_n['passing_acc'] = mscz_n.get('passing_acc', '')
                            # 9 Ago 2026: propaga staff_acc (alterazione da mostrare
                            # sul pentagramma, include le note dell'armatura)
                            svg_n['staff_acc'] = mscz_n.get('staff_acc', '')
                            svg_n['step'] = mscz_n.get('step', note_name)
                            # propaga note_name con alterazione (es. 'C#', 'Bb')
                            # per la tavola sonora (simbolo alterazione a sinistra del nome)
                            svg_n['note_name'] = mscz_n.get('note_name', note_name)
                            # 9 Ago 2026 (bug enarmonia Re#/Eb): MuseScore a volte
                            # disegna un enarmonico diverso (es. D#5 come Eb5) nella
                            # posizione dello step enarmonico (spazio del Mi invece
                            # che linea del Re). Correggiamo la Y in base allo step
                            # reale estratto da music21, usando le linee del sistema.
                            # Formula: half_steps = (octave-4)*7 + step_offset_C[step] - 7
                            # y_correct = middle_line_y - half_steps * half_step
                            mscz_octave = mscz_n.get('octave', None)
                            mscz_step = mscz_n.get('step', None)
                            sys_info = svg_n.get('system', None)
                            if (mscz_octave is not None and mscz_step is not None
                                    and sys_info is not None):
                                step_offset_C = {'C': 1, 'D': 2, 'E': 3, 'F': 4,
                                                 'G': 5, 'A': 6, 'B': 7}
                                if mscz_step in step_offset_C:
                                    half_steps = (mscz_octave - 4) * 7 + step_offset_C[mscz_step] - 7
                                    y_correct = sys_info['middle_line_y'] - half_steps * sys_info['half_step']
                                    svg_n['y'] = y_correct
                        # Build dur_key: 'half' + dots=1 → 'half_dotted'
                        dt = svg_n['duration_type']
                        if svg_n['dots'] > 0:
                            svg_n['dur_key'] = f"{dt}_dotted"
                        else:
                            svg_n['dur_key'] = dt
                        matched_mscz_indices.add(id(mscz_n))
                    else:
                        svg_n['duration_type'] = 'quarter'
                        svg_n['dots'] = 0
                        svg_n['onset'] = 0.0
                        svg_n['measure_idx'] = global_measure_idx
                        svg_n['dur_key'] = 'quarter'
            
            # Handle any remaining SVG notes not assigned to a measure
            for svg_n in sys_notes[note_idx:]:
                svg_n['duration_type'] = 'quarter'
                svg_n['dots'] = 0
                svg_n['onset'] = 0.0
                svg_n['measure_idx'] = sys_start_measure + sys_n_measures - 1
            
    else:
        for n in notes:
            n['duration_type'] = 'whole'
            n['dots'] = 0
            n['onset'] = 0.0
            n['measure_idx'] = 0
    
    modified = svg_content
    equalized_measures = {}  # system_key → list of (m_start, m_end) after equalization
    raw_barlines_by_system = {}  # system_key → individual barline X positions (after shifting)
    
    # Build rests_by_measure: measure_idx → list of (onset, duration_type)
    # Used to position rests by exact onset (like notes) instead of orig_pos
    rests_by_measure = {}
    if note_info:
        all_rests = note_info.get('rests', [])
        for r in all_rests:
            m = r['measure_idx']
            if m not in rests_by_measure:
                rests_by_measure[m] = []
            rests_by_measure[m].append((r['onset'], r['duration_type']))
    # Sort rests by onset within each measure (for ordered assignment)
    for m in rests_by_measure:
        rests_by_measure[m].sort()
    # Time signature beats for onset→position conversion.
    # In 4/4: 4 quarter beats per measure → onset/4 = position fraction.
    # In 6/8: 3 quarter beats per measure → onset/3 = position fraction.
    # (onsets are in "quarter beats" where eighth=0.5, quarter=1)
    # FIX #147/#152: support per-measure time signatures (cambi di tempo).
    # time_sigs_per_measure = {measure_idx: (num, den)} from extractor.
    # _ts_beats_for_measure(m_idx) returns quarter-beats for that measure.
    # _n_sectors_for_measure(m_idx) returns number of grey sectors.
    time_sigs_per_measure = note_info.get('time_sigs_per_measure', {}) if note_info else {}
    
    def _ts_beats_for(ts):
        """Convert (num, den) → quarter-beats per measure."""
        if ts[1] in (1, 2, 4):
            return ts[0] * (4 // ts[1])
        elif ts[1] == 8:
            return ts[0] * 0.5
        elif ts[1] == 16:
            return ts[0] * 0.25
        else:
            return float(ts[0])
    
    def _ts_beats_for_measure(m_idx):
        """Quarter-beats for a specific measure (supports cambi di tempo)."""
        ts = time_sigs_per_measure.get(m_idx)
        if ts is not None:
            return _ts_beats_for(ts)
        return time_sig_beats_global  # fallback to global
    
    def _n_sectors_for_measure(m_idx):
        """Number of grey sectors for a specific measure."""
        ts = time_sigs_per_measure.get(m_idx)
        if ts is not None:
            if ts[1] == 8 and ts[0] % 3 == 0:
                return ts[0] // 3  # 6/8→2, 9/8→3
            return int(_ts_beats_for(ts))  # 4/4→4, 3/4→3, 2/4→2
        return n_sectors_global  # fallback to global
    
    if note_info:
        ts_info = note_info.get('time_sig', (4, 4))
        if ts_info[1] in (1, 2, 4):
            time_sig_beats_global = ts_info[0] * (4 // ts_info[1])
        elif ts_info[1] == 8:
            time_sig_beats_global = ts_info[0] * 0.5
        elif ts_info[1] == 16:
            time_sig_beats_global = ts_info[0] * 0.25
        else:
            time_sig_beats_global = 4
    else:
        time_sig_beats_global = 4
    
    # Sector size in quarter-beats: for 4/4 each sector=1 quarter beat,
    # for 6/8 each sector=1.5 quarter beats (3/8 = one beat group).
    # Used to group notes in the same grey sector for equal-spacing.
    if note_info:
        ts_info2 = note_info.get('time_sig', (4, 4))
        if ts_info2[1] == 8 and ts_info2[0] % 3 == 0:
            n_sectors_global = ts_info2[0] // 3  # 6/8→2, 9/8→3, 12/8→4
        else:
            n_sectors_global = int(time_sig_beats_global)  # 4/4→4, 3/4→3
        sector_size = time_sig_beats_global / n_sectors_global  # quarter-beats per sector
    else:
        n_sectors_global = 4
        sector_size = 1.0
    
    # Save original accidental positions BEFORE equalization (for later matching)
    # Accidentals get shifted by equalization, so we need the original tx to
    # match them with notes using _orig_tx (also original).
    acc_orig_positions = []  # list of (original_tx, original_ty, element_string)
    acc_save_pat = re.compile(
        r'(<path class="Accidental" transform="matrix\()([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)(\)"[^>]*>)'
    )
    for am in acc_save_pat.finditer(modified):
        acc_orig_positions.append((float(am.group(6)), float(am.group(7)), am.group(0)))
    
    # 2b. Equalize measure widths — move barlines to equidistant positions
    # itera in ordine di Y (top→bottom) per calcolare correttamente
    # _sys_global_idx (che dipende dall'ordine dei sistemi)
    # FIX #147/#152: salva _sys_global_idx per ogni sistema per riuso nelle sezioni grigie
    _sys_to_global_idx = {}  # system_key → global measure index of first measure
    sorted_sys_items = sorted(systems.items(), key=lambda kv: kv[1]['top'])
    for x_start, info in sorted_sys_items:
        bls = sorted(barlines.get(x_start, []))
        
        # i sistemi MMRest hanno 1 barline (solo finale).
        # Non saltarli, ma gestiscili come 1 battuta.
        orig_x_start = x_start.split('_')[0] if '_' in str(x_start) else x_start
        notes_in_sys_check = [n for n in notes if n.get('system_key') == orig_x_start 
                              or n.get('system_key') == x_start]
        if notes_in_sys_check:
            notes_in_sys_check = [n for n in notes_in_sys_check 
                                  if info['top'] - 200 <= n['y'] <= info['bottom'] + 200]
        
        sorted_xs = sorted(systems.keys(), key=lambda k: systems[k]['top'])
        sys_pos = sorted_xs.index(x_start) if x_start in sorted_xs else 0
        
        # Calcola global_m_idx per questo sistema
        _sys_global_idx = measure_offset
        _mmrest_set_eq = set(gs for gs, gc in (mmrest_groups or []))
        _mmrest_count_map_eq = {gs: gc for gs, gc in (mmrest_groups or [])}
        for _prev_idx in range(sys_pos):
            _prev_x = sorted_xs[_prev_idx]
            if _prev_x in equalized_measures:
                _sys_global_idx += len(equalized_measures[_prev_x])
            elif _sys_global_idx in _mmrest_set_eq:
                _sys_global_idx += _mmrest_count_map_eq.get(_sys_global_idx, 1)
            elif (_sys_global_idx + 1) in _mmrest_set_eq:
                _sys_global_idx += 1  # pre-MMRest system = 1 measure
            else:
                _sys_global_idx += UNIFORM_MEASURES_PER_SYSTEM  # default
        # FIX #147/#152: salva il global idx per questo sistema
        _sys_to_global_idx[x_start] = _sys_global_idx
        # Verifica se questo sistema è un MMRest (1 battuta, 0 note)
        _is_mmrest_sys = False
        _mmrest_count = 0
        if mmrest_groups and not notes_in_sys_check:
            for gs, gc in mmrest_groups:
                if gs == _sys_global_idx:
                    _is_mmrest_sys = True
                    _mmrest_count = gc
                    break
        
        if _is_mmrest_sys:
            # Sistema MMRest: 1 battuta che occupa tutto il sistema
            staff_start = info['x_start']
            staff_end = info.get('x_end', 9215)
            equalized_measures[x_start] = [(staff_start, staff_end)]
            continue
        
        if len(bls) < 2:
            # Sistema con 1 battuta (es. half note + measure rests compattati):
            # Crea 1 misura e prosegui con l'equalizzazione (NON skip).
            # prima questo faceva `continue` e le note rimanevano
            # alla posizione originale di MuseScore (sopra la chiave).
            if len(bls) == 1:
                staff_start = info['x_start']
                equalized_measures[x_start] = [(staff_start, bls[0])]
            notes_in_sys = [n for n in notes if n.get('system_key') == x_start]
            if not notes_in_sys:
                continue  # nessuna nota, salta
            # Crea 1 gruppo con la singola battuta
            groups = [bls] if bls else [[info['x_end']]]
            group_centers = [sum(g)/len(g) for g in groups]
            n_groups = 1
            music_start = UNIFORM_MUSIC_START
            staff_width = info['x_end'] - music_start
            # Variable width based on time signature (Marco 8 Ago, Fix #3)
            _ns_single = _n_sectors_for_measure(_sys_global_idx)
            # last system may be too narrow (MuseScore
            # shrinks the last system). Use full measure width regardless of
            # staff_width to ensure correct grey sectors and duration bars.
            equal_w = _ns_single * BEAT_WIDTH
            new_centers = [music_start + equal_w]
            # Shift barline
            for grp, new_c in zip(groups, new_centers):
                old_c = sum(grp) / len(grp)
                shift = new_c - old_c
                for b in grp:
                    new_b = b + shift
                    new_x_str = f'{new_b:.2f}'
                    # prova entrambi i formati X (intero e float)
                    # filtra per Y (multiple barlines stesso X in sistemi diversi)
                    sys_top = info['top']
                    sys_bot = info['bottom']
                    for old_x_str in [str(b), f'{b:.2f}', str(int(b)) if b == int(b) else str(b)]:
                        pattern = rf'(<polyline class="BarLine"[^>]*points="){re.escape(old_x_str)},([\d.]+) {re.escape(old_x_str)},([\d.]+)"'
                        all_matches = list(re.finditer(pattern, modified))
                        target_match = None
                        for m in all_matches:
                            y_val = float(m.group(2))
                            if sys_top - 200 <= y_val <= sys_bot + 200:
                                target_match = m
                                break
                        if target_match:
                            y1_val = target_match.group(2)
                            y2_val = target_match.group(3)
                            prefix = target_match.group(1)
                            new_elem = f'{prefix}{new_x_str},{y1_val} {new_x_str},{y2_val}"'
                            modified = modified[:target_match.start()] + new_elem + modified[target_match.end():]
                            break
            raw_barlines_by_system[x_start] = sorted([b + (new_centers[0] - sum(groups[0])/len(groups[0])) for b in groups[0]])
            barlines[x_start] = new_centers
            orig_music_start = info['x_start']
            old_measure_bounds = [(orig_music_start, group_centers[0])]
            new_measure_bounds = [(music_start, new_centers[0])]
            equalized_measures[x_start] = new_measure_bounds
            for n in notes_in_sys:
                n['_orig_center_x'] = n['center_x']
            system_measure_indices = sorted(set(n['measure_idx'] for n in notes_in_sys if 'measure_idx' in n))
            # Vai al loop di riposizionamento note (salta il grouping normale sotto)
            _skip_grouping = True
        else:
            _skip_grouping = False
        
        if not _skip_grouping:
        
            # Group barlines that are very close together (e.g. final double bar = 2 lines)
            groups = [[bls[0]]]
            for b in bls[1:]:
                if b - groups[-1][-1] > 200:
                    groups.append([b])
                else:
                    groups[-1].append(b)
        
            n_groups = len(groups)
            group_centers = [sum(g)/len(g) for g in groups]
        
            # Music starts after clef/time sig
            # UNIFORM music_start across all systems so grey sectors align
            # Settori grigi sfalsati tra sistemi
            notes_in_sys = [n for n in notes if n['system_key'] == x_start]
            music_start = UNIFORM_MUSIC_START  # uniform across all systems
        
            # Variable measure width based on time signature (Marco 8 Ago, Fix #3).
            # 4/4 → 4 × BEAT_WIDTH = 3300px, 3/4 → 3 × BEAT_WIDTH = 2475px,
            # 2/4 → 2 × BEAT_WIDTH = 1650px. Each grey sector is always BEAT_WIDTH (825px).
            staff_width = info['x_end'] - music_start  # available width
            # Compute per-measure widths based on time signature
            measure_widths = []
            for i in range(n_groups):
                _gm = _sys_global_idx + i
                _ns = _n_sectors_for_measure(_gm)
                measure_widths.append(_ns * BEAT_WIDTH)
            total_needed = sum(measure_widths)
            # Scale down if total exceeds staff width
            if total_needed > staff_width:
                _scale = staff_width / total_needed
                measure_widths = [w * _scale for w in measure_widths]
                total_needed = sum(measure_widths)
        
            new_centers = []
            _cum_x = music_start
            for i in range(n_groups):
                _cum_x += measure_widths[i]
                new_centers.append(_cum_x)
        
            # Move each barline: shift by (new_center - old_center) for its group
            # IMPORTANT: each barline polyline has TWO points with the same X (top+bottom),
            # so we must replace ALL occurrences of the old X, not just the first.
            for grp, new_c in zip(groups, new_centers):
                old_c = sum(grp) / len(grp)
                shift = new_c - old_c
                for b in grp:
                    new_b = b + shift
                    # Find the full polyline element and replace both X coordinates
                    # MuseScore esporta alcune barlines con X intero
                    # (es. "5279") e altre con decimali (es. "5242.74"). str(5279.0)
                    # = "5279.0" non matcha "5279" nel SVG → barline non spostata.
                    # Soluzione: prova entrambi i formati (intero e float).
                    # filtra per Y — ci sono multiple barlines
                    # con lo stesso X in sistemi diversi. Senza filtro Y, re.subn(count=1)
                    # sostituisce la barline SBAGLIATA (prima nel SVG, non quella del sistema).
                    new_x_str = f'{new_b:.2f}'
                    sys_top = info['top']
                    sys_bot = info['bottom']
                    for old_x_str in [str(b), f'{b:.2f}', str(int(b)) if b == int(b) else str(b)]:
                        pattern = rf'(<polyline class="BarLine"[^>]*points="){re.escape(old_x_str)},([\d.]+) {re.escape(old_x_str)},([\d.]+)"'
                        all_matches = list(re.finditer(pattern, modified))
                        target_match = None
                        for m in all_matches:
                            y_val = float(m.group(2))
                            if sys_top - 200 <= y_val <= sys_bot + 200:
                                target_match = m
                                break
                        if target_match:
                            y1_val = target_match.group(2)
                            y2_val = target_match.group(3)
                            prefix = target_match.group(1)
                            new_elem = f'{prefix}{new_x_str},{y1_val} {new_x_str},{y2_val}"'
                            modified = modified[:target_match.start()] + new_elem + modified[target_match.end():]
                            break
        
            # Save individual barline positions (after shifting) for duration bar clipping
            # We need the ACTUAL barline X positions (not group centers) to clip duration bars
            individual_barlines = []
            for grp, new_c in zip(groups, new_centers):
                old_c = sum(grp) / len(grp)
                shift = new_c - old_c
                for b in grp:
                    individual_barlines.append(b + shift)
            raw_barlines_by_system[x_start] = sorted(individual_barlines)
        
            # Update barlines_by_system with new group centers
            barlines[x_start] = new_centers
        
            # Move ALL musical elements (notes, rests, accidentals) proportionally
            # Each element has transform="matrix(sx,0,0,sy,tx,ty)" — we adjust tx
            # Elements stay in their original measure but at the correct proportional position
        
            # Build measure boundaries: measure 0 = [music_start, first_barline]
            # measures 1..n = [barline[i], barline[i+1]]
            # OLD bounds use the ORIGINAL music start (where notes actually are),
            # NEW bounds use UNIFORM_MUSIC_START (where we want them to be).
            # Calculate original music start from the first note position.
            # Calculate original music start: use the staff line start (X of first StaffLine),
            # NOT the first note position. This ensures rests before the first note (e.g.
            # an eighth rest at onset 0.0) are included in the first measure's bounds.
            # prima usava min(notes_x) - 149 che escludeva le pause iniziali.
            orig_music_start = info['x_start']  # staff line start = true measure start
            old_measure_bounds = []
            old_measure_bounds.append((orig_music_start, group_centers[0]))
            # DEBUG 4ago
            if len(notes_in_sys) > 0:
                print(f"    DEBUG sys: notes={len(notes_in_sys)} groups={n_groups} group_centers={group_centers[:5]} orig_music_start={orig_music_start:.0f}")
                print(f"    DEBUG old_measure_bounds={old_measure_bounds[:3]}")
            for i in range(len(group_centers) - 1):
                old_measure_bounds.append((group_centers[i], group_centers[i + 1]))
        
            new_measure_bounds = []
            new_measure_bounds.append((music_start, new_centers[0]))
            for i in range(len(new_centers) - 1):
                new_measure_bounds.append((new_centers[i], new_centers[i + 1]))
        
            # Save equalized measure boundaries for later use (quarter bgs, duration rects)
            equalized_measures[x_start] = new_measure_bounds
        
            # Save original center_x for ledger line alignment (before equalization updates it)
            for n in notes_in_sys:
                n['_orig_center_x'] = n['center_x']
        
            # Determine the measure_idx of the first group in this system
            # from the notes' measure_idx (notes are matched by offset from .mscx)
            system_measure_indices = sorted(set(
                n['measure_idx'] for n in notes_in_sys 
                if 'measure_idx' in n
            ))
            
            # se una battuta ha SOLO pause (nessuna nota),
            # il suo measure_idx manca da system_measure_indices. Questo causava la
            # pausa di semibreve della battuta 39 (solo pause) di essere assegnata
            # alla battuta 40 e posizionata al centro della battuta sbagliata.
            # FIX: se system_measure_indices ha meno elementi di old_measure_bounds
            # (numero di battute nel sistema), ricostruire gli indici in modo contiguo
            # partendo da _sys_global_idx (indice logico della prima battuta del sistema).
            n_groups_in_sys = len(old_measure_bounds)
            if len(system_measure_indices) < n_groups_in_sys and n_groups_in_sys > 0:
                # Ricostruisce indici contigui: [_sys_global_idx, _sys_global_idx+1, ...]
                rebuilt = list(range(_sys_global_idx, _sys_global_idx + n_groups_in_sys))
                # Verifica: gli indici noti dalle note devono essere nel range ricostruito
                known = set(system_measure_indices)
                if all(k in rebuilt for k in known):
                    system_measure_indices = rebuilt
        
        # Track which rests have been matched (by position) in each measure
        rest_assignment_counter = {}  # measure_idx → next rest index to assign
        _matched_rest_indices = {}  # measure_idx → set of matched rest indices
        _processed_spans = set()  # byte offsets of elements already repositioned
        
        for grp_idx in range(len(old_measure_bounds)):
            old_m_start, old_m_end = old_measure_bounds[grp_idx]
            new_m_start, new_m_end = new_measure_bounds[grp_idx]
            old_m_width = old_m_end - old_m_start
            new_m_width = new_m_end - new_m_start
            
            if old_m_width <= 0:
                continue
            
            # Determine the measure_idx for this group
            # 6 Ago 2026 (bug 62): old_measure_bounds ha 1 elemento per BATTUTA
            # (non per settore). grp_idx scorre sulle battute, non sui settori.
            # Il vecchio codice faceva grp_idx // n_sectors_for_map, che in 4/4
            # (n_sectors=4) assegnava grp_idx=0 e grp_idx=1 entrambi a
            # measure_idx_position=0 → le pause della 2ª battuta venivano
            # assegnate alla 1ª battuta e posizionate all'onset sbagliato,
            # sovrapponendosi alle note della battuta successiva.
            # Fix: measure_idx_position = grp_idx (1 gruppo = 1 battuta).
            measure_idx_position = grp_idx
            if measure_idx_position < len(system_measure_indices):
                current_measure_idx = system_measure_indices[measure_idx_position]
            else:
                current_measure_idx = None
            
            # FIX #147/#152: per-measure time signature (cambi di tempo).
            # Use the global measure index to look up the correct time sig.
            _global_m_idx = _sys_global_idx + grp_idx
            _ts_beats = _ts_beats_for_measure(_global_m_idx)  # quarter-beats for THIS measure
            _n_sectors_m = _n_sectors_for_measure(_global_m_idx)  # sectors for THIS measure
            _sector_size_m = _ts_beats / _n_sectors_m if _n_sectors_m > 0 else 1.0
            
            # Collect ALL matches with their span positions FIRST,
            # then apply replacements from last to first (so earlier spans stay valid).
            # Previous code used re.finditer on `modified` while doing modified.replace()
            # inside the loop — this could double-shift elements and replace the wrong one
            # when two elements had identical transform matrices.
            elem_pattern = r'<(path|g|text|use)[^>]*transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"'
            replacements = []  # (start, end, old_str, new_str) — applied in reverse order
            
            # Collect ALL matches in this measure, then SORT by X (tx) so that
            # ordered rest matching assigns onsets left-to-right correctly.
            # MuseScore may render rests out of X order in the SVG source.
            all_matches = []
            for elem_match in re.finditer(elem_pattern, modified):
                tx = float(elem_match.group(6))
                ty = float(elem_match.group(7))
                
                # Check Y is in this system
                if not (info['top'] - 300 < ty < info['bottom'] + 300):
                    continue
                
                # Check X is in this measure
                left_bound = old_m_start - 50
                if not (left_bound <= tx <= old_m_end + 50):
                    continue
                
                # Skip elements already repositioned in a previous measure group
                # (byte offsets can shift by ±1 after in-place text replacements)
                elem_start = elem_match.start()
                already_processed = any(abs(elem_start - s) <= 2 for s in _processed_spans)
                if already_processed:
                    continue
                
                # Skip Hooks (eighth-note flags) — they are repositioned separately
                # to follow their stem, not the note center
                if 'class="Hook"' in elem_match.group(0):
                    continue
                
                all_matches.append(elem_match)
            
            # Sort by tx (X position) so rests are processed left-to-right
            all_matches.sort(key=lambda m: float(m.group(6)))
            
            for elem_match in all_matches:
                tx = float(elem_match.group(6))
                ty = float(elem_match.group(7))
                
                elem_str = elem_match.group(0)
                if 'BarLine' in elem_str or 'StaffLines' in elem_str:
                    continue
                # Skip Note elements (noteheads) but NOT NoteDot (augmentation dots)
                if 'class="Note"' in elem_str:
                    continue
                
                orig_pos = (tx - old_m_start) / old_m_width
                rest_onset_val = None  # reset per ogni elemento (solo Rest la imposta)
                rest_type_mismatch = False  # flag per rimozione pausa SVG sbagliata
                
                if 'Rest' in elem_str:
                    # Position rest by exact onset from .mscx — same formula as notes:
                    # center of Tavola Sonora cell = (onset + dur/2) / time_sig_beats
                    # MATCHING BY ORIGINAL POSITION:
                    # Instead of sequential counter, match each SVG rest to the .mscz rest
                    # with closest original position (tx relative to measure start).
                    rest_onset_val = None
                    rest_dtype_val = None
                    notes_in_same_beat = []
                    
                    # Get all rests for this measure from .mscz
                    m_rests = rests_by_measure.get(current_measure_idx, [])
                    
                    # ORDERED MATCHING: SVG rests appear in X order, .mscz rests
                    # are sorted by onset. Match the i-th SVG rest to the i-th
                    # .mscz rest. This is robust because MuseScore renders rests
                    # left-to-right in onset order.
                    # TYPE-AWARE matching. MuseScore 4
                    # non rende le half rest nell'SVG → l'ordered matching matchava
                    # la half rest .mscz alla pausa SVG sbagliata (es. semicroma di
                    # battuta successiva). Ora verifica che il tipo SVG corrisponda.
                    if m_rests:
                        # Count how many rests have already been matched in this measure
                        n_matched = len(_matched_rest_indices.get(current_measure_idx, set()))
                        if n_matched < len(m_rests):
                            r_idx = n_matched  # next unmatched rest in order
                            rest_onset_val, rest_dtype_val = m_rests[r_idx]
                            # Type check: verify SVG rest type matches .mscz type
                            # M76=quarter, M88=eighth, M0,/M113=16th, M4.64=whole
                            # NB: elem_match.group(0) copre solo fino al transform,
                            # non include d=. Cerco d= nel SVG dopo la fine del match.
                            svg_d = ''
                            d_search_start = elem_match.end()
                            d_search_region = modified[d_search_start:d_search_start+500]
                            if 'd="' in d_search_region:
                                svg_d = d_search_region.split('d="')[1].split('"')[0]
                            type_match = True
                            if rest_dtype_val == 'quarter' and not svg_d.startswith('M76'):
                                type_match = False
                            elif rest_dtype_val == 'eighth' and not svg_d.startswith('M88'):
                                type_match = False
                            elif rest_dtype_val == '16th' and not (svg_d.startswith('M0,') or svg_d.startswith('M113')):
                                type_match = False
                            elif rest_dtype_val == 'whole' and not svg_d.startswith('M4.64'):
                                type_match = False
                            elif rest_dtype_val == 'half':
                                # Half rest non è nell'SVG (bug MuseScore 4) → mai match
                                type_match = False
                            
                            if type_match:
                                if current_measure_idx not in _matched_rest_indices:
                                    _matched_rest_indices[current_measure_idx] = set()
                                _matched_rest_indices[current_measure_idx].add(r_idx)
                            else:
                                # Type mismatch: non matchare questa pausa SVG.
                                # Lascia r_idx unmatched per il clonamento.
                                # 6 Ago 2026 (bug 63): la pausa SVG di tipo sbagliato
                                # (es. semicroma di battuta 10 catturata dai bounds di
                                # battuta 8) deve essere RIMOSSA, non riposizionata,
                                # per evitare doppie pause (originale + clone).
                                rest_onset_val = None
                                rest_dtype_val = None
                                rest_type_mismatch = True  # flag per rimozione
                    
                    if rest_onset_val is not None:
                        # ONSET-BASED positioning: pausa all'INIZIO della sezione grigia
                        # (come le note), non al centro della durata.
                        # "la pausa va indicata graficamente all'inizio 
                        # della sezione grigia di riferimento"
                        dur_map = {'eighth': 0.5, '16th': 0.25, '32nd': 0.125,
                                   'quarter': 1.0, 'quarter_dotted': 1.5,
                                   'half': 2.0, 'half_dotted': 3.0,
                                   'whole': 4.0, 'whole_dotted': 6.0}
                        rest_dur = dur_map.get(rest_dtype_val, 1.0)
                        
                        # Check if notes share the same beat sector (for overlap avoidance)
                        beat_num = int(rest_onset_val / _sector_size_m)
                        notes_in_same_beat = []
                        if current_measure_idx is not None:
                            for n in notes_in_sys:
                                if n.get('measure_idx') == current_measure_idx:
                                    n_onset = n.get('onset', -1)
                                    if int(n_onset / _sector_size_m) == beat_num:
                                        notes_in_same_beat.append(n)
                        
                        # If rest shares sector with notes, nudge left to avoid overlap
                        if notes_in_same_beat:
                            # Place rest at left edge of its time slot (not center)
                            # to avoid overlapping the note circle
                            target_pos = rest_onset_val / _ts_beats + 0.02
                        else:
                            # Pausa sola nel settore: all'INIZIO (come le note)
                            # "ancora più a sinistra, all'inizio della
                            # rispettiva sezione grigia"
                            target_pos = rest_onset_val / _ts_beats + 0.02
                    
                    if rest_onset_val is None:
                        # 6 Ago 2026 (bug 63): se type mismatch, rimuovi la pausa SVG
                        # invece di riposizionarla (verrà clonata con il tipo corretto).
                        if rest_type_mismatch:
                            # Rimuovi questo path Rest dall'SVG
                            replacements.append((elem_match.start(), elem_match.end(), elem_match.group(0), ''))
                            _processed_spans.add(elem_match.start())
                            continue
                        # Fallback: old heuristic (half-note or orig_pos)
                        target_pos = None
                        half_notes_in_measure = [n for n in notes_in_sys 
                                               if n['duration_type'] in ('half', 'half_dotted')
                                               and old_m_start - 50 <= n['x'] <= old_m_end + 50]
                        if half_notes_in_measure:
                            half_note = half_notes_in_measure[0]
                            half_onset = half_note.get('onset', 0.0)
                            if half_onset < 2.0:
                                target_pos = 0.75  # note is beats 1-2, rest is beats 3-4
                            else:
                                target_pos = 0.25  # note is beats 3-4, rest is beats 1-2
                        else:
                            target_pos = orig_pos
                    
                    if target_pos < 0:
                        target_pos = 0.0
                elif 'Note' in elem_str:
                    matched_note = None
                    for n in notes_in_sys:
                        if abs(n['x'] - tx) < 100:
                            matched_note = n
                            break
                    
                    if matched_note:
                        dtype = matched_note['duration_type']
                        if dtype in ('whole', 'whole_dotted'):
                            target_pos = 0.50
                        else:
                            # ONSET-BASED positioning for ALL non-whole notes
                            # (quarter, eighth, 16th, half, half_dotted).
                            # Previously half/half_dotted used fixed positions (0.05/0.55)
                            # which caused overlaps with quarter notes at onset 0.0.
                            # Now all notes use onset-based positioning.
                            onset = matched_note.get('onset', 0.0)
                            beat_num = int(onset / _sector_size_m)
                            beat_frac = (onset / _sector_size_m) - beat_num  # 0.0, 0.33, 0.67, etc.
                            
                            # Count ALL notes in the SAME BEAT (same grey sector)
                            # for equal spacing — INCLUDING half/half_dotted.
                            # Previously half/half_dotted were excluded, causing them
                            # to use orig_pos and overlap with other notes.
                            notes_in_same_beat = []
                            if current_measure_idx is not None:
                                for n in notes_in_sys:
                                    if n.get('measure_idx') == current_measure_idx:
                                        n_onset = n.get('onset', -1)
                                        if int(n_onset / _sector_size_m) == beat_num and n.get('duration_type') not in ('whole', 'whole_dotted'):
                                            notes_in_same_beat.append(n)
                            
                            # Sort by onset to get left-to-right order
                            notes_in_same_beat.sort(key=lambda n: n.get('onset', 0))
                            
                            # Find this note's index within the beat
                            note_idx_in_beat = 0
                            for i, n in enumerate(notes_in_same_beat):
                                if n is matched_note:
                                    note_idx_in_beat = i
                                    break
                            
                            n_in_beat = len(notes_in_same_beat)
                            beat_width_frac = 1.0 / _n_sectors_m  # fraction of measure per sector
                            beat_start_frac = beat_num * beat_width_frac
                            
                            if n_in_beat <= 1:
                                # Single note in beat: position near START of sector (25%)
                                # not center (50%), so dotted-quarter and half notes
                                # don't sit too close to the next barline/sector boundary.
                                # Bug 56: semiminima puntata sola in settore
                                # → più vicina all'inizio della sezione grigia.
                                # Bug 54: minima puntata a onset 0 troppo
                                # vicina alla barline iniziale → 25% la allontana dal bordo.
                                target_pos = beat_start_frac + beat_width_frac * 0.25
                            else:
                                # Multiple notes in beat: equal spacing (j+1)/(n+1)
                                frac_in_beat = (note_idx_in_beat + 1) / (n_in_beat + 1)
                                target_pos = beat_start_frac + beat_width_frac * frac_in_beat
                    else:
                        target_pos = orig_pos
                else:
                    target_pos = orig_pos
                
                new_tx = new_m_start + target_pos * new_m_width
                # per le pause a onset 0.0 (inizio battuta), aggiungi barline_gap
                # come per le note, per evitare che la pausa sia attaccata alla chiave.
                # MA NON se la pausa condivide il settore con note (le note hanno già
                # il barline_gap e la pausa deve restare a sinistra per non sovrapporsi).
                if 'Rest' in elem_str and rest_onset_val is not None and rest_onset_val == 0.0:
                    if not notes_in_same_beat:
                        beat_w = new_m_width / _ts_beats
                        barline_gap = beat_w * 0.3
                        new_tx += barline_gap
                shift = new_tx - tx
                
                # pause di semicroma all'altezza della nota successiva.
                # MuseScore posiziona le semicrome al centro del pentagramma, ma per lo
                # studente è più facile vederle se sono alla stessa altezza della nota
                # che segue. Cerco la nota .mscz con onset > onset pausa, nello stesso sistema.
                # NB: questo blocco deve essere PRIMA del "if abs(shift) < 1: continue",
                # altrimenti le semicrome già in posizione X giusta (shift≈0) non vengono
                # mai allineate in Y.
                new_ty_for_align = None
                if 'Rest' in elem_str and rest_dtype_val == '16th' and rest_onset_val is not None:
                    # Cerca prima la nota successiva NELLA STESSA battuta (priorità 1),
                    # poi la prima nota della battuta successiva (priorità 2, solo se
                    # non c'è nulla nella stessa battuta).
                    best_note_y = None
                    best_onset = 999
                    # Priorità 1: stessa battuta, onset > rest_onset
                    for n in notes_in_sys:
                        n_onset = n.get('onset', 999)
                        n_midx = n.get('measure_idx', -1)
                        if n_midx == current_measure_idx and n_onset > rest_onset_val:
                            if n_onset < best_onset:
                                best_onset = n_onset
                                best_note_y = n.get('y', None)
                    # Priorità 2: prima nota della battuta successiva (solo se non trovata)
                    if best_note_y is None:
                        best_onset = 999
                        for n in notes_in_sys:
                            n_onset = n.get('onset', 999)
                            n_midx = n.get('measure_idx', -1)
                            if n_midx == current_measure_idx + 1 and n_onset < best_onset:
                                best_onset = n_onset
                                best_note_y = n.get('y', None)
                    if best_note_y is not None:
                        new_ty_for_align = best_note_y
                
                if abs(shift) < 1 and new_ty_for_align is None:
                    continue
                
                # Fix (e): use span-based replacement instead of str.replace()
                # Build old/new transform strings from the MATCHED text (not reconstructed)
                old_tx_str = elem_match.group(6)
                new_tx_str = f'{new_tx:.2f}'
                new_ty_str = elem_match.group(7)  # Y invariata di default
                
                # Applica allineamento Y semicroma (calcolato sopra)
                if new_ty_for_align is not None:
                    new_ty_str = f'{new_ty_for_align:.2f}'
                
                full_match = elem_match.group(0)
                old_transform = f'matrix({elem_match.group(2)},{elem_match.group(3)},{elem_match.group(4)},{elem_match.group(5)},{old_tx_str},{elem_match.group(7)})'
                new_transform = f'matrix({elem_match.group(2)},{elem_match.group(3)},{elem_match.group(4)},{elem_match.group(5)},{new_tx_str},{new_ty_str})'
                # Record the span of the transform attribute within the full match
                trans_start = elem_match.start() + full_match.index(old_transform)
                trans_end = trans_start + len(old_transform)
                replacements.append((trans_start, trans_end, old_transform, new_transform))
                _processed_spans.add(elem_match.start())
            
            # Apply replacements in REVERSE order (last span first) so earlier
            # spans remain valid. This fixes both (d) double-shift and (e) wrong-target.
            for start, end, old_t, new_t in sorted(replacements, key=lambda r: r[0], reverse=True):
                modified = modified[:start] + new_t + modified[end:]
        
        # === CLONE MISSING RESTS ===
        # MuseScore sometimes doesn't render the last rest in a measure.
        # For each .mscz rest that has NO matching SVG rest, clone an existing
        # rest glyph and position it at the correct onset-based X.
        if rests_by_measure and note_info:
            # Find which rests were matched (by checking which SVG rests exist)
            # Get all SVG rest positions AFTER repositioning (include d= for template)
            svg_rests_after = list(re.finditer(
                r'<path class="Rest"\s+transform="matrix\(([^,]+),([^,]+),([^,]+),([^,]+),([^,]+),([^)]+)\)"[^>]*d="([^"]*)"',
                modified
            ))
            # For each system, find missing rests
            for grp_idx in range(len(new_measure_bounds)):
                new_m_start, new_m_end = new_measure_bounds[grp_idx]
                new_m_width = new_m_end - new_m_start
                # FIX #147/#152: per-measure time signature
                _global_m_idx2 = _sys_global_idx + grp_idx
                _ts_beats2 = _ts_beats_for_measure(_global_m_idx2)
                # 6 Ago 2026 (bug 62): same fix as above — grp_idx scorre su
                # battute, non settori. measure_idx_position = grp_idx.
                _m_pos = grp_idx
                if _m_pos < len(system_measure_indices):
                    m_idx = system_measure_indices[_m_pos]
                else:
                    continue
                m_rests = rests_by_measure.get(m_idx, [])
                if not m_rests:
                    continue
                matched_set = _matched_rest_indices.get(m_idx, set())
                # For each UNMATCHED .mscz rest, clone a rest glyph
                for r_idx, (r_onset, r_dtype) in enumerate(m_rests):
                    if r_idx in matched_set:
                        continue  # this rest has a matching SVG rest
                    dur_map = {'eighth': 0.5, '16th': 0.25, '32nd': 0.125,
                               'quarter': 1.0, 'quarter_dotted': 1.5,
                               'half': 2.0, 'half_dotted': 3.0,
                               'whole': 4.0, 'whole_dotted': 6.0}
                    r_dur = dur_map.get(r_dtype, 1.0)
                    # posiziona la pausa all'INIZIO della
                    # sezione grigia (come le pause riposizionate e le note), NON al
                    # centro della durata. La semibreve (onset=0, dur=4) finiva al centro
                    # della battuta (settore 2) invece che all'inizio (settore 0).
                    # Formula precedente: (r_onset + r_dur/2) / beats → centro durata.
                    # Nuova: r_onset / beats + 0.02 → inizio sezione grigia.
                    target_pos = r_onset / _ts_beats2 + 0.02
                    expected_x = new_m_start + target_pos * new_m_width
                    # barline_gap per pause a onset 0.0 (non attaccate alla chiave)
                    if r_onset == 0.0:
                        beat_w = new_m_width / _ts_beats2
                        expected_x += beat_w * 0.3
                    # Clone the first SVG rest of the same type
                    # Quarter rest: d starts with "M76.125", eighth rest: d starts with "M88.375"
                    # Half rest: MuseScore 4 BUG — non rende le half rest nell'SVG export!
                    # Dobbiamo clonarle noi con un path generato (rettangolo nero sulla 3ª linea).
                    HALF_REST_PATH = 'M-3.29688,-0.890625 L3.29688,-0.890625 L3.29688,1.890625 L-3.29688,1.890625 Z'
                    if svg_rests_after:
                        template_d = None
                        template_scale = 3.386
                        for sr in svg_rests_after:
                            sr_ty = float(sr.group(6))
                            if not (info['top'] - 300 < sr_ty < info['bottom'] + 300):
                                continue
                            sr_d = sr.group(7)  # d= attribute captured by regex
                            if sr_d:
                                is_quarter = sr_d.startswith('M76')
                                is_eighth = sr_d.startswith('M88')
                                # half rest non ha template SVG (bug MuseScore 4),
                                # non cercare un template generico per le half rest.
                                if (r_dtype == 'quarter' and is_quarter) or \
                                   (r_dtype == 'eighth' and is_eighth) or \
                                   (r_dtype not in ('quarter', 'eighth', 'half')):
                                    template_d = sr_d
                                    template_scale = float(sr.group(1))
                                    break
                        # Half rest: nessun template nell'SVG (bug MuseScore 4).
                        # Usa path hardcoded con scale pieno (NON rimpicciolito).
                        if r_dtype == 'half' and template_d is None:
                            template_d = HALF_REST_PATH
                            template_scale = 3.386  # scale pieno come semiminime
                        if template_d:
                            # Clone the rest at the expected position
                            # Y: center of staff (same as other rests in this system)
                            staff_mid_y = (info['top'] + info['bottom']) / 2
                            # Adjust Y for the rest type (quarter rests are centered,
                            # eighth rests are offset)
                            if r_dtype == 'eighth':
                                rest_y = staff_mid_y - 80  # eighth rests sit higher
                            elif r_dtype == 'half':
                                rest_y = staff_mid_y - 50  # half rests centered on staff
                            else:
                                rest_y = staff_mid_y - 50  # quarter rests centered
                            if r_dtype == 'half':
                                # Half rest: disegna come rettangolo nero direttamente
                                # (il path MuseScore è troppo piccolo con scale normale).
                                # La half rest poggia sulla 3ª linea (centro pentagramma).
                                # larghezza = stessa della semibreve
                                # (~364px = 280px line_spacing × 1.3).
                                # Altezza = mezza riga pentagramma.
                                # NB: info top/bottom sono PRE y-stretch (gap 94.5px);
                                # usiamo line_spacing=280 (post-stretch, costante globale).
                                line_spacing = 280  # target_line_spacing post y-stretch
                                rect_w = line_spacing * 1.3
                                rect_h = line_spacing * 0.25
                                # La half rest poggia sulla 3ª linea (linea centrale).
                                # info top/bottom sono pre-stretch ma la 3ª linea è al centro:
                                # line3_y = (top + bottom) / 2, poi corretta dallo stretch.
                                # Poiché lo stretch scala uniformemente attorno al centro,
                                # il centro (middle) è invariato → usiamo middle_line_y.
                                line3_y = info.get('middle_line_y', (info['top'] + info['bottom']) / 2)
                                new_rest = (
                                    f'<rect class="Rest" '
                                    f'x="{expected_x:.2f}" '
                                    f'y="{line3_y - rect_h:.2f}" '
                                    f'width="{rect_w:.2f}" height="{rect_h:.2f}" '
                                    f'fill="#000000" />'
                                )
                            else:
                                new_rest = (
                                    f'<path class="Rest" '
                                    f'transform="matrix({template_scale:.5f},0,0,{template_scale:.5f},'
                                    f'{expected_x:.2f},{rest_y:.2f})" '
                                    f'd="{template_d}" />'
                                )
                            # Insert before </svg>
                            modified = modified.replace('</svg>', new_rest + '\n</svg>')
                            print(f"    → Cloned missing {r_dtype} rest at x={expected_x:.0f} (measure {m_idx+1}, onset={r_onset})")
        
        # Also update note positions in our data structure (for duration rects)
        # Position notes based on their duration:
        # - whole note: center of measure (50%)
        # - half note beats 1-2: center at 25%
        # - half note beats 3-4: center at 75%
        # We determine which half by the note's original position relative to the measure center
        for n in notes_in_sys:
            n['_orig_tx'] = n['x']  # save original transform X before updating
            for grp_idx in range(len(old_measure_bounds)):
                old_m_start, old_m_end = old_measure_bounds[grp_idx]
                if old_m_start - 50 <= n['x'] <= old_m_end + 50:
                    old_m_width = old_m_end - old_m_start
                    new_m_start, new_m_end = new_measure_bounds[grp_idx]
                    new_m_width = new_m_end - new_m_start
                    # FIX #147/#152: per-measure time signature
                    _gm_idx = _sys_global_idx + grp_idx
                    _ts_b = _ts_beats_for_measure(_gm_idx)
                    _n_sec = _n_sectors_for_measure(_gm_idx)
                    _sec_sz = _ts_b / _n_sec if _n_sec > 0 else 1.0
                    
                    dtype = n['duration_type']
                    new_m_width = new_m_end - new_m_start
                    beat_w = new_m_width / _ts_b


                    # Gap to shift first note right of the barline (only for beat 0)
                    barline_gap = beat_w * 0.3
                
                    if dtype in ('whole', 'whole_dotted'):
                        # Start of measure: center the circle just right of m_start
                        offset = NOTEHEAD_CENTER_OFFSET * (n.get('scale', 2.57143) / 1.25714)
                        n['x'] = new_m_start + barline_gap - offset
                        n['center_x'] = new_m_start + barline_gap
                    elif dtype in ('half', 'half_dotted'):
                        # Fix 3 Ago: assign _beat_num so half/half_dotted enter the
                        # onset-based positioning block below (like quarter/eighth).
                        # Previously they used fixed positions (half_gap for onset<2,
                        # 50%+half_gap for onset>=2) which caused overlaps with quarter
                        # notes at onset 0.0 (both at ~5% of measure).
                        onset = n.get('onset', 0.0)
                        n['_beat_num'] = int(onset)
                        n['_onset_in_beat'] = onset - int(onset)
                        dur_map = {'half': 2.0, 'half_dotted': 3.0}
                        n['_dur_in_beat'] = dur_map.get(dtype, 2.0)
                        # Temporary position (will be overridden in onset-based block)
                        target_pos = onset / 4.0
                        gap = barline_gap if onset == 0.0 else 0
                        n['x'] = new_m_start + target_pos * new_m_width + gap
                        n['center_x'] = n['x'] + NOTEHEAD_CENTER_OFFSET * (n.get('scale', 2.57143) / 1.25714)
                    else:
                        # Quarter or shorter: EQUAL SPACING within beat
                        # Group notes by beat (0,1,2,3), distribute equally within each beat.
                        # This ensures 16th notes don't overlap (proportional onset placement
                        # puts onsets 0.5 and 0.75 only 6.25% of measure apart = too close).
                        onset = n.get('onset', 0.0)  # quarter-beats within measure
                        beat_num = int(onset / _sec_sz)  # grey sector index
                        # Will be repositioned below in the beat-grouping pass
                        n['_beat_num'] = beat_num
                        n['_onset_in_beat'] = (onset / _sec_sz) - beat_num  # 0.0-1.0 within sector
                        # Duration in beats for centering (aligns with Tavola Sonora cells)
                        dur_map = {'eighth': 0.5, '16th': 0.25, '32nd': 0.125,
                                   'quarter': 1.0, 'quarter_dotted': 1.5,
                                   'half': 2.0, 'half_dotted': 3.0,
                                   'whole': 4.0, 'whole_dotted': 6.0}
                        n['_dur_in_beat'] = dur_map.get(n.get('duration_type', 'quarter'), 1.0)
                        # Temporary proportional position (will be overridden)
                        target_pos = onset / _ts_b
                        gap = barline_gap if onset == 0.0 else 0
                        n['x'] = new_m_start + target_pos * new_m_width + gap
                        n['center_x'] = n['x'] + NOTEHEAD_CENTER_OFFSET * (n.get('scale', 2.57143) / 1.25714)
                    # NOTE: do NOT update full_match — it must match the ORIGINAL path in the SVG
                    # The coloring step will replace the old path (with old transform) with a new
                    # colored path that has the updated transform X
                    old_tx_str = str(n['_orig_tx'])
                    new_tx_str = f'{n["x"]:.2f}'
                    # Store the new transform X for the coloring step to use
                    n['_new_tx_str'] = new_tx_str
                    n['_old_tx_str'] = old_tx_str
                    break
        
        # ONSET-BASED POSITIONING:
        # Position each note at the CENTER of its duration cell:
        #   center_x = measure_start + ((onset + duration/2) / beats_per_measure) * measure_width
        # This guarantees pixel-perfect alignment between MaidaScore circles and
        # Tavola Sonora cells below. Croma cell = 2x semicroma cell.
        # Chord notes (same onset) share the same X (stacked vertically).
        # Clamp center_x to keep circles inside the beat sector.
        time_sig_beats = time_sig_beats_global  # quarter-beats per measure (4 in 4/4, 3 in 6/8)
        for grp_idx in range(len(new_measure_bounds)):
            new_m_start, new_m_end = new_measure_bounds[grp_idx]
            new_m_width = new_m_end - new_m_start
            # FIX #147/#152: per-measure time signature
            _gm_idx3 = _sys_global_idx + grp_idx
            _ts_b3 = _ts_beats_for_measure(_gm_idx3)
            _n_sec3 = _n_sectors_for_measure(_gm_idx3)
            _sec_sz3 = _ts_b3 / _n_sec3 if _n_sec3 > 0 else 1.0
            # Find short notes (quarter or shorter) in this measure's X range.
            measure_notes = [n for n in notes_in_sys
                           if n.get('_beat_num') is not None
                           and new_m_start - 50 <= n['x'] <= new_m_end + 50]
            from itertools import groupby
            beat_width_frac = 1.0 / _n_sec3  # fraction of measure per sector
            
            # Group by beat_num (sector index)
            measure_notes_sorted = sorted(measure_notes, key=lambda n: (n.get('_beat_num', 0), n.get('onset', 0.0)))
            for beat_num_key, beat_group in groupby(measure_notes_sorted, key=lambda n: n.get('_beat_num', 0)):
                beat_list = list(beat_group)
                # Sub-group by onset within beat (chord notes share same X)
                beat_list_sorted = sorted(beat_list, key=lambda n: n.get('onset', 0.0))
                onset_groups = []
                for onset_key, onset_grp in groupby(beat_list_sorted, key=lambda n: round(n.get('onset', 0.0), 3)):
                    onset_groups.append(list(onset_grp))
                
                beat_start_frac = beat_num_key * beat_width_frac
                # Sector start in quarter-beats (for onset_in_beat calculation)
                sector_start_qb = beat_num_key * _sec_sz3
                
                for j, grp_list in enumerate(onset_groups):
                    onset = grp_list[0].get('onset', 0.0)
                    # Duration of this note (in quarter-beats)
                    dur_type = grp_list[0].get('duration_type', 'quarter')
                    dur_beats = DURATION_BEATS.get(dur_type, 1.0)
                    if grp_list[0].get('dots', 0) > 0:
                        dur_beats *= 1.5
                    
                    # For half/whole notes: position at START of cell (aligns with tavola)
                    # For short notes: position at CENTER of duration cell
                    # 4 Ago 2026 (bug 54/56): half/whole notes a 25% del settore (era 5%)
                    # → non troppo attaccate alla barline, più vicine all'inizio del settore.
                    if dur_beats >= 2.0:
                        # Half/whole: al 25% della cella (non 5% → troppo attaccata al bordo)
                        onset_in_beat = (onset - sector_start_qb) / _sec_sz3
                        frac_in_beat = onset_in_beat + 0.25
                    else:
                        # Center of the duration cell: onset + dur/2
                        onset_in_beat = (onset - sector_start_qb) / _sec_sz3
                        # 4 Ago 2026 (bug 56): se la nota è SOLA nel settore (1 onset group),
                        # posiziona al 25% invece del centro → più vicina all'inizio del
                        # settore grigio, per semiminime puntate sole.
                        if len(onset_groups) <= 1:
                            frac_in_beat = onset_in_beat + 0.25
                        else:
                            center_in_beat = onset_in_beat + dur_beats / (2.0 * _sec_sz3)
                            frac_in_beat = center_in_beat
                    frac_measure = beat_start_frac + beat_width_frac * frac_in_beat
                    # Clamp to keep circle inside measure
                    frac_measure = max(0.02, min(0.98, frac_measure))
                    center_x = new_m_start + frac_measure * new_m_width
                    
                    # Additional clamp: keep circle inside beat sector bounds
                    beat_start_x = new_m_start + beat_start_frac * new_m_width
                    beat_end_x = beat_start_x + beat_width_frac * new_m_width
                    # disc_r approximation for clamping (proportional for croma/semicroma)
                    n_dtype = grp_list[0].get('duration_type', 'quarter') if grp_list else 'quarter'
                    if n_dtype in ('16th', '16th_dotted'):
                        disc_r_approx = 85  # 130 * 0.65
                    elif n_dtype in ('eighth', 'eighth_dotted'):
                        disc_r_approx = 104  # 130 * 0.80
                    else:
                        disc_r_approx = 130
                    center_x = max(beat_start_x + disc_r_approx + 5, 
                                  min(center_x, beat_end_x - disc_r_approx - 5))
                    
                    for n in grp_list:
                        offset = NOTEHEAD_CENTER_OFFSET * (n.get('scale', 2.57143) / 1.25714)
                        n['x'] = center_x - offset
                        n['center_x'] = center_x
                        n['_new_tx_str'] = f'{n["x"]:.2f}'
        
        print(f"    Equalized {len(new_measure_bounds)} measures: widths={[round(m[1]-m[0]) for m in new_measure_bounds]}")
    
    # 1a-ll. Shift LedgerLine polylines in X to match their notes
    # Ledger lines have absolute coords (no transform), so equalization doesn't move them.
    # We saved _orig_center_x before equalization; now use the delta to shift ledger lines.
    # Strategy: match each ledger line to the note with closest ORIGINAL X (ledger lines
    # sit at the same X as their note, ±19px). Y proximity is secondary.
    ledger_aligned = 0
    for x_start, info in systems.items():
        notes_in_sys_ll = [n for n in notes if n.get('system_key') == x_start]
        if not notes_in_sys_ll:
            continue
        # Collect ALL ledger line matches FIRST (static list, not re.finditer on mutable string)
        ledger_pat = r'(<polyline class="LedgerLine"[^>]*points=")([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
        ledger_matches = list(re.finditer(ledger_pat, modified))
        replacements = []  # (old_str, new_str)
        for lm in ledger_matches:
            lx1 = float(lm.group(2))
            ly1 = float(lm.group(3))
            lx2 = float(lm.group(4))
            ly2 = float(lm.group(5))
            if not (info['top'] - 300 < ly1 < info['bottom'] + 300):
                continue
            ledger_mid_x = (lx1 + lx2) / 2
            # Match by ORIGINAL X proximity (ledger line is at same X as its note)
            best_note = None
            best_x_dist = float('inf')
            for n in notes_in_sys_ll:
                if '_orig_center_x' not in n:
                    continue
                x_dist = abs(ledger_mid_x - n['_orig_center_x'])
                if x_dist < best_x_dist:
                    best_x_dist = x_dist
                    best_note = n
            if best_note is not None and best_x_dist < 50:
                shift = best_note['center_x'] - best_note['_orig_center_x']
                # 4 Ago 2026 (ledger lines): allunga i tagli addizionali per renderli
                # ben visibili. MuseScore li esporta a 185px, ma i cerchi hanno diametro
                # 176-220px → il cerchio fuoriesce dai tagli. Allunghiamo di 40px per lato.
                # ridotto da 60 a 40px per rendere i tagli
                # più staccati tra loro (meno estensione = più spazio visivo).
                LEDGER_EXTEND = 40  # pixel di estensione per lato
                new_lx1 = lx1 + shift - LEDGER_EXTEND
                new_lx2 = lx2 + shift + LEDGER_EXTEND
                old_elem = lm.group(0)
                new_elem = f'{lm.group(1)}{new_lx1:.2f},{ly1:.2f} {new_lx2:.2f},{ly2}"'
                replacements.append((old_elem, new_elem))
                ledger_aligned += 1
        # Apply all replacements
        for old_elem, new_elem in replacements:
            modified = modified.replace(old_elem, new_elem, 1)
    if ledger_aligned > 0:
        print(f"    Ledger lines: {ledger_aligned} aligned + extended (+40px/side)")
    
    # 1a. Shift and color stems (they are polylines with absolute coords, not transform matrix)
    # Stems don't get moved by equalization (they lack transform="matrix(...)"
    # stem UP → right of notehead, stem DOWN → left of notehead
    # (matches MuseScore convention: up-stems on right side, down-stems on left side)
    # Also track stem shifts for beam repositioning (beams must follow their stems)
    # Pre-scan hooks to identify isolated eighth notes (crome isolate) that need
    # stem DOWN + hook at bottom.
    # Store (tx_rounded, ty_rounded) to match stem by X AND Y (same system).
    _hook_positions = set()
    if rhythm_mode:
        _pre_hook_pat = re.compile(
            r'<path class="Hook"[^>]*transform="matrix\(([^,]+),([^,]+),([^,]+),([^,]+),([^,]+),([^)]+)\)"'
        )
        for hm in _pre_hook_pat.finditer(modified):
            _hook_positions.add((round(float(hm.group(5))), round(float(hm.group(6)))))
    stem_pat = re.compile(
        r'<polyline class="Stem"([^>]*)points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"\s*/>'
    )
    stem_shifts = []  # list of (old_sx, new_sx, stem_min_y, stem_max_y) — for beam repositioning
    stem_shifts_list = stem_shifts  # alias used in beam code below
    for sm in list(stem_pat.finditer(modified)):
        sx = float(sm.group(2))
        sy1 = float(sm.group(3))
        sy2 = float(sm.group(5))
        # Find which note owns this stem (stem X near note's right edge, Y near note)
        owner = None
        for n in notes:
            if '_orig_tx' not in n:
                continue
            w = 123.453 * n['scale']  # glyph width
            if (n['_orig_tx'] - 40 <= sx <= n['_orig_tx'] + w + 40
                    and min(abs(n['y'] - sy1), abs(n['y'] - sy2)) < 100):
                owner = n
                break
        if owner is None:
            continue
        # Determine stem direction: which end of the stem is near the notehead?
        # sy1 near note Y → stem starts at note, extends to sy2
        #   if sy2 > sy1 → stem goes DOWN (y increases downward in SVG)
        #   if sy2 < sy1 → stem goes UP
        d1 = abs(sy1 - owner['y'])
        d2 = abs(sy2 - owner['y'])
        if d1 < d2:
            # sy1 is at the note
            stem_up = sy2 < sy1  # UP if sy2 is above sy1 (smaller y)
        else:
            # sy2 is at the note
            stem_up = sy1 < sy2  # UP if sy1 is above sy2
        
        owner_scale = owner['scale'] / 1.25714
        owner_base_r = DISC_R_OVERRIDE if DISC_R_OVERRIDE else NOTEHEAD_HEIGHT * owner_scale * 0.9
        # Proportional disc_r for croma/semicroma
        owner_dtype = owner.get('duration_type', 'quarter')
        if owner_dtype in ('16th', '16th_dotted'):
            owner_disc_r = owner_base_r * 0.65
        elif owner_dtype in ('eighth', 'eighth_dotted'):
            owner_disc_r = owner_base_r * 0.80
        else:
            owner_disc_r = owner_base_r
        # Stem UP → right side of circle; Stem DOWN → left side of circle
        if stem_up:
            new_sx = owner['center_x'] + owner_disc_r + 5
        else:
            new_sx = owner['center_x'] - owner_disc_r - 5

        # modalità rhythm — gambi tutti IN SU, stessa lunghezza.
        # 7 Ago: accorciati a 350px (erano 700, troppo lunghi senza pentagramma).
        # Tutte le teste sono sulla middle_line_y, i gambi partono da lì e vanno
        # verso l'alto con lunghezza uniforme. Le travature seguono automaticamente.
        if rhythm_mode:
            stem_mid_y = owner['system']['middle_line_y']
            STEM_LEN_RHYTHM = 350  # lunghezza gambo uniforme (ridotta)
            # crome isolate (with hook) → stem DOWN + hook at bottom.
            # Other notes → stem UP (for beams).
            # Match hook by X (within 60px) AND Y (same system, within 800px).
            # Hook ty is at the stem endpoint (top for up-stem, bottom for down-stem
            # in the original MuseScore export). The stem's sy1/sy2 overlap with
            # the hook's ty.
            _stem_y = (sy1 + sy2) / 2 if 'sy1' in dir() else 0
            # Use original stem Y (before rhythm override) for system matching
            _orig_stem_y = (float(sm.group(3)) + float(sm.group(5))) / 2
            _has_hook = any(
                abs(round(sx) - _hx) < 60 and abs(round(_orig_stem_y) - _hy) < 800
                for _hx, _hy in _hook_positions
            )
            # 9 Ago 2026 (nuova regola Marco): nel Step2 i gambi devono essere
            # SEMPRE verso l'alto, anche le crome isolate con hook. Questo
            # semplifica la gestione dello spazio.
            stem_up = True  # forzatura: tutti in su, sempre
            if stem_up:
                # Gambo in su: parte dalla testa (middle_line_y), va verso l'alto
                sy1 = stem_mid_y
                sy2 = stem_mid_y - STEM_LEN_RHYTHM
                # Forza posizione X sul lato destro (gambo in su)
                new_sx = owner['center_x'] + owner_disc_r + 5
            else:
                # Gambo in giù: parte dalla testa (middle_line_y), va verso il basso
                sy1 = stem_mid_y
                sy2 = stem_mid_y + STEM_LEN_RHYTHM
                # Forza posizione X sul lato sinistro (gambo in giù)
                new_sx = owner['center_x'] - owner_disc_r - 5

        # Track the shift for beam repositioning (include Y range for overlap check)
        # Use a list — multiple stems can share the same X (different systems)
        stem_shifts_list.append((round(sx, 1), new_sx, min(sy1, sy2), max(sy1, sy2)))
        
        old_stem = sm.group(0)
        new_stem = (f'<polyline class="Stem" fill="none" stroke="{owner["color"]}" '
                    f'stroke-width="20" stroke-linecap="round" '
                    f'points="{new_sx:.2f},{sy1} {new_sx:.2f},{sy2}" />')
        modified = modified.replace(old_stem, new_stem, 1)
    
    # 1b. Reposition beams to follow their stems
    # Beams are <path class="Beam"> with 4-point quadrilateral: M(x1,y1) L(x2,y2) L(x3,y3) L(x4,y4)
    # Structure: P1=top-left, P2=top-right, P3=bottom-right, P4=bottom-left
    # Left edge at x1=x4, right edge at x2=x3
    # In MuseScore, beam extends ~4.7px past the outermost stems on each side
    # When stems shift, beam edges must shift to match
    # IMPORTANT: match stems by X proximity AND Y overlap (the stem must pass
    # through the beam's Y range — secondary beams connect to mid-stem points)
    beam_pat = re.compile(
        r'<path class="Beam"[^>]*d="M([\d.]+),([\d.]+) L([\d.]+),([\d.]+) L([\d.]+),([\d.]+) L([\d.]+),([\d.]+)[^"]*"\s*/>'
    )
    # pre-raggruppa le beam per X (stesso beam_left/right).
    # Per ogni gruppo, identifica primaria (Y più alto = più lontana dalla testa)
    # e secondaria (Y più basso = più vicina alla testa). Questo distingue
    # correttamente le travature doppie delle semicrome (MuseScore esporta
    # primaria e secondaria con lo STESSO spessore, quindi il vecchio metodo
    # beam_thickness > 40 le classificava entrambe come primarie → sovrapposte).
    beam_matches = list(beam_pat.finditer(modified))
    # Mappa: beam_left_rounded -> lista di (match, beam_top, beam_bot)
    # FIX: raggruppa solo per X-left, non (x_left, x_right).
    # La beam secondaria (semicroma) è più CORTA della primaria (x_right diverso),
    # ma parte dalla stessa X-left. Raggruppando per (x_left, x_right) non si
    # raggruppano mai → is_secondary sempre False → semicrome con singola beam.
    from collections import defaultdict
    beam_groups = defaultdict(list)
    for bm in beam_matches:
        x1 = float(bm.group(1)); x2 = float(bm.group(3))
        y1 = float(bm.group(2)); y2 = float(bm.group(4))
        y3 = float(bm.group(6)); y4 = float(bm.group(8))
        beam_top = min(y1, y2, y3, y4)
        beam_bot = max(y1, y2, y3, y4)
        key = round(float(bm.group(1)), -1)
        beam_groups[key].append((bm, beam_top, beam_bot))
    # Per ogni gruppo con >1 beam, ordina per beam_top: la più in alto (Y minore)
    # è primaria, le altre sono secondarie.
    # FIX involuzione travature — beam in SISTEMI DIVERSI
    # non sono primaria/secondaria, sono beam indipendenti. Solo beam nello
    # STESSO sistema (ΔY < 500px) formano una coppia primaria/secondaria.
    # Prima, beam a stessa X ma in sistemi diversi (es. battuta 5 e 7) venivano
    # raggruppate → la più in basso marcata secondaria → posizionata a metà gambo.
    beam_is_secondary = set()  # set di id(bm) che sono secondarie
    for key, group in beam_groups.items():
        if len(group) > 1:
            # Ordina per beam_top crescente (più in alto = Y minore)
            group_sorted = sorted(group, key=lambda g: g[1])
            # Sottomostra in cluster per sistema (ΔY < 500)
            used = [False] * len(group_sorted)
            for i in range(len(group_sorted)):
                if used[i]:
                    continue
                cluster = [i]
                used[i] = True
                for j in range(i+1, len(group_sorted)):
                    if not used[j] and abs(group_sorted[j][1] - group_sorted[i][1]) < 500:
                        cluster.append(j)
                        used[j] = True
                # Solo se il cluster ha 2+ beam nello stesso sistema:
                # la più in alto è primaria, le altre secondarie
                if len(cluster) > 1:
                    for k, idx in enumerate(cluster):
                        if k > 0:
                            beam_is_secondary.add(id(group_sorted[idx][0]))
    
    # Identify secondaries with DIFFERENT X-left.
    # For 1croma+2semicrome (eighth+16th+16th), the secondary beam starts at
    # the 2nd note (different X-left from the primary which starts at the 1st).
    # The X-left grouping above misses them. Add a second pass: for each beam
    # not already identified as secondary, check if it has significant X-overlap
    # with a longer beam in the same system (ΔY < 500) and is ABOVE it (y_top smaller).
    # If so, it's a secondary beam.
    for bm in beam_matches:
        if id(bm) in beam_is_secondary:
            continue
        x1 = float(bm.group(1)); x2 = float(bm.group(3))
        y1 = float(bm.group(2)); y2 = float(bm.group(4))
        y3 = float(bm.group(6)); y4 = float(bm.group(8))
        bm_top = min(y1, y2, y3, y4)
        bm_bot = max(y1, y2, y3, y4)
        bm_left = min(x1, x2)
        bm_right = max(x1, x2)
        bm_w = bm_right - bm_left
        for bm2 in beam_matches:
            if bm2 is bm:
                continue
            if id(bm2) in beam_is_secondary:
                continue
            x1b = float(bm2.group(1)); x2b = float(bm2.group(3))
            y1b = float(bm2.group(2)); y2b = float(bm2.group(4))
            y3b = float(bm2.group(6)); y4b = float(bm2.group(8))
            bm2_top = min(y1b, y2b, y3b, y4b)
            bm2_bot = max(y1b, y2b, y3b, y4b)
            bm2_left = min(x1b, x2b)
            bm2_right = max(x1b, x2b)
            bm2_w = bm2_right - bm2_left
            # bm2 must be a primary (longer) beam, bm must be shorter
            if bm2_w <= bm_w:
                continue
            # Must be in the same system (ΔY < 500)
            if abs(bm_top - bm2_top) > 500:
                continue
            # bm must be ABOVE bm2 (y_top smaller = secondary)
            if bm_top >= bm2_top - 5:
                continue
            # Must have significant X overlap (at least 50px)
            x_overlap = min(bm_right, bm2_right) - max(bm_left, bm2_left)
            if x_overlap <= 50:
                continue
            # bm is a secondary of bm2
            beam_is_secondary.add(id(bm))
            break
    
    for bm in beam_matches:
        x1, y1 = float(bm.group(1)), float(bm.group(2))
        x2, y2 = float(bm.group(3)), float(bm.group(4))
        x3, y3 = float(bm.group(5)), float(bm.group(6))
        x4, y4 = float(bm.group(7)), float(bm.group(8))
        beam_left = x1   # = x4
        beam_right = x2  # = x3
        beam_top = min(y1, y2, y3, y4)
        beam_bot = max(y1, y2, y3, y4)
        is_secondary = id(bm) in beam_is_secondary
        
        # Find the stem nearest to the beam's left/right edge
        # Must match by X proximity (within ±15px) AND Y overlap
        # (stem Y range must overlap beam Y range to avoid matching stems
        # from other systems that happen to be at similar X)
        # in rhythm mode, stem_shifts_list ha coordinate POST-SHIFT
        # ma beam ha coordinate ORIGINALI → Y overlap fallisce. Soluzione: in rhythm
        # mode, identifica il sistema dal beam Y originale (usando systems dict),
        # poi trova stem in quel sistema per vicinanza X.
        left_stem = None  # (old_sx, new_sx, s_min_y, s_max_y)
        right_stem = None
        if rhythm_mode:
            # Trova il sistema più vicino al beam Y originale (coordinate pre-shift)
            beam_orig_y = (y1 + y2 + y3 + y4) / 4
            sys_middle_ys = [s['middle_line_y'] for s in systems.values()]
            beam_sys_y = min(sys_middle_ys, key=lambda my: abs(my - beam_orig_y))
            for old_sx, new_sx, s_min_y, s_max_y in stem_shifts_list:
                # s_max_y = middle_line_y post-shift del sistema dello stem.
                # Filtra: solo stem nello stesso sistema del beam.
                # La middle_line_y post-shift ≈ middle_line_y originale * scale + shift.
                # Confronta s_max_y con la middle_line_y post-shift del sistema del beam.
                # Più semplice: lo stem appartiene al sistema del beam se la sua
                # middle_line_y originale (≈ s_max_y in rhythm no-stretch) è vicina.
                if abs(s_max_y - beam_sys_y) > 500:
                    continue
                if abs(old_sx - beam_left) < 15:
                    if left_stem is None or abs(old_sx - beam_left) < abs(left_stem[0] - beam_left):
                        left_stem = (old_sx, new_sx, s_min_y, s_max_y)
                if abs(old_sx - beam_right) < 15:
                    if right_stem is None or abs(old_sx - beam_right) < abs(right_stem[0] - beam_right):
                        right_stem = (old_sx, new_sx, s_min_y, s_max_y)
        else:
            for old_sx, new_sx, s_min_y, s_max_y in stem_shifts_list:
                # Y overlap check: stem passes through beam's Y range
                y_overlaps = s_min_y <= beam_bot + 50 and s_max_y >= beam_top - 50
                if not y_overlaps:
                    continue
                # Left stem: within ±15px of beam left edge
                if abs(old_sx - beam_left) < 15:
                    if left_stem is None or abs(old_sx - beam_left) < abs(left_stem[0] - beam_left):
                        left_stem = (old_sx, new_sx, s_min_y, s_max_y)
                # Right stem: within ±15px of beam right edge
                if abs(old_sx - beam_right) < 15:
                    if right_stem is None or abs(old_sx - beam_right) < abs(right_stem[0] - beam_right):
                        right_stem = (old_sx, new_sx, s_min_y, s_max_y)
        
        if left_stem is None and right_stem is None:
            continue  # Can't identify connected stems, skip
        
        # Compute new beam edges: preserve the ~4.7px overhang on each side
        BEAM_OVERHANG = 4.7
        if left_stem is not None:
            new_left = left_stem[1] - BEAM_OVERHANG
        else:
            # Only right stem found: shift left edge by same amount as right
            right_shift = right_stem[1] - right_stem[0]
            new_left = beam_left + right_shift
        
        if right_stem is not None:
            new_right = right_stem[1] + BEAM_OVERHANG
        else:
            # Only left stem found: shift right edge by same amount as left
            left_shift = left_stem[1] - left_stem[0]
            new_right = beam_right + left_shift
        
        # modalità rhythm — riposiziona le travature in Y.
        # 7 Ago: semplificato. I gambi vanno da middle_line_y a middle_line_y - STEM_LEN.
        # Beam PRIMARIA (croma): in cima al gambo (middle - STEM_LEN + 20).
        # Beam SECONDARIA (semicroma): a metà gambo (middle - STEM_LEN + 70).
        # Distinguo primaria/secondaria dalla posizione Y originale della beam:
        # se beam_top era nel terzo superiore del gambo originale → primaria,
        # altrimenti → secondaria.
        if rhythm_mode:
            ref_stem = left_stem if left_stem else right_stem
            rhythm_mid_y = ref_stem[3]  # s_max_y = middle_line_y (dopo shift)
            rhythm_stem_top = ref_stem[2]  # s_min_y = middle_line_y - STEM_LEN (dopo shift)
            stem_len = rhythm_mid_y - rhythm_stem_top  # ~350
            # Posizione beam primaria: esattamente in cima al gambo.
            # il gambo fuoriesce dalla travatura →
            # beam a rhythm_stem_top (cima gambo), non +20 (che lasciava 20px di gambo sopra).
            # il gambo ha stroke-width=20 con stroke-linecap=round →
            # sporge 10px sopra rhythm_stem_top. Beam primaria a -10 per coprire il round cap.
            beam_primary_y = rhythm_stem_top - 10
            # 8 Ago : beam secondaria troppo distante dalla primaria.
            # Ridurre a circa metà dello spazio: 0.35 → 0.175.
            beam_secondary_y = rhythm_stem_top - 10 + stem_len * 0.175
            # spessori FISSI (non beam_thickness post-stretch).
            # beam_thickness post-stretch è gonfiato (47→120px) → beam enorme.
            # Primaria 48px (spessore naturale MuseScore), secondaria 30px.
            beam_primary_thickness = 48
            beam_secondary_thickness = 30
            # Decidi primaria vs secondaria: se beam_bot originale era più vicina
            # alla cima del gambo (valore Y più piccolo = più in alto) → primaria
            # Usiamo la distanza beam→testa originale: primaria è più lontana
            # dalla testa (più in alto). Confronto beam_top con la middle line.
            # Senza stretch, beam_top originale è in coordinate originali.
            # La beam più in alto (beam_top più piccolo) è primaria.
            # usa is_secondary dal pre-raggruppamento per X.
            # pre-raggruppamento ora clusterizza per sistema (ΔY<500),
            # quindi is_secondary è affidabile. Beam non secondarie = primarie.
            if is_secondary:
                # Beam secondaria (semicroma): a 1/3 dal gambo, spessore ridotto
                new_beam_top = beam_secondary_y
                new_beam_bot = beam_secondary_y + beam_secondary_thickness
            else:
                # Beam primaria (croma o semicroma primaria): in cima al gambo
                new_beam_top = beam_primary_y
                new_beam_bot = beam_primary_y + beam_primary_thickness
            # Beam quadrilateral: top-left, top-right, bottom-right, bottom-left
            ny1, ny2 = new_beam_top, new_beam_top
            ny3, ny4 = new_beam_bot, new_beam_bot
        else:
            # FIX #151: Step1 (modalità normale) — limita spessore beam.
            # y_stretch_systems scala le Y di ~3x → il spessore beam originale
            # (~47px) diventa 140-280px. Riduci al spessore naturale mantenendo
            # la posizione centrale della beam.
            # NOTA: il fix viene applicato DOPO y_stretch_systems (vedi riga ~4490)
            # perché lo stretch ingrossa le beam. Qui manteniamo le coordinate originali.
            ny1, ny2, ny3, ny4 = y1, y2, y3, y4
        
        old_beam = bm.group(0)
        new_beam = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                    f'd="M{new_left:.2f},{ny1:.2f} L{new_right:.2f},{ny2:.2f} '
                    f'L{new_right:.2f},{ny3:.2f} L{new_left:.2f},{ny4:.2f} '
                    f'L{new_left:.2f},{ny1:.2f}"/>')
        modified = modified.replace(old_beam, new_beam, 1)

    # 1c. Reposition Hooks (eighth-note flags) to follow their stems
    # Hooks are <path class="Hook" transform="matrix(sx,0,0,sy,tx,ty)" d="..."/>
    # Y is already remapped by y_stretch_systems (transform matrix f parameter).
    # We only need to shift X to match the stem's new position.
    hook_pat = re.compile(
        r'<path class="Hook"[^>]*transform="matrix\(([^,]+),([^,]+),([^,]+),([^,]+),([^,]+),([^)]+)\)"[^>]*/>'
    )
    for hm in list(hook_pat.finditer(modified)):
        h_tx = float(hm.group(5))  # current tx (already Y-remapped, X is original)
        h_ty_orig = float(hm.group(6))  # current ty (Y-remapped by y_stretch)
        # Find the stem nearest to this hook (by X proximity)
        # in rhythm mode, filtra anche per sistema (Y) per evitare
        # di matchare stem di altri sistemi con stessa X.
        best_stem = None
        best_dist = 9999
        if rhythm_mode:
            # Identifica il sistema dell'hook dalla sua Y
            sys_middle_ys = [s['middle_line_y'] for s in systems.values()]
            hook_sys_y = min(sys_middle_ys, key=lambda my: abs(my - h_ty_orig))
            for old_sx, new_sx, s_min_y, s_max_y in stem_shifts_list:
                if abs(old_sx - h_tx) > 30:
                    continue
                # Solo stem nello stesso sistema dell'hook
                if abs(s_max_y - hook_sys_y) > 500:
                    continue
                dist = abs(old_sx - h_tx)
                if dist < best_dist:
                    best_dist = dist
                    best_stem = (old_sx, new_sx, s_min_y, s_max_y)
        else:
            # filtra per sistema anche in Step1 (non-rhythm).
            # Senza questo filtro, l'hook trova stem di altri sistemi con stessa X.
            sys_middle_ys = [s['middle_line_y'] for s in systems.values()]
            hook_sys_y = min(sys_middle_ys, key=lambda my: abs(my - h_ty_orig))
            for old_sx, new_sx, s_min_y, s_max_y in stem_shifts_list:
                if abs(old_sx - h_tx) > 30:
                    continue
                # Solo stem nello stesso sistema dell'hook
                stem_mid_y = (s_min_y + s_max_y) / 2
                if abs(stem_mid_y - hook_sys_y) > 500:
                    continue
                dist = abs(old_sx - h_tx)
                if dist < best_dist:
                    best_dist = dist
                    best_stem = (old_sx, new_sx, s_min_y, s_max_y)
        if best_stem is None:
            continue
        old_sx, new_sx, stem_min_y, stem_max_y = best_stem
        x_shift = new_sx - old_sx
        new_tx = h_tx + x_shift
        # modalità rhythm — riposiziona l'hook in Y alla cima del gambo.
        # 7 Ago: usa la Y dello stem (già post-shift) invece di middle_line_y originale.
        # 7 Ago fix: MuseScore esporta gambi in GIÙ, quindi i flag/hook curvano verso
        # il BASSO (path M0,-0.3 C0,1.9 ... → y positiva = basso). Forzando i gambi
        # in SU, il flag deve curvare verso l'ALTO (lontano dalla testa). Soluzione:
        # negare sy (mirror Y verticale) nella transform matrix. ty = cima del gambo.
        new_ty_str = hm.group(6)
        new_sy_str = hm.group(4)  # scale Y originale (positivo)
        new_d = None  # path replacement for flag-up hooks (None = keep original)
        if rhythm_mode:
            # 9 Ago 2026 (nuova regola Marco): gambi sempre verso l'alto nel Step2.
            # L'uncino va alla CIMA del gambo (y minore = stem_min_y) con
            # curva verso il BASSO a destra (come bandiera che pende).
            #
            # MuseScore esporta 2 tipi di path per gli hook (uncini di croma):
            # - "flag UP" (M0,-0.33): per note con gambo in GIÙ, uncino curva verso l'alto
            # - "flag DOWN" (M0,75.14): per note con gambo in SU, uncino curva verso il basso
            # Forzando tutti i gambi in SU, gli hook "flag UP" sono SBAGLIATI:
            # il path curva verso l'alto invece che verso il basso.
            # Soluzione: sostituire il path "flag UP" con il path "flag DOWN",
            # mantenendo sy POSITIVO e ty=stem_min_y (cima del gambo).
            new_ty = stem_min_y
            new_ty_str = f'{new_ty:.2f}'
            new_sy_str = hm.group(4)  # keep sy positive
            # Rileva path "flag UP": inizia con M0,NEGATIVE (es. M0,-0.328125)
            old_d_match = re.search(r'd="([^"]*)"', hm.group(0))
            if old_d_match:
                old_d_val = old_d_match.group(1)
                m_flag = re.match(r'M0,([-\d.]+)', old_d_val)
                if m_flag and float(m_flag.group(1)) < 0:
                    # flag UP → sostituisci con flag DOWN (curva verso il basso)
                    new_d = ('M0,75.1406 C0,76.125 0.328125,77.7813 2.64063,78.7813 '
                             'C16.875,83.4063 45.3438,101.281 66.8594,138.031 '
                             'C72.8125,148.281 82.4219,162.516 82.4219,189.984 '
                             'C82.4219,213.828 76.125,238.313 67.5156,262.141 '
                             'C66.8594,264.141 66.2031,265.781 66.5313,267.109 '
                             'C66.5313,269.094 67.8594,270.422 69.8438,270.422 '
                             'C72.1563,270.422 73.8125,269.094 75.1406,266.781 '
                             'C90.0313,240.297 95.6563,209.516 95.6563,179.406 '
                             'C94,136.703 69.5156,105.25 69.5156,105.25 '
                             'C70.5,105.25 39.0625,64.5469 30.125,50.6406 '
                             'C17.875,31.7813 12.25,12.9063 11.5781,11.5781 '
                             'C11.25,10.5938 8.28125,-1.32813 8.28125,-1.32813 '
                             'C7.9375,-2.64063 6.28125,-3.96875 4.29688,-3.96875 '
                             'C1.98438,-3.96875 0,-1.98438 0,0.328125 L0,75.1406 ')
        # Find owner note color — match from the STEM's stroke color (already
        # set by the stem-coloring pass above), which is more reliable than
        # matching notes by X/Y (in rhythm mode note Y is remapped and can
        # mismatch the hook's ty which is at the stem top).
        hook_color = '#000000'
        stem_color_pat = re.compile(
            r'<polyline class="Stem"[^>]*stroke="([^"]*)"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
        )
        for scm in stem_color_pat.finditer(modified):
            ssx = float(scm.group(2))
            ssy_min = min(float(scm.group(3)), float(scm.group(5)))
            if abs(ssx - new_sx) < 200 and abs(ssy_min - stem_min_y) < 500:
                hook_color = scm.group(1)
                break
        if hook_color == '#000000':
            # Fallback: match note by X/Y (original method)
            h_ty = float(hm.group(6))  # current ty (already Y-remapped)
            for n in notes:
                if abs(n['center_x'] - new_sx) < 200 and abs(n['y'] - h_ty) < 2000:
                    hook_color = n['color']
                    break
        old_hook = hm.group(0)
        old_transform = f'transform="matrix({hm.group(1)},{hm.group(2)},{hm.group(3)},{hm.group(4)},{hm.group(5)},{hm.group(6)})"'
        new_transform = f'transform="matrix({hm.group(1)},{hm.group(2)},{hm.group(3)},{new_sy_str},{new_tx:.2f},{new_ty_str})"'
        if old_transform not in old_hook:
            continue
        new_hook = old_hook.replace(old_transform, new_transform)
        # Replace flag-up path with flag-down path (curves DOWN for up-stems)
        if new_d is not None:
            old_d_match = re.search(r'd="[^"]*"', new_hook)
            if old_d_match:
                new_hook = new_hook.replace(old_d_match.group(0), f'd="{new_d}"')
        new_hook = new_hook.replace('<path class="Hook"', f'<path class="Hook" fill="{hook_color}"')
        if old_hook not in modified:
            continue
        modified = modified.replace(old_hook, new_hook, 1)

    # 1. Color noteheads (AFTER equalization so text/rects use updated center_x)
    # Style: colored FILLED CIRCLES with bold letters (MaidaScore notation)
    # Replaces the original notehead path entirely with a clean circle + text
    # (Original glyph with fill on open noteheads creates visual noise)
    
    for n in reversed(notes):
        is_synthetic = n.get('_synthetic', False)
        color = n['color']
        cx = n['center_x']
        # in modalità rhythm, tutte le teste sulla stessa Y (middle_line_y)
        # del sistema — niente pentagramma, solo figurazione ritmica.
        if rhythm_mode:
            cy = n['system']['middle_line_y']
        else:
            cy = n['y']
        scale_factor = n['scale'] / 1.25714
        dtype = n['duration_type']
        
        # Circle radius — smaller, ~1 staff space (like reference image)
        # crome and semicrome get PROPORTIONALLY SMALLER discs.
        # This makes the score more realistic (smaller value = smaller notehead) AND saves
        # space in the grey sectors (easier to fit many notes without overlap).
        base_disc_r = DISC_R_OVERRIDE if DISC_R_OVERRIDE else NOTEHEAD_HEIGHT * scale_factor * 0.9
        if dtype in ('16th', '16th_dotted'):
            disc_r = base_disc_r * 0.65  # semicroma = 65% of quarter
        elif dtype in ('eighth', 'eighth_dotted'):
            disc_r = base_disc_r * 0.80  # croma = 80% of quarter
        else:
            disc_r = base_disc_r  # quarter, half, whole = full size
        
        # Filled vs open notehead:
        # whole/half = OPEN (ring with colored stroke, white fill)
        # quarter/shorter = FILLED (solid colored disc)
        is_open = dtype in ('whole', 'whole_dotted', 'half', 'half_dotted')
        
        if is_open:
            # Open notehead: white fill, colored stroke (ring)
            # Use dark variant for stroke: the original light colors
            # (yellow/lime/orange) have insufficient contrast on white (WCAG < 3:1).
            # Text is always BLACK (#111111) for max contrast on white fill.
            dark_color = NOTE_COLORS_DARK.get(n['name'], color)
            disc = (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{disc_r:.0f}" '
                    f'fill="white" stroke="{dark_color}" stroke-width="16" data-dots="{n.get("dots", 0)}" />')
            # Letter color: always black — max contrast on white bg
            txt_fill = '#111111'
            halo = 'white'
        else:
            # Filled notehead: solid colored disc
            disc = (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{disc_r:.0f}" '
                    f'fill="{color}" stroke="white" stroke-width="18" data-dots="{n.get("dots", 0)}" />')
            # Letter color: white on dark, black on yellow
            txt_fill = n['text_color']
            halo = 'black' if txt_fill == '#FFFFFF' else 'white'
        
        # Text — note names in Italian (Do/Re/Mi/Fa/So/La/Si)
        # So abbreviato: tutte le note ora 1-2 char,
        name_it = n['name_it']
        n_chars = len(name_it)
        if n_chars <= 1:
            font_size = disc_r * 1.55
        else:  # 2 chars (Do/Re/Mi/Fa/So/La/Si)
            font_size = disc_r * 1.05
        stroke_w = font_size * 0.06  # 6% halo (not 12% — too thick eats letter)
        
        text_el = (f'\n<text x="{cx:.1f}" y="{cy:.1f}" '
                   f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                   f'font-size="{font_size:.0f}" font-weight="900" fill="{txt_fill}" '
                   f'stroke="{halo}" stroke-width="{stroke_w:.0f}" '
                   f'paint-order="stroke fill" '
                   f'text-anchor="middle" dy="0.35em">{name_it}</text>')
        
        # Draw accidental (#/b/natural) on the staff,
        # above or below the notehead circle. Same size as tavola accidentals
        # (206px). Position: ABOVE the circle if stem goes DOWN, BELOW if UP.
        # Regola (Marco, 9 Ago 2026): alterazione SOPRA il pallino se gambo
        # verso il basso (stem down), SOTTO il pallino se gambo verso l'alto
        # (stem up). Nei blocchi grigi (tavola) resta SOTTO il nome, sempre.
        # NOTE (9 Ago): le alterazioni sul pentagramma vengono disegnate DOPO
        # il Y-stretch (sezione 2e3b) per evitare che lo stretch le sposti in
        # posizione sbagliata. Qui non disegniamo nulla.
        acc_el = ''
        p_acc = n.get('staff_acc', '') or n.get('passing_acc', '')
        # Salva p_acc e stem_dir nella nota per disegnarle DOPO il Y-stretch
        # (sezione 2e3b). Le alterazioni sul pentagramma vengono posizionate
        # usando le coordinate post-stretch dei cerchi.
        if p_acc and p_acc in ('#', 'b', 'natural'):
            n['staff_acc_to_draw'] = p_acc
        else:
            n['staff_acc_to_draw'] = ''
        
        # Replace the ENTIRE original notehead path with circle + text
        # in modalità rhythm, niente testo (Do/Re/Mi) sulle teste —
        # solo il colore identifica la nota. La figurazione ritmica mostra il ritmo.
        # le alterazioni di passaggio NON vanno indicate
        # sopra i cerchi nel pentagramma. Vanno solo nella tavola sonora,
        # a sinistra del nome della nota. Qui disegniamo solo il disco.
        if rhythm_mode:
            replacement = disc + acc_el
        else:
            replacement = disc + text_el + acc_el
        
        # augmentation dots are now handled by MuseScore's NoteDot
        # paths (enlarged in section 2f2). We no longer add custom dots here.
        
        if is_synthetic:
            # Synthetic notes have no SVG path to replace — append before </svg>
            modified = modified.replace('</svg>', replacement + '\n</svg>')
        else:
            old_path = n['full_match']  # has ORIGINAL transform X (matches SVG)
            modified = modified.replace(old_path, replacement, 1)
    
    # NOTE: Accidental processing moved AFTER Y-stretch (section 2c) because
    # the Y-stretch remaps all transform matrix ty values, which would move
    # the carefully calculated accidental positions. See section 2e3 below.
    
    # 2. Thicker staff lines
    staff_width_match = re.search(r'<polyline class="StaffLines"[^>]*stroke-width="([\d.]+)"', svg_content)
    if staff_width_match:
        orig_width = staff_width_match.group(1)
        modified = re.sub(
            rf'stroke-width="{re.escape(orig_width)}"',
            f'stroke-width="{STAFF_LINE_WIDTH_NEW}"',
            modified
        )

    # modalità rhythm — nascondi pentagramma, chiave, tagli addizionali.
    # Niente pentagramma: solo figurazione ritmica (teste colorate su una riga).
    # Alterazioni (KeySig + accidentals) MANTENUTE (richiesto per accessibilità).
    if rhythm_mode:
        # Staff lines: trasparenti
        modified = re.sub(
            r'(<polyline class="StaffLines"[^>]*stroke=")([^"]*)(")',
            r'\1transparent\3',
            modified
        )
        # Chiave (clef): rimuovi i path della chiave di violino
        modified = re.sub(r'<path class="Clef"[^>]*/>', '', modified)
        # Tagli addizionali (ledger lines): rimuovi
        modified = re.sub(r'<polyline class="LedgerLine"[^>]*/>', '', modified)
        # Barlines: mantieni (separano le battute)
        print("    [rhythm] Staff lines, chiave, tagli addizionali nascosti")
    
    # 3. Quarter backgrounds — grey alternating stripes
    # For 4/4: 4 sectors per measure (1 per quarter beat)
    # For 6/8: 2 sectors per measure (1 per 3/8 = dotted quarter beat)
    # For 3/4: 3 sectors per measure
    # For compound meters (6/8, 9/8, 12/8): sectors = numerator / 3
    if note_info:
        ts_bg = note_info.get('time_sig', (4, 4))
        if ts_bg[1] == 8 and ts_bg[0] % 3 == 0:
            n_sectors = ts_bg[0] // 3  # 6/8→2, 9/8→3, 12/8→4
        else:
            n_sectors = ts_bg[0]  # 4/4→4, 3/4→3, 2/4→2
    else:
        n_sectors = 4
    from collections import defaultdict
    # TAVOLA constants — definiti qui (prima dell'uso nelle sezioni grigi riga ~3975)
    # per permettere alle sezioni grigie di estendersi fino alla tavola.
    TAVOLA_ROW_HEIGHT = 175  # SVG units height of tavola sonora row
    TAVOLA_GAP = 180  # gap between staff bottom and tavola row top
    bg_elements = []
    for x_start, info in systems.items():
        notes_in_sys = [n for n in notes if n['system_key'] == x_start]
        measures = equalized_measures.get(x_start)
        if not measures:
            measures = compute_measure_boundaries(x_start, info,
                                                   barlines.get(x_start, []),
                                                   notes_in_sys)
        
        staff_top = info['top'] - 3
        staff_height = info['bottom'] - info['top'] + 6
        
        # sezioni grigie estese fino alla tavola sonora.
        # Prima finivano al bottom del pentagramma. Ora scendono fino a tavola_top
        # per rendere chiaro che ogni tavola appartiene al pentagramma sopra.
        # Calcolo tavola_top come in draw_tavola_sonora:
        # tavola_top = bottom_y + max(tavola_gap, max_stem_y - bottom_y + 50)
        bottom_y = info['bottom']
        max_stem_y_grey = bottom_y
        stem_pat_grey = r'<polyline class="Stem"[^>]*points="([\d.]+),([\d.]+) ([\d.]+),([\d.]+)"'
        top_y_grey = info['top']
        for sm_grey in re.finditer(stem_pat_grey, modified):
            sx1_g, sy1_g, sx2_g, sy2_g = float(sm_grey.group(1)), float(sm_grey.group(2)), float(sm_grey.group(3)), float(sm_grey.group(4))
            stem_min_y_g = min(sy1_g, sy2_g)
            stem_max_y_g = max(sy1_g, sy2_g)
            if top_y_grey - 100 <= stem_min_y_g <= bottom_y + 100:
                if stem_max_y_g > max_stem_y_grey:
                    max_stem_y_grey = stem_max_y_g
        dynamic_gap_grey = max(TAVOLA_GAP, max_stem_y_grey - bottom_y + 50)
        tavola_top_grey = bottom_y + dynamic_gap_grey
        # Estendi le sezioni grigie fino all'inizio della tavola (non oltre, per non coprirla)
        grey_height = tavola_top_grey - staff_top
        
        # salta i settori grigi per le battute MMRest (1 battuta)
        # 4 Ago 2026 (bug KS): sys_global_start deve contare battute LOGICHE
        # FIX #147/#152: usa _sys_to_global_idx salvato durante l'equalizzazione
        # (calcolato in ordine Y corretto, come _sys_global_idx).
        sys_global_start = _sys_to_global_idx.get(x_start, measure_offset)
        mmrest_skip_indices = set()
        if mmrest_groups:
            _mmrest_count_map_grey = {gs: gc for gs, gc in mmrest_groups}
            _mmrest_set_grey = set(_mmrest_count_map_grey.keys()) if mmrest_groups else set()
            for gs, gc in mmrest_groups:
                if sys_global_start <= gs < sys_global_start + len(measures):
                    mmrest_skip_indices.add(gs - sys_global_start)
        
        # FIX #3 : grey sectors are ALWAYS BEAT_WIDTH (825px = 1 quarter).
        # A 3/4 measure has 3 sectors × 825px = 2475px (narrower than 4/4's 3300px).
        # A 2/4 measure has 2 sectors × 825px = 1650px.
        # FIX #4 : alternation is GLOBAL across the entire page,
        # not per-measure. A running counter (global_q) tracks the sector index
        # across all measures and systems so light/dark always alternate.
        global_q = 0  # running sector counter for global alternation
        for m_idx, (m_start, m_end) in enumerate(measures):
            m_width = m_end - m_start
            _gm_grey = sys_global_start + m_idx
            n_sectors_m = _n_sectors_for_measure(_gm_grey)
            sector_width = BEAT_WIDTH  # always 825px = 1 quarter beat

            for q in range(n_sectors_m):
                bg_color = BG_COLOR_LIGHT if global_q % 2 == 0 else BG_COLOR_DARK
                q_x = m_start + q * sector_width
                bg_elements.append(
                    f'<rect x="{q_x:.1f}" y="{staff_top:.1f}" '
                    f'width="{sector_width:.1f}" height="{grey_height:.1f}" '
                    f'fill="{bg_color}" opacity="{BG_OPACITY}" />'
                )
                global_q += 1
    
    if bg_elements:
        first_sl = re.search(r'<polyline class="StaffLines"', modified)
        if first_sl:
            pos = first_sl.start()
            modified = modified[:pos] + '\n'.join(bg_elements) + '\n' + modified[pos:]
    
    # 4. Duration bars — SMALL HORIZONTAL BARS to the right of each note
    # Like the reference image: a colored bar extends rightward from the note head
    # at the same height as the note, showing how long to hold the note.
    # whole = long bar (4 beats), half = medium bar (2 beats), quarter = short bar (1 beat)
    # The bar starts just to the right of the note circle and extends rightward.
    # CRITICAL: the bar NEVER crosses the barline (m_end) and never overlaps the next note.
    duration_rects = []
    
    for n in notes:
        dtype = n.get('dur_key', n['duration_type'])  # dur_key includes _dotted suffix
        # rettangolini durata SOLO per figure lunghe che durano
        # più di un settore grigio: semibrevi, minime, semiminime puntate.
        # Crome, semicrome e semiminime semplici NON hanno rettangolo (occupano 1 settore).
        has_rect = dtype in ('half', 'whole', 'half_dotted', 'whole_dotted',
                             'quarter_dotted')
        
        if not has_rect:
            continue
        
        cx = n['center_x']
        # 7 Ago: in rhythm mode tutte le teste sono sulla middle_line_y.
        # Il rettangolo durata deve essere alla stessa altezza del cerchio.
        if rhythm_mode:
            cy = n['system']['middle_line_y']
        else:
            cy = n['y']
        info = n['system']
        sk = n['system_key']
        notes_in_sys = [nn for nn in notes if nn['system_key'] == sk]
        measures = equalized_measures.get(sk)
        if not measures:
            measures = compute_measure_boundaries(sk, info, barlines.get(sk, []), notes_in_sys)
        
        scale_factor = n['scale'] / 1.25714
        base_r = DISC_R_OVERRIDE if DISC_R_OVERRIDE else NOTEHEAD_HEIGHT * scale_factor * 0.9
        # Proportional disc_r for croma/semicroma
        if dtype in ('16th', '16th_dotted'):
            disc_r = base_r * 0.65
        elif dtype in ('eighth', 'eighth_dotted'):
            disc_r = base_r * 0.80
        else:
            disc_r = base_r
        
        for m_idx_local, (m_start, m_end) in enumerate(measures):
            if m_start <= cx < m_end or (abs(cx - m_start) < 10):
                m_width = m_end - m_start
                # FIX #147/#152: per-measure time signature
                _n_measure_idx = n.get('measure_idx', 0)
                _ts_b_rect = _ts_beats_for_measure(_n_measure_idx)
                beat_width = m_width / _ts_b_rect
                
                # Duration in beats
                if dtype in ('whole', 'whole_dotted'):
                    beats = 4
                elif dtype == 'half':
                    beats = 2
                elif dtype == 'half_dotted':
                    beats = 3
                elif dtype == 'quarter':
                    beats = 1
                elif dtype == 'quarter_dotted':
                    beats = 1.5
                elif dtype in ('eighth', 'eighth_dotted'):
                    beats = 0.5
                elif dtype == '16th':
                    beats = 0.25
                else:
                    beats = 4
                
                # The bar starts just past the right edge of the circle.
                # staccare il rettangolo dal pallino di qualche pixel.
                # Per le note PUNTATE, il rettangolo va DOPO il punto (sempre staccato).
                # Il punto è a ~80px dal bordo del cerchio, raggio ~15px dopo ingrandimento.
                DUR_RECT_GAP = 20  # pixel di stacco dal pallino (o dal punto)
                is_dotted = n.get('dots', 0) > 0
                if is_dotted:
                    # Rettagolo dopo il punto: cerchio + 80px (dot center) + 15px (dot r) + gap
                    bar_x = cx + disc_r + 80 + 15 + DUR_RECT_GAP
                else:
                    bar_x = cx + disc_r + DUR_RECT_GAP
                
                # The ideal end of the bar = beat_start + beat_width * beats
                # where beat_start = m_start + int(onset) * beat_width
                # This ensures the bar aligns with the grey sector boundaries
                onset = n.get('onset', 0.0)
                beat_start = m_start + onset * beat_width
                ideal_end = beat_start + beat_width * beats
                
                # Find the actual barline position: the first barline >= cx
                # Use raw individual barline positions (not group centers)
                raw_bls = sorted(raw_barlines_by_system.get(sk, barlines.get(sk, [])))
                actual_barline = None
                for bl in raw_bls:
                    if bl > cx + disc_r:
                        actual_barline = bl
                        break
                
                # Find the next note's x position (next note in the same system)
                next_cx = None
                for nn in notes_in_sys:
                    if nn['center_x'] > cx:
                        if next_cx is None or nn['center_x'] < next_cx:
                            next_cx = nn['center_x']
                
                # Hard limits — use ACTUAL barline position, not m_end
                barline_limit = (actual_barline - 20) if actual_barline else (m_end - 20)
                next_note_limit = (next_cx - disc_r - 15) if next_cx else barline_limit
                
                # If the bar would start AFTER the barline, skip it entirely
                # (note is too close to the barline — no room for a duration bar)
                if bar_x >= barline_limit:
                    break
                
                # The bar end is the minimum of ideal_end, barline_limit, next_note_limit
                bar_end = min(ideal_end, barline_limit, next_note_limit)
                
                # Bar width = bar_end - bar_x (never negative)
                bar_width = bar_end - bar_x
                if bar_width < 30:
                    bar_width = 30  # minimum visible width
                    bar_end = bar_x + bar_width
                    # Re-check: if even the minimum width exceeds barline, clamp
                    if bar_end > barline_limit:
                        bar_width = max(10, barline_limit - bar_x)
                
                # Final safety: if bar_width is still <= 0, skip
                if bar_width <= 0:
                    break
                
                # Height: THIN bar (like reference image) — white fill, colored border
                bar_h = disc_r * 0.55
                bar_y = cy - bar_h / 2
                
                duration_rects.append(
                    f'<rect x="{bar_x:.1f}" y="{bar_y:.1f}" '
                    f'width="{bar_width:.1f}" height="{bar_h:.1f}" '
                    f'fill="white" stroke="{n["color"]}" stroke-width="6" '
                    f'rx="4" ry="4" />'
                )
                break
    
    if duration_rects:
        # Z-ORDER FIX: insert duration rects BEFORE the first note circle/text
        # so they appear BEHIND the noteheads, not on top of the letters
        first_circle = re.search(r'<circle ', modified)
        if first_circle:
            pos = first_circle.start()
            modified = modified[:pos] + '\n'.join(duration_rects) + '\n' + modified[pos:]
        else:
            modified = modified.replace('</svg>',
                                         '\n'.join(duration_rects) + '\n</svg>')
    
    # 2c. Y-stretch: widen vertical spacing between staff lines so the 260px discs fit.
    # Only Y coordinates are scaled (relative to each system's middle line).
    # X coordinates, disc sizes, stem widths, fonts — all unchanged.
    # Target: line spacing (adjacent lines) = 280px (disc_diameter=260 + 20px margin).
    # Add top/bottom margins and auto-calculate gap to fit the page.
    vb_match = re.search(r'viewBox="[\d.\s]+"', modified)
    page_height = 14028  # default
    if vb_match:
        vb_parts = re.search(r'viewBox="([\d.\s]+)"', modified)
        if vb_parts:
            page_height = float(vb_parts.group(1).split()[-1])
    # TAVOLA SONORA: increase system gap to make room for the tavola row below each system
    # (TAVOLA_ROW_HEIGHT e TAVOLA_GAP definiti prima, riga ~3947, per le sezioni grigie)
    # Tavola Sonora attiva: gap FISSO (no auto-gap) per evitare che 2 sistemi
    # su una pagina A4 vengano distanziati enormemente.
    # Gap totale = 150 (base) + tavola(350) + tavola_gap(100) = 600px tra sistemi
    # extra_system_gap must accommodate: tavola gap (dynamic, ~250 for down-stems) +
    # tavola_row_height + margin between tavola and next system
    TAVOLA_GAP_DYNAMIC = 170  # generous gap for down-stems (stems can extend ~210px below staff)
    # tavola dimezzata (350→175), riduciamo il gap totale di conseguenza
    # per ravvicinare i pentagrammi sottostanti. Risparmio: 175px per sistema.
    # modalità rhythm — NESSUN stretch del pentagramma (le linee non si
    # vedono, le teste sono sulla middle line). Solo gap ridotto tra sistemi.
    # Lo stretch precedente (target=150) ingrandiva i gambi di 1.59× (555px invece
    # di 350), sballando travature e uncini. Ora: spacing originale = nessuno stretch.
    if rhythm_mode:
        # Estrai spacing originale per non stretchare (scale ≈ 1.0)
        first_sys = next(iter(systems.values()))
        orig_spacing = 2 * first_sys['half_step']
        # target > spacing * 1.02 per superare il guard check scale > 1.01
        # (scale = target/spacing deve essere > 1.01 per non essere skip-pato)
        no_stretch_target = orig_spacing * 1.02
        # Gap fisso: deve ospitare i gambi (357px sopra middle line) + tavola (175) + margine.
        # Il gambo del sistema N va verso l'alto e non deve toccare la tavola del sistema N-1.
        # Distanza aumentata progressivamente: margine inizialmente 150, poi 300.
        # Gap = gambo (360) + tavola (175) + margine (300) = 835.
        # distanza pentagrammi +50% → margine 300→717px
        # era 835px (margine 300), target 835*1.5=1252 → margine 717
        rhythm_fixed_gap = 360 + TAVOLA_ROW_HEIGHT + 717
        modified = y_stretch_systems(modified, systems, target_line_spacing=no_stretch_target,
                                     extra_system_gap=0,
                                     fixed_system_gap=rhythm_fixed_gap,
                                     top_margin=700, bottom_margin=700,
                                     page_height=None)
        print(f"  [rhythm] Y-stretch: NO stretch (spacing={orig_spacing:.1f}), fixed_gap={rhythm_fixed_gap}")
    else:
        modified = y_stretch_systems(modified, systems, target_line_spacing=280,
                                     extra_system_gap=150 + TAVOLA_ROW_HEIGHT + TAVOLA_GAP_DYNAMIC,
                                     top_margin=700, bottom_margin=700,
                                     page_height=None)  # None = no auto-gap
    # TAVOLA_ROW_HEIGHT=175 (era 350), gap totale = 150+175+250 = 575 (era 750)
    
    # Extend StaffLines of the last system to cover all
    # grey sectors. MuseScore shrinks the last system's StaffLines, but our
    # equalization places grey sectors and barlines at full width. Extend
    # StaffLines to match the barline position.
    if not rhythm_mode:
        # Find the last system's StaffLines and extend them to the barline
        # Get all StaffLines polylines
        _sl_pattern = r'(<polyline class="StaffLines"[^>]*points=")([\d.\-]+),([\d.\-]+) ([\d.\-]+),([\d.\-]+)("[^>]*/>)'
        _sl_matches = list(re.finditer(_sl_pattern, modified))
        if _sl_matches:
            # Find the rightmost x_end among all StaffLines (this is the normal width)
            _all_x_ends = [float(m.group(4)) for m in _sl_matches]
            _max_x_end = max(_all_x_ends) if _all_x_ends else 0
            # Find StaffLines that are shorter than max (last system)
            _short_sl = [m for m in _sl_matches if float(m.group(4)) < _max_x_end - 1000]
            if _short_sl:
                # Extend short StaffLines to the max x_end
                for m in reversed(_short_sl):
                    x1 = m.group(2)
                    y1 = m.group(3)
                    y2 = m.group(5)
                    prefix = m.group(1)
                    suffix = m.group(6)
                    new_elem = f'{prefix}{x1},{y1} {_max_x_end:.2f},{y2}{suffix}'
                    modified = modified[:m.start()] + new_elem + modified[m.end():]
                print(f"  [Step1] Extended {len(_short_sl)} StaffLines in last system to x={_max_x_end:.0f}")
    
    # FIX #151: Step1 (modalità normale) — riduci spessore beam dopo y_stretch.
    # y_stretch_systems scala le Y di ~3x → il spessore beam originale (~47px)
    # diventa 140-280px. Riduci al spessore naturale (47px) mantenendo la
    # posizione centrale della beam. Solo per beam ingrossate (>70px).
    if not rhythm_mode:
        # Reduce beam thickness AND secondary beam gap after y_stretch.
        # y_stretch_systems scales Y ~3x → beam thickness ~47→140px (fixed to 47),
        # AND secondary beam gap ~47→163px (fix: reposition secondary close to primary).
        # Step 1: collect all beams, fix thickness.
        _beam_re = re.compile(
            r'<path class="Beam"[^>]*d="M([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+)[^"]*"\s*/>'
        )
        beam_infos = []  # (match_obj, x_left, x_right, y_top, y_bot, thickness)
        for m in _beam_re.finditer(modified):
            vals = [float(m.group(i+1)) for i in range(8)]
            y_top = min(vals[1], vals[3])
            y_bot = max(vals[5], vals[7])
            x_left = min(vals[0], vals[6])
            x_right = max(vals[2], vals[4])
            beam_infos.append({
                'match': m,
                'x_left': x_left, 'x_right': x_right,
                'y_top': y_top, 'y_bot': y_bot,
                'th': y_bot - y_top,
            })

        # Group beams by (x_left, system_y) — primary and secondary share the
        # same x_left AND are in the same system (Y within 500px).
        from collections import defaultdict
        beam_groups = defaultdict(list)
        for bi in beam_infos:
            # Use Y to distinguish systems: round to nearest 1000px
            # (500px was too fine — secondaries 210px above primary got
            # assigned to a different system, breaking the grouping)
            sys_y = round(bi['y_top'] / 1000) * 1000
            beam_groups[(round(bi['x_left']), sys_y)].append(bi)

        # For each group with 2+ beams in the same system (gap < 500px),
        # identify primary (lower, longer) and secondary (higher, shorter).
        # Reposition secondary so its bottom is ~47px above primary's top
        # (standard beam spacing = 1 staff space).
        BEAM_PAIR_GAP = 47  # desired gap between primary top and secondary bottom
        # Both beams are clamped to 47px thickness in _rebuild_beam.
        # Gap between edges = center_gap - 47 (half+half of 47px thickness).
        # We want edge gap = 47px, so center_gap = 47 + 47 = 94.
        DESIRED_CENTER_GAP = BEAM_PAIR_GAP + 47
        # merge broken secondary beams AND reposition them
        # close to the primary. MuseScore breaks the secondary beam between
        # pairs of sixteenth notes (e.g. 4 sixteenths = primary continuous +
        # secondary broken in 2+2). Marco wants the secondary continuous AND
        # close to the primary (47px gap = 1 staff space).
        # Strategy: identify primaries (longer beams) and secondaries (shorter
        # beams above primaries with X overlap). Reposition each secondary
        # to 47px above its primary. Merge broken secondary fragments into
        # one continuous beam spanning the full primary width.
        DESIRED_CENTER_GAP = 94  # 47px gap + 47px thickness = center gap
        
        # Step 1: identify primary vs secondary beams.
        # A secondary beam is ABOVE a primary beam (y_top smaller) with X overlap.
        # Width doesn't matter — MuseScore may export 2+2 sixteenths where
        # all beams have the same width.
        # A beam is secondary if there exists another beam BELOW it (larger y)
        # with significant X overlap, in the same system (Y within 500px).
        beam_is_secondary = set()
        primary_of = {}  # id(secondary) -> id(primary)
        for bi in beam_infos:
            for pri in beam_infos:
                if pri is bi:
                    continue
                # Secondary must be ABOVE primary (y_top smaller)
                if bi['y_top'] >= pri['y_top'] - 5:
                    continue
                # Must be in the same system (Y within 500px)
                if abs(bi['y_top'] - pri['y_top']) > 500:
                    continue
                # Must have significant X overlap (at least 50px)
                x_overlap = min(bi['x_right'], pri['x_right']) - max(bi['x_left'], pri['x_left'])
                if x_overlap <= 50:
                    continue
                # This bi is a secondary of pri (pri is the closest beam below)
                beam_is_secondary.add(id(bi))
                primary_of[id(bi)] = id(pri)
                break  # found its primary
        
        # Step 2: reposition each secondary to 47px above its primary
        for sec_id, pri_id in primary_of.items():
            sec = None
            pri = None
            for bi in beam_infos:
                if id(bi) == sec_id:
                    sec = bi
                if id(bi) == pri_id:
                    pri = bi
                if sec and pri:
                    break
            if sec and pri:
                pri_center = (pri['y_top'] + pri['y_bot']) / 2
                sec_center = pri_center - DESIRED_CENTER_GAP
                old_h = sec['y_bot'] - sec['y_top']
                sec['y_top'] = sec_center - old_h / 2
                sec['y_bot'] = sec_center + old_h / 2
        
        # Step 3: merge broken secondary fragments. For each primary, collect
        # all its secondaries, extend the widest one to span the full primary
        # width, and hide the others (zero width).
        primary_to_secondaries = defaultdict(list)
        for sec_id, pri_id in primary_of.items():
            for bi in beam_infos:
                if id(bi) == sec_id:
                    primary_to_secondaries[pri_id].append(bi)
                    break
        
        for pri_id, secs in primary_to_secondaries.items():
            pri = None
            for bi in beam_infos:
                if id(bi) == pri_id:
                    pri = bi
                    break
            if not pri:
                continue
            if len(secs) >= 2:
                # Multiple broken secondaries (e.g. 4 sixteenths = 2+2):
                # merge them into one continuous secondary.
                # Extend first secondary to span from first to last secondary.
                secs.sort(key=lambda s: s['x_left'])
                secs[0]['x_left'] = secs[0]['x_left']
                secs[0]['x_right'] = secs[-1]['x_right']
                for s in secs[1:]:
                    s['x_left'] = s['x_right']  # zero-width = invisible
            # If only 1 secondary, keep its original width — do NOT extend to
            # primary width (the primary may cover croma+2semicrome = 3 notes
            # while the secondary covers only the 2 semicrome).
        
        # Step 4: merge secondaries of adjacent primaries (e.g. 4 sixteenths
        # exported as 2+2 by MuseScore: each pair has its own primary+secondary).
        # We detect adjacent primaries (X-adjacent, same Y) and merge their
        # secondaries into one continuous secondary.
        primaries = [bi for bi in beam_infos if id(bi) not in beam_is_secondary]
        # Group primaries by Y (same system, same beam level)
        pri_y_groups = []
        for pri in sorted(primaries, key=lambda p: (p['y_top'], p['x_left'])):
            placed = False
            for g in pri_y_groups:
                if abs(g[0]['y_top'] - pri['y_top']) < 80:
                    g.append(pri)
                    placed = True
                    break
            if not placed:
                pri_y_groups.append([pri])
        for g in pri_y_groups:
            if len(g) < 2:
                continue
            # Sort by X and find adjacent pairs (gap < 300px)
            g.sort(key=lambda p: p['x_left'])
            i = 0
            while i < len(g) - 1:
                pri_a = g[i]
                pri_b = g[i + 1]
                gap = pri_b['x_left'] - pri_a['x_right']
                if gap < 150:  # adjacent primaries (2+2 sixteenths gap ~50-100px after shift)
                    # Find their secondaries
                    sec_a = None
                    sec_b = None
                    for sid, pid in primary_of.items():
                        if pid == id(pri_a):
                            for bi in beam_infos:
                                if id(bi) == sid:
                                    sec_a = bi; break
                        elif pid == id(pri_b):
                            for bi in beam_infos:
                                if id(bi) == sid:
                                    sec_b = bi; break
                    if sec_a and sec_b:
                        # Merge: extend sec_a to span both primaries
                        sec_a['x_left'] = min(pri_a['x_left'], sec_a['x_left'])
                        sec_a['x_right'] = max(pri_b['x_right'], sec_b['x_right'])
                        sec_b['x_left'] = sec_b['x_right']  # hide sec_b
                        # Also merge the primaries into one continuous beam
                        pri_a['x_left'] = min(pri_a['x_left'], pri_a['x_left'])
                        pri_a['x_right'] = max(pri_a['x_right'], pri_b['x_right'])
                        pri_b['x_left'] = pri_b['x_right']  # hide pri_b
                    i += 2  # skip merged pair
                else:
                    i += 1

        # Now rebuild SVG with fixed thickness and repositioned secondaries.
        # Use finditer + manual replacement (NOT re.sub) because re.sub re-scans
        # the modified string after each substitution, shifting match spans.
        # bi['match'].span() stores original offsets, so we must replace by offset.
        _beam_spans = sorted([(bi['match'].span(), bi) for bi in beam_infos], 
                             key=lambda x: x[0][0])
        # Build new SVG by replacing beams in reverse order (to preserve offsets)
        for span, bi in reversed(_beam_spans):
            x_left = bi['x_left']
            x_right = bi['x_right']
            y_top = bi['y_top']
            y_bot = bi['y_bot']
            # Skip hidden beams (zero or negative width = invisible)
            if x_right - x_left <= 0:
                modified = modified[:span[0]] + modified[span[1]:]
                continue
            # Clamp thickness to 47px, keeping center
            new_th = 47
            center = (y_top + y_bot) / 2
            y_top = center - new_th / 2
            y_bot = center + new_th / 2
            new_beam = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                        f'd="M{x_left:.2f},{y_top:.2f} L{x_right:.2f},{y_top:.2f} '
                        f'L{x_right:.2f},{y_bot:.2f} L{x_left:.2f},{y_bot:.2f} '
                        f'L{x_left:.2f},{y_top:.2f}"/>')
            modified = modified[:span[0]] + new_beam + modified[span[1]:]
        
        # Create secondary beams for dotted-eighth + sixteenth
        # (croma puntata + semicroma). MuseScore does NOT export a secondary beam
        # for this figure — only the primary beam is drawn. We must create the
        # secondary beam manually, covering only the sixteenth note, positioned
        # 47px above the primary (same gap as other sixteenth beam pairs).
        # Strategy: find primary beams that span exactly 2 notes where one note
        # has a NoteDot (dotted). The secondary beam covers from the midpoint
        # between the two notes to the right edge of the primary beam.
        _new_secondary_beams = []
        # Create secondary beams for dotted-eighth + sixteenth
        # (croma puntata + semicroma). MuseScore does NOT export a secondary beam
        # for this figure. We create it manually, covering only the sixteenth note,
        # positioned 47px above the primary (same gap as other sixteenth beam pairs).
        # Strategy: use note_info to find dotted-eighth+16th pairs by measure+onset.
        # Then find the SVG notes (with center_x) in the same measure, and identify
        # the primary beam that spans those 2 notes. Create secondary from midpoint
        # to right edge of primary.
        if note_info:
            _dotted_eighth_pairs = []  # (measure_idx, eighth_onset, sixteenth_onset)
            _ni_notes = note_info.get('notes', [])
            for i, n in enumerate(_ni_notes):
                if n.get('dur_key') == 'eighth_dotted' and i + 1 < len(_ni_notes):
                    nn = _ni_notes[i + 1]
                    if nn.get('dur_key') == '16th' and nn['measure_idx'] == n['measure_idx']:
                        _dotted_eighth_pairs.append((n['measure_idx'], n['onset'], nn['onset']))
            if _dotted_eighth_pairs:
                # Build measure_idx → svg_notes mapping (svg notes have center_x)
                _svg_notes_by_meas = {}
                for sn in notes:
                    m_idx = sn.get('measure_idx')
                    if m_idx is not None:
                        _svg_notes_by_meas.setdefault(m_idx, []).append(sn)
                for m_idx, e_onset, s_onset in _dotted_eighth_pairs:
                    meas_notes = _svg_notes_by_meas.get(m_idx, [])
                    if len(meas_notes) < 2:
                        continue
                    meas_notes_sorted = sorted(meas_notes, key=lambda n: n['center_x'])
                    # Get the Y range of these notes (to filter beams in the same system)
                    _notes_y = [sn.get('y', 0) for sn in meas_notes_sorted]
                    _notes_y_mid = (min(_notes_y) + max(_notes_y)) / 2 if _notes_y else 0
                    # Map note_info onsets to svg notes by order (both sorted)
                    _ni_meas_notes = sorted(
                        [n for n in _ni_notes if n['measure_idx'] == m_idx],
                        key=lambda n: n['onset'])
                    # Find the index of the dotted-eighth and the 16th in onset order
                    _e_idx = None
                    _s_idx = None
                    for j, n in enumerate(_ni_meas_notes):
                        if n['onset'] == e_onset and n.get('dur_key') == 'eighth_dotted':
                            _e_idx = j
                        if n['onset'] == s_onset and n.get('dur_key') == '16th':
                            _s_idx = j
                    if _e_idx is None or _s_idx is None:
                        continue
                    if _e_idx >= len(meas_notes_sorted) or _s_idx >= len(meas_notes_sorted):
                        continue
                    # Get the center_x of the dotted-eighth and 16th
                    cx_eighth = meas_notes_sorted[_e_idx]['center_x']
                    cx_sixteenth = meas_notes_sorted[_s_idx]['center_x']
                    # Find the primary beam that spans these 2 notes
                    _candidate_beams = []
                    for bi in beam_infos:
                        if id(bi) in beam_is_secondary:
                            continue
                        bl = bi['x_left']
                        br = bi['x_right']
                        byt = bi['y_top']
                        byb = bi['y_bot']
                        bw = br - bl
                        if bw < 200 or bw > 800:
                            continue
                        # Beam must be in the same system as the notes
                        bi_y_mid = (byt + byb) / 2
                        if abs(bi_y_mid - _notes_y_mid) > 2000:
                            continue
                        # Beam must span both the dotted-eighth and the 16th
                        if not (bl - 100 <= cx_eighth <= br + 100):
                            continue
                        if not (bl - 100 <= cx_sixteenth <= br + 100):
                            continue
                        # Count svg notes under this beam (must be exactly 2)
                        notes_under = [sn2 for sn2 in meas_notes_sorted
                                       if bl - 100 <= sn2['center_x'] <= br + 100]
                        if len(notes_under) != 2:
                            continue
                        _candidate_beams.append((bw, bi, byt, byb))
                    if _candidate_beams:
                        # Pick narrowest beam
                        _candidate_beams.sort(key=lambda x: x[0])
                        bw, bi, byt, byb = _candidate_beams[0]
                        # Create secondary beam: from midpoint to right edge
                        mid_x = (cx_eighth + cx_sixteenth) / 2
                        sec_x_left = mid_x
                        sec_x_right = bi['x_right']
                        pri_center = (byt + byb) / 2
                        sec_center = pri_center - 94  # ABOVE primary (non-rhythm)
                        sec_y_top = sec_center - 31 / 2  # thinner (31px, not 47px)
                        sec_y_bot = sec_center + 31 / 2
                        _new_secondary_beams.append((sec_x_left, sec_y_top, sec_x_right, sec_y_bot))

        # Create secondary beams for 8th+16th+16th figures (Step1, non-rhythm)
        # The two 16th notes need a secondary beam connecting them.
        if note_info:
            _ni_notes_s1b = note_info.get('notes', [])
            _sixteenth_pairs_s1 = []
            for i, n in enumerate(_ni_notes_s1b):
                if (n.get('dur_key') == '16th' and i + 1 < len(_ni_notes_s1b)
                        and _ni_notes_s1b[i + 1].get('dur_key') == '16th'
                        and _ni_notes_s1b[i + 1]['measure_idx'] == n['measure_idx']
                        and _ni_notes_s1b[i + 1]['onset'] == n['onset'] + 0.25):
                    # Only create secondary beam if preceded by an 8th
                    if i > 0 and _ni_notes_s1b[i - 1].get('dur_key') == 'eighth':
                        _sixteenth_pairs_s1.append((n['measure_idx'], n['onset'],
                                                   _ni_notes_s1b[i + 1]['onset']))
            if _sixteenth_pairs_s1:
                _svg_notes_by_meas_s1b = {}
                for sn in notes:
                    m_idx = sn.get('measure_idx')
                    if m_idx is not None:
                        _svg_notes_by_meas_s1b.setdefault(m_idx, []).append(sn)
                _new_sec_16_s1 = []
                for m_idx, s1_onset, s2_onset in _sixteenth_pairs_s1:
                    meas_notes = _svg_notes_by_meas_s1b.get(m_idx, [])
                    if len(meas_notes) < 2:
                        continue
                    meas_notes_sorted = sorted(meas_notes, key=lambda n: n['center_x'])
                    _ni_meas_s1b = sorted(
                        [n for n in _ni_notes_s1b if n['measure_idx'] == m_idx],
                        key=lambda n: n['onset'])
                    _s1_idx = None
                    _s2_idx = None
                    for j, n in enumerate(_ni_meas_s1b):
                        if n['onset'] == s1_onset and n.get('dur_key') == '16th':
                            _s1_idx = j
                        if n['onset'] == s2_onset and n.get('dur_key') == '16th':
                            _s2_idx = j
                    if _s1_idx is None or _s2_idx is None:
                        continue
                    if _s1_idx >= len(meas_notes_sorted) or _s2_idx >= len(meas_notes_sorted):
                        continue
                    cx_1 = meas_notes_sorted[_s1_idx]['center_x']
                    cx_2 = meas_notes_sorted[_s2_idx]['center_x']
                    _notes_y = [sn.get('y', 0) for sn in meas_notes_sorted]
                    _notes_y_mid = (min(_notes_y) + max(_notes_y)) / 2 if _notes_y else 0
                    _cand_beams_16_s1 = []
                    for bi in beam_infos:
                        if id(bi) in beam_is_secondary:
                            continue
                        bl = bi['x_left']
                        br = bi['x_right']
                        byt = bi['y_top']
                        byb = bi['y_bot']
                        bw = br - bl
                        if bw < 200 or bw > 800:
                            continue
                        bi_y_mid = (byt + byb) / 2
                        if abs(bi_y_mid - _notes_y_mid) > 2000:
                            continue
                        if not (bl - 100 <= cx_1 <= br + 100):
                            continue
                        if not (bl - 100 <= cx_2 <= br + 100):
                            continue
                        _cand_beams_16_s1.append((bw, bi, byt, byb))
                    if not _cand_beams_16_s1:
                        continue
                    _cand_beams_16_s1.sort(key=lambda x: x[0])
                    bw16, bi16, byt16, byb16 = _cand_beams_16_s1[0]
                    # Check no existing secondary in same system
                    has_sec_16 = False
                    for bi2 in beam_infos:
                        if id(bi2) not in beam_is_secondary:
                            continue
                        if min(bi2['x_right'], bi16['x_right']) - max(bi2['x_left'], bi16['x_left']) <= 50:
                            continue
                        if abs(bi2['y_top'] - byt16) > 500:
                            continue
                        has_sec_16 = True
                        break
                    if has_sec_16:
                        continue
                    sec_x_left = cx_1 - 20
                    sec_x_right = cx_2 + 20
                    pri_center = (byt16 + byb16) / 2
                    sec_center = pri_center - 94  # ABOVE primary (non-rhythm)
                    sec_y_top = sec_center - 31 / 2
                    sec_y_bot = sec_center + 31 / 2
                    _new_sec_16_s1.append((sec_x_left, sec_y_top, sec_x_right, sec_y_bot))
                if _new_sec_16_s1:
                    for sx1, syt, sx2, syb in _new_sec_16_s1:
                        new_sec = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                                   f'd="M{sx1:.2f},{syt:.2f} L{sx2:.2f},{syt:.2f} '
                                   f'L{sx2:.2f},{syb:.2f} L{sx1:.2f},{syb:.2f} '
                                   f'L{sx1:.2f},{syt:.2f}"/>')
                        modified = modified.replace('</svg>', new_sec + '\n</svg>')
                    print(f"  [Step1] Created {len(_new_sec_16_s1)} secondary beams for 16th+16th")
        if _new_secondary_beams:
            for sx1, syt, sx2, syb in _new_secondary_beams:
                new_sec = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                           f'd="M{sx1:.2f},{syt:.2f} L{sx2:.2f},{syt:.2f} '
                           f'L{sx2:.2f},{syb:.2f} L{sx1:.2f},{syb:.2f} '
                           f'L{sx1:.2f},{syt:.2f}"/>')
                modified = modified.replace('</svg>', new_sec + '\n</svg>')
            print(f"  [Step1] Created {len(_new_secondary_beams)} secondary beams for dotted-eighth+16th")
        
        # Shorten stems that extend beyond their beam.
        # After y_stretch, some stems are longer than the beam position,
        # causing them to poke out past the beam. Clip stem endpoints to
        # the nearest beam edge.
        # Re-parse beams from the modified SVG (positions may have changed).
        _final_beams = []
        for m in _beam_re.finditer(modified):
            vals = [float(m.group(i+1)) for i in range(8)]
            y_top = min(vals[1], vals[3])
            y_bot = max(vals[5], vals[7])
            x_left = min(vals[0], vals[6])
            x_right = max(vals[2], vals[4])
            _final_beams.append((x_left, y_top, x_right, y_bot))
        
        def _clip_stem(m):
            prefix = m.group(1)
            x1, y1, x2, y2 = (float(m.group(2)), float(m.group(3)),
                              float(m.group(4)), float(m.group(5)))
            suffix = m.group(6)
            stem_x = x1  # stems are vertical, x1 == x2
            stem_top = min(y1, y2)
            stem_bot = max(y1, y2)
            for bx1, by_top, bx2, by_bot in _final_beams:
                if bx1 - 30 <= stem_x <= bx2 + 30:
                    # FIX: Only clip if beam is in the same system as the stem
                    # (Y within 500px). Otherwise beams from other systems with
                    # the same X cause false matches.
                    if abs(by_top - stem_top) > 1000:
                        continue
                    # This stem is near this beam.
                    # If beam is below stem midpoint (down-stem, beam at bottom):
                    #   stem_bot should not exceed by_bot + 5 (allow small overlap)
                    # If beam is above stem midpoint (up-stem, beam at top):
                    #   stem_top should not go below by_top - 5
                    beam_mid = (by_top + by_bot) / 2
                    stem_mid = (stem_top + stem_bot) / 2
                    if beam_mid > stem_mid:
                        # Down-stem: beam at bottom. Clip stem_bot to by_bot.
                        # Account for stroke-linecap=round (adds stroke_width/2
                        # beyond the endpoint). stroke-width is typically 20,
                        # so subtract 10px extra.
                        if stem_bot > by_bot + 5:
                            clip_y = by_bot - 10  # 10px for round linecap
                            if y1 > y2:  # y1 is bottom
                                y1 = clip_y
                            else:
                                y2 = clip_y
                    else:
                        # Up-stem: beam at top. Clip stem_top to by_top.
                        # Account for stroke-linecap=round (adds stroke_width/2).
                        if stem_top < by_top - 5:
                            clip_y = by_top + 10  # 10px for round linecap
                            if y1 < y2:  # y1 is top
                                y1 = clip_y
                            else:
                                y2 = clip_y
                    break
            return f'{prefix}{x1:.2f},{y1:.2f} {x2:.2f},{y2:.2f}"{suffix}'
        
        modified = re.sub(
            r'(<polyline[^>]*points=")([\d.\-]+),([\d.\-]+) ([\d.\-]+),([\d.\-]+)"([^>]*>)',
            _clip_stem, modified)
    
    # merge broken secondary beams in rhythm mode too.
    # In rhythm mode, beams are repositioned by the rhythm code above, but
    # broken secondary fragments (2+2 sixteenths) are NOT merged.
    # Marco wants 4 sixteenths joined by 2 continuous parallel beams.
    if rhythm_mode:
        _beam_re_r = re.compile(
            r'<path class="Beam"[^>]*d="M([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+) L([\d.\-]+),([\d.\-]+)[^"]*"\s*/>'
        )
        beam_infos_r = []
        for m in _beam_re_r.finditer(modified):
            vals = [float(m.group(i+1)) for i in range(8)]
            y_top = min(vals[1], vals[3])
            y_bot = max(vals[5], vals[7])
            x_left = min(vals[0], vals[6])
            x_right = max(vals[2], vals[4])
            beam_infos_r.append({
                'match': m,
                'x_left': x_left, 'x_right': x_right,
                'y_top': y_top, 'y_bot': y_bot,
                'th': y_bot - y_top,
            })
        # Identify primary vs secondary (same logic as non-rhythm mode)
        # In rhythm mode, secondary beams may be at the SAME Y as primaries
        # (overlapped) because the rhythm code didn't distinguish them.
        # We identify secondaries by: shorter beam with X overlap of a longer beam.
        beam_is_secondary_r = set()
        primary_of_r = {}
        for bi in beam_infos_r:
            for pri in beam_infos_r:
                if pri is bi:
                    continue
                # Secondary must be ABOVE primary (y_top smaller)
                if bi['y_top'] >= pri['y_top'] - 5:
                    continue
                # Must be in the same system (Y within 200px)
                if abs(bi['y_top'] - pri['y_top']) > 200:
                    continue
                # Must have significant X overlap (at least 50px)
                x_overlap = min(bi['x_right'], pri['x_right']) - max(bi['x_left'], pri['x_left'])
                if x_overlap <= 50:
                    continue
                beam_is_secondary_r.add(id(bi))
                primary_of_r[id(bi)] = id(pri)
                break
        # Reposition secondaries: move them above their primary (47px gap)
        DESIRED_CENTER_GAP_R = 94  # 47px gap + 47px thickness
        for sec_id, pri_id in primary_of_r.items():
            sec = None
            pri = None
            for bi in beam_infos_r:
                if id(bi) == sec_id:
                    sec = bi
                if id(bi) == pri_id:
                    pri = bi
                if sec and pri:
                    break
            if sec and pri:
                pri_center = (pri['y_top'] + pri['y_bot']) / 2
                sec_center = pri_center - DESIRED_CENTER_GAP_R
                old_h = sec['y_bot'] - sec['y_top']
                sec['y_top'] = sec_center - old_h / 2
                sec['y_bot'] = sec_center + old_h / 2
        # Merge broken secondaries: extend first secondary to full primary width
        primary_to_secs_r = defaultdict(list)
        for sec_id, pri_id in primary_of_r.items():
            for bi in beam_infos_r:
                if id(bi) == sec_id:
                    primary_to_secs_r[pri_id].append(bi)
                    break
        for pri_id, secs in primary_to_secs_r.items():
            pri = None
            for bi in beam_infos_r:
                if id(bi) == pri_id:
                    pri = bi
                    break
            if not pri:
                continue
            if len(secs) >= 2:
                # Multiple broken secondaries (e.g. 4 sixteenths = 2+2):
                # merge into one continuous secondary spanning first to last.
                secs.sort(key=lambda s: s['x_left'])
                secs[0]['x_left'] = secs[0]['x_left']
                secs[0]['x_right'] = secs[-1]['x_right']
                for s in secs[1:]:
                    s['x_left'] = s['x_right']
            # If only 1 secondary, keep original width (do NOT extend to primary)
        # Step 4 (rhythm): merge secondaries of adjacent primaries (2+2 → 4)
        primaries_r = [bi for bi in beam_infos_r if id(bi) not in beam_is_secondary_r]
        pri_y_groups_r = []
        for pri in sorted(primaries_r, key=lambda p: (p['y_top'], p['x_left'])):
            placed = False
            for g in pri_y_groups_r:
                if abs(g[0]['y_top'] - pri['y_top']) < 80:
                    g.append(pri)
                    placed = True
                    break
            if not placed:
                pri_y_groups_r.append([pri])
        for g in pri_y_groups_r:
            if len(g) < 2:
                continue
            g.sort(key=lambda p: p['x_left'])
            i = 0
            while i < len(g) - 1:
                pri_a = g[i]
                pri_b = g[i + 1]
                gap = pri_b['x_left'] - pri_a['x_right']
                if gap < 150:  # adjacent primaries (2+2 sixteenths gap ~50-100px)
                    sec_a = None
                    sec_b = None
                    for sid, pid in primary_of_r.items():
                        if pid == id(pri_a):
                            for bi in beam_infos_r:
                                if id(bi) == sid:
                                    sec_a = bi; break
                        elif pid == id(pri_b):
                            for bi in beam_infos_r:
                                if id(bi) == sid:
                                    sec_b = bi; break
                    if sec_a and sec_b:
                        sec_a['x_left'] = min(pri_a['x_left'], sec_a['x_left'])
                        sec_a['x_right'] = max(pri_b['x_right'], sec_b['x_right'])
                        sec_b['x_left'] = sec_b['x_right']
                        pri_a['x_right'] = max(pri_a['x_right'], pri_b['x_right'])
                        pri_b['x_left'] = pri_b['x_right']
                    i += 2
                else:
                    i += 1
        # Rebuild beams
        _beam_spans_r = sorted([(bi['match'].span(), bi) for bi in beam_infos_r],
                               key=lambda x: x[0][0])
        for span, bi in reversed(_beam_spans_r):
            x_left = bi['x_left']
            x_right = bi['x_right']
            y_top = bi['y_top']
            y_bot = bi['y_bot']
            # Skip hidden beams (zero or negative width = invisible)
            if x_right - x_left <= 0:
                modified = modified[:span[0]] + modified[span[1]:]
                continue
            h = max(y_bot - y_top, 1)
            new_beam = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                        f'd="M{x_left:.2f},{y_top:.2f} L{x_right:.2f},{y_top:.2f} '
                        f'L{x_right:.2f},{y_bot:.2f} L{x_left:.2f},{y_bot:.2f} '
                        f'L{x_left:.2f},{y_top:.2f} "/>')
            modified = modified[:span[0]] + new_beam + modified[span[1]:]
        if primary_to_secs_r:
            print(f"  [rhythm] Merged {len(primary_to_secs_r)} secondary beam groups")
        
        # Create secondary beams for dotted-eighth + sixteenth
        # (croma puntata + semicroma) in rhythm mode. Same logic as Step1.
        if note_info:
            _dotted_eighth_pairs_r = []
            _ni_notes_r = note_info.get('notes', [])
            for i, n in enumerate(_ni_notes_r):
                if n.get('dur_key') == 'eighth_dotted' and i + 1 < len(_ni_notes_r):
                    nn = _ni_notes_r[i + 1]
                    if nn.get('dur_key') == '16th' and nn['measure_idx'] == n['measure_idx']:
                        _dotted_eighth_pairs_r.append((n['measure_idx'], n['onset'], nn['onset']))
            if _dotted_eighth_pairs_r:
                _svg_notes_by_meas_r = {}
                for sn in notes:
                    m_idx = sn.get('measure_idx')
                    if m_idx is not None:
                        _svg_notes_by_meas_r.setdefault(m_idx, []).append(sn)
                _new_sec_beams_r = []
                for m_idx, e_onset, s_onset in _dotted_eighth_pairs_r:
                    meas_notes = _svg_notes_by_meas_r.get(m_idx, [])
                    if len(meas_notes) < 2:
                        continue
                    meas_notes_sorted = sorted(meas_notes, key=lambda n: n['center_x'])
                    _ni_meas_notes_r = sorted(
                        [n for n in _ni_notes_r if n['measure_idx'] == m_idx],
                        key=lambda n: n['onset'])
                    _e_idx = None
                    _s_idx = None
                    for j, n in enumerate(_ni_meas_notes_r):
                        if n['onset'] == e_onset and n.get('dur_key') == 'eighth_dotted':
                            _e_idx = j
                        if n['onset'] == s_onset and n.get('dur_key') == '16th':
                            _s_idx = j
                    if _e_idx is None or _s_idx is None:
                        continue
                    if _e_idx >= len(meas_notes_sorted) or _s_idx >= len(meas_notes_sorted):
                        continue
                    cx_eighth = meas_notes_sorted[_e_idx]['center_x']
                    cx_sixteenth = meas_notes_sorted[_s_idx]['center_x']
                    # Get the Y position of the notes in this measure (to filter
                    # beams in the same system — avoid matching beams from
                    # other systems with similar X).
                    _notes_y = [sn.get('y', 0) for sn in meas_notes_sorted]
                    _notes_y_mid = (min(_notes_y) + max(_notes_y)) / 2 if _notes_y else 0
                    # Find the primary beam that spans these 2 notes
                    _candidate_beams_r = []
                    for bi in beam_infos_r:
                        if id(bi) in beam_is_secondary_r:
                            continue
                        bl = bi['x_left']
                        br = bi['x_right']
                        byt = bi['y_top']
                        byb = bi['y_bot']
                        bw = br - bl
                        if bw < 200 or bw > 800:
                            continue
                        # Beam must be in the same system as the notes
                        bi_y_mid = (byt + byb) / 2
                        if abs(bi_y_mid - _notes_y_mid) > 2000:
                            continue
                        if not (bl - 100 <= cx_eighth <= br + 100):
                            continue
                        if not (bl - 100 <= cx_sixteenth <= br + 100):
                            continue
                        notes_under = [sn2 for sn2 in meas_notes_sorted
                                       if bl - 100 <= sn2['center_x'] <= br + 100]
                        if len(notes_under) != 2:
                            continue
                        _candidate_beams_r.append((bw, bi, byt, byb))
                    if _candidate_beams_r:
                        # Pick narrowest beam (closest to just these 2 notes)
                        _candidate_beams_r.sort(key=lambda x: x[0])
                        bw, bi, byt, byb = _candidate_beams_r[0]
                        # FIX: Check if there's already a secondary beam for this
                        # primary IN THE SAME SYSTEM. If so, don't create another.
                        has_existing_sec = False
                        for bi2 in beam_infos_r:
                            if id(bi2) not in beam_is_secondary_r:
                                continue
                            bl2 = bi2['x_left']
                            br2 = bi2['x_right']
                            x_overlap = min(br2, bi['x_right']) - max(bl2, bi['x_left'])
                            if x_overlap <= 50:
                                continue
                            if abs(bi2['y_top'] - byt) > 500:
                                continue
                            has_existing_sec = True
                            break
                        if not has_existing_sec:
                            # Create secondary beam: from midpoint to right edge
                            mid_x = (cx_eighth + cx_sixteenth) / 2
                            sec_x_left = mid_x
                            sec_x_right = bi['x_right']
                            # In rhythm mode, primary beam is at top (y smaller),
                            # secondary is below (y larger, closer to notehead).
                            # Gap = 94px center-to-center. Thickness = 31px
                            # (thinner than primary's 49px, matching MuseScore).
                            pri_center = (byt + byb) / 2
                            sec_center = pri_center + 94  # BELOW primary
                            sec_y_top = sec_center - 31 / 2
                            sec_y_bot = sec_center + 31 / 2
                            _new_sec_beams_r.append((sec_x_left, sec_y_top, sec_x_right, sec_y_bot))
                if _new_sec_beams_r:
                    for sx1, syt, sx2, syb in _new_sec_beams_r:
                        new_sec = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                                   f'd="M{sx1:.2f},{syt:.2f} L{sx2:.2f},{syt:.2f} '
                                   f'L{sx2:.2f},{syb:.2f} L{sx1:.2f},{syb:.2f} '
                                   f'L{sx1:.2f},{syt:.2f}"/>')
                        modified = modified.replace('</svg>', new_sec + '\n</svg>')
                    print(f"  [Step2] Created {len(_new_sec_beams_r)} secondary beams for dotted-eighth+16th")

        # Create secondary beams for 8th+16th+16th figures
        # (croma + semicroma + semicroma). The two 16th notes need a secondary
        # beam connecting them (La-Si pattern, battuta 17). This is a separate
        # block from dotted-eighth+16th because the 8th+16th+16th figure does
        # NOT have a dotted eighth — it's a plain eighth followed by two 16ths.
        if note_info:
            _ni_notes_r2 = note_info.get('notes', [])
            _sixteenth_pairs_r = []
            for i, n in enumerate(_ni_notes_r2):
                if (n.get('dur_key') == '16th' and i + 1 < len(_ni_notes_r2)
                        and _ni_notes_r2[i + 1].get('dur_key') == '16th'
                        and _ni_notes_r2[i + 1]['measure_idx'] == n['measure_idx']
                        and _ni_notes_r2[i + 1]['onset'] == n['onset'] + 0.25):
                    # Only create secondary beam if preceded by an 8th
                    # (8th+16th+16th figure). Skip if part of 4+ sixteenth group.
                    if i > 0 and _ni_notes_r2[i - 1].get('dur_key') == 'eighth':
                        _sixteenth_pairs_r.append((n['measure_idx'], n['onset'],
                                                   _ni_notes_r2[i + 1]['onset']))
            if _sixteenth_pairs_r:
                _svg_notes_by_meas_r2 = {}
                for sn in notes:
                    m_idx = sn.get('measure_idx')
                    if m_idx is not None:
                        _svg_notes_by_meas_r2.setdefault(m_idx, []).append(sn)
                _new_sec_16_beams = []
                for m_idx, s1_onset, s2_onset in _sixteenth_pairs_r:
                    meas_notes = _svg_notes_by_meas_r2.get(m_idx, [])
                    if len(meas_notes) < 2:
                        continue
                    meas_notes_sorted = sorted(meas_notes, key=lambda n: n['center_x'])
                    _ni_meas_notes_r2 = sorted(
                        [n for n in _ni_notes_r2 if n['measure_idx'] == m_idx],
                        key=lambda n: n['onset'])
                    _s1_idx = None
                    _s2_idx = None
                    for j, n in enumerate(_ni_meas_notes_r2):
                        if n['onset'] == s1_onset and n.get('dur_key') == '16th':
                            _s1_idx = j
                        if n['onset'] == s2_onset and n.get('dur_key') == '16th':
                            _s2_idx = j
                    if _s1_idx is None or _s2_idx is None:
                        continue
                    if _s1_idx >= len(meas_notes_sorted) or _s2_idx >= len(meas_notes_sorted):
                        continue
                    cx_1 = meas_notes_sorted[_s1_idx]['center_x']
                    cx_2 = meas_notes_sorted[_s2_idx]['center_x']
                    _notes_y = [sn.get('y', 0) for sn in meas_notes_sorted]
                    _notes_y_mid = (min(_notes_y) + max(_notes_y)) / 2 if _notes_y else 0
                    # Find primary beam spanning these 2 notes
                    _cand_beams_16 = []
                    for bi in beam_infos_r:
                        if id(bi) in beam_is_secondary_r:
                            continue
                        bl = bi['x_left']
                        br = bi['x_right']
                        byt = bi['y_top']
                        byb = bi['y_bot']
                        bw = br - bl
                        if bw < 200 or bw > 800:
                            continue
                        bi_y_mid = (byt + byb) / 2
                        if abs(bi_y_mid - _notes_y_mid) > 2000:
                            continue
                        if not (bl - 100 <= cx_1 <= br + 100):
                            continue
                        if not (bl - 100 <= cx_2 <= br + 100):
                            continue
                        _cand_beams_16.append((bw, bi, byt, byb))
                    if not _cand_beams_16:
                        continue
                    _cand_beams_16.sort(key=lambda x: x[0])
                    bw16, bi16, byt16, byb16 = _cand_beams_16[0]
                    # Check no existing secondary in same system
                    has_sec_16 = False
                    for bi2 in beam_infos_r:
                        if id(bi2) not in beam_is_secondary_r:
                            continue
                        if min(bi2['x_right'], bi16['x_right']) - max(bi2['x_left'], bi16['x_left']) <= 50:
                            continue
                        if abs(bi2['y_top'] - byt16) > 500:
                            continue
                        has_sec_16 = True
                        break
                    if has_sec_16:
                        continue
                    # Create secondary beam: from first 16th to second 16th
                    sec_x_left = cx_1 - 20
                    sec_x_right = cx_2 + 20
                    pri_center = (byt16 + byb16) / 2
                    sec_center = pri_center + 94  # BELOW primary (rhythm mode)
                    sec_y_top = sec_center - 31 / 2
                    sec_y_bot = sec_center + 31 / 2
                    _new_sec_16_beams.append((sec_x_left, sec_y_top, sec_x_right, sec_y_bot))
                if _new_sec_16_beams:
                    for sx1, syt, sx2, syb in _new_sec_16_beams:
                        new_sec = (f'<path class="Beam" fill="#000000" fill-rule="evenodd" '
                                   f'd="M{sx1:.2f},{syt:.2f} L{sx2:.2f},{syt:.2f} '
                                   f'L{sx2:.2f},{syb:.2f} L{sx1:.2f},{syb:.2f} '
                                   f'L{sx1:.2f},{syt:.2f}"/>')
                        modified = modified.replace('</svg>', new_sec + '\n</svg>')
                    print(f"  [Step2] Created {len(_new_sec_16_beams)} secondary beams for 16th+16th")
    
    # Re-parse systems AFTER Y-stretch (coordinates have changed!)
    # This is needed for enlarge_clef, rests, and accidentals which use system Y ranges
    # to find which system each element belongs to.
    parsed_post_stretch = parse_svg(modified)
    systems_post = parsed_post_stretch['systems']
    
    # 2d. Measure numbers — remove MuseScore glyph outlines, add readable <text> (2 Ago 2026)
    # MuseScore renders measure numbers as <path class="MeasureNumber" d="M..."> (glyph outlines).
    # These are illegible at our scale and often show "0" (music21 doesn't set measure numbers).
    # FIX: remove all MuseScore MeasureNumber paths, then add our own <text> elements with
    # the correct measure number (1, 2, 3...) above the start of each measure.
    modified = re.sub(r'<path class="MeasureNumber"[^>]*/>', '', modified)
    
    # Build system Y positions (post-Y-stretch) for placing numbers above each system
    target_line_spacing_val = 280
    top_margin_val = 700
    bottom_margin_val = 700
    mn_sys_info = []
    for x_start, info in systems.items():
        top = info['top']
        bottom = info['bottom']
        middle = info['middle_line_y']
        half_step = info['half_step']
        current_spacing = 2 * half_step
        scale = target_line_spacing_val / current_spacing if current_spacing > 0 else 1
        if scale <= 1.01:
            scale = 1.0
        margin = (bottom - top) * 1.5
        mn_sys_info.append({
            'middle': middle, 'scale': scale,
            'y_min': top - margin, 'y_max': bottom + margin,
            'top': top, 'bottom': bottom, 'x_start': x_start,
        })
    mn_sys_info.sort(key=lambda s: s['top'])
    # Compute stretched heights and gaps (same as y_stretch_systems)
    for si in mn_sys_info:
        si['stretched_height'] = (si['bottom'] - si['top']) * si['scale']
    n_sys = len(mn_sys_info)
    if page_height and n_sys > 0:
        total_h = sum(si['stretched_height'] for si in mn_sys_info)
        orig_gaps = sum(
            (mn_sys_info[i+1]['middle'] + (mn_sys_info[i+1]['top'] - mn_sys_info[i+1]['middle']) * mn_sys_info[i+1]['scale'])
            - (mn_sys_info[i]['middle'] + (mn_sys_info[i]['bottom'] - mn_sys_info[i]['middle']) * mn_sys_info[i]['scale'])
            for i in range(n_sys - 1)
        )
        avail = page_height - top_margin_val - bottom_margin_val - total_h - orig_gaps
        mn_gap = avail / (n_sys - 1) if n_sys > 1 and avail > 0 else 0
    else:
        mn_gap = 150
    first_stretched_top = mn_sys_info[0]['middle'] + (mn_sys_info[0]['top'] - mn_sys_info[0]['middle']) * mn_sys_info[0]['scale'] if mn_sys_info else 0
    mn_base_shift = top_margin_val - first_stretched_top if mn_sys_info else 0
    for i, si in enumerate(mn_sys_info):
        si['y_shift'] = mn_base_shift + mn_gap * i
        # Stretched top Y of this system (where number will be placed above)
        si['stretched_top'] = si['middle'] + (si['top'] - si['middle']) * si['scale'] + si['y_shift']
    
    # Add measure number <text> elements above each measure start
    # Read ACTUAL StaffLines Y positions from the SVG (not theoretical stretched_top)
    # to ensure numbers are placed exactly above each real system.
    staff_y_matches = re.findall(
        r'<polyline class="StaffLines"[^>]*points="[\d.\-]+,([\d.\-]+)', modified
    )
    all_staff_ys = sorted(set(float(y) for y in staff_y_matches))
    # Group staff Ys into systems (5 lines per system, gap > 400px = new system)
    real_system_tops = []
    if all_staff_ys:
        current_top = all_staff_ys[0]
        prev_y = all_staff_ys[0]
        for y in all_staff_ys[1:]:
            if y - prev_y > 400:
                real_system_tops.append(current_top)
                current_top = y
            prev_y = y
        real_system_tops.append(current_top)
    
    # Map mn_sys_info to real_system_tops by order (both sorted by top Y)
    # Also extract real note circles from SVG to detect high notes (above staff)
    # that could overlap with measure numbers.
    svg_circles = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', modified)
    svg_circle_data = [(float(cx), float(cy), float(r)) for cx, cy, r in svg_circles]
    # Extract stems (polyline class="Stem") — vertical lines that can extend
    # upward into the measure number's Y range.
    svg_stems = re.findall(
        r'<polyline class="Stem"[^>]*points="([\d.\-]+),([\d.\-]+) ([\d.\-]+),([\d.\-]+)"',
        modified
    )
    svg_stem_data = [(float(x1), float(y1), float(x2), float(y2)) for x1, y1, x2, y2 in svg_stems]
    
    measure_number_count = 0
    measure_number_texts = []
    # global measure counter — starts from measure_offset for
    # multi-page continuity. Counts ALL measures including rest-only measures.
    global_m_idx = measure_offset
    # calcola il numero di battuta REALE tenendo conto che ogni
    # MMRest(N) contribuisce N battute, non 1. Costruisci una mappa
    # mscz_idx → real_measure_number.
    real_measure_num = {}
    _real_num = 1  # 1-based
    _all_mmrest_indices = sorted(gs for gs, gc in (mmrest_groups or []))
    _mmrest_map = dict(mmrest_groups or [])
    for _mi in range(200):  # safety limit
        if _mi in _mmrest_map:
            # MMRest: occupa N battute reali
            real_measure_num[_mi] = _real_num  # numero della prima battuta
            _real_num += _mmrest_map[_mi]
        else:
            real_measure_num[_mi] = _real_num
            _real_num += 1
    for si_idx, si in enumerate(mn_sys_info):
        x_start = si['x_start']
        em_bounds = equalized_measures.get(x_start, [])
        if not em_bounds:
            continue
        # Find the measure_idx of the first measure in this system from notes
        sys_notes = [n for n in notes if abs(n['y'] - si['middle']) < 500]
        sys_measure_indices = sorted(set(n.get('measure_idx', -1) for n in sys_notes if 'measure_idx' in n))
        # Use global_m_idx for numbering (counts ALL measures including rests)
        # Fall back to note-based index only if global counter is not available
        if sys_measure_indices:
            first_m_idx = global_m_idx
        else:
            first_m_idx = global_m_idx
        # Use REAL system top Y from SVG, fall back to theoretical if not available
        if si_idx < len(real_system_tops):
            sys_top_y = real_system_tops[si_idx]
        else:
            sys_top_y = si['stretched_top']
        mn_y = sys_top_y - 140
        mn_font_size = 160
        # Find ALL circles near the top of the staff in this system.
        # A note sitting on the top line (cy ≈ sys_top_y) has its upper edge
        # at cy - r, which can overlap the measure number at sys_top_y - 140.
        # So we check any circle whose top edge could reach into the number's Y range.
        nearby_circles = [
            (cx, cy, r) for cx, cy, r in svg_circle_data
            if cy - r < sys_top_y - 40 and cy + r > sys_top_y - 240
        ]
        # Also extract stems (polyline class="Stem") that could overlap.
        # A stem is a vertical line; its top can extend up into the number's Y range.
        # Stems in this system have Y overlapping [sys_top_y - 240, sys_top_y - 40].
        system_stems = []
        for sx1, sy1, sx2, sy2 in svg_stem_data:
            stem_top = min(sy1, sy2)
            stem_bot = max(sy1, sy2)
            stem_x = (sx1 + sx2) / 2
            # Only stems whose vertical range intersects the number's Y band
            if stem_top < sys_top_y - 40 and stem_bot > sys_top_y - 240:
                system_stems.append((stem_x, stem_top, stem_bot))
        for grp_idx, (m_start, m_end) in enumerate(em_bounds):
            m_idx = first_m_idx + grp_idx
            if m_idx < 0:
                continue
            # salta i numeri di battuta per le battute MMRest
            # (il numero è già mostrato nel box nero)
            if m_idx in _mmrest_map:
                continue
            mn_num = m_idx + 1  # 4 Ago 2026 (bug KS): global_m_idx è già in battute logiche
            mn_x = m_start + 20
            # Check if any high note circle overlaps with the measure number position
            # Number spans roughly mn_x to mn_x + 120 (font 160, 1-2 digits)
            # Number Y center = sys_top_y - 140, height ~160px → from sys_top_y-220 to sys_top_y-60
            mn_y_top = sys_top_y - 240
            mn_y_bot = sys_top_y - 40
            for hc_x, hc_y, hc_r in nearby_circles:
                # Bounding box overlap between circle and number
                # Number: X=[mn_x-20, mn_x+140], Y=[sys_top_y-240, sys_top_y-40]
                # Circle: X=[hc_x-hc_r, hc_x+hc_r], Y=[hc_y-hc_r, hc_y+hc_r]
                if (hc_x - hc_r) < (mn_x + 140) and (hc_x + hc_r) > (mn_x - 20) \
                   and (hc_y - hc_r) < (sys_top_y - 40) and (hc_y + hc_r) > (sys_top_y - 240):
                    # Overlap! Shift number to the LEFT to avoid the high note
                    mn_x = m_start + 20 - 220
                    if mn_x < 50:
                        # Can't go left enough, try right of the note
                        mn_x = hc_x + 160
                    break
            # Also check stems (vertical lines) that could overlap the number
            if mn_x == m_start + 20:  # only if not already shifted by circle check
                for stem_x, stem_top, stem_bot in system_stems:
                    # Stem is ~20px wide vertically; check X and Y overlap with number
                    if (stem_x - 15) < (mn_x + 140) and (stem_x + 15) > (mn_x - 20) \
                       and stem_top < (sys_top_y - 40) and stem_bot > (sys_top_y - 240):
                        mn_x = m_start + 20 - 220
                        if mn_x < 50:
                            mn_x = stem_x + 160
                        break
            measure_number_texts.append(
                f'\n<text x="{mn_x:.1f}" y="{mn_y:.1f}" '
                f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                f'font-size="{mn_font_size}" font-weight="700" fill="#333333" '
                f'text-anchor="start" dy="0.35em">{mn_num}</text>'
            )
            measure_number_count += 1
        # Advance global measure counter by the number of LOGICAL measures in this system
        # 4 Ago 2026 (bug KS): MMRest systems count N logical measures, not 1
        if global_m_idx in _mmrest_map:
            global_m_idx += _mmrest_map[global_m_idx]
        elif (global_m_idx + 1) in _mmrest_map:
            global_m_idx += 1  # pre-MMRest system
        else:
            global_m_idx += len(em_bounds)
    
    if measure_number_texts:
        # Insert at END of SVG (before </svg>) so numbers are on TOP z-order,
        # above staff lines, circles, and everything else.
        close_svg = modified.rfind('</svg>')
        if close_svg >= 0:
            modified = modified[:close_svg] + ''.join(measure_number_texts) + modified[close_svg:]
        print(f"  Measure numbers: {measure_number_count} added as readable <text> above staff")
    
    # 2e. Enlarge clef
    # MuseScore renders clef with transform scale ~1.14; increase to match stretched staff
    clef_pat = r'<path class="Clef" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"'
    clef_count = 0
    def enlarge_clef(match):
        nonlocal clef_count
        a, b, c, d, tx, ty = [float(match.group(i)) for i in range(1, 7)]
        # Current scale = a (and d). Increase by factor 2.5 to match stretched staff
        orig_scale = a  # original scale (~1.143)
        clef_scale = 2.5
        new_a = a * clef_scale
        new_d = d * clef_scale
        # The G-clef glyph has its curl (ricciolino) on the 2nd line (G4).
        # In the original SVG, ty is positioned 2 staff spaces BELOW G4.
        # After Y-stretch, ty has been remapped so the offset is 2 * target_spacing = 560px.
        # But the glyph path data is NOT remapped (paths with curves are skipped).
        # So the glyph still has its original internal offset of 189px (2 * 94.5).
        # With the new scale (orig_scale * 2.5 = 2.857), the internal offset becomes
        # 189 * (2.857 / 1.143) = 189 * 2.5 = 472.5px below ty.
        # We need the curl to be at G4, which is 560px below ty (2 staff spaces × 280px).
        # So: ty + 472.5 should equal G4_y. Currently ty + 472.5 = ty + 472.5.
        # We need ty_new + 472.5 = G4_y = ty + 560 (since ty was 560px above... no)
        # Actually: ty is already remapped (560px below G4... no, 560px is the offset).
        # ty_remapped = G4 + 560 (ty is 560px below G4? No, ty is ABOVE G4 in the glyph).
        # Let's think again: in the original, ty = G4 + 189 (189px below G4).
        # After Y-stretch: ty_remapped = G4_proc + 560 (560px below G4_proc).
        # The glyph internal offset (from ty to curl) = 189px in glyph coords.
        # With scale 2.857: visual offset = 189 * 2.857 / 1.143 = 472.5px.
        # But wait — the glyph path coords are in the SAME space as the transform.
        # The path data has coordinates that get scaled by the matrix (a,d).
        # So the curl is at path_y * d + ty (matrix transform).
        # Original: curl_y = path_curl * orig_scale + ty_orig = G4_orig.
        # After Y-stretch (ty remapped, path NOT remapped):
        #   curl_y = path_curl * orig_scale + ty_remapped ≠ G4_proc (path not remapped!)
        # The path data was NOT remapped (curves skipped), so curl_y is wrong.
        # We need: path_curl * new_scale + new_ty = G4_proc
        # => new_ty = G4_proc - path_curl * new_scale
        # And: path_curl = (G4_orig - ty_orig) / orig_scale = -189 / 1.143 = -165.4
        # (negative because curl is ABOVE ty in the glyph)
        # Wait: ty_orig = G4_orig + 189, so G4_orig = ty_orig - 189.
        # path_curl * orig_scale + ty_orig = G4_orig = ty_orig - 189
        # path_curl = -189 / orig_scale = -189 / 1.143 = -165.4
        # new_ty = G4_proc - path_curl * new_scale = G4_proc - (-165.4) * 2.857
        #        = G4_proc + 472.5
        # But G4_proc = ty_remapped - 560 (ty is 560px below G4)
        # new_ty = (ty_remapped - 560) + 472.5 = ty_remapped - 87.5
        # Find the system for this clef (use POST-stretch systems)
        for sk, info in systems_post.items():
            staff_mid = (info['top'] + info['bottom']) / 2
            staff_h = info['bottom'] - info['top']
            line_spacing = staff_h / 4  # 280px for 5-line staff
            if info['top'] - staff_h * 1.5 <= ty <= info['bottom'] + staff_h * 1.5:
                # G4 = 2nd line from bottom = info['top'] + 1 * line_spacing
                g4_y = info['top'] + line_spacing
                # The glyph internal curl offset (in glyph coords, before scale):
                # curl is 2 staff spaces above ty in original = 2 * 94.5 = 189px
                # In glyph coords: 189 / orig_scale
                curl_glyph_offset = 189.0 / orig_scale  # negative = above ty
                # new_ty: curl_y = curl_glyph_offset * new_scale + new_ty = g4_y
                # But curl is ABOVE ty, so: g4_y = ty + curl_glyph_offset * new_scale
                # => new_ty = g4_y - curl_glyph_offset * new_scale
                # curl_glyph_offset is negative (above), so -(-165.4) * 2.857 = +472.5
                # Wait, curl is 189px BELOW ty in original (ty = G4 + 189, so G4 = ty - 189).
                # G4 is ABOVE ty. So curl is 189px above ty.
                # In glyph coords: -189 / orig_scale = -165.4
                # new curl_y = -165.4 * (orig_scale * 2.5) + new_ty
                # = -189 * 2.5 + new_ty = -472.5 + new_ty
                # This should equal g4_y:
                # new_ty = g4_y + 472.5
                # But ty_remapped = g4_y + 560 (ty is 560px below G4 after stretch)
                # So: new_ty = g4_y + 472.5 = (ty - 560) + 472.5 = ty - 87.5
                new_ty = ty - (560 - 472.5) + 70  # = ty - 87.5 + 70 = ty - 17.5
                # shift clef DOWN by 70px so the curl wraps the
                # 2nd line (G4) without touching the 1st line (E4) or 3rd line (B4).
                # +280 too low (touched E4), +140 still touched E4, +70 = sweet spot.
                # Uniform tx: the first system has tx=1597 (extra space for time sig),
                # but we move KeySig/TimeSig separately, so set all clefs to tx=1256
                uniform_tx = 1256.02
                clef_count += 1
                return f'<path class="Clef" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{uniform_tx:.2f},{new_ty:.2f})"'
        return match.group(0)
    modified = re.sub(clef_pat, enlarge_clef, modified)
    if clef_count > 0:
        print(f"  Clef: {clef_count} enlarged (scale ×2.5)")
    
    # 2e2. Enlarge and reposition KeySig (armatura di chiave) and TimeSig (4/4)
    # These must be moved RIGHT of the enlarged clef and scaled up to match.
    # Layout: clef (tx=1256, width~571) → KeySig (tx=1907) → TimeSig (tx=2101, S0 only) → music (2600)
    keysig_scale = 2.0  # scale factor for KeySig/TimeSig (slightly less than clef's 2.5)
    
    # KeySig: enlarge scale + shift tx to right of clef
    # diesis devono essere staggered (Do# a destra di Fa#), non sovrapposti
    keysig_count = 0
    # Track KeySig per system by Y band: group by nearest staff top
    # First, extract staff line Y positions to identify systems
    staff_ys = [float(y) for y in re.findall(
        r'<polyline class="StaffLines"[^>]*points="[\d.\-]+,([\d.\-]+)', modified)]
    # System tops = every 5th staff line (5 lines per system)
    system_tops = sorted(set(staff_ys[i] for i in range(0, len(staff_ys), 5)))
    
    # in modalità rhythm, niente pentagramma → niente simboli
    # di armatura o tempo. Scrivi alterazioni E tempo a LETTERE all'inizio di
    # ogni sistema. es. Re maggiore (2#) 4/4 → "Fa# Do#  4/4"; Fa maggiore → "Sib  4/4"
    if rhythm_mode and note_info:
        ks_steps = note_info.get('keysig_altered_steps', set())
        ks_alt = note_info.get('keysig_alteration', '')
        ts_info_r = note_info.get('time_sig', (4, 4))
        ts_text_r = f"{ts_info_r[0]}/{ts_info_r[1]}"  # es. "4/4", "6/8"
        # FIX #7 : per-measure time signatures in rhythm mode.
        # time_sigs_per_measure = {measure_idx: (num, den)}.
        # For each system, determine the time signature of its first measure
        # and show it if it differs from the previous system.
        time_sigs_pm_r = note_info.get('time_sigs_per_measure', {})
        # Build mapping: system_top Y (STRETCHED) → global measure index.
        # system_tops are stretched Y values from the modified SVG.
        # _sys_to_global_idx has keys = x_start (system key), values = global_idx.
        # systems[x_start]['top'] = ORIGINAL Y. We map by ORDER: the i-th system
        # (sorted by original Y) corresponds to the i-th system_top (stretched Y).
        sorted_x_starts = sorted(systems.keys(), key=lambda k: systems[k]['top'])
        sys_top_to_global = {}
        for i, x_start_key in enumerate(sorted_x_starts):
            if i < len(system_tops):
                sys_top_to_global[round(system_tops[i])] = _sys_to_global_idx.get(x_start_key, 0)
        # Sort system tops by Y
        sorted_sys_tops = sorted(sys_top_to_global.keys())
        # For each system, compute the time signature text.
        # Show TS on first system of each page AND when it changes.
        sys_top_to_ts_text = {}
        prev_ts = None
        for st in sorted_sys_tops:
            gm = sys_top_to_global[st]
            ts_m = time_sigs_pm_r.get(gm, ts_info_r)
            ts_t = f"{ts_m[0]}/{ts_m[1]}"
            # Show TS text if it differs from the previous system
            if ts_t != prev_ts:
                sys_top_to_ts_text[st] = ts_t
                prev_ts = ts_t
            else:
                sys_top_to_ts_text[st] = None  # don't show (same as previous)
        # Mappa step → nome italiano (abbreviato come nei pallini)
        STEP_IT = {'C': 'Do', 'D': 'Re', 'E': 'Mi', 'F': 'Fa', 'G': 'Sol', 'A': 'La', 'B': 'Si'}
        # Costruisci la stringa delle alterazioni in ordine standard
        SHARP_ORDER = ['F', 'C', 'G', 'D', 'A', 'E', 'B']
        FLAT_ORDER = ['B', 'E', 'A', 'D', 'G', 'C', 'F']
        if ks_alt == '#':
            order = [s for s in SHARP_ORDER if s in ks_steps]
        elif ks_alt == 'b':
            order = [s for s in FLAT_ORDER if s in ks_steps]
        else:
            order = []
        keysig_labels = []
        for s in order:
            it_name = STEP_IT.get(s, s)
            # Per Sol usa abbreviazione "So" come nei pallini
            if s == 'G':
                it_name = 'So'
            keysig_labels.append(f"{it_name}{ks_alt}")
        keysig_text = ' '.join(keysig_labels)  # es. "Fa# Do#"
        # Combina armatura + tempo (separati da spazi)
        if keysig_text:
            combined_text = f"{keysig_text}    {ts_text_r}"
        else:
            combined_text = ts_text_r  # Do maggiore: solo tempo
        
        # Trova la Y del primo sistema (middle line) per posizionare il testo
        # Trova le middle line Y di tutti i sistemi
        # Le middle line Y sono a system_tops[0] + 2*spatium (3ª linea su 5)
        # Ma meglio: usa le Y dei KeySig originali come riferimento
        keysig_y_tracker_rhythm = {}  # system Y -> count (per tracking)
        # 7 Ago: il primo sistema è quello con Y più bassa (più in alto).
        # Il tempo va mostrato SOLO sul primo sistema.
        first_sys_top = min(system_tops) if system_tops else None
        
        def replace_keysig_rhythm(match):
            nonlocal keysig_count
            a, b, c, d, tx, ty = [float(match.group(i)) for i in range(1, 7)]
            keysig_count += 1
            # FIX: KeySig is drawn ABOVE the staff, so its Y is SMALLER than the
            # system's top Y. Find the nearest system_top by Y proximity.
            if system_tops:
                sys_top = min(system_tops, key=lambda st: abs(st - ty))
            else:
                sys_top = None
            sys_key = round(sys_top) if sys_top is not None else round(ty)
            # Solo il PRIMO KeySig di ogni sistema genera il testo
            if sys_key not in keysig_y_tracker_rhythm:
                keysig_y_tracker_rhythm[sys_key] = 1
                # FIX #7 : show time signature when it changes.
                # First system: armatura + tempo. Subsequent systems: tempo only
                # if it differs from the previous system.
                is_first_system = (first_sys_top is not None and abs(sys_top - first_sys_top) < 50)
                if is_first_system and is_first_page:
                    # Primo pentagramma assoluto: armatura + tempo
                    display_text = combined_text  # es. "Fa#    4/4"
                else:
                    # Sistemi successivi: mostra solo il tempo se è cambiato
                    # Find nearest system top in sys_top_to_ts_text by Y proximity
                    nearest_top = min(sys_top_to_ts_text.keys(),
                                      key=lambda st: abs(st - sys_top)) if sys_top_to_ts_text else None
                    ts_for_sys = sys_top_to_ts_text.get(nearest_top) if nearest_top is not None else None
                    if ts_for_sys:
                        display_text = f"    {ts_for_sys}"  # solo tempo, niente armatura
                    else:
                        display_text = ''
                # Se non c'è armatura e non è il primo sistema, non mostrare nulla
                if not display_text:
                    return ''
                # Posiziona il testo a sinistra del pentagramma (dove sarebbe la chiave)
                font_sz = 180
                return (f'<text x="1270" y="{ty:.1f}" '
                        f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                        f'font-size="{font_sz}" font-weight="900" fill="#111111" '
                        f'text-anchor="start" dy="0.35em">{display_text}</text>')
            # Nascondi i KeySig successivi (duplicati nello stesso sistema)
            return ''
        
        modified = re.sub(
            r'<path class="KeySig" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"',
            replace_keysig_rhythm, modified
        )
        if keysig_count > 0:
            print(f"  [rhythm] KeySig+TimeSig: {keysig_count} simboli → testo \"{combined_text}\"")
        else:
            # Nessun KeySig nel SVG (es. Do maggiore/la minore, 0 alterazioni).
            # Aggiungi manualmente il testo del tempo ad ogni sistema.
            # Usa le Y dei TimeSig originali come riferimento, o system_tops.
            ts_ys = [float(m.group(1)) for m in re.finditer(
                r'<path class="TimeSig" transform="matrix\((?:[\d.\-]+,){5}([\d.\-]+)\)"',
                modified)]
            # Se non ci sono TimeSig, usa le middle-line Y dei sistemi
            if not ts_ys and system_tops:
                # middle line ≈ system_top + 2 * spatium (spatium ≈ (bottom-top)/4)
                ts_ys = []
                for st in system_tops:
                    # Trova il bottom del sistema (5 linee dopo)
                    idx = system_tops.index(st)
                    if idx + 1 < len(system_tops):
                        next_top = system_tops[idx + 1]
                    else:
                        next_top = st + 2000  # fallback
                    middle_y = st + (next_top - st) * 0.15  # approssimazione
                    ts_ys.append(middle_y)
            
            ts_font_sz = 180
            ts_text_elements = []
            seen_ys = set()
            no_ks_sys_idx = 0
            for ty in ts_ys:
                sys_top = min(system_tops, key=lambda st: abs(st - ty)) if system_tops else None
                sys_key = round(sys_top) if sys_top is not None else round(ty)
                if sys_key in seen_ys:
                    continue
                seen_ys.add(sys_key)
                no_ks_sys_idx += 1
                # tempo solo sul primo pentagramma ASSOLUTO
                # (prima pagina, primo sistema). Pagine successive: niente.
                if no_ks_sys_idx > 1 or not is_first_page:
                    continue  # sistemi successivi o pagine successive: nessun testo
                ts_text_elements.append(
                    f'<text x="1270" y="{ty:.1f}" '
                    f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                    f'font-size="{ts_font_sz}" font-weight="900" fill="#111111" '
                    f'text-anchor="start" dy="0.35em">{combined_text}</text>'
                )
            if ts_text_elements:
                modified = modified.replace('</svg>', '\n'.join(ts_text_elements) + '\n</svg>')
                print(f"  [rhythm] TimeSig (no KeySig): 1° sistema → testo \"{combined_text}\"")
        
        # Intra-system time signature changes.
        # For each system, check if the time signature changes between measures
        # within the same system. If so, show the new TS text at the X position
        # where the change occurs.
        intra_ts_elements = []
        for st in sorted_sys_tops:
            gm_start = sys_top_to_global.get(st, 0)
            # Get measure boundaries for this system from equalized_measures
            # Find x_start key for this system
            _nearest_xk = None
            for xk, si in systems.items():
                if round(si.get('top', 0)) == round(st) or abs(si.get('top', 0) - st) < 100:
                    _nearest_xk = xk
                    break
            if _nearest_xk is None:
                # Try by index
                _sorted_xk = sorted(systems.keys(), key=lambda k: systems[k]['top'])
                _si_idx = sorted_sys_tops.index(st)
                if _si_idx < len(_sorted_xk):
                    _nearest_xk = _sorted_xk[_si_idx]
            em_bounds = equalized_measures.get(_nearest_xk, [])
            if not em_bounds:
                continue
            prev_ts_intra = None
            for m_i, (m_start, m_end) in enumerate(em_bounds):
                gm = gm_start + m_i
                ts_m = time_sigs_pm_r.get(gm, ts_info_r)
                ts_t = f"{ts_m[0]}/{ts_m[1]}"
                if prev_ts_intra is not None and ts_t != prev_ts_intra:
                    # Time signature changed within this system!
                    # Show the new TS at the start of this measure.
                    font_sz_intra = 140
                    intra_ts_elements.append(
                        f'<text x="{m_start:.1f}" y="{st + 50:.1f}" '
                        f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                        f'font-size="{font_sz_intra}" font-weight="900" fill="#111111" '
                        f'text-anchor="start" dy="0.35em">{ts_t}</text>'
                    )
                prev_ts_intra = ts_t
        if intra_ts_elements:
            modified = modified.replace('</svg>', '\n'.join(intra_ts_elements) + '\n</svg>')
            print(f"  [rhythm] Intra-system TS changes: {len(intra_ts_elements)}")
    else:
        # Modalità normale: ingrandisci e riposiziona i simboli KeySig
        keysig_y_tracker = {}  # rounded system Y -> count of keysigs seen
        
        def enlarge_keysig(match):
            nonlocal keysig_count
            a, b, c, d, tx, ty = [float(match.group(i)) for i in range(1, 7)]
            # Scale up
            new_a = a * keysig_scale
            new_d = d * keysig_scale
            
            # Find which system this KeySig belongs to (nearest system top)
            sys_top = min(system_tops, key=lambda st: abs(st - ty)) if system_tops else None
            
            # Stagger: first KeySig (Fa#, higher Y = smaller ty) at base, second (Do#, lower) shifted right
            stagger_offset = 90.0 * keysig_scale  # ~180px between diesis
            if sys_top is not None:
                sys_key = round(sys_top)
                if sys_key not in keysig_y_tracker:
                    keysig_y_tracker[sys_key] = 0
                idx = keysig_y_tracker[sys_key]
                keysig_y_tracker[sys_key] += 1
                new_tx = 1907.0 + idx * stagger_offset
            else:
                new_tx = 1907.0
            
            keysig_count += 1
            return f'<path class="KeySig" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{ty:.2f})"'
        
        modified = re.sub(
            r'<path class="KeySig" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"',
            enlarge_keysig, modified
        )
        if keysig_count > 0:
            print(f"  KeySig: {keysig_count} enlarged (scale ×{keysig_scale}) and repositioned")
    
    # TimeSig: enlarge scale + shift tx to right of KeySig
    # in modalità rhythm, il tempo è già incluso nel testo
    # combinato KeySig+TimeSig (es. "Fa#    4/4"). Rimuovi i simboli TimeSig.
    timesig_count = 0
    
    if rhythm_mode:
        # Rimuovi i simboli TimeSig (già sostituiti da testo nel blocco KeySig)
        acc_ts_pat = re.compile(r'<path class="TimeSig"[^>]*/>')
        timesig_count = len(acc_ts_pat.findall(modified))
        modified = acc_ts_pat.sub('', modified)
        if timesig_count > 0:
            print(f"  [rhythm] TimeSig: {timesig_count} simboli rimossi (tempo già nel testo armatura)")
    else:
        def enlarge_timesig(match):
            nonlocal timesig_count
            a, b, c, d, tx, ty = [float(match.group(i)) for i in range(1, 7)]
            # FIX #2 : remove courtesy time signatures.
            # Courtesy TimeSigs appear at the END of a system (high X) to preview
            # the next system's meter. The real TimeSig is at the START (low X).
            # Threshold: tx > 3000 = courtesy (real TimeSig is near KeySig at ~1400-1900).
            if tx > 3000:
                return ''  # remove courtesy TimeSig
            new_a = a * keysig_scale
            new_d = d * keysig_scale
            # Default: right of KeySig (KeySig at 1907, width ~114px, ends ~2021)
            new_tx = 2101.0
            # if the time signature change is at the 2nd
            # measure of a 2-measure system (not the 1st), reposition the TimeSig
            # to the X of the 2nd measure instead of the system start.
            # Find which system this TimeSig belongs to:
            if system_tops and equalized_measures and _sys_to_global_idx:
                nearest_top = min(system_tops, key=lambda st: abs(st - ty))
                # Find the equalized_measures key for this system
                em_key = None
                for xk in sorted(equalized_measures.keys(), key=lambda k: float(k)):
                    if abs(float(xk) - nearest_top) < 500:
                        em_key = xk
                        break
                if em_key is not None:
                    em_bounds = equalized_measures.get(em_key, [])
                    if len(em_bounds) >= 2:
                        # 2+ measures in this system. Check if the TS change
                        # is at the 2nd measure (not the 1st).
                        global_idx = _sys_to_global_idx.get(em_key, 0)
                        m0_ts = time_sigs_per_measure.get(global_idx)
                        m1_ts = time_sigs_per_measure.get(global_idx + 1)
                        if m0_ts and m1_ts and m0_ts != m1_ts:
                            # TS changes at the 2nd measure!
                            # Reposition to the X of the 2nd measure.
                            new_tx = em_bounds[1][0]  # start of 2nd measure
            timesig_count += 1
            return f'<path class="TimeSig" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{ty:.2f})"'
        
        modified = re.sub(
            r'<path class="TimeSig" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"',
            enlarge_timesig, modified
        )
        if timesig_count > 0:
            print(f"  TimeSig: {timesig_count} enlarged (scale ×{keysig_scale}) and repositioned")

        # add text time signatures for intra-system meter changes
        # that MuseScore doesn't render as glyphs (when the change is at the 2nd
        # measure of a system, MuseScore only puts a courtesy TS at the end of
        # the previous system, not at the actual change point).
        # We add a text "N/D" at the X of the 2nd measure.
        added_ts_texts = []
        if system_tops and equalized_measures and _sys_to_global_idx and time_sigs_per_measure:
            # Map em_key (original pre-stretch Y) to system_tops (post-stretch Y) by order
            em_keys_sorted = sorted(equalized_measures.keys(), key=lambda k: float(k))
            for i, em_key in enumerate(em_keys_sorted):
                if i >= len(system_tops):
                    break
                em_bounds = equalized_measures.get(em_key, [])
                if len(em_bounds) < 2:
                    continue  # only 1 measure, no intra-system change possible
                global_idx = _sys_to_global_idx.get(em_key, 0)
                m0_ts = time_sigs_per_measure.get(global_idx)
                m1_ts = time_sigs_per_measure.get(global_idx + 1)
                if m0_ts and m1_ts and m0_ts != m1_ts:
                    # TS changes at the 2nd measure of this system.
                    m1_x = em_bounds[1][0]  # start of 2nd measure
                    # Use post-stretch system top for Y
                    sys_top_y = system_tops[i]
                    # Middle line = top + 2 * line_spacing (line_spacing ~280px post-stretch)
                    # Use the actual line spacing from staff lines if available
                    line_spacing = 280  # default post-stretch
                    ts_y = sys_top_y + 2 * line_spacing  # 3rd staff line (middle)
                    ts_text = f"{m1_ts[0]}/{m1_ts[1]}"
                    added_ts_texts.append(
                        f'<text x="{m1_x - 30:.1f}" y="{ts_y:.1f}" '
                        f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
                        f'font-size="140" font-weight="900" fill="#111111" '
                        f'text-anchor="start" dy="0.35em">{ts_text}</text>'
                    )
        if added_ts_texts:
            modified = modified.replace('</svg>', '\n'.join(added_ts_texts) + '\n</svg>')
            print(f"  TimeSig: {len(added_ts_texts)} text TS added for intra-system changes")
    
    # 2e3. Rimuovi TUTTI gli accidentals (bequadri/diesis/bemolli) dal pentagramma
    # Gli accidentals sono confusione visiva per l'allievo dislessico. La tavola
    # sonora colorata mostra già il nome corretto della nota. Le alterazioni
    # sono comunque preservate nel pitch (MIDI/suono).
    acc_pat = re.compile(r'<path class="Accidental"[^>]*/>')
    acc_removed = len(acc_pat.findall(modified))
    modified = acc_pat.sub('', modified)
    if acc_removed > 0:
        print(f"  Accidentals: {acc_removed} rimossi dal pentagramma (confusione visiva per l'allievo)")

    # 2e3b. Disegna alterazioni (#/b/natural) sul pentagramma DOPO il Y-stretch.
    # Le alterazioni vengono posizionate usando le coordinate POST-stretch dei
    # cerchi (che sono già stati aggiornati da y_stretch_systems).
    # Regola (Marco, 9 Ago 2026): SOPRA il pallino se gambo verso il basso
    # (stem down), SOTTO il pallino se gambo verso l'alto (stem up).
    # Nei blocchi grigi (tavola) resta SOTTO il nome, sempre (gestito altrove).
    # Step2 (9 Ago, nuova regola Marco): NON disegnare alterazioni sul pentagramma
    # in modalità rhythm — le alterazioni restano solo sotto le note dei blocchi.
    staff_acc_svgs = []
    disc_r_for_acc = DISC_R_OVERRIDE if DISC_R_OVERRIDE else 130
    acc_fs_staff = 206  # same as tavola
    # Prepara la mappatura sistema pre→post per identificare il sistema di
    # ogni nota. I sistemi pre e post hanno lo stesso ORDINE (ordinati per top),
    # quindi li abbiniamo per indice.
    sys_pre_sorted = sorted(systems.items(), key=lambda x: x[1]['top'])
    sys_post_sorted = sorted(systems_post.items(), key=lambda x: x[1]['top'])
    # Mappa: system_key (pre) → sistema post (per indice)
    pre_to_post = {}
    for i, (sk_pre, _info_pre) in enumerate(sys_pre_sorted):
        if i < len(sys_post_sorted):
            pre_to_post[sk_pre] = sys_post_sorted[i][1]
    # Pre-compila la lista dei cerchi con (cx, cy, r) per evitare re.finditer
    # ripetuto per ogni nota.
    all_circles = []
    for cm in re.finditer(r'<circle\s+cx="([\d.]+)"\s+cy="([\d.]+)"\s+r="([\d.]+)"', modified):
        all_circles.append((float(cm.group(1)), float(cm.group(2)), float(cm.group(3))))
    for n in notes:
        p_acc = n.get('staff_acc_to_draw', '')
        if not p_acc or p_acc not in ('#', 'b', 'natural'):
            continue
        # Trova il cerchio corrispondente a questa nota nel SVG post-stretch.
        # Il cerchio ha cx = center_x della nota. Cerchiamo il circle più vicino
        # in X, ma nello STESSO sistema della nota (per evitare di prendere
        # cerchi in altri sistemi con la stessa cx).
        cx_note = n.get('center_x', n.get('x', 0))
        # Identifica il sistema post-stretch di questa nota
        sys_key = n.get('system_key', '')
        post_sys = pre_to_post.get(sys_key, None)
        best_cy = None
        best_dist = 99999
        for cx_c, cy_c, r_c in all_circles:
            dist_x = abs(cx_c - cx_note)
            if dist_x > 50:
                continue
            # Se conosco il sistema post, verifico che il cerchio sia in quel sistema
            if post_sys is not None:
                # Usa il range del sistema post-stretch (con un margine per
                # note sopra/sotto il pentagramma)
                sys_top = post_sys['top'] - 500
                sys_bot = post_sys['bottom'] + 500
                if not (sys_top <= cy_c <= sys_bot):
                    continue
            # Tra i cerchi candidati, prendi quello con cx più vicino
            if dist_x < best_dist:
                best_dist = dist_x
                best_cy = cy_c
        if best_cy is None:
            continue
        acc_symbol = '#' if p_acc == '#' else ('\u266d' if p_acc == 'b' else '\u266e')
        stem_dir = n.get('stem_dir', None)
        # In modalità rhythm (Step2), tutti i gambi vengono ridisegnati verso
        # l'ALTO (middle_line_y - STEM_LEN), indipendentemente dalla direzione
        # originale. Quindi in rhythm mode, l'alterazione va sempre SOTTO.
        if rhythm_mode:
            stem_dir = 'up'
        acc_x = cx_note
        # Offset verticale del diesis dal pallino. Deve essere PICCOLO per
        # evitare che il diesis di una nota finisca sopra la nota soprastante
        # (es. Sol# sopra il pentagramma con La subito sopra: offset troppo
        # grande mette il diesis sopra il La invece che sopra il Sol).
        # 9 Ago: ridotto a disc_r + 30px (appena sopra il pallino).
        acc_offset = disc_r_for_acc + 30
        # Controlla se c'è un'altra nota subito sopra (entro 250px in x e
        # 300px in y). Se sì, usa un offset minore per stare vicino al Sol
        # senza sovrapporsi troppo al La.
        has_note_above = any(
            abs(c[0] - cx_note) < 250 and c[1] < best_cy - disc_r_for_acc and c[1] > best_cy - 300
            for c in all_circles
            if c[1] != best_cy
        )
        # Font-size del diesis: 206 normale, 140 se c'è una nota sopra
        # (più piccolo per stare nello spazio ridotto).
        acc_fs_actual = 140 if has_note_above and stem_dir != 'up' else acc_fs_staff
        if stem_dir == 'up':
            # Gambo verso l'alto → alterazione SOTTO il pallino
            acc_y = best_cy + acc_offset
            acc_x = cx_note
        elif has_note_above:
            # Nota sopra → diesis SOPRA ma con offset minore e font più piccolo
            acc_y = best_cy - disc_r_for_acc - 15
            acc_x = cx_note
        else:
            # Gambo verso il basso (o whole note senza gambo) → alterazione SOPRA
            acc_y = best_cy - acc_offset
            acc_x = cx_note
        # Font-size del diesis: 206 normale, 140 se c'è una nota sopra
        # (più piccolo per stare nello spazio ridotto).
        staff_acc_svgs.append(
            f'<text x="{acc_x:.1f}" y="{acc_y:.1f}" '
            f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
            f'font-size="{acc_fs_actual:.0f}" font-weight="900" '
            f'fill="#111111" text-anchor="middle" dy="0.35em">'
            f'{acc_symbol}</text>'
        )
    if staff_acc_svgs and not rhythm_mode:
        modified = modified.replace('</svg>', '\n' + '\n'.join(staff_acc_svgs) + '\n</svg>')
        print(f"  Alterazioni pentagramma: {len(staff_acc_svgs)} disegnate (post-stretch, regola gambo)")
    elif rhythm_mode and staff_acc_svgs:
        print(f"  Alterazioni pentagramma: {len(staff_acc_svgs)} skip (modalità rhythm — solo sui blocchi)")
    
    # 2f. Enlarge rests to match stretched staff
    # Rests have transform="matrix(a,0,0,d,tx,ty)" with scale ~1.143.
    # After Y-stretch, notes have 260px diameter circles — rests must scale up too.
    rest_scale_factor = 2.963  # standard Y-stretch factor (280/94.5)
    # Pause di croma (eighth rest) rimpicciolite nel pentagramma
    
    rest_count = 0
    rest_eighth_count = 0
    # Pause di croma/semicroma nel pentagramma: scale ridotto (più piccole delle note)
    # pausa croma più piccola → 0.65→0.50
    REST_EIGHTH_SCALE = 0.50  # 50% dello scale delle note → pause croma più piccole
    # semicrome ancora più piccole (~metà delle crome)
    REST_SIXTEENTH_SCALE = 0.33  # 33% dello scale delle note → pause semicroma molto piccole
    # pause di semiminima troppo grandi → ridotte del 30%
    # Ma devono stare dentro la sezione grigia (392px). Con 0.70 sono 415px (overflow).
    # Quindi 0.62 (-38%) per stare dentro con margine.
    REST_QUARTER_SCALE = 0.62  # 62% dello scale delle note → pause semiminima dentro settore grigio
    
    def enlarge_rest(match):
        nonlocal rest_count, rest_eighth_count
        a = float(match.group(1))
        b = match.group(2)
        c = match.group(3)
        d = float(match.group(4))
        tx = float(match.group(5))
        ty = float(match.group(6))
        path_d = match.group(7) if match.lastindex >= 7 else ''
        
        # Identifica il tipo di pausa dal path d:
        # Eighth rest: d starts with "M88" → rimpicciolisci (0.65)
        # 16th rest: d starts with "M0," or "M113" → rimpicciolisci ancora di più (0.33)
        # Quarter rest: d starts with "M76" → scale normale
        # semicroma ~metà della croma, molto più piccola
        is_eighth_rest = path_d.startswith('M88')
        is_sixteenth_rest = path_d.startswith('M0,') or path_d.startswith('M113')
        
        if is_sixteenth_rest:
            effective_scale = rest_scale_factor * REST_SIXTEENTH_SCALE
            rest_eighth_count += 1
        elif is_eighth_rest:
            effective_scale = rest_scale_factor * REST_EIGHTH_SCALE
            rest_eighth_count += 1
        else:
            # pause di semiminima ridotte del 30%
            effective_scale = rest_scale_factor * REST_QUARTER_SCALE
        
        new_a = a * effective_scale
        new_d = d * effective_scale
        
        # Adjust ty: the rest glyph grows from (tx,ty).
        # Quarter rest path: y from -67 to +108, center ≈ +20 (slightly below ty)
        # Eighth rest path: y from -132 to +63, center ≈ -34 (above ty)
        # Use rough average center offset to keep the rest visually centered
        # at the same staff position after scaling.
        # new center = ty + center_offset * new_d
        # old center = ty + center_offset * d
        # To keep center fixed: new_ty = ty - center_offset * (new_d - d)
        # Using center_offset ≈ 0 (rests are roughly centered at ty in MuseScore)
        # Actually, MuseScore positions rests with ty at the vertical center of the
        # staff line they sit on. The glyph extends both above and below ty.
        # With uniform scaling from (tx,ty), the center stays at ty.
        # So: new_ty = ty (no adjustment needed for rests — they scale symmetrically)
        # BUT: the glyph is NOT symmetric (quarter rest: -67 to +108, center at +20).
        # So the visual center shifts down by 20 * (new_d - d) ≈ 20 * (3.386 - 1.143) ≈ 45px.
        # Compensate: new_ty = ty - center_offset * (new_d - d)
        # quarter rest con REST_QUARTER_SCALE=0.62 → offset 45 (era 20)
        # per centrare il rest nel settore grigio (prima overflow bottom 12-24px).
        if not is_eighth_rest and not is_sixteenth_rest:
            glyph_center_offset = 45.0  # quarter rest: need more compensation to center in grey sector
        else:
            glyph_center_offset = 20.0  # eighth/sixteenth rest: original compensation
        new_ty = ty - glyph_center_offset * (new_d - d)
        
        rest_count += 1
        # Preserve the d= attribute if present
        if path_d:
            return f'<path class="Rest" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{tx:.2f},{new_ty:.2f})" d="{path_d}"'
        else:
            return f'<path class="Rest" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{tx:.2f},{new_ty:.2f})"'
    
    modified = re.sub(
        r'<path class="Rest" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"(?:\s+d="([^"]*)")?',
        enlarge_rest, modified
    )
    if rest_count > 0:
        print(f"  Rests: {rest_count} enlarged (scale ×{rest_scale_factor:.2f}, eighth rests ×{rest_scale_factor*REST_EIGHTH_SCALE:.2f})")
    
    # 2f2. Enlarge NoteDots (augmentation dots for dotted notes AND rests)
    # MuseScore renders dots as <path class="NoteDot"> with small scale.
    # Enlarge to match the stretched staff AND color them to match the note.
    notedot_count = 0
    
    # Build a list of note circle positions+colors for matching dots to notes
    note_circle_data = []
    for nc_match in re.finditer(r'<circle\s+cx="([\d.]+)"\s+cy="([\d.]+)"\s+r="([\d.]+)"\s+fill="([^"]+)"([^>]*)>', modified):
        nc_cx = float(nc_match.group(1))
        nc_cy = float(nc_match.group(2))
        nc_r = float(nc_match.group(3))
        nc_fill = nc_match.group(4)
        nc_attrs = nc_match.group(5)
        # For open noteheads (half/whole), fill="white" but stroke has the real color.
        # Use stroke color for dot coloring so dots match the note's visible color.
        if nc_fill == 'white':
            stroke_m = re.search(r'stroke="([^"]+)"', nc_attrs)
            if stroke_m:
                nc_fill = stroke_m.group(1)
        # extract data-dots attribute for correct dot→note matching
        dots_m = re.search(r'data-dots="(\d+)"', nc_attrs)
        nc_dots = int(dots_m.group(1)) if dots_m else 0
        if nc_r > 50:  # note circles only
            note_circle_data.append((nc_cx, nc_cy, nc_r, nc_fill, nc_dots))
    
    # Build a list of rest positions for matching dots to rests
    rest_pos_data = []
    for rp_match in re.finditer(r'<path class="Rest"[^>]*transform="matrix\([^,]+,[^,]+,[^,]+,[^,]+,([\d.\-]+),([\d.\-]+)\)"', modified):
        rp_tx = float(rp_match.group(1))
        rp_ty = float(rp_match.group(2))
        rest_pos_data.append((rp_tx, rp_ty))
    
    # NOTE DOT — matching corretto usando data-dots.
    # ROOT CAUSE: MuseScore posiziona i NoteDot a ~156px dalla nota nel SVG originale.
    # Dopo l'equalizzazione, il dot rimane vicino alla posizione originale e il matching
    # per distanza X lo assegna alla nota sbagliata. FIX: matchare i dot delle NOTE solo
    # alle note con data-dots="1" (info autoritativa dall'estrazione .mscz).
    
    dotted_notes = [(cx, cy, r, fill) for cx, cy, r, fill, dots in note_circle_data if dots > 0]
    used_dotted_notes = set()
    
    def enlarge_notedot(match):
        nonlocal notedot_count
        a = float(match.group(1))
        b = match.group(2)
        c = match.group(3)
        d = float(match.group(4))
        tx = float(match.group(5))
        ty = float(match.group(6))
        path_d = match.group(7) if match.lastindex >= 7 else ''
        
        new_a = a * rest_scale_factor
        new_d = d * rest_scale_factor
        new_tx = tx
        new_ty = ty
        dot_fill = '#000000'
        
        # prova prima a matchare con una nota dots>0
        if dotted_notes:
            best_dotted_dist = 99999
            best_dotted_idx = -1
            for di, (nc_cx, nc_cy, nc_r, nc_fill) in enumerate(dotted_notes):
                if di in used_dotted_notes:
                    continue
                if abs(nc_cy - ty) < 500:
                    dist = abs(nc_cx - tx)
                    if dist < best_dotted_dist:
                        best_dotted_dist = dist
                        best_dotted_idx = di
            
            if best_dotted_idx >= 0 and best_dotted_dist < 1500:
                nc_cx, nc_cy, nc_r, nc_fill = dotted_notes[best_dotted_idx]
                used_dotted_notes.add(best_dotted_idx)
                dot_fill = nc_fill
                new_ty = nc_cy
                new_tx = nc_cx + nc_r + 80
                notedot_count += 1
                if path_d:
                    return f'<path class="NoteDot" fill="{dot_fill}" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{new_ty:.2f})" d="{path_d}"'
                else:
                    return f'<path class="NoteDot" fill="{dot_fill}" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{new_ty:.2f})"'
        
        # Dot di pausa — logica originale
        nearest_rest_dist = 99999
        nearest_rest_tx = None
        nearest_rest_ty = None
        for rp_tx, rp_ty in rest_pos_data:
            if abs(rp_ty - ty) < 300:
                dist = abs(rp_tx - tx)
                if dist < nearest_rest_dist:
                    nearest_rest_dist = dist
                    nearest_rest_tx = rp_tx
                    nearest_rest_ty = rp_ty
        
        if nearest_rest_ty is not None:
            new_ty = nearest_rest_ty
        if nearest_rest_tx is not None:
            desired_tx = nearest_rest_tx + 280
            if tx < desired_tx:
                new_tx = desired_tx
        
        notedot_count += 1
        if path_d:
            return f'<path class="NoteDot" fill="{dot_fill}" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{new_ty:.2f})" d="{path_d}"'
        else:
            return f'<path class="NoteDot" fill="{dot_fill}" transform="matrix({new_a:.4f},{b},{c},{new_d:.4f},{new_tx:.2f},{new_ty:.2f})"'
    
    modified = re.sub(
        r'<path class="NoteDot" transform="matrix\(([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+),([\d.\-]+)\)"(?:\s+d="([^"]*)")?',
        enlarge_notedot, modified
    )
    if notedot_count > 0:
        print(f"  NoteDots: {notedot_count} enlarged (scale x{rest_scale_factor:.2f})")
        
    # 2g. Scale LedgerLine stroke-width to match stretched staff
    # Original: stroke-width=15.12 with staff spacing 94.5px (ratio 0.16)
    # Stretched: staff spacing 280px → stroke-width should be 280*0.16 = 44.8px
    # But 15.12 * 2.96 (scale) = 44.8. Use the scale factor.
    ledger_sw_count = 0
    def replace_ledger_line(match):
        nonlocal ledger_sw_count
        ledger_sw_count += 1
        elem = match.group(0)
        # Replace stroke-width value (15.12 → 30)
        elem = re.sub(r'(stroke-width=")[\d.]+(")', r'\g<1>30\g<2>', elem, count=1)
        return elem
    modified = re.sub(r'<polyline class="LedgerLine"[^>]*/>', replace_ledger_line, modified)
    if ledger_sw_count > 0:
        print(f"  Ledger lines: {ledger_sw_count} stroke-width scaled to 30px")
    
    # 2h. Remove MuseScore's built-in title/composer text paths ("Music21 Fragment" etc.)
    # These are rendered as <path class="Text" d="M..."> and overlap the staff.
    # We add our own custom title instead.
    modified = re.sub(r'<path class="Text"[^>]*/>', '', modified)
    
    # 2h2. Remove all ties and slurs (legature di valore e di fraseggio) —  2026
    # Ties/slurs are positioned incorrectly in the MaidaScore layout; remove them entirely.
    modified = re.sub(r'<path class="TieSegment"[^>]*/>', '', modified)
    modified = re.sub(r'<path class="SlurSegment"[^>]*/>', '', modified)
    
    # 2i. Add title and part name on first page
    # Small text at top of page, NOT overlapping the staff
    if is_first_page and (title_text or part_text):
        # Insert text elements right after the white background rect
        title_svg = ''
        if title_text:
            title_svg += f'<text x="4962" y="350" text-anchor="middle" font-family="Atkinson Hyperlegible" font-size="200" font-weight="700" fill="black">{title_text}</text>'
        if part_text:
            title_svg += f'<text x="4962" y="550" text-anchor="middle" font-family="Atkinson Hyperlegible" font-size="160" font-weight="500" fill="black">{part_text}</text>'
        # Insert after the first path (white background)
        modified = re.sub(r'(<path class="" fill="#ffffff"[^>]*/>)', r'\1' + title_svg, modified, count=1)
    
    # 2j. Draw Tavola Sonora row below each system
    # Celle colorate allineate ai confini delle battute del pentagramma MaidaScore.
    # Colore=altezza, larghezza=durata, bianco tratteggiato=pausa.
    print(f"  Disegno Tavola Sonora sotto ogni sistema...")
    modified = draw_tavola_sonora(modified, systems_post, equalized_measures,
                                  note_info, note_offset,
                                  tavola_row_height=TAVOLA_ROW_HEIGHT,
                                  tavola_gap=TAVOLA_GAP,
                                  processed_notes=notes,
                                  initial_rest_measures=initial_rest_measures,
                                  measure_offset=measure_offset,
                                  mmrest_groups=mmrest_groups)
    
    # MMRest manuale per TUTTE le sequenze di battute di pausa.
    # Ogni gruppo (start_measure, count) viene sostituito con un rettangolo
    # verticale nero + numero "N" (come nell'originale MuseScore).
    if mmrest_groups:
        # Costruisci una lista piatta di tutte le battute con i loro confini X,
        # mappando global_measure_idx → (m_start, m_end, sys_key, sys_info)
        # Scorrendo i sistemi in ordine di Y (top→bottom), le battute sono in ordine.
        all_meas_info = []  # list of (global_m_idx, m_start, m_end, sys_key, sys_info)
        sorted_sys_keys = sorted(systems_post.keys(), key=lambda k: systems_post[k]['top'])
        # matching per INDICE sistema (non per x_start, che è uguale per tutti)
        em_sorted_keys = sorted(equalized_measures.keys(),
                                key=lambda k: float(k.split('_')[1]))
        global_m_idx = measure_offset  # parte dal measure_offset della pagina
        _mmrest_map_mmr = dict(mmrest_groups)
        _mmrest_set_mmr = set(_mmrest_map_mmr.keys())
        for sys_i, sk in enumerate(sorted_sys_keys):
            si = systems_post[sk]
            # Match per indice: il sys_i-esimo sistema corrisponde al sys_i-esimo em_key
            measures = None
            if sys_i < len(em_sorted_keys):
                measures = equalized_measures[em_sorted_keys[sys_i]]
            if measures:
                # 4 Ago 2026 (bug KS): se questo sistema è un MMRest, avanza di N
                if global_m_idx in _mmrest_set_mmr:
                    all_meas_info.append((global_m_idx, measures[0][0], measures[0][1], sk, si))
                    global_m_idx += _mmrest_map_mmr[global_m_idx]
                else:
                    for m_start, m_end in measures:
                        all_meas_info.append((global_m_idx, m_start, m_end, sk, si))
                        global_m_idx += 1
        
        # Per ogni gruppo MMRest, trova le battute che appartengono a questa pagina
        for grp_start, grp_count in mmrest_groups:
            # l'MMRest è 1 battuta fisica nel SVG (non splittata).
            # grp_start = indice della battuta MMRest, grp_count = numero mostrato.
            # Cerca la singola battuta all'indice grp_start.
            grp_measures = []
            for gm_idx, m_start, m_end, sk, si in all_meas_info:
                if gm_idx == grp_start:  # SOLO la battuta MMRest (1 battuta)
                    grp_measures.append((gm_idx, m_start, m_end, sk, si))
                    break
            
            if not grp_measures:
                continue  # gruppo non in questa pagina
            
            # Verifica se TUTTE le battute del gruppo sono in questa pagina
            if len(grp_measures) < grp_count:
                print(f"  MMRest M{grp_start+1}-M{grp_start+grp_count} (parziale: {len(grp_measures)}/{grp_count} battute in questa pagina)")
            else:
                print(f"  MMRest M{grp_start+1}-M{grp_start+grp_count} ({grp_count} battute)")
            
            # Il numero da mostrare è SEMPRE il totale del gruppo (es. "28")
            display_count = grp_count
            
            # Raggruppa le battute per sistema (sk key)
            from collections import OrderedDict
            by_system = OrderedDict()
            for gm_idx, m_start, m_end, sk, si in grp_measures:
                if sk not in by_system:
                    by_system[sk] = {'measures': [], 'sys_info': si}
                by_system[sk]['measures'].append((m_start, m_end))
            
            # Per ogni sistema che contiene battute MMRest, rimuovi pause/settori/numeri/tavola
            for sk, info in by_system.items():
                sys_measures = info['measures']
                si = info['sys_info']
                sys_start_x = sys_measures[0][0]
                sys_end_x = sys_measures[-1][1]
                st = si['top']
                sb = si['bottom']
                sh = sb - st
                
                # Rimuovi pause in questo sistema nel range MMRest
                def remove_rests_sys(match, msl=sys_start_x, msel=sys_end_x, st=st, sb=sb, sh=sh):
                    tx_m = re.search(r'transform="matrix\([^,]+,[^,]+,[^,]+,[^,]+,([\d.\-]+),([\d.\-]+)\)"', match.group(0))
                    if tx_m:
                        tx, ty = float(tx_m.group(1)), float(tx_m.group(2))
                        if msl - 200 < tx < msel + 200 and st - sh < ty < sb + sh:
                            return ''
                    return match.group(0)
                modified = re.sub(r'<path class="Rest"[^>]*/>', remove_rests_sys, modified)
                
                # Rimuovi numeri battuta in questo sistema
                def remove_meas_nums_sys(match, msl=sys_start_x, msel=sys_end_x, st=st, sb=sb):
                    x_m = re.search(r'x="([\d.]+)"', match.group(0))
                    y_m = re.search(r'y="([\d.]+)"', match.group(0))
                    if x_m and y_m:
                        x, y = float(x_m.group(1)), float(y_m.group(1))
                        if msl - 200 < x < msel + 200 and st - 300 < y < sb + 100:
                            return ''
                    return match.group(0)
                modified = re.sub(r'<text[^>]*font-size="160"[^>]*>\d+</text>', remove_meas_nums_sys, modified)
                
                # Rimuovi settori grigi in questo sistema
                def remove_bg_sys(match, msl=sys_start_x, msel=sys_end_x, st=st, sb=sb):
                    x_m = re.search(r'x="([\d.]+)"', match.group(0))
                    y_m = re.search(r'y="([\d.]+)"', match.group(0))
                    if x_m and y_m:
                        x, y = float(x_m.group(1)), float(y_m.group(1))
                        if msl - 200 < x < msel + 200 and st - 200 < y < sb + 200:
                            return ''
                    return match.group(0)
                modified = re.sub(r'<rect[^>]*(?:#E8E8E8|#B8B8B8)[^>]*/?>', remove_bg_sys, modified)
                
                # Rimuovi barline interne in questo sistema
                def remove_barlines_sys(match, msl=sys_start_x, msel=sys_end_x, st=st, sb=sb):
                    pts_m = re.search(r'points="([\d.\-]+),([\d.\-]+)', match.group(0))
                    if pts_m:
                        bx, by = float(pts_m.group(1)), float(pts_m.group(2))
                        if msl + 100 < bx < msel - 100 and st - 200 < by < sb + 200:
                            return ''
                    return match.group(0)
                modified = re.sub(r'<polyline class="BarLine"[^>]*/?>', remove_barlines_sys, modified)
                
                # Rimuovi tavola sonora in questo sistema
                def remove_tavola_sys(match, msl=sys_start_x, msel=sys_end_x, sb=sb):
                    x_m = re.search(r'x="([\d.]+)"', match.group(0))
                    y_m = re.search(r'y="([\d.]+)"', match.group(0))
                    if x_m and y_m:
                        x, y = float(x_m.group(1)), float(y_m.group(1))
                        if msl - 200 < x < msel + 200 and sb - 50 < y < sb + 500:
                            return ''
                    return match.group(0)
                modified = re.sub(
                    r'<rect[^>]*(?:#E53935|#FB8C00|#FDD835|#64DD17|#00695C|#1E88E5|#8E24AA)[^>]*(?:opacity="0\.2")[^>]*/?>',
                    remove_tavola_sys, modified)
                # Rimuovi testo tavola (font 90-149px) ma NON "battute di pausa"
                def remove_tavola_text_sys(match, fn=remove_tavola_sys):
                    text_content = re.search(r'>([^<]*)<', match.group(0))
                    if text_content and 'battute di pausa' in text_content.group(1):
                        return match.group(0)  # non rimuovere
                    return fn(match)
                modified = re.sub(
                    r'<text[^>]*font-size="(?:9[0-9]|1[0-4][0-9])"[^>]*>[^<]*</text>',
                    remove_tavola_text_sys, modified)
            
            # Disegna il rettangolo nero + numero solo sul PRIMO sistema del gruppo
            first_sys_info = list(by_system.values())[0]['sys_info']
            first_sys_measures = list(by_system.values())[0]['measures']
            # Il rettangolo va al centro delle battute del primo sistema
            rect_start_x = first_sys_measures[0][0]
            rect_end_x = first_sys_measures[-1][1]
            rect_center_x = (rect_start_x + rect_end_x) / 2
            staff_top = first_sys_info['top']
            staff_bottom = first_sys_info['bottom']
            staff_h = staff_bottom - staff_top
            staff_center = (staff_top + staff_bottom) / 2
            
            rect_w = 180
            rect_h = staff_h * 0.75
            rect_x = rect_center_x - rect_w / 2
            rect_y = staff_center - rect_h / 2
            
            num_font_size = 200 if display_count < 10 else 160
            mmrest_svg = (
                f'<rect x="{rect_x:.1f}" y="{rect_y:.1f}" '
                f'width="{rect_w}" height="{rect_h:.1f}" '
                f'fill="black" rx="8"/>'
            )
            num_y = staff_center + num_font_size * 0.35
            mmrest_svg += (
                f'<text x="{rect_center_x:.1f}" y="{num_y:.1f}" '
                f'text-anchor="middle" font-family="Atkinson Hyperlegible" '
                f'font-size="{num_font_size}" font-weight="900" fill="white">{display_count}</text>'
            )
            modified = modified.replace('</svg>', mmrest_svg + '\n</svg>')
            
            print(f"    MMRest: centro={rect_center_x:.0f}, numero={display_count}, sistemi={len(by_system)}")
    
    # calcola il numero totale di battute in questa pagina
    # direttamente dal layout dei sistemi, senza dipendere da equalized_measures
    # (che non ha entry per i sistemi MMRest o sistemi da 1 battuta).
    # Per ogni sistema: se è un MMRest → 1 battuta, se la battuta successiva
    # è MMRest → 1 battuta (sistema pre-MMRest), altrimenti 2 battute.
    _mmrest_set = set(gs for gs, gc in (mmrest_groups or []))
    total_meas_in_page = 0
    _cumulative = measure_offset
    sorted_sys_keys = sorted(systems_post.keys(), key=lambda k: systems_post[k]['top'])
    n_systems = len(sorted_sys_keys)
    # 4 Ago 2026 (bug KS): costruisci una mappa measure_idx → MMRest count
    # per contare le battute LOGICHE (non fisiche). Un MMRest(28) rappresenta
    # 28 battute logiche, non 1.
    _mmrest_count_map = {gs: gc for gs, gc in (mmrest_groups or [])}
    for i in range(n_systems):
        if _cumulative in _mmrest_set:
            # Questo sistema è un MMRest → conta TUTTE le battute rappresentate
            mmrest_n = _mmrest_count_map.get(_cumulative, 1)
            total_meas_in_page += mmrest_n
            _cumulative += mmrest_n
        elif (_cumulative + 1) in _mmrest_set:
            # La prossima battuta è MMRest → questo sistema ha 1 battuta
            total_meas_in_page += 1
            _cumulative += 1
        else:
            # Sistema normale → 2 battute (o 1 se è l'ultima misura)
            # 4 Ago 2026 (bug 55): usa il numero reale di battute da note_info,
            # non l'hardcode 84 (che fermava il conteggio a battuta 84 e
            # causava pagine 10-14 duplicate con battute 85-94).
            _max_measures = len(note_info.get('notes', [])) and max(
                n.get('measure_idx', 0) for n in note_info.get('notes', [])
            ) + 1 or 84
            _max_rests = len(note_info.get('rests', [])) and max(
                r.get('measure_idx', 0) for r in note_info.get('rests', [])
            ) + 1 or 0
            _total_measures = max(_max_measures, _max_rests, 84)
            n = min(2, _total_measures - _cumulative)
            if n <= 0:
                n = 2  # fallback: non fermare il conteggio
            total_meas_in_page += n
            _cumulative += n
    
    # Inserisci il count come commento SVG (per debugging)
    modified = modified.replace('</svg>', f'<!-- MEASURES_IN_PAGE:{total_meas_in_page} -->\n</svg>')
    
    # rimuovi l'attributo temporaneo data-dots dai cerchi (serviva solo per il matching dei dot)
    modified = re.sub(r' data-dots="\d+"', '', modified)
    
    # centra i sistemi in tutte le pagine + shift maggiore a sinistra.
    # Shift fisso di 500px applicato a tutte le pagine (non solo la prima).
    # Il pentagramma va da ~709 a ~9215. Con shift 500: margine sx=209px, dx=1209px.
    # i sistemi più a sinistra rispetto alla centratura precedente (236px solo pag 1).
    if rhythm_mode:
        vb_match = re.search(r'viewBox="([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)"', modified)
        if vb_match:
            vb_x, vb_y, vb_w, vb_h = (float(vb_match.group(i)) for i in range(1, 5))
            CENTERING_SHIFT = 500  # shift fisso a sinistra
            new_vb_x = vb_x + CENTERING_SHIFT
            modified = modified.replace(
                vb_match.group(0),
                f'viewBox="{new_vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}"')
    
    # in modalità rhythm, RIMUOVI FISICAMENTE le StaffLines
    # invece di renderle solo trasparenti. Alcuni visualizzatori PDF mostrano
    # comunque le linee trasparenti → alcuni visualizzatori le mostravano ancora.
    # Rimozione alla fine (dopo tutti i calcoli che usano StaffLines).
    if rhythm_mode:
        modified = re.sub(r'<polyline class="StaffLines"[^>]*/>', '', modified)

    # FOOTER COPYRIGHT su ogni pagina.
    # Testo in basso al CENTRO: "generated by MaidaScore — © 2026 Marco Maida"
    # Legge la viewBox corrente (post-shift rhythm) per posizionarsi al centro del margine inferiore.
    vb_match_f = re.search(r'viewBox="([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)"', modified)
    if vb_match_f:
        _fvb_x, _fvb_y, _fvb_w, _fvb_h = (float(vb_match_f.group(i)) for i in range(1, 5))
    else:
        _fvb_w, _fvb_h = 9924.0, 14028.0
        _fvb_x, _fvb_y = 0.0, 0.0
    _footer_x = _fvb_x + _fvb_w / 2            # centro orizzontale della pagina
    _footer_y = _fvb_y + _fvb_h - 120          # margine inferiore 120px
    _footer_text = "generated by MaidaScore — © 2026 Marco Maida"
    _footer_svg = (
        f'<text x="{_footer_x:.1f}" y="{_footer_y:.1f}" '
        f'font-family="Atkinson Hyperlegible,Carlito,DejaVu Sans,sans-serif" '
        f'font-size="110" font-weight="400" fill="#888888" '
        f'text-anchor="middle">{_footer_text}</text>\n'
    )
    modified = modified.replace('</svg>', _footer_svg + '</svg>')

    return modified, total_meas_in_page


# ==============================================================================
# 5. SVG → PDF
# ==============================================================================

def svg_to_pdf(svg_path, pdf_path):
    """Converte SVG in PDF usando cairosvg."""
    import cairosvg
    cairosvg.svg2pdf(url=svg_path, write_to=pdf_path)
    return pdf_path


# ==============================================================================
# MAIN PIPELINE
# ==============================================================================

def main():
    # --rhythm mode = figurazione ritmica (niente pentagramma, teste colorate
    # su stessa Y, gambi+travature, sezioni grigie, rettangoli durata, pause, tavola).
    rhythm_mode = '--rhythm' in sys.argv
    if rhythm_mode:
        sys.argv.remove('--rhythm')

    # --lang <lang>: select note-name language (it=Italian Do Re Mi, en=English C D E).
    lang = 'it'  # default
    if '--lang' in sys.argv:
        idx = sys.argv.index('--lang')
        if idx + 1 < len(sys.argv):
            lang = sys.argv[idx + 1].lower()
            del sys.argv[idx:idx + 2]  # remove --lang and its value
        else:
            print(f"Errore: --lang richiede un valore (it o en)")
            sys.exit(1)
    if lang not in NOTE_NAMES_PALLINI:
        print(f"Errore: lingua '{lang}' non supportata. Lingue disponibili: "
              f"{', '.join(sorted(NOTE_NAMES_PALLINI.keys()))}")
        sys.exit(1)

    # Activate the selected language by reassigning the global dictionaries.
    global NOTE_NAMES_EN, NOTE_NAMES_IT_TAVOLA, NOTE_NAMES_IT_TAVOLA_SPLIT
    NOTE_NAMES_EN = NOTE_NAMES_PALLINI[lang]
    NOTE_NAMES_IT_TAVOLA = NOTE_NAMES_TAVOLA[lang]
    NOTE_NAMES_IT_TAVOLA_SPLIT = NOTE_NAMES_TAVOLA_SPLIT[lang]
    global LANG
    LANG = lang

    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} input.mscz [output_prefix] [part_index] [--rhythm] [--lang it|en]")
        print(f"  part_index: 0=primo strumento (default), 1=secondo, 2=terzo, ecc.")
        print(f"  --rhythm: modalità figurazione ritmica (niente pentagramma, solo ritmo)")
        print(f"  --lang:   lingua dei nomi delle note (it=Do Re Mi, en=C D E). Default: it")
        print(f"")
        print(f"Ottimizzato per flauto traverso, ma funziona con qualsiasi strumento")
        print(f"presente nel .mscz: usa part_index per selezionare la parte desiderata.")
        print(f"Per strumenti con estensione molto diversa dal flauto, alcune costanti")
        print(f"di layout (vedi CONFIG all'inizio del file) possono richiedere adattamento.")
        sys.exit(1)

    input_mscz = sys.argv[1]
    
    # Supporta anche .mxl (MusicXML compresso) come input.
    # Converti in .mscz prima di procedere.
    if input_mscz.lower().endswith('.mxl'):
        print(f"  Input .mxl rilevato → conversione in .mscz...")
        converted_mscz = '/tmp/' + os.path.splitext(os.path.basename(input_mscz))[0] + '_converted.mscz'
        cmd = f'{XVFB} {MUSESCORE_CMD} -o "{converted_mscz}" "{input_mscz}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if not os.path.exists(converted_mscz):
            print(f"  ✗ Errore conversione .mxl → .mscz")
            sys.exit(1)
        input_mscz = converted_mscz
        print(f"  ✓ Convertito: {input_mscz}")
    
    if len(sys.argv) >= 3:
        prefix = sys.argv[2]
    else:
        base = os.path.splitext(os.path.basename(input_mscz))[0]
        prefix = base + '_maidascore'
    
    # part_index: quale parte/strumento estrarre (0=prima, default)
    part_index = 0
    if len(sys.argv) >= 4:
        part_index = int(sys.argv[3])

    # Pulisci file intermedi precedenti per evitare doppi processamenti
    # Importante: non cancellare il file di input dell'utente!
    import glob as glob_mod
    input_abs = os.path.abspath(input_mscz)
    for pattern in [f'{prefix}_svg-*.svg', f'{prefix}_svg-*_maidascore.svg',
                    f'{prefix}_maidascore*.svg', f'{prefix}.pdf']:
        for f in glob_mod.glob(pattern):
            if os.path.abspath(f) != input_abs:
                os.remove(f)
    # Rimuovi il .mscz accessibile generato (non l'input!)
    gen_mscz = f'{prefix}.mscz'
    if os.path.exists(gen_mscz) and os.path.abspath(gen_mscz) != input_abs:
        os.remove(gen_mscz)
    
    print(f"{'='*60}")
    if rhythm_mode:
        print(f"Pipeline MaidaScore — MODALITÀ RITMICA (figurazione ritmica)")
    else:
        print(f"Pipeline MaidaScore per spartiti accessibili dislessici")
    lang_name = {'it': 'Italiano (Do Re Mi Fa Sol La Si)',
                 'en': 'English (C D E F G A B)'}[lang]
    print(f"Lingua nomi note: {lang_name}")
    print(f"{'='*60}")
    print(f"Input: {input_mscz}")
    print(f"Output prefix: {prefix}")
    print(f"Part index: {part_index} (0=primo, 1=secondo, 2=terzo, ecc.)")
    print()
    
    # Step 1: Extract notes
    print("[1/5] Estrazione note dal .mscz...")
    note_info = extract_notes_from_mscz(input_mscz, part_index=part_index)
    ts = note_info.get('time_sig', (4, 4))
    print(f"  Time signature: {ts[0]}/{ts[1]}")
    if ts != (4, 4):
        print(f"  ⚠ ATTENZIONE: MaidaScore è ottimizzato per 4/4. Time signature {ts[0]}/{ts[1]} "
              f"potrebbe produrre risultati non ottimali.")
    print(f"  Trovate {len(note_info['notes'])} note:")
    # Detect chords (multiple notes with same onset+measure)
    chord_warnings = set()
    for n in note_info['notes']:
        if n.get('n_chord_notes', 1) > 1:
            chord_warnings.add(n['measure_idx'])
    if chord_warnings:
        print(f"  ⚠ Accordi rilevati nelle battute: {sorted(chord_warnings)}")
        print(f"    Le note degli accordi saranno impilate allo stesso X (non distribuite).")
    for n in note_info['notes']:
        pitch_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        name = pitch_names[n['pitch'] % 12]
        print(f"    {name}{n['pitch']//12-1} (pitch={n['pitch']}, type={n['duration_type']}, dots={n['dots']})")
    print()
    
    # Step 2: Make accessible .mscz
    print("[2/5] Creazione .mscz accessibile...")
    accessible_mscz = prefix + '.mscz'
    # se il file ha più strumenti, estrai solo la parte richiesta
    single_part_mscz = extract_single_part_mscz(input_mscz, part_index=part_index)
    accessible_mscz, initial_rest_measures, mmrest_groups = make_accessible_mscz(single_part_mscz, accessible_mscz, part_index=part_index, rhythm_mode=rhythm_mode)
    print(f"  ✓ {accessible_mscz} (spatium={SPATIUM}, staffLineWidth={STAFF_LINE_WIDTH})")
    
    # ri-estrarre le note dal .mscz accessibile (dopo split MMRest)
    # usa extract_notes_via_music21 che gestisce i tie correttamente.
    # extract_notes_from_mscz (parser .mscx diretto) NON gestisce i tie → estrae
    # note legate come separate → mismatch con il rendering SVG di MuseScore.
    # 4 Ago 2026 (bug KS): estrai dal file ORIGINALE (input_mscz) non dall'
    # accessible_mscz (83 battute fisiche, measure_idx 0-82) né dal single_part_mscz
    # (che va in segfault con MuseScore a causa degli MMRest nativi).
    # Il file originale ha 140 battute logiche con measure_idx 0-139, che
    # corrispondono alle battute reali del brano per il mapping note→battute.
    note_info = extract_notes_via_music21(input_mscz, part_index=part_index)
    print(f"  Ri-estratte {len(note_info['notes'])} note dal file originale (via music21, tie-aware)")
    print(f"  measure_idx range: 0-{max(n['measure_idx'] for n in note_info['notes'])}")
    print()
    
    # Step 3: Export SVG
    print("[3/5] Export SVG da MuseScore 4...")
    svg_prefix = prefix + '_svg'
    svg_files = export_svg(accessible_mscz, svg_prefix)
    if not svg_files:
        print("  ✗ Errore export SVG")
        sys.exit(1)
    print(f"  ✓ {len(svg_files)} pagina/e SVG: {', '.join(svg_files)}")
    print()
    
    # Step 4: Post-process SVG
    print("[4/5] Post-processing SVG (colori, nomi, sfondi, rettangoli)...")
    processed_svgs = []
    note_offset = 0  # cumulative offset across pages
    measure_offset = 0  # cumulative measure offset across pages
    
    # 3 Ago: pre-calcola mappa measure_idx → primo indice nota nel .mscz
    # per calcolare correttamente il note_offset basato sul measure_offset
    _first_note_idx_by_measure = {}
    for _i, _n in enumerate(note_info.get('notes', [])):
        _mi = _n.get('measure_idx', -1)
        if _mi not in _first_note_idx_by_measure:
            _first_note_idx_by_measure[_mi] = _i
    
    for i, svg_file in enumerate(svg_files):
        print(f"  Pagina {i+1}:")
        with open(svg_file, 'r') as f:
            svg = f.read()
        
        # calcola note_offset contando le note SVG disegnate
        # nelle pagine precedenti. Questo è più affidabile del measure_offset
        # perché conta direttamente dal SVG, non da equalized_measures.
        # La prima pagina parte da 0, le successive sommano le note delle pagine precedenti.
        if i == 0:
            note_offset = 0
        # note_offset viene aggiornato dopo ogni pagina (vedi sotto)
        
        processed, meas_count = process_svg(svg, note_info, note_offset=note_offset,
                                is_first_page=(i == 0),
                                title_text=None,
                                part_text=None,
                                measure_offset=measure_offset,
                                initial_rest_measures=initial_rest_measures if i == 0 else 0,
                                mmrest_groups=mmrest_groups,
                                rhythm_mode=rhythm_mode)
        
        # aggiorna measure_offset dal count ritornato
        measure_offset += meas_count
        
        # aggiorna note_offset contando le note SVG in questa pagina
        # (note reali, non pause). Usa il SVG processato per contare i cerchi.
        import re as _re
        _n_circles = len(_re.findall(r'<circle ', processed))
        note_offset += _n_circles
        
        out_svg = svg_file.replace('.svg', '_maidascore.svg')
        with open(out_svg, 'w') as f:
            f.write(processed)
        processed_svgs.append(out_svg)
        print(f"  → {out_svg} (note cumulative offset: {note_offset}, measures: {meas_count})")
    print()
    
    # Step 5: SVG → PDF
    print("[5/5] Conversione SVG → PDF...")
    if len(processed_svgs) == 1:
        pdf_path = prefix + '.pdf'
        svg_to_pdf(processed_svgs[0], pdf_path)
        print(f"  ✓ {pdf_path}")
    else:
        # Multiple pages: convert each and merge
        pdf_parts = []
        for i, svg in enumerate(processed_svgs):
            pdf_part = svg.replace('.svg', '.pdf')
            svg_to_pdf(svg, pdf_part)
            pdf_parts.append(pdf_part)
        
        # Merge multiple PDF pages using pypdf (pure Python, no system deps)
        pdf_path = prefix + '.pdf'
        from pypdf import PdfWriter
        writer = PdfWriter()
        for pdf_part in pdf_parts:
            writer.append(pdf_part)
        with open(pdf_path, 'wb') as f:
            writer.write(f)
        # Clean up temporary per-page PDFs
        for pdf_part in pdf_parts:
            try:
                os.remove(pdf_part)
            except OSError:
                pass
        print(f"  ✓ {pdf_path} ({len(processed_svgs)} pagine)")
    
    print()
    
    # Step 6: Validate output (independent validator)
    print("[6/6] Validazione output (validatore standalone)...")
    try:
        from validate_maidascore import validate_all_pages
        val_dir = os.path.dirname(prefix) or '.'
        val_result = validate_all_pages(val_dir, single_part_mscz, prefix=prefix)
        print(val_result.report())
        if val_result.ok:
            print("  ✓ Validazione superata")
        else:
            print("  ✗ Validazione FALLITA — errori trovati!")
            print("  Il PDF potrebbe contenere errori. Controllare prima di inviare.")
    except Exception as e:
        print(f"  ⚠ Validatore non eseguito: {e}")
    
    print()
    print(f"{'='*60}")
    print(f"COMPLETATO!")
    print(f"  .mscz accessibile: {accessible_mscz}")
    print(f"  PDF MaidaScore:     {pdf_path}")
    print(f"{'='*60}")
    
    return accessible_mscz, pdf_path


if __name__ == '__main__':
    main()
