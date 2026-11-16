"""BudsTrack - Galaxy Buds head tracking for TrackIR / freetrack games on Windows.

Reads the motion sensor Samsung uses for 360 Audio over Bluetooth, turns it into
yaw / pitch / roll and feeds it to games (ETS2, ATS, MSFS, DCS, ...): either directly,
through the shared memory that opentrack's TrackIR / freetrack client DLLs read, or as
UDP packets for a running opentrack.

Standard library only.

    python budstrack.py               window; connects to the last used earbuds by itself
    python budstrack.py --background  start hidden in the notification area
    python budstrack.py --scan        list paired earbuds and head-tracking sensors
    python budstrack.py --probe       no GUI: connect and print data for 8 seconds
    python budstrack.py --sim         no earbuds: send a fake head motion to opentrack (UDP)

Protocol knowledge comes from GalaxyBudsClient (timschneeb, GPL-3.0); the sensor
frame conventions were first documented by AkbulutMirac/galaxy-buds-headtracker.
"""
import ctypes
import json
import math
import os
import queue
import socket  # importing it also initialises Winsock
import struct
import sys
import threading
import time
import uuid
import winreg
from ctypes import wintypes

__version__ = "0.3.0-beta.1"

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(APP_DIR, "budstrack_config.json")

DEFAULTS = {
    "device": "",
    "host": "127.0.0.1",
    "port": 4242,
    "smooth": 0.5,           # output filter: 0 = light, 0.9 = very steady
    "sensor_smoothing": 0.4,  # fixed blend applied to raw sensor quaternions
    "drift_fix": True,       # slowly pull yaw back to zero while looking ahead
    "drift_rate": 0.5,       # degrees per second
    "invert_yaw": False,
    "invert_pitch": False,
    "invert_roll": False,
    "hotkey": "ctrl+alt+c",  # global recenter hotkey
    "lang": "auto",          # auto | tr | en
    # Sensor conventions (measured on Galaxy Buds2):
    "q_world_to_body": True,          # the quaternion turns world vectors into earbud coordinates
    "world_up": [1.0, 0.0, 0.0],      # gravity axis of the sensor's world frame
    "pitch_axis": [0.99, 0.0, 0.10],  # head's right-pointing axis in earbud coordinates (calibrated)
    "calibrated": False,
    # Output: "direct" feeds TrackIR / freetrack games itself (opentrack only has to be installed,
    # not running); "opentrack" sends UDP to a running opentrack, which then does the mapping.
    "output": "direct",
    "yaw_gain": 2.0,         # direct output only: camera degrees per head degree
    "pitch_gain": 1.5,
    "auto_connect": True,    # connect to the last earbuds on start and keep retrying
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Texts (Turkish, English)
# --------------------------------------------------------------------------
TEXTS = {
    "title": ("BudsTrack - Galaxy Buds kafa takibi", "BudsTrack - Galaxy Buds head tracking"),
    "caption": ("Kulaklık sensörü, opentrack üzerinden oyuna gider.",
                "The earbud sensor reaches the game through opentrack."),
    "earbuds": ("Kulaklık:", "Earbuds:"),
    "dev_connected": ("bağlı", "connected"),
    "connect": ("Bağlan", "Connect"),
    "disconnect": ("Bağlantıyı kes", "Disconnect"),
    "ready": ("Hazır. Kulaklığı takın ve Bağlan'a basın.", "Ready. Put the earbuds in and press Connect."),
    "yaw": ("Yaw (sağ/sol):", "Yaw (left/right):"),
    "pitch": ("Pitch (yukarı/aşağı):", "Pitch (up/down):"),
    "roll": ("Roll (yatırma):", "Roll (tilt):"),
    "rate": ("%d örnek/sn", "%d samples/s"),
    "recenter": ("Merkezle (%s)", "Recenter (%s)"),
    "calibrate": ("Kalibrasyon...", "Calibrate..."),
    "settings": ("Ayarlar", "Settings"),
    "smoothing": ("Yumuşatma:", "Smoothing:"),
    "drift_fix": ("Yaw kaymasını otomatik düzelt", "Correct yaw drift automatically"),
    "invert_yaw": ("Yaw ters", "Invert yaw"),
    "invert_pitch": ("Pitch ters", "Invert pitch"),
    "invert_roll": ("Roll ters", "Invert roll"),
    "udp": ("OpenTrack UDP:", "opentrack UDP:"),
    "language": ("Dil:", "Language:"),
    "lang_note": ("Dil değişikliği yeniden açınca geçerli olur.", "The language changes after a restart."),
    "need_pair": ("Önce kulaklığı Windows Bluetooth ayarlarından eşleyin.",
                  "Pair the earbuds in Windows Bluetooth settings first."),
    "need_data": ("Kalibrasyon için önce bağlanın ve veri gelmesini bekleyin.",
                  "Connect first and wait for sensor data before calibrating."),
    "cal_title": ("Kalibrasyon", "Calibration"),
    "cal_1": ("1/2  Monitöre DÜZ bakın, başınızı dik ve sabit tutun.\nSonra Devam'a basın.",
              "1/2  Look STRAIGHT at the monitor, head upright and still.\nThen press Next."),
    "cal_2": ("2/2  Başınızı YUKARI kaldırın (yaklaşık 20-30 derece)\nve o konumda tutarken Devam'a basın.",
              "2/2  Lift your head UP (about 20-30 degrees)\nand press Next while holding it there."),
    "next": ("Devam", "Next"),
    "cal_small": ("Hareket çok küçük algılandı. Başınızı en az ~15 derece kaldırın.",
                  "The movement was too small. Lift your head at least ~15 degrees."),
    "cal_axis": ("Hareket yukarı bakış gibi görünmedi (eksen %d derece, beklenen ~90).\n"
                 "Başı yana yatırmadan, yalnızca yukarı kaldırıp yeniden deneyin.",
                 "That did not look like a head lift (axis at %d degrees, expected ~90).\n"
                 "Try again: lift only, without tilting sideways."),
    "cal_done": ("Tamam. Yukarı bakış pitch, sağa dönüş yaw, sağa yatırma roll. Düz bakıp merkezle.",
                 "Done. Looking up is pitch, turning right is yaw, tilting right is roll. Look straight and recenter."),
    "tab_track": ("Takip", "Tracking"),
    "tab_cal": ("Kalibrasyon", "Calibration"),
    "cal_intro": ("Dört kısa duruş: düz, yukarı, sağa yatır, sağa çevir. Her birini yap ve yarım saniye tut. Yanlış hareket kaydedilmez.",
                  "Four short poses: center, look up, tilt right, turn right. Hold each for half a second. A wrong move is not saved."),
    "cal_start": ("Başla", "Start"),
    "cal_again": ("Baştan al", "Start over"),
    "cal_still": ("Monitöre düz bak ve başını oynatma.", "Look straight at the monitor and hold still."),
    "cal_nod": ("Başını yavaşça yukarı kaldır ve tut. Şu an %d°.", "Slowly look up and hold it. Now %d°."),
    "cal_nod_wrong": ("Bu yukarı bakış değil. Başını yana çevirmeden sadece yukarı kaldır.",
                      "That is not looking up. Do not turn your head, only raise it."),
    "cal_tilt": ("Sağ kulağını sağ omzuna yaklaştır ve tut. Şu an %d°.",
                 "Bring your right ear toward your right shoulder and hold. Now %d°."),
    "cal_tilt_wrong": ("Bu sağa yatırma değil. Yukarı bakmadan sağ kulağını omuza getir.",
                       "That is not a right tilt. Without looking up, bring your right ear to your shoulder."),
    "cal_turn": ("Başını sağ omzuna doğru çevir ve tut. Şu an %d°.",
                 "Turn your head toward your right shoulder and hold. Now %d°."),
    "cal_turn_wrong": ("Bu sağa dönüş değil. Sadece başını sağa çevir.",
                       "That is not a right turn. Only turn your head to the right."),
    "cal_return": ("Önce tekrar düz bak. Sonraki hareket ondan sonra.",
                   "Look straight again first. The next move comes after that."),
    "cal_far": ("%d° fazla, en fazla %d° olsun. Biraz geri gel ve tut.",
                "%d° is too far; stay within %d°. Come back a little and hold."),
    "cal_inconsistent": ("Bu duruşlar bir kafa hareketi gibi durmuyor. Düz bakıp yukarıdan tekrar başla.",
                         "Those poses do not fit a real head move. Look straight and start again from looking up."),
    "cal_tilt_left": ("Bu sola yatırmaydı. Sağ kulağını sağ omuza getir.",
                      "That tilt was to the left. Bring your right ear to your right shoulder."),
    "cal_steps": (("Düz", "Yukarı", "Yatır", "Sağa"), ("Center", "Up", "Tilt", "Right")),
    "hint_cal": ("İpucu: ilk kullanımda Kalibrasyon'u bir kez yapın.", "Tip: run Calibrate once on first use."),
    "hotkey_fail": ("Uyarı: '%s' kısayolu kaydedilemedi (başka bir program kullanıyor olabilir).",
                    "Warning: could not register hotkey '%s' (another program may be using it)."),
    "s_connecting": ("Bağlanılıyor...", "Connecting..."),
    "s_waiting": ("Bağlandı, sensör verisi bekleniyor...", "Connected, waiting for sensor data..."),
    "s_sensor_on": ("Sensör açıldı.", "Sensor attached."),
    "s_tracking": ("Takip çalışıyor.", "Tracking."),
    "s_worn_off": ("Kulaklık çıkarıldı - çıkış donduruldu.", "Earbud removed - output frozen."),
    "s_closed": ("Bağlantı kesildi.", "Disconnected."),
    "s_error": ("Hata: %s", "Error: %s"),
    "e_connect": ("bağlantı kurulamadı (WSA %s). Kulaklık bu bilgisayara bağlı ve kutusundan çıkmış olmalı",
                  "could not connect (WSA %s). The earbuds must be connected to this PC and out of the case"),
    "s_nodata": ("Bağlandı ama sensör verisi gelmiyor. Kulaklık kulağınızda mı? Bu model 360 Ses'i destekliyor mu?",
                 "Connected, but no sensor data. Are the earbuds in your ears? Does this model support 360 Audio?"),
    "e_busy": ("kulaklığın yönetim kanalı başka bir programda (WSA %s). Samsung 'Galaxy Buds' uygulamasını, "
               "GalaxyBudsClient'ı ya da ikinci bir BudsTrack'i kapatıp yeniden deneyin",
               "the earbuds' management channel is in use by another program (WSA %s). Close Samsung's "
               "'Galaxy Buds' app, GalaxyBudsClient or a second BudsTrack and try again"),
    "e_link": ("bağlantı koptu (WSA %s)", "link lost (WSA %s)"),
    "direct": ("Oyuna doğrudan bağlan (OpenTrack penceresi gerekmez)",
               "Feed games directly (no opentrack window needed)"),
    "auto_connect": ("Açılışta kendiliğinden bağlan", "Connect automatically on start"),
    "yaw_gain": ("Sağ/sol hassasiyeti", "Left/right sensitivity"),
    "pitch_gain": ("Yukarı/aşağı hassasiyeti", "Up/down sensitivity"),
    "game_none": ("Oyun bekleniyor. Oyunu BudsTrack açıkken başlatın.",
                  "Waiting for a game. Start the game while BudsTrack is running."),
    "game_on": ("Oyun bağlı: %s", "Game connected: %s"),
    "game_udp": ("Açılar opentrack'e gönderiliyor (UDP).", "Angles are sent to opentrack (UDP)."),
    "no_opentrack": ("Doğrudan çıkış için opentrack kurulu olmalı (dosyaları kullanılıyor). Kur.bat'ı çalıştırın.",
                     "Direct output needs opentrack installed (its files are used). Run Kur.bat."),
    "tray_show": ("Pencereyi göster", "Show window"),
    "tray_recenter": ("Merkezle", "Recenter"),
    "tray_toggle": ("Takibi duraklat / sürdür (kulaklığı serbest bırakır)",
                    "Pause / resume tracking (frees the earbuds)"),
    "tray_quit": ("Çıkış", "Quit"),
    "tray_tip": ("BudsTrack - kafa takibi", "BudsTrack - head tracking"),
}
_lang = 1


def set_language(code):
    global _lang
    if code == "auto":
        try:
            code = "tr" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF == 0x1F else "en"
        except (AttributeError, OSError):
            code = "en"
    _lang = 0 if code == "tr" else 1


def tr(key, *args):
    text = TEXTS[key][_lang]
    return text % args if args else text


# --------------------------------------------------------------------------
# Galaxy Buds SPP protocol
# --------------------------------------------------------------------------
SOM, EOM = 0xFD, 0xDD
MSG_SET_SPATIAL_AUDIO = 124
MSG_SPATIAL_AUDIO_DATA = 194
MSG_SPATIAL_AUDIO_CONTROL = 195
CTL_ATTACH, CTL_DETACH, CTL_ATTACH_OK, CTL_DETACH_OK, CTL_KEEPALIVE = 0, 1, 2, 3, 4
DATA_GRV, DATA_WEAR_ON, DATA_WEAR_OFF = 32, 33, 34

# Buds2 / 2 Pro / FE / 3 / 4 use the new UUID, Buds Pro / Live the standard SPP one
UUID_SPP_NEW = uuid.UUID("2e73a4ad-332d-41fc-90e2-16bef06523f2")
UUID_SPP_STD = uuid.UUID("00001101-0000-1000-8000-00805f9b34fb")


def crc16(data):
    """CRC-16/XMODEM."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        crc &= 0xFFFF
    return crc


def encode_msg(msg_id, payload=b""):
    body = bytes([msg_id]) + bytes(payload)
    return bytes([SOM]) + struct.pack("<H", len(body) + 2) + body + struct.pack("<H", crc16(body)) + bytes([EOM])


def decode_stream(buf):
    """Yield (id, payload) for every complete frame in the bytearray and consume it."""
    while True:
        start = buf.find(SOM)
        if start < 0:
            buf.clear()
            return
        if start:
            del buf[:start]
        if len(buf) < 4:
            return
        size = struct.unpack_from("<H", buf, 1)[0] & 0x3FF
        total = size + 4
        if size < 3:
            del buf[:1]
            continue
        if len(buf) < total:
            return
        body = bytes(buf[3:3 + size - 2])
        crc = struct.unpack_from("<H", buf, 3 + size - 2)[0]
        if buf[total - 1] != EOM or crc16(body) != crc:
            del buf[:1]  # corrupt frame: shift one byte and resynchronise
            continue
        del buf[:total]
        yield body[0], body[1:]


# --------------------------------------------------------------------------
# Bluetooth RFCOMM through Winsock (connects by service UUID)
# --------------------------------------------------------------------------
AF_BTH, BTHPROTO_RFCOMM = 32, 3
SOL_SOCKET, SO_RCVTIMEO = 0xFFFF, 0x1006
WSAETIMEDOUT = 10060
WSAEADDRINUSE = 10048
INVALID_SOCKET = ctypes.c_size_t(-1).value

_ws = ctypes.WinDLL("ws2_32", use_last_error=True)
_ws.socket.restype = ctypes.c_size_t
_ws.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
_ws.connect.argtypes = (ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int)
_ws.send.argtypes = (ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int, ctypes.c_int)
_ws.recv.argtypes = (ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int, ctypes.c_int)
_ws.setsockopt.argtypes = (ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
_ws.closesocket.argtypes = (ctypes.c_size_t,)


class SOCKADDR_BTH(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_ulonglong),
        ("serviceClassId", ctypes.c_ubyte * 16),
        ("port", ctypes.c_ulong),
    ]


class BtError(OSError):
    def __init__(self, key, code):
        super().__init__(key)
        self.key, self.code = key, code


class BtSocket:
    """Python's socket module only takes an RFCOMM channel; with port 0 and a service
    UUID in SOCKADDR_BTH, Windows looks the channel up itself."""

    def __init__(self, mac_hex, service_uuid):
        self.s = _ws.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM)
        if self.s == INVALID_SOCKET:
            raise BtError("e_connect", _ws.WSAGetLastError())
        addr = SOCKADDR_BTH()
        addr.addressFamily = AF_BTH
        addr.btAddr = int(mac_hex, 16)
        addr.serviceClassId = (ctypes.c_ubyte * 16)(*service_uuid.bytes_le)
        addr.port = 0
        if _ws.connect(self.s, ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
            err = _ws.WSAGetLastError()
            self.close()
            raise BtError("e_connect", err)
        tv = struct.pack("<L", 300)
        _ws.setsockopt(self.s, SOL_SOCKET, SO_RCVTIMEO, tv, len(tv))
        self._buf = ctypes.create_string_buffer(2048)

    def send(self, data):
        if _ws.send(self.s, data, len(data), 0) < 0:
            raise BtError("e_link", _ws.WSAGetLastError())

    def recv(self):
        """Return b'' when nothing arrived in time; raise when the link is gone."""
        n = _ws.recv(self.s, self._buf, len(self._buf), 0)
        if n > 0:
            return self._buf.raw[:n]
        err = _ws.WSAGetLastError() if n < 0 else 0
        if n < 0 and err == WSAETIMEDOUT:
            return b""
        raise BtError("e_link", err)

    def close(self):
        if self.s != INVALID_SOCKET:
            _ws.closesocket(self.s)
            self.s = INVALID_SOCKET


class _BtSearch(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("fReturnAuthenticated", wintypes.BOOL),
                ("fReturnRemembered", wintypes.BOOL), ("fReturnUnknown", wintypes.BOOL),
                ("fReturnConnected", wintypes.BOOL), ("fIssueInquiry", wintypes.BOOL),
                ("cTimeoutMultiplier", ctypes.c_ubyte), ("hRadio", wintypes.HANDLE)]


class _BtInfo(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("Address", ctypes.c_ulonglong),
                ("ulClassofDevice", wintypes.ULONG), ("fConnected", wintypes.BOOL),
                ("fRemembered", wintypes.BOOL), ("fAuthenticated", wintypes.BOOL),
                ("stLastSeen", wintypes.WORD * 8), ("stLastUsed", wintypes.WORD * 8),
                ("szName", wintypes.WCHAR * 248)]


def bt_devices():
    """The paired devices Windows knows right now, as [(mac_hex, name, connected)].

    Asked from the Bluetooth stack each time (no radio inquiry, so it is instant), which
    is what keeps the list current: a device paired or removed a second ago is reflected,
    and so is whether it is connected. Connected devices come first, then Buds. Falls
    back to the registry when the API is unavailable.
    """
    found = []
    try:
        api = ctypes.WinDLL("bthprops.cpl")
        api.BluetoothFindFirstDevice.restype = wintypes.HANDLE
        api.BluetoothFindFirstDevice.argtypes = (ctypes.POINTER(_BtSearch), ctypes.POINTER(_BtInfo))
        api.BluetoothFindNextDevice.argtypes = (wintypes.HANDLE, ctypes.POINTER(_BtInfo))
        api.BluetoothFindDeviceClose.argtypes = (wintypes.HANDLE,)
        search = _BtSearch(ctypes.sizeof(_BtSearch), 1, 1, 0, 1, 0, 0, None)
        info = _BtInfo()
        info.dwSize = ctypes.sizeof(_BtInfo)
        handle = api.BluetoothFindFirstDevice(ctypes.byref(search), ctypes.byref(info))
        if handle:
            while True:
                if info.fAuthenticated or info.fConnected:
                    found.append(("%012x" % info.Address, info.szName or "?", bool(info.fConnected),
                                  (info.ulClassofDevice >> 8) & 0x1F))
                info = _BtInfo()
                info.dwSize = ctypes.sizeof(_BtInfo)
                if not api.BluetoothFindNextDevice(handle, ctypes.byref(info)):
                    break
            api.BluetoothFindDeviceClose(handle)
    except (OSError, AttributeError):
        found = []
    if not found:
        return [(mac, name, False) for mac, name in _registry_devices()]
    # keep audio devices (major class 4) and anything called Buds; mice, phones etc. only
    # when nothing of that kind exists, so a wrongly classified earbud is never hidden
    audio = [d for d in found if d[3] == 4 or "buds" in d[1].lower()]
    out = [d[:3] for d in (audio or found)]
    out.sort(key=lambda d: (not d[2], "buds" not in d[1].lower(), d[1].lower()))
    return out


def paired_devices():
    """Paired Bluetooth devices as [(mac_hex, name)], connected ones and Buds first."""
    return [(mac, name) for mac, name, _connected in bt_devices()]


def _registry_devices():
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices")
    except OSError:
        return out
    i = 0
    while True:
        try:
            mac = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            with winreg.OpenKey(root, mac) as k:
                raw = winreg.QueryValueEx(k, "Name")[0]
            name = raw if isinstance(raw, str) else bytes(raw).split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            name = "?"
        out.append((mac, name))
    out.sort(key=lambda d: ("buds" not in d[1].lower(), d[1].lower()))
    return out


# --------------------------------------------------------------------------
# Quaternion maths, (x, y, z, w)
# --------------------------------------------------------------------------
def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def q_norm(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def q_rotate(q, v):
    """q v q^-1"""
    x, y, z, _ = q_mul(q_mul(q, (v[0], v[1], v[2], 0.0)), q_conj(q))
    return (x, y, z)


def q_rotvec(q):
    """Rotation vector (axis * angle in radians)."""
    x, y, z, w = q if q[3] >= 0 else tuple(-c for c in q)
    s = math.sqrt(x * x + y * y + z * z)
    if s < 1e-9:
        return (0.0, 0.0, 0.0)
    ang = 2.0 * math.atan2(s, w)
    return (x / s * ang, y / s * ang, z / s * ang)


def q_matrix(q):
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(v_dot(a, a))


def v_unit(a):
    n = v_len(a) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


class Orientation:
    """Turns raw sensor quaternions into recentred yaw / pitch / roll in degrees.

    Head frame: X = right, Y = forward, Z = up. The earbud sits tilted in the ear, so
    its own axes say little about the head. Two facts pin the head frame down in
    earbud coordinates:
      * "up" is gravity, which the sensor knows, taken at the moment of recentring;
      * "right" is the axis the head turns about when it is lifted - fixed to the
        earbud, measured once by the calibration and stored.
    Because both are expressed in earbud coordinates, the result does not depend on
    the sensor's arbitrary (and drifting) heading reference.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.q = None        # filtered earbud->world quaternion
        self.ref = None      # the same at the last recentre
        self.axes = None     # head X, Y, Z in earbud coordinates at the last recentre
        self.yaw_off = 0.0
        self.last_t = None
        self.out = (0.0, 0.0, 0.0)
        self.raw = None
        self.calibrating = False

    def reset(self):
        with self.lock:
            self.q = self.ref = self.axes = None
            self.raw = None
            self.yaw_off, self.last_t, self.out = 0.0, None, (0.0, 0.0, 0.0)

    def read_raw(self):
        with self.lock:
            return self.raw

    def _set_reference(self):
        self.ref = self.q
        up = v_unit(q_rotate(q_conj(self.ref), self.cfg["world_up"]))  # world -> earbud
        right = self.cfg["pitch_axis"]
        d = v_dot(right, up)
        right = v_unit(tuple(right[i] - d * up[i] for i in range(3)))
        self.axes = (right, v_cross(up, right), up)
        self.yaw_off = 0.0

    def recenter(self):
        with self.lock:
            if self.q is not None:
                self._set_reference()

    def relative_rotvec(self):
        """Rotation since the last recentre, in earbud coordinates, plus the 'up' axis there."""
        with self.lock:
            if self.q is None or self.ref is None:
                return None
            return q_rotvec(q_mul(q_conj(self.ref), self.q)), self.axes[2]

    def update(self, raw):
        raw = q_norm(raw)
        if self.cfg["q_world_to_body"]:
            raw = q_conj(raw)
        now = time.monotonic()
        with self.lock:
            self.raw = raw
            if self.q is None:
                self.q = raw
            else:
                if sum(a * b for a, b in zip(self.q, raw)) < 0:
                    raw = tuple(-c for c in raw)
                k = 1.0 - max(0.0, min(0.95, float(self.cfg["sensor_smoothing"])))
                self.q = q_norm(tuple(a + (b - a) * k for a, b in zip(self.q, raw)))
            if self.ref is None:
                self._set_reference()
            dt = min(0.2, now - self.last_t) if self.last_t else 0.0
            self.last_t = now

            R = q_matrix(q_mul(q_conj(self.ref), self.q))
            ax = self.axes
            # the same rotation in head coordinates: H[a][b] = axis_a . (R * axis_b)
            Rb = [tuple(v_dot(row, ax[b]) for row in R) for b in range(3)]
            H = [[v_dot(ax[a], Rb[b]) for b in range(3)] for a in range(3)]
            # H = Rz(-yaw) * Rx(pitch) * Ry(roll)
            pitch = math.degrees(math.asin(max(-1.0, min(1.0, H[2][1]))))
            roll = math.degrees(math.atan2(-H[2][0], H[2][2]))
            yaw = -math.degrees(math.atan2(-H[0][1], H[1][1]))  # turning right is positive

            if self.cfg["drift_fix"] and not self.calibrating:
                y = yaw - self.yaw_off
                if abs(y) < 12 and abs(pitch) < 15:
                    step = float(self.cfg["drift_rate"]) * dt
                    self.yaw_off += max(-step, min(step, y))
            yaw = (yaw - self.yaw_off + 180.0) % 360.0 - 180.0
            if self.cfg["invert_yaw"]:
                yaw = -yaw
            if self.cfg["invert_pitch"]:
                pitch = -pitch
            if self.cfg["invert_roll"]:
                roll = -roll
            self.out = (yaw, pitch, roll)
            return self.out


def solve_calibration(gesture):
    """From the 'head lifted' pose return the head's right axis in earbud coordinates,
    or ("cal_small",) / ("cal_axis", degrees) when the gesture is unusable."""
    rotvec, up = gesture
    if math.degrees(v_len(rotvec)) < 10:
        return ("cal_small",)
    axis = v_unit(rotvec)  # lifting the head is a positive rotation about the right axis
    angle = math.degrees(math.acos(max(-1.0, min(1.0, v_dot(axis, up)))))
    if abs(angle - 90) > 30:  # a head lift turns about a horizontal axis
        return ("cal_axis", round(angle))
    return [round(c, 4) for c in axis]


def pose_delta(q_from, q_to):
    """Axis (or None) and angle in degrees of the rotation from q_from to q_to."""
    rv = q_rotvec(q_mul(q_conj(q_from), q_to))
    ang = math.degrees(v_len(rv))
    return (None if ang < 1e-4 else v_unit(rv)), ang


def q_average(qs):
    acc = [0.0, 0.0, 0.0, 0.0]
    ref = qs[0]
    for q in qs:
        if sum(a * b for a, b in zip(ref, q)) < 0:
            q = tuple(-c for c in q)
        for i in range(4):
            acc[i] += q[i]
    return q_norm(tuple(acc))


def up_in_earbud(q, world_up):
    return v_unit(q_rotate(q_conj(q), world_up))


_WORLD_CANDIDATES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


class HeadGuide:
    """Four-pose calibration that refuses a sloppy or mixed gesture.

    The user holds still, nods up, tilts the right ear down, then turns right.
    Each pose is an average of a steady hold, not a single frame. Pitch sign is
    taken from the nod, roll must agree with that nod, and yaw is flipped only
    when a right turn comes out backwards. A gesture that is too small, too
    large, or about the wrong axis is not stored.
    """

    STILL_SEC, HOLD_SEC = 0.65, 0.40
    STILL_DEG = 4.0
    LIMITS = {"nod": (12.0, 40.0), "tilt": (10.0, 35.0), "turn": (12.0, 50.0)}

    def __init__(self, world_up):
        self.world_up = tuple(world_up)
        self.phase = "still"
        self.samples = []
        self.q_still = None
        self.up = None
        self.nod = self.tilt = self.turn = None
        self.armed = True
        self.info = self._info("cal_still", 0.0, "wait")

    def _info(self, key, progress, tone, args=()):
        return {"text": key, "args": args, "progress": progress, "tone": tone,
                "phase": self.phase, "save": False}

    def push(self, q, t):
        if self.phase == "done":
            return self.info
        self.samples.append((t, q))
        self.samples = [(tt, qq) for tt, qq in self.samples if t - tt <= 1.4]
        if self.phase == "still":
            self.info = self._still(t)
        else:
            self.info = self._gesture(t)
        return self.info

    def _steady(self, t, seconds):
        if not self.samples:
            return 0.0, None
        newest = self.samples[-1][1]
        kept = []
        start = t
        for tt, qq in reversed(self.samples):
            if pose_delta(newest, qq)[1] > self.STILL_DEG:
                break
            start = tt
            kept.append(qq)
        age = t - start
        if age < seconds or len(kept) < 6:
            return age, None
        return age, q_average(kept)

    def _still(self, t):
        age, q = self._steady(t, self.STILL_SEC)
        if q is None:
            return self._info("cal_still", max(0.0, min(1.0, age / self.STILL_SEC)), "wait")
        self.q_still = q
        self.up = up_in_earbud(q, self.world_up)
        self.phase = "nod"
        self.armed = True
        self.samples.clear()
        return self._info("cal_nod", 0.0, "wait", (0,))

    def _classify(self, axis):
        if abs(v_dot(axis, self.up)) >= 0.62:
            return "turn"
        if self.nod is not None and abs(v_dot(axis, self.nod[0])) >= 0.62:
            return "nod"
        if self.nod is not None:
            return "tilt"
        return "nod"

    def _gesture(self, t):
        axis, ang = pose_delta(self.q_still, self.samples[-1][1])
        if not self.armed:
            if ang < 8.0:
                self.armed = True
                self.samples.clear()
                return self._info("cal_" + self.phase, 0.0, "wait", (0,))
            return self._info("cal_return", 0.0, "wait")
        lo, hi = self.LIMITS[self.phase]
        if axis is None or ang < 8.0:
            return self._info("cal_" + self.phase, max(0.0, ang / lo), "wait", (round(ang),))
        kind = self._classify(axis)
        if kind != self.phase:
            return self._info("cal_" + self.phase + "_wrong", 0.0, "bad")
        if ang > hi:
            return self._info("cal_far", 1.0, "bad", (round(ang), int(hi)))
        age, held = self._steady(t, self.HOLD_SEC)
        progress = max(0.0, min(1.0, ang / lo))
        if held is None:
            tone = "good" if ang >= lo else "wait"
            return self._info("cal_" + self.phase, progress, tone, (round(ang),))
        axis, ang = pose_delta(self.q_still, held)
        if axis is None or not (lo <= ang <= hi) or self._classify(axis) != self.phase:
            return self._info("cal_" + self.phase, progress, "wait", (round(ang),))
        setattr(self, self.phase, (axis, ang))
        order = ("nod", "tilt", "turn")
        nxt = order[order.index(self.phase) + 1] if self.phase != "turn" else "done"
        self.samples.clear()
        if nxt == "done":
            return self._finish()
        self.phase = nxt
        self.armed = False
        return self._info("cal_return", 0.0, "good")

    def _finish(self):
        got = self.result()
        if isinstance(got, tuple):
            self.nod = self.tilt = self.turn = None
            self.phase = "nod"
            self.armed = False
            self.info = self._info(got[0], 0.0, "bad")
            return self.info
        self.phase = "done"
        self.saved = got
        self.info = self._info("cal_done", 1.0, "good")
        self.info["save"] = True
        return self.info

    def result(self):
        nod, tilt, turn = self.nod[0], self.tilt[0], self.turn[0]
        preferred = [self.world_up] + [c for c in _WORLD_CANDIDATES if c != self.world_up]
        best = None
        for wu in preferred:
            up = up_in_earbud(self.q_still, wu)
            if abs(v_dot(nod, up)) > 0.5 or abs(v_dot(tilt, up)) > 0.5:
                continue
            if abs(v_dot(turn, up)) < 0.7 or abs(v_dot(nod, tilt)) > 0.5:
                continue
            score = abs(v_dot(nod, up)) + abs(v_dot(tilt, up)) + (1.0 - abs(v_dot(turn, up)))
            if best is None or score < best[0]:
                best = (score, wu, up)
        if best is None:
            return ("cal_inconsistent",)
        _score, wu, up = best
        right = v_unit(tuple(nod[i] - v_dot(nod, up) * up[i] for i in range(3)))
        forward = v_cross(up, right)
        agree = v_dot(tilt, forward)
        if agree < 0.65:
            return ("cal_tilt_left",) if agree < -0.45 else ("cal_inconsistent",)
        return {
            "pitch_axis": [round(c, 4) for c in right],
            "world_up": [round(c, 4) for c in wu],
            "invert_yaw": v_dot(turn, up) > 0,
            "invert_pitch": False,
            "invert_roll": False,
            "calibrated": True,
        }


# --------------------------------------------------------------------------
# Game output
# --------------------------------------------------------------------------
class UdpOutput:
    """opentrack 'UDP over network' input: six little-endian doubles x, y, z, yaw, pitch, roll."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, yaw, pitch, roll):
        send_pose(self.udp, self.cfg, yaw, pitch, roll)

    def game(self):
        return None

    def close(self):
        self.udp.close()


class FTHeap(ctypes.Structure):
    """opentrack's freetrack shared memory (freetrackclient/fttypes.h)."""
    _fields_ = [
        ("DataID", ctypes.c_uint32), ("CamWidth", ctypes.c_int32), ("CamHeight", ctypes.c_int32),
        ("Yaw", ctypes.c_float), ("Pitch", ctypes.c_float), ("Roll", ctypes.c_float),
        ("X", ctypes.c_float), ("Y", ctypes.c_float), ("Z", ctypes.c_float),
        ("RawYaw", ctypes.c_float), ("RawPitch", ctypes.c_float), ("RawRoll", ctypes.c_float),
        ("RawX", ctypes.c_float), ("RawY", ctypes.c_float), ("RawZ", ctypes.c_float),
        ("Points", ctypes.c_float * 8),
        ("GameID", ctypes.c_int32), ("Table", ctypes.c_ubyte * 8), ("GameID2", ctypes.c_int32),
    ]


def find_opentrack():
    """Folder of an installed opentrack that has the TrackIR / freetrack client DLLs, or None."""
    candidates = [os.path.join(os.environ.get(v, ""), "opentrack")
                  for v in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432")]
    candidates.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "opentrack"))
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, path) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    name = winreg.EnumKey(root, i)
                    if "opentrack" not in name.lower():
                        continue
                    with winreg.OpenKey(root, name) as k:
                        candidates.append(winreg.QueryValueEx(k, "InstallLocation")[0])
        except OSError:
            pass
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "modules", "NPClient64.dll")):
            return c
    return None


