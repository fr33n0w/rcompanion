#!/usr/bin/env python3
"""
rCompanion Server v0.1
REST API Flask per Windows - backend per ESP32-C6 rCompanion display
Gira sul PC Windows dove è in esecuzione RNS/LXMF

Endpoints:
  GET /api/status       - stato generale RNS
  GET /api/interfaces   - lista interfacce con stato
  GET /api/announces    - annunci recenti
  GET /api/lxmf         - messaggi LXMF in arrivo
  GET /api/identity     - identità nodo locale
  GET /api/log          - ultimi N righe di log RNS
  POST /api/restart     - restart interfaccia o rnsd
  GET /api/all          - tutti i dati in un colpo solo (per polling C6)

Avvio:
  pip install flask RNS LXMF
  python rcompanion_server.py
"""

import os
import sys
import time
import json
import threading
import traceback
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, jsonify, request, Response

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

PORT = 5000
HOST = "0.0.0.0"        # ascolta su tutte le interfacce LAN
DEBUG = False

# Quanti annunci/messaggi tenere in memoria
MAX_ANNOUNCES  = 200
MAX_MESSAGES   = 20
MAX_LOG_LINES  = 200

VERSION = "0.5"
GITHUB_REPO = "fr33n0w/rcompanion"  # per controllo aggiornamenti

# Polling interno stato interfacce (secondi)
STATUS_INTERVAL = 5

# URL endpoint dati nodi rmap.world (modifica col path corretto)
RMAP_URL = "https://rmap.world/?json=1"
RMAP_INTERVAL = 300  # fetch ogni 5 minuti
WEATHER_INTERVAL = 600  # meteo ogni 10 minuti

# ---------------------------------------------------------------------------
# Stato globale
# ---------------------------------------------------------------------------

state = {
    "rns_online":    False,
    "lxmf_online":   False,
    "uptime_start":  time.time(),
    "interfaces":    [],
    "announces":     deque(maxlen=MAX_ANNOUNCES),
    "messages":      deque(maxlen=MAX_MESSAGES),
    "unread_count":  0,
    "traffic_rx":    0,
    "traffic_tx":    0,
    "known_dests":   0,
    "paths":         0,
    "identity_hash": "",
    "identity_name": "",
    "log_lines":     deque(maxlen=MAX_LOG_LINES),
    "last_update":   0,
    "error":         None,
    "known_names":   {},  # hash -> display_name accumulato
    "total_announces": 0,  # contatore totale da avvio
    "traffic_history": [],  # ultimi 60 campioni (rx, tx, ts)
    "rnstatus_cache":  "",   # output rnstatus parsato
    "rnstatus_ifaces": [],   # interfacce da rnstatus
    "rnstatus_ts":     0,    # ultimo aggiornamento rnstatus
    "rmap_counts":     {},   # conteggio nodi per tipo da rmap.world
    "rmap_total":      0,    # totale nodi rmap (interfacce)
    "rmap_unique":     0,    # nodi unici per identity
    "rmap_views":      0,    # visite al sito rmap.world
    "rmap_ts":         0,    # ultimo fetch rmap
    "echo_log":        deque(maxlen=30),  # risposte echo inviate
    "weather":         {},   # dati meteo correnti
    "weather_city":    "Roma",  # citta meteo selezionata
    "weather_ts":      0,    # ultimo fetch meteo
    "host":            {},   # risorse host (cpu/ram/disco)
    "host_ts":         0,    # ultimo aggiornamento host
    "bot_echo":        True,  # echo automatico attivo
    "bot_commands":    True,  # risposte a comandi (help/meteo/status)
    "bot_custom":      {},   # parola/frase -> risposta custom
    "version_latest":  None,  # ultima versione su GitHub
    "version_update":  False, # aggiornamento disponibile
    "ann_lxmf":        0,    # contatore annunci LXMF
    "ann_nomad":       0,    # contatore annunci NomadNet
    "ann_times":       deque(maxlen=500),  # timestamp annunci per grafico orario
}

state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Persistenza impostazioni (sopravvivono ai riavvii)
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rcompanion_state.json")
# Chiavi dello state da rendere persistenti
PERSIST_KEYS = ["weather_city", "bot_echo", "bot_commands", "bot_custom"]

def save_settings():
    """Salva le impostazioni persistenti su file JSON."""
    try:
        with state_lock:
            data = {k: state[k] for k in PERSIST_KEYS if k in state}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"save_settings error: {e}", error=True)

def load_settings():
    """Carica le impostazioni persistenti dal file JSON, se esiste."""
    try:
        if not os.path.exists(SETTINGS_FILE):
            return
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            import json as _json
            data = _json.load(f)
        with state_lock:
            for k in PERSIST_KEYS:
                if k in data:
                    state[k] = data[k]
        log(f"Impostazioni caricate da {SETTINGS_FILE}")
    except Exception as e:
        log(f"load_settings error: {e}", error=True)

# ---------------------------------------------------------------------------
# RNS integration
# ---------------------------------------------------------------------------

rns_instance   = None
lxmf_router    = None
lxmf_dest      = None

