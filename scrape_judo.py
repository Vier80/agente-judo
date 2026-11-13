"""Agente Judo – risultati IJF (versione 2 del 13/11/2026, scheda 19, sessione 1)

Che cosa fa
-----------
Legge dal sito ufficiale della federazione mondiale (data.ijf.org) i risultati, incontro per
incontro, dei tornei senior individuali e li scrive nel database Notion "🥋 Agente Judo".
Per ogni torneo passa in rassegna le 14 categorie di peso (7 uomini, 7 donne).

Tornei inclusi (scheda 19, §5.1): Olimpiadi, Mondiali, Grand Slam, Grand Prix, Campionati
continentali e Giochi continentali (European, Asian, African, Pan American, Pacific,
Commonwealth, Mediterranean, Islamic Solidarity, Central American and Caribbean Games).
Esclusi: World Masters, Open e Coppe continentali, gare a squadre, kata, veterani, giovanili.

Per ogni incontro scrive anche chi ha vinto (colonna "Winner") e il codice IJF dei due judoka
("ID IJF 1" = bianco, "ID IJF 2" = blu): servono allo script ELO (sessione 2).
Gli incontri vinti per forfait, ritiro o decisione medica NON vengono scritti (§5.4).

Variabili d'ambiente (arrivano dal workflow)
--------------------------------------------
NOTION_TOKEN, NOTION_DATABASE_ID   i due segreti del repository
DRY_RUN=true                       "Solo prova": legge tutto, stampa le diagnosi, non scrive nulla
DATA_INIZIO / DATA_FINE            periodo dei tornei (AAAA-MM-GG). Se vuoti: ultimi 60 giorni / oggi
MODALITA                           "aggiungi" (default): scrive solo gli incontri nuovi
                                   "riscrivi": in più archivia le righe nel VECCHIO formato
                                   (senza Winner o senza ID IJF) dei tornei letti e le riscrive.
                                   Si può ripetere senza danni: le righe già nel formato nuovo
                                   non vengono toccate. Agisce SOLO sul db Agente Judo.
TEMPO_MAX_MINUTI                   dopo quanti minuti smettere di scrivere (default 165), per
                                   chiudere in ordine prima del limite di 180 minuti di GitHub
"""
import datetime as dt
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

import requests

# Orologio di partenza: serve per fermarsi in ordine prima del limite di tempo di GitHub.
AVVIO = time.monotonic()

# ------------------------------------------------------------------ impostazioni
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
# l'id del database si può incollare anche come link intero: ne estraggo i 32 caratteri
_raw_id = os.getenv("NOTION_DATABASE_ID", "").strip().replace("-", "")
_found = re.search(r"[0-9a-fA-F]{32}", _raw_id)
DATABASE_ID = _found.group(0) if _found else _raw_id
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() == "true"
MODALITA = os.getenv("MODALITA", "").strip().lower() or "aggiungi"

OGGI = dt.date.today()
GIORNI_DEFAULT = 60   # periodo letto quando DATA_INIZIO è vuota (lancio settimanale)
DATA_INIZIO = os.getenv("DATA_INIZIO", "").strip() or (OGGI - dt.timedelta(days=GIORNI_DEFAULT)).isoformat()
DATA_FINE = os.getenv("DATA_FINE", "").strip() or OGGI.isoformat()
try:
    TEMPO_MAX_MINUTI = float(os.getenv("TEMPO_MAX_MINUTI", "").strip() or 165)
except ValueError:
    TEMPO_MAX_MINUTI = 165.0

PAUSA_NOTION = 0.35      # secondi tra una scrittura e l'altra (Notion accetta circa 3 richieste al secondo)
SECONDI_PER_RIGA = 0.75  # stima del tempo reale per riga (pausa + risposta di Notion), per il preventivo

API = "https://data.ijf.org/api/get_json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Nomi ESATTI delle colonne del db Agente Judo (scheda 19 §4 e 01_schema_notion §5).
# Il codice li cerca senza distinguere maiuscole e minuscole.
# La colonna titolo ("Incontro") viene riconosciuta da sola perché è l'unica di tipo titolo.
COLUMNS = {
    "p1": ["Player 1"],          # judoka in bianco
    "noc1": ["NOC 1"],
    "id1": ["ID IJF 1"],        # id_person IJF del bianco
    "p2": ["Player 2"],         # judoka in blu
    "noc2": ["NOC 2"],
    "id2": ["ID IJF 2"],        # id_person IJF del blu
    "winner": ["Winner"],       # nome del vincitore, uguale a Player 1 o Player 2
    "gender": ["Gender"],
    "weight": ["Categoria"],
    "score": ["Score"],
    "round": ["Turno"],
    "tournament": ["Torneo"],
    "kind": ["Tipo Torneo"],
    "date": ["Data"],
    "duration": ["Durata"],
    "source": ["Fonte"],
    "id": ["ID Univoco"],
}
OPTIONAL = {"duration", "source"}  # se mancano si lavora lo stesso; tutte le altre sono obbligatorie