def parse_game_table(ftn_id, since):
    """Encryption key bytes for a game from the 'FTN_ID' column of opentrack's game list
    (the same field order as opentrack's csv.cpp)."""
    if since == "V160" or len(ftn_id) != 22:
        return bytes(8)
    try:
        b = bytes.fromhex(ftn_id)
    except ValueError:
        return bytes(8)
    return bytes([b[5], b[4], b[3], b[2], b[9], b[8], b[7], b[6]])


def load_game_list(path):
    """{game id: (name, key table)} from 'facetracknoir supported games.csv'."""
    games = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                col = line.rstrip("\r\n").split(";")
                if len(col) == 8 and col[6].strip().isdigit():
                    games[int(col[6])] = (col[1], parse_game_table(col[7].strip(), col[3]))
    except OSError:
        pass
    return games


class OutputError(Exception):
    pass


_REG_CLIENTS = (r"Software\NaturalPoint\NATURALPOINT\NPClient Location", r"Software\Freetrack\FreetrackClient")


class DirectOutput:
    """Feeds TrackIR / freetrack games without a running opentrack.

    Games load opentrack's NPClient64.dll / freetrackclient64.dll through a registry path;
    those DLLs read the shared memory block 'FT_SharedMem'. opentrack's freetrack protocol
    normally fills that block - here BudsTrack does it, so opentrack only has to be installed.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        base = find_opentrack()
        if not base:
            raise OutputError("no_opentrack")
        self.modules = os.path.join(base, "modules")
        self.games = load_game_list(os.path.join(base, "doc", "settings", "facetracknoir supported games.csv"))
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = k32.CreateFileMappingW.restype = k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.CreateFileMappingW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                           ctypes.c_ulong, ctypes.c_wchar_p)
        k32.MapViewOfFile.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_size_t)
        k32.UnmapViewOfFile.argtypes = k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.k32 = k32
        self.mutex = k32.CreateMutexW(None, False, "FT_Mutext")
        self.mapping = k32.CreateFileMappingW(ctypes.c_void_p(-1), None, 4, 0, ctypes.sizeof(FTHeap), "FT_SharedMem")
        self.view = k32.MapViewOfFile(self.mapping, 2, 0, 0, ctypes.sizeof(FTHeap)) if self.mapping else None
        if not self.view:
            raise OutputError("no_opentrack")
        self.heap = FTHeap.from_address(self.view)
        self.heap.DataID, self.heap.CamWidth, self.heap.CamHeight = 1, 100, 250
        self.heap.GameID2 = 0
        self.heap.Table[:] = bytes(8)
        self.game_id = 0
        self.game_name = None
        self.saved_paths = {}
        location = self.modules.replace("\\", "/") + "/"
        for key in _REG_CLIENTS:
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                    try:
                        self.saved_paths[key] = winreg.QueryValueEx(k, "Path")[0]
                    except OSError:
                        self.saved_paths[key] = ""
                    winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, location)
            except OSError:
                pass
        # Some games only talk to TrackIR while a process called TrackIR.exe exists;
        # opentrack ships a do-nothing one for that purpose.
        self.dummy = None
        try:
            import subprocess
            self.dummy = subprocess.Popen([os.path.join(self.modules, "TrackIR.exe")], cwd=self.modules,
                                          creationflags=0x08000000)
        except OSError:
            pass

    def send(self, yaw, pitch, roll):
        h = self.heap
        yaw = max(-179.0, min(179.0, yaw * float(self.cfg["yaw_gain"])))
        pitch = max(-89.0, min(89.0, pitch * float(self.cfg["pitch_gain"])))
        rad = math.pi / 180.0
        h.X = h.Y = h.Z = h.RawX = h.RawY = h.RawZ = 0.0
        h.Yaw, h.Pitch, h.Roll = -yaw * rad, -pitch * rad, roll * rad
        h.RawYaw, h.RawPitch, h.RawRoll = -yaw * rad, pitch * rad, roll * rad
        gid = h.GameID
        if gid != self.game_id:
            name, table = self.games.get(gid, (None, bytes(8)))
            h.Table[:] = table
            h.GameID2 = gid
            h.DataID = 0
            self.game_id = gid
            self.game_name = (name or "ID %d" % gid) if gid else None
        else:
            h.DataID = (h.DataID + 1) & 0xFFFFFFFF

    def game(self):
        return self.game_name

    def close(self):
        if self.dummy:
            try:
                self.dummy.kill()
            except OSError:
                pass
        for key in _REG_CLIENTS:
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                    winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, "")
            except OSError:
                pass
        if self.view:
            h = self.heap
            h.Yaw = h.Pitch = h.Roll = 0.0
            self.heap = None
            self.k32.UnmapViewOfFile(self.view)
            self.view = None
        for handle in (self.mapping, self.mutex):
            if handle:
                self.k32.CloseHandle(handle)
        self.mapping = self.mutex = None


def make_output(cfg):
    """Return (output, error key or None); falls back to UDP when direct output is unavailable."""
    if cfg["output"] == "direct":
        try:
            return DirectOutput(cfg), None
        except OutputError as e:
            return UdpOutput(cfg), str(e)
    return UdpOutput(cfg), None


# --------------------------------------------------------------------------
# Icon and system tray
# --------------------------------------------------------------------------
def ensure_icon():
    """Write a small icon file next to the script (once) and return its path."""
    path = os.path.join(APP_DIR, "budstrack.ico")
    if os.path.isfile(path):
        return path
    n = 32
    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            dx, dy = x - 15.5, y - 15.5
            d = math.hypot(dx, dy)
            if d > 15.0:
                row += b"\x00\x00\x00\x00"
            elif math.hypot(dx - 4.5, dy + 4.5) < 4.2:
                row += bytes((0xFF, 0xFF, 0xFF, 0xFF))          # "nose" dot
            else:
                row += bytes((0xFF, 0x7A, 0x00, 0xFF))          # BGRA: blue disc
        rows.append(bytes(row))
    pixels = b"".join(reversed(rows))                           # bitmaps are bottom-up
    mask = bytes(n * 4)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    image = header + pixels + mask
    try:
        with open(path, "wb") as f:
            f.write(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(image), 22) + image)
    except OSError:
        return None
    return path


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wintypes.HANDLE),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class TrayIcon(threading.Thread):
    """Notification-area icon. Clicks are reported as ("tray", action, ()) on the event queue,
    so all GUI work stays on the Tk thread."""

    WM_TRAY = 0x8001
    MENU = (("show", "tray_show"), ("recenter", "tray_recenter"), ("toggle", "tray_toggle"),
            ("quit", "tray_quit"))

    def __init__(self, events, icon_path):
        super().__init__(daemon=True)
        self.events, self.icon_path = events, icon_path
        self.ready = threading.Event()
        self.ok = False
        self.hwnd = None
        self.nid = None

    def run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p)
        user32.LoadImageW.restype = user32.LoadIconW.restype = user32.CreatePopupMenu.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT)
        user32.LoadIconW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        user32.AppendMenuW.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
        user32.TrackPopupMenu.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          wintypes.HWND, ctypes.c_void_p)
        user32.DestroyMenu.argtypes = (wintypes.HANDLE,)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.c_void_p)
        self.shell32 = shell32

        def proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                if lparam in (0x0202, 0x0203):      # left button up / double click
                    self.events.put(("tray", "show", ()))
                elif lparam == 0x0205:               # right button up
                    menu = user32.CreatePopupMenu()
                    for i, (_action, key) in enumerate(self.MENU):
                        user32.AppendMenuW(menu, 0, i + 1, tr(key))
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    user32.SetForegroundWindow(hwnd)
                    cmd = user32.TrackPopupMenu(menu, 0x0100 | 0x0020, pt.x, pt.y, 0, hwnd, None)
                    user32.DestroyMenu(menu)
                    if cmd:
                        self.events.put(("tray", self.MENU[cmd - 1][0], ()))
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = _WNDPROC(proc)  # keep a reference: Windows calls it for the window's lifetime
        cls = WNDCLASSW()
        cls.lpfnWndProc = self._proc
        cls.lpszClassName = "BudsTrackTray"
        user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(0, "BudsTrackTray", "BudsTrack", 0, 0, 0, 0, 0, None, None, None, None)
        icon = None
        if self.icon_path:
            icon = user32.LoadImageW(None, self.icon_path, 1, 16, 16, 0x0010)  # IMAGE_ICON, LR_LOADFROMFILE
        if not icon:
            icon = user32.LoadIconW(None, ctypes.c_void_p(32512))               # IDI_APPLICATION
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = 0x1 | 0x2 | 0x4                 # message, icon, tip
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = icon
        nid.szTip = tr("tray_tip")
        self.nid = nid
        self.ok = bool(self.hwnd) and bool(shell32.Shell_NotifyIconW(0, ctypes.byref(nid)))
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def remove(self):
        if self.ok and self.nid is not None:
            self.shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
            self.ok = False


class PoseFilter:
    """Steadies the angles sent to the game.

    Two stages per axis: a small dead band, so sensor noise around a resting head does
    not move the target at all, then a low-pass whose time constant depends on how far
    the output is behind - long while the head is still (no tremble), short while it is
    moving (no lag). It runs on a fixed clock, so the bursts Bluetooth delivers samples
    in are evened out as well.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.held = None
        self.out = None

    def reset(self):
        self.held = self.out = None

    def step(self, target, dt):
        if self.out is None:
            self.held, self.out = list(target), list(target)
            return tuple(self.out)
        amount = max(0.0, min(0.9, float(self.cfg["smooth"])))
        dead = 0.6 * amount
        tau_slow, tau_fast = 0.08 + 0.45 * amount, 0.025 + 0.06 * amount
        for i in range(3):
            d = target[i] - self.held[i]
            if i == 0:
                d = (d + 180.0) % 360.0 - 180.0
            if abs(d) > dead:
                self.held[i] += d - math.copysign(dead, d)
            e = self.held[i] - self.out[i]
            if i == 0:
                e = (e + 180.0) % 360.0 - 180.0
            w = max(0.0, min(1.0, (abs(e) - 0.3) / 2.7))
            tau = tau_slow + (tau_fast - tau_slow) * w
            self.out[i] += e * (1.0 - math.exp(-dt / tau))
        self.out[0] = (self.out[0] + 180.0) % 360.0 - 180.0
        return tuple(self.out)


