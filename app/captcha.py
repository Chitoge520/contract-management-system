"""Image captcha for the sign-in and registration forms, drawn locally with OpenCV (no outside service).

Each captcha can be used once and expires after a few minutes. Answers are not case sensitive;
characters that are easy to confuse (0/O, 1/I/L) are not used.
"""
import base64
import secrets
import threading
import time

CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
LENGTH = 4
TTL_SEC = 5 * 60
MAX_KEPT = 5000          # unanswered captchas kept in memory at most
W, H = 130, 42

_store = {}              # id -> (answer, expires_at)
_lock = threading.Lock()
_rnd = secrets.SystemRandom()


def _render(text):
    import cv2
    import numpy as np
    img = np.full((H, W, 3), [_rnd.randint(228, 255) for _ in range(3)], np.uint8)
    for _ in range(5):  # light background lines
        cv2.line(img, (_rnd.randint(0, W), _rnd.randint(0, H)), (_rnd.randint(0, W), _rnd.randint(0, H)),
                 [_rnd.randint(150, 210) for _ in range(3)], 1, cv2.LINE_AA)
    fonts = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_COMPLEX]
    x, cw = 8, 28
    for ch in text:
        mask = np.zeros((H, cw), np.uint8)
        cv2.putText(mask, ch, (4, 31), _rnd.choice(fonts), _rnd.uniform(0.9, 1.05), 255, 2, cv2.LINE_AA)
        m = cv2.getRotationMatrix2D((cw / 2, H / 2), _rnd.uniform(-25, 25), 1.0)
        m[1, 2] += _rnd.uniform(-3, 3)
        mask = cv2.warpAffine(mask, m, (cw, H))
        alpha = (mask.astype(np.float32) / 255.0)[..., None]
        color = np.array([_rnd.randint(0, 110) for _ in range(3)], np.float32)
        region = img[:, x:x + cw].astype(np.float32)
        img[:, x:x + cw] = (region * (1 - alpha) + color * alpha).astype(np.uint8)
        x += _rnd.randint(22, 26)
    for _ in range(2):  # dark curves through the characters
        pts = np.array([[i, int(H / 2 + _rnd.uniform(-12, 12))] for i in range(0, W + 1, W // 4)], np.int32)
        cv2.polylines(img, [pts], False, [_rnd.randint(40, 120) for _ in range(3)], 1, cv2.LINE_AA)
    for _ in range(120):  # speckles
        img[_rnd.randrange(H), _rnd.randrange(W)] = [_rnd.randint(0, 255) for _ in range(3)]
    ok, png = cv2.imencode(".png", img)
    return png.tobytes()


def new():
    """Return (captcha_id, data-URL of the PNG image)."""
    text = "".join(_rnd.choice(CHARS) for _ in range(LENGTH))
    cid = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for k in [k for k, (_, exp) in _store.items() if exp < now]:
            del _store[k]
        while len(_store) >= MAX_KEPT:  # drop the oldest
            del _store[next(iter(_store))]
        _store[cid] = (text, now + TTL_SEC)
    return cid, "data:image/png;base64," + base64.b64encode(_render(text)).decode()


def check(cid, answer) -> bool:
    """True if the answer is right. A captcha can only be tried once, right or wrong."""
    with _lock:
        rec = _store.pop(cid or "", None)
    if not rec or rec[1] < time.time():
        return False
    return secrets.compare_digest(rec[0], (answer or "").strip().upper())
