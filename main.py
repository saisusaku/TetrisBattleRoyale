import os
import json
import time
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
DOUBLE_DURATION_SEC = 10.0
GRAVITY_INTERVAL_SEC = 0.56
AWAY_QUIT_TIMEOUT_SEC = 45.0

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
countdown_tasks: Dict[str, asyncio.Task] = {}
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


def random_piece_id() -> int:
    return random.randint(1, len(SHAPES))


def spawn_player_piece(p: dict) -> dict:
    pid_shape = int(p.get("next_piece_id") or random_piece_id())
    if pid_shape < 1 or pid_shape > len(SHAPES):
        pid_shape = random_piece_id()
    p["next_piece_id"] = random_piece_id()
    shape = [row[:] for row in SHAPES[pid_shape - 1]]
    x = COLS // 2 - len(shape[0]) // 2
    y = 0
    return {"id": pid_shape, "shape": shape, "x": x, "y": y}


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


def compute_player_view_grid(p: dict):
    base = p.get("board") or p.get("grid") or empty_grid()
    snap = [row[:] for row in base]
    cur = p.get("cur_piece")
    if cur and p.get("alive"):
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


def bot_is_critical(board: List[List[int]]) -> bool:
    for r in range(min(5, ROWS)):
        if any(cell != 0 for cell in board[r]):
            return True
    return False


def is_room_controller(room: dict, client_id: str) -> bool:
    return client_id in room.get("admins", {}) or client_id == room.get("host_id")


def is_double_active_now(player: dict, now_ts: Optional[float] = None) -> bool:
    ts = time.time() if now_ts is None else now_ts
    return float(player.get("double_until", 0.0)) > ts


def cancel_countdown(room_code: str):
    task = countdown_tasks.pop(room_code, None)
    if task and not task.done():
        task.cancel()


def add_player_charge(p: dict, amount: float) -> bool:
    if amount <= 0 or not p.get("alive"):
        return False
    if float(p.get("charge", 0.0)) >= 100.0 or int(p.get("item_tokens", 0)) > 0:
        p["charge"] = 100.0
        p["item_tokens"] = 1
        return False
    new_val = min(100.0, float(p.get("charge", 0.0)) + float(amount))
    p["charge"] = new_val
    if new_val >= 100.0:
        p["charge"] = 100.0
        p["item_tokens"] = 1
        return True
    return False


def reset_player_match_fields(p: dict, alive: bool = True, is_active_match: bool = False):
    p["alive"] = alive
    p["quitter"] = False
    p["score"] = 0
    p["ko"] = 0
    p["board"] = empty_grid()
    p["grid"] = empty_grid()
    p["next_piece_id"] = random_piece_id()
    if p.get("is_bot"):
        p["cur_piece"] = pick_smart_human_target(p) if (alive and is_active_match) else None
    else:
        p["cur_piece"] = spawn_player_piece(p) if (alive and is_active_match) else None
    p["drop_accum"] = 0.0
    p["pending_garbage"] = 0
    p["spawn_cooldown"] = 0
    p["last_attacker_id"] = None
    p["last_target_id"] = None
    p["double_until"] = 0.0
    p["armor_charges"] = 0
    p["charge"] = 0.0
    p["item_tokens"] = 0
    p["last_seen"] = time.time()
    p["away_since"] = None


async def send_player_sync(room_code: str, player_id: str, p: dict):
    now_ts = time.time()
    rem_dbl = max(0.0, float(p.get("double_until", 0.0)) - now_ts)
    cur = p.get("cur_piece")
    active_piece_payload = (
        {"shape": cur["shape"], "x": cur["x"], "y": cur["y"]}
        if cur and p.get("alive")
        else None
    )
    board_only = p.get("board") or p.get("grid") or empty_grid()
    p["grid"] = compute_player_view_grid(p)
    await send_to_player(room_code, player_id, {
        "type": "server_sync",
        "grid": board_only,
        "active_piece": active_piece_payload,
        "next_piece_id": int(p.get("next_piece_id") or 1),
        "score": int(p.get("score", 0)),
        "ko": int(p.get("ko", 0)),
        "alive": bool(p.get("alive", False)),
        "pending_garbage": int(p.get("pending_garbage", 0)),
        "armor_charges": int(p.get("armor_charges", 0)),
        "double_remaining": round(rem_dbl, 2),
        "charge": round(float(p.get("charge", 0.0)), 1),
        "item_tokens": int(p.get("item_tokens", 0)),
    })


async def bot_use_powerup(room_code: str, bot_id: str, bot: dict):
    room = rooms.get(room_code)
    if not room or not room.get("active") or not bot.get("alive"):
        return
    if float(bot.get("charge", 0.0)) < 100.0 and int(bot.get("item_tokens", 0)) <= 0:
        return

    bot["charge"] = 0.0
    bot["item_tokens"] = 0
    board = bot.get("board") or empty_grid()

    if bot_is_critical(board) and bot.get("armor_charges", 0) == 0:
        choice = "armor"
    else:
        choice = random.choice(["double", "armor"])

    if choice == "armor":
        bot["armor_charges"] = 3
        msg = f"🛡️ {bot['name']} mengaktifkan ARMOR (tahan 3 serangan)!"
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": msg,
            "from_id": bot_id,
            "from_name": bot["name"],
            "target_id": bot_id,
            "target_ids": [bot_id],
            "lines": 0,
            "double_active": False,
        })
    else:
        bot["double_until"] = time.time() + DOUBLE_DURATION_SEC
        msg = f"⚡ {bot['name']} mengaktifkan DOUBLE ATTACK selama {int(DOUBLE_DURATION_SEC)} detik!"
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": msg,
            "from_id": bot_id,
            "from_name": bot["name"],
            "target_id": bot_id,
            "target_ids": [bot_id],
            "lines": 0,
            "double_active": True,
        })

    await broadcast_room_state(room_code)


