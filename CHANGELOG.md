# Changelog

## [1.2.26] — 1 Ott 2026
### Fixed
- fix: tavola sonora fuori posto a metà pagina (Marco: "da battuta 43 a 48 si vedono le tavole fuori posto al centro del pentagramma"). CAUSA RADICE: draw_tavola_sonora calcola tavola_top = bottom_y + max(tavola_gap, max_stem_y - bottom_y + 50) con max_stem_y letto dall'SVG AL MOMENTO della chiamata (riga ~11075) — PRIMA dei pass in coda (9/10/11/12) che spostano le beams fuori rigo e estendono i gambi: al momento della draw i gambi del sistema 2 arrivavano a ~5845 → tavola_top = 5895, DENTRO il rigo 3 (top 5470): la tavola di b37-42 si sovrapponeva al pentagramma di b43-48. Bug pre-esistente (presente in v1.2.20 e v1.2.25). FIX DOPPIO: (1) la chiamata draw_tavola_sonora SPOSTATA DOPO i pass in coda (prima del footer): la tavola usa le posizioni definitive di gambi e beams; (2) CAP: tavola_top = min(tavola_top_calcolato, top_del_rigo_successivo - row_height - 40), floor = bottom_y + tavola_gap: la tavola non supera mai il gap libero tra i due righi anche se i gambi si estendono oltre.
### Gotchas
- (1) SPOSTARE la draw dopo i pass SENZA cap si limita a spostare il problema: con i gambi post-pass il max_stem_y di un altro sistema può superare il rigo successivo (tavola sys4 finiva a 10896 dentro il rigo 5): il cap è indispensabile; (2) il primo tentativo di cap con sorted(..., reverse=True)[-1] prendeva il top MINORE (700) = cap assurdo: il top successivo = sorted ASCENDENTE [0]; (3) l'identificazione delle celle tavola = rect rx=8 h~175 (in notazione le celle tavola e le micro-celle rhythm si distinguono per h: 175 vs 392); (4) in rhythm mode le tavole hanno fill PRIMA di rx nell'attributo (regex fill...rx, non rx...fill); (5) gli elementi della tavola sbagliata si mescolano alle note del rigo sovrapposto (cerchi/nomi note a Y simili): un Pass di riposizionamento post-hoc era fragile (nomi note e testi tavola con stessa X e Y ±11) — il fix alla fonte (ordine di chiamata + cap) è robusto e senza ambiguità; (6) la draw usa max_stem_y anche per le sezioni grigie (riga ~7204) ma lì grey_height = staff_height = non influisce.