def init_rns():
    """
    Si aggancia all'istanza rnsd esistente via shared instance.
    rnsd deve essere già in esecuzione prima di avviare questo server.
    """
    global rns_instance, lxmf_router, lxmf_dest

    try:
        import RNS
        import LXMF

        log("rCompanion: aggancio a rnsd (shared instance)...")

        # require_shared_instance=True: si aggancia a rnsd esistente,
        # non avvia nulla di nuovo, non registra signal handler
        rns_instance = RNS.Reticulum(require_shared_instance=True)

        with state_lock:
            state["rns_online"] = True
            state["error"]      = None

        # Identità: prova più metodi in ordine
        try:
            import pathlib

            # Metodo 1: leggi da RNS.Reticulum.storagepath
            storage = pathlib.Path(RNS.Reticulum.storagepath)
            identity_found = False
            for ident_path in [
                storage / "transport_identity",
                storage / "identity",
                storage.parent / "transport_identity",
                storage.parent / "identity",
            ]:
                if ident_path.exists():
                    try:
                        identity = RNS.Identity.from_file(str(ident_path))
                        if identity:
                            h = identity.hash.hex()
                            with state_lock:
                                state["identity_hash"] = h
                            log(f"Identità da file: {h[:16]}...")
                            identity_found = True
                            break
                    except Exception:
                        pass

            if not identity_found:
                # Metodo 2: crea istanza temporanea per leggere identità locale
                try:
                    local_id = RNS.Identity()
                    # Usa storagepath per trovare identity
                    sp = str(storage)
                    log(f"Storage path: {sp}")
                    log("Hash disponibile dopo primo announce del nodo")
                except Exception as e2:
                    log(f"Identity metodo 2 failed: {e2}")

        except Exception as e:
            log(f"Identità non letta: {e}")

        log("RNS shared instance OK")

        # Handler annunci
        RNS.Transport.register_announce_handler(_AnnounceHandler())
        RNS.Transport.register_announce_handler(_LXMFAnnounceHandler())
        RNS.Transport.register_announce_handler(_NomadAnnounceHandler())

        # LXMF router come client leggero
        try:
            lxmf_router = LXMF.LXMRouter(
                storagepath=_lxmf_storage(),
                autopeer=False
            )
            delivery_identity = RNS.Identity.from_file(
                _lxmf_storage() + "/identity"
            ) or RNS.Identity()
            lxmf_dest = lxmf_router.register_delivery_identity(
                delivery_identity,
                display_name="rCompanion"
            )
            lxmf_router.register_delivery_callback(_lxmf_delivery)
            with state_lock:
                state["lxmf_online"] = True
                state["identity_name"] = "rCompanion"
            # Salva identità LXMF per riusi
            delivery_identity.to_file(_lxmf_storage() + "/identity")
            log("LXMF router OK")
        except Exception as e:
            log(f"LXMF init warning: {e}", error=True)

        # Leggi nome nodo da config RNS
        try:
            import configparser
            rns_config_path = os.path.join(
                os.path.expanduser("~"), ".reticulum", "config"
            )
            cp = configparser.ConfigParser()
            cp.read(rns_config_path)
            # Cerca display_name in qualsiasi sezione
            for section in cp.sections():
                dn = cp.get(section, "display_name", fallback=None)
                if dn:
                    with state_lock:
                        state["identity_name"] = dn
                    log(f"Nome nodo: {dn}")
                    break
        except Exception as e:
            log(f"Config RNS non letta: {e}")

        # Loop stats
        threading.Thread(target=_stats_loop, daemon=True).start()
        # Echo bot
        threading.Thread(target=_echo_bot_loop, daemon=True).start()
        # rnstatus
        threading.Thread(target=_rnstatus_loop, daemon=True).start()
        # Auto-announce
        threading.Thread(target=_auto_announce_loop, daemon=True).start()
        # rmap fetch
        threading.Thread(target=_rmap_loop, daemon=True).start()
        # Meteo
        threading.Thread(target=_weather_loop, daemon=True).start()
        # Host resources
        threading.Thread(target=_host_loop, daemon=True).start()
        # Version check
        threading.Thread(target=_version_loop, daemon=True).start()

    except ImportError as e:
        msg = f"Impossibile importare RNS/LXMF: {e}"
        log(msg, error=True)
        with state_lock:
            state["error"] = msg
    except Exception as e:
        msg = f"Errore aggancio RNS: {e}\n{traceback.format_exc()}"
        log(msg, error=True)
        with state_lock:
            state["error"] = msg


def _lxmf_storage():
    """Percorso storage LXMF in AppData o cartella locale."""
    appdata = os.environ.get("APPDATA", ".")
    path = os.path.join(appdata, "rCompanion", "lxmf")
    os.makedirs(path, exist_ok=True)
    return path


class _AnnounceHandler:
    """Handler RNS per intercettare tutti gli annunci in transito."""
    aspect_filter = None  # None = tutti gli aspect

    def received_announce(self, destination_hash, announced_identity,
                          app_data):
        try:
            import RNS
            aspect = "unknown"
            if announced_identity:
                # Prova a ricavare l'aspect dal nome destinazione
                pass

            app_data_str = ""
            if app_data:
                try:
                    app_data_str = app_data.decode("utf-8", errors="replace")
                except Exception:
                    app_data_str = app_data.hex()

            # Decodifica nome peer: prima prova il metodo LXMF canonico
            display_name = ""
            try:
                import LXMF
                if app_data:
                    dn = LXMF.display_name_from_app_data(app_data)
                    if isinstance(dn, str):
                        display_name = dn
                    elif isinstance(dn, (bytes, bytearray)):
                        display_name = dn.decode("utf-8", errors="replace")
            except Exception:
                pass
            # Fallback: estrai parte ASCII leggibile da app_data
            if not display_name and app_data:
                try:
                    raw = app_data.decode("utf-8", errors="ignore")
                    import re
                    parts = re.findall(r'[ -~]{3,}', raw)
                    display_name = max(parts, key=len) if parts else ""
                except Exception:
                    pass
                if not display_name:
                    display_name = "".join(
                        chr(b) for b in app_data if 32 <= b < 127
                    )

            def _ascii(s):
                return "".join(c for c in str(s) if ord(c) < 128).strip()
            entry = {
                "hash":         destination_hash.hex(),
                "app_data":     _ascii(app_data_str),
                "display_name": _ascii(display_name),
                "ts":           time.time(),
                "ts_human":     datetime.now().strftime("%H:%M:%S"),
            }
            with state_lock:
                state["announces"].appendleft(entry)
                state["total_announces"] += 1
                state["ann_times"].append(time.time())
                # Accumula nome nel dizionario persistente
                dn_clean = "".join(c for c in str(display_name) if ord(c) < 128).strip()
                if dn_clean:
                    state["known_names"][entry["hash"]] = dn_clean

            log(f"Announce: {entry['hash'][:16]} [{display_name[:20]}]")
        except Exception as e:
            log(f"Errore announce handler: {e}", error=True)