# categorie di peso: id_weight dell'IJF -> (genere, nome)
WEIGHTS = {1: ("Men", "-60"), 2: ("Men", "-66"), 3: ("Men", "-73"), 4: ("Men", "-81"),
           5: ("Men", "-90"), 6: ("Men", "-100"), 7: ("Men", "+100"),
           8: ("Women", "-48"), 9: ("Women", "-52"), 10: ("Women", "-57"), 11: ("Women", "-63"),
           12: ("Women", "-70"), 13: ("Women", "-78"), 14: ("Women", "+78")}

# Turni previsti (scheda 19 §5.5). Quelli diversi vengono segnalati nel log (punto 4 del §6).
TURNI_PREVISTI = ["Round of 128", "Round of 64", "Round of 32", "Round of 16", "Quarter-Final",
                  "Repechage", "Semi-Final", "Bronze", "Final"]

# Giochi continentali ammessi (scheda 19 §5.1): parole cercate nel nome o nel rank_name del torneo.
GIOCHI_AMMESSI = (
    "european games", "asian games", "african games",
    "pan american games", "panamerican games", "pan-american games",
    "pacific games", "commonwealth games", "mediterranean games",
    "islamic solidarity games",
    "central american and caribbean games", "central american & caribbean games",
)

# Codici di atleti neutrali o della squadra rifugiati (servono solo per la diagnosi del §6 punto 1).
NEUTRALI = {"IJF", "AIN", "IRT", "EOR", "NEU", "ROT"}

# Nomi di campi IJF che potrebbero indicare forfait, ritiri o decisioni mediche (§6 punto 5).
CAMPI_FORFAIT = re.compile(r"fusen|kiken|forfeit|walk.?over|withdr|medic|injur", re.I)
# Nomi di campi da mostrare nella diagnosi: tipo di esito, stato, ecc.
CAMPI_SOSPETTI = re.compile(r"fusen|kiken|forfeit|walk|withdr|medic|injur|win.?type|type.?win|"
                            r"result|status|decision|hantei|ending|reason", re.I)


def die(msg):
    print(f"ERRORE: {msg}")
    sys.exit(1)


def minuti_trascorsi():
    return (time.monotonic() - AVVIO) / 60


def tempo_scaduto():
    """Vero quando è ora di smettere di scrivere, per non essere interrotti a metà da GitHub."""
    return minuti_trascorsi() >= TEMPO_MAX_MINUTI


# ------------------------------------------------------------------ diagnosi
# Contenitore unico delle informazioni che la "Solo prova" deve mostrare per chiudere
# i punti 1, 3, 4, 5 e 6 del §6 della scheda 19.
DIAG = {
    "turni": Counter(),            # nome del turno -> numero di incontri
    "esclusi": Counter(),          # motivo di esclusione di un incontro -> quanti
    "esempi_forfait": [],          # alcuni incontri esclusi perché il punteggio non spiega la vittoria
    "campi_sospetti": defaultdict(Counter),  # campo IJF -> valori visti
    "campi_contest": None,         # elenco dei campi di un incontro (una volta sola)
    "neutrali": [],                # esempi di campi "nazione" per atleti neutrali
    "neutrali_conteggio": Counter(),
}


# ------------------------------------------------------------------ dati IJF
def api(**params):
    """Chiama l'interfaccia dati IJF; riprova fino a 4 volte se il sito è lento."""
    q = {f"params[{k}]": v for k, v in params.items()}
    last = None
    for attempt in range(4):
        try:
            r = SESSION.get(API, params=q, timeout=60)
            if r.status_code >= 500:
                raise RuntimeError(f"stato {r.status_code}")
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 + 2 * attempt)
    raise RuntimeError(f"interfaccia IJF non raggiungibile ({last})")


def clean(v):
    """Toglie spazi doppi e a capo da un testo; restituisce "" se il valore non è un testo."""
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else ""