## [1.2.25] — 1 Ott 2026
### Fixed
- fix: doppia travatura mancante sulle 4 semicrome a b3 (e "linea di travatura in basso sbagliata" sull'ultimo Sol). CAUSA RADICE: nel layout RAW affiancato di MuseScore le coppie croma+semicrome multi-battuta hanno la SECONDARIA che attraversa i confini di battuta (es. sec raw x1290-3785 copre b2+metà b3); quando il postprocessore rimappa le beams per battuta, il frammento di secondaria che ricade nella battuta successiva viene posizionato a partire dalla sua X raw FUORI dal nuovo gruppo: la sec b3 finiva DOPO l'ultima semicroma (x5134-5287 oltre la fine della prim x4664-5139) invece di coprire le semicrome — le 4 semicrome restavano con la sola primaria e la linea appariva oltre l'ultimo Sol. Il BeamMode injection (begin/mid/mid/end = gruppo unico full-width) era corretto ma il frammento RAW multibattuta lo sovrascriveva. FIX: Pass 12 in coda a process_svg (coordinate definitive, ENTRAMBE le modalità): per ogni secondaria corta (w<=400) il cui x_left sta OLTRE la fine della primaria associata (stesso sistema, Y-gap < 200, finestra X +150, sec già allineata = skip) e con >= 2 note sotto la primaria: riposizionare la sec all'inizio del gruppo (x_left = prim.x_left) e stenderla fino all'ultima semicroma (doppia travatura completa come il beaming originale del file di Marco). Guard anti-falsi-match: skip se sec.x_left già allineato a prim.x_left ±60; replace sicuro (solo se il path d esiste e cambia).
### Gotchas
- (1) l'audit "sec sporgente" con finestra X +600 matcha le sec delle battute ADIACENTI (stessa Y, gap < 200 tra beams di battute diverse): la finestra corretta = prim.x_right - 50 .. +150 (le sec sporgenti vere stanno subito oltre la fine della prim); (2) la finestra Y delle teste r72/88 sotto la prim deve essere 1200px (le teste con ledger stanno a 933px dal beam mid: 900 scartava); (3) il replace del path d senza verifica ha creato DUPLICATI (sec spostata in y sbagliata 1027 vs 1104): verificare sempre che old_d in modified e new != old; (4) il check "beam obliqua" con confronto p0 vs il primo punto con X diverso DEVE usare il TOP edge (p0-p1): il confronto con y_bot (p3 = y1+thickness) dà ΔY 47 = falsi positivi 130; (5) le teste del post = circle r 72/88/110 (NON r58): le semicrome hanno r72; il filtro r>=115 scarta le pause.

## [1.2.24] — 1 Ott 2026
### Fixed
- fix: gambi che attraversano la beam spostata dal Pass 11 e vanno OLTRE il bordo (Marco: "gambi che fuoriescono dalle travature, vanno oltre", b14 e altri punti). CAUSA: il ramo _ext_pair11 gestisce solo i gambi interamente fuori dalla nuova posizione della beam (_yb < new_top = estendi bottom; _yt > new_bot = estendi top): il caso ATTRAVERSAMENTO (yt < new_top E yb > new_bot) non era gestito — il gambo resta lungo quanto la vecchia posizione della beam e sporge oltre il bordo (es. gambo x4292 y8747-9566, beam spostata da y9504-9551 a y9270-9317: sporge di 249px). DOPPIO FIX: (1) ramo elif per l'attraversamento: accorcia l'endpoint oltre in base alla direzione dello shift (shift<0 = beam salita/stems-down → bottom = new_bot+10; shift>0 = beam scesa/stems-up → top = new_top-10); (2) il check "gambo già attaccato a una testa" faceva return PRIMA dell'accorciamento = i gambi attaccati alla testa (i più comuni) MAI accorciati: ora il check testa NON skippa quando il gambo attraversa la beam spostata (for/else: return solo se il gambo NON attraversa).
### Gotchas
- (1) l'audit "gambi oltre" deve considerare legittimo un endpoint che coincide con QUALSIASI beam del range X ±30 (tolleranza 20px per stroke-linecap round) O con la TESTA: un gambo che attraversa la sec per attaccarsi alla croma (o che attraversa la beam per raggiungere la testa sotto) è la STRUTTURA CORRETTA delle coppie impilate; (2) la baseline del revert (v1.2.20) per "gambi oltre" con audit severo era 49 = il revert NON era pulito: l'audit con endpoint-vs-testa/beam corretto dà 0 sia nel revert sia nel fix; (3) la logica intermedia "testa dal lato opposto" ha prodotto 17-21 falsi positivi (head nel range ±110 con r+5 matchava teste di SISTEMI diversi): il collect dei bad senza il filtro Y del rigo è inaffidabile; (4) la metrica definitiva = diff vs revert: v128 0 NUOVI gambi oltre (e alcuni fixati), revert 0, v126 0.

## [1.2.23] — 1 Ott 2026
### Fixed
- fix: Pass 11 partner matching moved beams of the WRONG staff (Marco: "gambi fuori dalle travature, gambi troppo lunghi che fuoriescono dal sistema"). CAUSA: il partner della coppia era scelto SOLO per sovrapposizione X >60% SENZA check Y — le beams con lo stesso X in RIGHI DIVERSI della stessa pagina vengono accopiate erroneamente (la croma del rigo 3 x5544 y5189 ha X-overlap 100% con la sec del rigo 1 x5563 y1345, Y completamente diversi) → il Pass 11 spostava la sec del rigo 1 di +301 insieme alla croma del rigo 3 → la sec appesa sotto la croma con gap 192 e il gambo che non la raggiunge. FIX: check Y aggiunto al partner matching: gap Y < 200px (le coppie impilate sono vicine in Y per definizione). AUDIT: 0 beams fuori dal rigo, 0 coppie staccate (gap geometrico >100), 0 beams oblique, gambi 18/14/13 = baseline noto v1.2.20, sec b34 tornata a y1345 attaccata (era y1646 staccato), rhythm 0 teste senza gambo. Validatore: stessi 10 false positivi noti.
### Gotchas
- (1) il partner senza check Y accopia beams dello stesso X in righi diversi: il gap Y (max(top)-min(bot) dei due) deve essere <200px; (2) l'audit "coppie gap errato" con gap = differenza dei TOP dà falsi positivi per le coppie sec-sopra (il gap geometrico reale = bottom sec vs top croma = max(top)-min(bot)); (3) l'audit X-overlap con indici misti (b1[1]=y_top usato come x_right) dà falsi positivi 63; (4) il debug ROUND con w = b[1]-b[0] (y_top - x_left) è fuorviante: i beams hanno Y indipendenti.

## [1.2.22] — 1 Ott 2026
### Fixed
- fix: dotted-eighth secondary beams missing when the beamed group is parked between systems (staves 3, bars 3 and 13-16). Cause: the horizontal layout of MuseScore parks multi-bar beam groups (eighth+two sixteenths) between staves legitimately; the vertical layout post-processor remaps Y faithfully, so the group stays outside the staff — sixteenth notes appear with no visible beam and stems cross the pentagram. FIX: new Pass 11 in process_svg (before the footer, both modes): the WHOLE group (beam + partner secondary beam) is moved with ONE single shift (computed from the primary beam), preserving the group gap — moving beams independently fuses them (double beam destroyed, v1.2.21 regression reverted in 757a04c). Stems of the group are extended toward the new beam position ONLY when not already attached to a notehead (Pass 10 already extended them). Gotchas: (1) the group is detected by X-range overlap >60% of the narrower beam (not by the pairing flag); (2) stems-up (beam above staff) → beam bottom = staff top + 20; stems-down → beam top = staff bottom - 20 - thickness; (3) the group shift must be applied BEFORE extending stems and the SVG re-parsed per iteration (edits invalidate beam offsets — stale offsets produced 27 beams outside the staff); (4) the Stem polyline replacement MUST re-emit the closing quote (missing quote broke the XML tag and made every stem unreadable); (5) staffline polylines can have more than 2 points (multi-segment) — parse with replace(',',' ').split()[1], not split(',')[1]; (6) re-parse heads per iteration for the already-attached check.

## [1.2.20] — 1 Ott 2026
- fix: stem-extension regex (Pass 8/10) included r=110 heads (quarter/half notes). Stems of these notes (head 47px above stem top) were never extended — vertical lines crossing the staff without touching the notehead. Audit: 0 headless stems on Radetsky notation (3 pages) and rhythm.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.19] - 2026-10-01
### Fixed
- Gambi staccati dalla testa ("crome senza gambo", es. battute 13-16 notazione): i pass 9d/9e (coppie impilate) girano DOPO il pass 8 e spostano/estendono i gambi alla beam — un gambo esteso dal pass 9d/9e può NON raggiungere la testa (gambo finito alla beam mentre la testa sta più lontano, es. testa cy 5889 e gambo y 5179-5236 = gap 653px). FIX: Pass 10 in coda a process_svg (coordinate definitive, ENTRAMBE le modalità, prima del footer): re-parse heads e stems dall'SVG corrente e per ogni gambo la cui estremità NON interseca il cerchio della testa più vicina (±67) estende l'endpoint verso la testa (come il pass 8). NON estende i gambi già attaccati a 2+ teste (accordi verticali) né i gambi con la testa dentro l'intervallo.
- Audit: notazione 0 teste senza gambo (era 5-6), rhythm 1-2 residue, example 0. Validatore TUTTO OK su tutte le pagine (entrambe le modalità).

