import os
import json
import random
import string
import sqlite3
import asyncio
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tetris_br.db")
ROWS, COLS = 20, 10

app = FastAPI()

# Struktur rooms:
# {
#   room_code: {
#     "active": bool,
#     "admins": { admin_id: WebSocket },
#     "players": {
#       player_id: {
#         "ws": WebSocket | None,
#         "is_bot": bool,
#         "name": str,
#         "alive": bool,
#         "score": int,
#         "ko": int,
#         "grid": list
#       }
#     }
#   }
# }
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


def get_room_players_summary(room_code: str, include_grid: bool = False):
    room = rooms.get(room_code)
    if not room:
        return {}
    result = {}
    for pid, p in room["players"].items():
        item = {
            "name": p["name"],
            "is_bot": p.get("is_bot", False),
            "alive": p["alive"],
            "ko": p["ko"],
            "score": p["score"]
        }
        if include_grid:
            item["grid"] = p["grid"]
        result[pid] = item
    return result


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
            "reason": "Bertahan Sampai Akhir"
        })

    runners_up = [
        (pid, p) for pid, p in all_players
        if pid != winner_id
    ]
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
            "reason": f"Poin Tertinggi (Rank #{next_rank})"
        })
        next_rank += 1

    return podium


async def broadcast_to_room(room_code: str, message: dict, exclude_id: Optional[str] = None, include_admins: bool = True):
    room = rooms.get(room_code)
    if not room:
        return

    payload = json.dumps(message)
    dead_players = []
    for pid, p in list(room["players"].items()):
        if pid == exclude_id or p.get("is_bot") or not p.get("ws"):
            continue
        try:
            await p["ws"].send_text(payload)
        except Exception:
            dead_players.append(pid)

    for pid in dead_players:
        room["players"].pop(pid, None)

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
    if p.get("is_bot") or not p.get("ws"):
        return
    try:
        await p["ws"].send_text(json.dumps(message))
    except Exception:
        room["players"].pop(player_id, None)


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


async def perform_attack(room_code: str, attacker_id: str, lines: int):
    room = rooms.get(room_code)
    if not room or not room["active"] or attacker_id not in room["players"]:
        return

    attack_map = {1: 1, 2: 2, 3: 3, 4: 5}
    garbage_count = attack_map.get(lines, 1)

    alive_opponents = [
        pid for pid, p in room["players"].items()
        if p["alive"] and pid != attacker_id
    ]
    if not alive_opponents:
        return

    target_id = random.choice(alive_opponents)
    attacker_name = room["players"][attacker_id]["name"]
    target_name = room["players"][target_id]["name"]

    await send_to_player(room_code, target_id, {
        "type": "receive_attack",
        "from_id": attacker_id,
        "from_name": attacker_name,
        "lines": garbage_count
    })

    # Jika target adalah bot, tambahkan baris sampah ke grid bot
    target_player = room["players"].get(target_id)
    if target_player and target_player.get("is_bot") and target_player["alive"]:
        hole = random.randint(0, COLS - 1)
        g = target_player["grid"]
        for _ in range(min(garbage_count, 4)):
            g.pop(0)
            row = [8] * COLS
            row[hole] = 0
            g.append(row)
        target_player["last_attacker_id"] = attacker_id
        await send_to_admins(room_code, {
            "type": "admin_screen_update",
            "pid": target_id,
            "name": target_player["name"],
            "alive": target_player["alive"],
            "score": target_player["score"],
            "ko": target_player["ko"],
            "grid": target_player["grid"]
        })

    await broadcast_to_room(room_code, {
        "type": "attack_log",
        "msg": f"⚡ {attacker_name} mengirim {garbage_count} baris sampah ke {target_name}!",
        "target_id": target_id
    })


