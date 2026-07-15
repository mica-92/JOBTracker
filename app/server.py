"""
server.py  —  Flask API + sirve el frontend
Endpoints:
  GET  /                          -> app HTML
  GET  /api/dias                  -> lista de dias en la DB
  GET  /api/data?dia=YYYY-MM-DD   -> datos completos del dia (calculados)
  GET  /api/csv?dia=YYYY-MM-DD    -> descarga CSV del dia
  POST /api/marcacion             -> agregar marcacion manual
  PUT  /api/marcacion/<id>        -> editar marcacion (hora/lugar/tipo)
  DELETE /api/marcacion/<id>      -> soft-delete
"""

import os, sys, sqlite3, json, io, csv
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, jsonify, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "marcaciones.db")
HTML_PATH= os.path.join(BASE_DIR, "app.html")

app = Flask(__name__)

# ── CADENAS: variante → nombre canónico ──────────────────────────────────────
# Agregar nuevas variantes aquí. El valor es el nombre canónico del grupo.
CADENAS_MAP = {
    # CARREFOUR
    "CARREFOUR":          "CARREFOUR",
    "HIPER CARREFOUR":    "CARREFOUR",
    "CARREFOUR MAXI":     "CARREFOUR",
    "MAXICARREFOUR":      "CARREFOUR",
    "CARERFOUR":          "CARREFOUR",   # typo frecuente
    "CAREFOUR":           "CARREFOUR",   # typo frecuente
    # VEA
    "VEA":                "VEA",
    "PLAZA VEA":          "VEA",
    # WAL MART
    "WAL MART":           "WAL MART",
    "WALMART":            "WAL MART",
    # CHANGO MAS
    "CHANGO MAS":         "CHANGO MAS",
    "CHANGOMAS":          "CHANGO MAS",
    # RESTO (una sola variante conocida por ahora)
    "PUNTO MAYORISTA":    "PUNTO MAYORISTA",
    "CENTRAL OESTE":      "CENTRAL OESTE",
    "MAXICONSUMO":        "MAXICONSUMO",
    "TREOLAND":           "TREOLAND",
    "TORNADO":            "TORNADO",
    "DIARCO":             "DIARCO",
    "MAKRO":              "MAKRO",
    "JUMBO":              "JUMBO",
    "COTO":               "COTO",
    "DISCO":              "DISCO",
    "VITAL":              "VITAL",
    "METRO":              "METRO",
    "NINI":               "NINI",
    "YAGUAR":             "YAGUAR",
    "DIA":                "DIA",
}
# Ordenar prefijos de más largo a más corto para match correcto
_CADENAS_PREFIXES = sorted(CADENAS_MAP.keys(), key=len, reverse=True)

