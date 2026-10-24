import os
import sqlite3
from datetime import datetime

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pelada.db")

COLORS = ["vermelho", "amarelo", "verde", "azul"]
EVENT_TYPES = ["gol", "assistencia", "defesa", "salvou"]
# tipo de evento -> coluna de estatística
STAT_KEY = {"gol": "gols", "assistencia": "assistencias", "defesa": "defesas", "salvou": "salvos"}
POINTS = {"gols": 3, "assistencias": 2, "defesas": 2, "salvos": 2}

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS peladas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pelada_teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pelada_id INTEGER NOT NULL REFERENCES peladas(id) ON DELETE CASCADE,
    color TEXT NOT NULL,
    UNIQUE (pelada_id, color)
);
CREATE TABLE IF NOT EXISTS team_players (
    team_id INTEGER NOT NULL REFERENCES pelada_teams(id) ON DELETE CASCADE,
    pelada_id INTEGER NOT NULL REFERENCES peladas(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, player_id),
    UNIQUE (pelada_id, player_id)
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pelada_id INTEGER NOT NULL REFERENCES peladas(id) ON DELETE CASCADE,
    home_team_id INTEGER NOT NULL REFERENCES pelada_teams(id) ON DELETE CASCADE,
    away_team_id INTEGER NOT NULL REFERENCES pelada_teams(id) ON DELETE CASCADE,
    home_goals INTEGER NOT NULL DEFAULT 0,
    away_goals INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pelada_id INTEGER NOT NULL REFERENCES peladas(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('gol','assistencia','defesa','salvou'))
);
-- Ajustes manuais (deltas). pelada_id = 0 -> ajuste do registro geral.
CREATE TABLE IF NOT EXISTS adjustments (
    pelada_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    gols INTEGER NOT NULL DEFAULT 0,
    assistencias INTEGER NOT NULL DEFAULT 0,
    defesas INTEGER NOT NULL DEFAULT 0,
    salvos INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pelada_id, player_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------- cálculos
def empty_stats():
    return {"gols": 0, "assistencias": 0, "defesas": 0, "salvos": 0}


def calc_points(s):
    return sum(s[k] * POINTS[k] for k in POINTS)


def player_stats(db, pelada_id=None, include_general_adj=True):
    """Soma eventos + ajustes. pelada_id=None -> geral (todas as peladas + ajuste geral)."""
    stats = {}

    def bucket(pid):
        return stats.setdefault(pid, empty_stats())

    if pelada_id is None:
        ev = db.execute("SELECT player_id, type, COUNT(*) c FROM events GROUP BY player_id, type").fetchall()
        adj = db.execute("SELECT * FROM adjustments").fetchall()
    else:
        ev = db.execute(
            "SELECT player_id, type, COUNT(*) c FROM events WHERE pelada_id=? GROUP BY player_id, type",
            (pelada_id,),
        ).fetchall()
        adj = db.execute("SELECT * FROM adjustments WHERE pelada_id=?", (pelada_id,)).fetchall()
    for r in ev:
        bucket(r["player_id"])[STAT_KEY[r["type"]]] += r["c"]
    for r in adj:
        b = bucket(r["player_id"])
        for k in POINTS:
            b[k] += r[k]
    return stats


def ranking_rows(db, stats, only_ids=None):
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM players")}
    rows = []
    for pid, s in stats.items():
        if only_ids is not None and pid not in only_ids and not any(s.values()):
            continue
        rows.append({"player_id": pid, "name": names.get(pid, "?"), "pontos": calc_points(s), **s})
    if only_ids is not None:
        for pid in only_ids:
            if pid not in stats:
                rows.append({"player_id": pid, "name": names.get(pid, "?"), "pontos": 0, **empty_stats()})
    rows.sort(key=lambda r: (-r["pontos"], -r["gols"], -r["assistencias"], r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["pos"] = i
    return rows


def standings(teams, matches):
    table = {
        t["id"]: {"team_id": t["id"], "color": t["color"], "P": 0, "J": 0, "V": 0, "E": 0, "D": 0, "GP": 0, "GC": 0, "SG": 0}
        for t in teams
    }
    for m in matches:
        h, a = table.get(m["home_team_id"]), table.get(m["away_team_id"])
        if not h or not a:
            continue
        hg, ag = m["home_goals"], m["away_goals"]
        h["J"] += 1
        a["J"] += 1
        h["GP"] += hg
        h["GC"] += ag
        a["GP"] += ag
        a["GC"] += hg
        if hg > ag:
            h["V"] += 1
            a["D"] += 1
            h["P"] += 3
        elif ag > hg:
            a["V"] += 1
            h["D"] += 1
            a["P"] += 3
        else:
            h["E"] += 1
            a["E"] += 1
            h["P"] += 1
            a["P"] += 1
    rows = list(table.values())
    for r in rows:
        r["SG"] = r["GP"] - r["GC"]
    rows.sort(key=lambda r: (-r["P"], -r["V"], -r["SG"], -r["GP"], r["color"]))
    for i, r in enumerate(rows, 1):
        r["pos"] = i
    return rows


def pelada_detail(db, pid):
    p = db.execute("SELECT * FROM peladas WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    teams = [dict(r) for r in db.execute("SELECT id, color FROM pelada_teams WHERE pelada_id=? ORDER BY id", (pid,))]
    roster_ids = set()
    for t in teams:
        t["players"] = [
            r["player_id"] for r in db.execute("SELECT player_id FROM team_players WHERE team_id=?", (t["id"],))
        ]
        roster_ids.update(t["players"])
    matches = [dict(r) for r in db.execute("SELECT * FROM matches WHERE pelada_id=? ORDER BY id", (pid,))]
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM players")}
    for m in matches:
        m["events"] = [
            {"id": e["id"], "player_id": e["player_id"], "name": names.get(e["player_id"], "?"), "type": e["type"]}
            for e in db.execute("SELECT * FROM events WHERE match_id=? ORDER BY id", (m["id"],))
        ]
    stats = player_stats(db, pid)
    return {
        "id": p["id"],
        "date": p["date"],
        "teams": teams,
        "matches": matches,
        "standings": standings(teams, matches),
        "players_table": ranking_rows(db, stats, only_ids=roster_ids),
    }


# ---------------------------------------------------------------- páginas
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- jogadores
@app.get("/api/players")
def list_players():
    rows = get_db().execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/players")
def create_player():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return err("Informe o nome do jogador.")
    db = get_db()
    try:
        cur = db.execute("INSERT INTO players (name) VALUES (?)", (name,))
        db.commit()
    except sqlite3.IntegrityError:
        return err("Já existe um jogador com esse nome.")
    return jsonify({"id": cur.lastrowid, "name": name}), 201


@app.put("/api/players/<int:pid>")
def rename_player(pid):
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return err("Informe o nome do jogador.")
    db = get_db()
    try:
        db.execute("UPDATE players SET name=? WHERE id=?", (name, pid))
        db.commit()
    except sqlite3.IntegrityError:
        return err("Já existe um jogador com esse nome.")
    return jsonify({"id": pid, "name": name})


@app.delete("/api/players/<int:pid>")
def delete_player(pid):
    db = get_db()
    db.execute("DELETE FROM players WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- peladas
@app.get("/api/peladas")
def list_peladas():
    db = get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM peladas ORDER BY date DESC, id DESC")]
    return jsonify([pelada_detail(db, i) for i in ids])


@app.post("/api/peladas")
def create_pelada():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")  # data atual salva automaticamente
    cur = db.execute("INSERT INTO peladas (date) VALUES (?)", (today,))
    db.commit()
    return jsonify(pelada_detail(db, cur.lastrowid)), 201


@app.delete("/api/peladas/<int:pid>")
def delete_pelada(pid):
    db = get_db()
    db.execute("DELETE FROM adjustments WHERE pelada_id=?", (pid,))
    db.execute("DELETE FROM peladas WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.put("/api/peladas/<int:pid>/teams")
def set_teams(pid):
    """Body: {"teams": {"vermelho": [player_ids], "azul": [...]}}  (2 a 4 times)."""
    db = get_db()
    if not db.execute("SELECT 1 FROM peladas WHERE id=?", (pid,)).fetchone():
        return err("Pelada não encontrada.", 404)
    data = (request.get_json(silent=True) or {}).get("teams") or {}
    if any(c not in COLORS for c in data):
        return err("Cores válidas: vermelho, amarelo, verde, azul.")
    if not (2 <= len(data) <= 4):
        return err("Uma pelada precisa ter de 2 a 4 times.")
    seen = set()
    for plist in data.values():
        for p in plist:
            if p in seen:
                return err("Um jogador não pode estar em dois times no mesmo dia.")
            seen.add(p)

    existing = {r["color"]: r["id"] for r in db.execute("SELECT id, color FROM pelada_teams WHERE pelada_id=?", (pid,))}
    for color, tid in existing.items():
        if color not in data:  # time removido -> apaga partidas dele (cascade)
            db.execute("DELETE FROM pelada_teams WHERE id=?", (tid,))
    for color, plist in data.items():
        if color in existing:
            tid = existing[color]
        else:
            tid = db.execute("INSERT INTO pelada_teams (pelada_id, color) VALUES (?,?)", (pid, color)).lastrowid
        db.execute("DELETE FROM team_players WHERE team_id=?", (tid,))
    db.execute("DELETE FROM team_players WHERE pelada_id=?", (pid,))
    for color, plist in data.items():
        tid = db.execute("SELECT id FROM pelada_teams WHERE pelada_id=? AND color=?", (pid, color)).fetchone()["id"]
        for p in plist:
            db.execute("INSERT INTO team_players (team_id, pelada_id, player_id) VALUES (?,?,?)", (tid, pid, p))
    db.commit()
    return jsonify(pelada_detail(db, pid))


# ---------------------------------------------------------------- partidas
def parse_goals(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


@app.post("/api/peladas/<int:pid>/matches")
def create_match(pid):
    db = get_db()
    d = request.get_json(silent=True) or {}
    hg, ag = parse_goals(d.get("home_goals", 0)), parse_goals(d.get("away_goals", 0))
    if hg is None or ag is None:
        return err("Gols inválidos.")
    h, a = d.get("home_team_id"), d.get("away_team_id")
    if h == a:
        return err("Escolha dois times diferentes.")
    ok = db.execute(
        "SELECT COUNT(*) c FROM pelada_teams WHERE pelada_id=? AND id IN (?,?)", (pid, h, a)
    ).fetchone()["c"]
    if ok != 2:
        return err("Times inválidos para esta pelada.")
    db.execute(
        "INSERT INTO matches (pelada_id, home_team_id, away_team_id, home_goals, away_goals) VALUES (?,?,?,?,?)",
        (pid, h, a, hg, ag),
    )
    db.commit()
    return jsonify(pelada_detail(db, pid)), 201


@app.put("/api/matches/<int:mid>")
def update_match(mid):
    db = get_db()
    m = db.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    if not m:
        return err("Partida não encontrada.", 404)
    d = request.get_json(silent=True) or {}
    hg, ag = parse_goals(d.get("home_goals")), parse_goals(d.get("away_goals"))
    if hg is None or ag is None:
        return err("Gols inválidos.")
    db.execute("UPDATE matches SET home_goals=?, away_goals=? WHERE id=?", (hg, ag, mid))
    db.commit()
    return jsonify(pelada_detail(db, m["pelada_id"]))


@app.delete("/api/matches/<int:mid>")
def delete_match(mid):
    db = get_db()
    m = db.execute("SELECT pelada_id FROM matches WHERE id=?", (mid,)).fetchone()
    if not m:
        return err("Partida não encontrada.", 404)
    db.execute("DELETE FROM matches WHERE id=?", (mid,))
    db.commit()
    return jsonify(pelada_detail(db, m["pelada_id"]))


# ---------------------------------------------------------------- eventos
@app.post("/api/matches/<int:mid>/events")
def add_event(mid):
    db = get_db()
    m = db.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    if not m:
        return err("Partida não encontrada.", 404)
    d = request.get_json(silent=True) or {}
    etype, player = d.get("type"), d.get("player_id")
    if etype not in EVENT_TYPES:
        return err("Tipo de evento inválido.")
    if not db.execute("SELECT 1 FROM players WHERE id=?", (player,)).fetchone():
        return err("Jogador inválido.")
    db.execute(
        "INSERT INTO events (pelada_id, match_id, player_id, type) VALUES (?,?,?,?)",
        (m["pelada_id"], mid, player, etype),
    )
    db.commit()
    return jsonify(pelada_detail(db, m["pelada_id"])), 201


@app.delete("/api/events/<int:eid>")
def delete_event(eid):
    db = get_db()
    e = db.execute("SELECT pelada_id FROM events WHERE id=?", (eid,)).fetchone()
    if not e:
        return err("Evento não encontrado.", 404)
    db.execute("DELETE FROM events WHERE id=?", (eid,))
    db.commit()
    return jsonify(pelada_detail(db, e["pelada_id"]))


# ---------------------------------------------------------------- ranking / ajustes
@app.get("/api/ranking")
def general_ranking():
    db = get_db()
    players = [r["id"] for r in db.execute("SELECT id FROM players")]
    return jsonify(ranking_rows(db, player_stats(db), only_ids=set(players)))


@app.get("/api/adjust-view")
def adjust_view():
    """Estatísticas atuais de todos os jogadores no escopo (pelada_id=0 -> geral)."""
    db = get_db()
    scope = request.args.get("pelada_id", type=int, default=0)
    stats = player_stats(db, None if scope == 0 else scope)
    out = []
    for p in db.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE"):
        s = stats.get(p["id"], empty_stats())
        out.append({"player_id": p["id"], "name": p["name"], **s, "pontos": calc_points(s)})
    return jsonify(out)


@app.put("/api/adjustments")
def set_adjustment():
    """Define os totais desejados do jogador no escopo; grava a diferença como ajuste."""
    db = get_db()
    d = request.get_json(silent=True) or {}
    scope, player = d.get("pelada_id", 0), d.get("player_id")
    try:
        desired = {k: int(d[k]) for k in POINTS}
    except (KeyError, TypeError, ValueError):
        return err("Valores inválidos.")
    if any(v < 0 for v in desired.values()):
        return err("Os valores não podem ser negativos.")
    if not db.execute("SELECT 1 FROM players WHERE id=?", (player,)).fetchone():
        return err("Jogador inválido.")

    if scope == 0:
        # total geral sem o próprio ajuste geral
        base = empty_stats()
        for r in db.execute("SELECT type, COUNT(*) c FROM events WHERE player_id=? GROUP BY type", (player,)):
            base[STAT_KEY[r["type"]]] += r["c"]
        for r in db.execute("SELECT * FROM adjustments WHERE player_id=? AND pelada_id<>0", (player,)):
            for k in POINTS:
                base[k] += r[k]
    else:
        if not db.execute("SELECT 1 FROM peladas WHERE id=?", (scope,)).fetchone():
            return err("Pelada não encontrada.", 404)
        base = empty_stats()
        for r in db.execute(
            "SELECT type, COUNT(*) c FROM events WHERE player_id=? AND pelada_id=? GROUP BY type", (player, scope)
        ):
            base[STAT_KEY[r["type"]]] += r["c"]

    delta = {k: desired[k] - base[k] for k in POINTS}
    db.execute(
        """INSERT INTO adjustments (pelada_id, player_id, gols, assistencias, defesas, salvos)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(pelada_id, player_id) DO UPDATE SET
             gols=excluded.gols, assistencias=excluded.assistencias,
             defesas=excluded.defesas, salvos=excluded.salvos""",
        (scope, player, delta["gols"], delta["assistencias"], delta["defesas"], delta["salvos"]),
    )
    db.commit()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
