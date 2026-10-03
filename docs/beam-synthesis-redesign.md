# BeamSynthesizer — Redesign architetturale universale del beaming MaidaScore

Consulto Claude Fable 5, 3 Ottobre 2026. Sostituisce l'approccio patchwork (Pass 9-20g, ~2000 righe)
con una sintesi deterministica delle travature.

## Principio

**Le beams sono OUTPUT, non stato.** La geometria delle beams raw di MuseScore, dopo
equalizzazione X e y-stretch, è rumore: non va riparata, va ignorata. La semantica dei gruppi
viene dal .mscx (BeamMode: catene begin→mid→end; `none` o pausa interrompono), la geometria
dai gambi equalizzati (unica fonte di verità sulle posizioni, già corretti e non toccati).

## Contratto a tre fasi

1. **group()** — dal .mscx: eventi per (battuta, voce) → macchina a stati sui BeamMode →
   lista `BeamGroup`. Politica cross-battuta: default `split` sui sottogruppi (singleton → flag);
   `continue` solo se indicato esplicitamente. Cross-sistema = sempre split.
2. **bind()** — matching gruppi↔gambi SVG per (battuta, voce, banda X/Y), con **assert loudly**:
   se `#gambi SVG != #accordi mscx` → errore con contesto (battuta/voce), NON geometria sbagliata
   da riparare a valle. Tassonomia dei fallimenti con retry (finestra X ±tol, raddoppio) e
   fallback (gruppo non beamato + log strutturato). Il rendering non si interrompe mai.
3. **synthesize()** — geometria pura, non può fallire:
   - beam RETTA e ORIZZONTALE (MaidaScore: niente pendenza): y_prim = punta del gambo estremo
     (min per stems-up = la più alta; max per stems-down = la più bassa); spessore 47 verso le teste.
   - livelli secondari: `y_off(lvl) = dir * (lvl-1) * LEVEL_PITCH` (LEVEL_PITCH=15). Invariante:
     i livelli impilano DALL'anchor VERSO le teste. → **regola direzionale di Marco soddisfatta
     per costruzione**: gambo su = croma (liv 1) linea più alta; gambo giù = più bassa.
   - runs massimali per livello (find_runs): run ≥2 → segmento pieno [primo gambo−4.7 .. ultimo+4.7];
     run = 1 → hook: direzione orizzontale dal contesto (prima del gruppo → destra; ultima → sinistra;
     interna → punta alla nota che completa la suddivisione, tipico: croma puntata+semicroma → hook
     a sinistra). HOOK_LEN = clamp(0.6·Δx_vicino, 90, 130).
   - pausa interna: la primaria scavalca, le secondarie si interrompono, nota isolata → hook verso interno.
   - accordo: N teste = 1 gambo (dedup per stem_x).
   - path SVG: `M x1,y1 L x2,y1 L x2,y1+47 L x1,y1+47 Z`, class="Beam".
4. **validate()** — pass di SOLA VERIFICA (nessuna mutazione): beams dentro la banda Y del sistema
   (avrebbe intercettato il bug h1497), ogni nota beamable coperta, nessuna coppia sovrapposta,
   secondarie a distanza esatta LEVEL_PITCH. Fallimento = errore rumoroso, mai auto-fix.

## Costanti (pixel SVG equalizzato)

BEAM_THICKNESS=47, LEVEL_PITCH=15, OVERHANG=4.7, HOOK_LEN=clamp(0.6·Δx,90,130), TOL_X=3, TOL_TIP=2.

## Ordine di implementazione (3 commit, ognuno verificato su Radetsky prima del prossimo)

1. **Infrastruttura**: `strip_raw_beams()` + flag `use_beam_synthesizer` (default OFF, guardia UNICA
   intorno al blocco legacy) + punto di aggancio (dopo y-stretch, gambi definitivi, prima del footer).
   Verifica: flag OFF → byte-identico; flag ON → pagine senza beams, resto intatto.
2. **Primarie**: group+bind+synthesize solo livello 1. Verifica: side-by-side col legacy, primarie
   uguali o migliori; secondarie assenti (atteso).
3. **Secondarie+hook+switch default ON**: pass completo sul corpus, zero regressioni visive +
   difetti noti risolti. Legacy resta dietro flag per rollback.

## Regola per il futuro (da scrivere nel repo)

Ogni difetto trovato si corregge NELLA FUNZIONE DI GENERAZIONE (con nuovo caso nel corpus),
mai con un pass a valle. Nessun nuovo pass di riparazione.

## Suite di regressione (10 test sintetici, geometria attesa esatta)

T1 due crome | T2 4 semicrome prim+sec | T3 c+c+c.+s hook← | T4 s+c. hook→ | T5 cross-battuta
(split/continue) | T6 stems-down sec sopra | T7 accordo | T8 8 biscrome 3 livelli | T9 c+ss+c sec
solo tra le 16a | T10 pausa interna: prim scavalca, sec spezzate, hook su isolata.