class OutputPump(threading.Thread):
    """Sends the filtered pose to the game output about 125 times a second."""

    def __init__(self, cfg, out_box):
        super().__init__(daemon=True)
        self.cfg, self.out_box = cfg, out_box
        self.lock = threading.Lock()      # also taken by the GUI while it swaps the output
        self.filter = PoseFilter(cfg)
        self.target = None
        self.frozen = False
        self.stop_flag = threading.Event()
        self.sent = (0.0, 0.0, 0.0)

    def set_target(self, yaw, pitch, roll):
        self.target = (yaw, pitch, roll)

    def freeze(self, frozen):
        self.frozen = frozen

    def clear(self):
        self.target = None
        self.frozen = False
        self.filter.reset()

    def run(self):
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)  # sleep() would otherwise tick at ~64 Hz
        except (AttributeError, OSError):
            pass
        last = time.perf_counter()
        while not self.stop_flag.is_set():
            time.sleep(0.008)
            now = time.perf_counter()
            dt, last = min(0.1, now - last), now
            target = self.target
            if target is None or self.frozen:
                continue
            with self.lock:
                self.sent = self.filter.step(target, dt)
                self.out_box["o"].send(*self.sent)


_instance_mutex = None


def already_running():
    """True when another BudsTrack is open; that one is asked to show its window."""
    global _instance_mutex
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = k32.CreateMutexW(None, False, "BudsTrack.SingleInstance")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW("BudsTrackTray", None)
    if hwnd:
        user32.PostMessageW(hwnd, TrayIcon.WM_TRAY, 0, 0x0202)
    return True