def evaluate_board(board: List[List[int]], lines_cleared: int) -> float:
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

    top_blocked = 12.0 if any(cell != 0 for cell in kept[0]) else 0.0
    score_val = evaluate_board(kept, cleared) - top_blocked
    return y, score_val


def pick_smart_human_target(bot: dict):
    board = bot["board"]
    base_shape = random.choice(SHAPES)

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
        "hesitate_ticks": 0,
    }


def dedup_entries(raw_items: List[dict]) -> List[dict]:
    merged: Dict[str, dict] = {}
    for item in raw_items:
        key = (item.get("name") or "").strip().lower()
        if not key:
            key = f"__id_{item.get('pid', id(item))}"

        if key not in merged:
            merged[key] = dict(item)
        else:
            existing = merged[key]
            existing["score"] = max(int(existing.get("score", 0)), int(item.get("score", 0)))
            existing["ko"] = max(int(existing.get("ko", 0)), int(item.get("ko", 0)))
            existing["alive"] = bool(existing.get("alive")) or bool(item.get("alive"))
            existing["connected"] = bool(existing.get("connected")) or bool(item.get("connected"))
            existing["quitter"] = bool(existing.get("quitter")) and bool(item.get("quitter"))
    return list(merged.values())


def get_room_players_summary(room_code: str, include_grid: bool = False):
    room = rooms.get(room_code)
    if not room:
        return {}
    now_ts = time.time()
    result = {}
    for pid, p in room["players"].items():
        is_quitter = (not p.get("is_bot")) and (p.get("quitter", False) or not p.get("connected", True))
        rem_dbl = max(0.0, float(p.get("double_until", 0.0)) - now_ts)
        item = {
            "name": p["name"],
            "is_bot": p.get("is_bot", False),
            "connected": p.get("connected", True),
            "quitter": is_quitter,
            "alive": p["alive"],
            "ko": p["ko"],
            "score": p["score"],
            "armor_charges": p.get("armor_charges", 0),
            "double_active": rem_dbl > 0,
            "double_remaining": round(rem_dbl, 1),
            "charge": round(float(p.get("charge", 0.0)), 1),
            "item_tokens": int(p.get("item_tokens", 0)),
        }
        if include_grid:
            item["grid"] = compute_bot_view_grid(p) if p.get("is_bot") else compute_player_view_grid(p)
        result[pid] = item
    return result


