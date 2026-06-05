# rCompanion v0.5 - main.py
# ESP32-C6 LAFVIN 1.47" ST7789 - layout fix + nuove feature

import time, json, network, urequests, machine, neopixel, uasyncio as asyncio
import gc
from machine import Pin, SPI, PWM
import st7789py as st7789
import vga1_8x16
FN = vga1_8x16
FB = vga1_8x16
VERSION = "0.5"

# ── Config ─────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "wifi_ssid":     "YOURWIFI",
    "wifi_pass":     "YOURPASSWORD",
    "server_ip":     "192.168.1.XX",
    "server_port":   5000,
    "poll_interval": 5,
    "node_name":     "rCompanion",
    "brightness":    80,
    "auto_rotate":   0,    # secondi tra pagine (0=disabilitato)
    "enabled_pages": [],   # lista bool per pagina (vuoto = tutte attive)
    "led_follow_page": True,  # LED segue il colore della pagina
}
CONFIG_FILE = "config.json"

PIN_SCL=7; PIN_SDA=6; PIN_RST=21; PIN_DC=15; PIN_CS=14; PIN_BLK=22
PIN_LED=8; PIN_BTNA=9; PIN_BTNB=10

# ── Colori ─────────────────────────────────────────────────────────────────

BLACK=0x0000; WHITE=0xFFFF; GREEN=0x07E0; RED=0xF800; BLUE=0x001F
CYAN=0x07FF;  YELLOW=0xFFE0; GRAY=0x8410; DKGRAY=0x2104; ORANGE=0xFD20
TEAL=0x03EF;  PURPLE=0x781F; DKGREEN=0x0320; DKBLUE=0x0008

BG=BLACK; FG=WHITE; ACCENT=CYAN; ONLINE=GREEN; OFFLINE=RED
WARN=YELLOW; DIM=GRAY; DKBG=DKGRAY

# Display geometry — OX=40 calibrato
OX=40; DW=166; DH=320; CW=8; CH=16; CPL=DW//CW  # CPL=20, DW=166 (col_offset=34, fisico=172)

# ── Stato ──────────────────────────────────────────────────────────────────

cfg=dict(DEFAULT_CONFIG); current_page=0; NUM_PAGES=26
data={}; last_poll=0; wifi_ok=False; poll_error=False; srv_ok=False; poll_fails=0; webui_ok=False
traffic_history=[]  # campioni locali per grafico
events_cache=[]  # eventi da /api/events
echo_cache=[]  # log risposte echo
prev_unread=0  # conteggio messaggi non letti al poll precedente
clock_epoch=0  # epoch ora server
clock_synced_at=0  # ticks_ms al momento del sync

# ── LED ────────────────────────────────────────────────────────────────────

np = neopixel.NeoPixel(Pin(PIN_LED), 1)
LED={"ok":(0,25,0),"lxmf":(40,40,0),"announce":(0,0,40),
     "error":(60,0,0),"iface_dn":(40,0,40),"refresh":(15,15,15),"off":(0,0,0)}

# Colore header (RGB565) di ogni pagina, per il LED "segui pagina"
PAGE_COLORS = [
    0x07FF,  # 1 Overview (ACCENT=CYAN)
    0x03EF,  # 2 Interfaces (TEAL)
    0x0320,  # 3 Announces (DKGREEN)
    0x0008,  # 4 LXMF (DKBLUE)
    0x781F,  # 5 Identity (PURPLE)
    0x3186,  # 6 RNS Status
    0x4A0F,  # 7 Traffic
    0x0A4A,  # 8 Peers
    0x4A00,  # 9 Events
    0x6200,  # 10 RMAP
    0x001F,  # 11 Clock (BLUE)
    0x528A,  # 12 Settings
    0x07E0,  # 13 Echo (GREEN)
    0x5D1F,  # 14 Meteo
    0xFD20,  # 15 Risorse C6 (ORANGE)
    0xF81F,  # 16 Server PC (MAGENTA)
    0x07FF,  # 17 About (CYAN)
    0xFBE0,  # 18 Ann Stats
    0x051F,  # 19 WiFi Scan (BLUE)
    0x781F,  # 20 BT Scan (PURPLE)
    0xFD20,  # 21 Consumo (ORANGE)
    0x07E0,  # 22 Snake (GREEN)
    0xFFE0,  # 23 Pong (YELLOW)
    0x07FF,  # 24 Invaders (CYAN)
    0xFD20,  # 25 Breakout (ORANGE)
    0x781F,  # 26 Screensaver (PURPLE)
]