class _LXMFAnnounceHandler:
    """Conta annunci sull'aspect lxmf.delivery."""
    aspect_filter = "lxmf.delivery"
    def received_announce(self, destination_hash, announced_identity, app_data, **kw):
        try:
            with state_lock:
                state["ann_lxmf"] += 1
        except Exception:
            pass


class _NomadAnnounceHandler:
    """Conta annunci sull'aspect nomadnetwork.node."""
    aspect_filter = "nomadnetwork.node"
    def received_announce(self, destination_hash, announced_identity, app_data, **kw):
        try:
            with state_lock:
                state["ann_nomad"] += 1
        except Exception:
            pass


def _lxmf_delivery(message):
    """Callback LXMF per messaggi in arrivo."""
    try:
        import RNS, LXMF
        src_hash_raw = message.source_hash
        src_hex = src_hash_raw.hex()

        # Risoluzione nome canonica LXMF: recall app_data dell'annuncio
        display_name = ""
        try:
            app_data = RNS.Identity.recall_app_data(src_hash_raw)
            if app_data:
                dn = LXMF.display_name_from_app_data(app_data)
                if isinstance(dn, str):
                    display_name = dn
                elif isinstance(dn, (bytes, bytearray)):
                    display_name = dn.decode("utf-8", errors="replace")
        except Exception:
            pass
        # Fallback: cerca nei nomi gia noti
        if not display_name:
            with state_lock:
                display_name = state["known_names"].get(src_hex, "")

        def _ascii(s):
            return "".join(c for c in str(s) if ord(c) < 128).strip()

        entry = {
            "hash":    "<" + src_hex + ">",
            "display_name": _ascii(display_name),
            "title":   _ascii(message.title.decode("utf-8", errors="replace")
                       if message.title else ""),
            "content": _ascii(message.content.decode("utf-8", errors="replace")
                       if message.content else ""),
            "ts":      time.time(),
            "ts_human": datetime.now().strftime("%H:%M:%S"),
            "read":    False,
        }
        with state_lock:
            state["messages"].appendleft(entry)
            state["unread_count"] += 1
            # Memorizza il nome risolto per uso futuro
            if entry["display_name"]:
                state["known_names"][src_hex] = entry["display_name"]
        log(f"LXMF da {entry['display_name'] or src_hex[:16]}: {entry['content'][:40]}")
    except Exception as e:
        log(f"Errore LXMF delivery: {e}", error=True)


def _stats_loop():
    """Aggiorna periodicamente le statistiche di RNS."""
    import RNS
    while True:
        try:
            # Destinazioni note — attributo cambiato in RNS recente
            known = 0
            for attr in ("destination_table", "destinations", "_destinations"):
                tbl = getattr(RNS.Transport, attr, None)
                if tbl is not None:
                    known = len(tbl)
                    break

            # Path table
            paths = 0
            for attr in ("path_table", "hops_to", "_path_table"):
                tbl = getattr(RNS.Transport, attr, None)
                if tbl is not None:
                    paths = len(tbl)
                    break

            # Interfacce
            ifaces = []
            iface_list = getattr(RNS.Transport, "interfaces", [])
            for iface in iface_list:
                ifaces.append({
                    "name":   str(iface),
                    "type":   type(iface).__name__,
                    "online": getattr(iface, "online",
                              getattr(iface, "IN", True)),
                    "rxb":    getattr(iface, "rxb", 0),
                    "txb":    getattr(iface, "txb", 0),
                })

            total_rx = sum(i["rxb"] for i in ifaces)
            total_tx = sum(i["txb"] for i in ifaces)

            now = time.time()
            with state_lock:
                state["interfaces"]  = ifaces
                state["known_dests"] = known
                state["paths"]       = paths
                state["traffic_rx"]  = total_rx
                state["traffic_tx"]  = total_tx
                state["rns_online"]  = True
                state["last_update"] = now
                # Storico traffico: tieni ultimi 60 campioni
                history = state["traffic_history"]
                history.append({"rx": total_rx, "tx": total_tx, "ts": now})
                if len(history) > 60:
                    history.pop(0)

        except Exception as e:
            log(f"Errore stats loop: {e}", error=True)

        time.sleep(STATUS_INTERVAL)

# ---------------------------------------------------------------------------
# Echo bot LXMF

def _bot_compose_reply(content):
    """Decide la risposta del bot in base al contenuto. Ritorna (testo, titolo) o (None,None)."""
    text = content.strip()
    low = text.lower()

    with state_lock:
        echo_on = state.get("bot_echo", True)
        cmds_on = state.get("bot_commands", True)
        custom = dict(state.get("bot_custom", {}))

    # 1. Comandi
    if cmds_on:
        if low in ("help", "?", "aiuto", "/help", "comandi"):
            msg = ("Comandi rCompanion:\n"
                   "- help: questo messaggio\n"
                   "- meteo: meteo attuale\n"
                   "- status: stato del nodo\n"
                   "- info: info sul nodo\n")
            if custom:
                msg += "Parole speciali:\n"
                for k in list(custom.keys())[:10]:
                    msg += f"- {k}\n"
            msg += "Altri messaggi ricevono echo."
            return (msg, "rCompanion Help")
        if low in ("meteo", "weather", "tempo"):
            with state_lock:
                wx = dict(state.get("weather", {}))
            if wx and wx.get("temp") is not None:
                return (f"Meteo {wx.get('city','?')}: {wx.get('temp')}C "
                        f"(perc. {wx.get('feels','?')}C), "
                        f"umidita {wx.get('humidity','?')}%, "
                        f"pressione {wx.get('pressure','?')}hPa, "
                        f"vento {wx.get('wind','?')}km/h", "Meteo")
            return ("Dati meteo non ancora disponibili.", "Meteo")
        if low in ("status", "stato", "/status"):
            with state_lock:
                rns = state.get("rns_online", False)
                paths = state.get("paths", state.get("known_dests", 0))
                dests = state.get("known_dests", 0)
            try: up = _uptime_str()
            except Exception: up = "?"
            return (f"rCompanion attivo.\nRNS: {'online' if rns else 'offline'}\n"
                    f"Paths: {paths}\nDestinazioni: {dests}\nUptime: {up}", "Status")
        if low in ("info", "about", "/info"):
            return (f"rCompanion v{VERSION} - companion ESP32 per nodo Reticulum.\n"
                    "Mostra stato RNS, traffico, mappa rmap.world, meteo e altro.", "Info")

    # 2. Custom replies (match per parola/frase contenuta)
    for key, val in custom.items():
        if key and key.lower() in low:
            return (val, "rCompanion")

    # 3. Echo
    if echo_on:
        return (f"Echo: {text}", "rCompanion Echo")

    return (None, None)