def parse_date(s):
    """'2026/10/04' o '2026-10-04 ...' -> '2026-10-04'; '' se non riconosciuta."""
    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _n(v):
    """Numero intero da un valore IJF (che può essere testo, None o numero)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def classify(c):
    """Decide se un torneo va letto.

    Restituisce (tipo, None) se il torneo è incluso, con tipo = valore di "Tipo Torneo",
    oppure (None, motivo) se è escluso. L'ordine dei controlli conta: prima le esclusioni,
    poi i tipi dal più importante al meno importante.
    """
    rank = clean(c.get("rank_name")).lower()
    name = clean(c.get("name")).lower()
    testo = f"{rank} | {name}"

    # 1. esclusioni
    if str(c.get("is_teams")) == "1":
        return None, "gara a squadre"
    ages = c.get("ages") or []
    if isinstance(ages, str):
        ages = [ages]
    if ages and "sen" not in [str(a).lower() for a in ages]:
        return None, "non senior"
    if re.search(r"junior|cadet|veteran|youth|u23|u21|u18|universi|military|school", testo):
        return None, "non senior (dal nome)"
    if "kata" in testo or "training camp" in testo or "seminar" in testo:
        return None, "kata / stage"
    if "masters" in testo:
        return None, "World Masters (escluso per scelta)"

    # 2. tipi inclusi, dal più importante
    if "olympic games" in testo or rank.startswith("olympic"):
        return "Olimpiadi", None
    if "world championships" in testo:
        return "Mondiali", None
    if any(g in testo for g in GIOCHI_AMMESSI):
        return "Giochi continentali", None
    if "grand slam" in testo:
        return "Grand Slam", None
    if "grand prix" in testo:
        return "Grand Prix", None
    # i campionati continentali veri e propri hanno "Championships" nel nome
    if "championship" in name:
        if clean(c.get("continent_short")).upper() not in ("", "IJF"):
            return "Campionati continentali", None
        return None, "campionato non continentale"
    if "games" in testo:
        return None, "Giochi non compresi nell'elenco"
    if "open" in testo or "cup" in testo or "continental" in rank:
        return None, "Open / Coppa continentale"
    return None, "tipo non riconosciuto"


def list_competitions():
    """Tornei con data d'inizio tra DATA_INIZIO e DATA_FINE: inclusi ed esclusi."""
    seen, chosen, skipped = set(), [], []
    for year in range(int(DATA_INIZIO[:4]), int(DATA_FINE[:4]) + 1):
        data = api(action="competition.get_list", year=year)
        if not isinstance(data, list):
            raise RuntimeError(f"elenco tornei {year} in formato inatteso: {str(data)[:200]}")
        for c in data:
            cid = str(c.get("id_competition"))
            if cid in seen:
                continue
            seen.add(cid)
            d_from = parse_date(c.get("date_from"))
            if not d_from or d_from < DATA_INIZIO or d_from > DATA_FINE or d_from > OGGI.isoformat():
                continue
            kind, motivo = classify(c)
            if kind:
                chosen.append((kind, c))
            else:
                skipped.append((motivo, c))
    chosen.sort(key=lambda kc: parse_date(kc[1].get("date_from")))
    skipped.sort(key=lambda kc: parse_date(kc[1].get("date_from")))
    return chosen, skipped


def fetch_contests(comp_id, id_weight):
    data = api(action="contest.find", id_competition=comp_id, id_weight=id_weight, limit=1000)
    items = data.get("contests") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def side_score(c, suffix):
    """Punteggio di un lato nel formato del db: es. 'I 1W 2S' (suffix 'w' = bianco, 'b' = blu)."""
    parts = []
    if _n(c.get(f"ippon_{suffix}")) > 0:
        parts.append("I")
    if _n(c.get(f"waza_{suffix}")) > 0:
        parts.append(f"{_n(c.get(f'waza_{suffix}'))}W")
    if _n(c.get(f"yuko_{suffix}")) > 0:
        parts.append(f"{_n(c.get(f'yuko_{suffix}'))}Y")
    if _n(c.get(f"penalty_{suffix}")) > 0:
        parts.append(f"{_n(c.get(f'penalty_{suffix}'))}S")
    if _n(c.get(f"hsk_{suffix}")) > 0:
        parts.append("HM")
    return " ".join(parts) or "0"