def detectar_cadena(lugar):
    lu = lugar.upper().strip()
    if lu in ("-", "", "FUERA DE RANGO"):
        return "FUERA DE RANGO"
    for prefix in _CADENAS_PREFIXES:
        if lu == prefix or lu.startswith(prefix + " ") or lu.startswith(prefix + "-"):
            return CADENAS_MAP[prefix]
    return "OTROS"

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def rows_for_day(dia):
    con = get_db()
    rows = con.execute("""
        SELECT id,usuario,dia,hora,lugar,cadena,tipo,manual
        FROM marcaciones
        WHERE dia=? AND deleted=0
        ORDER BY usuario,hora
    """,(dia,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── ANALISIS ──────────────────────────────────────────────────────────────────
def parse_time(s):
    return datetime.strptime(s.strip(),"%H:%M")

def fmt_dur(minutes):
    h,m = int(minutes)//60, int(minutes)%60
    return f"{h}h {m:02d}m"

def analyze_user(u_rows):
    """
    Builds a chronological timeline of:
      - LUGAR rows  : paired ENTRADA/SALIDA at a real place
      - FUERA rows  : paired ENTRADA/SALIDA where lugar=='-'  (anomalia)
      - INTERVALO   : gap between consecutive events (transit time, never anomalia)
      - UNPAIRED    : single ENTRADA or SALIDA with no partner (anomalia)

    timeline entry keys:
      kind       : 'lugar' | 'fuera' | 'intervalo' | 'unpaired'
      lugar      : place name (or '-' / '' for fuera/intervalo)
      e          : entrada hora  (HH:MM or None)
      s          : salida hora   (HH:MM or None)
      min        : duration in minutes
      is_anom    : bool
      anom_msg   : str  (if is_anom)
      ids        : list of DB row ids involved
    """
    u_rows   = sorted(u_rows, key=lambda x: x["hora"])
    anomalias = []
    timeline  = []
    total_mins = 0.0

    # ── Step 1: pair rows by lugar ────────────────────────────────────────────
    # We do this per-lugar first, building a list of closed segments,
    # then we reconstruct the chronological timeline including intervals.

    # Group by lugar preserving order
    lugar_groups = defaultdict(list)
    for r in u_rows:
        lugar_groups[r["lugar"]].append(r)

    # Build closed segments per lugar
    segments = []   # {kind, lugar, e_hora, s_hora, min, id_e, id_s, is_anom}
    unpaired = []   # rows without a partner

    for lugar, lr in lugar_groups.items():
        lr = sorted(lr, key=lambda x: x["hora"])
        i  = 0
        while i < len(lr):
            ri = lr[i]
            if ri["tipo"] == "ENTRADA":
                if i+1 < len(lr) and lr[i+1]["tipo"] == "SALIDA":
                    ro   = lr[i+1]
                    diff = (parse_time(ro["hora"]) - parse_time(ri["hora"])).total_seconds() / 60
                    diff = max(diff, 0)
                    is_fuera = (lugar == "-")
                    is_anom  = is_fuera or diff < 15
                    msg = ""
                    if is_fuera and diff < 15:
                        msg = f"Fuera de Rango < 15 min: ENTRADA {ri['hora']} - SALIDA {ro['hora']} ({diff:.0f} min)"
                    elif is_fuera:
                        msg = f"Fuera de Rango: ENTRADA {ri['hora']} - SALIDA {ro['hora']} ({fmt_dur(diff)})"
                    elif diff < 15:
                        msg = f"Marcacion < 15 min en '{lugar}': ENTRADA {ri['hora']} - SALIDA {ro['hora']} ({diff:.0f} min)"
                    segments.append({
                        "kind":   "fuera" if is_fuera else "lugar",
                        "lugar":  lugar,
                        "e":      ri["hora"], "s": ro["hora"], "min": diff,
                        "id_e":   ri["id"],   "id_s": ro["id"],
                        "is_anom": is_anom,   "anom_msg": msg,
                    })
                    if not is_fuera:
                        total_mins += diff
                    i += 2
                else:
                    # ENTRADA sin SALIDA
                    unpaired.append(ri)
                    i += 1
            else:
                # SALIDA sin ENTRADA
                unpaired.append(ri)
                i += 1

    # ── Step 2: sort segments chronologically by entrada hora ─────────────────
    segments.sort(key=lambda x: x["e"])

    # ── Step 3: insert INTERVALO between consecutive segments ─────────────────
    for idx, seg in enumerate(segments):
        timeline.append(seg)
        if idx < len(segments) - 1:
            next_seg = segments[idx + 1]
            gap_min  = (parse_time(next_seg["e"]) - parse_time(seg["s"])).total_seconds() / 60
            if gap_min > 0:
                timeline.append({
                    "kind":    "intervalo",
                    "lugar":   "",
                    "e":       seg["s"], "s": next_seg["e"], "min": gap_min,
                    "id_e":    None,     "id_s": None,
                    "is_anom": False,    "anom_msg": "",
                })

    # ── Step 4: handle unpaired rows ─────────────────────────────────────────
    for r in unpaired:
        is_entrada = r["tipo"] == "ENTRADA"
        is_fuera   = r["lugar"] == "-"
        if is_fuera:
            msg = (f"Fuera de Rango sin salida: ENTRADA {r['hora']}" if is_entrada
                   else f"Fuera de Rango sin entrada: SALIDA {r['hora']}")
        else:
            msg = (f"ENTRADA sin SALIDA en '{r['lugar']}' a las {r['hora']}" if is_entrada
                   else f"SALIDA sin ENTRADA en '{r['lugar']}' a las {r['hora']}")
        timeline.append({
            "kind":    "unpaired",
            "lugar":   r["lugar"],
            "e":       r["hora"] if is_entrada else None,
            "s":       r["hora"] if not is_entrada else None,
            "min":     0,
            "id_e":    r["id"] if is_entrada else None,
            "id_s":    r["id"] if not is_entrada else None,
            "is_anom": True,
            "anom_msg": msg,
        })
        anomalias.append({"msg": msg, "ids": [r["id"]]})

    # ── Step 5: collect anomalias from segments ───────────────────────────────
    for seg in segments:
        if seg["is_anom"] and seg["anom_msg"]:
            ids = [x for x in [seg["id_e"], seg["id_s"]] if x is not None]
            anomalias.append({"msg": seg["anom_msg"], "ids": ids})

    # ── Step 6: duplicates ────────────────────────────────────────────────────
    for i in range(len(u_rows) - 1):
        r1, r2 = u_rows[i], u_rows[i+1]
        if r1["hora"] == r2["hora"] and r1["lugar"] == r2["lugar"]:
            anomalias.append({
                "msg": f"Marcacion duplicada: {r1['hora']} en '{r1['lugar']}'",
                "ids": [r1["id"], r2["id"]]
            })

    # ── Dedup anomalias ───────────────────────────────────────────────────────
    seen, ua = set(), []
    for a in anomalias:
        if a["msg"] not in seen:
            seen.add(a["msg"]); ua.append(a)

    # ── Summary fields ────────────────────────────────────────────────────────
    entradas = [r for r in u_rows if r["tipo"] == "ENTRADA"]
    salidas  = [r for r in u_rows if r["tipo"] == "SALIDA"]
    pe = entradas[0] if entradas else None
    us = salidas[-1]  if salidas  else None

    if not pe: ua.insert(0, {"msg": "Sin ningun registro de entrada", "ids": []})
    if not us: ua.insert(0 if pe else 1, {"msg": "Sin ningun registro de salida", "ids": []})

    # lugar_times kept for tab3/tab1 summary (exclude fuera and intervals)
    lugar_times  = defaultdict(float)
    lugar_visits = defaultdict(list)
    for seg in segments:
        if seg["kind"] == "lugar":
            lugar_times[seg["lugar"]]  += seg["min"]
            lugar_visits[seg["lugar"]].append(seg)

    has_anomalia = len(ua) > 0

    return {
        "primer_entrada": {"hora": pe["hora"], "lugar": pe["lugar"]} if pe else None,
        "ultima_salida":  {"hora": us["hora"], "lugar": us["lugar"]} if us else None,
        "total":          len(u_rows),
        "total_mins":     total_mins,
        "has_anomalia":   has_anomalia,
        "lugar_times":    dict(sorted(lugar_times.items())),
        "lugar_visits":   dict(lugar_visits),
        "timeline":       timeline,    # NEW: chronological detail table
        "anomalias":      ua,
        "rows":           u_rows,
    }

def full_day_data(dia):
    raw = rows_for_day(dia)
    if not raw: return {}

    # group by user, re-assign tipo from scratch (respecting DB order)
    user_raw = defaultdict(list)
    for r in raw: user_raw[r["usuario"]].append(r)

    # For DB rows: tipo is already stored; just use it as-is
    # (edits save tipo explicitly)
    users_data = {}
    for usuario, ur in user_raw.items():
        users_data[usuario] = analyze_user(ur)

    # cadena list
    cadenas = sorted(set(r["cadena"] for r in raw))

    # tab3: cadena -> lugar -> visits
    cadena_map = defaultdict(lambda: defaultdict(list))
    for usuario, info in users_data.items():
        for lugar, visits in info["lugar_visits"].items():
            cadena = detectar_cadena(lugar)
            for v in visits:
                cadena_map[cadena][lugar].append({
                    "usuario": usuario,
                    "e": v["e"], "s": v["s"],
                    "min": v["min"],
                    "id_e": v["id_e"], "id_s": v["id_s"]
                })

    n_entradas = sum(1 for r in raw if r["tipo"]=="ENTRADA")
    n_salidas  = sum(1 for r in raw if r["tipo"]=="SALIDA")
    # active = last mark per user is ENTRADA
    last_tipo  = {}
    for r in sorted(raw, key=lambda x: x["hora"]):
        last_tipo[r["usuario"]] = r["tipo"]
    n_active = sum(1 for t in last_tipo.values() if t=="ENTRADA")

    # Build all_marks including INTERVALO rows from each user's timeline
    all_marks = []
    for r in raw:
        all_marks.append({**r, "kind": "marca"})
    for usuario, info in users_data.items():
        for row in info.get("timeline", []):
            if row["kind"] == "intervalo":
                all_marks.append({
                    "id":       None,
                    "usuario":  usuario,
                    "dia":      dia,
                    "hora":     row["e"],    # sort by start of interval
                    "hora_s":   row["s"],
                    "lugar":    "INTERVALO",
                    "cadena":   "INTERVALO",
                    "tipo":     "INTERVALO",
                    "manual":   0,
                    "kind":     "intervalo",
                    "min":      row["min"],
                })
    all_marks.sort(key=lambda x: (x["hora"], x["usuario"]))

    return {
        "dia": dia,
        "users": users_data,
        "all_marks": all_marks,
        "cadena_map": {c: dict(locs) for c,locs in cadena_map.items()},
        "cadenas": cadenas,
        "stats": {
            "n_entradas": n_entradas,
            "n_salidas":  n_salidas,
            "n_active":   n_active,
            "n_workers":  len(users_data)
        }
    }

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file(HTML_PATH)

@app.route("/api/dias")
def api_dias():
    con = get_db()
    dias = [r[0] for r in con.execute(
        "SELECT DISTINCT dia FROM marcaciones WHERE deleted=0 ORDER BY dia DESC"
    ).fetchall()]
    con.close()
    return jsonify(dias)

@app.route("/api/data")
def api_data():
    dia = request.args.get("dia","")
    if not dia:
        return jsonify({"error":"dia requerido"}), 400
    data = full_day_data(dia)
    return jsonify(data)

@app.route("/api/txt")
def api_txt():
    dia = request.args.get("dia","")
    if not dia: return jsonify({"error":"dia requerido"}),400
    data = full_day_data(dia)
    if not data: return jsonify({"error":"sin datos"}),404

    d   = datetime.strptime(dia,"%Y-%m-%d")
    SEP = "=" * 70
    lines = [SEP, f"  REPORTE DE MARCACIONES - {d.strftime('%d/%m/%Y')}", SEP, ""]

    for usuario in sorted(data["users"].keys()):
        info = data["users"][usuario]
        pe, us = info["primer_entrada"], info["ultima_salida"]

        lines += [SEP, f"USUARIO: {usuario}", SEP, "\n[ RESUMEN GENERAL ]"]
        lines.append(f"  Primera entrada  : {pe['hora']} - {pe['lugar']}" if pe else "  Primera entrada  : Sin registro")
        lines.append(f"  Ultima salida    : {us['hora']} - {us['lugar']}" if us else "  Ultima salida    : Sin registro")
        lines.append(f"  Total marcaciones: {info['total']}")
        lines.append(f"  Total horas      : {fmt_dur(info['total_mins'])}")
        lines.append(f"  Anomalias        : {'SI' if info['has_anomalia'] else 'No'}")

        lines.append("\n[ DETALLE DE MARCACIONES ]")
        lines.append(f"  {'LUGAR':<40} {'ENTRADA':>7}  {'SALIDA':>7}  DURACION")
        lines.append("  " + "-"*66)

        for row in info.get("timeline", []):
            if row["kind"] == "lugar":
                flag = "! " if row["is_anom"] else "  "
                lines.append(f"{flag}{row['lugar']:<40} {row['e']:>7}  {row['s']:>7}  {fmt_dur(row['min'])}")
                if row["is_anom"] and row["anom_msg"]:
                    lines.append(f"    ^ {row['anom_msg']}")
            elif row["kind"] == "fuera":
                lines.append(f"! {'FUERA DE RANGO':<40} {row['e']:>7}  {row['s']:>7}  {fmt_dur(row['min'])}")
                if row["anom_msg"]:
                    lines.append(f"    ^ {row['anom_msg']}")
            elif row["kind"] == "intervalo":
                lines.append(f"  {'  ~ intervalo':<40} {row['e']:>7}  {row['s']:>7}  {fmt_dur(row['min'])}")
            elif row["kind"] == "unpaired":
                lbl = "FUERA DE RANGO" if row["lugar"]=="-" else row["lugar"]
                e_s = f"{row['e'] or '?':>7}  {row['s'] or '?':>7}"
                lines.append(f"! {lbl:<40} {e_s}  (sin par)")

        if info["anomalias"]:
            lines.append("\n[ ANOMALIAS ]")
            for a in info["anomalias"]:
                lines.append(f"  ! {a['msg']}")

        lines.append("")

    lines += [SEP, "FIN DEL REPORTE", SEP]
    txt = "\n".join(lines)
    filename = f"{dia.replace('-','')}_RM.txt"
    return send_file(
        io.BytesIO(txt.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename
    )

def api_csv():
    dia = request.args.get("dia","")
    if not dia: return jsonify({"error":"dia requerido"}),400
    raw = rows_for_day(dia)
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["ID","Usuario","Dia","Hora","Lugar","Cadena","Tipo","Manual"])
    for r in raw:
        w.writerow([r["id"],r["usuario"],r["dia"],r["hora"],
                    r["lugar"],r["cadena"],r["tipo"],
                    "MANUAL" if r["manual"] else "ORIGINAL"])
    out.seek(0)
    filename = f"{dia.replace('-','')}_marcaciones.csv"
    return send_file(
        io.BytesIO(("\ufeff"+out.read()).encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename
    )

@app.route("/api/marcacion", methods=["POST"])
def api_add():
    d   = request.json or {}
    req = ["usuario","dia","hora","lugar","tipo"]
    if not all(k in d for k in req):
        return jsonify({"error":"Faltan campos"}),400
    lugar  = d["lugar"].upper().strip()
    cadena = detectar_cadena(lugar)
    con    = get_db()
    cur    = con.execute("""
        INSERT INTO marcaciones(usuario,dia,hora,lugar,cadena,tipo,manual,deleted)
        VALUES(?,?,?,?,?,?,1,0)
    """,(d["usuario"],d["dia"],d["hora"],lugar,cadena,d["tipo"].upper()))
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return jsonify({"ok":True,"id":new_id})

@app.route("/api/marcacion/<int:mid>", methods=["PUT"])
def api_edit(mid):
    d   = request.json or {}
    con = get_db()
    row = con.execute("SELECT * FROM marcaciones WHERE id=?",(mid,)).fetchone()
    if not row: con.close(); return jsonify({"error":"No encontrado"}),404
    hora  = d.get("hora",  row["hora"])
    lugar = d.get("lugar", row["lugar"]).upper().strip()
    tipo  = d.get("tipo",  row["tipo"]).upper()
    cadena= detectar_cadena(lugar)
    con.execute("""
        UPDATE marcaciones SET hora=?,lugar=?,cadena=?,tipo=?,manual=1
        WHERE id=?
    """,(hora,lugar,cadena,tipo,mid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/marcacion/<int:mid>", methods=["DELETE"])
def api_delete(mid):
    con = get_db()
    con.execute("UPDATE marcaciones SET deleted=1 WHERE id=?",(mid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