def _echo_bot_loop():
    """Risponde ai messaggi LXMF in arrivo: comandi, custom reply, echo."""
    import RNS, LXMF
    seen = set()
    while True:
        try:
            with state_lock:
                messages = list(state["messages"])
            for msg in messages:
                mid = msg.get("hash","") + str(msg.get("ts",""))
                if mid not in seen:
                    seen.add(mid)
                    src_hash = msg.get("hash","").strip("<>")
                    content  = msg.get("content","")
                    if src_hash and content and lxmf_router and lxmf_dest:
                        reply_text, reply_title = _bot_compose_reply(content)
                        if not reply_text:
                            log(f"Bot: nessuna risposta per '{content[:20]}' (echo off?)")
                            continue
                        try:
                            dest_hash = bytes.fromhex(src_hash)
                            # 1) Serve un PATH verso il client per consegnare
                            if not RNS.Transport.has_path(dest_hash):
                                log(f"Bot: nessun path per {src_hash[:8]}, lo richiedo")
                                try: RNS.Transport.request_path(dest_hash)
                                except Exception: pass
                                seen.discard(mid)  # riprova al prossimo ciclo
                                continue
                            # 2) Serve l'identita del destinatario
                            dest_id = RNS.Identity.recall(dest_hash)
                            if dest_id is None:
                                log(f"Bot: identita {src_hash[:8]} non nota, attendo")
                                seen.discard(mid)
                                continue
                            dest = RNS.Destination(
                                dest_id,
                                RNS.Destination.OUT,
                                RNS.Destination.SINGLE,
                                "lxmf", "delivery"
                            )
                            reply = LXMF.LXMessage(
                                dest,
                                lxmf_dest,
                                reply_text,
                                title=reply_title,
                                desired_method=LXMF.LXMessage.DIRECT
                            )
                            def _on_failed(m):
                                log(f"Bot: consegna fallita a {src_hash[:8]}", error=True)
                            def _on_sent(m):
                                log(f"Bot: consegnato a {src_hash[:8]}")
                            try:
                                reply.register_delivery_callback(_on_sent)
                                reply.register_failed_callback(_on_failed)
                            except Exception: pass
                            lxmf_router.handle_outbound(reply)
                            with state_lock:
                                nm = state["known_names"].get(src_hash, "")
                                state["echo_log"].appendleft({
                                    "hash": src_hash,
                                    "name": nm or src_hash[:8],
                                    "content": content[:40],
                                    "reply": reply_text[:40],
                                    "ts": time.time(),
                                    "ts_human": datetime.now().strftime("%H:%M:%S"),
                                })
                            log(f"Bot reply -> {src_hash[:8]}: {reply_text[:30]}")
                        except Exception as e:
                            log(f"Reply error: {e}", error=True)
        except Exception as e:
            log(f"Bot loop error: {e}", error=True)
        time.sleep(2)

# ---------------------------------------------------------------------------
# rnstatus parser

def _parse_rnstatus(raw):
    """Parsa output di rnstatus in lista di dict interfacce."""
    ifaces = []
    current = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            if current:
                ifaces.append(current)
                current = None
            continue
        # Nuova interfaccia: inizia con nome tipo [nome]
        if line.startswith("Interface") or ("Interface" in line and line.endswith("]") or line.endswith(">")):
            if current:
                ifaces.append(current)
            current = {"name": line, "type": "", "status": "", "rx": "", "tx": "", "raw": line}
        elif current:
            low = line.lower()
            if "status" in low:
                current["status"] = line.split(":",1)[-1].strip()
            elif "rx" in low and "bytes" in low:
                current["rx"] = line.split(":",1)[-1].strip()
            elif "tx" in low and "bytes" in low:
                current["tx"] = line.split(":",1)[-1].strip()
            elif "type" in low:
                current["type"] = line.split(":",1)[-1].strip()
            current["raw"] += " | " + line
    if current:
        ifaces.append(current)
    return ifaces


def _rnstatus_loop():
    """Aggiorna cache rnstatus ogni 30 secondi."""
    import subprocess, os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    while True:
        try:
            result = subprocess.run(
                ["rnstatus"],
                capture_output=True, timeout=10, encoding="utf-8",
                errors="replace", env=env
            )
            raw = result.stdout or ""
            if not raw.strip() and result.stderr:
                log(f"rnstatus stderr: {result.stderr[:150]}", error=True)
                raw = "rnstatus: errore esecuzione"
            # Rimuovi caratteri non-ASCII (box drawing, emoji, ecc.)
            raw_clean = "".join(c if ord(c) < 128 else " " for c in raw)
            ifaces = _parse_rnstatus(raw_clean)
            def _ascii(s):
                return "".join(c for c in str(s) if ord(c) < 128).strip()
            ifaces_clean = [{k: _ascii(v) for k,v in iface.items()} for iface in ifaces]
            with state_lock:
                state["rnstatus_cache"] = raw_clean
                state["rnstatus_ifaces"] = ifaces_clean
                state["rnstatus_ts"] = time.time()
            log(f"rnstatus: {len(ifaces)} interfacce trovate")
        except FileNotFoundError:
            with state_lock:
                state["rnstatus_cache"] = "rnstatus non trovato nel PATH"
        except Exception as e:
            log(f"rnstatus error: {e}", error=True)
        time.sleep(30)


