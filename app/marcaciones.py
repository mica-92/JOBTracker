"""
marcaciones.py
==============
Uso:
  python marcaciones.py            -> importa marcaciones.xlsx (toma fecha del archivo)
  python marcaciones.py 20260430   -> importa un dia especifico

Luego levanta server.py en http://localhost:5000 y abre el browser.
"""
import sys, os, sqlite3, threading, webbrowser, time
from datetime import datetime
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    print("Falta openpyxl.  Instalar: pip install openpyxl")
    sys.exit(1)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "marcaciones.db")
EXCEL_PATH = os.path.join(BASE_DIR, "marcaciones.xlsx")
SERVER_PY  = os.path.join(BASE_DIR, "server.py")

# ── CADENAS: variante → nombre canónico ──────────────────────────────────────
# Agregar nuevas variantes aquí. El valor es el nombre canónico del grupo.
CADENAS_MAP = {
    "CARREFOUR":          "CARREFOUR",
    "HIPER CARREFOUR":    "CARREFOUR",
    "CARREFOUR MAXI":     "CARREFOUR",
    "MAXICARREFOUR":      "CARREFOUR",
    "CARERFOUR":          "CARREFOUR",
    "CAREFOUR":           "CARREFOUR",
    "VEA":                "VEA",
    "PLAZA VEA":          "VEA",
    "WAL MART":           "WAL MART",
    "WALMART":            "WAL MART",
    "CHANGO MAS":         "CHANGO MAS",
    "CHANGOMAS":          "CHANGO MAS",
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
_CADENAS_PREFIXES = sorted(CADENAS_MAP.keys(), key=len, reverse=True)

def detectar_cadena(lugar):
    lu = lugar.upper().strip()
    if lu in ("-", "", "FUERA DE RANGO"):
        return "FUERA DE RANGO"
    for prefix in _CADENAS_PREFIXES:
        if lu == prefix or lu.startswith(prefix + " ") or lu.startswith(prefix + "-"):
            return CADENAS_MAP[prefix]
    return "OTROS"

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS marcaciones (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT NOT NULL,
        dia     TEXT NOT NULL,
        hora    TEXT NOT NULL,
        lugar   TEXT NOT NULL,
        cadena  TEXT NOT NULL,
        tipo    TEXT NOT NULL,
        manual  INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dia ON marcaciones(dia)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_usr ON marcaciones(usuario,dia)")
    con.commit()
    return con

# ── EXCEL ─────────────────────────────────────────────────────────────────────
def load_excel(path):
    if not os.path.exists(path):
        print(f"X No se encontro: {path}"); sys.exit(1)
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows, headers = [], None
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(c).strip() if c else "" for c in row]; continue
        if not any(row): continue
        rec     = dict(zip(headers, row))
        usuario = str(rec.get("Usuario","") or "").strip()
        dia_raw = rec.get("Dia") or rec.get("Día") or ""
        h_raw   = rec.get("Hora","") or ""
        lugar   = str(rec.get("Lugar","") or "").strip().upper()
        if not usuario: continue
        # parse dia
        if isinstance(dia_raw, datetime):
            dia = dia_raw.strftime("%Y-%m-%d")
        else:
            s = str(dia_raw).strip()
            for fmt in ("%d/%m/%Y","%Y-%m-%d","%d-%m-%Y"):
                try: dia = datetime.strptime(s,fmt).strftime("%Y-%m-%d"); break
                except: pass
            else: dia = s
        # parse hora
        if isinstance(h_raw, datetime):   hora = h_raw.strftime("%H:%M")
        elif hasattr(h_raw,"hour"):       hora = f"{h_raw.hour:02d}:{h_raw.minute:02d}"
        else:                             hora = str(h_raw).strip()
        rows.append({"usuario":usuario,"dia":dia,"hora":hora,"lugar":lugar})
    return rows

def assign_tipo(rows):
    groups = defaultdict(list)
    for r in rows: groups[(r["usuario"],r["dia"])].append(r)
    result = []
    for _,ur in groups.items():
        ur  = sorted(ur, key=lambda x: x["hora"])
        cnt = defaultdict(int)
        for r in ur:
            tipo = "ENTRADA" if cnt[r["lugar"]]%2==0 else "SALIDA"
            cnt[r["lugar"]] += 1
            result.append({**r,"tipo":tipo,"cadena":detectar_cadena(r["lugar"])})
    return result

def check_dependencies():
    """Verifica e instala dependencias necesarias."""
    required = {
        "flask":   "flask",
        "openpyxl": "openpyxl",
    }
    missing = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    print(f"\nInstalando dependencias faltantes: {', '.join(missing)}")
    import subprocess as sp
    for pkg in missing:
        print(f"   pip install {pkg} ...")
        result = sp.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"   ERROR instalando {pkg}:")
            print(result.stderr)
            sys.exit(1)
        print(f"   OK {pkg} instalado.")


def import_to_db(rows, dia_str):
    con = init_db()
    con.execute("DELETE FROM marcaciones WHERE dia=? AND manual=0",(dia_str,))
    con.executemany("""
        INSERT INTO marcaciones(usuario,dia,hora,lugar,cadena,tipo,manual,deleted)
        VALUES(?,?,?,?,?,?,0,0)
    """,[(r["usuario"],r["dia"],r["hora"],r["lugar"],r["cadena"],r["tipo"]) for r in rows])
    con.commit(); con.close()
    print(f"OK  {len(rows)} marcaciones importadas para {dia_str}")

def main():
    check_dependencies()
    if len(sys.argv) > 1:
        try:
            d = datetime.strptime(sys.argv[1].strip(), "%Y%m%d")
            dia_str = d.strftime("%Y-%m-%d")
        except ValueError:
            print("Formato de fecha invalido. Usar: python marcaciones.py YYYYMMDD")
            sys.exit(1)
    else:
        dia_str = None   # se detecta desde el Excel

    print(f"\n-- Importando marcaciones --")
    rows  = load_excel(EXCEL_PATH)
    typed = assign_tipo(rows)

    if dia_str:
        rows_day = [r for r in typed if r["dia"] == dia_str]
        if not rows_day:
            print(f"Advertencia: no hay datos para {dia_str}, importando todos los datos del Excel.")
            rows_day = typed
            dia_str  = rows_day[0]["dia"]
    else:
        # Tomar la fecha del Excel (primer registro)
        if not typed:
            print("X El Excel no tiene datos.")
            sys.exit(1)
        dia_str  = typed[0]["dia"]
        rows_day = [r for r in typed if r["dia"] == dia_str]

    print(f"   Fecha: {dia_str}")
    import_to_db(rows_day, dia_str)

    if not os.path.exists(SERVER_PY):
        print(f"X Falta server.py en {BASE_DIR}")
        sys.exit(1)

    print("-- Abriendo http://localhost:5000 --")

    def _open():
        time.sleep(1.8)
        webbrowser.open(f"http://localhost:5000?dia={dia_str}")
    threading.Thread(target=_open, daemon=True).start()

    # Usar subprocess en lugar de os.execv para evitar problemas con
    # rutas con espacios en Windows (ej: "OneDrive - Accenture")
    import subprocess
    proc = subprocess.Popen([sys.executable, SERVER_PY])
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
