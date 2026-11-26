import os
import json
import random
import string
import sqlite3
import asyncio
from typing import Dict, Optional, List, Tuple
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tetris_br.db")
BARCODE_PATH = os.path.join(BASE_DIR, "KepoTetris.jpeg")
ROWS, COLS = 20, 10

SHAPES = [
    [[1, 1, 1, 1]],
    [[2, 0, 0], [2, 2, 2]],
    [[0, 0, 3], [3, 3, 3]],
    [[4, 4], [4, 4]],
    [[0, 5, 5], [5, 5, 0]],
    [[0, 6, 0], [6, 6, 6]],
    [[7, 7, 0], [0, 7, 7]],
]

app = FastAPI()

rooms: Dict[str, dict] = {}
bot_counter = 1


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS stats (
            username TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            total_ko INTEGER DEFAULT 0,
            high_score INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


def record_stat(username: str, win: int = 0, ko: int = 0, score: int = 0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO stats (username, wins, total_ko, high_score)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            wins = wins + excluded.wins,
            total_ko = total_ko + excluded.total_ko,
            high_score = MAX(high_score, excluded.high_score)
    ''', (username, win, ko, score))
    conn.commit()
    conn.close()


def generate_room_code() -> str:
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in rooms:
            return code


def empty_grid():
    return [[0 for _ in range(COLS)] for _ in range(ROWS)]


def rotate_matrix(m):
    return [list(row) for row in zip(*m[::-1])]


def bot_collide(board, shape, px, py) -> bool:
    for r_idx, row in enumerate(shape):
        for c_idx, val in enumerate(row):
            if val:
                nr = py + r_idx
                nc = px + c_idx
                if nc < 0 or nc >= COLS or nr >= ROWS:
                    return True
                if nr >= 0 and board[nr][nc] != 0:
                    return True
    return False


def compute_bot_view_grid(bot: dict):
    base = bot.get("board") or empty_grid()
    snap = [row[:] for row in base]
    cur = bot.get("cur_piece")
    if cur and bot.get("alive"):
        shape = cur["shape"]
        px, py = cur["x"], cur["y"]
        for r_idx, row in enumerate(shape):
            for c_idx, val in enumerate(row):
                if val:
                    nr = py + r_idx
                    nc = px + c_idx
                    if 0 <= nr < ROWS and 0 <= nc < COLS:
                        snap[nr][nc] = val
    return snap


def evaluate_board(board: List[List[int]], lines_cleared: int) -> float:
    """Heuristik posisi papan setelah blok mendarat."""
    agg_height = 0
    holes = 0
    col_heights = [0] * COLS

    for c in range(COLS):
        block_seen = False
        for r in range(ROWS):
            if board[r][c] != 0:
                if not block_seen:
                    col_heights[c] = ROWS - r
                    agg_height += col_heights[c]
                    block_seen = True
            else:
                if block_seen:
                    holes += 1

    bumpiness = sum(abs(col_heights[i] - col_heights[i + 1]) for i in range(COLS - 1))
    max_height = max(col_heights) if col_heights else 0

    line_bonus = {0: 0.0, 1: 4.5, 2: 11.0, 3: 22.0, 4: 40.0}.get(lines_cleared, lines_cleared * 8.0)

    return (
        line_bonus
        - (holes * 5.2)
        - (agg_height * 0.45)
        - (bumpiness * 1.1)
        - (max(0, max_height - 12) * 1.6)
    )


def simulate_landing(board: List[List[int]], shape: List[List[int]], x: int) -> Optional[Tuple[int, float]]:
    """Simulasikan drop blok pada kolom x, kembalikan (final_y, score_heuristik)."""
    if bot_collide(board, shape, x, 0):
        return None

    y = 0
    while not bot_collide(board, shape, x, y + 1):
        y += 1

    temp = [row[:] for row in board]
    for r_idx, row in enumerate(shape):
        for c_idx, val in enumerate(row):
            if val:
                nr = y + r_idx
                nc = x + c_idx
                if 0 <= nr < ROWS and 0 <= nc < COLS:
                    temp[nr][nc] = val

    cleared = 0
    kept = []
    for r in temp:
        if all(cell != 0 for cell in r):
            cleared += 1
        else:
            kept.append(r)
    while len(kept) < ROWS:
        kept.insert(0, [0] * COLS)

    # Penalti jika puncak papan menyentuh baris teratas
    top_blocked = 12.0 if any(cell != 0 for cell in kept[0]) else 0.0
    score_val = evaluate_board(kept, cleared) - top_blocked
    return y, score_val


def pick_smart_human_target(bot: dict):
    """Pilih rotasi & target_x terbaik secara heuristik, dengan sedikit variasi manusiawi."""
    board = bot["board"]
    base_shape = random.choice(SHAPES)

    # Kumpulkan semua orientasi unik
    rotations: List[List[List[int]]] = []
    curr = [row[:] for row in base_shape]
    for _ in range(4):
        if not any(curr == existing for existing in rotations):
            rotations.append([row[:] for row in curr])
        curr = rotate_matrix(curr)

    moves = []
    for rot_shape in rotations:
        w = len(rot_shape[0])
        for x in range(COLS - w + 1):
            sim = simulate_landing(board, rot_shape, x)
            if sim is not None:
                _, h_score = sim
                moves.append((h_score, rot_shape, x))

    if not moves:
        w = len(base_shape[0])
        start_x = max(0, min(COLS - w, COLS // 2 - w // 2))
        return {
            "shape": [row[:] for row in base_shape],
            "x": start_x,
            "y": 0,
            "target_x": start_x,
            "hesitate_ticks": 0,
        }

    moves.sort(key=lambda item: item[0], reverse=True)

    # 85% pilih langkah terbaik, 15% pilih opsi #2/#3 agar tidak seperti mesin super-sempurna
    if random.random() < 0.85 or len(moves) == 1:
        _, chosen_shape, target_x = moves[0]
    else:
        _, chosen_shape, target_x = random.choice(moves[1 : min(3, len(moves))])

    w = len(chosen_shape[0])
    start_x = max(0, min(COLS - w, COLS // 2 - w // 2))

    return {
        "shape": chosen_shape,
        "x": start_x,
        "y": 0,
        "target_x": target_x,
        "hesitate_ticks": random.randint(0, 1),
    }


def get_room_players_summary(room_code: str, include_grid: bool = False):
    room = rooms.get(room_code)
    if not room:
        return {}
    result = {}
    for pid, p in room["players"].items():
        item = {
            "name": p["name"],
            "is_bot": p.get("is_bot", False),
            "connected": p.get("connected", True),
            "alive": p["alive"],
            "ko": p["ko"],
            "score": p["score"],
        }
        if include_grid:
            item["grid"] = compute_bot_view_grid(p) if p.get("is_bot") else p["grid"]
        result[pid] = item
    return result


def get_lobby_list():
    lobby = []
    for code, room in rooms.items():
        has_admin = len(room.get("admins", {})) > 0
        has_players = len(room.get("players", {})) > 0
        if not has_admin and not has_players:
            continue

        if room.get("active"):
            state = "playing"
            status_label = "🔥 Sedang Main"
        elif room.get("last_winner"):
            state = "finished"
            status_label = "🏆 Selesai (Bisa Gabung)"
        else:
            state = "waiting"
            status_label = "⏳ Menunggu Pemain"

        total_participants = len(room.get("players", {}))
        alive_count = sum(1 for p in room.get("players", {}).values() if p.get("alive"))
        bot_count = sum(1 for p in room.get("players", {}).values() if p.get("is_bot"))

        scoreboard = sorted(
            [
                {
                    "name": p["name"],
                    "score": p["score"],
                    "ko": p["ko"],
                    "alive": p["alive"],
                    "connected": p.get("connected", True),
                    "is_bot": p.get("is_bot", False),
                }
                for p in room.get("players", {}).values()
            ],
            key=lambda x: (x["score"], x["ko"]),
            reverse=True,
        )

        lobby.append({
            "room_code": code,
            "state": state,
            "status_label": status_label,
            "players_count": total_participants,
            "alive_count": alive_count,
            "bot_count": bot_count,
            "last_winner": room.get("last_winner"),
            "scoreboard": scoreboard[:8],
        })
    return lobby


def compute_podium(room: dict, winner_id: Optional[str]):
    podium = []
    all_players = list(room["players"].items())

    if winner_id and winner_id in room["players"]:
        w = room["players"][winner_id]
        podium.append({
            "rank": 1,
            "pid": winner_id,
            "name": w["name"],
            "is_bot": w.get("is_bot", False),
            "score": w["score"],
            "ko": w["ko"],
            "reason": "Bertahan Sampai Akhir",
        })

    runners_up = [(pid, p) for pid, p in all_players if pid != winner_id]
    runners_up.sort(key=lambda item: (item[1]["score"], item[1]["ko"]), reverse=True)

    next_rank = 2 if podium else 1
    for pid, p in runners_up:
        if next_rank > 3:
            break
        podium.append({
            "rank": next_rank,
            "pid": pid,
            "name": p["name"],
            "is_bot": p.get("is_bot", False),
            "score": p["score"],
            "ko": p["ko"],
            "reason": f"Poin Tertinggi (Rank #{next_rank})",
        })
        next_rank += 1

    return podium


async def broadcast_to_room(room_code: str, message: dict, exclude_id: Optional[str] = None, include_admins: bool = True):
    room = rooms.get(room_code)
    if not room:
        return

    payload = json.dumps(message)
    for pid, p in list(room["players"].items()):
        if pid == exclude_id or p.get("is_bot") or not p.get("connected") or not p.get("ws"):
            continue
        try:
            await p["ws"].send_text(payload)
        except Exception:
            p["connected"] = False
            p["alive"] = False
            p["ws"] = None

    if include_admins:
        dead_admins = []
        for aid, ws in list(room["admins"].items()):
            if aid == exclude_id:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead_admins.append(aid)

        for aid in dead_admins:
            room["admins"].pop(aid, None)


async def send_to_player(room_code: str, player_id: str, message: dict):
    room = rooms.get(room_code)
    if not room or player_id not in room["players"]:
        return
    p = room["players"][player_id]
    if p.get("is_bot") or not p.get("connected") or not p.get("ws"):
        return
    try:
        await p["ws"].send_text(json.dumps(message))
    except Exception:
        p["connected"] = False
        p["alive"] = False
        p["ws"] = None


async def send_to_admins(room_code: str, message: dict):
    room = rooms.get(room_code)
    if not room:
        return
    payload = json.dumps(message)
    dead_admins = []
    for aid, ws in list(room["admins"].items()):
        try:
            await ws.send_text(payload)
        except Exception:
            dead_admins.append(aid)
    for aid in dead_admins:
        room["admins"].pop(aid, None)


def append_random_garbage_rows(grid: list, count: int):
    last_hole = None
    for _ in range(max(1, count)):
        hole = random.randint(0, COLS - 1)
        if COLS > 1 and hole == last_hole:
            hole = (hole + random.randint(1, COLS - 1)) % COLS
        last_hole = hole
        grid.pop(0)
        row = [8] * COLS
        row[hole] = 0
        grid.append(row)


async def perform_attack(room_code: str, attacker_id: str, lines: int):
    room = rooms.get(room_code)
    if not room or not room["active"] or attacker_id not in room["players"]:
        return

    attack_map = {1: 1, 2: 2, 3: 3, 4: 5}
    garbage_count = attack_map.get(lines, 1)

    alive_opponents = [
        pid for pid, p in room["players"].items()
        if p["alive"] and p.get("connected", True) and pid != attacker_id
    ]
    if not alive_opponents:
        return

    attacker = room["players"][attacker_id]
    candidates = [pid for pid in alive_opponents if pid != attacker.get("last_target_id")]
    target_id = random.choice(candidates if candidates else alive_opponents)
    attacker["last_target_id"] = target_id

    attacker_name = attacker["name"]
    target_name = room["players"][target_id]["name"]

    await send_to_player(room_code, target_id, {
        "type": "receive_attack",
        "from_id": attacker_id,
        "from_name": attacker_name,
        "lines": garbage_count,
    })

    target_player = room["players"].get(target_id)
    if target_player and target_player.get("is_bot") and target_player["alive"]:
        append_random_garbage_rows(target_player["board"], min(garbage_count, 5))
        target_player["last_attacker_id"] = attacker_id
        cur = target_player.get("cur_piece")
        if cur and bot_collide(target_player["board"], cur["shape"], cur["x"], cur["y"]):
            cur["y"] = max(0, cur["y"] - min(garbage_count, 4))
            if bot_collide(target_player["board"], cur["shape"], cur["x"], cur["y"]):
                target_player["cur_piece"] = None

        await send_to_admins(room_code, {
            "type": "admin_screen_update",
            "pid": target_id,
            "name": target_player["name"],
            "alive": target_player["alive"],
            "score": target_player["score"],
            "ko": target_player["ko"],
            "grid": compute_bot_view_grid(target_player),
        })

    await broadcast_to_room(room_code, {
        "type": "attack_log",
        "msg": f"⚡ {attacker_name} mengirim {garbage_count} baris sampah ke {target_name}!",
        "target_id": target_id,
    })


async def eliminate_player(room_code: str, victim_id: str, killer_id: Optional[str]):
    room = rooms.get(room_code)
    if not room or victim_id not in room["players"]:
        return
    victim = room["players"][victim_id]
    if not victim["alive"]:
        return

    victim["alive"] = False
    victim["cur_piece"] = None
    victim_name = victim["name"]

    if killer_id and killer_id in room["players"] and killer_id != victim_id:
        room["players"][killer_id]["ko"] += 1
        killer_name = room["players"][killer_id]["name"]
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": f"💀 {killer_name} meng-KO {victim_name}!",
        })
    else:
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": f"💀 {victim_name} tereliminasi!",
        })

    record_stat(victim_name, win=0, ko=victim["ko"], score=victim["score"])

    await broadcast_to_room(room_code, {
        "type": "lobby_update",
        "players": get_room_players_summary(room_code, include_grid=False),
    }, include_admins=False)

    await send_to_admins(room_code, {
        "type": "admin_players_sync",
        "players": get_room_players_summary(room_code, include_grid=True),
    })

    await check_winner(room_code)


async def bot_loop():
    while True:
        await asyncio.sleep(0.28)
        for room_code, room in list(rooms.items()):
            if not room.get("active"):
                continue

            any_bot_moved = False

            for pid, p in list(room["players"].items()):
                if not p.get("is_bot") or not p.get("alive"):
                    continue

                board = p.get("board")
                if not board or len(board) != ROWS:
                    board = empty_grid()
                    p["board"] = board

                if p.get("spawn_cooldown", 0) > 0:
                    p["spawn_cooldown"] -= 1
                    continue

                cur = p.get("cur_piece")
                if not cur:
                    new_piece = pick_smart_human_target(p)
                    if bot_collide(board, new_piece["shape"], new_piece["x"], new_piece["y"]):
                        await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                        continue
                    p["cur_piece"] = new_piece
                    cur = new_piece
                    any_bot_moved = True

                # Gerak 1 langkah per tick menuju target_x lalu turun bertahap
                if cur.get("hesitate_ticks", 0) > 0:
                    cur["hesitate_ticks"] -= 1
                elif cur["x"] < cur["target_x"] and not bot_collide(board, cur["shape"], cur["x"] + 1, cur["y"]):
                    cur["x"] += 1
                elif cur["x"] > cur["target_x"] and not bot_collide(board, cur["shape"], cur["x"] - 1, cur["y"]):
                    cur["x"] -= 1
                else:
                    if not bot_collide(board, cur["shape"], cur["x"], cur["y"] + 1):
                        cur["y"] += 1
                    else:
                        for r_idx, row in enumerate(cur["shape"]):
                            for c_idx, val in enumerate(row):
                                if val:
                                    nr = cur["y"] + r_idx
                                    nc = cur["x"] + c_idx
                                    if 0 <= nr < ROWS and 0 <= nc < COLS:
                                        board[nr][nc] = val

                        p["cur_piece"] = None
                        p["spawn_cooldown"] = 1

                        cleared_lines = 0
                        new_board = []
                        for r in board:
                            if all(cell != 0 for cell in r):
                                cleared_lines += 1
                            else:
                                new_board.append(r)

                        while len(new_board) < ROWS:
                            new_board.insert(0, [0] * COLS)
                        p["board"] = new_board

                        if cleared_lines > 0:
                            p["score"] += cleared_lines * 100
                            await perform_attack(room_code, pid, cleared_lines)

                        if any(cell != 0 for cell in p["board"][0]):
                            await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                            continue

                any_bot_moved = True
                await send_to_admins(room_code, {
                    "type": "admin_screen_update",
                    "pid": pid,
                    "name": p["name"],
                    "alive": p["alive"],
                    "score": p["score"],
                    "ko": p["ko"],
                    "grid": compute_bot_view_grid(p),
                })

            if any_bot_moved:
                await broadcast_to_room(room_code, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(room_code, include_grid=False),
                }, include_admins=False)


@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(bot_loop())


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/KepoTetris.jpeg")
async def serve_barcode():
    if os.path.exists(BARCODE_PATH):
        return FileResponse(BARCODE_PATH, media_type="image/jpeg")
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/api/rooms")
async def api_get_rooms():
    return {"rooms": get_lobby_list()}


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    global bot_counter
    await websocket.accept()
    current_room: Optional[str] = None
    role: Optional[str] = None

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            event = data.get("type")

            if event == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if event == "create_room":
                custom_code = str(data.get("room_code", "")).strip().upper()[:8]
                room_code = custom_code if custom_code else generate_room_code()

                if room_code in rooms:
                    old_room = rooms[room_code]
                    old_payload = json.dumps({
                        "type": "room_closed",
                        "msg": f"Room '{room_code}' di-reset oleh Admin baru.",
                    })
                    for _, p in list(old_room.get("players", {}).items()):
                        if not p.get("is_bot") and p.get("ws"):
                            try:
                                await p["ws"].send_text(old_payload)
                            except Exception:
                                pass
                    for old_aid, old_ws in list(old_room.get("admins", {}).items()):
                        if old_aid != client_id:
                            try:
                                await old_ws.send_text(old_payload)
                            except Exception:
                                pass

                rooms[room_code] = {
                    "active": False,
                    "last_winner": None,
                    "admins": {client_id: websocket},
                    "players": {},
                }
                current_room = room_code
                role = "admin"

                await websocket.send_text(json.dumps({
                    "type": "room_created",
                    "room_code": room_code,
                    "active": False,
                    "players": {},
                }))

            elif event == "admin_add_bot":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                bot_id = f"bot_{bot_counter}"
                bot_name = f"Bot_{bot_counter}"
                bot_counter += 1

                room["players"][bot_id] = {
                    "ws": None,
                    "is_bot": True,
                    "connected": True,
                    "name": bot_name,
                    "alive": not room["active"],
                    "score": 0,
                    "ko": 0,
                    "board": empty_grid(),
                    "grid": empty_grid(),
                    "cur_piece": None,
                    "spawn_cooldown": 0,
                    "last_attacker_id": None,
                    "last_target_id": None,
                }

                await broadcast_to_room(current_room, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(current_room, include_grid=False),
                }, include_admins=False)

                await send_to_admins(current_room, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(current_room, include_grid=True),
                })

            elif event == "admin_remove_bot":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                target_bot_id = data.get("bot_id")

                if not target_bot_id:
                    bot_ids = [pid for pid, p in room["players"].items() if p.get("is_bot")]
                    target_bot_id = bot_ids[-1] if bot_ids else None

                if target_bot_id and target_bot_id in room["players"] and room["players"][target_bot_id].get("is_bot"):
                    removed_name = room["players"][target_bot_id]["name"]
                    del room["players"][target_bot_id]

                    await send_to_admins(current_room, {
                        "type": "attack_log",
                        "msg": f"🤖 {removed_name} dihapus dari room.",
                    })
                    await broadcast_to_room(current_room, {
                        "type": "lobby_update",
                        "players": get_room_players_summary(current_room, include_grid=False),
                    }, include_admins=False)
                    await send_to_admins(current_room, {
                        "type": "admin_players_sync",
                        "players": get_room_players_summary(current_room, include_grid=True),
                    })
                    await check_winner(current_room)

            elif event == "admin_start_match":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                active_participants = [
                    pid for pid, p in room["players"].items()
                    if p.get("is_bot") or p.get("connected", True)
                ]
                if len(active_participants) < 2:
                    await websocket.send_text(json.dumps({
                        "type": "system_msg",
                        "msg": "Minimal butuh 2 peserta aktif (pemain atau bot) untuk mulai Battle Royale!",
                    }))
                    continue

                room["active"] = True
                room["last_winner"] = None
                for pid, p in list(room["players"].items()):
                    if not p.get("is_bot") and not p.get("connected", True):
                        p["alive"] = False
                        continue
                    p["alive"] = True
                    p["score"] = 0
                    p["ko"] = 0
                    p["board"] = empty_grid()
                    p["grid"] = empty_grid()
                    p["cur_piece"] = None
                    p["spawn_cooldown"] = random.randint(0, 2)
                    p["last_attacker_id"] = None
                    p["last_target_id"] = None

                await broadcast_to_room(current_room, {
                    "type": "match_started",
                    "players": get_room_players_summary(current_room, include_grid=True),
                })

            elif event == "admin_reset_room":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                room["active"] = False
                room["last_winner"] = None
                for pid, p in list(room["players"].items()):
                    if not p.get("is_bot") and not p.get("connected", True):
                        del room["players"][pid]
                        continue
                    p["alive"] = True
                    p["score"] = 0
                    p["ko"] = 0
                    p["board"] = empty_grid()
                    p["grid"] = empty_grid()
                    p["cur_piece"] = None
                    p["spawn_cooldown"] = 0
                    p["last_attacker_id"] = None
                    p["last_target_id"] = None

                await broadcast_to_room(current_room, {
                    "type": "room_reset",
                    "players": get_room_players_summary(current_room, include_grid=True),
                })

            elif event == "join_game":
                room_code = str(data.get("room_code", "")).strip().upper()
                username = str(data.get("username", "")).strip()[:15] or f"Player_{client_id[:4]}"

                if room_code not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "join_error",
                        "msg": f"Room '{room_code}' tidak ditemukan! Pilih room yang tersedia atau minta kode ke Admin.",
                    }))
                    continue

                room = rooms[room_code]

                duplicate_name = any(
                    pid != client_id and p["name"].lower() == username.lower() and (p.get("is_bot") or p.get("connected", True))
                    for pid, p in room["players"].items()
                )
                if duplicate_name:
                    await websocket.send_text(json.dumps({
                        "type": "join_error",
                        "msg": f"Nama '{username}' sudah dipakai peserta lain di room ini. Gunakan nama lain!",
                    }))
                    continue

                existing = room["players"].get(client_id)
                is_mid_game = room["active"]

                if existing and is_mid_game:
                    existing["ws"] = websocket
                    existing["connected"] = True
                    existing["name"] = username
                    can_play_now = existing["alive"]
                else:
                    room["players"][client_id] = {
                        "ws": websocket,
                        "is_bot": False,
                        "connected": True,
                        "name": username,
                        "alive": not is_mid_game,
                        "score": 0,
                        "ko": 0,
                        "grid": empty_grid(),
                        "last_attacker_id": None,
                        "last_target_id": None,
                    }
                    can_play_now = not is_mid_game

                current_room = room_code
                role = "player"

                await websocket.send_text(json.dumps({
                    "type": "joined_success",
                    "room_code": room_code,
                    "active": room["active"],
                    "alive": can_play_now,
                }))

                await broadcast_to_room(room_code, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(room_code, include_grid=False),
                }, include_admins=False)

                await send_to_admins(room_code, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(room_code, include_grid=True),
                })

            elif event == "update_board":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                p = room["players"].get(client_id)
                if p and p.get("alive"):
                    p["score"] = int(data.get("score", 0))
                    p["grid"] = data.get("grid", [])

                    await send_to_admins(current_room, {
                        "type": "admin_screen_update",
                        "pid": client_id,
                        "name": p["name"],
                        "alive": p["alive"],
                        "score": p["score"],
                        "ko": p["ko"],
                        "grid": p["grid"],
                    })

            elif event == "line_cleared":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                p = room["players"].get(client_id)
                if not room["active"] or not p or not p.get("alive"):
                    continue
                lines = int(data.get("lines", 1))
                await perform_attack(current_room, client_id, lines)

            elif event == "player_eliminated":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                killer_id = data.get("last_attacker_id")
                await eliminate_player(current_room, client_id, killer_id)

    except WebSocketDisconnect:
        if current_room and current_room in rooms:
            room = rooms[current_room]
            if role == "player" and client_id in room["players"]:
                p = room["players"][client_id]
                left_name = p["name"]
                was_alive = p["alive"]
                p["connected"] = False
                p["alive"] = False
                p["ws"] = None

                if was_alive:
                    record_stat(left_name, win=0, ko=p["ko"], score=p["score"])

                await broadcast_to_room(current_room, {
                    "type": "attack_log",
                    "msg": f"🚪 {left_name} keluar arena (skor tersimpan di lobby).",
                })
                await broadcast_to_room(current_room, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(current_room, include_grid=False),
                }, include_admins=False)
                await send_to_admins(current_room, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(current_room, include_grid=True),
                })
                await check_winner(current_room)
            elif role == "admin" and client_id in room["admins"]:
                del room["admins"][client_id]


async def check_winner(room_code: str):
    room = rooms.get(room_code)
    if not room or not room["active"]:
        return

    survivors = [
        pid for pid, p in room["players"].items()
        if p["alive"] and (p.get("is_bot") or p.get("connected", True))
    ]

    if len(survivors) == 1:
        winner_id = survivors[0]
        winner = room["players"][winner_id]
        room["active"] = False
        room["last_winner"] = winner["name"]
        record_stat(winner["name"], win=1, ko=winner["ko"], score=winner["score"])
        podium = compute_podium(room, winner_id)

        await broadcast_to_room(room_code, {
            "type": "match_over",
            "winner": winner["name"],
            "ko": winner["ko"],
            "score": winner["score"],
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True),
        })
    elif len(survivors) == 0:
        room["active"] = False
        ranked = sorted(
            room["players"].items(),
            key=lambda item: (item[1]["score"], item[1]["ko"]),
            reverse=True,
        )
        best_pid = ranked[0][0] if ranked else None
        podium = compute_podium(room, None)
        winner_name = ranked[0][1]["name"] if ranked else "Tidak Ada"
        room["last_winner"] = winner_name if ranked else None

        if best_pid:
            w = room["players"][best_pid]
            record_stat(w["name"], win=1, ko=w["ko"], score=w["score"])

        await broadcast_to_room(room_code, {
            "type": "match_over",
            "winner": winner_name,
            "ko": ranked[0][1]["ko"] if ranked else 0,
            "score": ranked[0][1]["score"] if ranked else 0,
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True),
        })