def _rmap_loop():
    """Scarica dati nodi da rmap.world e conta per tipo."""
    import urllib.request, json as _json, ssl
    # Contesto SSL senza verifica certificati (dati pubblici, Windows senza CA)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    time.sleep(5)
    while True:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
            nodes = []
            node_count = 0
            views = 0
            import re

            # Metodo 1: prova endpoint JSON
            try:
                req = urllib.request.Request(RMAP_URL, headers=headers)
                with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data_rmap = _json.loads(raw)
                nodes = data_rmap.get("nodes", []) if isinstance(data_rmap, dict) else data_rmap
                node_count = data_rmap.get("node_count", len(nodes)) if isinstance(data_rmap, dict) else len(nodes)
                views = data_rmap.get("views", 0) if isinstance(data_rmap, dict) else 0
            except Exception:
                # Metodo 2: scraping HTML - estrai const allNodes = [...] e views
                req2 = urllib.request.Request("https://rmap.world/", headers=headers)
                with urllib.request.urlopen(req2, timeout=15, context=ssl_ctx) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                m = re.search(r"const allNodes\s*=\s*(\[.*?\]);", html, re.DOTALL)
                if m:
                    nodes = _json.loads(m.group(1))
                    node_count = len(nodes)
                    log(f"rmap: scraping HTML ok ({node_count} nodi)")
                else:
                    log("rmap: allNodes non trovato nell'HTML", error=True)
                # Estrai views: "Views: 36,529"
                mv = re.search(r"Views:\s*([\d,\.]+)", html)
                if mv:
                    try:
                        views = int(mv.group(1).replace(",", "").replace(".", ""))
                    except Exception:
                        views = 0
            # Conta per node_type, dedupe per identity_hash (un nodo = molte interfacce)
            counts = {}
            seen_identities = {}  # identity_hash -> set di tipi
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                ntype = str(n.get("node_type", "Unknown"))
                counts[ntype] = counts.get(ntype, 0) + 1
            # Conta nodi unici (per identity)
            unique_ids = set()
            for n in nodes:
                if isinstance(n, dict):
                    iid = n.get("identity_hash") or n.get("hash")
                    if iid: unique_ids.add(iid)
            with state_lock:
                state["rmap_counts"] = counts
                state["rmap_total"] = node_count
                state["rmap_unique"] = len(unique_ids)
                if views: state["rmap_views"] = views
                state["rmap_ts"] = time.time()
            log(f"rmap: {node_count} nodi, {len(unique_ids)} unici, {len(counts)} tipi, {views} views")
        except Exception as e:
            log(f"rmap fetch error: {e}", error=True)
        time.sleep(RMAP_INTERVAL)


def _version_loop():
    """Controlla aggiornamenti su GitHub ogni ora."""
    import urllib.request, json as _json, ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    time.sleep(20)
    while True:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "rCompanion"})
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                rel = _json.loads(resp.read().decode("utf-8", errors="replace"))
            latest = str(rel.get("tag_name", "")).lstrip("v")
            update = False
            if latest:
                try:
                    lv = tuple(int(x) for x in latest.split("."))
                    cv = tuple(int(x) for x in VERSION.split("."))
                    update = lv > cv
                except Exception:
                    update = (latest != VERSION)
            with state_lock:
                state["version_latest"] = latest
                state["version_update"] = update
            if update:
                log(f"Aggiornamento disponibile: v{latest} (locale v{VERSION})")
        except Exception:
            # Repo non ancora pubblicato o nessuna release: silenzioso
            pass
        time.sleep(3600)


def _host_loop():
    """Monitora risorse del PC host (CPU/RAM/disco)."""
    try:
        import psutil
        has_psutil = True
    except ImportError:
        has_psutil = False
        log("psutil non installato: pip install psutil", error=True)

    while True:
        try:
            host = {}
            if has_psutil:
                host["cpu"] = psutil.cpu_percent(interval=1)
                vm = psutil.virtual_memory()
                host["ram_pct"] = vm.percent
                host["ram_used"] = vm.used // (1024*1024)   # MB
                host["ram_total"] = vm.total // (1024*1024)  # MB
                # Disco: partizione del sistema
                try:
                    du = psutil.disk_usage("/")
                except Exception:
                    du = psutil.disk_usage("C:\\")
                host["disk_pct"] = du.percent
                host["disk_used"] = du.used // (1024*1024*1024)   # GB
                host["disk_total"] = du.total // (1024*1024*1024)  # GB
                # Temperatura CPU se disponibile
                try:
                    temps = psutil.sensors_temperatures()
                    if temps:
                        for name, entries in temps.items():
                            if entries:
                                host["cpu_temp"] = round(entries[0].current)
                                break
                except Exception:
                    pass
                # Numero core + freq
                try:
                    host["cores"] = psutil.cpu_count(logical=True)
                    freq = psutil.cpu_freq()
                    if freq:
                        host["cpu_mhz"] = int(freq.current)
                except Exception:
                    pass
            else:
                host["error"] = "psutil mancante"

            with state_lock:
                state["host"] = host
                state["host_ts"] = time.time()
        except Exception as e:
            log(f"host loop error: {e}", error=True)
        time.sleep(5)