def rgb565_to_led(c, dim=5):
    """Converte RGB565 in tupla RGB per NeoPixel, attenuata (dim = divisore)."""
    r = ((c >> 11) & 0x1F) << 3
    g = ((c >> 5) & 0x3F) << 2
    b = (c & 0x1F) << 3
    return (r//dim, g//dim, b//dim)

def update_led():
    """Aggiorna il LED: segue il colore pagina se attivo, altrimenti lo stato."""
    if cfg.get("led_follow_page", True):
        c = PAGE_COLORS[current_page] if current_page < len(PAGE_COLORS) else 0x07FF
        np[0] = rgb565_to_led(c); np.write()
    else:
        # Modalita stato
        s = data.get("status",{}) if data else {}
        if not s.get("rns_online"): led_set("error")
        elif s.get("unread_lxmf",0)>0: led_set("lxmf")
        else:
            any_down=any(not i.get("online",True) for i in data.get("interfaces",[])) if data else False
            led_set("iface_dn" if any_down else "ok")

def led_set(n): np[0]=LED.get(n,(0,0,0)); np.write()
async def led_pulse(n,t=3,on=150,off=150):
    for _ in range(t):
        led_set(n); await asyncio.sleep_ms(on)
        led_set("off"); await asyncio.sleep_ms(off)

async def led_flash_message():
    """Flash giallo+verde alternato per ~1s all'arrivo di un messaggio."""
    for _ in range(5):  # 5 cicli x 200ms = 1s
        led_set("lxmf"); await asyncio.sleep_ms(100)   # giallo
        led_set("ok");   await asyncio.sleep_ms(100)   # verde
    update_led()  # ripristina colore pagina/stato

# ── Config ─────────────────────────────────────────────────────────────────

def load_config():
    global cfg
    try:
        with open(CONFIG_FILE) as f: cfg.update(json.load(f))
    except: pass

def save_config():
    with open(CONFIG_FILE,"w") as f: json.dump(cfg,f)

# ── WiFi ───────────────────────────────────────────────────────────────────

def wifi_connect():
    global wifi_ok
    wlan=network.WLAN(network.STA_IF); wlan.active(True)
    if wlan.isconnected(): wifi_ok=True; return wlan.ifconfig()[0]
    wlan.connect(cfg["wifi_ssid"],cfg["wifi_pass"])
    for _ in range(20):
        if wlan.isconnected(): wifi_ok=True; return wlan.ifconfig()[0]
        time.sleep(0.5)
    wifi_ok=False; return None

def get_ip():
    w=network.WLAN(network.STA_IF)
    return w.ifconfig()[0] if w.isconnected() else "---"

# ── Display ────────────────────────────────────────────────────────────────

tft=None
blk_pwm=None

def display_init():
    global tft, blk_pwm
    try:
        spi=SPI(1,baudrate=40000000,sck=Pin(PIN_SCL),mosi=Pin(PIN_SDA))
        tft=st7789.ST7789(spi,240,320,
            reset=Pin(PIN_RST,Pin.OUT),dc=Pin(PIN_DC,Pin.OUT),
            cs=Pin(PIN_CS,Pin.OUT),backlight=Pin(PIN_BLK,Pin.OUT),rotation=0)
        tft.init(st7789._ST7789_INIT_CMDS)
        tft.fill(BG)
        # PWM sul backlight per luminosita regolabile
        try:
            blk_pwm=PWM(Pin(PIN_BLK), freq=1000)
            apply_brightness()
        except Exception as be:
            print(f"PWM backlight non disponibile: {be}")
        return True
    except Exception as e: print(f"Display FAIL: {e}"); return False

def apply_brightness():
    """Applica la luminosita corrente (cfg['brightness'] 0-100) al backlight PWM."""
    global blk_pwm
    if blk_pwm is None: return
    pct=max(5,min(100,int(cfg.get("brightness",80))))  # min 5% per non spegnere
    blk_pwm.duty(int(pct*1023/100))

# ── Drawing ────────────────────────────────────────────────────────────────

def clear(): 
    if tft: tft.fill(BG)

def t(x, y, s, fg=FG, bg=BG):
    if tft:
        s = str(s)
        max_chars = max(1, (DW - x - 2) // CW)  # -2px margine
        if len(s) > max_chars:
            s = s[:max_chars-1] + "~"
        tft.text(FN, s, OX+x, y, fg, bg)

def tc(y, s, fg=FG, bg=BG):
    if tft:
        s = trunc(str(s), CPL-1)
        x = OX + max(0,(DW - len(s)*CW)//2)
        tft.text(FN, s, x, y, fg, bg)

def tr(y, s, fg=FG, bg=BG):
    if tft:
        s = trunc(str(s), CPL-1)
        x = OX + DW - len(s)*CW - 2  # 2px margine
        tft.text(FN, s, max(OX,x), y, fg, bg)

def hl(y, color=DKBG):
    if tft: tft.hline(OX, y, DW, color)

def box(x, y, w, h, color):
    if tft: tft.fill_rect(OX+x, y, w, h, color)

def dot(x, y, on, sz=10):
    if tft: tft.fill_rect(OX+x, y, sz, sz, ONLINE if on else OFFLINE)

def trunc(s, n):
    s=str(s); return s if len(s)<=n else s[:n-1]+"~"

def trow(y, label, value, lc=DIM, vc=FG):
    """Riga label sinistra + valore destra, no overlap garantito."""
    if not tft: return
    lbl = trunc(str(label), 5)   # label max 5 chars
    val = trunc(str(value), 13)  # valore max 13 chars (5+1+13=19 <= 21)
    tft.text(FN, lbl, OX, y, lc, BG)
    vx = OX + DW - len(val)*CW - 2  # 2px margine destra
    lx = OX + len(lbl)*CW + CW      # dopo label + spazio
    tft.text(FN, val, max(lx, vx), y, vc, BG)

def clean_name(s):
    """Estrai parte ASCII leggibile da stringa con bytes non-ASCII."""
    out=""
    for c in str(s):
        if 32<=ord(c)<127: out+=c
    # Prendi la sottostringa più lunga di caratteri leggibili
    parts=[p for p in out.split() if len(p)>=2]
    return " ".join(parts) if parts else out.strip()

# ── Header ─────────────────────────────────────────────────────────────────

def header(title, page_n, bg_color=ACCENT, fg_color=BLACK):
    """
    Header 2 righe:
    Riga 1: titolo centrato
    Riga 2: "── N/5 ──" centrato
    """
    box(0, 0, DW, CH*2+2, bg_color)
    # Titolo centrato — tronca se necessario
    tc(1, trunc(title, CPL), fg_color, bg_color)
    # Numero pagina centrato seconda riga
    pstr = f"- {page_n}/{NUM_PAGES} -"
    tc(CH+2, pstr, fg_color, bg_color)

# ── Pagina 0: OVERVIEW ────────────────────────────────────────────────────

def draw_page_overview():
    clear()
    header("rCompanion", 1, ACCENT, BLACK)
    if not data:
        tc(160, "no data", WARN); return

    s = data.get("status", {})
    y = CH*2 + 8

    # Tre status pill: RNS / LXMF / SRV
    rns_up  = s.get("rns_online", False)
    lxmf_up = s.get("lxmf_online", False)

    # Riga status compatta: dot + label + stato
    def status_row(label, online, yy):
        dot(0, yy+3, online, 10)
        fg = ONLINE if online else DIM
        t(14, yy, label, fg, BG)
        tr(yy, "OK" if online else "--", ONLINE if online else OFFLINE)

    status_row("RNS",  rns_up,  y); y += CH+3
    status_row("LXMF", lxmf_up, y); y += CH+3
    status_row("SRV",  srv_ok,  y); y += CH+3
    status_row("WEB",  webui_ok, y); y += CH+6

    hl(y); y += 5

    # Traffico
    trow(y, "TX", s.get("traffic_tx","?"), DIM, CYAN); y+=CH+2
    trow(y, "RX", s.get("traffic_rx","?"), DIM, CYAN); y+=CH+5

    hl(y); y+=5

    # Stats
    trow(y, "Dest",  s.get("known_dests",0)); y+=CH+2
    trow(y, "Paths", s.get("paths",0));       y+=CH+2
    unread = s.get("unread_lxmf",0)
    trow(y, "LXMF", f"{unread} new", DIM, YELLOW if unread else FG); y+=CH+5

    hl(y); y+=5

    # Mini grafico traffico RX (barra proporzionale)
    if traffic_history and len(traffic_history) >= 2:
        # Calcola rate attuale
        h = traffic_history
        dt = h[-1].get("ts",0) - h[-2].get("ts",1)
        if dt > 0:
            drx = max(0, h[-1].get("rx",0) - h[-2].get("rx",0))
            dtx = max(0, h[-1].get("tx",0) - h[-2].get("tx",0))
            rate_rx = drx // max(1,int(dt))
            rate_tx = dtx // max(1,int(dt))
            # Barra max 166px, scala a 10KB/s
            max_rate = 10240
            bar_rx = min(DW-2, int((rate_rx / max_rate) * DW))
            bar_tx = min(DW-2, int((rate_tx / max_rate) * DW))
            if tft:
                tft.fill_rect(OX, y, DW, 6, DKBG)
                if bar_rx > 0: tft.fill_rect(OX, y, bar_rx, 5, CYAN)
                tft.fill_rect(OX, y+6, DW, 6, DKBG)
                if bar_tx > 0: tft.fill_rect(OX, y+6, bar_tx, 5, ORANGE)
            y += 15

    hl(y); y+=5

    # Uptime + IP C6 + WebUI + IP server
    t(0, y, trunc(s.get("uptime","?"), CPL), DIM); y+=CH+2
    t(0, y, f"C6: {get_ip()}", DIM);               y+=CH+2
    t(0, y, "WebUI port: 80", DIM);                y+=CH+2
    t(0, y, f"SRV:{cfg['server_ip']}", DIM)

# ── Pagina 1: INTERFACCE ──────────────────────────────────────────────────

def draw_page_interfaces():
    clear()
    header("Interfaces", 2, TEAL, WHITE)
    if not data:
        tc(160, "no data", WARN); return

    ifaces = data.get("interfaces", [])
    if not ifaces:
        tc(160, "nessuna", DIM); return

    # Mappa tipo → label corta
    TYPE_LABELS = {
        "LocalClientInterface":  "LOCAL",
        "LocalServerInterface":  "LOCAL",
        "TCPClientInterface":    "TCP",
        "TCPServerInterface":    "TCPSRV",
        "UDPInterface":          "UDP",
        "AutoInterface":         "AUTO",
        "I2PInterface":          "I2P",
        "RNodeInterface":        "RNODE",
        "AX25KISSInterface":     "AX.25",
        "BackboneInterface":     "BB",
    }

    y = CH*2 + 8
    for iface in ifaces[:6]:
        online = iface.get("online", True)
        name   = iface.get("name","?")
        itype  = iface.get("type","")
        label  = TYPE_LABELS.get(itype, itype[:6] if itype else "?")

        dot(0, y+3, online, 10)
        # Label tipo colorata
        t(14, y, f"[{label}]", CYAN if online else DIM, BG)
        y += CH+1
        # Nome troncato
        t(4, y, trunc(clean_name(name), CPL-1), FG if online else DIM, BG)
        y += CH+1

        # RX/TX compatto
        def fmt(b): return f"{b//1024}K" if b>=1024 else f"{b}B"
        rxb=iface.get("rxb",0); txb=iface.get("txb",0)
        trow(y, f"R:{fmt(rxb)}", f"T:{fmt(txb)}", DIM, DIM)
        y += CH+3
        hl(y); y+=4
        if y > DH-CH-10: break

    t(0, DH-CH-2, f"Tot:{len(ifaces)}", DIM)

# ── Pagina 2: ANNUNCI ────────────────────────────────────────────────────

def draw_page_announces():
    clear()
    header("Announces", 3, DKGREEN, WHITE)
    if not data:
        tc(160, "no data", WARN); return

    ann_data = data.get("announces",{})
    recent   = ann_data.get("recent_5m",0)
    ann_list = ann_data.get("list",[])
    s        = data.get("status",{})

    y = CH*2 + 8
    t(0,y,"5min",DIM); tr(y,str(recent),ACCENT); y+=CH+2
    t(0,y,"Known",DIM); tr(y,str(s.get("known_dests",0))); y+=CH+2
    t(0,y,"Paths",DIM); tr(y,str(s.get("paths",0)));       y+=CH+5
    hl(y); y+=5

    for ann in ann_list[:6]:
        h    = ann.get("hash","")[:12]
        name = ann.get("display_name","") or clean_name(ann.get("app_data",""))
        ts   = ann.get("ts_human","")

        # Hash corto + timestamp a destra
        t(0, y, h, CYAN)
        tr(y, ts, DIM)
        y += CH+1
        # Nome peer pulito
        name_clean = trunc(name, CPL-1) if name else "(anon)"
        t(2, y, name_clean, FG if name else DIM)
        y += CH+3
        if y > DH-CH-10: break

# ── Pagina 3: LXMF ───────────────────────────────────────────────────────

def draw_page_lxmf():
    clear()
    unread = data.get("lxmf",{}).get("unread",0) if data else 0
    hc = YELLOW if unread else DKBLUE
    fc = BLACK if unread else WHITE
    header(f"LXMF ({unread} new)" if unread else "LXMF Inbox", 4, hc, fc)

    if not data:
        tc(160, "no data", WARN); return

    msgs = data.get("lxmf",{}).get("messages",[])
    if not msgs:
        tc(160, "Inbox vuota", DIM); return

    y = CH*2+8
    for msg in msgs[:4]:
        dn   = msg.get("display_name","")
        src  = dn[:16] if dn else msg.get("hash","")[:12]
        body = msg.get("content","")
        ts   = msg.get("ts_human","")
        read = msg.get("read",True)

        if not read:
            box(0, y, 3, CH*3+4, YELLOW)

        t(5,y,src,ACCENT); tr(y,ts,DIM)
        y+=CH+1
        body_clean="".join(c if 32<=ord(c)<127 else " " for c in body)
        t(5,y,trunc(body_clean,CPL-1),FG)
        y+=CH
        if len(body_clean)>CPL-1:
            t(5,y,trunc(body_clean[CPL-1:(CPL-1)*2],CPL-1),FG)
        y+=CH+3
        hl(y); y+=4
        if y>DH-CH-10: break

# ── Pagina 4: IDENTITÀ ───────────────────────────────────────────────────

def draw_page_identity():
    clear()
    header("Identity", 5, PURPLE, WHITE)
    if not data:
        tc(160, "no data", WARN); return

    identity = data.get("identity",{})
    s        = data.get("status",{})
    y = CH*2+8

    # Nome nodo
    name = identity.get("name") or cfg.get("node_name","?")
    tc(y, trunc(name,CPL), ACCENT, BG); y+=CH+5
    hl(y); y+=5

    # RNS Hash su 2 righe
    t(0,y,"RNS Hash:",DIM); y+=CH+2
    h = identity.get("hash","").strip("<>").replace(" ","")
    if len(h)>=8:
        t(0,y,h[:16],FG); y+=CH+1
        t(0,y,h[16:32],FG); y+=CH+1
        if len(h)>32: t(0,y,h[32:],FG)
        y+=CH+4
    else:
        t(0,y,"(attendi announce)",DIM); y+=CH+4
    hl(y); y+=5

    # LXMF Hash
    t(0,y,"LXMF Hash:",DIM); y+=CH+2
    lh = identity.get("lxmf_hash","").strip("<>").replace(" ","")
    if len(lh)>=8:
        t(0,y,lh[:16],CYAN); y+=CH+1
        t(0,y,lh[16:],CYAN); y+=CH+4
    else:
        t(0,y,"(non disponibile)",DIM); y+=CH+4
    hl(y); y+=5

    t(0,y,trunc(s.get("uptime","?"),CPL),DIM)

# ── Dispatcher ────────────────────────────────────────────────────────────

def draw_page_rnstatus():
    clear()
    header("RNS Status", 6, 0x3186, WHITE)
    if not data:
        tc(160, "no data", WARN); return

    rns_data = data.get("rnstatus", {})
    ifaces   = rns_data.get("ifaces", [])
    ts       = rns_data.get("ts", 0)

    y = CH*2 + 8

    if not ifaces:
        tc(120, "rnstatus", DIM)
        tc(140, "nessun dato", DIM)
        tc(160, "attendi 30s...", DIM)
        return

    # Se il primo elemento sembra un errore, mostralo
    first = ifaces[0].get("name","")
    if "error" in first.lower() or "errore" in first.lower() or "File" in first:
        tc(100, "rnstatus", WARN)
        tc(120, "non disponibile", DIM)
        tc(150, "su questo OS", DIM)
        t(0, DH-CH-2, "vedi pag.2 iface", DIM)
        return

    # Mostra ogni interfaccia
    for iface in ifaces[:7]:
        name   = iface.get("name","?")
        status = iface.get("status","")
        rx     = iface.get("rx","")
        tx     = iface.get("tx","")
        online = "up" in status.lower() or "online" in status.lower() or not status

        dot(0, y+3, online, 8)
        t(12, y, trunc(name, CPL-2), FG if online else DIM)
        y += CH+1
        if rx or tx:
            trow(y, rx[:9], tx[:9], DIM, DIM)
            y += CH+1
        hl(y); y += 3
        if y > DH-CH-10: break

    # Conteggio interfacce trovate
    t(0, DH-CH-2, f"{len(ifaces)} interfacce", DIM)


def draw_page_traffic():
    """Pagina 7: grafico traffico storico RX/TX."""
    clear()
    header("Traffic", 7, 0x4A0F, WHITE)
    if not data:
        tc(160, "no data", WARN); return
    y = CH*2 + 8
    th = traffic_history
    if len(th) < 2:
        tc(110, "raccolta dati", DIM)
        tc(130, f"campioni: {len(th)}", DIM)
        tc(150, "attendi ~30s", DIM); return
    rates_rx=[]; rates_tx=[]
    for i in range(1,len(th)):
        dt = th[i].get("ts",0)-th[i-1].get("ts",1)
        if dt>0:
            rates_rx.append(max(0,(th[i].get("rx",0)-th[i-1].get("rx",0))//int(dt)))
            rates_tx.append(max(0,(th[i].get("tx",0)-th[i-1].get("tx",0))//int(dt)))
    if not rates_rx:
        tc(120,"...",DIM); return
    peak = max(max(rates_rx),max(rates_tx),1)
    def fmt(b):
        return f"{b//1024}K" if b>=1024 else f"{b}B"
    trow(y,"Peak", fmt(peak)+"/s", DIM, ACCENT); y+=CH+5

    # Grafico RX con asse e griglia
    t(0,y,"RX",CYAN)
    tr(y, fmt(rates_rx[-1])+"/s", CYAN); y+=CH+2
    gh=46
    n=min(len(rates_rx), (DW-2)//3)
    # Linea base
    if tft: tft.hline(OX, y+gh, DW, DKBG)
    for i in range(n):
        v=rates_rx[-(n-i)]
        bh=max(1,int((v/peak)*gh)) if v>0 else 0
        if tft and bh>0:
            tft.fill_rect(OX+i*3, y+gh-bh, 2, bh, CYAN)
    y+=gh+8

    # Grafico TX
    t(0,y,"TX",ORANGE)
    tr(y, fmt(rates_tx[-1])+"/s", ORANGE); y+=CH+2
    if tft: tft.hline(OX, y+gh, DW, DKBG)
    for i in range(n):
        v=rates_tx[-(n-i)]
        bh=max(1,int((v/peak)*gh)) if v>0 else 0
        if tft and bh>0:
            tft.fill_rect(OX+i*3, y+gh-bh, 2, bh, ORANGE)
    y+=gh+6
    t(0,y,f"{n} campioni", DIM)


def draw_page_peers():
    """Pagina 8: lista peer/nomi noti."""
    clear()
    header("Peers", 8, 0x0A4A, WHITE)
    if not data:
        tc(160, "no data", WARN); return
    y = CH*2 + 8
    names = data.get("names", {})
    if not names:
        tc(120,"nessun peer",DIM); return
    trow(y, "Noti", str(len(names)), DIM, ACCENT); y+=CH+4
    hl(y); y+=4
    for h,name in list(names.items())[:11]:
        label = name if name else h[:10]
        t(0,y,trunc(label, CPL),FG); y+=CH+1
        if y>DH-CH-4: break


def draw_page_events():
    """Pagina 9: log eventi cronologico."""
    clear()
    header("Events", 9, 0x4A00, WHITE)
    if not data:
        tc(160, "no data", WARN); return
    y = CH*2 + 8
    if not events_cache:
        tc(120,"nessun evento",DIM)
        tc(140,"si popola da solo",DIM); return
    for ev in events_cache[:12]:
        typ = ev.get("type","")
        mark = "M" if typ=="lxmf" else ">"
        col = YELLOW if typ=="lxmf" else CYAN
        nm = trunc(ev.get("name",""), CPL-3)
        t(0,y,mark,col)
        t(16,y,nm,FG)
        y+=CH+1
        if y>DH-CH-4: break


def draw_page_rmap():
    """Pagina 10: nodi rmap.world per tipo + mappa densita."""
    clear()
    header("RMAP Nodes", 10, 0x6200, WHITE)
    if not data:
        tc(160, "no data", WARN); return
    y = CH*2 + 8
    rmap = data.get("rmap", {})
    counts = rmap.get("counts", {})
    total  = rmap.get("total", 0)
    unique = rmap.get("unique", 0)
    views  = rmap.get("views", 0)

    if not counts:
        tc(110,"rmap.world",DIM)
        tc(130,"nessun dato",DIM)
        tc(150,"attendi fetch",DIM); return

    # Label tipi compatte
    LBL = {"RNode_LoRa":"LoRa","Backbone":"Backbone","I2P":"I2P",
           "TCP_Client":"TCP","AX25_KISS":"AX25","KISS":"KISS",
           "UDP":"UDP","Unknown":"Other"}
    # Colori per tipo
    COL = {"RNode_LoRa":0x051F,"Backbone":0xF800,"I2P":0x780F,
           "TCP_Client":0x8410,"Unknown":0x4208}

    # Tot = interfacce totali, Nodi = nodi fisici unici (per identita)
    trow(y, "Interf.", str(total), DIM, ACCENT); y+=CH+1
    trow(y, "Nodi", str(unique), DIM, FG); y+=CH+1
    if views:
        trow(y, "Visite", f"{views:,}".replace(",","."), DIM, CYAN); y+=CH+1
    y+=2
    hl(y); y+=3

    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    maxc = items[0][1] if items else 1

    for ntype, cnt in items[:6]:
        label = LBL.get(ntype, trunc(ntype, 8))
        col = COL.get(ntype, 0x6200)
        t(0,y,label,FG)
        tr(y,str(cnt),CYAN)
        y+=CH
        bw = int((cnt/maxc)*(DW-4))
        if tft and bw>0:
            tft.fill_rect(OX, y, bw, 4, col)
        y+=6
        if y>DH-CH*2-12: break

    # Footer: fonte dati su 2 righe in basso
    t(0, DH-CH*2, "fetched from:", DIM)
    t(0, DH-CH, "https://rmap.world", DIM)


# Segmenti per cifre 0-9 (7-segment): [top,tl,tr,mid,bl,br,bot]
SEG_MAP = {
    "0":[1,1,1,0,1,1,1], "1":[0,0,1,0,0,1,0], "2":[1,0,1,1,1,0,1],
    "3":[1,0,1,1,0,1,1], "4":[0,1,1,1,0,1,0], "5":[1,1,0,1,0,1,1],
    "6":[1,1,0,1,1,1,1], "7":[1,0,1,0,0,1,0], "8":[1,1,1,1,1,1,1],
    "9":[1,1,1,1,0,1,1],
}

def draw_digit(x, y, ch, color, w=18, h=34, th=4):
    """Disegna una cifra 7-segmenti a (x,y) con offset OX."""
    if ch not in SEG_MAP:
        return
    seg = SEG_MAP[ch]
    px = OX + x
    # top
    if seg[0]: tft.fill_rect(px+th, y, w-2*th, th, color)
    # top-left
    if seg[1]: tft.fill_rect(px, y+th, th, (h-3*th)//2, color)
    # top-right
    if seg[2]: tft.fill_rect(px+w-th, y+th, th, (h-3*th)//2, color)
    # mid
    if seg[3]: tft.fill_rect(px+th, y+(h-th)//2, w-2*th, th, color)
    # bottom-left
    if seg[4]: tft.fill_rect(px, y+(h+th)//2, th, (h-3*th)//2, color)
    # bottom-right
    if seg[5]: tft.fill_rect(px+w-th, y+(h+th)//2, th, (h-3*th)//2, color)
    # bottom
    if seg[6]: tft.fill_rect(px+th, y+h-th, w-2*th, th, color)

def draw_colon(x, y, color, h=34):
    px = OX + x
    tft.fill_rect(px, y+h//3, 4, 4, color)
    tft.fill_rect(px, y+2*h//3, 4, 4, color)

def draw_big_clock(y, time_str, color):
    """Disegna HH:MM:SS centrato con cifre 7-segmenti."""
    if not tft: return
    # Layout: 6 cifre + 2 due punti
    # cifra=18px, colon=8px, gap=3px
    dw, cw, gap = 18, 8, 3
    total = 6*dw + 2*cw + 7*gap
    x = max(0, (DW - total)//2)
    for ch in time_str:
        if ch == ":":
            draw_colon(x, y, color)
            x += cw + gap
        else:
            draw_digit(x, y, ch, color)
            x += dw + gap

def draw_page_clock():
    """Pagina 11: orologio grande, sincronizzato dal server."""
    clear()
    header("Clock", 11, 0x001F, WHITE)
    if not data:
        tc(160, "no data", WARN); return

    # Calcola ora corrente: epoch server + secondi passati dal sync
    if clock_epoch > 0:
        elapsed = time.ticks_diff(time.ticks_ms(), clock_synced_at) // 1000
        now = clock_epoch + elapsed
        # Converti epoch in HH:MM:SS (UTC+offset gia incluso dal server)
        secs = now % 86400
        hh = secs // 3600
        mm = (secs % 3600) // 60
        ss = secs % 60
        time_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
    else:
        ck = data.get("clock", {})
        time_str = ck.get("time", "--:--:--")

    date_str = data.get("clock", {}).get("date", "")

    # Orologio grande a cifre 7-segmenti
    y = 100
    draw_big_clock(y, time_str, ACCENT)
    y += 50
    tc(y, date_str, FG)
    y += CH + 20
    hl(y); y += 10

    s = data.get("status", {})
    tc(y, "Uptime", DIM); y += CH + 2
    tc(y, trunc(s.get("uptime","?"), CPL), FG); y += CH + 12
    tc(y, cfg["node_name"], DIM)


def draw_page_settings():
    """Pagina 12: settings correnti + indirizzo WebUI."""
    clear()
    header("Settings", 12, 0x528A, WHITE)
    y = CH*2 + 8

    trow(y, "Node", trunc(cfg["node_name"],11), DIM, ACCENT); y+=CH+2
    trow(y, "Poll", f'{cfg["poll_interval"]}s', DIM, FG); y+=CH+2
    ar = cfg.get("auto_rotate",0)
    trow(y, "Rotate", f"{ar}s" if ar else "off", DIM, FG); y+=CH+2
    trow(y, "Light", f'{cfg["brightness"]}%', DIM, FG); y+=CH+4
    hl(y); y+=4

    t(0,y,"Server:",DIM); y+=CH
    t(2,y,f'{cfg["server_ip"]}:{cfg["server_port"]}',CYAN); y+=CH+4
    hl(y); y+=4

    t(0,y,"WiFi:",DIM); y+=CH
    t(2,y,trunc(cfg["wifi_ssid"],CPL-1),FG); y+=CH+4
    hl(y); y+=4

    # WebUI per modificare
    t(0,y,"WebUI config:",DIM); y+=CH
    t(2,y,f"http://{get_ip()}",ACCENT); y+=CH+4
    t(0,y,"BTN_B = reload cfg",DIM)


def draw_page_echo():
    """Pagina 13: log risposte echo - a chi abbiamo risposto."""
    clear()
    header("Echo Replies", 13, 0x07E0, BLACK)
    y = CH*2 + 8
    if not echo_cache:
        tc(120,"nessuna",DIM)
        tc(140,"risposta echo",DIM)
        tc(165,"si popola da solo",DIM); return
    t(0,y,f"Risposte: {len(echo_cache)}",ACCENT); y+=CH+3
    hl(y); y+=4
    for e in echo_cache[:9]:
        nm = trunc(e.get("name",""), CPL)
        t(0,y,nm,FG); y+=CH
        ct = e.get("content","")
        if ct:
            t(8,y,trunc('"'+ct+'"', CPL-1),DIM); y+=CH
        y+=2
        if y>DH-CH-4: break


def _wx_desc(code):
    """Descrizione breve da weather_code WMO."""
    c = code if code is not None else -1
    if c==0: return "Sereno"
    if c in (1,2): return "Poco nuv."
    if c==3: return "Nuvoloso"
    if c in (45,48): return "Nebbia"
    if 51<=c<=57: return "Pioviggine"
    if 61<=c<=67: return "Pioggia"
    if 71<=c<=77: return "Neve"
    if 80<=c<=82: return "Rovesci"
    if 95<=c<=99: return "Temporale"
    return "--"


def draw_barometer(cx, cy, r, pressure):
    """Disegna un barometro analogico: lancetta su scala 960-1060 hPa."""
    if not tft or pressure is None: return
    # Arco scala (semicerchio superiore) con tacche
    import math
    # range 960..1060 hPa mappato su 180gradi (da 180 a 360 gradi = sinistra->destra in alto)
    p = max(960, min(1060, pressure))
    frac = (p - 960) / 100.0  # 0..1
    angle = math.pi + frac * math.pi  # da pi (sx) a 2pi (dx)
    # Tacche ogni 20 hPa
    for i in range(6):
        a = math.pi + (i/5.0)*math.pi
        x1 = int(cx + (r-4)*math.cos(a)); y1 = int(cy + (r-4)*math.sin(a))
        x2 = int(cx + r*math.cos(a));     y2 = int(cy + r*math.sin(a))
        tft.line(x1,y1,x2,y2, DIM)
    # Lancetta
    hx = int(cx + (r-8)*math.cos(angle))
    hy = int(cy + (r-8)*math.sin(angle))
    tft.line(cx, cy, hx, hy, ACCENT)
    # Perno
    tft.fill_rect(cx-2, cy-2, 4, 4, WHITE)


def draw_page_weather():
    """Pagina 14: meteo con barometro digitale."""
    clear()
    header("Meteo", 14, 0x5D1F, WHITE)
    y = CH*2 + 6
    wx = data.get("weather", {}) if data else {}
    if not wx or wx.get("temp") is None:
        tc(120,"meteo",DIM)
        tc(140,"in caricamento",DIM)
        tc(165,"o citta errata",DIM); return

    city = wx.get("city","?")
    tc(y, trunc(city, CPL), ACCENT); y+=CH+2
    tc(y, _wx_desc(wx.get("code")), FG); y+=CH+4

    # Temperatura grande
    temp = wx.get("temp")
    if temp is not None:
        tc(y, f"{temp:.1f}C", WHITE); y+=CH+2
    feels = wx.get("feels")
    if feels is not None:
        tc(y, f"perc. {feels:.0f}C", DIM); y+=CH+4

    # Barometro
    press = wx.get("pressure")
    if press is not None:
        cx = OX + DW//2
        cy = y + 36
        draw_barometer(cx, cy, 34, press)
        y = cy + 42
        tc(y, f"{press:.0f} hPa", CYAN); y+=CH+3

    # Umidita + vento
    hum = wx.get("humidity"); wind = wx.get("wind")
    if hum is not None:
        trow(y, "Umidita", f"{hum:.0f}%", DIM, FG); y+=CH+1
    if wind is not None:
        trow(y, "Vento", f"{wind:.0f}km/h", DIM, FG); y+=CH+1
    t(0, DH-CH, "cambia citta: WebUI", DIM)


def draw_page_resources():
    """Pagina 15: risorse C6 - RAM, flash, temperatura interna."""
    clear()
    header("Risorse C6", 15, 0xFD20, BLACK)
    y = CH*2 + 8
    import gc
    gc.collect()
    free = gc.mem_free()
    alloc = gc.mem_alloc()
    total = free + alloc

    trow(y, "RAM tot", f"{total//1024}KB", DIM, ACCENT); y+=CH+2
    trow(y, "Usata", f"{alloc//1024}KB", DIM, ORANGE); y+=CH+2
    trow(y, "Libera", f"{free//1024}KB", DIM, ONLINE); y+=CH+3

    # Barra uso RAM
    pct = int((alloc/total)*100) if total else 0
    bw = int((alloc/total)*DW) if total else 0
    if tft:
        tft.fill_rect(OX, y, DW, 8, DKBG)
        col = RED if pct>80 else (WARN if pct>60 else ONLINE)
        if bw>0: tft.fill_rect(OX, y, bw, 8, col)
    y+=12
    tc(y, f"{pct}% in uso", DIM); y+=CH+4
    hl(y); y+=4

    # Flash filesystem
    try:
        import os
        st = os.statvfs("/")
        fs_total = st[0]*st[2]
        fs_free = st[0]*st[3]
        trow(y, "Flash", f"{fs_total//1024}KB", DIM, FG); y+=CH+1
        trow(y, "FS free", f"{fs_free//1024}KB", DIM, ONLINE); y+=CH+3
    except Exception:
        pass

    # Temperatura interna MCU (ESP32-C6)
    try:
        import esp32
        tc_temp = esp32.mcu_temperature()
        hl(y); y+=4
        trow(y, "MCU temp", f"{tc_temp}C", DIM, CYAN); y+=CH+1
    except Exception:
        try:
            # Fallback: alcuni port espongono via machine
            import machine
            if hasattr(machine, "ADC"):
                pass
        except Exception:
            pass

    # Frequenza CPU
    try:
        import machine
        mhz = machine.freq()//1000000
        trow(y, "CPU", f"{mhz}MHz", DIM, FG)
    except Exception:
        pass


def draw_page_host():
    """Pagina 16: risorse del PC host (server) via psutil."""
    clear()
    header("Server PC", 16, 0xF81F, WHITE)
    y = CH*2 + 8
    host = data.get("host", {}) if data else {}

    if not host or host.get("error"):
        tc(115,"host stats",DIM)
        tc(135,"non disponibili",DIM)
        tc(160,"pip install psutil",DIM); return

    # CPU
    cpu = host.get("cpu", 0)
    cores = host.get("cores", 0)
    lbl = f"CPU {cores}c" if cores else "CPU"
    trow(y, lbl, f"{cpu:.0f}%", DIM, ACCENT); y+=CH
    bw = int((cpu/100)*DW)
    if tft:
        tft.fill_rect(OX, y, DW, 7, DKBG)
        col = RED if cpu>80 else (WARN if cpu>50 else ONLINE)
        if bw>0: tft.fill_rect(OX, y, bw, 7, col)
    y+=11

    # RAM
    rp = host.get("ram_pct", 0)
    ru = host.get("ram_used", 0); rt = host.get("ram_total", 0)
    trow(y, "RAM", f"{ru//1024:.1f}/{rt//1024:.1f}G" if rt>1024 else f"{ru}/{rt}M", DIM, FG); y+=CH
    bw = int((rp/100)*DW)
    if tft:
        tft.fill_rect(OX, y, DW, 7, DKBG)
        col = RED if rp>85 else (WARN if rp>65 else ONLINE)
        if bw>0: tft.fill_rect(OX, y, bw, 7, col)
    y+=11

    # Disco
    dp = host.get("disk_pct", 0)
    du = host.get("disk_used", 0); dt = host.get("disk_total", 0)
    trow(y, "Disco", f"{du}/{dt}G", DIM, FG); y+=CH
    bw = int((dp/100)*DW)
    if tft:
        tft.fill_rect(OX, y, DW, 7, DKBG)
        col = RED if dp>90 else (WARN if dp>75 else ONLINE)
        if bw>0: tft.fill_rect(OX, y, bw, 7, col)
    y+=13
    hl(y); y+=4

    # Extra: temp CPU + freq
    ct = host.get("cpu_temp")
    if ct is not None:
        trow(y, "CPU temp", f"{ct}C", DIM, CYAN); y+=CH+1
    mhz = host.get("cpu_mhz")
    if mhz:
        trow(y, "CPU freq", f"{mhz}MHz", DIM, FG); y+=CH+1
    tc(DH-CH, f"server {cfg['server_ip']}", DIM)


def draw_page_about():
    """Pagina 17: info rCompanion + versione + aggiornamenti."""
    clear()
    header("About", 17, 0x07FF, BLACK)
    y = CH*2 + 10

    tc(y, "rCompanion", ACCENT); y+=CH+2
    tc(y, f"v{VERSION}", WHITE); y+=CH+8

    # Info breve
    tc(y, "Companion display", DIM); y+=CH
    tc(y, "per nodo Reticulum", DIM); y+=CH
    tc(y, "su ESP32-C6", DIM); y+=CH+10
    hl(y); y+=6

    # Controllo versione
    ver = data.get("version", {}) if data else {}
    latest = ver.get("latest")
    update = ver.get("update", False)

    if latest:
        trow(y, "Locale", f"v{VERSION}", DIM, FG); y+=CH+1
        trow(y, "Online", f"v{latest}", DIM, FG); y+=CH+3
        if update:
            tc(y, "AGGIORNAMENTO!", WARN); y+=CH
            tc(y, "disponibile", WARN); y+=CH+2
        else:
            tc(y, "aggiornato", ONLINE); y+=CH+2
    else:
        trow(y, "Locale", f"v{VERSION}", DIM, FG); y+=CH+1
        tc(y, "online: n/d", DIM); y+=CH+2

    # Footer github
    t(0, DH-CH*2, "github.com/", DIM)
    t(0, DH-CH, "fr33n0w/rcompanion", DIM)


def draw_pie(cx, cy, r, slices):
    """Disegna una torta. slices = [(valore, colore), ...]. Un solo passaggio."""
    if not tft: return
    import math
    tot = sum(s[0] for s in slices)
    if tot <= 0: return
    # Angoli cumulativi (parte da -90 gradi = alto)
    bounds = []
    a = -math.pi/2
    for val, col in slices:
        if val <= 0: continue
        a2 = a + (val/tot)*2*math.pi
        bounds.append((a, a2, col))
        a = a2
    r2 = r*r
    for dy in range(-r, r+1):
        for dx in range(-r, r+1):
            if dx*dx+dy*dy > r2:
                continue
            ang = math.atan2(dy, dx)
            # normalizza in [-pi/2, 3pi/2)
            if ang < -math.pi/2:
                ang += 2*math.pi
            for a0, a1, col in bounds:
                if a0 <= ang < a1:
                    tft.pixel(cx+dx, cy+dy, col)
                    break

def draw_page_annstats():
    """Pagina 18: grafico annunci/ora + torta per tipo."""
    clear()
    header("Announce Stats", 18, 0xFBE0, BLACK)
    y = CH*2 + 6
    ast = data.get("annstats", {}) if data else {}
    types = ast.get("types", {})
    hourly = ast.get("hourly", [])
    total = ast.get("total", 0)

    if not types and not hourly:
        tc(140,"raccolta dati...",DIM); return

    trow(y, "Totale", str(total), DIM, ACCENT); y+=CH+4

    # Grafico a barre orario (ultime 12h)
    t(0,y,"Annunci/ora (12h)",FG); y+=CH+2
    gh=38
    if hourly:
        mx=max(hourly+[1])
        bw=10; gap=2
        n=min(len(hourly),12)
        if tft: tft.hline(OX, y+gh, n*(bw+gap), DIM)
        for i in range(n):
            v=hourly[i]
            bh=max(1,int((v/mx)*gh)) if v>0 else 0
            if tft and bh>0:
                tft.fill_rect(OX+i*(bw+gap), y+gh-bh, bw, bh, CYAN)
        y+=gh+8

    # Torta per tipo
    t(0,y,"Per tipo:",FG); y+=CH+2
    lxmf=types.get("LXMF",0); nomad=types.get("NomadNet",0); other=types.get("Other",0)
    tot=lxmf+nomad+other
    cx=OX+36; cy=y+36; r=30
    draw_pie(cx, cy, r, [(lxmf,0x051F),(nomad,0x07E0),(other,0xFD20)])
    # Legenda
    leg=[("LXMF",lxmf,0x051F),("NomadNet",nomad,0x07E0),("Other",other,0xFD20)]
    lx=82; ly=y+10
    for name,val,col in leg:
        pct=int((val/tot)*100) if tot else 0
        if tft: tft.fill_rect(OX+lx,ly+3,8,8,col)
        t(lx+12,ly,f"{name} {pct}%",FG)
        ly+=CH+4


# ── Screensaver ────────────────────────────────────────────────────────────
ss_mode=0; ss_x=80; ss_y=120; ss_dx=2; ss_dy=2
ss_frame=0; ss_ang=0.0; ss_stars=[]; ss_active=False
SS_X0=OX+2; SS_X1=OX+DW-2; SS_Y0=CH*2+8; SS_Y1=DH-4  # area sotto header

def ss_robot(x, y, blink, col=0x07FF):
    """Disegna un robottino blocky a (x,y) assoluti."""
    if not tft: return
    # antenna
    tft.fill_rect(x+13, y, 2, 5, DIM)
    tft.fill_rect(x+11, y-3, 5, 4, RED)
    # testa
    tft.fill_rect(x+4, y+5, 18, 13, col)
    # occhi (spenti se blink)
    ec = BG if blink else WHITE
    tft.fill_rect(x+8, y+9, 3, 4, ec)
    tft.fill_rect(x+15, y+9, 3, 4, ec)
    # bocca
    tft.fill_rect(x+8, y+15, 10, 2, DKGRAY)
    # corpo
    tft.fill_rect(x+2, y+20, 22, 13, col)
    tft.fill_rect(x+9, y+24, 8, 5, DKGRAY)  # pannello
    # braccia
    tft.fill_rect(x-2, y+21, 4, 9, col)
    tft.fill_rect(x+24, y+21, 4, 9, col)
    # gambe
    tft.fill_rect(x+5, y+33, 5, 4, DIM)
    tft.fill_rect(x+16, y+33, 5, 4, DIM)

ROBOT_W=30; ROBOT_H=42

def ss_rns(cx, cy, ang, col=0x07E0):
    """Logo RNS stile rete hub-and-spoke rotante."""
    if not tft: return
    import math
    R=22; N=6
    for i in range(N):
        a = ang + i*(2*math.pi/N)
        ox = int(cx + R*math.cos(a)); oy = int(cy + R*math.sin(a))
        tft.line(cx, cy, ox, oy, DIM)
        tft.fill_rect(ox-3, oy-3, 6, 6, col)
    # hub centrale
    tft.fill_rect(cx-4, cy-4, 8, 8, CYAN)
RNS_R=28

def ss_clear_box(x, y, w, h):
    if tft: tft.fill_rect(max(OX,x), max(0,y), w, h, BG)

def draw_page_screensaver():
    """Pagina 19: placeholder - l'animazione e' gestita da screensaver_loop."""
    # Non disegna nulla: il task screensaver_loop possiede lo schermo
    pass


async def screensaver_loop():
    """Anima lo screensaver quando si e' sulla pagina dedicata."""
    global ss_mode, ss_x, ss_y, ss_dx, ss_dy, ss_frame, ss_ang, ss_stars, ss_active
    try:
        import urandom as _rnd
    except ImportError:
        import random as _rnd
    def _rb(n): 
        try: return _rnd.getrandbits(n)
        except: return _rnd.randint(0, (1<<n)-1)
    SS_IDX = NUM_PAGES - 1  # ultima pagina
    last_on = False
    px, py = ss_x, ss_y  # posizione precedente per cancellare
    while True:
        try:
            on = (current_page == SS_IDX)
            if not on:
                if last_on:
                    last_on = False
                    draw_current_page()
                await asyncio.sleep_ms(200)
                continue
            if not last_on:
                clear()
                gc.collect()
                header("Screensaver", NUM_PAGES, 0x781F, WHITE)
                ss_frame = 0; ss_mode = 0
                ss_x, ss_y = (SS_X0+SS_X1)//2, (SS_Y0+SS_Y1)//2
                ss_dx, ss_dy = 2, 2; ss_ang = 0.0
                ss_stars = [[_rb(8)%DW + OX, SS_Y0 + _rb(8)%(DH-SS_Y0), 1+_rb(2)] for _ in range(30)]
                px, py = ss_x, ss_y
            last_on = True
            ss_frame += 1
            mode = (ss_frame // 140) % 4
            if mode != ss_mode:
                ss_mode = mode
                clear()
                header("Screensaver", NUM_PAGES, 0x781F, WHITE)
                ss_x, ss_y = (SS_X0+SS_X1)//2, (SS_Y0+SS_Y1)//2
                px, py = ss_x, ss_y

            if ss_mode == 0:
                ss_clear_box(px-3, py-4, ROBOT_W+6, ROBOT_H+6)
                ss_x += ss_dx; ss_y += ss_dy
                if ss_x <= SS_X0: ss_x=SS_X0; ss_dx=abs(ss_dx)
                if ss_x+ROBOT_W >= SS_X1: ss_x=SS_X1-ROBOT_W; ss_dx=-abs(ss_dx)
                if ss_y <= SS_Y0+3: ss_y=SS_Y0+3; ss_dy=abs(ss_dy)
                if ss_y+ROBOT_H >= SS_Y1: ss_y=SS_Y1-ROBOT_H; ss_dy=-abs(ss_dy)
                blink = (ss_frame % 30) < 3
                cols=[0x07FF,0x07E0,0xFD20,0xF81F,0xFFE0]
                ss_robot(ss_x, ss_y, blink, cols[(ss_frame//40)%len(cols)])
                px, py = ss_x, ss_y
            elif ss_mode == 1:
                ss_clear_box(px-RNS_R-2, py-RNS_R-2, RNS_R*2+4, RNS_R*2+4)
                ss_x += ss_dx; ss_y += ss_dy
                if ss_x-RNS_R <= SS_X0: ss_x=SS_X0+RNS_R; ss_dx=abs(ss_dx)
                if ss_x+RNS_R >= SS_X1: ss_x=SS_X1-RNS_R; ss_dx=-abs(ss_dx)
                if ss_y-RNS_R <= SS_Y0: ss_y=SS_Y0+RNS_R; ss_dy=abs(ss_dy)
                if ss_y+RNS_R >= SS_Y1: ss_y=SS_Y1-RNS_R; ss_dy=-abs(ss_dy)
                ss_ang += 0.15
                ss_rns(ss_x, ss_y, ss_ang)
                px, py = ss_x, ss_y
                tc(DH-CH-4, "Reticulum", DIM)
            elif ss_mode == 2:
                for s in ss_stars:
                    if tft: tft.pixel(s[0], s[1], BG)
                    s[1] += s[2]
                    if s[1] >= SS_Y1:
                        s[0] = OX + (_rb(8) % DW)
                        s[1] = SS_Y0
                        s[2] = 1 + (_rb(2))
                    c = WHITE if s[2]>=3 else (CYAN if s[2]==2 else DIM)
                    if tft: tft.pixel(s[0], s[1], c)
            else:
                ss_clear_box(px, py, 11*CW, CH)
                ss_x += ss_dx; ss_y += ss_dy
                msg = "rCompanion"
                tw = len(msg)*CW
                if ss_x <= SS_X0: ss_x=SS_X0; ss_dx=abs(ss_dx)
                if ss_x+tw >= SS_X1: ss_x=SS_X1-tw; ss_dx=-abs(ss_dx)
                if ss_y <= SS_Y0: ss_y=SS_Y0; ss_dy=abs(ss_dy)
                if ss_y+CH >= SS_Y1: ss_y=SS_Y1-CH; ss_dy=-abs(ss_dy)
                cols=[0x07FF,0x07E0,0xFD20,0xF81F]
                if tft: tft.text(FN, msg, ss_x, ss_y, cols[(ss_frame//35)%len(cols)], BG)
                px, py = ss_x, ss_y

            await asyncio.sleep_ms(60)
        except Exception as e:
            print("SCREENSAVER ERR:", e)
            await asyncio.sleep_ms(300)


wifi_scan_cache=[]; wifi_scan_ts=0
bt_scan_cache=[]; bt_scan_ts=0

def draw_page_wifiscan():
    """Pagina WiFi scanner: mostra le reti trovate."""
    global wifi_scan_cache, wifi_scan_ts
    clear()
    header("WiFi Scan", 20, 0x051F, WHITE)
    y = CH*2 + 6
    # Scansiona al massimo ogni 8s (blocca ~1-2s)
    now = time.ticks_ms()
    if time.ticks_diff(now, wifi_scan_ts) > 8000 or not wifi_scan_cache:
        tc(150, "scansione...", DIM)
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
            if not wlan.active(): wlan.active(True)
            nets = wlan.scan()  # (ssid,bssid,ch,rssi,sec,hidden)
            nets.sort(key=lambda n: n[3], reverse=True)
            wifi_scan_cache = nets[:12]
            wifi_scan_ts = now
        except Exception as e:
            tc(170, "scan err", RED); return
        clear(); header("WiFi Scan", 20, 0x051F, WHITE); y = CH*2 + 6

    t(0, y, f"Reti: {len(wifi_scan_cache)}", ACCENT); y+=CH+3
    hl(y); y+=3
    SEC={0:"open",1:"WEP",2:"WPA",3:"WPA2",4:"WPA/2",5:"WPA3"}
    for n in wifi_scan_cache:
        try:
            ssid = n[0].decode("utf-8","replace") if isinstance(n[0],(bytes,bytearray)) else str(n[0])
        except: ssid="?"
        if not ssid: ssid="<hidden>"
        rssi = n[3]; ch = n[2]
        # colore per potenza segnale
        sc = ONLINE if rssi>-60 else (WARN if rssi>-75 else RED)
        t(0, y, trunc(ssid, 13), FG)
        tr(y, f"{rssi}", sc)
        y+=CH
        t(4, y, f"ch{ch} {SEC.get(n[4],'?')}", DIM)
        y+=CH+2
        if y > DH-CH: break

def draw_page_btscan():
    """Pagina Bluetooth scanner: dispositivi BLE trovati."""
    clear()
    header("Bluetooth Scan", 21, 0x781F, WHITE)
    y = CH*2 + 6
    if not bt_scan_cache:
        tc(130, "scansione BLE", DIM)
        tc(150, "in corso...", DIM)
        tc(175, "attendi ~5s", DIM)
        return
    t(0, y, f"Dispositivi: {len(bt_scan_cache)}", ACCENT); y+=CH+3
    hl(y); y+=3
    for dev in bt_scan_cache[:11]:
        name = dev.get("name") or dev.get("addr","?")
        rssi = dev.get("rssi", 0)
        sc = ONLINE if rssi>-60 else (WARN if rssi>-80 else RED)
        t(0, y, trunc(name, 14), FG)
        tr(y, f"{rssi}", sc)
        y+=CH+3
        if y > DH-CH: break

def draw_page_power():
    """Pagina consumo elettrico (stima)."""
    clear()
    header("Consumo (stima)", 22, 0xFD20, BLACK)
    y = CH*2 + 8
    # Stima: base MCU + WiFi + display backlight
    base = 45      # mA MCU attivo
    wifi = 60 if wifi_ok else 0
    bright = cfg.get("brightness", 80)
    disp = int(15 + (bright/100)*35)   # 15-50mA per backlight
    total_ma = base + wifi + disp
    mw = total_ma * 5  # a 5V USB

    trow(y, "MCU", f"~{base}mA", DIM, FG); y+=CH+2
    trow(y, "WiFi", f"~{wifi}mA", DIM, FG); y+=CH+2
    trow(y, "Display", f"~{disp}mA", DIM, FG); y+=CH+3
    hl(y); y+=4
    trow(y, "Totale", f"~{total_ma}mA", DIM, ACCENT); y+=CH+2
    trow(y, "Potenza", f"~{mw}mW", DIM, CYAN); y+=CH+2
    trow(y, "@5V USB", f"~{mw/1000:.2f}W", DIM, FG); y+=CH+4
    hl(y); y+=4
    # Barra visiva consumo (scala 0-200mA)
    pct = min(100, int((total_ma/200)*100))
    if tft:
        tft.fill_rect(OX, y, DW, 8, DKBG)
        c = RED if total_ma>150 else (WARN if total_ma>100 else ONLINE)
        tft.fill_rect(OX, y, int((pct/100)*DW), 8, c)
    y+=14
    t(0, y, "Stima senza sensore", DIM); y+=CH
    t(0, y, "HW. Indicativo.", DIM)


def draw_page_snake():
    """Pagina Snake: il gioco e' gestito da snake_loop (placeholder)."""
    pass

def draw_page_pong():
    """Pagina Pong: gestita da pong_loop (placeholder)."""
    pass

def draw_page_invaders():
    """Pagina Invaders: gestita da invaders_loop (placeholder)."""
    pass

def draw_page_breakout():
    """Pagina Breakout: gestita da breakout_loop (placeholder)."""
    pass


PAGE_DRAW=[draw_page_overview,draw_page_interfaces,draw_page_announces,
           draw_page_lxmf,draw_page_identity,draw_page_rnstatus,
           draw_page_traffic,draw_page_peers,draw_page_events,draw_page_rmap,
           draw_page_clock,draw_page_settings,draw_page_echo,
           draw_page_weather,draw_page_resources,draw_page_host,
           draw_page_about,draw_page_annstats,
           draw_page_wifiscan,draw_page_btscan,draw_page_power,
           draw_page_snake,draw_page_pong,draw_page_invaders,
           draw_page_breakout,draw_page_screensaver]

# ── Giochi ─────────────────────────────────────────────────────────────────
# Indici pagine gioco (devono combaciare col dispatcher sopra)
SNAKE_IDX = 21
PONG_IDX = 22
INVADERS_IDX = 23
BREAKOUT_IDX = 24
GAME_PAGES = (SNAKE_IDX, PONG_IDX, INVADERS_IDX, BREAKOUT_IDX)
game_running = False       # True quando un gioco e' attivo (button routing al gioco)
snake_btn = None           # tap del pulsante A (segnale azione per i giochi)

def is_game_page(idx):
    return idx in GAME_PAGES

async def snake_loop():
    """Gioco Snake. Avvio automatico entrando nella pagina, uscita tenendo A+B 2s."""
    global game_running, snake_btn
    import urandom as _rnd
    def _rb(n):
        try: return _rnd.getrandbits(n)
        except: return 0
    GRID=8  # dimensione cella px
    armed=True  # pronto ad avviare una partita al prossimo ingresso
    while True:
      try:
        if current_page != SNAKE_IDX:
            armed=True   # ri-arma: rientrando ripartira (non toccare game_running condiviso)
            await asyncio.sleep_ms(150)
            continue
        # Siamo sulla pagina Snake
        if not armed:
            # Uscito manualmente, in attesa di lasciare la pagina
            await asyncio.sleep_ms(150)
            continue
        # Avvia partita
        armed=False; game_running=True
        gx0=OX+2; gy0=CH*2+8; gx1=OX+DW-2; gy1=DH-CH-4
        cols=(gx1-gx0)//GRID; rows=(gy1-gy0)//GRID
        snake=[(cols//2, rows//2)]
        d=(1,0)
        food=(_rb(8)%cols, _rb(8)%rows)
        score=0; dead=False
        clear(); header("Snake", 22, 0x07E0, BLACK)
        tc(DH-CH-2, "A=gira  A(1.5s)=esci", DIM)
        snake_btn=None
        def cell(cx,cy,col):
            if tft: tft.fill_rect(gx0+cx*GRID, gy0+cy*GRID, GRID-1, GRID-1, col)
        cell(food[0],food[1],RED)
        cell(snake[0][0],snake[0][1],ONLINE)
        speed=220
        while current_page==SNAKE_IDX and game_running:
            if snake_btn=='L':
                d=(d[1], -d[0]); snake_btn=None
            elif snake_btn=='R':
                d=(-d[1], d[0]); snake_btn=None
            if not dead:
                head=(snake[0][0]+d[0], snake[0][1]+d[1])
                if head[0]<0 or head[0]>=cols or head[1]<0 or head[1]>=rows or head in snake:
                    dead=True
                    tc(gy0+rows*GRID//2, "GAME OVER", RED)
                    tc(gy0+rows*GRID//2+CH, f"Score {score}", WHITE)
                else:
                    snake.insert(0, head)
                    cell(head[0],head[1],ONLINE)
                    if head==food:
                        score+=1
                        while True:
                            food=(_rb(8)%cols, _rb(8)%rows)
                            if food not in snake: break
                        cell(food[0],food[1],RED)
                        speed=max(90, speed-4)
                    else:
                        tail=snake.pop()
                        cell(tail[0],tail[1],BG)
            else:
                await asyncio.sleep_ms(1700)
                # ricomincia partita se ancora sulla pagina e gioco attivo
                if current_page==SNAKE_IDX and game_running:
                    armed=True
                break
            await asyncio.sleep_ms(speed)
        await asyncio.sleep_ms(50)
      except Exception as e:
        print("SNAKE ERR:", e)
        game_running=False
        await asyncio.sleep_ms(300)


async def pong_loop():
    """Pong a parete, un pulsante: tap = inverti direzione racchetta."""
    global game_running, snake_btn
    armed=True
    while True:
      try:
        if current_page != PONG_IDX:
            armed=True
            await asyncio.sleep_ms(150); continue
        if not armed:
            await asyncio.sleep_ms(150); continue
        armed=False; game_running=True
        gc.collect()
        gx0=OX+2; gy0=CH*2+8; gx1=OX+DW-3; gy1=DH-CH-6
        clear(); header("Pong", 23, 0xFFE0, BLACK)
        tc(DH-CH-2, "A=inverti  A(1.5s)=esci", DIM)
        PW=4; PH=34  # racchetta verticale a destra
        py=(gy0+gy1)//2; pdir=-1
        bx=gx0+20; by=(gy0+gy1)//2; bdx=2; bdy=2
        score=0; dead=False
        px_paddle=gx1-PW
        snake_btn=None
        # disegno iniziale
        def drw_paddle(yy,col): 
            if tft: tft.fill_rect(px_paddle, yy, PW, PH, col)
        def drw_ball(x,y,col):
            if tft: tft.fill_rect(x, y, 4, 4, col)
        drw_paddle(py, CYAN)
        while current_page==PONG_IDX and game_running:
            if snake_btn: snake_btn=None; pdir=-pdir
            if not dead:
                # muovi racchetta
                drw_paddle(py, BG)
                py+=pdir*4
                if py<=gy0: py=gy0; pdir=1
                if py+PH>=gy1: py=gy1-PH; pdir=-1
                drw_paddle(py, CYAN)
                # muovi pallina
                drw_ball(bx,by,BG)
                bx+=bdx; by+=bdy
                if by<=gy0: by=gy0; bdy=abs(bdy)
                if by+4>=gy1: by=gy1-4; bdy=-abs(bdy)
                if bx<=gx0: bx=gx0; bdx=abs(bdx)
                # rimbalzo sulla racchetta
                if bx+4>=px_paddle and py<=by<=py+PH and bdx>0:
                    bdx=-abs(bdx); score+=1
                    if score%3==0 and abs(bdx)<4: bdx+=(1 if bdx>0 else -1)
                elif bx+4>=gx1:
                    dead=True
                    tc((gy0+gy1)//2, "GAME OVER", RED)
                    tc((gy0+gy1)//2+CH, f"Score {score}", WHITE)
                drw_ball(bx,by,YELLOW)
                tr(CH*2+8, f"{score}", DIM)
            else:
                await asyncio.sleep_ms(1700)
                if current_page==PONG_IDX and game_running: armed=True
                break
            await asyncio.sleep_ms(45)
        await asyncio.sleep_ms(50)
      except Exception as e:
        print("PONG ERR:", e); game_running=False
        await asyncio.sleep_ms(300)


async def invaders_loop():
    """Space Invaders a un pulsante: nave auto L-R, tap = spara."""
    global game_running, snake_btn
    armed=True
    while True:
      try:
        if current_page != INVADERS_IDX:
            armed=True
            await asyncio.sleep_ms(150); continue
        if not armed:
            await asyncio.sleep_ms(150); continue
        armed=False; game_running=True
        gc.collect()
        gx0=OX+2; gy0=CH*2+10; gx1=OX+DW-3; gy1=DH-CH-8
        clear(); header("Invaders", 24, 0x07FF, BLACK)
        tc(DH-CH-2, "A=spara  A(1.5s)=esci", DIM)
        # griglia invasori
        COLS=5; ROWS=3; IW=14; IH=10; GAPX=6; GAPY=8
        inv=[]
        ox0=gx0+4; oy0=gy0+4
        for r in range(ROWS):
            for c in range(COLS):
                inv.append([ox0+c*(IW+GAPX), oy0+r*(IH+GAPY), True])
        idir=2; idrop=False
        shipx=(gx0+gx1)//2; shipdir=2; SHIPW=16; SHIPH=8
        shipy=gy1-SHIPH
        bullet=None  # [x,y]
        score=0; dead=False; win=False
        snake_btn=None
        move_div=0
        def drw_inv(it,col):
            if tft: tft.fill_rect(it[0],it[1],IW,IH,col)
        def drw_ship(x,col):
            if tft: tft.fill_rect(x,shipy,SHIPW,SHIPH,col)
        for it in inv: drw_inv(it, GREEN)
        drw_ship(shipx, CYAN)
        while current_page==INVADERS_IDX and game_running:
            if snake_btn:
                snake_btn=None
                if bullet is None:
                    bullet=[shipx+SHIPW//2, shipy-4]
            if not dead and not win:
                # muovi nave
                drw_ship(shipx, BG)
                shipx+=shipdir
                if shipx<=gx0: shipx=gx0; shipdir=abs(shipdir)
                if shipx+SHIPW>=gx1: shipx=gx1-SHIPW; shipdir=-abs(shipdir)
                drw_ship(shipx, CYAN)
                # muovi invasori ogni N frame
                move_div+=1
                if move_div>=6:
                    move_div=0
                    edge=False
                    for it in inv:
                        if it[2]:
                            it[0]+=idir
                            if it[0]<=gx0 or it[0]+IW>=gx1: edge=True
                    if edge:
                        idir=-idir
                        for it in inv:
                            if it[2]:
                                drw_inv(it,BG); it[1]+=IH; it[0]+=idir
                                if it[1]+IH>=shipy: dead=True
                    # ridisegna invasori
                    for it in inv:
                        if it[2]: drw_inv(it,GREEN)
                # muovi proiettile
                if bullet:
                    if tft: tft.fill_rect(bullet[0],bullet[1],2,4,BG)
                    bullet[1]-=6
                    if bullet[1]<gy0:
                        bullet=None
                    else:
                        hit=False
                        for it in inv:
                            if it[2] and it[0]<=bullet[0]<=it[0]+IW and it[1]<=bullet[1]<=it[1]+IH:
                                it[2]=False; drw_inv(it,BG); hit=True; score+=1; break
                        if hit: bullet=None
                        elif bullet and tft: tft.fill_rect(bullet[0],bullet[1],2,4,RED)
                if all(not it[2] for it in inv):
                    win=True
                    tc((gy0+gy1)//2, "WIN!", GREEN)
                    tc((gy0+gy1)//2+CH, f"Score {score}", WHITE)
                if dead:
                    tc((gy0+gy1)//2, "GAME OVER", RED)
                    tc((gy0+gy1)//2+CH, f"Score {score}", WHITE)
                tr(CH*2+10, f"{score}", DIM)
            else:
                await asyncio.sleep_ms(1800)
                if current_page==INVADERS_IDX and game_running: armed=True
                break
            await asyncio.sleep_ms(55)
        await asyncio.sleep_ms(50)
      except Exception as e:
        print("INVADERS ERR:", e); game_running=False
        await asyncio.sleep_ms(300)


async def breakout_loop():
    """Breakout a un pulsante: tap = inverti direzione racchetta."""
    global game_running, snake_btn
    armed=True
    while True:
      try:
        if current_page != BREAKOUT_IDX:
            armed=True
            await asyncio.sleep_ms(150); continue
        if not armed:
            await asyncio.sleep_ms(150); continue
        armed=False; game_running=True
        gc.collect()
        gx0=OX+2; gy0=CH*2+10; gx1=OX+DW-3; gy1=DH-CH-8
        clear(); header("Breakout", 25, 0xFD20, BLACK)
        tc(DH-CH-2, "A=inverti  A(1.5s)=esci", DIM)
        PW=28; PH=4
        pxp=(gx0+gx1)//2-PW//2; pdir=3
        pyp=gy1-PH
        bx=(gx0+gx1)//2; by=pyp-8; bdx=2; bdy=-2
        # mattoni
        BCOLS=6; BROWS=4; BW=(gx1-gx0-4)//BCOLS; BH=8
        bricks=[]
        bcolors=[RED,ORANGE,YELLOW,GREEN]
        for r in range(BROWS):
            for c in range(BCOLS):
                bricks.append([gx0+2+c*BW, gy0+2+r*(BH+3), True, bcolors[r%len(bcolors)]])
        score=0; dead=False; win=False
        snake_btn=None
        def drw_pad(x,col):
            if tft: tft.fill_rect(x,pyp,PW,PH,col)
        def drw_ball(x,y,col):
            if tft: tft.fill_rect(x,y,4,4,col)
        for b in bricks:
            if b[2] and tft: tft.fill_rect(b[0],b[1],BW-2,BH,b[3])
        drw_pad(pxp,CYAN)
        while current_page==BREAKOUT_IDX and game_running:
            if snake_btn: snake_btn=None; pdir=-pdir
            if not dead and not win:
                drw_pad(pxp,BG)
                pxp+=pdir
                if pxp<=gx0: pxp=gx0; pdir=abs(pdir)
                if pxp+PW>=gx1: pxp=gx1-PW; pdir=-abs(pdir)
                drw_pad(pxp,CYAN)
                drw_ball(bx,by,BG)
                bx+=bdx; by+=bdy
                if bx<=gx0: bx=gx0; bdx=abs(bdx)
                if bx+4>=gx1: bx=gx1-4; bdx=-abs(bdx)
                if by<=gy0: by=gy0; bdy=abs(bdy)
                # racchetta
                if by+4>=pyp and pxp<=bx<=pxp+PW and bdy>0:
                    bdy=-abs(bdy)
                    off=(bx-(pxp+PW//2))
                    if off<-8 and bdx>0: bdx=-abs(bdx)
                    elif off>8 and bdx<0: bdx=abs(bdx)
                # fondo = morte
                if by+4>=gy1:
                    dead=True
                    tc((gy0+gy1)//2, "GAME OVER", RED)
                    tc((gy0+gy1)//2+CH, f"Score {score}", WHITE)
                # mattoni
                for b in bricks:
                    if b[2] and b[0]<=bx<=b[0]+BW and b[1]<=by<=b[1]+BH:
                        b[2]=False
                        if tft: tft.fill_rect(b[0],b[1],BW-2,BH,BG)
                        bdy=-bdy; score+=1; break
                drw_ball(bx,by,WHITE)
                if all(not b[2] for b in bricks):
                    win=True
                    tc((gy0+gy1)//2, "WIN!", GREEN)
                    tc((gy0+gy1)//2+CH, f"Score {score}", WHITE)
                tr(CH*2+10, f"{score}", DIM)
            else:
                await asyncio.sleep_ms(1800)
                if current_page==BREAKOUT_IDX and game_running: armed=True
                break
            await asyncio.sleep_ms(40)
        await asyncio.sleep_ms(50)
      except Exception as e:
        print("BREAKOUT ERR:", e); game_running=False
        await asyncio.sleep_ms(300)


async def bt_scan_loop():
    """Scan BLE quando si e' sulla pagina Bluetooth. Conservativo per evitare
    conflitti radio BLE+WiFi che possono resettare il C6."""
    global bt_scan_cache, bt_scan_ts
    BT_IDX = 19
    raw_hits = []  # accumulo grezzo dall'IRQ (lavoro minimo nell'interrupt)
    def _irq(event, data):
        # event 5 = _IRQ_SCAN_RESULT — NESSUNA allocazione pesante qui
        try:
            if event == 5 and len(raw_hits) < 60:
                addr_type, addr, adv_type, rssi, adv_data = data
                raw_hits.append((bytes(addr), rssi, bytes(adv_data)))
        except Exception:
            pass
    last_scan = 0
    while True:
        try:
            if current_page != BT_IDX:
                await asyncio.sleep_ms(400)
                continue
            # Una scansione ogni 10s mentre si e' sulla pagina
            if time.ticks_diff(time.ticks_ms(), last_scan) < 10000 and bt_scan_cache:
                await asyncio.sleep_ms(500)
                continue
            try:
                import bluetooth
            except ImportError:
                bt_scan_cache=[{"addr":"n/d","name":"BLE non disponibile","rssi":0}]
                await asyncio.sleep_ms(3000); continue

            gc.collect()
            raw_hits.clear()
            ble = bluetooth.BLE()
            try:
                ble.active(True)
                ble.irq(_irq)
                # scan passivo 3s: finestra piccola = meno conflitto con WiFi
                # gap_scan(durata_ms, intervallo_us, finestra_us, active)
                ble.gap_scan(3000, 100000, 30000, False)
                await asyncio.sleep_ms(3300)
            finally:
                # Ferma e DISATTIVA il BLE per restituire la radio al WiFi
                try: ble.gap_scan(None)
                except: pass
                try: ble.active(False)
                except: pass
            # Processa i risultati FUORI dall'IRQ
            devs = {}
            for addr, rssi, adv in raw_hits:
                name = _parse_ble_name(adv)
                devs[addr] = {
                    "addr": ":".join("%02X" % b for b in addr),
                    "name": name,
                    "rssi": rssi,
                }
            out = sorted(devs.values(), key=lambda d: d["rssi"], reverse=True)
            bt_scan_cache = out[:12] if out else [{"addr":"-","name":"nessun device","rssi":0}]
            bt_scan_ts = time.ticks_ms()
            last_scan = time.ticks_ms()
            gc.collect()
        except Exception as e:
            print("BT scan err:", e)
            bt_scan_cache=[{"addr":"n/d","name":"errore scan","rssi":0}]
            await asyncio.sleep_ms(3000)
        await asyncio.sleep_ms(800)

def _parse_ble_name(adv):
    """Estrae il nome dispositivo dai dati advertising BLE."""
    i = 0
    try:
        while i < len(adv):
            ln = adv[i]
            if ln == 0: break
            t = adv[i+1]
            if t in (0x08, 0x09):  # short/complete local name
                return bytes(adv[i+2:i+1+ln]).decode("utf-8", "replace")
            i += ln + 1
    except Exception:
        pass
    return None


def page_enabled(idx):
    """True se la pagina idx e' attiva (lista vuota = tutte attive)."""
    ep = cfg.get("enabled_pages", [])
    if not ep or idx >= len(ep):
        return True
    return bool(ep[idx])

def next_enabled_page(start):
    """Trova la prossima pagina attiva dopo 'start'."""
    for i in range(1, NUM_PAGES+1):
        cand = (start + i) % NUM_PAGES
        if page_enabled(cand):
            return cand
    return start  # nessuna attiva: resta

def draw_current_page():
    if not tft: return
    try: PAGE_DRAW[current_page]()
    except Exception as e:
        print(f"Draw err p{current_page}: {e}")
        clear(); tc(140,"draw error",RED); tc(160,trunc(str(e),CPL),DIM)

def draw_splash():
    clear()
    tc(80, "rCompanion", ACCENT, BG)
    tc(102,"v0.5",DIM)
    tc(140,cfg["node_name"],FG)
    tc(170,"connecting...",DIM)

# ── BTN_B ─────────────────────────────────────────────────────────────────

async def btn_b_action():
    global p7_sub, events_cache
    base=f"http://{cfg['server_ip']}:{cfg['server_port']}"
    if current_page==11:
        # Settings: ricarica config da file
        load_config()
        draw_current_page()
        await led_pulse("ok",2)
        return
    if current_page==0:
        await do_poll()
    elif current_page==3:
        try:
            r=urequests.post(f"{base}/api/lxmf/read"); r.close()
            if data and "lxmf" in data:
                data["lxmf"]["unread"]=0
                for m in data["lxmf"].get("messages",[]): m["read"]=True
            draw_current_page(); await led_pulse("ok",2)
        except Exception as e: print(f"BTN_B: {e}")
    else:
        await led_pulse("refresh",1)

# ── Polling ───────────────────────────────────────────────────────────────

async def do_poll():
    global data,last_poll,poll_error,srv_ok,traffic_history,poll_fails
    base=f"http://{cfg['server_ip']}:{cfg['server_port']}"
    led_set("refresh")
    try:
        r=urequests.get(f"{base}/api/all?ann=8&msg=4",timeout=3)
        raw=r.content
        r.close()
        # Bytes grezzi, rimuovi non-ASCII, poi parse
        clean=bytes(b for b in raw if b < 128)
        import json as _json
        data=_json.loads(clean)
        last_poll=time.time(); poll_error=False; srv_ok=True
        poll_fails=0
        # Sync storico traffico: accumula localmente l'ultimo campione del server
        th=data.get("traffic_history",[])
        if th:
            last=th[-1]
            # Aggiungi solo se nuovo timestamp (evita duplicati)
            if not traffic_history or traffic_history[-1].get("ts")!=last.get("ts"):
                traffic_history.append(last)
                if len(traffic_history)>40:
                    traffic_history.pop(0)
        # Sync orologio dal server
        global clock_epoch, clock_synced_at
        ck=data.get("clock",{})
        if ck.get("epoch"):
            clock_epoch=ck["epoch"]
            clock_synced_at=time.ticks_ms()
        # Fetch eventi se siamo sulla pagina events (9) o ogni tanto
        global events_cache, echo_cache
        if current_page==8:
            try:
                re=urequests.get(f"{base}/api/events?limit=15",timeout=3)
                rawe=re.content; re.close()
                cleane=bytes(b for b in rawe if b<128)
                events_cache=_json.loads(cleane).get("events",[])
            except: pass
        # Fetch echo log se siamo sulla pagina echo (12)
        if current_page==12:
            try:
                re2=urequests.get(f"{base}/api/echo",timeout=3)
                rawe2=re2.content; re2.close()
                cleane2=bytes(b for b in rawe2 if b<128)
                echo_cache=_json.loads(cleane2).get("log",[])
            except: pass

        update_led()
        # Rileva nuovo messaggio in arrivo: flash giallo+verde
        global prev_unread
        cur_unread = data.get("status",{}).get("unread_lxmf",0)
        if cur_unread > prev_unread:
            asyncio.create_task(led_flash_message())
        prev_unread = cur_unread
        try:
            draw_current_page()
        except Exception as de:
            import sys
            print("DRAW EXCEPTION:")
            sys.print_exception(de)
    except Exception as e:
        import sys
        print("POLL EXCEPTION:")
        sys.print_exception(e)
        poll_error=True
        poll_fails += 1
        if poll_fails >= 2:
            srv_ok=False; led_set("error")

async def poll_loop():
    while True:
        await do_poll()
        gc.collect()  # libera memoria ogni ciclo (evita MemoryError)
        await asyncio.sleep(cfg["poll_interval"])

# ── Pulsanti ──────────────────────────────────────────────────────────────

async def button_loop():
    global current_page, game_running, snake_btn
    ba=Pin(PIN_BTNA,Pin.IN,Pin.PULL_UP)
    bb=Pin(PIN_BTNB,Pin.IN,Pin.PULL_UP)
    la=lb=1
    both_since=0  # ticks_ms da quando entrambi premuti
    while True:
        a=ba.value(); b=bb.value()

        # Su una pagina gioco: SOLO BTN_A (tap=gira, hold 1.5s=esci).
        # BTN_B ignorato del tutto (evita reset hardware sul tasto destro).
        if is_game_page(current_page):
            if a==0:  # A premuto
                if both_since==0:
                    both_since=time.ticks_ms()
                elif time.ticks_diff(time.ticks_ms(), both_since) >= 1500:
                    # Hold lungo: esci dal gioco
                    both_since=-1  # marca "uscita gia fatta"
                    game_running=False
                    current_page=next_enabled_page(current_page)
                    draw_current_page(); update_led()
                    await asyncio.sleep_ms(400)
            else:  # A rilasciato
                if both_since>0:
                    # era un tap breve -> gira
                    if time.ticks_diff(time.ticks_ms(), both_since) < 1500:
                        snake_btn='R'
                both_since=0
            la=a; lb=b
            await asyncio.sleep_ms(40)
            continue

        # Navigazione normale
        if a==0 and la==1:
            current_page=next_enabled_page(current_page)
            draw_current_page(); update_led(); await asyncio.sleep_ms(250)
        if b==0 and lb==1:
            await btn_b_action(); await asyncio.sleep_ms(250)
        la=a; lb=b
        await asyncio.sleep_ms(50)


async def header_indicators():
    """Spinner in alto a sx (refresh) e dot a dx (carosello attivo)."""
    spin="|/-\\"
    i=0
    while True:
        try:
            ss_idx = NUM_PAGES-1
            if tft and not is_game_page(current_page) and current_page != ss_idx:
                bgc = PAGE_COLORS[current_page] if current_page < len(PAGE_COLORS) else 0
                tft.text(FN, spin[i%4], OX+1, 1, WHITE, bgc)
                if cfg.get("auto_rotate",0) > 0:
                    tft.fill_rect(OX+DW-9, 3, 6, 6, WHITE)
                else:
                    tft.fill_rect(OX+DW-9, 3, 6, 6, bgc)
                i+=1
        except Exception:
            pass
        await asyncio.sleep_ms(250)


async def auto_rotate_loop():
    """Scorre le pagine automaticamente se auto_rotate > 0, timing costante."""
    global current_page
    SS_IDX = NUM_PAGES - 1
    while True:
        interval = cfg.get("auto_rotate", 0)
        if interval > 0:
            # Dormi a piccoli passi per reagire subito ai cambi di config
            slept = 0
            while slept < interval:
                await asyncio.sleep_ms(500)
                slept += 0.5
                if cfg.get("auto_rotate", 0) != interval:
                    break  # config cambiata, ricomincia
            else:
                # Non avanzare se siamo sullo screensaver (lascia animare)
                if current_page != SS_IDX:
                    current_page = next_enabled_page(current_page)
                    draw_current_page(); update_led()
        else:
            await asyncio.sleep(2)

# ── WebUI ─────────────────────────────────────────────────────────────────

async def start_webui():
    try:
        from microdot import Microdot, Response
        web=Microdot()

        @web.get("/")
        async def index(req):
            gc.collect()
            return Response(_html(), headers={"Content-Type":"text/html"})

        @web.get("/api/config")
        async def gcfg(req):
            return {k:v for k,v in cfg.items() if "pass" not in k}

        @web.post("/api/config")
        async def scfg(req):
            try:
                for k,v in req.json.items():
                    if k in cfg: cfg[k]=v
                save_config()
                # Applica subito: luminosita + ridisegno pagina
                try: apply_brightness()
                except: pass
                draw_current_page(); update_led()
                return {"ok":True}
            except Exception as e: return {"ok":False,"error":str(e)},400

        @web.get("/api/data")
        async def gdata(req): return data if data else {}

        @web.get("/api/now")
        async def gnow(req):
            PGN=["Overview","Interfaces","Announces","LXMF","Identity","RNS Status",
                 "Traffic","Peers","Events","RMAP","Clock","Settings","Echo",
                 "Meteo","Risorse C6","Server PC","About","Ann Stats",
                 "WiFi Scan","BT Scan","Consumo","Snake","Pong","Invaders","Breakout","Screensaver"]
            s=data.get("status",{}) if data else {}
            return {"page": PGN[current_page] if current_page<len(PGN) else "?",
                    "idx": current_page, "total": NUM_PAGES,
                    "rns": s.get("rns_online", False),
                    "srv": srv_ok, "web": webui_ok,
                    "rotate": cfg.get("auto_rotate",0)}

        @web.post("/api/reboot")
        async def reboot(req):
            import machine
            async def _later():
                await asyncio.sleep_ms(500)
                machine.reset()
            asyncio.create_task(_later())
            return {"ok":True}

        print("WebUI :80")
        global webui_ok
        webui_ok=True
        await web.start_server(port=80,debug=False)
    except ImportError:
        print("microdot non trovato")
    except Exception as e:
        print(f"WebUI: {e}")
        webui_ok=False

def _html():
    ip=get_ip(); rns=data.get("status",{}).get("rns_online",False) if data else False
    PG_NAMES=["Overview","Interfaces","Announces","LXMF","Identity","RNS Status",
              "Traffic","Peers","Events","RMAP","Clock","Settings","Echo",
              "Meteo","Risorse C6","Server PC","About","Ann Stats",
              "WiFi Scan","BT Scan","Consumo","Snake","Pong","Invaders","Breakout","Screensaver"]
    pg=PG_NAMES[current_page] if current_page<len(PG_NAMES) else "?"
    wcity=data.get("weather",{}).get("city","Roma") if data else "Roma"
    # Costruisci checkbox toggle pagine
    ep=cfg.get("enabled_pages",[])
    toggles=""
    for i,name in enumerate(PG_NAMES):
        en = (not ep or i>=len(ep) or ep[i])
        ck = "checked" if en else ""
        toggles += f'<label class="pg"><input type="checkbox" class="pgchk" data-idx="{i}" {ck}> {name}</label>'
    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rCompanion</title>
<style>
*{{box-sizing:border-box}}
body{{background:#0a0a0a;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;padding:14px;max-width:520px;margin:0 auto}}
h1{{color:#00ffcc;margin:0;font-size:1.4em;letter-spacing:.5px}}
.sub{{color:#555;font-size:.8em;margin-bottom:14px}}
.card{{background:#121212;border:1px solid #222;border-radius:10px;padding:16px;margin-bottom:12px}}
.card b{{color:#00ffcc;font-size:.95em;text-transform:uppercase;letter-spacing:.5px}}
label{{display:block;color:#999;font-size:.8em;margin:10px 0 3px}}
input,textarea{{width:100%;background:#1c1c1c;border:1px solid #333;
       color:#fff;padding:8px 10px;border-radius:6px;font-family:inherit;font-size:.95em}}
input[type=range]{{padding:0;accent-color:#00ffcc}}
input:focus,textarea:focus{{outline:none;border-color:#00ffcc}}
button{{background:#00ffcc;color:#000;border:none;padding:10px 22px;
        border-radius:6px;font-weight:bold;cursor:pointer;margin-top:12px;font-size:.9em}}
button:active{{transform:scale(.97)}}
button.sec{{background:#2a2a2a;color:#00ffcc;border:1px solid #00ffcc}}
button.danger{{background:#ff5a3c;color:#fff}}
button.big{{width:100%;padding:14px;font-size:1em;margin-top:0}}
.row{{display:flex;justify-content:space-between;padding:5px 0;
      border-bottom:1px solid #1c1c1c;font-size:.9em}}
.row:last-child{{border:none}}
.ok{{color:#00e060;font-weight:bold}}.err{{color:#ff4040;font-weight:bold}}.dim{{color:#666}}
.pgrid{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px}}
.pg{{display:flex;align-items:center;gap:6px;color:#ccc;font-size:.85em;margin:0;cursor:pointer}}
.pg input{{width:auto;margin:0}}
.val{{color:#00ffcc;font-weight:bold}}
</style></head><body>
<h1>rCompanion</h1><div class="sub">v0.5 · {ip}</div>
<div class="card">
  <div class="row"><span>RNS</span><span id="st_rns" class="{'ok' if rns else 'err'}">{'ONLINE' if rns else 'OFFLINE'}</span></div>
  <div class="row"><span>Pagina</span><span id="st_page" class="val">{pg}</span></div>
  <div class="row"><span>Server</span><span class="dim">{cfg['server_ip']}:{cfg['server_port']}</span></div>
</div>
<div class="card"><b>Config</b>
  <label>Server IP</label><input id="si" value="{cfg['server_ip']}">
  <label>Server Port</label><input id="sp" type="number" value="{cfg['server_port']}">
  <label>WiFi SSID</label><input id="ss" value="{cfg['wifi_ssid']}">
  <label>WiFi Password</label><input id="wp" type="password" placeholder="(invariata)">
  <label>Nome nodo</label><input id="nn" value="{cfg['node_name']}">
  <label>Poll/refresh: <span id="piv">{cfg['poll_interval']}</span>s</label>
  <input id="pi" type="range" min="1" max="30" value="{cfg['poll_interval']}" oninput="document.getElementById('piv').textContent=this.value">
  <label>Backlight %</label><input id="br" type="number" min="0" max="100" value="{cfg['brightness']}">
  <label>Carosello auto: <span id="arv">{cfg.get('auto_rotate',0)}</span>s (0=off)</label>
  <input id="ar" type="range" min="0" max="30" value="{cfg.get('auto_rotate',0)}" oninput="document.getElementById('arv').textContent=this.value">
  <label class="pg" style="margin-top:8px"><input type="checkbox" id="lfp" {'checked' if cfg.get('led_follow_page',True) else ''}> LED segue colore pagina</label>
  <button onclick="save()">Salva</button>
</div>
<div class="card"><b>Meteo</b>
  <label>Citta (pagina Meteo)</label><input id="wc" value="{wcity}" placeholder="Roma">
  <button onclick="saveCity()">Imposta citta</button>
</div>
<div class="card"><b>Pagine attive</b>
  <div class="sub" style="margin:4px 0 8px">Spunta solo le pagine che vuoi vedere col tasto</div>
  <div class="pgrid">{toggles}</div>
  <button onclick="savePages()">Salva pagine</button>
</div>
<div class="card"><b>Bot LXMF</b>
  <div class="sub" style="margin:4px 0 8px">Risposte automatiche ai messaggi ricevuti</div>
  <label class="pg"><input type="checkbox" id="becho"> Echo automatico</label>
  <label class="pg"><input type="checkbox" id="bcmd"> Comandi (help/meteo/status/info)</label>
  <label>Risposte custom (una per riga: parola = risposta)</label>
  <textarea id="bcustom" rows="4" style="width:100%;box-sizing:border-box;background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px;border-radius:4px;font-family:monospace" placeholder="ciao = Ciao! Sono rCompanion&#10;orari = Aperto 9-18"></textarea>
  <button onclick="saveBot()">Salva bot</button>
</div>
<div class="card">
  <button class="big danger" onclick="saveReboot()">Salva tutto e Riavvia C6</button>
</div>
<script>
async function save(){{
  const m={{"si":"server_ip","sp":"server_port","ss":"wifi_ssid",
            "nn":"node_name","pi":"poll_interval","br":"brightness",
            "ar":"auto_rotate"}};
  const b={{}};
  for(const[k,v] of Object.entries(m)){{
    const el=document.getElementById(k);
    if(!el)continue;
    const x=el.value;
    if(x!=="") b[v]=isNaN(x)||k==="si"||k==="ss"||k==="nn"?x:Number(x);
  }}
  const pw=document.getElementById("wp").value;
  if(pw) b.wifi_pass=pw;
  b.led_follow_page=document.getElementById("lfp").checked;
  const r=await fetch("/api/config",{{method:"POST",
    headers:{{"Content-Type":"application/json"}},body:JSON.stringify(b)}});
  alert((await r.json()).ok?"Salvato e applicato!":"Errore");
}}
async function saveCity(){{
  const c=document.getElementById("wc").value;
  if(!c)return;
  const url="http://{cfg['server_ip']}:{cfg['server_port']}/api/weather/city";
  try{{
    const r=await fetch(url,{{method:"POST",
      headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{city:c}})}});
    alert((await r.json()).ok?"Citta impostata: "+c:"Errore");
  }}catch(e){{alert("Errore connessione server");}}
}}
async function savePages(){{
  const chks=document.querySelectorAll(".pgchk");
  const ep=[];
  chks.forEach(c=>{{ ep[parseInt(c.dataset.idx)]=c.checked; }});
  if(!ep.some(x=>x)){{ alert("Almeno una pagina deve restare attiva"); return; }}
  const r=await fetch("/api/config",{{method:"POST",
    headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{enabled_pages:ep}})}});
  alert((await r.json()).ok?"Pagine salvate!":"Errore");
}}
const SRV="http://{cfg['server_ip']}:{cfg['server_port']}";
async function loadBot(){{
  try{{
    const r=await fetch(SRV+"/api/bot");
    const d=await r.json();
    document.getElementById("becho").checked=!!d.echo;
    document.getElementById("bcmd").checked=!!d.commands;
    const lines=[];
    for(const[k,v] of Object.entries(d.custom||{{}})) lines.push(k+" = "+v);
    document.getElementById("bcustom").value=lines.join("\\n");
  }}catch(e){{}}
}}
async function saveBot(){{
  const custom={{}};
  document.getElementById("bcustom").value.split("\\n").forEach(line=>{{
    const i=line.indexOf("=");
    if(i>0){{ const k=line.slice(0,i).trim(); const v=line.slice(i+1).trim();
      if(k&&v) custom[k]=v; }}
  }});
  const b={{echo:document.getElementById("becho").checked,
           commands:document.getElementById("bcmd").checked, custom:custom}};
  try{{
    const r=await fetch(SRV+"/api/bot",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},body:JSON.stringify(b)}});
    alert((await r.json()).ok?"Bot salvato!":"Errore");
  }}catch(e){{alert("Errore connessione server");}}
}}
async function loadCity(){{
  try{{
    const r=await fetch(SRV+"/api/weather");
    const d=await r.json();
    if(d.city) document.getElementById("wc").value=d.city;
  }}catch(e){{}}
}}
async function saveReboot(){{
  if(!confirm("Salvare tutto e riavviare il C6?"))return;
  await save();
  try{{ await fetch("/api/reboot",{{method:"POST"}}); }}catch(e){{}}
  alert("Riavvio in corso... ricarica la pagina tra ~15s");
}}
async function tick(){{
  try{{
    const r=await fetch("/api/now");
    const d=await r.json();
    document.getElementById("st_page").textContent=d.page+" ("+(d.idx+1)+"/"+d.total+")";
    const rns=document.getElementById("st_rns");
    rns.textContent=d.rns?"ONLINE":"OFFLINE";
    rns.className=d.rns?"ok":"err";
  }}catch(e){{}}
}}
setInterval(tick, 2000); tick();
loadBot(); loadCity();
</script></body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    load_config()
    display_init()
    draw_splash()
    led_set("refresh")
    ip=wifi_connect()
    if ip:
        tc(200,f"IP:{ip}",ONLINE)
        await asyncio.sleep_ms(800)
        tc(240,"READY!",RED)
        led_set("ok")
        await asyncio.sleep_ms(1000)
    else:
        tc(200,"WiFi FAIL",RED); led_set("error")
        await asyncio.sleep_ms(1500)
    try:
        await do_poll()
    except Exception as e:
        import sys
        print("INITIAL POLL EXCEPTION:")
        sys.print_exception(e)
    asyncio.create_task(poll_loop())
    asyncio.create_task(button_loop())
    asyncio.create_task(auto_rotate_loop())
    asyncio.create_task(screensaver_loop())
    asyncio.create_task(snake_loop())
    asyncio.create_task(pong_loop())
    asyncio.create_task(invaders_loop())
    asyncio.create_task(breakout_loop())
    asyncio.create_task(bt_scan_loop())
    asyncio.create_task(header_indicators())
    asyncio.create_task(start_webui())
    while True: await asyncio.sleep(60)

asyncio.run(main())
