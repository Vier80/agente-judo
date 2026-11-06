"""Agente Judo (IJF)

Legge dal sito ufficiale della federazione mondiale (ijf.org) i risultati, incontro per
incontro, dei tornei senior individuali (Mondiali, Grand Slam, Grand Prix e campionati
continentali) e li scrive in un database Notion. Per ogni torneo passa in rassegna le 14
categorie di peso (7 uomini, 7 donne).

Variabili: NOTION_TOKEN, NOTION_DATABASE_ID.
Se DRY_RUN=true fa solo una prova: stampa i dati trovati senza scrivere su Notion.
Impostazioni facoltative (variabili d'ambiente): DATA_INIZIO (default 2026-10-04, giorno
d'inizio dei Mondiali di Baku), INCLUDI_OPEN=true per aggiungere anche Open e Coppe continentali.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
from zoneinfo import ZoneInfo

import requests

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
_raw_id = os.getenv("NOTION_DATABASE_ID", "").strip().replace("-", "")
_found = re.search(r"[0-9a-fA-F]{32}", _raw_id)
DATABASE_ID = _found.group(0) if _found else _raw_id
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() == "true"
DATA_INIZIO = os.getenv("DATA_INIZIO", "").strip() or "2026-10-04"
INCLUDI_OPEN = os.getenv("INCLUDI_OPEN", "").strip().lower() == "true"

API = "https://data.ijf.org/api/get_json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Nomi delle colonne nel database (maiuscole/minuscole non contano).
COLUMNS = {
    "p1": ["Player 1"],
    "noc1": ["NOC 1"],
    "p2": ["Player 2"],
    "noc2": ["NOC 2"],
    "gender": ["Gender"],
    "weight": ["Categoria"],
    "score": ["Score"],
    "winner": ["Vincitore"],
    "round": ["Turno"],
    "tournament": ["Torneo"],
    "kind": ["Tipo torneo", "Tipo"],
    "date": ["Data"],
    "duration": ["Durata"],
    "source": ["Fonte", "Fonte (link)", "Link"],
    "id": ["ID univoco", "ID"],
}
OPTIONAL = {"winner", "kind", "duration", "source"}  # se la colonna manca, si lavora lo stesso

# categorie di peso: id_weight dell'IJF -> (genere, nome)
WEIGHTS = {1: ("Men", "-60"), 2: ("Men", "-66"), 3: ("Men", "-73"), 4: ("Men", "-81"),
           5: ("Men", "-90"), 6: ("Men", "-100"), 7: ("Men", "+100"),
           8: ("Women", "-48"), 9: ("Women", "-52"), 10: ("Women", "-57"), 11: ("Women", "-63"),
           12: ("Women", "-70"), 13: ("Women", "-78"), 14: ("Women", "+78")}


def die(msg):
    print(f"ERRORE: {msg}")
    sys.exit(1)


# ------------------------------------------------------------- dati IJF
def api(**params):
    """Chiama l'interfaccia dati IJF; riprova se il sito e' lento."""
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
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else ""


def parse_date(s):
    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def classify(c):
    """Tipo di torneo da includere (testo) oppure None se va escluso."""
    if str(c.get("is_teams")) == "1":
        return None
    ages = c.get("ages") or []
    if isinstance(ages, str):
        ages = [ages]
    if ages and "sen" not in ages:
        return None
    rank = clean(c.get("rank_name")).lower()
    name = clean(c.get("name")).lower()
    if "kata" in name or "training camp" in name or "seminar" in name or "masters" in rank:
        return None
    if "world championships" in rank:
        return "Mondiali"
    if "grand slam" in rank:
        return "Grand Slam"
    if "grand prix" in rank:
        return "Grand Prix"
    # i campionati continentali veri e propri hanno "Championships" nel nome; Open e Coppe
    # continentali (es. Asian Open, European Cup) sono tornei minori e restano fuori
    # a meno che si imposti INCLUDI_OPEN=true
    if "championship" in name:
        return "Campionati continentali" if clean(c.get("continent_short")).upper() not in ("", "IJF") else None
    if "open" in rank or "open" in name or "cup" in rank or "cup" in name or "continental" in rank:
        return "Continental Open/Cup" if INCLUDI_OPEN else None
    return None


def list_competitions(today):
    """Tornei senior individuali da DATA_INIZIO a oggi, con tipo e motivo di esclusione."""
    start_year = int(DATA_INIZIO[:4])
    seen, chosen, skipped = set(), [], []
    for year in range(start_year, today.year + 1):
        data = api(action="competition.get_list", year=year)
        if not isinstance(data, list):
            raise RuntimeError(f"elenco tornei {year} in formato inatteso: {str(data)[:200]}")
        for c in data:
            cid = str(c.get("id_competition"))
            if cid in seen:
                continue
            seen.add(cid)
            d_from = parse_date(c.get("date_from"))
            if not d_from or d_from < DATA_INIZIO or d_from > today.isoformat():
                continue
            kind = classify(c)
            (chosen if kind else skipped).append((kind, c))
    chosen.sort(key=lambda kc: parse_date(kc[1].get("date_from")))
    return chosen, skipped


def fetch_contests(comp_id, id_weight):
    data = api(action="contest.find", id_competition=comp_id, id_weight=id_weight, limit=1000)
    items = data.get("contests") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def side_score(c, suffix):
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


def _n(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fighter(c, suffix):
    fam, giv = clean(c.get(f"family_name_{suffix}")), clean(c.get(f"given_name_{suffix}"))
    name = f"{fam} {giv}".strip() or clean(c.get(f"person_{suffix}"))
    return name, clean(c.get(f"country_short_{suffix}"))


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


def contest_to_row(c, comp, kind, tz):
    """Trasforma un incontro in una riga; None se non concluso, senza esito o non senior."""
    if str(c.get("is_finished")) != "1" or str(c.get("bye")) == "1":
        return None
    age = clean(c.get("age"))
    if age and age.lower() not in ("seniors", "senior"):
        return None
    w_name, w_noc = fighter(c, "white")
    b_name, b_noc = fighter(c, "blue")
    if not w_name or not b_name:
        return None
    wid = str(c.get("id_winner") or "")
    if wid and wid == str(c.get("id_person_white")):
        winner = w_name
    elif wid and wid == str(c.get("id_person_blue")):
        winner = b_name
    else:
        return None
    gender, weight = WEIGHTS.get(_n(c.get("id_weight")), ("", ""))
    weight = (weight or clean(c.get("weight"))) + " kg"
    score = f"{side_score(c, 'w')} - {side_score(c, 'b')}"
    if str(c.get("gs")) == "1":
        score += " (GS)"
    cid = str(comp.get("id_competition"))
    return {
        "id": f"{cid}|{c.get('id_fight')}",
        "p1": w_name, "noc1": w_noc, "p2": b_name, "noc2": b_noc,
        "gender": gender, "weight": weight, "score": score, "winner": winner,
        "round": clean(c.get("round_name")),
        "tournament": clean(comp.get("name")), "kind": kind,
        "date": contest_date(c, comp, tz),
        "duration": re.sub(r"^00:", "", clean(c.get("duration"))),
        "source": f"https://www.ijf.org/competition/{cid}/contests",
    }


def tz_for(comp_id):
    try:
        info = api(action="competition.info", id_competition=comp_id)
        return ZoneInfo(info.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def collect_competition(kind, comp):
    cid = comp["id_competition"]
    tz = tz_for(cid)
    rows, stats = {}, []
    for wid, (gender, wname) in WEIGHTS.items():
        items = fetch_contests(cid, wid)
        done = 0
        for c in items:
            r = contest_to_row(c, comp, kind, tz)
            if r:
                rows[r["id"]] = r
                done += 1
        if len(items) >= 1000:
            print(f"    ATTENZIONE: {gender} {wname}: raggiunto il limite di 1000 incontri")
        stats.append((gender, wname, len(items), done))
    return list(rows.values()), stats


# ------------------------------------------------------------------- Notion
def notion(method, path, **kwargs):
    r = None
    for attempt in range(4):
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
    props = notion("GET", f"/databases/{DATABASE_ID}")["properties"]
    by_name = {n.strip().lower(): (n, p["type"]) for n, p in props.items()}
    mapping, missing = {}, []
    for key, options in COLUMNS.items():
        hit = next((by_name[o.lower()] for o in options if o.lower() in by_name), None)
        if hit:
            mapping[key] = hit
        elif key in OPTIONAL:
            print(f"Nota: nel database non c'è la colonna '{options[0]}': quel dato non verrà scritto.")
        else:
            missing.append(options[0])
    titles = [(n, p["type"]) for n, p in props.items() if p["type"] == "title"]
    mapping["title"] = titles[0]
    if missing:
        die("nel database non trovo queste colonne: " + ", ".join(missing)
            + ". Colonne presenti: " + ", ".join(props.keys()))
    return mapping


def existing_ids(id_column):
    ids, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion("POST", f"/databases/{DATABASE_ID}/query", json=body)
        for page in data["results"]:
            prop = page["properties"].get(id_column, {})
            parts = prop.get("rich_text") or prop.get("title") or []
            text = "".join(p.get("plain_text", "") for p in parts)
            if text:
                ids.add(text)
        if not data.get("has_more"):
            return ids
        cursor = data["next_cursor"]


def notion_value(ptype, value):
    value = "" if value is None else str(value)
    if ptype == "title":
        return {"title": [{"text": {"content": value[:2000]}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": value[:2000]}}]}
    if ptype == "select":
        return {"select": {"name": value.replace(",", " ")[:100]} if value else None}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "url":
        return {"url": value or None}
    raise RuntimeError(f"Tipo di colonna non gestito: {ptype}. Usa testo, selezione, data o URL.")


def create_row(mapping, m):
    values = dict(m)
    values["title"] = f"{m['p1']} vs {m['p2']}"
    props = {name: notion_value(ptype, values.get(key, "")) for key, (name, ptype) in mapping.items()}
    notion("POST", "/pages", json={"parent": {"database_id": DATABASE_ID}, "properties": props})


# --------------------------------------------------------------------- main
def main():
    if not DRY_RUN and (not NOTION_TOKEN or not DATABASE_ID):
        die("mancano NOTION_TOKEN o NOTION_DATABASE_ID (controlla i Secrets su GitHub).")
    mapping, done = None, set()
    if not DRY_RUN:
        mapping = read_schema()
        done = existing_ids(mapping["id"][0])
        print(f"Righe già presenti in Notion: {len(done)}")
    else:
        print("MODALITÀ PROVA: non scrivo nulla su Notion.")

    today = dt.date.today()
    print(f"Tornei senior individuali dal {DATA_INIZIO} a oggi ({today.isoformat()}).")
    chosen, skipped = list_competitions(today)
    print(f"\nTornei inclusi: {len(chosen)}")
    for kind, c in chosen:
        print(f"  + {parse_date(c.get('date_from'))} | {kind:23} | {c.get('name')} (id {c.get('id_competition')})")
    print(f"Tornei esclusi (non senior individuali, o tipo non richiesto): {len(skipped)}")
    for _, c in skipped:
        print(f"  - {parse_date(c.get('date_from'))} | rank='{clean(c.get('rank_name'))}' | "
              f"ages={c.get('ages')} | teams={c.get('is_teams')} | {c.get('name')} (id {c.get('id_competition')})")

    errors = added = 0
    for kind, comp in chosen:
        print(f"\nLeggo {comp.get('name')} (id {comp.get('id_competition')})")
        try:
            matches, stats = collect_competition(kind, comp)
        except Exception as e:
            print(f"  Torneo non leggibile: {e}")
            errors += 1
            continue
        for gender, wname, total, ok in stats:
            if total:
                print(f"    {gender} {wname} kg: {total} incontri nei dati, {ok} conclusi")
        new = [m for m in matches if m["id"] not in done]
        print(f"  Incontri conclusi: {len(matches)}, nuovi: {len(new)}")
        if DRY_RUN:
            shown = {}
            for m in new:
                key = (m["gender"], m["weight"])
                if shown.get(key, 0) < 2:
                    shown[key] = shown.get(key, 0) + 1
                    print(f"  [{m['gender']}] {m['weight']} | {m['round']} | "
                          f"{m['p1']} ({m['noc1']}) vs {m['p2']} ({m['noc2']}) | {m['score']} | "
                          f"vince {m['winner']} | {m['date']} | {m['duration']}")
            continue
        for m in new:
            try:
                create_row(mapping, m)
                done.add(m["id"])
                added += 1
                if added % 50 == 0:
                    print(f"  ... scritte {added} righe", flush=True)
                time.sleep(0.4)
            except RuntimeError as e:
                print(f"  Errore su {m['p1']} vs {m['p2']}: {e}")
                errors += 1
    if DRY_RUN:
        print("\nProva terminata (per ogni categoria sono mostrati due incontri di esempio).")
    else:
        print(f"\nFatto. Righe aggiunte: {added}. Errori: {errors}.")
    if errors:
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