# --------------------------------------------------------------------------
# Tracker thread
# --------------------------------------------------------------------------
class Tracker(threading.Thread):
    def __init__(self, cfg, orient, mac, events, pump):
        super().__init__(daemon=True)
        self.cfg, self.orient, self.mac, self.events = cfg, orient, mac, events
        self.pump = pump
        self.stop_flag = threading.Event()
        self.samples = 0
        self.worn = True

    def log(self, kind, key, *args):
        self.events.put((kind, key, args))

    def run(self):
        sock = None
        try:
            self.log("status", "s_connecting")
            last_err = None
            for u in (UUID_SPP_NEW, UUID_SPP_STD):
                try:
                    sock = BtSocket(self.mac, u)
                    break
                except BtError as e:
                    last_err = e
                    if e.code == WSAEADDRINUSE:
                        # Another program (Samsung's Galaxy Buds app, GalaxyBudsClient, a second
                        # BudsTrack) holds the management channel. Falling back to the plain SPP
                        # service would "connect" but never deliver sensor data.
                        raise BtError("e_busy", e.code)
            if sock is None:
                raise last_err
            # Only the sensor stream is attached. The earbuds' own settings (360 Audio, noise
            # control, touch controls, equaliser) are never written: the stream works without
            # SET_SPATIAL_AUDIO, verified on Buds2 with 360 Audio switched off.
            sock.send(encode_msg(MSG_SPATIAL_AUDIO_CONTROL, bytes([CTL_ATTACH])))
            self.log("status", "s_waiting")
            buf = bytearray()
            last_keep = connected_at = time.monotonic()
            warned = False
            while not self.stop_flag.is_set():
                if not warned and not self.samples and time.monotonic() - connected_at > 6:
                    warned = True
                    self.log("status", "s_nodata")
                data = sock.recv()
                if data:
                    buf += data
                    for mid, payload in decode_stream(buf):
                        self.handle(mid, payload)
                if time.monotonic() - last_keep >= 2.0:
                    sock.send(encode_msg(MSG_SPATIAL_AUDIO_CONTROL, bytes([CTL_KEEPALIVE])))
                    last_keep = time.monotonic()
            try:
                sock.send(encode_msg(MSG_SPATIAL_AUDIO_CONTROL, bytes([CTL_DETACH])))
                time.sleep(0.2)
            except BtError:
                pass
            self.log("stopped", "s_closed")
        except BtError as e:
            self.log("stopped", "s_error", (e.key, e.code))
        finally:
            if sock:
                sock.close()
            self.pump.clear()

    def handle(self, mid, payload):
        if mid == MSG_SPATIAL_AUDIO_CONTROL and payload:
            if payload[0] == CTL_ATTACH_OK:
                self.log("status", "s_sensor_on")
        elif mid == MSG_SPATIAL_AUDIO_DATA and payload:
            ev = payload[0]
            if ev == DATA_GRV and len(payload) >= 9:
                x, y, z, w = (v / 10000.0 for v in struct.unpack_from("<4h", payload, 1))
                if x == y == z == w == 0:
                    return
                yaw, pitch, roll = self.orient.update((x, y, z, w))
                self.samples += 1
                if self.samples == 1:
                    self.log("status", "s_tracking")
                self.pump.set_target(yaw, pitch, roll)
            elif ev == DATA_WEAR_OFF:
                self.worn = False
                self.pump.freeze(True)
                self.log("status", "s_worn_off")
            elif ev == DATA_WEAR_ON:
                self.worn = True
                self.pump.freeze(False)
                self.log("status", "s_tracking")