def _weather_loop():
    """Scarica meteo da Open-Meteo (gratuito, no API key) per la citta selezionata."""
    import urllib.request, urllib.parse, json as _json, ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    headers = {"User-Agent": "rCompanion/1.0"}
    last_city = None
    cached_coords = None
    time.sleep(8)
    while True:
        try:
            with state_lock:
                city = state.get("weather_city", "Roma")
            # Geocoding solo se citta cambiata
            if city != last_city or cached_coords is None:
                geo_url = ("https://geocoding-api.open-meteo.com/v1/search?name="
                           + urllib.parse.quote(city) + "&count=1&language=it")
                req = urllib.request.Request(geo_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                    geo = _json.loads(resp.read().decode("utf-8", errors="replace"))
                results = geo.get("results", [])
                if results:
                    cached_coords = (results[0]["latitude"], results[0]["longitude"],
                                     results[0].get("name", city))
                    last_city = city
                else:
                    log(f"meteo: citta '{city}' non trovata", error=True)
                    time.sleep(WEATHER_INTERVAL)
                    continue

            lat, lon, cname = cached_coords
            # Dati meteo correnti + pressione
            wx_url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                      "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                      "surface_pressure,weather_code,wind_speed_10m,pressure_msl"
                      "&timezone=auto")
            req = urllib.request.Request(wx_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                wx = _json.loads(resp.read().decode("utf-8", errors="replace"))
            cur = wx.get("current", {})
            weather = {
                "city":     cname,
                "temp":     cur.get("temperature_2m"),
                "feels":    cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "pressure": cur.get("pressure_msl") or cur.get("surface_pressure"),
                "wind":     cur.get("wind_speed_10m"),
                "code":     cur.get("weather_code"),
            }
            with state_lock:
                state["weather"] = weather
                state["weather_ts"] = time.time()
            log(f"meteo: {cname} {weather['temp']}C {weather['pressure']}hPa")
        except Exception as e:
            log(f"meteo error: {e}", error=True)
        time.sleep(WEATHER_INTERVAL)


def _auto_announce_loop():
    """Auto-annuncia identità rCompanion sulla rete ogni 10 minuti."""
    import RNS
    time.sleep(15)  # aspetta avvio completo
    while True:
        try:
            if lxmf_dest and lxmf_router:
                lxmf_router.announce(lxmf_dest.hash)
                log("Auto-announce inviato")
        except Exception as e:
            log(f"Auto-announce error: {e}", error=True)
        time.sleep(600)  # ogni 10 minuti


# ---------------------------------------------------------------------------
# Logger interno
# ---------------------------------------------------------------------------

def log(msg, error=False):
    ts  = datetime.now().strftime("%H:%M:%S")
    lvl = "ERR" if error else "INF"
    line = f"[{ts}] [{lvl}] {msg}"
    print(line)
    with state_lock:
        state["log_lines"].appendleft(line)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

def _uptime_str():
    secs = int(time.time() - state["uptime_start"])
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"

def _fmt_bytes(b):
    if b < 1024:
        return f"{b}B"
    elif b < 1024 * 1024:
        return f"{b/1024:.1f}KB"
    else:
        return f"{b/1024/1024:.1f}MB"


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "rns_online":    state["rns_online"],
            "lxmf_online":   state["lxmf_online"],
            "uptime":        _uptime_str(),
            "uptime_secs":   int(time.time() - state["uptime_start"]),
            "traffic_rx":    _fmt_bytes(state["traffic_rx"]),
            "traffic_tx":    _fmt_bytes(state["traffic_tx"]),
            "traffic_rx_raw": state["traffic_rx"],
            "traffic_tx_raw": state["traffic_tx"],
            "known_dests":   state["known_dests"],
            "paths":         state["paths"],
            "unread_lxmf":   state["unread_count"],
            "last_update":   state["last_update"],
            "error":         state["error"],
        })


@app.route("/api/interfaces")
def api_interfaces():
    with state_lock:
        ifaces = list(state["interfaces"])
    return jsonify({
        "count":      len(ifaces),
        "interfaces": ifaces,
    })


@app.route("/api/announces")
def api_announces():
    limit = int(request.args.get("limit", 10))
    with state_lock:
        all_ann  = list(state["announces"])
        announces = all_ann[:limit]
    cutoff = time.time() - 300
    recent = sum(1 for a in all_ann if a["ts"] > cutoff)
    with state_lock:
        total = state["total_announces"]
    # Conta per aspect (da app_data leggibile)
    aspects = {}
    for a in all_ann:
        ad = a.get("app_data","")
        # Cerca aspect leggibile
        aspect = "other"
        for kw in ["lxmf","nomadnet","rnode","meshchat","backbone","node"]:
            if kw in ad.lower():
                aspect = kw
                break
        aspects[aspect] = aspects.get(aspect, 0) + 1
    return jsonify({
        "total":     total,
        "recent_5m": recent,
        "aspects":   aspects,
        "announces": announces,
    })


@app.route("/api/lxmf")
def api_lxmf():
    limit = int(request.args.get("limit", 5))
    with state_lock:
        messages = list(state["messages"])[:limit]
        unread   = state["unread_count"]
        known    = dict(state["known_names"])
    # Arricchisci display_name retroattivamente
    for msg in messages:
        if not msg.get("display_name"):
            h = msg.get("hash","").strip("<>")
            msg["display_name"] = known.get(h, "")
    return jsonify({
        "unread":   unread,
        "messages": messages,
    })


@app.route("/api/lxmf/read", methods=["POST"])
def api_lxmf_read():
    """Marca tutti i messaggi come letti."""
    with state_lock:
        for msg in state["messages"]:
            msg["read"] = True
        state["unread_count"] = 0
    return jsonify({"ok": True})


@app.route("/api/identity")
def api_identity():
    import RNS
    lxmf_h = ""
    try:
        if lxmf_dest:
            # lxmf_dest è una Destination RNS, hash è bytes
            raw = lxmf_dest.hash
            if isinstance(raw, bytes):
                lxmf_h = raw.hex()
            else:
                lxmf_h = str(raw)
    except Exception as e:
        log(f"LXMF hash error: {e}", error=True)
    # Fallback: leggi da file lxmf storage
    if not lxmf_h:
        try:
            import pathlib, RNS
            lxmf_id_path = pathlib.Path(RNS.Reticulum.storagepath) / "lxmf_client"
            for f in ["identity", "delivery_identity"]:
                p = lxmf_id_path / f
                if p.exists():
                    lid = RNS.Identity.from_file(str(p))
                    if lid:
                        lxmf_h = lid.hash.hex()
                        break
        except Exception as e:
            log(f"LXMF hash fallback error: {e}")
    with state_lock:
        return jsonify({
            "hash":      state["identity_hash"],
            "name":      state["identity_name"],
            "lxmf_hash": lxmf_h,
        })