async def eliminate_player(room_code: str, victim_id: str, killer_id: Optional[str]):
    room = rooms.get(room_code)
    if not room or victim_id not in room["players"]:
        return
    victim = room["players"][victim_id]
    if not victim["alive"]:
        return

    victim["alive"] = False
    victim_name = victim["name"]

    if killer_id and killer_id in room["players"] and killer_id != victim_id:
        room["players"][killer_id]["ko"] += 1
        killer_name = room["players"][killer_id]["name"]
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": f"💀 {killer_name} meng-KO {victim_name}!"
        })
    else:
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": f"💀 {victim_name} tereliminasi!"
        })

    record_stat(victim_name, win=0, ko=victim["ko"], score=victim["score"])

    await broadcast_to_room(room_code, {
        "type": "lobby_update",
        "players": get_room_players_summary(room_code, include_grid=False)
    }, include_admins=False)

    await send_to_admins(room_code, {
        "type": "admin_players_sync",
        "players": get_room_players_summary(room_code, include_grid=True)
    })

    await check_winner(room_code)


async def bot_loop():
    """Loop simulasi pergerakan, poin, serangan, dan eliminasi bot di semua room aktif."""
    while True:
        await asyncio.sleep(1.1)
        for room_code, room in list(rooms.items()):
            if not room.get("active"):
                continue

            for pid, p in list(room["players"].items()):
                if not p.get("is_bot") or not p.get("alive"):
                    continue

                g = p["grid"]
                if not g or len(g) != ROWS:
                    g = empty_grid()
                    p["grid"] = g

                # Cek apakah tumpukan bot sudah menyentuh baris paling atas (row 0)
                if any(cell != 0 for cell in g[0]):
                    await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                    continue

                action = random.random()
                if action < 0.55:
                    # Tambahkan potongan balok acak di baris terbawah yang masih ada ruang
                    color_id = random.randint(1, 7)
                    block_len = random.randint(2, 4)
                    start_col = random.randint(0, COLS - block_len)
                    placed = False
                    for r in range(ROWS - 1, -1, -1):
                        if all(g[r][c] == 0 for c in range(start_col, start_col + block_len)):
                            for c in range(start_col, start_col + block_len):
                                g[r][c] = color_id
                            placed = True
                            break
                    if not placed:
                        # Dorong baris ke atas jika bagian bawah penuh
                        g.pop(0)
                        new_row = [0] * COLS
                        for c in range(start_col, start_col + block_len):
                            new_row[c] = color_id
                        g.append(new_row)

                # Simulasi hapus baris & serangan random
                if random.random() < 0.28:
                    lines_cleared = random.choice([1, 1, 2, 3, 4])
                    p["score"] += lines_cleared * 100
                    # Bersihkan 1 baris terbawah agar papan bot tetap dinamis
                    g.pop()
                    g.insert(0, [0] * COLS)
                    await perform_attack(room_code, pid, lines_cleared)

                await send_to_admins(room_code, {
                    "type": "admin_screen_update",
                    "pid": pid,
                    "name": p["name"],
                    "alive": p["alive"],
                    "score": p["score"],
                    "ko": p["ko"],
                    "grid": p["grid"]
                })

            await broadcast_to_room(room_code, {
                "type": "lobby_update",
                "players": get_room_players_summary(room_code, include_grid=False)
            }, include_admins=False)