def spiegazione_vittoria(c, win, lose):
    """Dice se il punteggio spiega la vittoria (win/lose = 'w' oppure 'b').

    Una vittoria "vera" ha almeno una di queste cause:
    - lo sconfitto ha hansoku-make o 3 shido;
    - il vincitore ha ippon;
    - il vincitore ha più waza-ari, oppure stessi waza-ari e più yuko.
    Se nessuna è vera, l'esito non viene dal combattimento: quasi sempre forfait (fusen-gachi),
    ritiro per infortunio (kiken-gachi) o decisione medica. Restituisce il motivo oppure None.
    """
    if _n(c.get(f"hsk_{lose}")) > 0 or _n(c.get(f"penalty_{lose}")) >= 3:
        return "squalifica dell'avversario"
    if _n(c.get(f"ippon_{win}")) > 0:
        return "ippon"
    wz, lz = _n(c.get(f"waza_{win}")), _n(c.get(f"waza_{lose}"))
    if wz > lz:
        return "waza-ari"
    if wz == lz and _n(c.get(f"yuko_{win}")) > _n(c.get(f"yuko_{lose}")):
        return "yuko"
    return None


def fighter(c, color):
    """(nome 'COGNOME Nome', NOC, id_person IJF) del judoka bianco o blu."""
    fam, giv = clean(c.get(f"family_name_{color}")), clean(c.get(f"given_name_{color}"))
    name = f"{fam} {giv}".strip() or clean(c.get(f"person_{color}"))
    pid = str(c.get(f"id_person_{color}") or "").strip()
    return name, clean(c.get(f"country_short_{color}")), pid


def contest_date(c, comp, tz):
    """Giorno dell'incontro (fuso orario della sede); in caso di dubbio il primo giorno del torneo."""
    d_from, d_to = parse_date(comp.get("date_from")), parse_date(comp.get("date_to"))
    ts = c.get("date_start_ts")
    try:
        t = int(ts)
        t = t / 1000 if t > 10 ** 11 else t
        day = dt.datetime.fromtimestamp(t, tz).date().isoformat()
        if d_from and d_to and d_from <= day <= d_to:
            return day
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return parse_date(c.get("competition_date")) or d_from


def osserva_campi(c):
    """Raccoglie per la diagnosi i nomi dei campi e i valori di quelli 'sospetti'."""
    if DIAG["campi_contest"] is None:
        DIAG["campi_contest"] = sorted(c.keys())
    for k, v in c.items():
        if CAMPI_SOSPETTI.search(k) and (v is None or isinstance(v, (str, int, float, bool))):
            testo = str(v)
            if len(testo) <= 40:
                DIAG["campi_sospetti"][k][testo] += 1
    for color in ("white", "blue"):
        noc = clean(c.get(f"country_short_{color}")).upper()
        if noc in NEUTRALI:
            DIAG["neutrali_conteggio"][noc] += 1
            nome = fighter(c, color)[0]
            if len(DIAG["neutrali"]) < 8 and nome not in {n for n, _ in DIAG["neutrali"]}:
                # tutti i campi del judoka che parlano di nazione: cerchiamo la nazione d'origine
                campi = {k: v for k, v in c.items()
                         if color in k and re.search(r"country|nation|ioc|noc|flag", k, re.I)}
                DIAG["neutrali"].append((nome, campi))


