"""Перевірка нової версії LT-Gen на GitHub (v0.2.0).

Навіщо: програму передають з рук у руки, і людина роками сидить на старій
версії, не знаючи про виправлення. Тому перевірка ПРИМУСОВА (рішення 2026-10-11),
але тиха й ненав'язлива:

- один запит до GitHub API за номером останнього релізу - жодні дані школи не
  передаються (лише стандартні заголовки HTTP);
- у фоновому потоці при старті сервера, щоб не гальмувати запуск;
- не частіше ніж раз на добу (кеш base/onovlennya.json);
- три невдалі спроби - і в цьому сеансі більше не пробуємо, нічого не показуємо
  (школа без інтернету не має бачити ні помилок, ні натяків);
- знайдено новішу - банер висить у ПОТОЧНОМУ сеансі, а для цієї ж версії більше
  не з'являється (pokazano_dlya у кеші); вийде ще новіша - покажемо знову.
Роботу програми це ніяк не блокує."""

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

from version import APP_VERSION

RELIZY_API = "https://api.github.com/repos/LT-Gen/Lesson_Timetable_Generator/releases/latest"
RELIZY_URL = "https://github.com/LT-Gen/Lesson_Timetable_Generator/releases/latest"
TAIMAUT_S = 3
SPROB = 3
PAUZA_MIZH_SPROBAMY_S = 20

# що показати в банері в цьому сеансі (None - нічого)
NOVA_VERSIYA = None


def versiya_chyslamy(tekst):
    """'v0.10.0' -> (0, 10, 0): порівнювати треба числами, рядково 0.10 < 0.9."""
    chastyny = []
    for ch in str(tekst or "").strip().lstrip("vV").split("."):
        tsyfry = "".join(c for c in ch if c.isdigit())
        chastyny.append(int(tsyfry) if tsyfry else 0)
    return tuple(chastyny)


def _prochytaty_kesh(shlyah):
    try:
        with open(shlyah, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _zapysaty_kesh(shlyah, dani):
    try:
        with open(shlyah, "w", encoding="utf-8") as f:
            json.dump(dani, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _ostannya_z_github():
    zapyt = urllib.request.Request(RELIZY_API, headers={
        "User-Agent": f"LT-Gen/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(zapyt, timeout=TAIMAUT_S) as vidpovid:
        return json.load(vidpovid).get("tag_name") or ""


def _perevirka(kesh_shlyah, otrymaty=_ostannya_z_github, pauza=PAUZA_MIZH_SPROBAMY_S):
    global NOVA_VERSIYA
    kesh = _prochytaty_kesh(kesh_shlyah)
    ostannya = None
    try:
        svizhyi = datetime.now() - datetime.fromisoformat(kesh.get("perevireno", "")) < timedelta(days=1)
    except (TypeError, ValueError):
        svizhyi = False
    if svizhyi:
        ostannya = kesh.get("ostannya")
    else:
        for sproba in range(SPROB):
            try:
                ostannya = otrymaty()
                break
            except Exception:   # нема мережі, GitHub недоступний, ліміт - мовчки
                if sproba < SPROB - 1:
                    time.sleep(pauza)
        if not ostannya:
            return
        kesh["perevireno"] = datetime.now().isoformat(timespec="seconds")
        kesh["ostannya"] = ostannya
    if (ostannya and versiya_chyslamy(ostannya) > versiya_chyslamy(APP_VERSION)
            and kesh.get("pokazano_dlya") != ostannya):
        NOVA_VERSIYA = ostannya.lstrip("vV")
        kesh["pokazano_dlya"] = ostannya
    _zapysaty_kesh(kesh_shlyah, kesh)


def zapustyty_perevirku(base_dir):
    """Фоновий потік перевірки - викликається один раз при старті сервера."""
    potik = threading.Thread(
        target=_perevirka, args=(os.path.join(base_dir, "onovlennya.json"),), daemon=True)
    potik.start()
    return potik