@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(bot_loop())


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


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

            # --- ADMIN EVENTS ---
            if event == "create_room":
                custom_code = str(data.get("room_code", "")).strip().upper()[:8]
                room_code = custom_code if custom_code else generate_room_code()

                if room_code not in rooms:
                    rooms[room_code] = {
                        "active": False,
                        "admins": {},
                        "players": {}
                    }

                rooms[room_code]["admins"][client_id] = websocket
                current_room = room_code
                role = "admin"

                await websocket.send_text(json.dumps({
                    "type": "room_created",
                    "room_code": room_code,
                    "active": rooms[room_code]["active"],
                    "players": get_room_players_summary(room_code, include_grid=True)
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
                    "name": bot_name,
                    "alive": not room["active"],
                    "score": 0,
                    "ko": 0,
                    "grid": empty_grid(),
                    "last_attacker_id": None
                }

                await broadcast_to_room(current_room, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(current_room, include_grid=False)
                }, include_admins=False)

                await send_to_admins(current_room, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(current_room, include_grid=True)
                })

            elif event == "admin_start_match":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if len(room["players"]) < 2:
                    await websocket.send_text(json.dumps({
                        "type": "system_msg",
                        "msg": "Minimal butuh 2 peserta (pemain atau bot) untuk mulai Battle Royale!"
                    }))
                    continue

                room["active"] = True
                for pid in room["players"]:
                    room["players"][pid]["alive"] = True
                    room["players"][pid]["score"] = 0
                    room["players"][pid]["ko"] = 0
                    room["players"][pid]["grid"] = empty_grid()
                    room["players"][pid]["last_attacker_id"] = None

                await broadcast_to_room(current_room, {
                    "type": "match_started",
                    "players": get_room_players_summary(current_room, include_grid=True)
                })

            elif event == "admin_reset_room":
                if role != "admin" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                room["active"] = False
                for pid in room["players"]:
                    room["players"][pid]["alive"] = True
                    room["players"][pid]["score"] = 0
                    room["players"][pid]["ko"] = 0
                    room["players"][pid]["grid"] = empty_grid()
                    room["players"][pid]["last_attacker_id"] = None

                await broadcast_to_room(current_room, {
                    "type": "room_reset",
                    "players": get_room_players_summary(current_room, include_grid=True)
                })

            # --- PLAYER EVENTS ---
            elif event == "join_game":
                room_code = str(data.get("room_code", "")).strip().upper()
                username = str(data.get("username", "")).strip()[:15] or f"Player_{client_id[:4]}"

                if room_code not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "join_error",
                        "msg": f"Room '{room_code}' tidak ditemukan! Minta kode room ke Admin."
                    }))
                    continue

                room = rooms[room_code]
                is_mid_game = room["active"]
                room["players"][client_id] = {
                    "ws": websocket,
                    "is_bot": False,
                    "name": username,
                    "alive": not is_mid_game,
                    "score": 0,
                    "ko": 0,
                    "grid": empty_grid(),
                    "last_attacker_id": None
                }
                current_room = room_code
                role = "player"

                await websocket.send_text(json.dumps({
                    "type": "joined_success",
                    "room_code": room_code,
                    "active": room["active"],
                    "alive": not is_mid_game
                }))

                await broadcast_to_room(room_code, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(room_code, include_grid=False)
                }, include_admins=False)

                await send_to_admins(room_code, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(room_code, include_grid=True)
                })

            elif event == "update_board":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if client_id in room["players"]:
                    room["players"][client_id]["score"] = int(data.get("score", 0))
                    room["players"][client_id]["grid"] = data.get("grid", [])

                    await send_to_admins(current_room, {
                        "type": "admin_screen_update",
                        "pid": client_id,
                        "name": room["players"][client_id]["name"],
                        "alive": room["players"][client_id]["alive"],
                        "score": room["players"][client_id]["score"],
                        "ko": room["players"][client_id]["ko"],
                        "grid": room["players"][client_id]["grid"]
                    })

            elif event == "line_cleared":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not room["active"] or client_id not in room["players"]:
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
                left_name = room["players"][client_id]["name"]
                del room["players"][client_id]
                await broadcast_to_room(current_room, {
                    "type": "attack_log",
                    "msg": f"🚪 {left_name} keluar arena."
                })
                await broadcast_to_room(current_room, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(current_room, include_grid=False)
                }, include_admins=False)
                await send_to_admins(current_room, {
                    "type": "admin_players_sync",
                    "players": get_room_players_summary(current_room, include_grid=True)
                })
                await check_winner(current_room)
            elif role == "admin" and client_id in room["admins"]:
                del room["admins"][client_id]


async def check_winner(room_code: str):
    room = rooms.get(room_code)
    if not room or not room["active"]:
        return

    survivors = [pid for pid, p in room["players"].items() if p["alive"]]
    if len(survivors) == 1:
        winner_id = survivors[0]
        winner = room["players"][winner_id]
        room["active"] = False
        record_stat(winner["name"], win=1, ko=winner["ko"], score=winner["score"])
        podium = compute_podium(room, winner_id)

        await broadcast_to_room(room_code, {
            "type": "match_over",
            "winner": winner["name"],
            "ko": winner["ko"],
            "score": winner["score"],
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True)
        })
    elif len(survivors) == 0:
        room["active"] = False
        podium = compute_podium(room, None)
        await broadcast_to_room(room_code, {
            "type": "match_over",
            "winner": podium[0]["name"] if podium else "Tidak Ada",
            "ko": podium[0]["ko"] if podium else 0,
            "score": podium[0]["score"] if podium else 0,
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True)
        })