def get_lobby_list():
    lobby = []
    for code, room in rooms.items():
        has_admin = len(room.get("admins", {})) > 0
        has_players = len(room.get("players", {})) > 0
        has_host = bool(room.get("host_id"))
        if not has_admin and not has_players and not has_host:
            continue

        if room.get("active"):
            state = "playing"
            status_label = "🔥 Sedang Main"
        elif room.get("countdown"):
            state = "countdown"
            status_label = f"⏳ Hitung Mundur {room['countdown']}"
        elif room.get("last_winner"):
            state = "finished"
            status_label = "🏆 Selesai (Bisa Gabung)"
        else:
            state = "waiting"
            status_label = "⏳ Menunggu Pemain"

        raw_board = [
            {
                "pid": pid,
                "name": p["name"],
                "score": p["score"],
                "ko": p["ko"],
                "alive": p["alive"],
                "connected": p.get("connected", True),
                "quitter": (not p.get("is_bot")) and (p.get("quitter", False) or not p.get("connected", True)),
                "is_bot": p.get("is_bot", False),
            }
            for pid, p in room.get("players", {}).items()
        ]
        scoreboard = sorted(
            dedup_entries(raw_board),
            key=lambda x: (x["score"], x["ko"]),
            reverse=True,
        )

        total_participants = len(scoreboard)
        alive_count = sum(1 for x in scoreboard if x.get("alive"))
        bot_count = sum(1 for x in scoreboard if x.get("is_bot"))

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
    seen_names = set()

    if winner_id and winner_id in room["players"]:
        w = room["players"][winner_id]
        w_key = (w.get("name") or winner_id).strip().lower()
        seen_names.add(w_key)
        podium.append({
            "rank": 1,
            "id": winner_id,
            "pid": winner_id,
            "name": w["name"],
            "is_bot": w.get("is_bot", False),
            "score": w["score"],
            "ko": w["ko"],
            "reason": "Bertahan Sampai Akhir",
        })

    runners_up = dedup_entries([
        {
            "pid": pid,
            "name": p["name"],
            "is_bot": p.get("is_bot", False),
            "score": p["score"],
            "ko": p["ko"],
        }
        for pid, p in room["players"].items()
        if pid != winner_id and (p.get("name") or pid).strip().lower() not in seen_names
    ])
    runners_up.sort(key=lambda item: (item["score"], item["ko"]), reverse=True)

    next_rank = 2 if podium else 1
    for item in runners_up:
        if next_rank > 3:
            break
        podium.append({
            "rank": next_rank,
            "id": item["pid"],
            "pid": item["pid"],
            "name": item["name"],
            "is_bot": item.get("is_bot", False),
            "score": item["score"],
            "ko": item["ko"],
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
            p["quitter"] = True
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


async def broadcast_room_state(room_code: str):
    room = rooms.get(room_code)
    if not room:
        return
    if room.get("active"):
        state_str = "playing"
    elif room.get("countdown"):
        state_str = "countdown"
    elif room.get("last_winner"):
        state_str = "finished"
    else:
        state_str = "waiting"

    players_with_grid = get_room_players_summary(room_code, include_grid=True)
    players_no_grid = get_room_players_summary(room_code, include_grid=False)

    await send_to_admins(room_code, {
        "type": "room_state",
        "room_code": room_code,
        "host_id": room.get("host_id"),
        "state": state_str,
        "active": room.get("active", False),
        "countdown": room.get("countdown"),
        "players": players_with_grid,
    })

    payload = json.dumps({
        "type": "room_state",
        "room_code": room_code,
        "host_id": room.get("host_id"),
        "state": state_str,
        "active": room.get("active", False),
        "countdown": room.get("countdown"),
        "players": players_no_grid,
    })
    for pid, p in list(room["players"].items()):
        if p.get("is_bot") or not p.get("connected") or not p.get("ws"):
            continue
        try:
            await p["ws"].send_text(payload)
        except Exception:
            p["connected"] = False
            p["quitter"] = True
            p["alive"] = False
            p["ws"] = None


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
        p["quitter"] = True
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


def reassign_host_if_needed(room: dict, leaving_id: str):
    if room.get("host_id") == leaving_id:
        next_host = next(
            (
                pid for pid, p in room.get("players", {}).items()
                if pid != leaving_id and not p.get("is_bot") and p.get("connected", True) and not p.get("quitter", False)
            ),
            None,
        )
        room["host_id"] = next_host


async def mark_player_quit(room_code: str, player_id: str, reason_msg: Optional[str] = None):
    room = rooms.get(room_code)
    if not room or player_id not in room["players"]:
        return

    p = room["players"][player_id]
    if p.get("is_bot"):
        return

    already_quitter = bool(p.get("quitter")) and not bool(p.get("alive")) and not bool(p.get("connected", True))
    was_alive = bool(p.get("alive"))
    left_name = p["name"]

    p["connected"] = False
    p["quitter"] = True
    p["alive"] = False
    p["cur_piece"] = None
    p["ws"] = None
    reassign_host_if_needed(room, player_id)

    if already_quitter:
        if room.get("active"):
            await check_winner(room_code)
        return

    if was_alive:
        record_stat(left_name, win=0, ko=p.get("ko", 0), score=p.get("score", 0))

    await broadcast_to_room(room_code, {
        "type": "attack_log",
        "msg": reason_msg or f"🟨 {left_name} keluar arena (QUITTER - tampil sampai ronde selesai/reset).",
    })
    await broadcast_room_state(room_code)

    if room.get("active"):
        await check_winner(room_code)


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


async def perform_attack(room_code: str, attacker_id: str, lines: int, client_double: bool = False):
    room = rooms.get(room_code)
    if not room or not room["active"] or attacker_id not in room["players"]:
        return

    attack_map = {1: 1, 2: 2, 3: 3, 4: 5}
    garbage_count = attack_map.get(lines, 1)

    attacker = room["players"][attacker_id]
    dbl_active = is_double_active_now(attacker) or bool(client_double)
    if dbl_active:
        garbage_count *= 2

    alive_opponents = [
        pid for pid, p in room["players"].items()
        if p["alive"] and (p.get("is_bot") or p.get("connected", True)) and pid != attacker_id
    ]
    if not alive_opponents:
        return

    candidates = [pid for pid in alive_opponents if pid != attacker.get("last_target_id")]
    target_id = random.choice(candidates if candidates else alive_opponents)
    attacker["last_target_id"] = target_id

    attacker_name = attacker["name"]
    target_player = room["players"][target_id]
    target_name = target_player["name"]
    dbl_tag = " [2X DOUBLE]" if dbl_active else ""

    if target_player.get("armor_charges", 0) > 0:
        target_player["armor_charges"] -= 1
        rem = target_player["armor_charges"]
        if not target_player.get("is_bot"):
            await send_to_player(room_code, target_id, {
                "type": "armor_blocked",
                "from_id": attacker_id,
                "from_name": attacker_name,
                "blocked_lines": garbage_count,
                "remaining_charges": rem,
            })
        await broadcast_to_room(room_code, {
            "type": "attack_log",
            "msg": f"🛡️ Armor {target_name} menangkis {garbage_count} baris{dbl_tag} dari {attacker_name}! (Sisa {rem}x)",
            "from_id": attacker_id,
            "from_name": attacker_name,
            "target_id": target_id,
            "target_ids": [target_id],
            "lines": garbage_count,
            "double_active": dbl_active,
        })
        await broadcast_room_state(room_code)
        return

    await send_to_player(room_code, target_id, {
        "type": "receive_attack",
        "from_id": attacker_id,
        "from_name": attacker_name,
        "lines": garbage_count,
    })

    if target_player.get("is_bot") and target_player["alive"]:
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
    elif not target_player.get("is_bot") and target_player["alive"]:
        target_player["pending_garbage"] = int(target_player.get("pending_garbage", 0)) + min(garbage_count, 6)
        target_player["last_attacker_id"] = attacker_id

    await broadcast_to_room(room_code, {
        "type": "attack_log",
        "msg": f"⚡ {attacker_name} mengirim {garbage_count} baris sampah{dbl_tag} ke {target_name}!",
        "from_id": attacker_id,
        "from_name": attacker_name,
        "target_id": target_id,
        "target_ids": [target_id],
        "lines": garbage_count,
        "double_active": dbl_active,
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

    if not victim.get("is_bot"):
        await send_player_sync(room_code, victim_id, victim)

    record_stat(victim_name, win=0, ko=victim["ko"], score=victim["score"])
    await broadcast_room_state(room_code)
    await check_winner(room_code)


async def step_human_player(room_code: str, pid: str, p: dict, dt: float) -> bool:
    room = rooms.get(room_code)
    if not room or not room.get("active") or not p.get("alive") or p.get("quitter"):
        return False

    board = p.get("board")
    if not board or len(board) != ROWS:
        board = empty_grid()
        p["board"] = board

    cur = p.get("cur_piece")
    if not cur:
        cur = spawn_player_piece(p)
        if bot_collide(board, cur["shape"], cur["x"], cur["y"]):
            await eliminate_player(room_code, pid, p.get("last_attacker_id"))
            return True
        p["cur_piece"] = cur

    p["drop_accum"] = float(p.get("drop_accum", 0.0)) + dt
    moved = False

    while p.get("alive") and room.get("active") and float(p.get("drop_accum", 0.0)) >= GRAVITY_INTERVAL_SEC:
        p["drop_accum"] -= GRAVITY_INTERVAL_SEC
        cur = p.get("cur_piece")
        if not cur:
            cur = spawn_player_piece(p)
            p["cur_piece"] = cur

        if not bot_collide(board, cur["shape"], cur["x"], cur["y"] + 1):
            cur["y"] += 1
            moved = True
        else:
            for r_idx, row in enumerate(cur["shape"]):
                for c_idx, val in enumerate(row):
                    if val:
                        nr = cur["y"] + r_idx
                        nc = cur["x"] + c_idx
                        if 0 <= nr < ROWS and 0 <= nc < COLS:
                            board[nr][nc] = val

            p["cur_piece"] = None
            moved = True

            cleared_lines = 0
            new_board = []
            for r in board:
                if all(cell != 0 for cell in r):
                    cleared_lines += 1
                else:
                    new_board.append(r)
            while len(new_board) < ROWS:
                new_board.insert(0, [0] * COLS)
            board = new_board
            p["board"] = board

            if cleared_lines > 0:
                pts = [0, 100, 300, 500, 800][cleared_lines] if cleared_lines <= 4 else cleared_lines * 200
                p["score"] = int(p.get("score", 0)) + pts
                line_charge_map = {1: 10.0, 2: 22.0, 3: 35.0, 4: 50.0}
                gain = line_charge_map.get(cleared_lines, cleared_lines * 12.0)
                just_full = add_player_charge(p, gain)
                if just_full:
                    await send_to_player(room_code, pid, {
                        "type": "charge_update",
                        "charge": p["charge"],
                        "item_tokens": p["item_tokens"],
                        "full_now": True,
                    })
                await perform_attack(room_code, pid, cleared_lines)
                if not room.get("active"):
                    return True

            pending = int(p.get("pending_garbage", 0))
            if pending > 0:
                append_random_garbage_rows(board, min(pending, 6))
                p["pending_garbage"] = 0
                p["board"] = board

            if any(cell != 0 for cell in board[0]):
                await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                return True

            nxt = spawn_player_piece(p)
            if bot_collide(board, nxt["shape"], nxt["x"], nxt["y"]):
                await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                return True
            p["cur_piece"] = nxt

    p["grid"] = compute_player_view_grid(p)
    return moved


async def apply_player_input_action(room_code: str, pid: str, p: dict, action: str):
    room = rooms.get(room_code)
    if not room or not room.get("active") or not p.get("alive") or p.get("quitter"):
        return

    board = p.get("board")
    if not board or len(board) != ROWS:
        board = empty_grid()
        p["board"] = board

    cur = p.get("cur_piece")
    if not cur:
        cur = spawn_player_piece(p)
        p["cur_piece"] = cur

    if action == "left":
        if not bot_collide(board, cur["shape"], cur["x"] - 1, cur["y"]):
            cur["x"] -= 1
    elif action == "right":
        if not bot_collide(board, cur["shape"], cur["x"] + 1, cur["y"]):
            cur["x"] += 1
    elif action == "rotate":
        rot = rotate_matrix(cur["shape"])
        if not bot_collide(board, rot, cur["x"], cur["y"]):
            cur["shape"] = rot
        elif not bot_collide(board, rot, cur["x"] - 1, cur["y"]):
            cur["x"] -= 1
            cur["shape"] = rot
        elif not bot_collide(board, rot, cur["x"] + 1, cur["y"]):
            cur["x"] += 1
            cur["shape"] = rot
    elif action == "down":
        if not bot_collide(board, cur["shape"], cur["x"], cur["y"] + 1):
            cur["y"] += 1
        else:
            p["drop_accum"] = GRAVITY_INTERVAL_SEC
            await step_human_player(room_code, pid, p, 0.0)
    elif action == "hard_drop":
        drop_dist = 0
        while not bot_collide(board, cur["shape"], cur["x"], cur["y"] + 1):
            cur["y"] += 1
            drop_dist += 1
        p["score"] = int(p.get("score", 0)) + drop_dist * 2
        p["drop_accum"] = GRAVITY_INTERVAL_SEC
        await step_human_player(room_code, pid, p, 0.0)

    p["grid"] = compute_player_view_grid(p)


async def run_match_countdown(room_code: str):
    try:
        for num in [3, 2, 1]:
            room = rooms.get(room_code)
            if not room or not room.get("countdown"):
                return
            room["countdown"] = num
            await broadcast_to_room(room_code, {
                "type": "countdown",
                "count": num,
                "label": str(num),
            })
            await asyncio.sleep(0.9)

        room = rooms.get(room_code)
        if not room:
            return

        room["countdown"] = None
        room["active"] = True
        room["last_winner"] = None

        for pid, p in list(room["players"].items()):
            if not p.get("is_bot") and (not p.get("connected", True) or p.get("quitter", False)):
                p["alive"] = False
                p["quitter"] = True
                p["cur_piece"] = None
                continue
            reset_player_match_fields(p, alive=True, is_active_match=True)

        await broadcast_to_room(room_code, {
            "type": "countdown",
            "count": 0,
            "label": "MULAI!",
        })
        await broadcast_to_room(room_code, {
            "type": "match_started",
            "players": get_room_players_summary(room_code, include_grid=True),
        })
        for pid, p in list(room["players"].items()):
            if not p.get("is_bot") and p.get("connected", True) and not p.get("quitter", False):
                await send_player_sync(room_code, pid, p)
        await broadcast_room_state(room_code)
    except asyncio.CancelledError:
        room = rooms.get(room_code)
        if room:
            room["countdown"] = None
        raise
    finally:
        countdown_tasks.pop(room_code, None)


async def game_loop():
    tick_sec = 0.14
    loop_idx = 0
    while True:
        await asyncio.sleep(tick_sec)
        loop_idx += 1
        now_ts = time.time()
        for room_code, room in list(rooms.items()):
            if not room.get("active"):
                continue

            any_moved = False
            admin_frames = []

            for pid, p in list(room["players"].items()):
                if p.get("is_bot") or not p.get("alive"):
                    continue
                ws = p.get("ws")
                if not p.get("connected", True) or ws is None or getattr(ws.client_state, "name", "") != "CONNECTED":
                    await mark_player_quit(room_code, pid)
                    continue

                away_since = p.get("away_since")
                if away_since is not None and (now_ts - float(away_since)) >= AWAY_QUIT_TIMEOUT_SEC:
                    await mark_player_quit(
                        room_code,
                        pid,
                        f"🟨 {p['name']} meninggalkan game >45 detik (QUITTER).",
                    )
                    continue

                last_seen = float(p.get("last_seen", now_ts))
                if (now_ts - last_seen) >= AWAY_QUIT_TIMEOUT_SEC:
                    await mark_player_quit(
                        room_code,
                        pid,
                        f"🟨 {p['name']} tidak merespons >45 detik (QUITTER).",
                    )
                    continue

                just_full = add_player_charge(p, 1.0 * tick_sec)
                if just_full:
                    await send_to_player(room_code, pid, {
                        "type": "charge_update",
                        "charge": p["charge"],
                        "item_tokens": p["item_tokens"],
                        "full_now": True,
                    })

                moved_h = await step_human_player(room_code, pid, p, tick_sec)
                if moved_h or loop_idx % 2 == 0:
                    await send_player_sync(room_code, pid, p)
                    any_moved = True
                    admin_frames.append({
                        "pid": pid,
                        "name": p["name"],
                        "alive": p["alive"],
                        "score": p["score"],
                        "ko": p["ko"],
                        "armor_charges": p.get("armor_charges", 0),
                        "grid": compute_player_view_grid(p),
                    })

            if not room.get("active"):
                continue

            for pid, p in list(room["players"].items()):
                if not p.get("is_bot") or not p.get("alive"):
                    continue

                board = p.get("board")
                if not board or len(board) != ROWS:
                    board = empty_grid()
                    p["board"] = board

                crit_bonus = 1.5 if bot_is_critical(board) else 0.0
                add_player_charge(p, (1.0 + crit_bonus) * tick_sec)
                if float(p.get("charge", 0.0)) >= 100.0 or int(p.get("item_tokens", 0)) > 0:
                    await bot_use_powerup(room_code, pid, p)

                if p.get("spawn_cooldown", 0) > 0:
                    p["spawn_cooldown"] -= 1
                else:
                    cur = p.get("cur_piece")
                    if not cur:
                        new_piece = pick_smart_human_target(p)
                        if bot_collide(board, new_piece["shape"], new_piece["x"], new_piece["y"]):
                            await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                            continue
                        p["cur_piece"] = new_piece
                        cur = new_piece

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
                            p["spawn_cooldown"] = 0

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
                                line_charge_map = {1: 10.0, 2: 22.0, 3: 35.0, 4: 50.0}
                                gain = line_charge_map.get(cleared_lines, cleared_lines * 12.0)
                                add_player_charge(p, gain)
                                if float(p.get("charge", 0.0)) >= 100.0 or int(p.get("item_tokens", 0)) > 0:
                                    await bot_use_powerup(room_code, pid, p)
                                await perform_attack(room_code, pid, cleared_lines)

                            if any(cell != 0 for cell in p["board"][0]):
                                await eliminate_player(room_code, pid, p.get("last_attacker_id"))
                                continue

                any_moved = True
                admin_frames.append({
                    "pid": pid,
                    "name": p["name"],
                    "alive": p["alive"],
                    "score": p["score"],
                    "ko": p["ko"],
                    "armor_charges": p.get("armor_charges", 0),
                    "grid": compute_bot_view_grid(p),
                })

            if admin_frames:
                await send_to_admins(room_code, {
                    "type": "admin_batch_update",
                    "frames": admin_frames,
                })

            if any_moved and loop_idx % 2 == 0:
                await broadcast_to_room(room_code, {
                    "type": "lobby_update",
                    "players": get_room_players_summary(room_code, include_grid=False),
                })


@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(game_loop())


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


@app.post("/api/quit/{room_code}/{client_id}")
async def api_player_quit(room_code: str, client_id: str):
    code = room_code.strip().upper()
    if code in rooms and client_id in rooms[code]["players"]:
        p = rooms[code]["players"][client_id]
        await mark_player_quit(code, client_id, f"🟨 {p['name']} meninggalkan halaman (QUITTER).")
    return {"ok": True}


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

            if current_room and current_room in rooms and client_id in rooms[current_room]["players"]:
                p_seen = rooms[current_room]["players"][client_id]
                p_seen["last_seen"] = time.time()
                if event not in ("player_away", "ping"):
                    p_seen["away_since"] = None

            if event == "ping":
                if current_room and current_room in rooms and client_id in rooms[current_room]["players"]:
                    p_ping = rooms[current_room]["players"][client_id]
                    p_ping["last_seen"] = time.time()
                    if not data.get("is_away"):
                        p_ping["away_since"] = None
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if event == "player_away":
                if role == "player" and current_room and current_room in rooms:
                    p = rooms[current_room]["players"].get(client_id)
                    if p and p.get("alive") and not p.get("quitter", False):
                        is_away = bool(data.get("away", True))
                        p["last_seen"] = time.time()
                        if is_away:
                            if p.get("away_since") is None:
                                p["away_since"] = time.time()
                        else:
                            p["away_since"] = None
                            await send_player_sync(current_room, client_id, p)
                continue

            if event == "create_room":
                custom_code = str(data.get("room_code", "")).strip().upper()[:8]
                room_code = custom_code if custom_code else generate_room_code()
                as_player = bool(data.get("as_player", False))
                host_username = str(data.get("username", "")).strip()[:15] or f"Host_{client_id[:4]}"

                cancel_countdown(room_code)

                if room_code in rooms:
                    old_room = rooms[room_code]
                    old_payload = json.dumps({
                        "type": "room_closed",
                        "msg": f"Room '{room_code}' di-reset oleh Admin/Host baru.",
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

                if as_player:
                    rooms[room_code] = {
                        "active": False,
                        "countdown": None,
                        "last_winner": None,
                        "host_id": client_id,
                        "admins": {},
                        "players": {
                            client_id: {
                                "ws": websocket,
                                "is_bot": False,
                                "connected": True,
                                "quitter": False,
                                "name": host_username,
                                "alive": True,
                                "score": 0,
                                "ko": 0,
                                "board": empty_grid(),
                                "grid": empty_grid(),
                                "next_piece_id": random_piece_id(),
                                "cur_piece": None,
                                "drop_accum": 0.0,
                                "pending_garbage": 0,
                                "last_attacker_id": None,
                                "last_target_id": None,
                                "double_until": 0.0,
                                "armor_charges": 0,
                                "charge": 0.0,
                                "item_tokens": 0,
                                "last_seen": time.time(),
                                "away_since": None,
                            }
                        },
                    }
                    current_room = room_code
                    role = "player"

                    await websocket.send_text(json.dumps({
                        "type": "joined_room",
                        "room_code": room_code,
                        "state": "waiting",
                        "active": False,
                        "alive": True,
                        "is_host": True,
                        "host_id": client_id,
                        "players": get_room_players_summary(room_code, include_grid=False),
                    }))
                    await broadcast_room_state(room_code)
                else:
                    rooms[room_code] = {
                        "active": False,
                        "countdown": None,
                        "last_winner": None,
                        "host_id": None,
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
                if not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not is_room_controller(room, client_id):
                    continue

                bot_id = f"bot_{bot_counter}"
                bot_name = f"Bot_{bot_counter}"
                bot_counter += 1

                room["players"][bot_id] = {
                    "ws": None,
                    "is_bot": True,
                    "connected": True,
                    "quitter": False,
                    "name": bot_name,
                    "alive": True,
                    "score": 0,
                    "ko": 0,
                    "board": empty_grid(),
                    "grid": empty_grid(),
                    "cur_piece": pick_smart_human_target({"board": empty_grid()}) if room.get("active") else None,
                    "spawn_cooldown": 0,
                    "last_attacker_id": None,
                    "last_target_id": None,
                    "double_until": 0.0,
                    "armor_charges": 0,
                    "charge": 0.0,
                    "item_tokens": 0,
                }

                await broadcast_room_state(current_room)

            elif event == "admin_remove_bot":
                if not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not is_room_controller(room, client_id):
                    continue

                target_bot_id = data.get("bot_id")
                if not target_bot_id:
                    bot_ids = [pid for pid, p in room["players"].items() if p.get("is_bot")]
                    target_bot_id = bot_ids[-1] if bot_ids else None

                if target_bot_id and target_bot_id in room["players"] and room["players"][target_bot_id].get("is_bot"):
                    removed_name = room["players"][target_bot_id]["name"]
                    del room["players"][target_bot_id]

                    await broadcast_to_room(current_room, {
                        "type": "attack_log",
                        "msg": f"🤖 {removed_name} dihapus dari room.",
                    })
                    await broadcast_room_state(current_room)
                    await check_winner(current_room)

            elif event == "admin_start_match":
                if not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not is_room_controller(room, client_id):
                    continue

                active_participants = [
                    pid for pid, p in room["players"].items()
                    if p.get("is_bot") or (p.get("connected", True) and not p.get("quitter", False))
                ]
                if len(active_participants) < 2:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Minimal butuh 2 peserta aktif (pemain atau bot) untuk mulai Battle Royale!",
                    }))
                    continue

                if room_code_task := countdown_tasks.get(current_room):
                    if not room_code_task.done():
                        continue

                room["active"] = False
                room["countdown"] = 3
                room["last_winner"] = None
                await broadcast_room_state(current_room)
                countdown_tasks[current_room] = asyncio.create_task(run_match_countdown(current_room))

            elif event == "admin_reset_room":
                if not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not is_room_controller(room, client_id):
                    continue

                cancel_countdown(current_room)
                room["active"] = False
                room["countdown"] = None
                room["last_winner"] = None
                for pid, p in list(room["players"].items()):
                    if not p.get("is_bot") and (not p.get("connected", True) or p.get("quitter", False)):
                        del room["players"][pid]
                        continue
                    reset_player_match_fields(p, alive=True, is_active_match=False)

                await broadcast_to_room(current_room, {
                    "type": "room_reset",
                    "players": get_room_players_summary(current_room, include_grid=True),
                })
                await broadcast_room_state(current_room)

            elif event == "join_game":
                room_code = str(data.get("room_code", "")).strip().upper()
                username = str(data.get("username", "")).strip()[:15] or f"Player_{client_id[:4]}"

                if room_code not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Room '{room_code}' tidak ditemukan! Pilih room yang tersedia atau buat room baru.",
                    }))
                    continue

                room = rooms[room_code]

                stale_same_name_ids = [
                    pid for pid, p in list(room["players"].items())
                    if pid != client_id
                    and not p.get("is_bot")
                    and p["name"].lower() == username.lower()
                    and (not p.get("connected", True) or p.get("quitter", False) or not p.get("alive", False))
                ]
                for stale_id in stale_same_name_ids:
                    del room["players"][stale_id]

                duplicate_name = any(
                    pid != client_id
                    and p["name"].lower() == username.lower()
                    and (p.get("is_bot") or (p.get("connected", True) and not p.get("quitter", False) and p.get("alive", True)))
                    for pid, p in room["players"].items()
                )
                if duplicate_name:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Nama '{username}' sudah dipakai peserta aktif di room ini. Gunakan nama lain!",
                    }))
                    continue

                existing = room["players"].get(client_id)
                is_mid_game = room["active"] or bool(room.get("countdown"))

                if existing and is_mid_game and not existing.get("quitter", False):
                    existing["ws"] = websocket
                    existing["connected"] = True
                    existing["quitter"] = False
                    existing["name"] = username
                    existing["last_seen"] = time.time()
                    existing["away_since"] = None
                    if existing["alive"] and room["active"] and not existing.get("cur_piece"):
                        existing["cur_piece"] = spawn_player_piece(existing)
                    can_play_now = existing["alive"]
                else:
                    new_p = {
                        "ws": websocket,
                        "is_bot": False,
                        "connected": True,
                        "quitter": False,
                        "name": username,
                        "alive": not is_mid_game,
                        "score": 0,
                        "ko": 0,
                        "board": empty_grid(),
                        "grid": empty_grid(),
                        "next_piece_id": random_piece_id(),
                        "cur_piece": None,
                        "drop_accum": 0.0,
                        "pending_garbage": 0,
                        "last_attacker_id": None,
                        "last_target_id": None,
                        "double_until": 0.0,
                        "armor_charges": 0,
                        "charge": 0.0,
                        "item_tokens": 0,
                        "last_seen": time.time(),
                        "away_since": None,
                    }
                    if not is_mid_game:
                        new_p["cur_piece"] = None
                    room["players"][client_id] = new_p
                    can_play_now = not is_mid_game

                if not room.get("host_id") and not room.get("admins"):
                    room["host_id"] = client_id

                current_room = room_code
                role = "player"
                state_str = "playing" if room["active"] else ("countdown" if room.get("countdown") else ("finished" if room.get("last_winner") else "waiting"))

                await websocket.send_text(json.dumps({
                    "type": "joined_room",
                    "room_code": room_code,
                    "state": state_str,
                    "active": room["active"],
                    "countdown": room.get("countdown"),
                    "alive": can_play_now,
                    "is_host": room.get("host_id") == client_id,
                    "host_id": room.get("host_id"),
                    "players": get_room_players_summary(room_code, include_grid=False),
                }))
                await send_player_sync(room_code, client_id, room["players"][client_id])
                await broadcast_room_state(room_code)

            elif event == "player_quit":
                if role == "player" and current_room and current_room in rooms:
                    p = rooms[current_room]["players"].get(client_id)
                    msg = f"🟨 {p['name']} meninggalkan halaman (QUITTER)." if p else None
                    await mark_player_quit(current_room, client_id, msg)

            elif event == "player_input":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                p = room["players"].get(client_id)
                if not room.get("active") or not p or not p.get("alive") or p.get("quitter", False):
                    continue
                action = str(data.get("action", "")).strip()
                await apply_player_input_action(current_room, client_id, p, action)
                await send_player_sync(current_room, client_id, p)
                await send_to_admins(current_room, {
                    "type": "admin_screen_update",
                    "pid": client_id,
                    "name": p["name"],
                    "alive": p["alive"],
                    "score": p["score"],
                    "ko": p["ko"],
                    "grid": compute_player_view_grid(p),
                })

            elif event == "use_item":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                if not room.get("active"):
                    continue
                p = room["players"].get(client_id)
                if not p or not p.get("alive"):
                    continue

                if float(p.get("charge", 0.0)) < 100.0 and int(p.get("item_tokens", 0)) <= 0:
                    continue

                item_type = data.get("item")
                if item_type == "armor":
                    p["armor_charges"] = 3
                    p["charge"] = 0.0
                    p["item_tokens"] = 0
                    await send_player_sync(current_room, client_id, p)
                    await broadcast_to_room(current_room, {
                        "type": "attack_log",
                        "msg": f"🛡️ {p['name']} mengaktifkan ARMOR (tahan 3 serangan)!",
                    })
                    await broadcast_room_state(current_room)
                elif item_type == "double":
                    p["double_until"] = time.time() + DOUBLE_DURATION_SEC
                    p["charge"] = 0.0
                    p["item_tokens"] = 0
                    await send_to_player(current_room, client_id, {
                        "type": "item_activated",
                        "item": "double",
                        "double_seconds": int(DOUBLE_DURATION_SEC),
                    })
                    await send_player_sync(current_room, client_id, p)
                    await broadcast_to_room(current_room, {
                        "type": "attack_log",
                        "msg": f"⚡ {p['name']} mengaktifkan DOUBLE ATTACK selama {int(DOUBLE_DURATION_SEC)} detik!",
                    })
                    await broadcast_room_state(current_room)
                elif item_type == "breaker":
                    p["charge"] = 0.0
                    p["item_tokens"] = 0
                    board = p.get("board") or empty_grid()
                    non_empty_rows = [idx for idx, row in enumerate(board) if any(cell != 0 for cell in row)]
                    if non_empty_rows:
                        to_remove = random.sample(non_empty_rows, k=min(2, len(non_empty_rows)))
                        kept = [row for idx, row in enumerate(board) if idx not in to_remove]
                        while len(kept) < ROWS:
                            kept.insert(0, [0] * COLS)
                        p["board"] = kept
                    p["grid"] = compute_player_view_grid(p)
                    await send_player_sync(current_room, client_id, p)
                    await broadcast_to_room(current_room, {
                        "type": "attack_log",
                        "msg": f"💥 {p['name']} mengaktifkan BREAKER (menghapus baris tumpukan)!",
                    })
                    await broadcast_room_state(current_room)

            elif event == "update_board":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                p = room["players"].get(client_id)
                if p and p.get("alive") and not p.get("quitter", False):
                    p["score"] = max(int(p.get("score", 0)), int(data.get("score", 0)))
                    if isinstance(data.get("grid"), list) and len(data["grid"]) == ROWS:
                        p["board"] = data["grid"]
                        p["grid"] = compute_player_view_grid(p)

                    await send_to_admins(current_room, {
                        "type": "admin_screen_update",
                        "pid": client_id,
                        "name": p["name"],
                        "alive": p["alive"],
                        "score": p["score"],
                        "ko": p["ko"],
                        "grid": compute_player_view_grid(p),
                    })

            elif event == "line_cleared":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                room = rooms[current_room]
                p = room["players"].get(client_id)
                if not room["active"] or not p or not p.get("alive") or p.get("quitter", False):
                    continue
                lines = max(1, int(data.get("lines", 1)))
                client_double = bool(data.get("double_active", False))

                line_charge_map = {1: 10.0, 2: 22.0, 3: 35.0, 4: 50.0}
                gain = line_charge_map.get(lines, lines * 12.0)
                just_full = add_player_charge(p, gain)
                await send_to_player(current_room, client_id, {
                    "type": "charge_update",
                    "charge": p["charge"],
                    "item_tokens": p["item_tokens"],
                    "full_now": just_full,
                })

                await perform_attack(current_room, client_id, lines, client_double=client_double)

            elif event == "player_eliminated":
                if role != "player" or not current_room or current_room not in rooms:
                    continue
                killer_id = data.get("last_attacker_id")
                await eliminate_player(current_room, client_id, killer_id)

    except WebSocketDisconnect:
        if current_room and current_room in rooms:
            room = rooms[current_room]
            if role == "player" and client_id in room["players"]:
                await mark_player_quit(current_room, client_id)
            elif role == "admin" and client_id in room["admins"]:
                del room["admins"][client_id]
    finally:
        if current_room and current_room in rooms and role == "player" and client_id in rooms[current_room]["players"]:
            p = rooms[current_room]["players"][client_id]
            if p.get("alive") or p.get("connected", True):
                await mark_player_quit(current_room, client_id)