def event_text(key, args):
    if key == "s_error":
        return tr("s_error", tr(args[0][0], args[0][1]))
    return tr(key, *args)


def send_pose(udp, cfg, yaw, pitch, roll):
    """opentrack 'UDP over network': six little-endian doubles x, y, z, yaw, pitch, roll."""
    try:
        udp.sendto(struct.pack("<6d", 0.0, 0.0, 0.0, yaw, pitch, roll), (cfg["host"], int(cfg["port"])))
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# Global hotkey (recentre while a game has focus)
# --------------------------------------------------------------------------
def parse_hotkey(text):
    mods, vk = 0, 0
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= 0x0002
        elif part == "alt":
            mods |= 0x0001
        elif part == "shift":
            mods |= 0x0004
        elif part == "win":
            mods |= 0x0008
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif part in ("home", "end", "insert", "delete", "pause"):
            vk = {"home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "pause": 0x13}[part]
    return mods, vk


def hotkey_thread(text, callback, result):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    mods, vk = parse_hotkey(text)
    ok = bool(vk) and bool(user32.RegisterHotKey(None, 1, mods | 0x4000, vk))
    result.append(ok)
    if not ok:
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:
            callback()


# --------------------------------------------------------------------------
# GUI — macOS-style window. Tracking behaviour below is unchanged.
# --------------------------------------------------------------------------
_MAC = {
    "bg": "#F5F5F7",
    "card": "#FFFFFF",
    "line": "#D2D2D7",
    "hair": "#E5E5EA",
    "text": "#1D1D1F",
    "sub": "#6E6E73",
    "blue": "#007AFF",
    "blue_h": "#0071E3",
    "blue_p": "#005BBB",
    "green": "#34C759",
    "amber": "#FF9F0A",
    "red": "#FF3B30",
    "mute": "#8E8E93",
    "stage": "#1C1C1E",
}


def _hwnd(widget):
    user32 = ctypes.windll.user32
    wid = widget.winfo_id()
    parent = user32.GetParent(wid)
    return parent or wid


def _mac_chrome(widget):
    """Drop the Windows caption so the traffic-light bar is the real title bar."""
    user32 = ctypes.windll.user32
    hwnd = _hwnd(widget)
    style = user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
    style = (style & ~0x00C00000 & ~0x00040000) | 0x00080000 | 0x00020000
    user32.SetWindowLongW(hwnd, -16, style)
    ex = user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
    user32.SetWindowLongW(hwnd, -20, ex | 0x00040000)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except (AttributeError, OSError):
        pass


def run_gui():
    import tkinter as tk
    from tkinter import ttk

    BG, CARD = _MAC["bg"], _MAC["card"]
    TEXT, SUB = _MAC["text"], _MAC["sub"]
    FONT = ("Segoe UI", 10)
    FONT_B = ("Segoe UI", 10, "bold")

    def blob(canvas, x1, y1, x2, y2, radius, fill):
        r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
        canvas.create_oval(x1, y1, x1 + 2 * r, y1 + 2 * r, fill=fill, outline="")
        canvas.create_oval(x2 - 2 * r, y1, x2, y1 + 2 * r, fill=fill, outline="")
        canvas.create_oval(x1, y2 - 2 * r, x1 + 2 * r, y2, fill=fill, outline="")
        canvas.create_oval(x2 - 2 * r, y2 - 2 * r, x2, y2, fill=fill, outline="")
        canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
        canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")

    class TrafficLights(tk.Canvas):
        def __init__(self, parent, commands):
            super().__init__(parent, width=58, height=18, bg=BG, highlightthickness=0, bd=0)
            self.commands = commands
            self.colors = ("#FF5F57", "#FEBC2E", "#28C840")
            self.inks = ("#4A0002", "#5C4300", "#063B12")
            self.glyphs = ("×", "–", "+")
            self.hover = False
            self._draw()
            self.bind("<Enter>", lambda _e: self._set(True))
            self.bind("<Leave>", lambda _e: self._set(False))
            self.bind("<Button-1>", self._click)

        def _set(self, hover):
            self.hover = hover
            self._draw()

        def _draw(self):
            self.delete("all")
            for i, color in enumerate(self.colors):
                x = 2 + i * 20
                self.create_oval(x, 2, x + 14, 16, fill=color, outline="")
                if self.hover:
                    self.create_text(x + 7, 9, text=self.glyphs[i], fill=self.inks[i], font=("Segoe UI", 8, "bold"))

        def _click(self, event):
            index = int(event.x // 20)
            if 0 <= index < 3 and self.commands[index]:
                self.commands[index]()

    class MacButton(tk.Canvas):
        def __init__(self, parent, text, command, width, role="secondary"):
            super().__init__(parent, width=width, height=32, bg=parent["bg"], highlightthickness=0, bd=0)
            self.command = command
            self.role = role
            self.label = text
            self.enabled = True
            self.width_px = width
            self._draw(False, False)
            self.bind("<Enter>", lambda _e: self._draw(True, False))
            self.bind("<Leave>", lambda _e: self._draw(False, False))
            self.bind("<Button-1>", lambda _e: self._draw(True, True) if self.enabled else None)
            self.bind("<ButtonRelease-1>", self._release)

        def set_text(self, text):
            self.label = text
            self._draw(False, False)

        def set_role(self, role):
            self.role = role
            self._draw(False, False)

        def set_enabled(self, enabled):
            self.enabled = enabled
            self._draw(False, False)

        def _palette(self, hover, pressed):
            if not self.enabled:
                return "#E5E5EA", "#8E8E93", "#E5E5EA"
            if self.role == "primary":
                fill = _MAC["blue_p"] if pressed else _MAC["blue_h"] if hover else _MAC["blue"]
                return fill, "#FFFFFF", fill
            if self.role == "danger":
                fill = "#FFE4E3" if (hover or pressed) else CARD
                return fill, _MAC["red"], "#FFD0CE" if hover else _MAC["line"]
            fill = "#EFEFF4" if pressed else "#F7F7FA" if hover else CARD
            return fill, TEXT, _MAC["line"]

        def _draw(self, hover, pressed):
            self.delete("all")
            fill, fg, edge = self._palette(hover, pressed)
            blob(self, 1, 1, self.width_px - 1, 31, 10, edge)
            blob(self, 2, 2, self.width_px - 2, 30, 9, fill)
            self.create_text(self.width_px / 2, 16, text=self.label, fill=fg, font=FONT)

        def _release(self, event):
            inside = 0 <= event.x <= self.width_px and 0 <= event.y <= 32
            self._draw(inside, False)
            if self.enabled and inside and self.command:
                self.command()

    class MacSwitch(tk.Canvas):
        def __init__(self, parent, text, variable, command, width):
            super().__init__(parent, width=width, height=28, bg=CARD, highlightthickness=0, bd=0)
            self.variable = variable
            self.command = command
            self.label = text
            self.width_px = width
            self._draw()
            self.bind("<Button-1>", self._toggle)

        def _toggle(self, _event):
            self.variable.set(not self.variable.get())
            self._draw()
            self.command()

        def _draw(self):
            self.delete("all")
            self.create_text(2, 14, text=self.label, anchor="w", fill=TEXT, font=FONT)
            on = bool(self.variable.get())
            fill = _MAC["green"] if on else "#E5E5EA"
            x, y = self.width_px - 46, 3
            blob(self, x, y, x + 42, y + 22, 11, fill)
            knob = x + 22 if on else x + 2
            self.create_oval(knob, y + 2, knob + 18, y + 20, fill="#FFFFFF", outline="#D0D0D4")

    class MacSlider(tk.Canvas):
        def __init__(self, parent, variable, command, width, lo=0.0, hi=0.9):
            super().__init__(parent, width=width, height=22, bg=CARD, highlightthickness=0, bd=0)
            self.variable = variable
            self.command = command
            self.width_px = width
            self.lo, self.hi = lo, hi
            self.pad = 8
            self._draw()
            self.bind("<Button-1>", self._set)
            self.bind("<B1-Motion>", self._set)

        def _set(self, event):
            span = self.width_px - 2 * self.pad
            portion = max(0.0, min(1.0, (event.x - self.pad) / span))
            value = round(self.lo + portion * (self.hi - self.lo), 2)
            self.variable.set(value)
            self.command(value)
            self._draw()

        def _draw(self):
            self.delete("all")
            portion = max(0.0, min(1.0, (float(self.variable.get()) - self.lo) / (self.hi - self.lo)))
            y = 11
            x1, x2 = self.pad, self.width_px - self.pad
            knob = x1 + portion * (x2 - x1)
            self.create_line(x1, y, x2, y, fill="#E5E5EA", width=4, capstyle="round")
            self.create_line(x1, y, knob, y, fill=_MAC["blue"], width=4, capstyle="round")
            self.create_oval(knob - 7, y - 7, knob + 7, y + 7, fill="#FFFFFF", outline="#C7C7CC")

    if already_running():
        return
    cfg = load_config()
    set_language(cfg["lang"])
    orient = Orientation(cfg)
    events = queue.Queue()
    state = {"tracker": None, "dot": _MAC["mute"], "want": False, "shown": False, "game": "", "out_error": None}
    out_box = {"o": None}
    out_box["o"], state["out_error"] = make_output(cfg)
    pump = OutputPump(cfg, out_box)
    pump.start()
    tray = TrayIcon(events, ensure_icon())

    root = tk.Tk()
    root.title("BudsTrack")
    root.configure(bg=_MAC["line"])
    root.resizable(False, False)
    root.withdraw()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BudsTrack.GalaxyBuds")
    except (AttributeError, OSError):
        pass

    shell = tk.Frame(root, bg=BG)
    shell.pack(fill="both", expand=True, padx=1, pady=1)

    def start_drag(event):
        state["dx"] = event.x_root - root.winfo_x()
        state["dy"] = event.y_root - root.winfo_y()

    def do_drag(event):
        root.geometry("+%d+%d" % (event.x_root - state["dx"], event.y_root - state["dy"]))

    def bind_drag(widget):
        widget.bind("<Button-1>", start_drag)
        widget.bind("<B1-Motion>", do_drag)

    bar = tk.Frame(shell, bg=BG, height=52)
    bar.pack(fill="x")
    bar.pack_propagate(False)
    title = tk.Label(bar, text="BudsTrack", bg=BG, fg=TEXT, font=("Segoe UI", 13, "bold"))
    title.place(relx=0.5, y=16, anchor="n")
    for widget in (bar, title):
        bind_drag(widget)

    caption = tk.Label(shell, text=tr("caption"), bg=BG, fg=SUB, font=FONT)
    caption.pack(pady=(0, 8))

    content_w = 452
    track_page = tk.Frame(shell, bg=BG)
    cal_page = tk.Frame(shell, bg=BG)
    settings_page = tk.Frame(shell, bg=BG)
    card_parent = {"w": track_page}

    class Segmented(tk.Canvas):
        def __init__(self, parent, items, width, command):
            super().__init__(parent, width=width, height=32, bg=BG, highlightthickness=0, bd=0)
            self.items = items
            self.index = 0
            self.command = command
            self.width_px = width
            self.bind("<Button-1>", self._click)
            self._draw()

        def set_index(self, index):
            self.index = index
            self._draw()

        def _click(self, event):
            index = min(len(self.items) - 1, max(0, int(event.x / (self.width_px / len(self.items)))))
            if index != self.index:
                self.index = index
                self._draw()
                self.command(index)

        def _draw(self):
            self.delete("all")
            blob(self, 0, 0, self.width_px, 32, 10, "#E5E5EA")
            seg = self.width_px / len(self.items)
            blob(self, self.index * seg + 3, 3, (self.index + 1) * seg - 3, 29, 8, "#FFFFFF")
            for i, name in enumerate(self.items):
                self.create_text((i + 0.5) * seg, 16, text=name, fill=TEXT if i == self.index else SUB,
                                 font=FONT_B if i == self.index else FONT)

    def card():
        wrap = tk.Frame(card_parent["w"], bg=BG)
        wrap.pack(fill="x", padx=18, pady=(0, 12))
        edge = tk.Frame(wrap, bg=_MAC["hair"])
        edge.pack(fill="x")
        inner = tk.Frame(edge, bg=CARD)
        inner.pack(fill="x", padx=1, pady=1)
        body = tk.Frame(inner, bg=CARD)
        body.pack(fill="x", padx=14, pady=12)
        return body

    def show_page(index):
        pages = (track_page, cal_page, settings_page)
        for page in pages:
            page.pack_forget()
        pages[index].pack(fill="x")
        root.after(20, fit_window)

    def fit_window():
        if not root.winfo_viewable() and root.winfo_width() < 2:
            return
        root.update_idletasks()
        root.geometry("520x%d+%d+%d" % (shell.winfo_reqheight() + 2, root.winfo_x(), root.winfo_y()))

    segmented = Segmented(shell, (tr("tab_track"), tr("tab_cal"), tr("settings")), content_w, show_page)
    segmented.pack(pady=(0, 12))

    card_parent["w"] = track_page
    link = card()
    tk.Label(link, text=tr("earbuds"), bg=CARD, fg=SUB, font=FONT).pack(anchor="w")
    devices = []
    dev_var = tk.StringVar()
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "Mac.TCombobox",
        fieldbackground=CARD,
        background=CARD,
        foreground=TEXT,
        bordercolor=_MAC["line"],
        lightcolor=CARD,
        darkcolor=CARD,
        arrowcolor=SUB,
        padding=6,
        relief="flat",
    )
    style.map("Mac.TCombobox", fieldbackground=[("readonly", CARD)], bordercolor=[("focus", _MAC["blue"])])
    root.option_add("*TCombobox*Listbox.font", FONT)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", _MAC["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    combo = ttk.Combobox(link, textvariable=dev_var, state="readonly", style="Mac.TCombobox", font=FONT)
    combo.pack(fill="x", pady=(6, 10))

    def refresh_devices():
        """Re-reads the paired devices; keeps the current choice selected if it still exists."""
        fresh = bt_devices()
        if fresh == devices:
            return
        chosen = devices[combo.current()][0] if 0 <= combo.current() < len(devices) else cfg["device"]
        devices[:] = fresh
        labels = ["%s   %s%s" % (name, mac, "   • " + tr("dev_connected") if on else "")
                  for mac, name, on in devices]
        combo.configure(values=labels)
        index = next((i for i, d in enumerate(devices) if d[0] == chosen), 0)
        if labels:
            combo.current(index)
        else:
            dev_var.set("")

    def poll_devices():
        tracker = state["tracker"]
        if root.winfo_viewable() or (state["want"] and not (tracker and tracker.is_alive())):
            refresh_devices()
        root.after(3000, poll_devices)

    combo.configure(postcommand=refresh_devices)  # always current when the list is opened
    refresh_devices()

    status_row = tk.Frame(link, bg=CARD)
    status_row.pack(fill="x", pady=(0, 10))
    dot = tk.Canvas(status_row, width=12, height=12, bg=CARD, highlightthickness=0, bd=0)
    dot.pack(side="left", padx=(0, 8))
    dot_id = dot.create_oval(1, 1, 11, 11, fill=_MAC["mute"], outline="")
    status = tk.StringVar(value=tr("ready") if cfg["calibrated"] else tr("ready") + "  " + tr("hint_cal"))
    tk.Label(status_row, textvariable=status, bg=CARD, fg=TEXT, font=FONT, wraplength=400, justify="left").pack(side="left", fill="x")

    game_var = tk.StringVar(value="")
    tk.Label(link, textvariable=game_var, bg=CARD, fg=SUB, font=FONT, wraplength=420, justify="left").pack(
        anchor="w", pady=(0, 10))

    def paint_dot(color):
        state["dot"] = color
        dot.itemconfigure(dot_id, fill=color)

    def refresh_game():
        if state["out_error"] and cfg["output"] == "direct":
            text = tr(state["out_error"])
        elif isinstance(out_box["o"], UdpOutput):
            text = tr("game_udp")
        else:
            name = out_box["o"].game()
            text = tr("game_on", name) if name else tr("game_none")
        if text != state["game"]:
            state["game"] = text
            game_var.set(text)

    btn_conn = MacButton(link, tr("connect"), None, content_w, "primary")
    btn_conn.pack()

    card_parent["w"] = shell
    stage = card()
    card_parent["w"] = track_page
    preview = tk.Frame(stage, bg=CARD)
    preview.pack(fill="x")
    CV = 196
    canvas = tk.Canvas(preview, width=CV, height=CV, bg=_MAC["stage"], highlightthickness=0, bd=0)
    canvas.pack(side="left")
    canvas.create_oval(28, 28, CV - 28, CV - 28, outline="#3A3A3C")
    canvas.create_oval(58, 58, CV - 58, CV - 58, outline="#2C2C2E")
    canvas.create_line(CV / 2, 18, CV / 2, CV - 18, fill="#2C2C2E")
    canvas.create_line(18, CV / 2, CV - 18, CV / 2, fill="#2C2C2E")
    head = canvas.create_oval(0, 0, 0, 0, fill="#F5F5F7", outline="")
    nose = canvas.create_line(0, 0, 0, 0, fill=_MAC["blue"], width=3, capstyle="round")

    readouts = tk.Frame(preview, bg=CARD)
    readouts.pack(side="left", fill="both", expand=True, padx=(16, 0))
    vals = {}
    for key in ("yaw", "pitch", "roll"):
        row = tk.Frame(readouts, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text=tr(key), bg=CARD, fg=SUB, font=FONT).pack(anchor="w")
        vals[key] = tk.StringVar(value="0.0°")
        tk.Label(row, textvariable=vals[key], bg=CARD, fg=TEXT, font=("Segoe UI", 16)).pack(anchor="w")
    rate_var = tk.StringVar(value="")
    tk.Label(readouts, textvariable=rate_var, bg=CARD, fg=SUB, font=FONT).pack(anchor="w", pady=(8, 0))

    actions = tk.Frame(stage, bg=CARD)
    actions.pack(fill="x", pady=(12, 0))
    half = (content_w - 8) // 2
    btn_center = MacButton(actions, tr("recenter", cfg["hotkey"]), orient.recenter, half, "secondary")
    btn_center.pack(side="left")
    btn_cal = MacButton(actions, tr("calibrate"), None, half, "secondary")
    btn_cal.pack(side="right")

    card_parent["w"] = settings_page
    opts = card()
    card_parent["w"] = track_page
    smooth_row = tk.Frame(opts, bg=CARD)
    smooth_row.pack(fill="x")
    tk.Label(smooth_row, text=tr("smoothing"), bg=CARD, fg=TEXT, font=FONT).pack(side="left")
    smooth_val = tk.StringVar(value="%.2f" % cfg["smooth"])
    tk.Label(smooth_row, textvariable=smooth_val, bg=CARD, fg=SUB, font=FONT).pack(side="right")
    smooth = tk.DoubleVar(value=cfg["smooth"])

    def on_smooth(value):
        cfg["smooth"] = value
        smooth_val.set("%.2f" % value)

    MacSlider(opts, smooth, on_smooth, content_w).pack(anchor="w", pady=(4, 8))
    tk.Frame(opts, bg=_MAC["hair"], height=1).pack(fill="x", pady=(0, 6))
    switches = {}
    for key in ("drift_fix", "invert_yaw", "invert_pitch", "invert_roll"):
        var = tk.BooleanVar(value=cfg[key])
        widget = MacSwitch(opts, tr(key), var, lambda k=key, v=var: cfg.__setitem__(k, v.get()), content_w)
        widget.pack(anchor="w", pady=2)
        switches[key] = (var, widget)

    def set_output():
        cfg["output"] = "direct" if direct_var.get() else "opentrack"
        with pump.lock:  # the pump must not write while the old output is torn down
            out_box["o"].close()  # frees the registry entry / shared memory before the new one takes them
            out_box["o"], state["out_error"] = make_output(cfg)
        refresh_game()

    direct_var = tk.BooleanVar(value=cfg["output"] == "direct")
    MacSwitch(opts, tr("direct"), direct_var, set_output, content_w).pack(anchor="w", pady=2)
    auto_var = tk.BooleanVar(value=cfg["auto_connect"])
    MacSwitch(opts, tr("auto_connect"), auto_var,
              lambda: cfg.__setitem__("auto_connect", auto_var.get()), content_w).pack(anchor="w", pady=2)

    for gain_key in ("yaw_gain", "pitch_gain"):
        row = tk.Frame(opts, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text=tr(gain_key), bg=CARD, fg=TEXT, font=FONT).pack(side="left")
        shown = tk.StringVar(value="\u00d7%.1f" % cfg[gain_key])
        tk.Label(row, textvariable=shown, bg=CARD, fg=SUB, font=FONT).pack(side="right")
        gain = tk.DoubleVar(value=cfg[gain_key])

        def on_gain(value, k=gain_key, label=shown):
            cfg[k] = round(value, 1)
            label.set("\u00d7%.1f" % value)

        MacSlider(opts, gain, on_gain, content_w, 1.0, 4.0).pack(anchor="w", pady=(2, 0))

    track_page.pack(fill="x")

    tk.Frame(opts, bg=_MAC["hair"], height=1).pack(fill="x", pady=(8, 8))
    net = tk.Frame(opts, bg=CARD)
    net.pack(fill="x")
    tk.Label(net, text=tr("udp"), bg=CARD, fg=SUB, font=FONT).pack(side="left")
    host_var, port_var = tk.StringVar(value=cfg["host"]), tk.StringVar(value=str(cfg["port"]))

    def mac_entry(parent, variable, width):
        return tk.Entry(
            parent, textvariable=variable, width=width, font=FONT, bg=CARD, fg=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=_MAC["line"],
            highlightcolor=_MAC["blue"], insertbackground=TEXT, bd=0,
        )

    port_entry = mac_entry(net, port_var, 6)
    port_entry.pack(side="right")
    host_entry = mac_entry(net, host_var, 16)
    host_entry.pack(side="right", padx=(0, 6))

    lang_row = tk.Frame(opts, bg=CARD)
    lang_row.pack(fill="x", pady=(10, 0))
    tk.Label(lang_row, text=tr("language"), bg=CARD, fg=SUB, font=FONT).pack(side="left")
    lang_names = {"auto": "Auto", "tr": "Türkçe", "en": "English"}
    lang_var = tk.StringVar(value=lang_names.get(cfg["lang"], "Auto"))
    lang_box = ttk.Combobox(
        lang_row, textvariable=lang_var, values=list(lang_names.values()),
        width=12, state="readonly", style="Mac.TCombobox", font=FONT,
    )
    lang_box.pack(side="right")

    def apply_lang(_event=None):
        code = next(key for key, name in lang_names.items() if name == lang_var.get())
        if code == cfg["lang"]:
            return
        cfg["lang"] = code
        status.set(tr("lang_note"))
        paint_dot(_MAC["amber"])

    lang_box.bind("<<ComboboxSelected>>", apply_lang)

    def apply_net(*_args):
        cfg["host"] = host_var.get().strip() or "127.0.0.1"
        if port_var.get().isdigit():
            cfg["port"] = int(port_var.get())

    host_var.trace_add("write", apply_net)
    port_var.trace_add("write", apply_net)

    def place_over_root(win):
        win.update_idletasks()
        x = root.winfo_x() + max(0, (root.winfo_width() - win.winfo_width()) // 2)
        y = root.winfo_y() + 78
        win.geometry("+%d+%d" % (x, y))

    def sheet(title_text, body, error=False):
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.configure(bg=_MAC["line"])
        win.transient(root)
        edge = tk.Frame(win, bg=CARD)
        edge.pack(padx=1, pady=1)
        tk.Label(edge, text=title_text, bg=CARD, fg=TEXT, font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(edge, text=body, bg=CARD, fg=TEXT if not error else _MAC["red"], font=FONT, justify="left", wraplength=340).pack(anchor="w", padx=18)
        ok = MacButton(edge, "OK", win.destroy, 88, "primary")
        ok.pack(anchor="e", padx=18, pady=16)
        place_over_root(win)
        win.grab_set()
        win.focus_force()
        win.bind("<Return>", lambda _e: win.destroy())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.wait_window()

    def toggle():
        tracker = state["tracker"]
        if tracker and tracker.is_alive():
            state["want"] = False
            tracker.stop_flag.set()
            btn_conn.set_enabled(False)
            return
        refresh_devices()
        if combo.current() < 0:
            if state["shown"]:
                sheet("BudsTrack", tr("need_pair"))
            return
        state["want"] = True
        cfg["device"] = devices[combo.current()][0]
        orient.reset()
        state["tracker"] = Tracker(cfg, orient, cfg["device"], events, pump)
        state["tracker"].start()
        btn_conn.set_text(tr("disconnect"))
        btn_conn.set_role("danger")
        paint_dot(_MAC["amber"])

    btn_conn.command = toggle

    card_parent["w"] = cal_page
    cal_card = card()
    tk.Label(cal_card, text=tr("cal_title"), bg=CARD, fg=TEXT, font=("Segoe UI", 13, "bold")).pack(anchor="w")
    cal_text = tk.StringVar(value=tr("cal_intro"))
    tk.Label(cal_card, textvariable=cal_text, bg=CARD, fg=TEXT, font=FONT, justify="left", wraplength=420).pack(anchor="w", pady=(6, 8))
    step_names = tr("cal_steps")
    step_row = tk.Frame(cal_card, bg=CARD)
    step_row.pack(fill="x", pady=(0, 8))
    step_labels = []
    for name in step_names:
        lbl = tk.Label(step_row, text=name, bg=CARD, fg=SUB, font=FONT)
        lbl.pack(side="left", padx=(0, 14))
        step_labels.append(lbl)
    meter = tk.Canvas(cal_card, width=content_w, height=8, bg=CARD, highlightthickness=0, bd=0)
    meter.pack(anchor="w", pady=(0, 12))
    meter_fill = {"id": None}

    def paint_meter(progress, tone):
        meter.delete("all")
        color = {"good": _MAC["green"], "bad": _MAC["red"]}.get(tone, _MAC["blue"])
        blob(meter, 0, 1, content_w, 7, 3, "#E5E5EA")
        if progress > 0:
            blob(meter, 0, 1, max(8, content_w * progress), 7, 3, color)

    paint_meter(0, "wait")
    cal_actions = tk.Frame(cal_card, bg=CARD)
    cal_actions.pack(fill="x")
    guide_box = {"guide": None}

    def mark_steps(phase):
        order = ("still", "nod", "tilt", "turn")
        current = order.index(phase) if phase in order else len(order)
        for i, lbl in enumerate(step_labels):
            if i < current:
                lbl.configure(fg=_MAC["green"])
            elif i == current:
                lbl.configure(fg=_MAC["blue"])
            else:
                lbl.configure(fg=SUB)

    def apply_guide_result(saved):
        cfg.update(saved)
        for key, (var, widget) in switches.items():
            if key in saved:
                var.set(saved[key])
                widget._draw()
        save_config(cfg)
        orient.calibrating = False
        guide_box["guide"] = None

    def begin_cal():
        tracker = state["tracker"]
        if not (tracker and tracker.is_alive() and tracker.samples):
            cal_text.set(tr("need_data"))
            paint_meter(0, "bad")
            return
        orient.calibrating = True
        guide_box["guide"] = HeadGuide(tuple(cfg["world_up"]))
        mark_steps("still")
        paint_meter(0, "wait")
        cal_text.set(tr("cal_still"))

    def reset_cal():
        orient.calibrating = False
        guide_box["guide"] = None
        cal_text.set(tr("cal_intro"))
        paint_meter(0, "wait")
        mark_steps("still")
        for lbl in step_labels:
            lbl.configure(fg=SUB)

    MacButton(cal_actions, tr("cal_start"), begin_cal, half, "primary").pack(side="left")
    MacButton(cal_actions, tr("cal_again"), reset_cal, half, "secondary").pack(side="right")

    def open_cal():
        segmented.set_index(1)
        show_page(1)

    btn_cal.command = open_cal

    hk_result = []
    threading.Thread(target=hotkey_thread, args=(cfg["hotkey"], orient.recenter, hk_result), daemon=True).start()

    last = {"n": 0, "t": time.monotonic()}
    dot_for = {
        "s_tracking": _MAC["green"],
        "s_sensor_on": _MAC["green"],
        "s_waiting": _MAC["amber"],
        "s_connecting": _MAC["amber"],
        "s_worn_off": _MAC["amber"],
        "s_nodata": _MAC["amber"],
        "s_error": _MAC["red"],
        "s_closed": _MAC["mute"],
    }

    def tick():
        try:
            while True:
                kind, key, args = events.get_nowait()
                if kind == "tray":
                    if key == "show":
                        show_window()
                    elif key == "recenter":
                        orient.recenter()
                    elif key == "toggle":
                        toggle()
                    elif key == "quit":
                        quit_app()
                        return
                    continue
                status.set(event_text(key, args))
                paint_dot(dot_for.get(key, _MAC["mute"]))
                if kind == "stopped":
                    btn_conn.set_text(tr("connect"))
                    btn_conn.set_role("primary")
                    btn_conn.set_enabled(True)
                    if state["want"] and cfg["auto_connect"] and key == "s_error":
                        root.after(5000, retry)  # earbuds not connected yet / still in the case
        except queue.Empty:
            pass
        yaw, pitch, roll = pump.sent if pump.target is not None else orient.out  # what the game gets
        for key, value in zip(("yaw", "pitch", "roll"), (yaw, pitch, roll)):
            vals[key].set("%+.1f°" % value)
        cx = CV / 2 + max(-1, min(1, yaw / 90.0)) * 62
        cy = CV / 2 - max(-1, min(1, pitch / 60.0)) * 62
        angle = math.radians(roll)
        nx, ny = math.sin(angle) * 16, -math.cos(angle) * 16
        canvas.coords(head, cx - 11, cy - 11, cx + 11, cy + 11)
        canvas.coords(nose, cx, cy, cx + nx, cy + ny)
        tracker = state["tracker"]
        now = time.monotonic()
        refresh_game()
        guide = guide_box["guide"]
        raw = orient.read_raw()
        if guide is not None and raw is not None and guide.phase != "done":
            info = guide.push(raw, now)
            cal_text.set(tr(info["text"], *info["args"]) if info["args"] else tr(info["text"]))
            paint_meter(info["progress"], info["tone"])
            mark_steps(info["phase"])
            if info.get("save"):
                apply_guide_result(guide.saved)
        if tracker and now - last["t"] >= 1.0:
            rate_var.set(tr("rate", round((tracker.samples - last["n"]) / (now - last["t"]))) if tracker.is_alive() else "")
            last["n"], last["t"] = tracker.samples, now
        root.after(33, tick)

    def retry():
        tracker = state["tracker"]
        if state["want"] and not (tracker and tracker.is_alive()):
            toggle()

    def quit_app():
        save_config(cfg)
        state["want"] = False
        tracker = state["tracker"]
        if tracker and tracker.is_alive():
            tracker.stop_flag.set()
            tracker.join(1.5)
        pump.stop_flag.set()
        with pump.lock:
            out_box["o"].close()
        tray.remove()
        root.destroy()

    def on_close():
        """The red button hides the window; BudsTrack keeps tracking from the tray."""
        save_config(cfg)
        if tray.ok:
            root.withdraw()
        else:
            quit_app()

    def show_window():
        if not state["shown"]:
            state["shown"] = True
            finish_show()
            return
        root.deiconify()
        root.lift()
        root.focus_force()

    def minimize():
        ctypes.windll.user32.ShowWindow(_hwnd(root), 6)

    def center_window():
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("+%d+%d" % ((sw - w) // 2, max(24, (sh - h) // 5)))

    TrafficLights(bar, (on_close, minimize, center_window)).place(x=14, y=17)

    def late():
        if hk_result and not hk_result[0]:
            status.set(tr("hotkey_fail", cfg["hotkey"]))
            paint_dot(_MAC["amber"])

    def finish_show():
        _mac_chrome(root)
        root.update_idletasks()
        width = 520
        height = shell.winfo_reqheight() + 2
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (width, height, (sw - width) // 2, max(24, (sh - height) // 5)))
        root.deiconify()
        root.lift()
        root.focus_force()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(500, late)
    root.after(3000, poll_devices)
    tray.start()
    tray.ready.wait(2.0)
    background = "--background" in sys.argv and tray.ok and cfg["device"] and cfg["calibrated"]
    if cfg["device"] and (cfg["auto_connect"] or "--connect" in sys.argv):
        root.after(400, toggle)
    if not background:
        root.after(30, show_window)
    tick()
    root.mainloop()
# --------------------------------------------------------------------------
# Command line modes
# --------------------------------------------------------------------------
def run_probe(seconds=8.0):
    cfg = load_config()
    set_language("en")
    devs = paired_devices()
    mac = cfg["device"] or next((m for m, n in devs if "buds" in n.lower()), "")
    if not mac:
        print("No paired Galaxy Buds found:", devs)
        return 1
    print("Device:", mac)
    orient, events = Orientation(cfg), queue.Queue()
    out, _err = make_output(cfg)
    pump = OutputPump(cfg, {"o": out})
    pump.start()
    t = Tracker(cfg, orient, mac, events, pump)
    t.start()

    def drain():
        while not events.empty():
            _kind, key, args = events.get()
            print("  >", event_text(key, args))
    end = time.monotonic() + seconds
    while time.monotonic() < end and t.is_alive():
        time.sleep(0.5)
        drain()
        up = None if orient.axes is None else tuple(round(c, 2) for c in orient.axes[2])
        print("samples=%d  ypr=%s  up_in_earbud=%s" % (t.samples, tuple(round(c, 1) for c in orient.out), up))
    t.stop_flag.set()
    t.join(3)
    drain()
    return 0 if t.samples else 2


def run_sim():
    cfg = load_config()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("Sending a fake head motion to %s:%s (Ctrl+C to stop)" % (cfg["host"], cfg["port"]))
    t0 = time.monotonic()
    try:
        while True:
            t = time.monotonic() - t0
            send_pose(udp, cfg, 40 * math.sin(t), 15 * math.sin(t * 0.7), 0.0)
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------
# --scan: what do the paired earbuds offer? (groundwork for other brands)
# --------------------------------------------------------------------------
class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort), ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort), ("Reserved", ctypes.c_ushort * 17),
                ("Counts", ctypes.c_ushort * 10)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort), ("ProductID", ctypes.c_ushort),
                ("VersionNumber", ctypes.c_ushort)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("InterfaceClassGuid", ctypes.c_ubyte * 16),
                ("Flags", ctypes.c_ulong), ("Reserved", ctypes.c_void_p)]


def hid_devices():
    """All HID interfaces as dicts: path, vid, pid, usage_page, usage, report sizes, product."""
    hid = ctypes.WinDLL("hid", use_last_error=True)
    setup = ctypes.WinDLL("setupapi", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setup.SetupDiGetClassDevsW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong)
    setup.SetupDiEnumDeviceInterfaces.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                                  ctypes.c_void_p)
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                       ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)
    setup.SetupDiDestroyDeviceInfoList.argtypes = (ctypes.c_void_p,)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                ctypes.c_ulong, ctypes.c_void_p)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    hid.HidD_GetAttributes.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    hid.HidD_GetPreparsedData.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    hid.HidD_FreePreparsedData.argtypes = (ctypes.c_void_p,)
    hid.HidP_GetCaps.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    hid.HidD_GetProductString.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)

    guid = (ctypes.c_ubyte * 16)()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    found = []
    if not info or info == ctypes.c_void_p(-1).value:
        return found
    index = 0
    while True:
        iface = _SP_DEVICE_INTERFACE_DATA()
        iface.cbSize = ctypes.sizeof(iface)
        if not setup.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), index, ctypes.byref(iface)):
            break
        index += 1
        need = ctypes.c_ulong(0)
        setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(iface), None, 0, ctypes.byref(need), None)
        if not need.value:
            continue
        buf = ctypes.create_string_buffer(need.value)
        struct.pack_into("<L", buf, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
        if not setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(iface), buf, need.value, None, None):
            continue
        path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
        dev = {"path": path, "vid": 0, "pid": 0, "usage_page": 0, "usage": 0, "in": 0, "feature": 0, "product": ""}
        handle = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights needed for the queries
        if handle and handle != ctypes.c_void_p(-1).value:
            attr = _HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(attr)
            if hid.HidD_GetAttributes(handle, ctypes.byref(attr)):
                dev["vid"], dev["pid"] = attr.VendorID, attr.ProductID
            pre = ctypes.c_void_p()
            if hid.HidD_GetPreparsedData(handle, ctypes.byref(pre)):
                caps = _HIDP_CAPS()
                if hid.HidP_GetCaps(pre, ctypes.byref(caps)) == 0x00110000:
                    dev.update(usage_page=caps.UsagePage, usage=caps.Usage,
                               **{"in": caps.InputReportByteLength, "feature": caps.FeatureReportByteLength})
                hid.HidD_FreePreparsedData(pre)
            name = ctypes.create_unicode_buffer(127)
            if hid.HidD_GetProductString(handle, name, ctypes.sizeof(name)):
                dev["product"] = name.value
            k32.CloseHandle(handle)
        found.append(dev)
    setup.SetupDiDestroyDeviceInfoList(info)
    return found