@app.route("/api/log")
def api_log():
    limit = int(request.args.get("limit", 50))
    with state_lock:
        lines = list(state["log_lines"])[:limit]
    return jsonify({
        "lines": lines,
        "count": len(lines),
    })


@app.route("/api/names")
def api_names():
    with state_lock:
        return jsonify(state["known_names"])


@app.route("/api/rmap")
def api_rmap():
    with state_lock:
        return jsonify({
            "counts": state["rmap_counts"],
            "total":  state["rmap_total"],
            "unique": state["rmap_unique"],
            "views":  state["rmap_views"],
            "ts":     state["rmap_ts"],
        })


@app.route("/api/echo")
def api_echo():
    with state_lock:
        return jsonify({"log": list(state["echo_log"])})


@app.route("/api/host")
def api_host():
    with state_lock:
        return jsonify({"host": state["host"], "ts": state["host_ts"]})


@app.route("/api/bot")
def api_bot():
    with state_lock:
        return jsonify({
            "echo":     state.get("bot_echo", True),
            "commands": state.get("bot_commands", True),
            "custom":   state.get("bot_custom", {}),
        })


@app.route("/api/bot", methods=["POST"])
def api_bot_set():
    data = request.get_json(force=True, silent=True) or {}
    with state_lock:
        if "echo" in data:     state["bot_echo"] = bool(data["echo"])
        if "commands" in data: state["bot_commands"] = bool(data["commands"])
        if "custom" in data and isinstance(data["custom"], dict):
            # Pulisci e limita
            clean = {}
            for k, v in list(data["custom"].items())[:20]:
                k = str(k).strip()[:40]
                v = str(v).strip()[:200]
                if k and v:
                    clean[k] = v
            state["bot_custom"] = clean
    save_settings()
    return jsonify({"ok": True})


def _ann_stats():
    """Calcola statistiche annunci: tipi (torta) + buckets orari.
    NOTA: il chiamante DEVE detenere state_lock."""
    now = time.time()
    total = state["total_announces"]
    lxmf  = state["ann_lxmf"]
    nomad = state["ann_nomad"]
    times = list(state["ann_times"])
    other = max(0, total - lxmf - nomad)
    # Buckets per le ultime 12 ore (1 bucket = 1 ora)
    buckets = [0]*12
    for t in times:
        age_h = (now - t) / 3600.0
        if 0 <= age_h < 12:
            buckets[11 - int(age_h)] += 1
    return {
        "types": {"LXMF": lxmf, "NomadNet": nomad, "Other": other},
        "total": total,
        "hourly": buckets,  # 12 valori, ultimo = ora corrente
    }


@app.route("/api/annstats")
def api_annstats():
    with state_lock:
        return jsonify(_ann_stats())


@app.route("/api/version")
def api_version():
    """Versione locale + controllo aggiornamenti su GitHub."""
    import urllib.request, json as _json, ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    latest = None
    update = False
    try:
        # GitHub API: ultima release
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "rCompanion"})
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            rel = _json.loads(resp.read().decode("utf-8", errors="replace"))
        latest = str(rel.get("tag_name", "")).lstrip("v")
        if latest and latest != VERSION:
            # Confronto semplice numerico
            try:
                lv = tuple(int(x) for x in latest.split("."))
                cv = tuple(int(x) for x in VERSION.split("."))
                update = lv > cv
            except Exception:
                update = latest != VERSION
    except Exception as e:
        log(f"version check: {e}", error=True)
    return jsonify({
        "local":  VERSION,
        "latest": latest,
        "update": update,
        "repo":   GITHUB_REPO,
    })


@app.route("/api/weather")
def api_weather():
    with state_lock:
        return jsonify({
            "weather": state["weather"],
            "city":    state["weather_city"],
            "ts":      state["weather_ts"],
        })


@app.route("/api/weather/city", methods=["POST"])
def api_weather_city():
    data = request.get_json(force=True, silent=True) or {}
    city = str(data.get("city", "")).strip()[:40]
    if city:
        with state_lock:
            state["weather_city"] = city
            state["weather"] = {}  # forza refresh
            state["weather_ts"] = 0
        save_settings()
        return jsonify({"ok": True, "city": city})
    return jsonify({"ok": False}), 400


@app.route("/api/events")
def api_events():
    """Eventi recenti combinati: annunci + messaggi, ordine cronologico."""
    limit = int(request.args.get("limit", 15))
    events = []
    with state_lock:
        for a in list(state["announces"])[:limit]:
            events.append({
                "type": "ann",
                "name": a.get("display_name","") or a.get("hash","")[:8],
                "ts":   a.get("ts",0),
                "tsh":  a.get("ts_human",""),
            })
        for m in list(state["messages"])[:limit]:
            events.append({
                "type": "lxmf",
                "name": m.get("display_name","") or m.get("hash","").strip("<>")[:8],
                "ts":   m.get("ts",0),
                "tsh":  m.get("ts_human",""),
            })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify({"events": events[:limit]})


@app.route("/api/rnstatus")
def api_rnstatus():
    with state_lock:
        return jsonify({
            "raw":    state["rnstatus_cache"],
            "ifaces": state["rnstatus_ifaces"],
            "ts":     state["rnstatus_ts"],
        })


@app.route("/api/traffic")
def api_traffic():
    with state_lock:
        history = list(state["traffic_history"])
    # Calcola delta tra campioni per rate
    deltas = []
    for i in range(1, len(history)):
        dt = history[i]["ts"] - history[i-1]["ts"]
        if dt > 0:
            drx = max(0, history[i]["rx"] - history[i-1]["rx"])
            dtx = max(0, history[i]["tx"] - history[i-1]["tx"])
            deltas.append({
                "ts":   history[i]["ts"],
                "rx_s": round(drx / dt),
                "tx_s": round(dtx / dt),
            })
    return jsonify({
        "history": history[-20:],   # ultimi 20 campioni assoluti
        "deltas":  deltas[-20:],    # ultimi 20 rate (bytes/sec)
    })