async def check_winner(room_code: str):
    room = rooms.get(room_code)
    if not room or not room["active"]:
        return

    survivors = [
        pid for pid, p in room["players"].items()
        if p["alive"] and not p.get("quitter", False) and (p.get("is_bot") or p.get("connected", True))
    ]

    if len(survivors) == 1:
        winner_id = survivors[0]
        winner = room["players"][winner_id]
        room["active"] = False
        room["last_winner"] = winner["name"]
        record_stat(winner["name"], win=1, ko=winner["ko"], score=winner["score"])
        podium = compute_podium(room, winner_id)

        await broadcast_to_room(room_code, {
            "type": "match_finished",
            "winner": winner["name"],
            "ko": winner["ko"],
            "score": winner["score"],
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True),
        })
    elif len(survivors) == 0:
        room["active"] = False
        non_quitters = [
            (pid, p) for pid, p in room["players"].items()
            if p.get("is_bot") or not p.get("quitter", False)
        ]
        pool = non_quitters if non_quitters else list(room["players"].items())
        ranked = sorted(
            dedup_entries([
                {
                    "pid": pid,
                    "name": p["name"],
                    "score": p["score"],
                    "ko": p["ko"],
                }
                for pid, p in pool
            ]),
            key=lambda item: (item["score"], item["ko"]),
            reverse=True,
        )
        best_pid = ranked[0]["pid"] if ranked else None
        podium = compute_podium(room, None)
        winner_name = ranked[0]["name"] if ranked else "Tidak Ada"
        room["last_winner"] = winner_name if ranked else None

        if best_pid and best_pid in room["players"]:
            w = room["players"][best_pid]
            record_stat(w["name"], win=1, ko=w["ko"], score=w["score"])

        await broadcast_to_room(room_code, {
            "type": "match_finished",
            "winner": winner_name,
            "ko": ranked[0]["ko"] if ranked else 0,
            "score": ranked[0]["score"] if ranked else 0,
            "podium": podium,
            "players": get_room_players_summary(room_code, include_grid=True),
        })
