#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAMSTER FAUCET BOT TURBO v8.0 - Web Interface
Backend Flask com WebSocket (Flask-SocketIO) para execução em tempo real.
"""

import eventlet
eventlet.monkey_patch()

import os
import json
import time
import random
import threading
import base64
import uuid
import requests as http_requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'hamster-turbo-v8-secret')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ═══════════════════════════════════════════════════════════════
#                      CONFIGURAÇÕES
# ═══════════════════════════════════════════════════════════════

BASE_URL = "https://expressapi-2rffgzjdzq-uc.a.run.app"
APP_NAME = "Hamster"
APP_VERSION = 10810.0
COOLDOWN_MINUTES = 10

FIREBASE_API_KEY = "AIzaSyDLwdoID0m70AY0Y2elYLoF_h49LAJwYe4"
FIREBASE_TOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

HEADERS = {
    "user-agent": "Dart/3.10 (dart:io)",
    "content-type": "application/json",
    "accept-encoding": "gzip",
}

DELAY_ENTRE_TAREFAS = (1, 3)
DELAY_APOS_AD = (1, 2)
RETRY_BLOCK_DELAY = (5, 15)
MAX_RETRIES_BLOCK = 3
MIN_TASKS_TO_START_CYCLE = 3
TOKEN_REFRESH_MARGIN = 300

MAHJONG_GAMES = {
    f"Game {i}": {
        "eventName": f"Game {i}",
        "eventType": "mahjong",
        "timerTitle": f"timer{i}",
        "timer": COOLDOWN_MINUTES,
    }
    for i in range(1, 7)
}

SPINNER_GAMES = {
    f"Spinner {i}": {
        "eventName": f"Spinner {i}",
        "eventType": "spinner",
        "timerTitle": f"timer{i + 6}",
        "timer": COOLDOWN_MINUTES,
    }
    for i in range(1, 7)
}

NORMAL_GAMES = {
    f"Normal Game {i}": {"eventName": f"Normal Game {i}"}
    for i in range(1, 3)
}

ALL_TASK_NAMES = list(MAHJONG_GAMES.keys()) + list(NORMAL_GAMES.keys()) + list(SPINNER_GAMES.keys())

# Store active bot sessions
active_sessions = {}


# ═══════════════════════════════════════════════════════════════
#                    UTILIDADES JWT
# ═══════════════════════════════════════════════════════════════

def decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return {}


def get_token_info(token):
    return decode_jwt_payload(token)


def get_token_expiry(token):
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if exp:
        return datetime.fromtimestamp(exp)
    return None


def is_refresh_token(token):
    return token.startswith("AMf-") or token.startswith("AG")


def refresh_id_token(refresh_token):
    try:
        resp = http_requests.post(FIREBASE_TOKEN_URL, json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, headers={"Content-Type": "application/json"}, timeout=15)
        data = resp.json()
        return data.get("id_token"), data.get("refresh_token", "")
    except Exception as e:
        return None, None


# ═══════════════════════════════════════════════════════════════
#                    BOT PRINCIPAL (Web Version)
# ═══════════════════════════════════════════════════════════════

class HamsterFaucetBot:
    def __init__(self, auth_token, refresh_token="", user_name="", country="Brazil", photo_url="", session_id=""):
        self.auth_token = auth_token
        self.refresh_token = refresh_token
        self.user_name = user_name or get_token_info(auth_token).get("name", "")
        self.country = country
        self.photo_url = photo_url or get_token_info(auth_token).get("picture", "")
        self.session_id = session_id

        self.http_session = http_requests.Session()
        self.http_session.headers.update(HEADERS)

        self.cooldowns = {}
        self.total_points = 0.0
        self.total_ads = 0
        self.total_cycles = 0
        self.tasks_completed = 0
        self.tasks_blocked = 0
        self.errors = 0
        self.token_refreshes = 0
        self.giveaway_joined = False
        self.start_time = datetime.now()
        self.running = False
        self.stop_requested = False
        self.stats = {
            "mahjong_ok": 0, "mahjong_fail": 0,
            "spinner_ok": 0, "spinner_fail": 0,
            "normal_ok": 0, "normal_fail": 0,
            "ads_ok": 0, "ads_fail": 0,
        }
        self._lock = threading.Lock()

    def _emit_log(self, tag, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        socketio.emit('log', {
            'tag': tag,
            'message': msg,
            'timestamp': ts,
            'session_id': self.session_id
        }, room=self.session_id)

    def _emit_stats(self):
        socketio.emit('stats', {
            'total_points': round(self.total_points, 2),
            'total_ads': self.total_ads,
            'total_cycles': self.total_cycles,
            'tasks_completed': self.tasks_completed,
            'tasks_blocked': self.tasks_blocked,
            'errors': self.errors,
            'token_refreshes': self.token_refreshes,
            'giveaway_joined': self.giveaway_joined,
            'stats': self.stats,
            'session_id': self.session_id
        }, room=self.session_id)

    def _post(self, endpoint, payload):
        payload["authToken"] = self.auth_token
        try:
            resp = self.http_session.post(f"{BASE_URL}{endpoint}", json=payload, timeout=15)
            return resp.status_code, resp.json()
        except http_requests.exceptions.Timeout:
            return 0, {"message": "Timeout"}
        except Exception as e:
            return 0, {"message": str(e)}

    def _is_on_cooldown(self, name):
        with self._lock:
            if name in self.cooldowns:
                remaining = (self.cooldowns[name] - datetime.now()).total_seconds()
                if remaining > 0:
                    m, s = divmod(int(remaining), 60)
                    self._emit_log("WAIT", f"{name}: Cooldown {m}m {s}s restantes")
                    return True
                else:
                    del self.cooldowns[name]
        return False

    def _set_cooldown(self, name, minutes=COOLDOWN_MINUTES):
        with self._lock:
            self.cooldowns[name] = datetime.now() + timedelta(minutes=minutes)
        self._emit_log("WAIT", f"{name}: Cooldown {minutes}min")

    def _count_available(self):
        now = datetime.now()
        count = 0
        for name in ALL_TASK_NAMES:
            with self._lock:
                if name in self.cooldowns:
                    if (self.cooldowns[name] - now).total_seconds() > 0:
                        continue
            count += 1
        return count

    def _auto_refresh_token(self):
        if not self.refresh_token:
            expiry = get_token_expiry(self.auth_token)
            if expiry is None:
                return True
            now = datetime.now()
            remaining = (expiry - now).total_seconds()
            if remaining < TOKEN_REFRESH_MARGIN:
                m, s = divmod(int(remaining), 60)
                self._emit_log("TOKEN", f"TOKEN EXPIRA EM {m}m {s}s!")
            return True

        expiry = get_token_expiry(self.auth_token)
        if expiry is None:
            return self._do_refresh()

        now = datetime.now()
        remaining = (expiry - now).total_seconds()

        if remaining <= TOKEN_REFRESH_MARGIN:
            self._emit_log("TOKEN", "Renovando token...")
            return self._do_refresh()

        return True

    def _do_refresh(self):
        new_id_token, new_refresh_token = refresh_id_token(self.refresh_token)
        if new_id_token:
            self.auth_token = new_id_token
            if new_refresh_token:
                self.refresh_token = new_refresh_token
            self.token_refreshes += 1
            self._emit_log("TOKEN", f"TOKEN RENOVADO! (#{self.token_refreshes})")
            return True
        else:
            self._emit_log("ERR", "FALHA ao renovar token!")
            return False

    def _quick_add_ad(self, context=""):
        code, data = self._post("/amar/addAd", {
            "app": APP_NAME,
            "version": APP_VERSION,
        })
        if code == 200 and data.get("status"):
            with self._lock:
                self.total_ads += 1
                self.stats["ads_ok"] += 1
            self._emit_log("AD", f"Ad registrado ({context})")
            time.sleep(random.uniform(*DELAY_APOS_AD))
            return True
        else:
            self._emit_log("WARN", f"addAd: HTTP {code} - {data.get('message', '')}")
            with self._lock:
                self.stats["ads_fail"] += 1
            return False

    def _ensure_block_list(self, context=""):
        code, data = self._post("/amar/checkIfUserAlreadyExist", {
            "app": APP_NAME,
            "version": APP_VERSION,
        })
        if code == 200:
            self._emit_log("FIX", f"Block_List OK ({context})")
        else:
            self._emit_log("WARN", f"checkIfUserAlreadyExist falhou: {code} ({context})")
        time.sleep(1)
        return code == 200

    def add_ad(self):
        self._emit_log("AD", "Registrando anuncio...")
        code, data = self._post("/amar/addAd", {
            "app": APP_NAME,
            "version": APP_VERSION,
        })
        if code == 200 and data.get("status"):
            self._emit_log("OK", f"addAd: {data.get('message', 'OK')}")
            with self._lock:
                self.total_ads += 1
                self.stats["ads_ok"] += 1
            self._emit_stats()
            return True
        else:
            self._emit_log("ERR", f"addAd: HTTP {code} - {data.get('message', '')}")
            with self._lock:
                self.stats["ads_fail"] += 1
            self._emit_stats()
            return False

    def claim_mahjong(self, game_num, retry=0):
        name = f"Game {game_num}"
        if name not in MAHJONG_GAMES:
            self._emit_log("ERR", f"{name} invalido (1-6)")
            return False
        if self._is_on_cooldown(name):
            return False

        cfg = MAHJONG_GAMES[name]
        if retry == 0:
            self._quick_add_ad(context=name)

        self._emit_log("GAME", f"Resgatando {name}..." + (f" (retry {retry})" if retry > 0 else ""))

        code, data = self._post("/amar/addPointsR", {
            "app": APP_NAME,
            "eventName": cfg["eventName"],
            "eventType": cfg["eventType"],
            "timerTitle": cfg["timerTitle"],
            "timer": cfg["timer"],
            "version": APP_VERSION,
            "points": 0,
        })

        msg = data.get("message", "")
        if code == 200 and data.get("status"):
            self._emit_log("OK", f"{name}: {msg}")
            self._set_cooldown(name)
            with self._lock:
                self.tasks_completed += 1
                self.stats["mahjong_ok"] += 1
            self._emit_stats()
            return True
        elif code == 429:
            self._emit_log("WARN", f"{name}: Cooldown no servidor")
            self._set_cooldown(name)
            with self._lock:
                self.stats["mahjong_fail"] += 1
            self._emit_stats()
            return False
        elif "Block_List" in msg:
            if "NOT_FOUND" in msg:
                self._ensure_block_list(context=f"{name} fix")
            with self._lock:
                self.tasks_blocked += 1
            if retry < MAX_RETRIES_BLOCK:
                delay = random.randint(*RETRY_BLOCK_DELAY)
                self._emit_log("RETRY", f"{name}: Block_List. Retry em {delay}s... ({retry + 1}/{MAX_RETRIES_BLOCK})")
                time.sleep(delay)
                self._quick_add_ad(context=f"{name} retry")
                return self.claim_mahjong(game_num, retry=retry + 1)
            else:
                self._emit_log("WARN", f"{name}: Block_List persistente. Cooldown 15min.")
                self._set_cooldown(name, minutes=COOLDOWN_MINUTES + 5)
                with self._lock:
                    self.stats["mahjong_fail"] += 1
                self._emit_stats()
                return False
        else:
            self._emit_log("ERR", f"{name}: HTTP {code} - {msg}")
            with self._lock:
                self.errors += 1
                self.stats["mahjong_fail"] += 1
            self._emit_stats()
            return False

    def claim_spinner(self, spinner_num, retry=0):
        name = f"Spinner {spinner_num}"
        if name not in SPINNER_GAMES:
            self._emit_log("ERR", f"{name} invalido (1-6)")
            return False
        if self._is_on_cooldown(name):
            return False

        cfg = SPINNER_GAMES[name]
        points = round(random.uniform(5000, 20000), 14)

        self._ensure_block_list(context=name)

        if retry == 0:
            self._quick_add_ad(context=name)

        self._emit_log("SPIN", f"{name}: addPointsI ({points:.2f} pts)..." + (f" (retry {retry})" if retry > 0 else ""))

        code, data = self._post("/amar/addPointsI", {
            "app": APP_NAME,
            "eventName": cfg["eventName"],
            "eventType": cfg["eventType"],
            "timerTitle": cfg["timerTitle"],
            "timer": cfg["timer"],
            "version": APP_VERSION,
            "points": points,
        })

        msg = data.get("message", "")

        if code == 200 and data.get("status") and "Added" in msg:
            self._emit_log("PTS", f"{name}: +{points:.2f} pts PERMANENTES!")
            self._set_cooldown(name)
            with self._lock:
                self.total_points += points
                self.tasks_completed += 1
                self.stats["spinner_ok"] += 1
            self._emit_stats()
            return True

        if code == 429:
            self._emit_log("WARN", f"{name}: Cooldown no servidor")
            self._set_cooldown(name)
            with self._lock:
                self.stats["spinner_fail"] += 1
            self._emit_stats()
            return False

        if "NOT_FOUND" in msg and "Block_List" in msg:
            if retry < MAX_RETRIES_BLOCK:
                self._emit_log("RETRY", f"{name}: Block_List NOT_FOUND. Recriando... ({retry + 1}/{MAX_RETRIES_BLOCK})")
                self._ensure_block_list(context=f"{name} retry")
                time.sleep(random.randint(2, 5))
                self._quick_add_ad(context=f"{name} retry")
                return self.claim_spinner(spinner_num, retry=retry + 1)
            else:
                self._emit_log("ERR", f"{name}: Block_List NOT_FOUND persistente apos {MAX_RETRIES_BLOCK} tentativas")
                self._set_cooldown(name, minutes=COOLDOWN_MINUTES + 5)
                with self._lock:
                    self.tasks_blocked += 1
                    self.stats["spinner_fail"] += 1
                self._emit_stats()
                return False

        if "Block_List" in msg:
            with self._lock:
                self.tasks_blocked += 1
            if retry < MAX_RETRIES_BLOCK:
                delay = random.randint(*RETRY_BLOCK_DELAY)
                self._emit_log("RETRY", f"{name}: Block_List. Retry em {delay}s... ({retry + 1}/{MAX_RETRIES_BLOCK})")
                time.sleep(delay)
                self._ensure_block_list(context=f"{name} retry")
                self._quick_add_ad(context=f"{name} retry")
                return self.claim_spinner(spinner_num, retry=retry + 1)
            else:
                self._emit_log("WARN", f"{name}: Block_List persistente. Cooldown 15min.")
                self._set_cooldown(name, minutes=COOLDOWN_MINUTES + 5)
                with self._lock:
                    self.stats["spinner_fail"] += 1
                self._emit_stats()
                return False

        self._emit_log("ERR", f"{name}: HTTP {code} - {msg}")
        with self._lock:
            self.errors += 1
            self.stats["spinner_fail"] += 1
        self._emit_stats()
        return False

    def claim_normal_game(self, game_num, retry=0):
        name = f"Normal Game {game_num}"
        if name not in NORMAL_GAMES:
            self._emit_log("ERR", f"{name} invalido (1-2)")
            return False
        if self._is_on_cooldown(name):
            return False

        if retry == 0:
            self._quick_add_ad(context=name)

        self._emit_log("GAME", f"Resgatando {name}..." + (f" (retry {retry})" if retry > 0 else ""))
        code, data = self._post("/amar/addNormalGamePointsWithInterval", {
            "app": APP_NAME,
            "version": APP_VERSION,
            "eventName": name,
        })

        msg = data.get("message", "")
        if code == 200 and data.get("status"):
            self._emit_log("OK", f"{name}: {msg}")
            self._set_cooldown(name)
            with self._lock:
                self.tasks_completed += 1
                self.stats["normal_ok"] += 1
            self._emit_stats()
            return True
        elif code == 200 and "Daily limit" in msg:
            self._emit_log("WARN", f"{name}: Limite diario atingido (30 niveis)")
            self._set_cooldown(name, minutes=60 * 12)
            with self._lock:
                self.stats["normal_fail"] += 1
            self._emit_stats()
            return False
        elif code == 429:
            self._emit_log("WARN", f"{name}: Cooldown no servidor")
            self._set_cooldown(name)
            with self._lock:
                self.stats["normal_fail"] += 1
            self._emit_stats()
            return False
        elif "Block_List" in msg:
            if "NOT_FOUND" in msg:
                self._ensure_block_list(context=f"{name} fix")
            with self._lock:
                self.tasks_blocked += 1
            if retry < MAX_RETRIES_BLOCK:
                delay = random.randint(*RETRY_BLOCK_DELAY)
                self._emit_log("RETRY", f"{name}: Block_List. Retry em {delay}s... ({retry + 1}/{MAX_RETRIES_BLOCK})")
                time.sleep(delay)
                self._quick_add_ad(context=f"{name} retry")
                return self.claim_normal_game(game_num, retry=retry + 1)
            else:
                self._emit_log("WARN", f"{name}: Block_List persistente. Cooldown 15min.")
                self._set_cooldown(name, minutes=COOLDOWN_MINUTES + 5)
                with self._lock:
                    self.stats["normal_fail"] += 1
                self._emit_stats()
                return False
        else:
            self._emit_log("ERR", f"{name}: HTTP {code} - {msg}")
            with self._lock:
                self.errors += 1
                self.stats["normal_fail"] += 1
            self._emit_stats()
            return False

    def join_giveaway(self):
        if self.giveaway_joined:
            self._emit_log("INFO", "Ja inscrito no Giveaway!")
            return True
        self._emit_log("GIVE", "Entrando no Grand Giveaway...")
        code, data = self._post("/amar/joinAirdrop", {
            "app": APP_NAME,
            "eventName": "Giveaway",
            "country": self.country,
            "name": self.user_name,
            "photoUrl": self.photo_url,
            "version": APP_VERSION,
        })
        if code == 200 and data.get("status"):
            self._emit_log("OK", f"Giveaway: {data.get('message', 'Inscrito!')}")
            self.giveaway_joined = True
            self._emit_stats()
            return True
        else:
            self._emit_log("ERR", f"Giveaway: HTTP {code} - {data.get('message', '')}")
            self._emit_stats()
            return False

    def _get_pending_tasks(self):
        tasks = []
        now = datetime.now()
        for i in range(1, 7):
            name = f"Game {i}"
            with self._lock:
                if name in self.cooldowns:
                    if (self.cooldowns[name] - now).total_seconds() > 0:
                        continue
                    else:
                        del self.cooldowns[name]
            tasks.append({"name": name, "type": "mahjong", "num": i})

        for i in range(1, 3):
            name = f"Normal Game {i}"
            with self._lock:
                if name in self.cooldowns:
                    if (self.cooldowns[name] - now).total_seconds() > 0:
                        continue
                    else:
                        del self.cooldowns[name]
            tasks.append({"name": name, "type": "normal", "num": i})

        for i in range(1, 7):
            name = f"Spinner {i}"
            with self._lock:
                if name in self.cooldowns:
                    if (self.cooldowns[name] - now).total_seconds() > 0:
                        continue
                    else:
                        del self.cooldowns[name]
            tasks.append({"name": name, "type": "spinner", "num": i})

        return tasks

    def run_cycle(self):
        self.total_cycles += 1
        if not self._auto_refresh_token():
            return -1

        tasks = self._get_pending_tasks()
        total_pending = len(tasks)

        mahjong_count = sum(1 for t in tasks if t["type"] == "mahjong")
        normal_count = sum(1 for t in tasks if t["type"] == "normal")
        spinner_count = sum(1 for t in tasks if t["type"] == "spinner")

        self._emit_log("TURBO", f"{'=' * 50}")
        self._emit_log("TURBO", f"MODO TURBO v8.0 - {total_pending}/14 tarefas pendentes")
        self._emit_log("TURBO", f"Mahjong: {mahjong_count} | Normal: {normal_count} | Spinner: {spinner_count}")
        self._emit_log("TURBO", f"{'=' * 50}")

        if total_pending == 0:
            self._emit_log("WARN", "Nenhuma tarefa disponivel!")
            return 0

        cooldown_count = 14 - total_pending
        if cooldown_count > 0:
            self._emit_log("WARN", f"{cooldown_count} tarefas em cooldown (pulando)")

        total_executed = 0
        random.shuffle(tasks)
        tasks.sort(key=lambda t: {"mahjong": 0, "normal": 1, "spinner": 2}[t["type"]])

        for idx, task in enumerate(tasks):
            if self.stop_requested:
                self._emit_log("WARN", "Parada solicitada!")
                break

            if idx > 0:
                delay = random.uniform(*DELAY_ENTRE_TAREFAS)
                time.sleep(delay)

            try:
                result = False
                if task["type"] == "mahjong":
                    result = self.claim_mahjong(task["num"])
                elif task["type"] == "normal":
                    result = self.claim_normal_game(task["num"])
                elif task["type"] == "spinner":
                    result = self.claim_spinner(task["num"])

                if result:
                    total_executed += 1
            except Exception as e:
                self._emit_log("ERR", f"{task['name']}: Erro - {e}")
                with self._lock:
                    self.errors += 1

        if not self.giveaway_joined:
            self.join_giveaway()

        self._emit_stats()
        return total_executed

    def run_auto(self, cycles=0):
        self.running = True
        self.stop_requested = False
        cycle_count = 0

        info = get_token_info(self.auth_token)
        expiry = get_token_expiry(self.auth_token)
        expiry_str = expiry.strftime("%H:%M:%S") if expiry else "?"
        has_refresh = "SIM" if self.refresh_token else "NAO"

        self._emit_log("TURBO", "=" * 55)
        self._emit_log("TURBO", "HAMSTER FAUCET - MODO TURBO v8.0")
        self._emit_log("TURBO", "=" * 55)
        self._emit_log("INFO", "Fluxo Spinner: checkUser -> addAd -> addPointsI (PERMANENTE)")
        self._emit_log("INFO", "Fluxo Mahjong: addAd -> addPointsR (points=0)")
        self._emit_log("INFO", "Fluxo Normal:  addAd -> addNormalGamePointsWithInterval")
        self._emit_log("INFO", f"Delays: 1-3s entre tarefas | ~2-3 min/ciclo")
        self._emit_log("TURBO", "=" * 55)
        self._emit_log("INFO", f"Usuario: {self.user_name or 'N/A'} ({info.get('email', 'N/A')})")
        self._emit_log("INFO", f"Token expira: {expiry_str} | Auto-refresh: {has_refresh}")
        self._emit_log("INFO", f"Ciclos: {'Infinito' if cycles == 0 else cycles}")

        try:
            while (cycles == 0 or cycle_count < cycles) and not self.stop_requested:
                cycle_count += 1
                self._emit_log("TURBO", f"CICLO #{cycle_count} - {datetime.now().strftime('%H:%M:%S')}")

                executed = self.run_cycle()

                if executed == -1:
                    self._emit_log("TOKEN", "Token expirado!")
                    break

                self._emit_log("PTS", f"Ciclo #{cycle_count}: {executed} tarefas | Pontos: {self.total_points:.2f}")
                self._emit_log("INFO", f"OK: {self.tasks_completed} | Block: {self.tasks_blocked} | Ads: {self.total_ads} | Erros: {self.errors}")

                if cycles > 0 and cycle_count >= cycles:
                    break

                if not self.stop_requested:
                    self._wait_next_smart()

        except Exception as e:
            self._emit_log("ERR", f"Erro no loop: {e}")

        self._emit_summary()
        self.running = False

    def _wait_next_smart(self):
        with self._lock:
            if not self.cooldowns:
                return

        available = self._count_available()
        if available >= MIN_TASKS_TO_START_CYCLE:
            return

        now = datetime.now()
        with self._lock:
            sorted_cds = sorted(
                [(name, cd) for name, cd in self.cooldowns.items() if cd > now],
                key=lambda x: x[1]
            )

        if not sorted_cds:
            return

        target_idx = min(MIN_TASKS_TO_START_CYCLE - available, len(sorted_cds)) - 1
        if target_idx < 0:
            return

        target_task, target_time = sorted_cds[target_idx]
        wait = max(0, (target_time - now).total_seconds())
        wait += random.randint(5, 15)

        self._emit_log("WAIT", f"Esperando {int(wait // 60)}m {int(wait % 60)}s...")

        total = int(wait)
        for restante in range(total, 0, -1):
            if self.stop_requested:
                return
            time.sleep(1)

            if restante % 60 == 0 and self.refresh_token:
                self._auto_refresh_token()

            if self._count_available() >= MIN_TASKS_TO_START_CYCLE:
                return

    def get_status(self):
        status_data = {
            "mahjong": [],
            "normal": [],
            "spinner": [],
        }
        now = datetime.now()

        for i in range(1, 7):
            name = f"Game {i}"
            with self._lock:
                if name in self.cooldowns:
                    rest = (self.cooldowns[name] - now).total_seconds()
                    if rest > 0:
                        m, s = divmod(int(rest), 60)
                        status_data["mahjong"].append({"name": name, "status": "cooldown", "remaining": f"{m}m {s}s"})
                        continue
            status_data["mahjong"].append({"name": name, "status": "ok", "remaining": ""})

        for i in range(1, 3):
            name = f"Normal Game {i}"
            with self._lock:
                if name in self.cooldowns:
                    rest = (self.cooldowns[name] - now).total_seconds()
                    if rest > 0:
                        m, s = divmod(int(rest), 60)
                        status_data["normal"].append({"name": name, "status": "cooldown", "remaining": f"{m}m {s}s"})
                        continue
            status_data["normal"].append({"name": name, "status": "ok", "remaining": ""})

        for i in range(1, 7):
            name = f"Spinner {i}"
            with self._lock:
                if name in self.cooldowns:
                    rest = (self.cooldowns[name] - now).total_seconds()
                    if rest > 0:
                        m, s = divmod(int(rest), 60)
                        status_data["spinner"].append({"name": name, "status": "cooldown", "remaining": f"{m}m {s}s"})
                        continue
            status_data["spinner"].append({"name": name, "status": "ok", "remaining": ""})

        self._emit_log("INFO", "═" * 50)
        self._emit_log("INFO", "STATUS")
        self._emit_log("INFO", "═" * 50)
        for cat_name, items in [("MAHJONG (1-6)", status_data["mahjong"]), ("NORMAL (1-2)", status_data["normal"]), ("SPINNER (1-6)", status_data["spinner"])]:
            self._emit_log("INFO", f"  {cat_name}:")
            for item in items:
                if item["status"] == "ok":
                    self._emit_log("OK", f"    {item['name']}: Disponivel")
                else:
                    self._emit_log("WAIT", f"    {item['name']}: {item['remaining']}")

        self._emit_log("INFO", "─" * 50)
        self._emit_log("PTS", f"Pontos: {self.total_points:.2f}")
        self._emit_log("INFO", f"OK: {self.tasks_completed} | Block: {self.tasks_blocked} | Ads: {self.total_ads}")
        self._emit_stats()

    def _emit_summary(self):
        elapsed = str(datetime.now() - self.start_time).split('.')[0]
        self._emit_log("TURBO", "═" * 50)
        self._emit_log("TURBO", "RESUMO FINAL (TURBO v8.0)")
        self._emit_log("TURBO", "═" * 50)
        self._emit_log("PTS", f"Pontos: {self.total_points:.2f}")
        self._emit_log("INFO", f"Ciclos: {self.total_cycles}")
        self._emit_log("OK", f"Completou: {self.tasks_completed}")
        self._emit_log("WARN", f"Blocked: {self.tasks_blocked}")
        self._emit_log("AD", f"Ads: {self.total_ads}")
        self._emit_log("ERR", f"Erros: {self.errors}")
        self._emit_log("GIVE", f"Giveaway: {'Sim' if self.giveaway_joined else 'Nao'}")
        self._emit_log("TOKEN", f"Refreshes: {self.token_refreshes}")
        self._emit_log("INFO", f"Sessao: {elapsed}")
        self._emit_log("INFO", f"Mahjong: {self.stats['mahjong_ok']} OK / {self.stats['mahjong_fail']} Fail")
        self._emit_log("INFO", f"Normal: {self.stats['normal_ok']} OK / {self.stats['normal_fail']} Fail")
        self._emit_log("INFO", f"Spinner: {self.stats['spinner_ok']} OK / {self.stats['spinner_fail']} Fail")
        self._emit_log("TURBO", "═" * 50)
        self._emit_stats()

        socketio.emit('task_complete', {'session_id': self.session_id}, room=self.session_id)


# ═══════════════════════════════════════════════════════════════
#                    ROTAS FLASK
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def handle_connect():
    session_id = request.sid
    emit('connected', {'session_id': session_id})


@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        bot = active_sessions[session_id]
        bot.stop_requested = True
        del active_sessions[session_id]


@socketio.on('start_bot')
def handle_start_bot(data):
    session_id = request.sid
    token = data.get('token', '').strip()
    action = data.get('action', '')
    param = data.get('param', None)

    if not token:
        emit('log', {'tag': 'ERR', 'message': 'Token vazio!', 'timestamp': datetime.now().strftime("%H:%M:%S"), 'session_id': session_id})
        return

    # Resolve token
    if is_refresh_token(token):
        emit('log', {'tag': 'TOKEN', 'message': 'Refresh token detectado -> obtendo ID token...', 'timestamp': datetime.now().strftime("%H:%M:%S"), 'session_id': session_id})
        id_token, new_refresh = refresh_id_token(token)
        if id_token:
            emit('log', {'tag': 'OK', 'message': 'Token obtido com sucesso!', 'timestamp': datetime.now().strftime("%H:%M:%S"), 'session_id': session_id})
            auth_token = id_token
            refresh_tok = new_refresh or token
        else:
            emit('log', {'tag': 'ERR', 'message': 'Falha ao obter ID token do refresh token!', 'timestamp': datetime.now().strftime("%H:%M:%S"), 'session_id': session_id})
            return
    else:
        auth_token = token
        refresh_tok = ""

    # Stop existing bot if any
    if session_id in active_sessions:
        active_sessions[session_id].stop_requested = True
        time.sleep(1)

    bot = HamsterFaucetBot(
        auth_token=auth_token,
        refresh_token=refresh_tok,
        session_id=session_id,
    )
    active_sessions[session_id] = bot

    def run_action():
        try:
            if action == "auto":
                bot.run_auto(cycles=0)
            elif action == "1cycle":
                executed = bot.run_cycle()
                if executed >= 0:
                    bot._emit_log("PTS", f"Ciclo: {executed} tarefas")
                bot._emit_summary()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "ncycles":
                n = int(param) if param else 1
                bot.run_auto(cycles=n)
            elif action == "mahjong":
                g = int(param) if param else 1
                bot.claim_mahjong(g)
                bot._emit_stats()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "spinner":
                s = int(param) if param else 1
                bot.claim_spinner(s)
                bot._emit_stats()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "normal":
                g = int(param) if param else 1
                bot.claim_normal_game(g)
                bot._emit_stats()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "addad":
                bot.add_ad()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "giveaway":
                bot.join_giveaway()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
            elif action == "status":
                bot.get_status()
                socketio.emit('task_complete', {'session_id': session_id}, room=session_id)
        except Exception as e:
            bot._emit_log("ERR", f"Erro: {e}")
            socketio.emit('task_complete', {'session_id': session_id}, room=session_id)

    thread = threading.Thread(target=run_action, daemon=True)
    thread.start()


@socketio.on('stop_bot')
def handle_stop_bot():
    session_id = request.sid
    if session_id in active_sessions:
        active_sessions[session_id].stop_requested = True
        emit('log', {'tag': 'WARN', 'message': 'Parando bot...', 'timestamp': datetime.now().strftime("%H:%M:%S"), 'session_id': session_id})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