def contest_to_row(c, comp, kind, tz):
    """Trasforma un incontro IJF in una riga per Notion; None (con il motivo in DIAG) se va escluso."""
    osserva_campi(c)
    if str(c.get("bye")) == "1":
        DIAG["esclusi"]["bye"] += 1
        return None
    if str(c.get("is_finished")) != "1":
        DIAG["esclusi"]["incontro non concluso"] += 1
        return None
    age = clean(c.get("age"))
    if age and age.lower() not in ("seniors", "senior"):
        DIAG["esclusi"]["incontro non senior"] += 1
        return None
    w_name, w_noc, w_id = fighter(c, "white")
    b_name, b_noc, b_id = fighter(c, "blue")
    if not w_name or not b_name:
        DIAG["esclusi"]["manca il nome di un judoka"] += 1
        return None

    # chi ha vinto: lo dice id_winner, confrontato con i due id_person
    wid = str(c.get("id_winner") or "")
    if wid and wid == w_id:
        winner, win, lose = w_name, "w", "b"
    elif wid and wid == b_id:
        winner, win, lose = b_name, "b", "w"
    else:
        DIAG["esclusi"]["vincitore non indicato"] += 1
        return None

    gender, weight = WEIGHTS.get(_n(c.get("id_weight")), ("", ""))
    weight = (weight or clean(c.get("weight"))) + " kg"
    score = f"{side_score(c, 'w')} - {side_score(c, 'b')}"
    if str(c.get("gs")) == "1":
        score += " (GS)"
    rnd = clean(c.get("round_name"))
    duration = re.sub(r"^00:", "", clean(c.get("duration")))

    # forfait, ritiri e decisioni mediche: esclusi (scheda 19 §5.4)
    flag = next((k for k, v in c.items() if CAMPI_FORFAIT.search(k) and str(v).strip() not in ("", "0", "None", "False")), None)
    motivo = None
    if flag:
        motivo = f"segnalato dal campo IJF '{flag}'"
    elif spiegazione_vittoria(c, win, lose) is None:
        motivo = "il punteggio non spiega la vittoria"
    if motivo:
        DIAG["esclusi"]["forfait / ritiro / decisione medica"] += 1
        if len(DIAG["esempi_forfait"]) < 25:
            DIAG["esempi_forfait"].append(
                f"{clean(comp.get('name'))[:38]} | {weight} | {rnd} | {w_name} vs {b_name} | "
                f"{score} | durata {duration or '?'} | vince {winner} | {motivo}")
        return None

    DIAG["turni"][rnd or "(vuoto)"] += 1
    cid = str(comp.get("id_competition"))
    return {
        "id": f"{cid}|{c.get('id_fight')}",
        "p1": w_name, "noc1": w_noc, "id1": w_id,
        "p2": b_name, "noc2": b_noc, "id2": b_id,
        "winner": winner,
        "gender": gender, "weight": weight, "score": score,
        "round": rnd,
        "tournament": clean(comp.get("name")), "kind": kind,
        "date": contest_date(c, comp, tz),
        "duration": duration,
        "source": f"https://www.ijf.org/competition/{cid}/contests",
    }