def run_scan():
    """Print what this PC can see of the paired earbuds, and save it to budstrack_scan.txt.

    Galaxy Buds are handled by BudsTrack already. Earbuds that follow Android's open
    head-tracker standard show up as a HID *sensor* (usage page 0x20); that is the
    hook a future backend would use, so reports from other brands are valuable.
    """
    lines = ["BudsTrack %s scan" % __version__, "", "Paired Bluetooth devices:"]
    for mac, name in paired_devices():
        kind = "Galaxy Buds - supported" if "buds" in name.lower() and "galaxy" in name.lower() else ""
        lines.append("  %s  %-32s %s" % (mac, name, kind))
    devs = hid_devices()
    sensors = [d for d in devs if d["usage_page"] == 0x20]
    lines += ["", "HID devices: %d, of which sensors (usage page 0x20): %d" % (len(devs), len(sensors))]
    for d in devs:
        bluetooth = bluetooth_path(d["path"])
        if d["usage_page"] == 0x20 or bluetooth:
            lines.append("  %s vid=%04X pid=%04X usage=%04X:%04X in=%d feature=%d %s%s" % (
                "SENSOR" if d["usage_page"] == 0x20 else "bt-hid", d["vid"], d["pid"], d["usage_page"], d["usage"],
                d["in"], d["feature"], d["product"], "  <- possible Android head tracker" if d["usage_page"] == 0x20
                and d["usage"] in (0xE1, 0x8A) else ""))
    lines.append("")
    if any(d["usage_page"] == 0x20 and bluetooth_path(d["path"]) for d in devs):
        lines.append("A Bluetooth sensor device is present. Please open an issue with this file attached.")
    else:
        lines.append("No Bluetooth head-tracking sensor is exposed to Windows by the connected earbuds.")
    text = "\n".join(lines)
    print(text)
    try:
        with open(os.path.join(APP_DIR, "budstrack_scan.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return 0


def bluetooth_path(path):
    path = path.lower()
    return "bth" in path or "{00001124-" in path or "{00001812-" in path


if __name__ == "__main__":
    if "--scan" in sys.argv:
        sys.exit(run_scan())
    if "--probe" in sys.argv:
        sys.exit(run_probe())
    if "--sim" in sys.argv:
        sys.exit(run_sim())
    run_gui()