## [1.2.18] - 2026-10-01
### Fixed
- Linea di travatura in più a battuta con croma puntata + semicroma (dotted-eighth + sixteenth): MuseScore esporta la backward hook dell'ultima semicroma anche per questa figura (beam stretta w<200 ancorata al gambo della semicroma). Il creator della secondaria manuale, non rilevando l'hook esistente, ne creava un DUPLICATO — due linee sovrapposte sulla semicroma, poi due linee parallele dopo il decollide. FIX: il creator salta la creazione della secondaria quando l'hook è già presente nel SVG (check: beam stretta w<200 con x_right al gambo della semicroma ±60, stesso sistema, anche se già marcata secondaria dal pairing). Verifica oggettiva: 0 beams secondarie duplicate su Radetsky (entrambe le modalità) ed example; 0 beams oblique; 0 teste senza gambo.

## [1.2.17] - 2026-10-01

### Fixed
- **Extra beam line on backward hook (round 13)**: MuseScore exports the
  backward hook of the last 16th note as a beam SECONDARIA FULL-WIDTH
  (same X as the primary) for groups eighth+eighth+dotted-eighth+16th
  (croma-croma-3/4-1/4): the full-width secondary covers ALL notes = an
  extra beam line that should not exist (the hook must cover only the
  last 16th). The notazione branch (round 9quinto, X ranges identical)
  left it full-width. FIX: same-X full-width secondary (w diff 0) →
  truncate to the hook width: from the last 16th stem (cx - r - 4.7) to
  the note right edge + overhang (cx + r + 4.7) — the note sits beyond
  the primary's end (the primary ends at the last stem), so the hook
  fragment covers the note. GOTCHAS: (1) the truncation scan uses sn.get('y')
  not 'center_y' (note dicts have 'y', not 'center_y' — the filter with
  center_y=0 rejected all notes → no truncation); (2) the truncation
  range = the PRIMARY ±100 (the last 16th sits beyond the truncated
  secondary's right edge — without the margin the note is not seen);
  (3) sec['x_left']/_x_right MUST be assigned (an earlier draft computed
  the interpolated Y but never assigned the new X = incomplete truncation);
  (4) the Y interpolation uses _sec_x_orig_l/_r (the secondary X BEFORE
  reposition, defined before the sec/pri branch to avoid UnboundLocalError);
  (5) the audit "oblique beam" check must break at the FIRST point with a
  different X (comparing p0 with p2 bottom-right gives false positives on
  straight quadrilateral beams).
  Audit: 0 full-width secondaries across Radetsky (both modes), example;
  0 oblique beams (corrected audit); validator passed on all pages.

## [1.2.16] - 2026-10-01

### Fixed
- **Pass 9e filters rejected stems-down croma pairs (b13-16, round 12bis)**:
  three filter bugs left the stems-down croma pairs unprocessed — (1) the
  stem-group Y filter used the stem MIN (the head, far above the pair for
  stems-down) instead of the interval intersection; (2) the croma stem
  X filter rejected stems 4px inside the secondary border (the croma stem
  sits at the note X, ±15 from the sec border); (3) the touch check used
  max(st) instead of min(st) for the beam-side endpoint. New: the group
  stem must have a real notehead (r 58-90) near its X (fragments extended
  by the 9d pass, with no head, were swapped for the croma stem → wrong
  direction → re-swap of already-corrected pairs). The crossing-stem case
  is now checked FIRST (top above the zone and bottom below) and its
  direction is read from the real notehead nearest the stem X, not from
  y1-vs-y2 (MuseScore writes crossing stems with the head as first point
  in either order). Audit: 0 wrong pairs across Radetsky F1/F2 (both
  modes), example; 0 anomalous beams. Validator passed.

## [1.2.15] - 2026-10-01

### Fixed
- **Croma beam placement depends on stem direction (round 12)**: in a
  stacked pair (croma + semicromas) the croma line sits on the UPPER
  line when the stem points UP, on the LOWER line when the stem points
  DOWN — the previous rule ("croma always on top", correct only for
  stems-up) was inverted for stems-down. New Pass 9e at the end of
  process_svg (final coordinates, both modes): for each croma pair
  (X overlap > 50px, gap 0-150, width diff > 40, chromatic stem outside
  the secondary X-range touching the croma) the direction is read from
  the stem (head side above/below the pair), then both beams are moved
  by pure top-swap (croma to the sec top, sec to the croma top — gap
  preserved by construction). Audit: pre-fix 17/17 croma pairs inverted
  (stems-down, rhythm) across Radetsky measures 1, 20-21, 23, 27-28;
  post-fix 0 pairs wrong, 0 anomalous beams, gaps preserved, on all
  pages in both modes (Radetsky F1/F2, example). Validator passed.

## [1.2.14] - 2026-10-01

### Fixed
- **Stems in stacked beam pairs extended to outer edge**: stems in a
  stacked pair (primary + secondary beam) stopped at the inner edge of
  the higher beam (stem top = beam bottom) instead of crossing both
  lines to the outer edge — the secondary beam appeared suspended in
  the gap without stems attached. Convention: in a stacked pair stems
  cross both beams up to the outer edge of the farthest beam from the
  noteheads. New Pass 9d runs at the end of process_svg (final
  coordinates, both modes): for each stem in a stacked pair (X overlap
  > 50px, gap 0-100) the outer end is extended to the outer edge of the
  farthest beam (offset 10px for round linecaps). Audit: pre-fix 34/34
  stacked pairs wrong (rhythm) and 6/6 (notation) across Radetsky
  measures 1, 20-21, 23, 27-28; post-fix 0 pairs wrong on all pages in
  both modes. Validator passed.

## [1.2.13] - 2026-09-30

### Fixed
- **Duplicated/triple beams removed**: stale-beam filter margin widened to
  300px (beams spanning adjacent beat groups were excluded by the narrow
  window and left in place as stacked triples/quadruples); beat-adjacent
  beam fragments of a split beam no longer overlap after repositioning;
  fully-contained duplicate fragments are removed instead of moved.
- **Croma beam attached and on top**: in a 2 semicromas + croma group the
  croma beam line must be the HIGHER line of the stacked pair, attached
  to it (gap 0) — the croma beam bottom touches the top line of the
  preceding semicroma beam. Above/below is decided from the stem
  direction, not from the beams' current relative position.
- **Uniform beam thickness 47px** in all modes (resized in both flatten
  branches), decollide loop repeats until no pair has a real gap < 15px.

## [1.2.11] - 2026-09-29

### Changed
- **F (Fa) palette color lightened**: the green used for the F note is now
  `#A8DB78` (was `#64DD17`), more comfortable on the eyes while keeping an
  excellent 13.1:1 contrast ratio against the black note-name text
  (WCAG AAA). The open-note variant (white head + dark green border
  `#558B2F`, 4.10:1) is unchanged. Updated in the generator, the
  standalone validator, the README palette table and the conversion guide.

## [1.2.10] - 2026-09-25

### Fixed
- **Sound-table note name eaten by MMRest cleanup**: the text of the first
  table cell right after a multi-measure-rest group (e.g. the "Sol" label)
  fell inside the removal window's +200px right slack and was deleted,
  leaving a colored cell with no name. The right edge of the removal
  window is now flush with the group border, consistent with the earlier
  fixes for measure numbers and gray sectors.
- **Title and author now taken from the score text elements**: when a
  .mscz has an empty or OMR-artifact `workTitle`/`composer` metaTag
  (e.g. title falling back to the file name, composer showing "Music21"),
  the title/author are now read from the score's own
  `<Text><style>title/composer</style>` elements, which reflect what the
  score actually displays.

## [1.2.9] - 2026-09-23

### Fixed
- **Naturals now appear in the sound-table cells too**: a note requiring a
  natural sign (e.g. F natural after F# in the same measure) now shows the
  natural glyph under the note name in the tavola sonora cells, in both
  notation and rhythm mode, in both the timeline and fallback rendering
  paths. Previously only the staff circles got the natural sign.

## [1.2.8] - 2026-09-23

### Fixed
- **Missing naturals (bequadri) after a passing accidental in the same measure**:
  after e.g. F# followed by F natural in the same measure, the natural sign
  was never drawn, so the previous sharp visually applied to the whole
  measure. MuseScore marks visually-necessary naturals with
  `accidental.displayStatus=True`; the note extractor now honours that flag
  and draws the natural sign on the staff (top/bottom of the notehead disc,
  per stem direction). Verified on Gnomus: 6 expected naturals, 6 drawn,
  each aligned to the correct notehead.

## [1.2.7] - 2026-09-23

### Fixed
- **Missing tavola sonora cells when a collapsed MMRest is not the first
  measure of the system**: the logical remapping (physical m_idx → logical
  measure index) in `draw_tavola_sonora` only applied the MMRest extra
  advance (gc-1) when the collapsed group was at m_idx 0. With the group in
  the middle (e.g. b35 + MMRest b36-38 + b39 in Gnomus), the following
  measure was mapped to a logical index inside the MMRest group and landed
  in `_mmrest_skip_measures`, so its tavola cells were never drawn. The
  collapse is now applied regardless of the group position (the
  `len(measures) > 1` guard for pure-MMRest systems is retained).

## [1.2.6] - 2026-09-22

### Fixed
- **Inverted 4x16th note groups in notation mode**: the anti-overlap
  resolver computed the minimum disc distance using the full-size radius
  (110px) even for 16th notes (real radius 0.65x). With four 16th notes
  in one sector, the third note was pushed past the fourth ("push-past"),
  inverting the visual order (e.g. Fa-Si-Si-Fa instead of Fa-Si-Fa-Si on
  the Pizzicato Polka score). Fixed: (1) the sweep pass now uses
  `_compute_note_radius()` (real per-duration radius) instead of the
  full-size override; (2) deterministic sort with onset tie-break;
  (3) the per-sector layout orders groups by `(measure_idx, onset)`
  instead of `center_x`. Regression-tested on Canzon, Amen and Danza
  delle Spade, both notation and rhythm modes.

## [1.2.5] - 2026-09-16

### Fixed
- **Measure numbering after multi-measure rests**: measure numbers used the
  geometric SVG group index (m_idx+1): each MMRest(N) collapsed into a single
  SVG group made the numbering lose N-1 logical measures. After an initial
  MMRest(4) the first played measure was numbered "2" instead of "5"
  (Danza delle Spade). Fixed: the system's first measure index now derives
  from `_sys_to_global_idx` (build_system_layout, single source of truth),
  and a per-system `_mm_extra` accumulator skips the logical measures
  swallowed by an MMRest group in mixed systems (real measures + MMRest in
  the same system). Verified against the canonical MuseScore logical
  numbering via music21 (notes on measures 5-49, 58-87, 92).

## [1.2.4] - 2026-09-15

### Fixed
- **Spurious barlines in mixed MMRest + real measures systems**: the x_end
  alignment correction for the last group of a system (`new_c -= grp_max -
  old_c`) was also applied to the collapsed MMRest group. With 7+ internal
  barlines grp_max >> old_c, making new_c negative (e.g. -4200): the collapse
  proportional scale inverted and the internal barlines were scattered far
  outside the MMRest box, outside the MMRest cleanup window — surviving as
  stray barlines across the staff. The collapsed MMRest group is now excluded
  from that correction.

## [1.2.3] - 2026-09-15

### Fixed
- **Rest overlapping the following eighth note in notation mode**: the
  rest-circle anti-collision pass ran in rhythm mode only, but circle
  discs exist in notation mode too. It is now always active (the rest
  dedup pass remains rhythm-only). Three follow-up bugs fixed to make it
  work in notation mode: vertical filter window widened from 100px to
  600px (rests and discs are ~500px apart vertically on the same staff),
  measure bounds now use equalized measures instead of pre-equalization
  raw barlines, and the occupied-rests list no longer blocks a rest
  against its own prepopulated position.

## [1.2.2] - 2026-09-15

### Fixed
- **Double rest glyphs (bug doppia pausa)**: MuseScore sometimes renders MORE
  Rest glyphs than the .mscz declares (e.g. a quarter rest split into an extra
  eighth glyph). The post-processor repositioned the extra glyph at its original
  position, producing two overlapping eighth rests ~175px apart in the same
  grey sector. Extra glyphs are now removed when every rest of the measure has
  already been matched.
- **Un-repositioned rests removed in notation mode too** (previously
  rhythm-mode only): original MuseScore rests left at their original
  position/scale overlapped notes after layout.
- **Empty staff after initial multi-measure rest**: the initial rest group is
  no longer isolated on its own system; it now packs compactly in the first
  system together with the first music measures.
- **Beams overlapping, grey sectors out of margin, "4" box clipped** at the
  page edge; eighth rests placed outside their sector.
- **Double xvfb-run**: MuseScore segfaulted when the `mscore` wrapper already
  launches xvfb-run (skip the second wrapper).

## [1.1.6] - 2026-09-12

### Fixed
- **Sound tables (tavole) empty after MMRest**: the system layout lookup used
  post-stretch system keys against a pre-stretch-keyed layout dict, so it never
  matched and fell back to a uniform heuristic that miscounted measures after
  a multi-measure rest. Rests ("pausa" dashed cells) were drawn under measures
  that actually contain notes. Now systems are matched by Y-order index.

## [1.1.5] - 2026-09-12

### Added
- **Width-based system packing**: measures are now grouped on each staff by
  total width (grey sectors) instead of a fixed count. Short measures (e.g. 2/4
  after a 4/4 section) are packed 4-per-staff instead of 2, eliminating empty
  staff space and reducing PDF page count (15 → 10 pages in the test score).

## [1.1.4] - 2026-09-12

### Fixed
- **Single-measure system with time-signature change** (e.g. a 2/4 measure alone
  on a staff after a 4/4 section): grey sectors now start after the clef/key
  signature (UNIFORM_MUSIC_START) instead of at the staff start — previously they
  covered the clef and key signature, and the measure spanned the whole staff width.
- **Rests in note-less single-measure systems**: when such a system contains only
  rests, they are now repositioned onset-based like all other rests, instead of
  being left at their original MuseScore X position (outside the grey sectors).

## [1.1.3] - 2026-09-12

### Fixed
- **Key signature vertical positions on staff**: accidentals are now placed at the
  correct staff line/space for each pitch, following standard engraving rules.
- **Staff accidentals rendered as musical glyphs**: sharps, flats and naturals on the
  staff use Unicode musical characters (DejaVu Sans), enlarged 45% for readability,
  with a fallback to ASCII rendering on fonts without glyph support.
- **Spurious beams crossing rests**: beam injection from the original .mscx no longer
  draws beams through rests (e.g. eighth-note + rest groups).
- **Rests pushed outside their grey sector**: the anti-collision nudge now clamps
  rests inside their own grey sector, and never places a rest on top of a barline.
- **Rest-note minimum visual gap**: when a rest shares a grey sector with a note,
  it is positioned at 75% of the sector (mirroring the 25% placement of single
  notes), with the nudge padding raised from 20 to 100px for a comfortable gap.

### Changed
- **Sound table accidentals**: bigger and bolder, drawn with Unicode musical glyphs
  and placed closer to the note blocks.
- **Logical multi-measure rest groups**: measures with time-signature changes are
  excluded from logical MMRest groups.

## [1.1.2] - 2026-09-10

### Added
- **Score title and author on the first page**: the header now shows the score
  title (bold) and the author, drawn in a band added above the viewBox so the
  staff positions are unchanged. Title is read from `<credit-words>` /
  `movement-title`, author from the composer metadata (`<creator>` for MusicXML,
  `<metaTag name="composer">` for MuseScore files).

### Fixed
- Removed a duplicate header block that drew the title/author twice.

## [1.1.1] - 2026-09-10

### Changed
- **Grey sectors restricted to the staff**: the alternating grey quarter-backgrounds
  now end at the fifth staff line (bottom) instead of extending all the way down to
  the tavola sonora (sound table). The space between the staff and the sound table
  is now clean white, giving a clearer visual separation between the notation and
  the sound table. Verified on multi-time-signature scores (4/4 → 3/4).

## [1.1.0] - 2026-09-10

### Added
- **Wavy lines (ondine) inside duration bars**: notes lasting longer than
  one beat now render a regular, perfectly symmetric sine wave inside the
  colored duration bar. The wave has constant amplitude and period
  (auto-scaled, ~70px per wave), drawn with 8 samples per half-wave for a
  smooth path. Visual indication of sustained sound, complementing the
  color-coded circles.

### Fixed
- **Triplet handling — overfull measures**: triplet 16ths (actual duration
  1/6 of a quarter) were extracted as regular 16ths (0.25) and re-appended
  sequentially during the single-part rebuild, producing overfull measures
  (e.g. 17/16) and a silent MuseScore 4 crash at SVG export (exit code 40).
  Note extraction now stores the exact `quarterLength` of every note/rest,
  and the rebuild uses it instead of mapping duration types through a
  fixed table.

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

## [1.1.7] - 2026-09-12

### Aggiunto
- Pause nella tavola sonora: sfondo nero, testo bianco bold e conteggio dei beat al posto di "pausa" (semiminima=UNO, minima=UNO-DUE, semibreve=UNO-DUE-TRE-QUATTRO, croma/semicroma=UN). Pause multi-beat divise in settori allineati ai settori grigi del pentagramma.

## [1.1.8] - 2026-09-12

### Corretto
- Travature (beam) mancanti nelle battute dopo pause multi-battuta collassate: il file ricostruito può avere meno battute dell'originale (es. 139 vs 142), quindi il match per numero di battuta sfasava l'iniezione dei beam e MuseScore applicava l'auto-beaming (crome staccate). Ora le battute vengono allineate globalmente per firma (rest/durata) con SequenceMatcher, sia per input .mscx che .mxl.

## [1.1.9] - 2026-09-12

### Modificato
- Travature (beam) più spesse: beamWidth aumentato da 0.5 a 0.8 nel template di stile. Le travature si confondevano con le linee del pentagramma, ora risaltano visivamente (leggibilità per dislessici).

## [1.2.0] - 2026-09-13

### Corretto
- Stanghette staccate dalle travature inclinate: il FIX #151 restringeva lo spessore della travatura usando il centro del bounding-box globale, appiattendo le travature inclinate. Ora i due bordi verticali vengono clampati indipendentemente preservando l'inclinazione originale, così le stanghette arrivano sempre fino alla travatura.

## [1.2.1] - 2026-09-14

### Corretto
- **Pausa di semiminima centrata nel settore grigio**: la pausa veniva
  spinta verso destra dal nudge anti-collisione; ora è centrata nella
  propria sezione grigia.
- **Gambo mancante su note ravvicinate**: con note molto vicine, il
  matching stem per X falliva; ora usa la vicinanza (nearest match).
- **Note sovrapposte nell'ultimo rigo**: battute di coda con righi
  parziali non vengono più compresse con sovrapposizioni.
- **Pause interamente dentro il proprio settore grigio**: clamp
  migliorato per le pause rispetto ai settori.
- **Canzon "solo ritmo" degradava a 1 battuta/rigo**: il retry
  anti-spezzatura calcolava il limite in settori come
  "battute reali × 2" (formula pensata per battute 2/4); con battute 4/4
  dava 4 settori = 1 sola battuta per rigo. Ora ×4 settori. Inoltre la
  pagina di rendering della modalità ritmo passa a 32 pollici: i righi da
  20 settori con battute dense (10+ note) venivano spezzati da MuseScore
  anche su pagina da 16.5".

### Modificato
- **Documentazione delle due versioni**: docstring, README e descrizione
  del repository spiegano ora le due uscite — notazione completa con
  pentagramma a dimensioni allargate (per lo studio) e "solo ritmo" più
  compressa (più battute per rigo, occupa meno spazio sulle partiture
  lunghe — utile come traccia durante i concerti).