def tz_for(comp_id):
    """Fuso orario della sede del torneo (serve per il giorno giusto degli incontri)."""
    try:
        info = api(action="competition.info", id_competition=comp_id)
        return ZoneInfo(info.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def collect_competition(kind, comp):
    """Tutti gli incontri validi di un torneo, categoria per categoria."""
    cid = comp["id_competition"]
    tz = tz_for(cid)
    rows, letti = {}, 0
    for wid, (gender, wname) in WEIGHTS.items():
        items = fetch_contests(cid, wid)
        letti += len(items)
        for c in items:
            r = contest_to_row(c, comp, kind, tz)
            if r:
                rows[r["id"]] = r
        if len(items) >= 1000:
            print(f"    ATTENZIONE: {gender} {wname}: raggiunto il limite di 1000 incontri")
    return list(rows.values()), letti


# ------------------------------------------------------------------ Notion
def notion(method, path, **kwargs):
    """Chiamata all'API Notion; aspetta e riprova se Notion è occupato (429) o in errore (5xx)."""
    r = None
    for attempt in range(5):
        r = requests.request(method, "https://api.notion.com/v1" + path,
                             headers=NOTION_HEADERS, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
            continue
        break
    if not r.ok:
        raise RuntimeError(f"Notion ha risposto {r.status_code}: {r.text[:400]}")
    return r.json()


def read_schema():
    """Legge le colonne del db e le associa ai dati. Si ferma se manca una colonna obbligatoria."""
    props = notion("GET", f"/databases/{DATABASE_ID}")["properties"]
    titles = [(n, p["type"]) for n, p in props.items() if p["type"] == "title"]
    by_name = {n.strip().lower(): (n, p["type"]) for n, p in props.items() if p["type"] != "title"}
    mapping, missing = {}, []
    for key, options in COLUMNS.items():
        hit = next((by_name[o.lower()] for o in options if o.lower() in by_name), None)
        if hit:
            mapping[key] = hit
        elif key in OPTIONAL:
            print(f"Nota: nel database non c'è la colonna '{options[0]}': quel dato non verrà scritto.")
        else:
            missing.append(options[0])
    if missing:
        die("nel database non trovo queste colonne: " + ", ".join(missing)
            + ". Colonne presenti: " + ", ".join(props.keys())
            + ". (Se una di queste è la colonna titolo, va rinominata: il titolo deve essere 'Incontro'.)")
    mapping["title"] = titles[0]
    print(f"Colonne trovate in Notion: titolo '{titles[0][0]}' + "
          + ", ".join(f"{n} ({t})" for k, (n, t) in mapping.items() if k != "title"))
    return mapping


def testo_prop(prop):
    """Testo semplice di una proprietà Notion (titolo o testo)."""
    parts = prop.get("rich_text") or prop.get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def leggi_righe_esistenti(mapping):
    """Tutte le righe del db Agente Judo.

    Restituisce un dizionario  ID Univoco -> {"pagine": [id pagina, ...], "completa": vero/falso}.
    "completa" = la riga è già nel formato nuovo (Winner, ID IJF 1 e ID IJF 2 compilati).
    """
    col_id = mapping["id"][0]
    col_w, col_1, col_2 = mapping["winner"][0], mapping["id1"][0], mapping["id2"][0]
    righe, senza_id, cursor = {}, 0, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion("POST", f"/databases/{DATABASE_ID}/query", json=body)
        for page in data["results"]:
            p = page["properties"]
            uid = testo_prop(p.get(col_id, {}))
            if not uid:
                senza_id += 1
                continue
            completa = all(testo_prop(p.get(c, {})) for c in (col_w, col_1, col_2))
            info = righe.setdefault(uid, {"pagine": [], "completa": True})
            info["pagine"].append(page["id"])
            info["completa"] = info["completa"] and completa
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if senza_id:
        print(f"Nota: {senza_id} righe in Notion non hanno 'ID Univoco': lo script le lascia stare.")
    return righe


def notion_value(ptype, value):
    """Converte un valore nel formato che Notion vuole per quel tipo di colonna."""
    value = "" if value is None else str(value)
    if ptype == "title":
        return {"title": [{"text": {"content": value[:2000]}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": value[:2000]}}] if value else []}
    if ptype == "select":
        return {"select": {"name": value.replace(",", " ")[:100]} if value else None}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "url":
        return {"url": value or None}
    raise RuntimeError(f"Tipo di colonna non gestito: {ptype}. Usa testo, selezione, data o URL.")


def create_row(mapping, m):
    values = dict(m)
    values["title"] = f"{m['p1']} vs {m['p2']}"   # colonna "Incontro"
    props = {name: notion_value(ptype, values.get(key, "")) for key, (name, ptype) in mapping.items()}
    notion("POST", "/pages", json={"parent": {"database_id": DATABASE_ID}, "properties": props})


def archivia(page_id):
    """Sposta una riga del db Agente Judo nel cestino di Notion (recuperabile per 30 giorni)."""
    notion("PATCH", f"/pages/{page_id}", json={"archived": True})


# ------------------------------------------------------------------ stampe
def stampa_diagnosi(skipped):
    print("\n" + "=" * 78)
    print("DIAGNOSI (servono a chiudere i punti aperti del §6 della scheda 19)")
    print("=" * 78)

    print("\n[Punto 3] Giochi e tornei 'Games' trovati nel periodo:")
    games = [(m, c) for m, c in skipped if "games" in f"{c.get('rank_name')} {c.get('name')}".lower()]
    if games:
        for m, c in games:
            print(f"  - ESCLUSO ({m}) | rank='{clean(c.get('rank_name'))}' | {c.get('name')}")
    else:
        print("  nessun torneo 'Games' escluso (quelli inclusi sono nell'elenco dei tornei inclusi).")

    print("\n[Punto 4] Turni trovati negli incontri validi (* = turno non previsto):")
    for t, n in DIAG["turni"].most_common():
        segno = " " if t in TURNI_PREVISTI else "*"
        print(f"  {segno} {t:<22} {n:>6}")

    print("\n[Punto 5] Incontri esclusi per motivo:")
    for m, n in DIAG["esclusi"].most_common():
        print(f"  {m:<40} {n:>6}")
    if DIAG["esempi_forfait"]:
        print("  Esempi di incontri esclusi come forfait / ritiro / decisione medica:")
        for e in DIAG["esempi_forfait"]:
            print(f"    · {e}")
    print("  Campi IJF che potrebbero indicare il tipo di esito (nome campo: valori visti):")
    if DIAG["campi_sospetti"]:
        for k, vals in sorted(DIAG["campi_sospetti"].items()):
            print(f"    {k}: " + ", ".join(f"{v!r}×{n}" for v, n in vals.most_common(10)))
    else:
        print("    nessuno")
    if DIAG["campi_contest"]:
        print("  Tutti i campi di un incontro IJF:")
        print("    " + ", ".join(DIAG["campi_contest"]))

    print("\n[Punto 1] Atleti neutrali o rifugiati negli incontri:",
          ", ".join(f"{k} {v}" for k, v in DIAG["neutrali_conteggio"].items()) or "nessuno")
    for nome, campi in DIAG["neutrali"]:
        print(f"    {nome}: {campi}")


# ------------------------------------------------------------------ main
def main():
    # controlli sulle impostazioni
    for nome, valore in (("DATA_INIZIO", DATA_INIZIO), ("DATA_FINE", DATA_FINE)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valore):
            die(f"{nome} deve essere nel formato AAAA-MM-GG (hai scritto '{valore}').")
    if DATA_FINE < DATA_INIZIO:
        die("DATA_FINE è prima di DATA_INIZIO.")
    if MODALITA not in ("aggiungi", "riscrivi"):
        die(f"MODALITA deve essere 'aggiungi' o 'riscrivi' (hai scritto '{MODALITA}').")
    if not DRY_RUN and (not NOTION_TOKEN or not DATABASE_ID):
        die("mancano NOTION_TOKEN o NOTION_DATABASE_ID (controlla i Secrets su GitHub).")

    print(f"{'SOLO PROVA: non scrivo nulla su Notion.' if DRY_RUN else 'SCRITTURA su Notion.'} "
          f"Modalità: {MODALITA}. Periodo: dal {DATA_INIZIO} al {DATA_FINE}.")

    # 1. stato del db Agente Judo (anche in prova, se il token c'è: è solo lettura)
    mapping, esistenti = None, {}
    if NOTION_TOKEN and DATABASE_ID:
        try:
            mapping = read_schema()
            esistenti = leggi_righe_esistenti(mapping)
            vecchie = sum(1 for v in esistenti.values() if not v["completa"])
            print(f"Righe già presenti in Notion: {len(esistenti)}, di cui nel vecchio formato "
                  f"(senza Winner o ID IJF): {vecchie}")
            if vecchie and MODALITA == "aggiungi":
                print("  (le righe vecchie restano come sono: per riscriverle usa la modalità 'riscrivi')")
        except SystemExit:
            if not DRY_RUN:
                raise
            print("In prova continuo comunque, ma prima del lancio vero le colonne vanno sistemate.")
        except RuntimeError as e:
            if not DRY_RUN:
                raise
            print(f"Non riesco a leggere il db Notion ({e}). In prova continuo senza.")
    else:
        print("Token Notion assente: in prova non leggo il db.")

    # 2. elenco dei tornei
    chosen, skipped = list_competitions()
    print(f"\nTornei inclusi: {len(chosen)}")
    for kind, c in chosen:
        print(f"  + {parse_date(c.get('date_from'))} | {kind:23} | {c.get('name')} "
              f"(id {c.get('id_competition')}, rank '{clean(c.get('rank_name'))}')")
    giovanili = [x for x in skipped if x[0].startswith("non senior")]
    altri = [x for x in skipped if not x[0].startswith("non senior")]
    print(f"Tornei esclusi: {len(skipped)} (di cui {len(giovanili)} giovanili o veterani, non elencati)")
    for motivo, c in altri:
        print(f"  - {parse_date(c.get('date_from'))} | {motivo:32} | rank='{clean(c.get('rank_name'))}' | "
              f"teams={c.get('is_teams')} | {c.get('name')} (id {c.get('id_competition')})")

    # 3. lettura dei tornei e scrittura
    errori = scritte = archiviate = 0
    da_scrivere_tot = da_archiviare_tot = 0
    letti_tot = validi_tot = 0
    interrotto = False
    tornei_letti = set()
    for kind, comp in chosen:
        if not DRY_RUN and tempo_scaduto():
            interrotto = True
            break
        cid = str(comp.get("id_competition"))
        print(f"\nLeggo {comp.get('name')} (id {cid}, {kind})")
        try:
            matches, letti = collect_competition(kind, comp)
        except Exception as e:
            print(f"  Torneo non leggibile: {e}")
            errori += 1
            continue
        tornei_letti.add(cid)
        letti_tot += letti
        validi_tot += len(matches)

        # righe vecchie di questo torneo da archiviare (solo in modalità riscrivi)
        vecchie = set()
        if MODALITA == "riscrivi" and matches:
            vecchie = {uid for uid, v in esistenti.items() if uid.startswith(cid + "|") and not v["completa"]}
        # incontri già in Notion da saltare: tutti, tranne le righe vecchie che stanno per essere riscritte
        presenti = {uid for uid in esistenti if uid not in vecchie}
        nuovi = [m for m in matches if m["id"] not in presenti]
        da_scrivere_tot += len(nuovi)
        da_archiviare_tot += len(vecchie)
        print(f"  Incontri nei dati IJF: {letti}, validi: {len(matches)}, da scrivere: {len(nuovi)}"
              + (f", righe vecchie da archiviare: {len(vecchie)}" if MODALITA == "riscrivi" else ""))

        if DRY_RUN:
            if matches:
                m = matches[0]
                print(f"  esempio: [{m['gender']}] {m['weight']} | {m['round']} | {m['p1']} ({m['noc1']}, id {m['id1']}) "
                      f"vs {m['p2']} ({m['noc2']}, id {m['id2']}) | {m['score']} | vince {m['winner']} | "
                      f"{m['date']} | {m['duration']}")
            continue

        # archiviazione delle righe vecchie del torneo (solo db Agente Judo, mai Athletes)
        for uid in vecchie:
            for page_id in esistenti[uid]["pagine"]:
                try:
                    archivia(page_id)
                    archiviate += 1
                    time.sleep(PAUSA_NOTION)
                except RuntimeError as e:
                    print(f"  Errore nell'archiviare la riga {uid}: {e}")
                    errori += 1
            esistenti.pop(uid, None)

        # scrittura degli incontri nuovi
        for m in nuovi:
            if tempo_scaduto():
                interrotto = True
                break
            try:
                create_row(mapping, m)
                esistenti[m["id"]] = {"pagine": [], "completa": True}
                scritte += 1
                if scritte % 100 == 0:
                    print(f"  ... scritte {scritte} righe ({minuti_trascorsi():.0f} minuti)", flush=True)
                time.sleep(PAUSA_NOTION)
            except RuntimeError as e:
                print(f"  Errore su {m['p1']} vs {m['p2']}: {e}")
                errori += 1
        if interrotto:
            break

    # righe vecchie di tornei non letti in questa esecuzione (solo un avviso)
    vecchie_fuori = [uid for uid, v in esistenti.items()
                     if not v["completa"] and uid.split("|")[0] not in tornei_letti]

    # 4. riepilogo
    stampa_diagnosi(skipped)
    print("\n" + "=" * 78)
    print("RIEPILOGO")
    print("=" * 78)
    print(f"Tornei inclusi: {len(chosen)} · incontri nei dati IJF: {letti_tot} · validi: {validi_tot}")
    if DRY_RUN:
        minuti = (da_scrivere_tot + da_archiviare_tot) * SECONDI_PER_RIGA / 60
        print(f"[Punto 6] Righe da scrivere: {da_scrivere_tot}"
              + (f" · righe vecchie da archiviare: {da_archiviare_tot}" if MODALITA == "riscrivi" else "")
              + f" · tempo stimato in Notion: circa {minuti:.0f} minuti (limite per lancio: {TEMPO_MAX_MINUTI:.0f}).")
        if minuti > TEMPO_MAX_MINUTI:
            print(f"  Servono più lanci: lo script si ferma da solo a {TEMPO_MAX_MINUTI:.0f} minuti "
                  f"e il lancio successivo riparte da dove si era fermato.")
        if not mapping and NOTION_TOKEN:
            print("ATTENZIONE: lo schema Notion non è a posto (vedi sopra).")
        print("\nProva terminata: non è stato scritto nulla su Notion.")
    else:
        print(f"Righe scritte: {scritte} · righe vecchie archiviate: {archiviate} · errori: {errori} · "
              f"minuti: {minuti_trascorsi():.0f}")
    if vecchie_fuori:
        print(f"Nota: restano {len(vecchie_fuori)} righe nel vecchio formato di tornei fuori dal periodo "
              f"o esclusi (prima parte degli ID: {', '.join(sorted({u.split('|')[0] for u in vecchie_fuori})[:10])}).")
    if interrotto:
        print(f"\nATTENZIONE: mi sono fermato dopo {TEMPO_MAX_MINUTI:.0f} minuti per non superare il limite di "
              f"GitHub. Rilancia con le stesse impostazioni: le righe già scritte vengono saltate.")
        sys.exit(1)
    if errori:
        sys.exit(1)


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception:
        traceback.print_exc()
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