@app.route("/api/debug")
def api_debug():
    import RNS, pathlib
    storage = pathlib.Path(RNS.Reticulum.storagepath)
    paths = {
        "storagepath": str(storage),
        "transport_identity_exists": (storage / "transport_identity").exists(),
        "identity_exists": (storage / "identity").exists(),
        "home_storage_identity": str(pathlib.Path.home() / ".reticulum" / "storage" / "identity"),
    }
    # Lista file nello storage
    try:
        files = [str(f.name) for f in storage.iterdir()]
        paths["storage_files"] = files
    except Exception as e:
        paths["storage_files_error"] = str(e)
    with state_lock:
        paths["current_identity_hash"] = state["identity_hash"]
        paths["current_lxmf_online"] = state["lxmf_online"]
    return jsonify(paths)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """
    Riavvia un'interfaccia o l'intero rnsd.
    Body JSON: {"target": "all"} oppure {"target": "TCPServerInterface"}
    """
    data   = request.get_json(silent=True) or {}
    target = data.get("target", "all")
    try:
        import RNS
        if target == "all":
            # Riavvia il processo rnsd se standalone
            log("Restart rnsd richiesto dal C6")
            # Qui puoi aggiungere subprocess.Popen(["rnsd", ...]) se vuoi
            return jsonify({"ok": True, "message": "restart rnsd non implementato in questa modalità"})
        else:
            # Cerca e riavvia interfaccia specifica
            for iface in RNS.Transport.interfaces:
                if target in str(iface):
                    if hasattr(iface, "reconnect"):
                        iface.reconnect()
                        log(f"Interfaccia {target} reconnect richiesto")
                        return jsonify({"ok": True, "message": f"reconnect {target}"})
            return jsonify({"ok": False, "message": f"interfaccia {target} non trovata"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/all")
def api_all():
    """
    Tutti i dati in un colpo solo — usato dal C6 per ridurre le chiamate HTTP.
    """
    limit_ann = int(request.args.get("ann", 5))
    limit_msg = int(request.args.get("msg", 3))

    with state_lock:
        announces = list(state["announces"])[:limit_ann]
        messages  = list(state["messages"])[:limit_msg]
        cutoff    = time.time() - 300
        recent_5m = sum(1 for a in announces if a["ts"] > cutoff)

        # Epoch "locale" robusto: usa direttamente l'ora di sistema.
        # Differenza now()-utcnow() = offset reale del sistema (fuso+DST inclusi)
        _now = datetime.now()
        _utc = datetime.utcnow()
        _off = round((_now - _utc).total_seconds())
        _local_epoch = int(time.time()) + _off

        return jsonify({
            "status": {
                "rns_online":    state["rns_online"],
                "lxmf_online":   state["lxmf_online"],
                "uptime":        _uptime_str(),
                "uptime_secs":   int(time.time() - state["uptime_start"]),
                "traffic_rx":    _fmt_bytes(state["traffic_rx"]),
                "traffic_tx":    _fmt_bytes(state["traffic_tx"]),
                "known_dests":   state["known_dests"],
                "paths":         state["paths"],
                "unread_lxmf":   state["unread_count"],
                "error":         state["error"],
            },
            "interfaces": state["interfaces"],
            "announces": {
                "recent_5m": recent_5m,
                "total":     state["total_announces"],
                "list":      announces,
            },
            "lxmf": {
                "unread":   state["unread_count"],
                "messages": messages,
            },
            "identity": {
                "hash":      state["identity_hash"],
                "name":      state["identity_name"],
                "lxmf_hash": lxmf_dest.hash.hex() if lxmf_dest and hasattr(lxmf_dest, "hash") and isinstance(lxmf_dest.hash, bytes) else "",
            },
            "server_ts": time.time(),
            "clock": {"time": datetime.now().strftime("%H:%M:%S"), "date": datetime.now().strftime("%d/%m/%Y"), "epoch": _local_epoch},
            "rnstatus":  {"ifaces": state["rnstatus_ifaces"], "ts": state["rnstatus_ts"]},
            "traffic_history": state["traffic_history"][-30:],
            "names": dict(list(state["known_names"].items())[-50:]),
            "rmap": {"counts": state["rmap_counts"], "total": state["rmap_total"], "unique": state["rmap_unique"], "views": state["rmap_views"]},
            "weather": state["weather"],
            "host": state["host"],
            "version": {"local": VERSION, "latest": state["version_latest"], "update": state["version_update"]},
            "annstats": _ann_stats(),
        })


def sanitize_ascii(obj):
    """Ricorsivamente pulisce stringhe da caratteri non-ASCII per MicroPython."""
    if isinstance(obj, str):
        return "".join(c if ord(c) < 128 else "?" for c in obj)
    elif isinstance(obj, dict):
        return {k: sanitize_ascii(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_ascii(i) for i in obj]
    return obj


@app.after_request
def add_cors(response):
    """CORS aperto per LAN — il C6 chiama da IP diverso."""
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Sanitizza JSON per MicroPython (no caratteri > 127)
    if response.content_type and "json" in response.content_type:
        try:
            import json as _json
            data = _json.loads(response.get_data(as_text=True))
            clean = sanitize_ascii(data)
            response.set_data(_json.dumps(clean, ensure_ascii=True))
        except Exception:
            pass
    return response


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("  rCompanion Server v0.1")
    print(f"  In ascolto su http://0.0.0.0:{PORT}")
    print("=" * 55)

    # Carica impostazioni persistenti (citta meteo, bot, ecc.)
    load_settings()

    # RNS DEVE girare nel main thread su Windows (signal.signal)
    # Avvia Flask in thread separato invece
    flask_thread = threading.Thread(
        target=lambda: app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    log("Flask avviato in background")

    # Init RNS nel main thread
    init_rns()

    # Mantieni il main thread vivo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Shutdown rCompanion server")
