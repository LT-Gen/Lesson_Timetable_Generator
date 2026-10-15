# LT-Gen — генератор розкладу уроків для української школи.
# Copyright (C) 2026  Rogala <https://github.com/Rogala>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
import shutil
import sqlite3
import json
import calendar
from datetime import datetime, timedelta, date

from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort, flash

from db import (
    ukr_klyuch,
    init_db, get_connection, DB_PATH, DEMO_DB_PATH,
    DEMO_REZHYM_AKTYVNYI, prochytaty_rezhym_bazy, zapysaty_rezhym_bazy,
    DEMO_MASTER_PREFIX, DEMO_MASTER_TABLES,
)
from version import APP_VERSION, APP_NAME_UA
import onovlennya
from mon_normy_loader import (
    default_hranychne_navantazhennya,
    default_faktychne_navantazhennya,
    default_maks_urokiv_den,
    default_maks_urokiv_den_z_fizkulturoyu,
)

app = Flask(__name__)
# потрібен для flash() (попередження після дій типу "деякі клітинки матриці
# пропущено") - фіксований, не секретний у сенсі криптографії (локальний
# застосунок для однієї школи, не публічний веб-сервіс), просто щоб Flask не падав
app.secret_key = "school-schedule-generator-local-app"


def klas_nazva(paralel, litera):
    """Формує назву класу: '5-А' якщо є літера, або просто '5', якщо клас єдиний на паралелі."""
    return f"{paralel}-{litera}" if litera else f"{paralel}"


def skorochene_pib(pib):
    """'Іванов Іван Іванович' -> 'Іванов І.І.' - для компактних таблиць розкладу."""
    if not pib:
        return pib
    chastyny = pib.split()
    if len(chastyny) < 2:
        return pib
    prizvyshche = chastyny[0]
    initsialy = "".join(f"{ch[0]}." for ch in chastyny[1:] if ch)
    return f"{prizvyshche} {initsialy}"


# Раніше тут були 4 захардкоджені функції з дефолтними значеннями по паралелі -
# дублювались окремо в generator.py (реальний ризик розсинхрону, виявлений і
# виправлений). Тепер обидва файли читають з одного джерела - data/json/mon_normy.json
# через mon_normy_loader.py (незалежний модуль, без циклічного імпорту).



app.jinja_env.globals["klas_nazva"] = klas_nazva
app.jinja_env.globals["skorochene_pib"] = skorochene_pib
app.jinja_env.globals["default_maks_urokiv_den"] = default_maks_urokiv_den
app.jinja_env.globals["default_maks_urokiv_den_z_fizkulturoyu"] = default_maks_urokiv_den_z_fizkulturoyu
app.jinja_env.globals["default_hranychne_navantazhennya"] = default_hranychne_navantazhennya
app.jinja_env.globals["default_faktychne_navantazhennya"] = default_faktychne_navantazhennya
app.jinja_env.globals["app_name"] = APP_NAME_UA
app.jinja_env.globals["app_version"] = APP_VERSION

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
SCHEDULE_DIR = os.path.join(PROJECT_DIR, "schedule")
BACKUP_DIR = os.path.dirname(DB_PATH)

# Підпис для звіту "нічого не втрачено" після оновлення версії/імпорту бекапу -
# db.py пише файл ОДИН РАЗ під час init_db(), якщо виявлено зміну версії; тут
# читаємо й одразу видаляємо, щоб банер показався користувачу рівно один раз.
# Порядок і людські назви для звіту. Порядок - за змістом (спершу те, що завуч
# вносив руками, потім похідне), а не алфавітний. Таблиця, якої тут немає,
# все одно потрапить у звіт - у кінці й під своїм технічним іменем: db.py бере
# перелік із самої бази, тож ніщо не може мовчки випасти.
_NAZVY_TABLYTS_ZVITU = {
    "klasy": "Класи",
    "predmety": "Предмети",
    "vchyteli": "Вчителі",
    "kabinety": "Кабінети",
    "asystenty": "Асистенти",
    "klas_komplekty": "Клас-комплекти",
    "navchalnyj_plan": "Навчальний план (записи)",
    "mizhpredmetni_grupy": "Міжпредметні групи поділу",
    "pozaurochni_zanyattya": "Позаурочні заняття (каталог)",
    "pozaurochnyj_plan": "Позаурочний план (записи)",
    "grupy": "Групи (профільна школа)",
    "grupy_klasy": "Склад груп",
    "grupa_navchalnyj_plan": "Профілі навчання (записи)",
    "kabinety_predmety": "Прив'язка кабінет-предмет",
    "kabinety_vchyteli": "Прив'язка кабінет-вчитель",
    "asystenty_predmety": "Прив'язка асистент-предмет",
    "predmet_paralel_pravyla": "Правила здвоєних уроків",
    "teacher_availability": "Доступність вчителів",
    "class_availability": "Доступність класів",
    "kabinet_availability": "Доступність кабінетів",
    "nenavchalni_dni": "Ненавчальні дні (календар)",
    "rozklad_varianty": "Збережені варіанти розкладу",
    "rozklad_varianty_uroky": "Уроки збережених варіантів",
    "zakryti_semestry": "Закриті семестри",
    "pidsumky_semestru": "Підсумки закритих семестрів",
    "zatverdzheni_hodyny": "Затверджені річні години",
    "peredachi_hodyn": "Передачі годин іншому вчителеві",
    "peredachi_znimky": "Знімки прочитаного при передачах",
    "dzvinky": "Розклад дзвінків",
    "school_settings": "Налаштування школи",
    "generated_urok": "Згенерований розклад (уроки)",
    "ostannya_generatsiya": "Остання генерація",
    "urok_log": "Журнал замін",
}


def _prochytaty_i_vydalyty_zvit_migratsiyi():
    shlyah = os.path.join(BACKUP_DIR, "ostannya_migratsiya.json")
    if not os.path.isfile(shlyah):
        return None
    try:
        with open(shlyah, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(shlyah)
    except (OSError, json.JSONDecodeError):
        return None

    do_mapa = data.get("do", {}) or {}
    pislya_mapa = data.get("pislya", {}) or {}
    # відомі таблиці - у заданому порядку, невідомі (нові) - у кінці, під
    # технічним іменем: краще незвична назва в списку, ніж мовчазний пропуск
    vidomi = [k for k in _NAZVY_TABLYTS_ZVITU if k in do_mapa or k in pislya_mapa]
    reshta = sorted((set(do_mapa) | set(pislya_mapa)) - set(_NAZVY_TABLYTS_ZVITU))
    poryadok = vidomi + [k for k in reshta if k not in _NAZVY_TABLYTS_ZVITU]

    riadky = []
    ye_vtrata = False
    for kod in poryadok:
        do = do_mapa.get(kod)
        pislya = pislya_mapa.get(kod)
        zmenshylos = do is not None and pislya is not None and pislya < do
        riadky.append({
            "nazva": _NAZVY_TABLYTS_ZVITU.get(kod, kod),
            "do": do, "pislya": pislya, "zmenshylos": zmenshylos,
            "nova": do is None and pislya is not None,
            # таблицю прибрано міграцією (напр. мертва taryfikatsiya у 0.0.37) -
            # це не втрата даних, і порівнювати "стало" з "було" тут нема з чим
            "prybrano": do is not None and pislya is None,
        })
        if zmenshylos:
            ye_vtrata = True
    data["riadky"] = riadky
    data["ye_vtrata"] = ye_vtrata
    return data


# ---------- Непередбачені помилки: файл base/pomylky.log + зрозуміла сторінка ----------
# Без цього завуч бачив голе "Internal Server Error", а трейсбек лишався в
# чорному вікні сервера, яке він не скопіює. Тепер кожен збій дописується у
# файл поруч із базою (з датою, адресою сторінки й повним трейсбеком), а
# користувач бачить сторінку з поясненням, який файл надіслати.
POMYLKY_LOG = os.path.join(BACKUP_DIR, "pomylky.log")
_POMYLKY_LOG_MAKS_BAJT = 512 * 1024   # файл не росте безмежно: старе відрізаємо


def _zapysaty_pomylku(tekst):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if os.path.isfile(POMYLKY_LOG) and os.path.getsize(POMYLKY_LOG) > _POMYLKY_LOG_MAKS_BAJT:
            with open(POMYLKY_LOG, "r", encoding="utf-8", errors="replace") as f:
                hvist = f.read()[-_POMYLKY_LOG_MAKS_BAJT // 2:]
            with open(POMYLKY_LOG, "w", encoding="utf-8") as f:
                f.write(hvist)
        with open(POMYLKY_LOG, "a", encoding="utf-8") as f:
            f.write(tekst)
    except OSError:
        pass  # запис журналу не має сам ставати причиною ще однієї помилки


@app.errorhandler(Exception)
def _nepredbachena_pomylka(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e          # 404/400/405 тощо - звичайна поведінка Flask
    import traceback
    chas = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _zapysaty_pomylku(
        f"\n===== {chas}  {request.method} {request.path}  v{APP_VERSION} =====\n"
        + traceback.format_exc()
    )
    # у вікні сервера трейсбек теж лишається - для розробника
    traceback.print_exc()
    return render_template(
        "pomylka.html", active=None, chas=chas, shlyah=request.path,
        pomylka=f"{type(e).__name__}: {e}", log_fajl=POMYLKY_LOG,
    ), 500


@app.context_processor
def _inject_zvit_migratsiyi():
    return {
        "zvit_migratsiyi": _prochytaty_i_vydalyty_zvit_migratsiyi(),
        "demo_rezhym": DEMO_REZHYM_AKTYVNYI,
        "demo_baza_isnuye": os.path.isfile(DEMO_DB_PATH),
        "nova_versiya": onovlennya.NOVA_VERSIYA,
        "nova_versiya_url": onovlennya.RELIZY_URL,
    }


# ---------- Демо-база / реальна база ----------
# Поруч існують два незалежні файли - base/school.db (реальна школа) і
# base/school-demo.db (навчальний приклад; кладеться в реліз під час збірки).
# Активну задає файл-мітка base/aktyvna_baza.txt (db.py читає раз при старті).
# Перемикання - лише перезапис мітки; жодного копіювання, обидва файли недоторкані.
# Зміна набуває чинності після перезапуску. Генерації демо-даних у застосунку
# НЕМАЄ - демо-база це просто знімок school.db розробника на момент релізу.


@app.route("/baza/pereklyuchyty", methods=["POST"])
def baza_pereklyuchyty():
    rezhym = request.form.get("rezhym", "")
    if rezhym not in ("demo", "realna"):
        abort(400)
    if rezhym == "demo" and not os.path.isfile(DEMO_DB_PATH):
        flash("Демо-база (school-demo.db) відсутня - перемикання неможливе.", "error")
        return redirect(url_for("nalashtuvannya"))
    zapysaty_rezhym_bazy(rezhym)
    if rezhym == prochytaty_rezhym_bazy():  # переконались, що мітку записано
        nazva = "демонстраційну" if rezhym == "demo" else "реальну"
        flash(
            f"Активну базу перемкнено на {nazva}. {_povidomlennya_pro_perezapusk}",
            "success",
        )
    return redirect(url_for("nalashtuvannya"))


DEN_NAZVY = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

# Колір за складністю предмета (RGB) - ті самі відтінки, що й у веб-розкладі (style.css)
VAZHKIST_KOLIR = {
    "vazhkyi": (253, 227, 227),
    "serednii": (253, 246, 224),
    "legkyi": (227, 243, 230),
}

# Стандартний Windows-шрифт з кирилицею - є на кожному ПК зі школи, не залежить
# від того, що встановлено в fpdf2 (там лише латиниця в базових шрифтах)
_ARIAL_REGULAR = r"C:\Windows\Fonts\arial.ttf"
_ARIAL_BOLD = r"C:\Windows\Fonts\arialbd.ttf"


def _perenesty_tekst(pdf, text, max_w):
    """Розбиває текст на рядки по словах так, щоб кожен влазив у ширину max_w (мм)
    ПОТОЧНИМ шрифтом pdf (викликати після set_font для потрібного накреслення/розміру).
    Надто довге окреме слово ріжеться по символах - інакше воно ніколи б не влізло."""
    if not text:
        return [""]
    slova = text.split(" ")
    riadky = []
    potochnyi = ""
    for slovo in slova:
        kandydat = (potochnyi + " " + slovo).strip()
        if not potochnyi or pdf.get_string_width(kandydat) <= max_w:
            potochnyi = kandydat
            continue
        riadky.append(potochnyi)
        potochnyi = slovo
    if potochnyi:
        riadky.append(potochnyi)

    # друга проходка: якщо якийсь рядок все одно надто широкий (одне довге слово
    # без пробілів - напр. довга назва без скорочень) - ріжемо його по символах
    ostatochno = []
    for riadok in riadky:
        while pdf.get_string_width(riadok) > max_w and len(riadok) > 1:
            for i in range(len(riadok) - 1, 0, -1):
                if pdf.get_string_width(riadok[:i]) <= max_w:
                    ostatochno.append(riadok[:i])
                    riadok = riadok[i:]
                    break
            else:
                break
        ostatochno.append(riadok)
    return ostatochno or [""]


def _pidgotuvaty_riadky_komirky(pdf, text, max_w, maks_riadkiv):
    """Переносить текст під ширину max_w, обмежуючи РІВНО maks_riadkiv рядками
    (доповнює порожніми, якщо коротше) - потрібно для фіксованої висоти комірки в
    сітці PDF-розкладу. Обрізає з "…" лише у крайньому рідкісному випадку, коли
    текст не вміщається навіть у відведену кількість рядків."""
    riadky = _perenesty_tekst(pdf, text, max_w)
    if len(riadky) <= maks_riadkiv:
        return riadky + [""] * (maks_riadkiv - len(riadky))
    obrizani = riadky[:maks_riadkiv]
    ostannii = obrizani[-1]
    while pdf.get_string_width(ostannii + "…") > max_w and len(ostannii) > 1:
        ostannii = ostannii[:-1]
    obrizani[-1] = ostannii + "…"
    return obrizani


def _generuvaty_pdf_rozkladu(conn, obraniy_parnist="oboydva"):
    """Генерує один PDF-файл (книжкова орієнтація) - ОДНА СТОРІНКА НА КОЖЕН КЛАС
    (1-11), увесь тижневий розклад класу одразу (рядки - уроки, стовпці - дні), той
    самий вигляд, що й перегляд класу на /rozklad. Зберігає в ту саму папку, що й
    Excel-експорт генератора (SCHEDULE_DIR). Фільтрує по парності (parnyi/neparnyi)
    так само, як перемикач тижня на сторінці - PDF відповідає тому, що зараз видно
    на екрані, а не показує обидва тижні впереміш."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    settings = conn.execute(
        "SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1"
    ).fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    urokiv = settings["urokiv_na_den"] if settings else 8
    DEN_KOROTKO = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    # заголовок кожної сторінки й ім'я файлу мають нести тиждень, ІНАКШЕ файл, раз
    # завантажений, більше нічим не відрізняється від протилежного тижня - раніше
    # це позначалось лише текстом на кнопці "Зберегти PDF" на сторінці, а сам PDF
    # мовчав. Показуємо мітку лише якщо школа взагалі використовує парність - для
    # решти шкіл вона тільки плутала б (як і перемикач тижня в UI).
    maye_parnist = _ye_parnisni_zapysy(conn)
    tyzhden_lyabel = (" (парний тиждень)" if obraniy_parnist == "parnyi" else " (непарний тиждень)") if maye_parnist else ""

    rows_all = conn.execute(
        """
        SELECT gu.klas_id, gu.den, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
               COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
               COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
               p.vazhkist,
               v.pib AS vchytel_pib, kab.nazva AS kabinet_nazva
        FROM generated_urok gu
        LEFT JOIN predmety p ON p.id = gu.predmet_id
        LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
        JOIN vchyteli v ON v.id = gu.vchytel_id
        LEFT JOIN kabinety kab ON kab.id = gu.kabinet_id
        WHERE gu.parnist IN ('oboydva', ?)
        """,
        (obraniy_parnist,),
    ).fetchall()
    cell_map_vsi = {}
    for r in rows_all:
        cell_map_vsi.setdefault((r["klas_id"], r["den"], r["urok"]), []).append(r)

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.add_font("Arial", "", _ARIAL_REGULAR)
    pdf.add_font("Arial", "B", _ARIAL_BOLD)
    pdf.set_auto_page_break(False)

    page_w = pdf.w - 20
    kolonka_urok_w = 14
    kolonka_den_w = (page_w - kolonka_urok_w) / dniv
    # предмет - до 2 рядків, вчитель - 1, кабінет - до 2 (замість обрізання довгий
    # текст переноситься; висота комірки лишається фіксованою - 5 рядкових слотів)
    RIADKIV_PREDMET, RIADKIV_VCHYTEL, RIADKIV_KABINET = 2, 1, 2
    ryadok_tekstu_h = 3.4
    padynh_zverhu = 1.3
    riadok_h = padynh_zverhu * 2 + ryadok_tekstu_h * (RIADKIV_PREDMET + RIADKIV_VCHYTEL + RIADKIV_KABINET)
    zagolovok_h = 8

    for k in all_klasy:
        cell_map = {
            (den, urok): zapysy
            for (klas_id, den, urok), zapysy in cell_map_vsi.items() if klas_id == k["id"]
        }

        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, f"Розклад — Клас {klas_nazva(k['paralel'], k['litera'])}{tyzhden_lyabel}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # ---- заголовок таблиці: "Урок" + дні тижня ----
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(245, 246, 247)
        pdf.set_xy(x0, y0)
        pdf.cell(kolonka_urok_w, zagolovok_h, "Урок", border=1, align="C", fill=True)
        for d in range(1, dniv + 1):
            pdf.cell(kolonka_den_w, zagolovok_h, DEN_KOROTKO[d - 1], border=1, align="C", fill=True)
        pdf.ln(zagolovok_h)

        # ---- рядок на кожен урок ----
        pdf.set_font("Arial", "", 8)
        for urok in range(1, urokiv + 1):
            y_riadka = pdf.get_y()
            if y_riadka + riadok_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                y_riadka = pdf.get_y()
            x = pdf.l_margin
            pdf.set_font("Arial", "B", 9)
            pdf.set_fill_color(245, 246, 247)
            pdf.set_xy(x, y_riadka)
            pdf.cell(kolonka_urok_w, riadok_h, str(urok), border=1, align="C", fill=True)
            pdf.set_font("Arial", "", 8)
            x += kolonka_urok_w
            for d in range(1, dniv + 1):
                zapysy = cell_map.get((d, urok), [])
                kolir = (255, 255, 255)
                if zapysy and zapysy[0]["predmet_id"] and zapysy[0]["vazhkist"]:
                    kolir = VAZHKIST_KOLIR.get(zapysy[0]["vazhkist"], (255, 255, 255))

                # текст переноситься по словах у фіксовану кількість рядкових слотів
                # (предмет - 2, вчитель - 1, кабінет - 2), а не обрізається - довга
                # назва кабінету йде на другий рядок замість наїзду на сусідню комірку.
                # тег парності (парн./непарн.) додається до РЯДКА ВЧИТЕЛЯ, не предмета -
                # ПІБ після skorochene_pib завжди короткий ("Прізвище І.П."), тому тег
                # надійно вміщується
                def _pib_z_tegom(u):
                    pib = skorochene_pib(u["vchytel_pib"]) or ""
                    if u["parnist"] != "oboydva":
                        pib += f" ({'парн.' if u['parnist'] == 'parnyi' else 'непарн.'})"
                    return pib

                # той самий вчитель на обидві підгрупи (див. навчальний план) -
                # генератор планує лише підгрупу 2 як реальний урок; підгрупа 1
                # ніде не запланована, тому тут лише інформаційний допис
                def _predmet_z_pidgrupoyu(u, nazva):
                    return nazva + " (2 підгрупа)" if u["pidgrupa_2_vchytel_id"] else nazva

                def _kabinet_z_dopysom(u, kabinet):
                    return kabinet + " (0 урок 1 підгрупа)" if u["pidgrupa_2_vchytel_id"] else kabinet

                nazva_predmeta, pib_vchytelya, nazva_kabinetu = "", "", ""
                if len(zapysy) == 1:
                    u = zapysy[0]
                    nazva_predmeta = _predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "")
                    pib_vchytelya = _pib_z_tegom(u)
                    nazva_kabinetu = _kabinet_z_dopysom(u, u["kabinet_nazva"] or "")
                elif zapysy:
                    nazva_predmeta = " / ".join(_predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "") for u in zapysy)
                    pib_vchytelya = " / ".join(_pib_z_tegom(u) for u in zapysy)
                    nazva_kabinetu = " / ".join(_kabinet_z_dopysom(u, u["kabinet_nazva"] or "") for u in zapysy if u["kabinet_nazva"])

                max_w_tekstu = kolonka_den_w - 1.5  # невеликий відступ по боках
                pdf.set_font("Arial", "B", 8)
                riadky_predmeta = _pidgotuvaty_riadky_komirky(pdf, nazva_predmeta, max_w_tekstu, RIADKIV_PREDMET)
                pdf.set_font("Arial", "", 7.5)
                riadky_vchytelya = _pidgotuvaty_riadky_komirky(pdf, pib_vchytelya, max_w_tekstu, RIADKIV_VCHYTEL)
                riadky_kabinetu = _pidgotuvaty_riadky_komirky(pdf, nazva_kabinetu, max_w_tekstu, RIADKIV_KABINET)

                pdf.set_fill_color(*kolir)
                pdf.rect(x, y_riadka, kolonka_den_w, riadok_h, style="DF")

                y_tekstu = y_riadka + padynh_zverhu
                for riadok_tekstu in riadky_predmeta:
                    pdf.set_font("Arial", "B", 8)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_vchytelya:
                    pdf.set_font("Arial", "", 7.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_kabinetu:
                    pdf.set_font("Arial", "", 7.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                x += kolonka_den_w
            pdf.set_xy(pdf.l_margin, y_riadka + riadok_h)

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tyzhden_dlya_imeni = f"_{obraniy_parnist}" if maye_parnist else ""
    filename = f"rozklad_klasiv{tyzhden_dlya_imeni}_{timestamp}.pdf"
    filepath = os.path.join(SCHEDULE_DIR, filename)
    pdf.output(filepath)
    return filename


@app.route("/rozklad/pdf", methods=["POST"])
def rozklad_pdf():
    conn = get_connection()
    has_rozklad = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0
    if has_rozklad:
        obraniy_parnist = request.form.get("parnist")
        if obraniy_parnist not in ("parnyi", "neparnyi"):
            obraniy_parnist = _potochna_parnist(conn)
        _generuvaty_pdf_rozkladu(conn, obraniy_parnist)
    conn.close()
    # кнопка є і на "Перегляд розкладу" - повертаємо туди, звідки прийшли
    povernennya = request.form.get("povernennya")
    return redirect(url_for("rozklad") if povernennya == "rozklad" else url_for("home"))


def _generuvaty_pdf_rozkladu_shkoly(conn, obraniy_parnist="oboydva"):
    """Один PDF (альбомна орієнтація), по одній сторінці на КОЖЕН НАВЧАЛЬНИЙ ДЕНЬ
    (5 сторінок при 5-денному тижні) - той самий вигляд, що й таблиця "Поточний
    розклад" на Головній сторінці (рядки - класи, стовпці - уроки), просто одразу
    для всіх днів. НЕ плутати з _generuvaty_pdf_rozkladu (сторінка/клас, весь
    тиждень одразу) - інший розріз, для сторінки "Перегляд розкладу"."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    settings = conn.execute(
        "SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1"
    ).fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    urokiv = settings["urokiv_na_den"] if settings else 8

    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    dzvinky_rows = conn.execute(
        "SELECT urok, pochatok, kinets FROM dzvinky WHERE zmina = 1 ORDER BY urok"
    ).fetchall()
    dzvinky_1_zmina = {r["urok"]: r for r in dzvinky_rows}

    maye_parnist = _ye_parnisni_zapysy(conn)
    tyzhden_lyabel = (" (парний тиждень)" if obraniy_parnist == "parnyi" else " (непарний тиждень)") if maye_parnist else ""

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_margins(8, 8, 8)
    pdf.add_font("Arial", "", _ARIAL_REGULAR)
    pdf.add_font("Arial", "B", _ARIAL_BOLD)
    pdf.set_auto_page_break(False)

    page_w = pdf.w - 16
    kolonka_klas_w = 22
    kolonka_urok_w = (page_w - kolonka_klas_w) / urokiv
    RIADKIV_PREDMET, RIADKIV_VCHYTEL, RIADKIV_KABINET = 2, 1, 2
    ryadok_tekstu_h = 2.7
    padynh_zverhu = 1.0
    riadok_h = padynh_zverhu * 2 + ryadok_tekstu_h * (RIADKIV_PREDMET + RIADKIV_VCHYTEL + RIADKIV_KABINET)
    zagolovok_h = 7

    for den in range(1, dniv + 1):
        rows = conn.execute(
            """
            SELECT gu.klas_id, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
                   COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
                   COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
                   p.vazhkist,
                   v.pib AS vchytel_pib, kab.nazva AS kabinet_nazva
            FROM generated_urok gu
            LEFT JOIN predmety p ON p.id = gu.predmet_id
            LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
            JOIN vchyteli v ON v.id = gu.vchytel_id
            LEFT JOIN kabinety kab ON kab.id = gu.kabinet_id
            WHERE gu.den = ? AND gu.parnist IN ('oboydva', ?)
            """,
            (den, obraniy_parnist),
        ).fetchall()
        cell_map = {}
        for r in rows:
            cell_map.setdefault((r["klas_id"], r["urok"]), []).append(r)

        pdf.add_page()
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 9, f"Розклад — {DEN_NAZVY[den - 1]}{tyzhden_lyabel}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # ---- заголовок таблиці: "Клас" + номери уроків (з часом 1-ї зміни) ----
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("Arial", "B", 9)
        pdf.set_fill_color(245, 246, 247)
        pdf.set_xy(x0, y0)
        pdf.cell(kolonka_klas_w, zagolovok_h, "Клас", border=1, align="C", fill=True)
        for urok in range(1, urokiv + 1):
            dzv = dzvinky_1_zmina.get(urok)
            mitka = str(urok)
            if dzv and dzv["pochatok"] and dzv["kinets"]:
                mitka = f"{urok} ({dzv['pochatok']}-{dzv['kinets']})"
            pdf.cell(kolonka_urok_w, zagolovok_h, mitka, border=1, align="C", fill=True)
        pdf.ln(zagolovok_h)

        # ---- рядок на кожен клас ----
        pdf.set_font("Arial", "", 7)
        for k in all_klasy:
            y_riadka = pdf.get_y()
            if y_riadka + riadok_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                y_riadka = pdf.get_y()
            x = pdf.l_margin
            pdf.set_font("Arial", "B", 8)
            pdf.set_fill_color(245, 246, 247)
            pdf.set_xy(x, y_riadka)
            pdf.cell(kolonka_klas_w, riadok_h, klas_nazva(k["paralel"], k["litera"]), border=1, align="C", fill=True)
            pdf.set_font("Arial", "", 7)
            x += kolonka_klas_w
            for urok in range(1, urokiv + 1):
                zapysy = cell_map.get((k["id"], urok), [])
                kolir = (255, 255, 255)
                if zapysy and zapysy[0]["predmet_id"] and zapysy[0]["vazhkist"]:
                    kolir = VAZHKIST_KOLIR.get(zapysy[0]["vazhkist"], (255, 255, 255))

                # тег парності додається до РЯДКА ВЧИТЕЛЯ (не предмета) - ПІБ після
                # skorochene_pib завжди короткий, тег надійно вміщується; тут це
                # здебільшого лише підстраховка - клітинка й так відфільтрована по
                # obraniy_parnist, тег стосується хіба що змішаних записів (дуже
                # рідкісний випадок кількох записів в одній клітинці різної парності)
                def _pib_z_tegom(u):
                    pib = skorochene_pib(u["vchytel_pib"]) or ""
                    if u["parnist"] != "oboydva":
                        pib += f" ({'парн.' if u['parnist'] == 'parnyi' else 'непарн.'})"
                    return pib

                # той самий вчитель на обидві підгрупи (див. навчальний план) -
                # генератор планує лише підгрупу 2 як реальний урок; підгрупа 1
                # ніде не запланована, тому тут лише інформаційний допис
                def _predmet_z_pidgrupoyu(u, nazva):
                    return nazva + " (2 підгрупа)" if u["pidgrupa_2_vchytel_id"] else nazva

                def _kabinet_z_dopysom(u, kabinet):
                    return kabinet + " (0 урок 1 підгрупа)" if u["pidgrupa_2_vchytel_id"] else kabinet

                nazva_predmeta, pib_vchytelya, nazva_kabinetu = "", "", ""
                if len(zapysy) == 1:
                    u = zapysy[0]
                    nazva_predmeta = _predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "")
                    pib_vchytelya = _pib_z_tegom(u)
                    nazva_kabinetu = _kabinet_z_dopysom(u, u["kabinet_nazva"] or "")
                elif zapysy:
                    nazva_predmeta = " / ".join(_predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "") for u in zapysy)
                    pib_vchytelya = " / ".join(_pib_z_tegom(u) for u in zapysy)
                    nazva_kabinetu = " / ".join(_kabinet_z_dopysom(u, u["kabinet_nazva"] or "") for u in zapysy if u["kabinet_nazva"])

                max_w_tekstu = kolonka_urok_w - 1.5
                pdf.set_font("Arial", "B", 7)
                riadky_predmeta = _pidgotuvaty_riadky_komirky(pdf, nazva_predmeta, max_w_tekstu, RIADKIV_PREDMET)
                pdf.set_font("Arial", "", 6.5)
                riadky_vchytelya = _pidgotuvaty_riadky_komirky(pdf, pib_vchytelya, max_w_tekstu, RIADKIV_VCHYTEL)
                riadky_kabinetu = _pidgotuvaty_riadky_komirky(pdf, nazva_kabinetu, max_w_tekstu, RIADKIV_KABINET)

                pdf.set_fill_color(*kolir)
                pdf.rect(x, y_riadka, kolonka_urok_w, riadok_h, style="DF")

                y_tekstu = y_riadka + padynh_zverhu
                for riadok_tekstu in riadky_predmeta:
                    pdf.set_font("Arial", "B", 7)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_urok_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_vchytelya:
                    pdf.set_font("Arial", "", 6.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_urok_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_kabinetu:
                    pdf.set_font("Arial", "", 6.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_urok_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                x += kolonka_urok_w
            pdf.set_xy(pdf.l_margin, y_riadka + riadok_h)

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tyzhden_dlya_imeni = f"_{obraniy_parnist}" if maye_parnist else ""
    filename = f"rozklad_shkoly{tyzhden_dlya_imeni}_{timestamp}.pdf"
    filepath = os.path.join(SCHEDULE_DIR, filename)
    pdf.output(filepath)
    return filename


@app.route("/rozklad/pdf-shkola", methods=["POST"])
def rozklad_pdf_shkola():
    conn = get_connection()
    has_rozklad = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0
    if has_rozklad:
        obraniy_parnist = request.form.get("parnist")
        if obraniy_parnist not in ("parnyi", "neparnyi"):
            obraniy_parnist = _potochna_parnist(conn)
        _generuvaty_pdf_rozkladu_shkoly(conn, obraniy_parnist)
    conn.close()
    return redirect(url_for("home"))


def _generuvaty_pdf_rozkladu_vchyteliv(conn, obraniy_parnist="oboydva"):
    """Той самий PDF-принцип, що й _generuvaty_pdf_rozkladu, але ОДНА СТОРІНКА НА
    КОЖНОГО ВЧИТЕЛЯ (не на клас): рядки - уроки, стовпці - дні, в клітинці клас
    (не вчитель, бо він уже відомий з заголовка сторінки) + предмет + кабінет."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    settings = conn.execute(
        "SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1"
    ).fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    urokiv = settings["urokiv_na_den"] if settings else 8
    DEN_KOROTKO = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    maye_parnist = _ye_parnisni_zapysy(conn)
    tyzhden_lyabel = (" (парний тиждень)" if obraniy_parnist == "parnyi" else " (непарний тиждень)") if maye_parnist else ""

    rows_all = conn.execute(
        """
        SELECT gu.vchytel_id, gu.den, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
               COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
               COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
               p.vazhkist,
               k.paralel, k.litera, kab.nazva AS kabinet_nazva
        FROM generated_urok gu
        LEFT JOIN predmety p ON p.id = gu.predmet_id
        LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
        JOIN klasy k ON k.id = gu.klas_id
        LEFT JOIN kabinety kab ON kab.id = gu.kabinet_id
        WHERE gu.parnist IN ('oboydva', ?)
        """,
        (obraniy_parnist,),
    ).fetchall()
    cell_map_vsi = {}
    for r in rows_all:
        cell_map_vsi.setdefault((r["vchytel_id"], r["den"], r["urok"]), []).append(r)

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.add_font("Arial", "", _ARIAL_REGULAR)
    pdf.add_font("Arial", "B", _ARIAL_BOLD)
    pdf.set_auto_page_break(False)

    page_w = pdf.w - 20
    kolonka_urok_w = 14
    kolonka_den_w = (page_w - kolonka_urok_w) / dniv
    RIADKIV_PREDMET, RIADKIV_KLAS, RIADKIV_KABINET = 2, 1, 2
    ryadok_tekstu_h = 3.4
    padynh_zverhu = 1.3
    riadok_h = padynh_zverhu * 2 + ryadok_tekstu_h * (RIADKIV_PREDMET + RIADKIV_KLAS + RIADKIV_KABINET)
    zagolovok_h = 8

    for v in all_vchyteli:
        cell_map = {
            (den, urok): zapysy
            for (vchytel_id, den, urok), zapysy in cell_map_vsi.items() if vchytel_id == v["id"]
        }
        if not cell_map:
            continue  # вчитель без жодного уроку - не витрачаємо на нього сторінку

        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, f"Розклад — {v['pib']}{tyzhden_lyabel}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(245, 246, 247)
        pdf.set_xy(x0, y0)
        pdf.cell(kolonka_urok_w, zagolovok_h, "Урок", border=1, align="C", fill=True)
        for d in range(1, dniv + 1):
            pdf.cell(kolonka_den_w, zagolovok_h, DEN_KOROTKO[d - 1], border=1, align="C", fill=True)
        pdf.ln(zagolovok_h)

        pdf.set_font("Arial", "", 8)
        for urok in range(1, urokiv + 1):
            y_riadka = pdf.get_y()
            if y_riadka + riadok_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                y_riadka = pdf.get_y()
            x = pdf.l_margin
            pdf.set_font("Arial", "B", 9)
            pdf.set_fill_color(245, 246, 247)
            pdf.set_xy(x, y_riadka)
            pdf.cell(kolonka_urok_w, riadok_h, str(urok), border=1, align="C", fill=True)
            pdf.set_font("Arial", "", 8)
            x += kolonka_urok_w
            for d in range(1, dniv + 1):
                zapysy = cell_map.get((d, urok), [])
                kolir = (255, 255, 255)
                if zapysy and zapysy[0]["predmet_id"] and zapysy[0]["vazhkist"]:
                    kolir = VAZHKIST_KOLIR.get(zapysy[0]["vazhkist"], (255, 255, 255))

                def _klas_z_tegom(u):
                    klas_txt = klas_nazva(u["paralel"], u["litera"])
                    if u["parnist"] != "oboydva":
                        klas_txt += f" ({'парн.' if u['parnist'] == 'parnyi' else 'непарн.'})"
                    return klas_txt

                # той самий вчитель на обидві підгрупи - генератор планує лише
                # підгрупу 2 як реальний урок; підгрупа 1 - інформаційний допис
                def _predmet_z_pidgrupoyu(u, nazva):
                    return nazva + " (2 підгрупа)" if u["pidgrupa_2_vchytel_id"] else nazva

                def _kabinet_z_dopysom(u, kabinet):
                    return kabinet + " (0 урок 1 підгрупа)" if u["pidgrupa_2_vchytel_id"] else kabinet

                nazva_predmeta, klas_tekst, nazva_kabinetu = "", "", ""
                if len(zapysy) == 1:
                    u = zapysy[0]
                    nazva_predmeta = _predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "")
                    klas_tekst = _klas_z_tegom(u)
                    nazva_kabinetu = _kabinet_z_dopysom(u, u["kabinet_nazva"] or "")
                elif zapysy:
                    nazva_predmeta = " / ".join(_predmet_z_pidgrupoyu(u, u["predmet_skor"] or u["predmet_nazva"] or "") for u in zapysy)
                    klas_tekst = " / ".join(_klas_z_tegom(u) for u in zapysy)
                    nazva_kabinetu = " / ".join(_kabinet_z_dopysom(u, u["kabinet_nazva"] or "") for u in zapysy if u["kabinet_nazva"])

                max_w_tekstu = kolonka_den_w - 1.5
                pdf.set_font("Arial", "B", 8)
                riadky_predmeta = _pidgotuvaty_riadky_komirky(pdf, nazva_predmeta, max_w_tekstu, RIADKIV_PREDMET)
                pdf.set_font("Arial", "", 7.5)
                riadky_klasu = _pidgotuvaty_riadky_komirky(pdf, klas_tekst, max_w_tekstu, RIADKIV_KLAS)
                riadky_kabinetu = _pidgotuvaty_riadky_komirky(pdf, nazva_kabinetu, max_w_tekstu, RIADKIV_KABINET)

                pdf.set_fill_color(*kolir)
                pdf.rect(x, y_riadka, kolonka_den_w, riadok_h, style="DF")

                y_tekstu = y_riadka + padynh_zverhu
                for riadok_tekstu in riadky_predmeta:
                    pdf.set_font("Arial", "B", 8)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_klasu:
                    pdf.set_font("Arial", "", 7.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                for riadok_tekstu in riadky_kabinetu:
                    pdf.set_font("Arial", "", 7.5)
                    pdf.set_xy(x, y_tekstu)
                    pdf.cell(kolonka_den_w, ryadok_tekstu_h, riadok_tekstu, border=0, align="C")
                    y_tekstu += ryadok_tekstu_h
                x += kolonka_den_w
            pdf.set_xy(pdf.l_margin, y_riadka + riadok_h)

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tyzhden_dlya_imeni = f"_{obraniy_parnist}" if maye_parnist else ""
    filename = f"rozklad_vchyteliv{tyzhden_dlya_imeni}_{timestamp}.pdf"
    filepath = os.path.join(SCHEDULE_DIR, filename)
    pdf.output(filepath)
    return filename


@app.route("/rozklad/pdf-vchyteli", methods=["POST"])
def rozklad_pdf_vchyteli():
    conn = get_connection()
    has_rozklad = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0
    if has_rozklad:
        obraniy_parnist = request.form.get("parnist")
        if obraniy_parnist not in ("parnyi", "neparnyi"):
            obraniy_parnist = _potochna_parnist(conn)
        _generuvaty_pdf_rozkladu_vchyteliv(conn, obraniy_parnist)
    conn.close()
    return redirect(url_for("rozklad"))


# ---------- Головна ----------

@app.route("/")
def home():
    conn = get_connection()
    has_rozklad = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0

    school_grid = None
    obraniy_den = None
    obraniy_parnist = None
    ye_parnist = _ye_parnisni_zapysy(conn)
    dniv = 5
    if has_rozklad:
        settings = conn.execute("SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1").fetchone()
        dniv = settings["dniv_na_tyzhden"] if settings else 5
        urokiv = settings["urokiv_na_den"] if settings else 8

        today_weekday = datetime.now().isoweekday()  # 1=Пн ... 7=Нд
        obraniy_den = int(request.args.get("den") or (today_weekday if 1 <= today_weekday <= dniv else 1))
        obraniy_den = max(1, min(dniv, obraniy_den))

        obraniy_parnist = request.args.get("parnist")
        if obraniy_parnist not in ("parnyi", "neparnyi"):
            obraniy_parnist = _potochna_parnist(conn)

        all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
        rows = conn.execute(
            """
            SELECT gu.klas_id, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
                   COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
                   COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
                   p.vazhkist,
                   v.pib AS vchytel_pib, kab.nazva AS kabinet_nazva
            FROM generated_urok gu
            LEFT JOIN predmety p ON p.id = gu.predmet_id
            LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
            JOIN vchyteli v ON v.id = gu.vchytel_id
            LEFT JOIN kabinety kab ON kab.id = gu.kabinet_id
            WHERE gu.den = ? AND gu.parnist IN ('oboydva', ?)
            """,
            (obraniy_den, obraniy_parnist),
        ).fetchall()
        cell_map = {}
        for r in rows:
            cell_map.setdefault((r["klas_id"], r["urok"]), []).append(r)

        dzvinky_rows = conn.execute(
            "SELECT urok, pochatok, kinets FROM dzvinky WHERE zmina = 1 ORDER BY urok"
        ).fetchall()
        dzvinky_1_zmina = {r["urok"]: r for r in dzvinky_rows}

        school_grid = {
            "klasy": all_klasy,
            "urokiv": urokiv,
            "cell_map": cell_map,
            "dzvinky": dzvinky_1_zmina,
        }

    conn.close()

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    pdf_fajly = [f for f in os.listdir(SCHEDULE_DIR) if f.lower().endswith(".pdf")]
    ostannii_pdf = max(pdf_fajly, key=lambda f: os.path.getmtime(os.path.join(SCHEDULE_DIR, f))) if pdf_fajly else None

    return render_template(
        "home.html", active="home",
        school_grid=school_grid, obraniy_den=obraniy_den, dniv=dniv,
        obraniy_parnist=obraniy_parnist, ye_parnist=ye_parnist,
        ostannii_pdf=ostannii_pdf,
    )


@app.route("/schedule/download/<path:filename>")
def schedule_download(filename):
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(400)
    return send_from_directory(SCHEDULE_DIR, filename, as_attachment=True)


@app.route("/schedule/view/<path:filename>")
def schedule_view(filename):
    """Відкриває файл (PDF) у новій вкладці браузера замість примусового
    завантаження - для кнопки "Переглянути останній" на головній."""
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(400)
    return send_from_directory(SCHEDULE_DIR, filename, as_attachment=False)


@app.route("/schedule/delete/<path:filename>", methods=["POST"])
def schedule_delete(filename):
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(400)
    filepath = os.path.join(SCHEDULE_DIR, filename)
    if os.path.isfile(filepath) and filename.lower().endswith((".xlsx", ".pdf")):
        os.remove(filepath)
    return redirect(url_for("generatsiya"))


@app.route("/schedule/delete-all", methods=["POST"])
def schedule_delete_all():
    if os.path.isdir(SCHEDULE_DIR):
        for f in os.listdir(SCHEDULE_DIR):
            if f.lower().endswith((".xlsx", ".pdf")):
                os.remove(os.path.join(SCHEDULE_DIR, f))
    return redirect(url_for("generatsiya"))


# ---------- Класи ----------

def _read_klas_form(form):
    """Справді базові поля форми класу - паралель, літера, кількість учнів,
    домашній кабінет, класний керівник. tip_klasu НЕ тут - похідне поле,
    керується автоматично сторінкою 'Клас-комплекти' (реальна приналежність
    до komplekt_id), не ручним вибором тут (раніше дозволяло розсинхрон -
    виправлено). Решта (уроки/день, навантаження, ООП, мова) - окрема
    сторінка 'Додатково'."""
    domashniy_kabinet_id = form.get("domashniy_kabinet_id") or None
    klasnyi_kerivnyk_id = form.get("klasnyi_kerivnyk_id") or None
    kilkist_chlopciv = int(form.get("kilkist_chlopciv") or 0)
    kilkist_divchat = int(form.get("kilkist_divchat") or 0)
    return {
        "paralel": int(form["paralel"]),
        # до 4 знаків: профільні класи бувають 10-АФ, 10-ЛК, 10-Іп
        "litera": form.get("litera", "").strip()[:4] or None,
        "kilkist_uchniv": kilkist_chlopciv + kilkist_divchat,
        "kilkist_chlopciv": kilkist_chlopciv,
        "kilkist_divchat": kilkist_divchat,
        "domashniy_kabinet_id": int(domashniy_kabinet_id) if domashniy_kabinet_id else None,
        "klasnyi_kerivnyk_id": int(klasnyi_kerivnyk_id) if klasnyi_kerivnyk_id else None,
    }


def _read_klas_form_dodatkovo(form):
    """Розширені поля класу - максимум уроків/день, навантаження, мова навчання,
    класний керівник."""
    maks_urokiv_den = form.get("maks_urokiv_den") or None
    maks_urokiv_den_z_fizkulturoyu = form.get("maks_urokiv_den_z_fizkulturoyu") or None
    hranychne_navantazhennya = form.get("hranychne_navantazhennya") or None
    faktychne_navantazhennya = form.get("faktychne_navantazhennya") or None
    klasnyi_kerivnyk_id = form.get("klasnyi_kerivnyk_id") or None
    return {
        "maks_urokiv_den": int(maks_urokiv_den) if maks_urokiv_den else None,
        "maks_urokiv_den_z_fizkulturoyu": int(maks_urokiv_den_z_fizkulturoyu) if maks_urokiv_den_z_fizkulturoyu else None,
        "hranychne_navantazhennya": int(hranychne_navantazhennya) if hranychne_navantazhennya else None,
        "faktychne_navantazhennya": int(faktychne_navantazhennya) if faktychne_navantazhennya else None,
        "mova_navchannya": form.get("mova_navchannya", "").strip() or None,
        "klasnyi_kerivnyk_id": int(klasnyi_kerivnyk_id) if klasnyi_kerivnyk_id else None,
    }



@app.route("/klasy/seed", methods=["POST"])
def klasy_seed():
    conn = get_connection()
    settings = conn.execute("SELECT tip_zakladu FROM school_settings WHERE id = 1").fetchone()
    tip_zakladu = settings["tip_zakladu"] if settings else "stara_shkola"
    p_vid, p_do = diapazon_paraleley_zakladu(tip_zakladu)

    created = 0
    for paralel in range(p_vid, p_do + 1):
        existing = conn.execute(
            "SELECT id FROM klasy WHERE paralel = ? AND litera IS NULL", (paralel,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO klasy (paralel, litera, kilkist_uchniv, tip_klasu) VALUES (?, NULL, 0, 'zvychajnyj')",
                (paralel,),
            )
            created += 1
    conn.commit()
    conn.close()
    flash(f"Створено класів: {created}." if created else "Нових класів не створено — на кожній паралелі клас без літери вже є.",
          "success")
    return redirect(url_for("klasy_list"))


def _zavantazhyty_komplekty_z_chlenamy(conn):
    """Список клас-комплектів з назвами класів-членів - спільна логіка для
    списку комплектів і вибору комплекту у формі позаурочного плану."""
    komplekty = conn.execute("SELECT * FROM klas_komplekty ORDER BY nazva COLLATE UKR").fetchall()
    result = []
    for kk in komplekty:
        chleny = conn.execute(
            "SELECT id, paralel, litera FROM klasy WHERE komplekt_id = ? ORDER BY paralel", (kk["id"],)
        ).fetchall()
        row = dict(kk)
        row["chleny"] = [klas_nazva(c["paralel"], c["litera"]) for c in chleny]
        row["kilkist_chleniv"] = len(chleny)
        result.append(row)
    return result


@app.route("/klas-komplekty")
def klas_komplekty_list():
    conn = get_connection()
    result = _zavantazhyty_komplekty_z_chlenamy(conn)
    conn.close()
    return render_template("klas_komplekty_list.html", active="klas_komplekty", komplekty=result)


def _pryznachyty_klasnogo_kerivnyka(conn, chleny_ids, vchytel_id):
    """Призначає вчителя на класні предмети (POCHATKOVI_KLASNI_PREDMETY) для ВСІХ
    класів групи - потрібно, щоб синхронізація клас-комплекту в генераторі
    спрацювала (вона вимагає ОДНАКОВОГО вчителя для того самого предмета в різних
    класах групи, інакше кожен клас лишається із власним класоводом). Також
    записує того самого вчителя в klasy.klasnyi_kerivnyk_id кожного члена -
    для відображення (довідкове поле), не лише для навчального плану."""
    if not vchytel_id or not chleny_ids:
        return
    placeholders_klasy = ",".join("?" for _ in chleny_ids)
    conn.execute(
        f"UPDATE klasy SET klasnyi_kerivnyk_id = ? WHERE id IN ({placeholders_klasy})",
        (vchytel_id, *chleny_ids),
    )
    from mon_normy_loader import pochatkovi_klasni_predmety
    nazvy = pochatkovi_klasni_predmety()
    if not nazvy:
        return
    placeholders_predmety = ",".join("?" for _ in nazvy)
    conn.execute(
        f"""
        UPDATE navchalnyj_plan SET vchytel_id = ?
        WHERE klas_id IN ({placeholders_klasy})
          AND predmet_id IN (SELECT id FROM predmety WHERE nazva IN ({placeholders_predmety}))
        """,
        (vchytel_id, *chleny_ids, *nazvy),
    )


@app.route("/klas-komplekty/new", methods=["GET", "POST"])
def klas_komplekty_new():
    conn = get_connection()
    # лише класи паралелей 1-4, ще не в жодному комплекті - кандидати на членство
    dostupni_klasy = conn.execute(
        "SELECT id, paralel, litera FROM klasy WHERE paralel BETWEEN 1 AND 4 AND komplekt_id IS NULL "
        "ORDER BY paralel, litera COLLATE UKR"
    ).fetchall()
    vsi_vchyteli = conn.execute("SELECT id, pib FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    errors = []
    if request.method == "POST":
        nazva = request.form.get("nazva", "").strip()
        chleny_ids = [int(x) for x in request.form.getlist("chleny_ids")]
        klasnyi_kerivnyk_id = request.form.get("klasnyi_kerivnyk_id") or None
        if klasnyi_kerivnyk_id:
            klasnyi_kerivnyk_id = int(klasnyi_kerivnyk_id)
        if not nazva:
            errors.append("Назва клас-комплекту обов'язкова.")
        if len(chleny_ids) < 2 or len(chleny_ids) > 4:
            errors.append("Клас-комплект повинен об'єднувати від 2 до 4 класів (норматив МОН).")

        if not errors:
            cur = conn.execute(
                "INSERT INTO klas_komplekty (nazva, klasnyi_kerivnyk_id) VALUES (?, ?)",
                (nazva, klasnyi_kerivnyk_id),
            )
            komplekt_id = cur.lastrowid
            placeholders = ",".join("?" for _ in chleny_ids)
            conn.execute(
                f"UPDATE klasy SET komplekt_id = ?, tip_klasu = 'komplekt' WHERE id IN ({placeholders})",
                (komplekt_id, *chleny_ids),
            )
            _pryznachyty_klasnogo_kerivnyka(conn, chleny_ids, klasnyi_kerivnyk_id)
            conn.commit()
            conn.close()
            return redirect(url_for("klas_komplekty_list"))

        conn.close()
        return render_template(
            "klas_komplekty_form.html", active="klas_komplekty", komplekt=None,
            dostupni_klasy=dostupni_klasy, obrani_ids=chleny_ids, vsi_vchyteli=vsi_vchyteli, errors=errors,
        )

    conn.close()
    return render_template(
        "klas_komplekty_form.html", active="klas_komplekty", komplekt=None,
        dostupni_klasy=dostupni_klasy, obrani_ids=[], vsi_vchyteli=vsi_vchyteli, errors=[],
    )


@app.route("/klas-komplekty/<int:komplekt_id>/edit", methods=["GET", "POST"])
def klas_komplekty_edit(komplekt_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM klas_komplekty WHERE id = ?", (komplekt_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    # кандидати: класи без комплекту АБО вже в цьому самому комплекті (щоб лишались обраними)
    dostupni_klasy = conn.execute(
        "SELECT id, paralel, litera FROM klasy WHERE paralel BETWEEN 1 AND 4 "
        "AND (komplekt_id IS NULL OR komplekt_id = ?) ORDER BY paralel, litera COLLATE UKR",
        (komplekt_id,),
    ).fetchall()
    potochni_chleny = conn.execute(
        "SELECT id FROM klasy WHERE komplekt_id = ?", (komplekt_id,)
    ).fetchall()
    potochni_ids = [c["id"] for c in potochni_chleny]
    vsi_vchyteli = conn.execute("SELECT id, pib FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()

    errors = []
    if request.method == "POST":
        nazva = request.form.get("nazva", "").strip()
        chleny_ids = [int(x) for x in request.form.getlist("chleny_ids")]
        klasnyi_kerivnyk_id = request.form.get("klasnyi_kerivnyk_id") or None
        if klasnyi_kerivnyk_id:
            klasnyi_kerivnyk_id = int(klasnyi_kerivnyk_id)
        if not nazva:
            errors.append("Назва клас-комплекту обов'язкова.")
        if len(chleny_ids) < 2 or len(chleny_ids) > 4:
            errors.append("Клас-комплект повинен об'єднувати від 2 до 4 класів (норматив МОН).")

        if not errors:
            conn.execute(
                "UPDATE klasy SET komplekt_id = NULL, tip_klasu = 'zvychajnyj' WHERE komplekt_id = ?",
                (komplekt_id,),
            )
            conn.execute(
                "UPDATE klas_komplekty SET nazva = ?, klasnyi_kerivnyk_id = ? WHERE id = ?",
                (nazva, klasnyi_kerivnyk_id, komplekt_id),
            )
            placeholders = ",".join("?" for _ in chleny_ids)
            conn.execute(
                f"UPDATE klasy SET komplekt_id = ?, tip_klasu = 'komplekt' WHERE id IN ({placeholders})",
                (komplekt_id, *chleny_ids),
            )
            _pryznachyty_klasnogo_kerivnyka(conn, chleny_ids, klasnyi_kerivnyk_id)
            conn.commit()
            conn.close()
            return redirect(url_for("klas_komplekty_list"))

        conn.close()
        return render_template(
            "klas_komplekty_form.html", active="klas_komplekty", komplekt=dict(existing),
            dostupni_klasy=dostupni_klasy, obrani_ids=chleny_ids, vsi_vchyteli=vsi_vchyteli, errors=errors,
        )

    conn.close()
    return render_template(
        "klas_komplekty_form.html", active="klas_komplekty", komplekt=dict(existing),
        dostupni_klasy=dostupni_klasy, obrani_ids=potochni_ids, vsi_vchyteli=vsi_vchyteli, errors=[],
    )


@app.route("/klas-komplekty/<int:komplekt_id>/delete", methods=["POST"])
def klas_komplekty_delete(komplekt_id):
    conn = get_connection()
    # члени лишаються звичайними класами. НЕ покладаємось лише на ON DELETE SET NULL
    # бази (виявилось ненадійним на мігрованих базах - колонка додана без цього
    # правила, і SQLite ALTER TABLE не завжди коректно застосовує його заднім числом)
    # - очищаємо явно тут, незалежно від каскаду.
    conn.execute(
        "UPDATE klasy SET tip_klasu = 'zvychajnyj', komplekt_id = NULL WHERE komplekt_id = ?",
        (komplekt_id,),
    )
    conn.execute("DELETE FROM klas_komplekty WHERE id = ?", (komplekt_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("klas_komplekty_list"))


@app.route("/klasy")
def klasy_list():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT k.*, kab.nazva AS kabinet_nazva
        FROM klasy k
        LEFT JOIN kabinety kab ON kab.id = k.domashniy_kabinet_id
        ORDER BY k.paralel, k.litera COLLATE UKR
        """
    ).fetchall()
    vykorystani_ids = {
        r["klas_id"] for r in conn.execute("SELECT DISTINCT klas_id FROM navchalnyj_plan").fetchall()
    }
    settings = conn.execute("SELECT tip_zakladu FROM school_settings WHERE id = 1").fetchone()
    tip_zakladu = settings["tip_zakladu"] if settings else "stara_shkola"
    p_vid_zakladu, p_do_zakladu = diapazon_paraleley_zakladu(tip_zakladu)
    conn.close()
    conn = get_connection()
    result = []
    for k in rows:
        row = dict(k)
        row["u_plani"] = k["id"] in vykorystani_ids
        row["poza_zakladom"] = k["paralel"] < p_vid_zakladu or k["paralel"] > p_do_zakladu
        # що зникне разом із класом - у текст підтвердження видалення
        chastyny = []
        for sql, opys in (
            ("SELECT COUNT(*) c FROM navchalnyj_plan WHERE klas_id = ?", "рядків навчального плану"),
            ("SELECT COUNT(*) c FROM pozaurochnyj_plan WHERE klas_id = ?", "позаурочних"),
            ("SELECT COUNT(*) c FROM asystenty WHERE klas_id = ?", "асистентів"),
            ("SELECT COUNT(*) c FROM generated_urok WHERE klas_id = ?", "уроків у поточному розкладі"),
        ):
            n = conn.execute(sql, (k["id"],)).fetchone()["c"]
            if n:
                chastyny.append(f"{n} {opys}")
        row["naslidky"] = "; ".join(chastyny)
        result.append(row)
    conn.close()
    ye_poza_zakladom = any(r["poza_zakladom"] for r in result)
    return render_template("klasy_list.html", active="klasy", klasy=result,
                           ye_poza_zakladom=ye_poza_zakladom,
                           p_vid_zakladu=p_vid_zakladu, p_do_zakladu=p_do_zakladu)


@app.route("/klasy/new", methods=["GET", "POST"])
def klasy_new():
    conn = get_connection()
    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    vsi_vchyteli = conn.execute("SELECT id, pib FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    school_row = conn.execute("SELECT mova_navchannya FROM school_settings WHERE id = 1").fetchone()
    school_mova = school_row["mova_navchannya"] if school_row else "Українська"
    errors = []
    if request.method == "POST":
        data = _read_klas_form(request.form)

        try:
            conn.execute(
                """
                INSERT INTO klasy
                    (paralel, litera, kilkist_uchniv, kilkist_chlopciv, kilkist_divchat,
                     domashniy_kabinet_id, klasnyi_kerivnyk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["paralel"], data["litera"], data["kilkist_uchniv"],
                    data["kilkist_chlopciv"], data["kilkist_divchat"],
                    data["domashniy_kabinet_id"], data["klasnyi_kerivnyk_id"],
                ),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("klasy_list"))
        except sqlite3.IntegrityError:
            conn.close()
            nazva = klas_nazva(data['paralel'], data['litera'])
            errors = [f"Клас \"{nazva}\" вже існує. Оберіть іншу паралель або літеру."]

        return render_template(
            "klasy_form.html", active="klasy", klas=data, all_kabinety=all_kabinety,
            vsi_vchyteli=vsi_vchyteli, school_mova=school_mova, errors=errors,
        )

    conn.close()
    return render_template(
        "klasy_form.html", active="klasy", klas=None, all_kabinety=all_kabinety,
        vsi_vchyteli=vsi_vchyteli, school_mova=school_mova, errors=[],
    )


@app.route("/klasy/<int:klas_id>/edit", methods=["GET", "POST"])
def klasy_edit(klas_id):
    conn = get_connection()
    existing = conn.execute(
        """
        SELECT k.*, kk.nazva AS komplekt_nazva
        FROM klasy k
        LEFT JOIN klas_komplekty kk ON kk.id = k.komplekt_id
        WHERE k.id = ?
        """,
        (klas_id,),
    ).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    vsi_vchyteli = conn.execute("SELECT id, pib FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    school_row = conn.execute("SELECT mova_navchannya FROM school_settings WHERE id = 1").fetchone()
    school_mova = school_row["mova_navchannya"] if school_row else "Українська"
    e_komplekti = existing["komplekt_id"] is not None
    errors = []
    if request.method == "POST":
        data = _read_klas_form(request.form)

        try:
            if e_komplekti:
                # клас у клас-комплекті - класного керівника тут НЕ чіпаємо, він
                # керується виключно сторінкою "Клас-комплекти" (де й реально
                # призначається всім членам групи одночасно)
                conn.execute(
                    """
                    UPDATE klasy SET
                        paralel = ?, litera = ?, kilkist_uchniv = ?, kilkist_chlopciv = ?,
                        kilkist_divchat = ?, domashniy_kabinet_id = ?
                    WHERE id = ?
                    """,
                    (
                        data["paralel"], data["litera"], data["kilkist_uchniv"],
                        data["kilkist_chlopciv"], data["kilkist_divchat"],
                        data["domashniy_kabinet_id"], klas_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE klasy SET
                        paralel = ?, litera = ?, kilkist_uchniv = ?, kilkist_chlopciv = ?,
                        kilkist_divchat = ?, domashniy_kabinet_id = ?, klasnyi_kerivnyk_id = ?
                    WHERE id = ?
                    """,
                    (
                        data["paralel"], data["litera"], data["kilkist_uchniv"],
                        data["kilkist_chlopciv"], data["kilkist_divchat"],
                        data["domashniy_kabinet_id"], data["klasnyi_kerivnyk_id"], klas_id,
                    ),
                )
            conn.commit()
            conn.close()
            return redirect(url_for("klasy_list"))
        except sqlite3.IntegrityError:
            nazva = klas_nazva(data['paralel'], data['litera'])
            errors = [f"Клас \"{nazva}\" вже існує. Оберіть іншу паралель або літеру."]

        conn.close()
        data["id"] = klas_id
        data["komplekt_nazva"] = existing["komplekt_nazva"]
        return render_template(
            "klasy_form.html", active="klasy", klas=data, all_kabinety=all_kabinety,
            vsi_vchyteli=vsi_vchyteli, school_mova=school_mova, errors=errors,
        )

    conn.close()
    return render_template(
        "klasy_form.html", active="klasy", klas=dict(existing), all_kabinety=all_kabinety,
        vsi_vchyteli=vsi_vchyteli, school_mova=school_mova, errors=[],
    )


@app.route("/klasy/<int:klas_id>/dodatkovo", methods=["GET", "POST"])
def klasy_dodatkovo(klas_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM klasy WHERE id = ?", (klas_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    school_row = conn.execute("SELECT mova_navchannya FROM school_settings WHERE id = 1").fetchone()
    school_mova = school_row["mova_navchannya"] if school_row else "Українська"
    vsi_vchyteli = conn.execute("SELECT id, pib FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()

    errors = []
    if request.method == "POST":
        data = _read_klas_form_dodatkovo(request.form)

        if not errors:
            conn.execute(
                """
                UPDATE klasy SET
                    maks_urokiv_den = ?, maks_urokiv_den_z_fizkulturoyu = ?,
                    hranychne_navantazhennya = ?, faktychne_navantazhennya = ?,
                    mova_navchannya = ?, klasnyi_kerivnyk_id = ?
                WHERE id = ?
                """,
                (
                    data["maks_urokiv_den"], data["maks_urokiv_den_z_fizkulturoyu"],
                    data["hranychne_navantazhennya"], data["faktychne_navantazhennya"],
                    data["mova_navchannya"], data["klasnyi_kerivnyk_id"], klas_id,
                ),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("klasy_list"))

        # помилка валідації - показуємо щойно введені дані, не старі збережені
        conn.close()
        data["id"] = klas_id
        data["paralel"] = existing["paralel"]
        return render_template(
            "klasy_dodatkovo.html", active="klasy", klas=data,
            school_mova=school_mova, vsi_vchyteli=vsi_vchyteli, errors=errors,
        )

    conn.close()
    return render_template(
        "klasy_dodatkovo.html", active="klasy", klas=dict(existing),
        school_mova=school_mova, vsi_vchyteli=vsi_vchyteli, errors=[],
    )


@app.route("/klasy/<int:klas_id>/delete", methods=["POST"])
def klasy_delete(klas_id):
    conn = get_connection()
    conn.execute("DELETE FROM klasy WHERE id = ?", (klas_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("klasy_list"))


# ---------- Розділи-заглушки (у розробці) ----------

def _read_predmet_form_basic(form):
    """Базові поля - форма створення/редагування предмета. НЕ чіпає розширені
    поля (складність, коди тощо) - вони на окремій сторінці 'Додатково', щоб
    випадково не скинути їх при звичайному редагуванні назви/скорочення."""
    podil_pravylo = form.get("podil_pravylo", "nikoly")
    return {
        "nazva": form["nazva"].strip(),
        "skorochennya": form.get("skorochennya", "").strip() or None,
        "zdvoyeni_urok": 1 if form.get("zdvoyeni_urok") == "on" else 0,
        "podil_pravylo": podil_pravylo if podil_pravylo in ("avto", "nikoly") else "nikoly",
    }


def _read_predmet_form_dodatkovo(form):
    """Розширені поля - окрема сторінка 'Додатково' (складність, коди, обмеження
    за днями тощо). Викликається ОКРЕМО від базової форми."""
    zaboroneni = form.get("zaboroneni_uroky", "").strip()
    fiksovanyi_den = form.get("fiksovanyi_den") or None
    fiksovanyi_urok = form.get("fiksovanyi_urok") or None
    return {
        "zaboroneni_uroky": zaboroneni or None,
        "min_promizhok_dni": int(form["min_promizhok_dni"]) if form.get("min_promizhok_dni") else None,
        "fiksovanyi_den": int(fiksovanyi_den) if fiksovanyi_den else None,
        "fiksovanyi_urok": int(fiksovanyi_urok) if fiksovanyi_urok else None,
        "ostannim_urokom": 1 if form.get("ostannim_urokom") == "on" else 0,
        "kategoria": form.get("kategoria", "").strip() or None,
        "osvitnya_galuz": form.get("osvitnya_galuz", "").strip() or None,
        "vrahovuyetsya_u_hranychnomu": 1 if form.get("vrahovuyetsya_u_hranychnomu") == "on" else 0,
        "vazhkist": form.get("vazhkist", "serednii"),
        "normovanyi_kod": form.get("normovanyi_kod", "").strip() or None,
        "spets_kabinet_lyshe_pidgrupa1": 1 if form.get("spets_kabinet_lyshe_pidgrupa1") == "on" else 0,
    }


# Гігієнічна шкала важкості предметів (за специфікацією timetable_generator_constraints.md,
# категорії C1/C2/C3) - глобальна класифікація. Виняток: "Українська мова" в 1-4 класах
# рахується як C1 (важка) через фізичне навантаження письма й формування навички - це
# віковий override, застосовується окремо в generator.py, не тут.
# Тип закладу (за реформою НУШ) - визначає реальний діапазон паралелей і, якщо заклад
# охоплює кілька рівнів, дозволяє генерувати розклад ОКРЕМО по кожному (кнопки/лічильники
# на "Генерації розкладу" - повністю незалежні одна від одної незалежно від їх кількості).
# Гімназія (5-9) і ліцей (10-12) - ОКРЕМІ рівні, хоч тривалість уроку в них однакова
# (45хв) - у ліцею геть інша структура плану (профільні предмети), тому користувач
# свідомо генерує їх окремо, а не через спільну тривалість уроку.
# Кожен рівень: (мітка, min_paralel, max_paralel)
from mon_normy_loader import tip_zakladu_options, diapazon_paraleley_zakladu
TIP_ZAKLADU_OPTIONS = tip_zakladu_options()
# Раніше тут було 6 захардкоджених довідників (типовий перелік предметів, складність,
# скорочення, освітні галузі, типи закладу) - лише в app.py, без дублювання в
# generator.py, але все одно варто в одному місці з рештою нормативів МОН.

app.jinja_env.globals["TIP_ZAKLADU_OPTIONS"] = TIP_ZAKLADU_OPTIONS

# Тип закладу в UI задається 4 незалежними чекбоксами (Початкова/Гімназія/Ліцей/
# "1-11 стара школа") замість одного випадного списку з 7 назв - "1-11" взаємовиключний
# з рештою (несумісна стара структура), а комбінація трьох інших однозначно мапиться
# на один із існуючих кодів (значення в БД лишається тим самим, змінюється лише форма
# введення).
_TZ_KOMBINATSII = {
    (True, False, False): "pochatkova",
    (False, True, False): "gimnaziya",
    (True, True, False): "gimnaziya_z_pochatkovoyu",
    (False, False, True): "liczei",
    (False, True, True): "liczei_z_gimnaziyeyu",
    (True, True, True): "povnyi_liczei",
}


def _tip_zakladu_z_checkboxiv(poch, gim, lic, stara):
    """(poch, gim, lic, stara) з форми -> (код tip_zakladu, чи застосовано fallback).
    Якщо stara=True або нічого не позначено - повертає 'stara_shkola'."""
    if stara or not (poch or gim or lic):
        return "stara_shkola", not (poch or gim or lic or stara)
    return _TZ_KOMBINATSII.get((poch, gim, lic), "stara_shkola"), False


def _tip_zakladu_do_checkboxiv(tip_zakladu):
    """Код tip_zakladu з БД -> стан 4 чекбоксів для початкового рендеру форми."""
    if tip_zakladu == "stara_shkola":
        return {"pochatkova": False, "gimnaziya": False, "liczei": False, "stara": True}
    for (poch, gim, lic), kod in _TZ_KOMBINATSII.items():
        if kod == tip_zakladu:
            return {"pochatkova": poch, "gimnaziya": gim, "liczei": lic, "stara": False}
    return {"pochatkova": False, "gimnaziya": False, "liczei": False, "stara": True}


# 3 незалежні прапорці рівня (Початкова/Гімназія/Ліцей) на вчителі й кабінеті -
# захист від помилки призначення в багаторівневій школі + компактні колонки у
# списках. (кол. в БД, повна назва рівня як у tip_zakladu_options, коротка мітка колонки)
_RIVEN_KOLONKY = [
    ("riven_pochatkova", "Початкова школа", "Поч."),
    ("riven_gimnaziya", "Гімназія", "Гімн."),
    ("riven_liceum", "Ліцей", "Ліцей"),
]


def _aktyvni_riven_kolonky(tip_zakladu):
    """Які з 3 колонок-прапорців показувати для поточного типу закладу - лише якщо
    в закладу активовано 2+ рівні (той самий поріг, що й окремі генератори
    розкладу на "Генерації"). Для одного рівня чи "1-11 стара школа" (завжди рівно
    1 "рівень", з іншою назвою) прапорці ніде не показуються - там вони нічого не
    додають, бо іншого рівня просто не існує."""
    rivni = TIP_ZAKLADU_OPTIONS.get(tip_zakladu, TIP_ZAKLADU_OPTIONS["stara_shkola"])["rivni"]
    if len(rivni) < 2:
        return []
    nazvy_rivniv = {r[0] for r in rivni}
    return [kol for kol in _RIVEN_KOLONKY if kol[1] in nazvy_rivniv]


def _potochnyi_tip_zakladu(conn):
    row = conn.execute("SELECT tip_zakladu FROM school_settings WHERE id = 1").fetchone()
    return row["tip_zakladu"] if row else "stara_shkola"


def _read_riven_prapory(form, riven_kolonky, existing):
    """Читає 3 чекбокси рівня з форми. Колонку, яку для цього типу закладу не
    показували (не входить у riven_kolonky), НІКОЛИ не звужуємо мовчки - лишаємо
    попереднє значення запису (а для нового запису - типово увімкнено)."""
    shown = {kod for kod, _, _ in riven_kolonky}
    result = {}
    for kod, _, _ in _RIVEN_KOLONKY:
        if kod in shown:
            result[kod] = 1 if form.get(kod) else 0
        else:
            result[kod] = existing[kod] if existing is not None and kod in existing.keys() else 1
    return result


def _klasy_poza_zakladom(conn, tip_zakladu):
    """Класи, чия паралель випадає поза діапазоном поточного типу закладу (напр.
    користувач понизив рівень із "Ліцей" до "Гімназія", а класи 10-11 лишились) -
    список рядків klasy, відсортований за паралеллю."""
    p_vid, p_do = diapazon_paraleley_zakladu(tip_zakladu)
    return conn.execute(
        "SELECT * FROM klasy WHERE paralel < ? OR paralel > ? ORDER BY paralel, litera COLLATE UKR",
        (p_vid, p_do),
    ).fetchall()


# --- демо-школа під обраний тип закладу -------------------------------------
# Реліз віддає ОДИН файл school-demo.db з повною школою 1-11. Якщо в завуча
# початкова школа або гімназія, він на "Налаштуваннях школи" міняє тип - і в
# ДЕМО-режимі демо-дані перебудовуються під цей діапазон паралелей. Джерело -
# знімок _demo_master_* у тій самій базі (див. db.py), тому повний набір 1-11
# ніколи не втрачається: повернути його = обрати "1-11 (стара школа)".
# На РЕАЛЬНІЙ базі цей механізм не працює в принципі (запобіжник нижче).

# похідні таблиці: після зміни складу класів старий розклад/звіти стосуються
# вилучених класів, тож чистяться повністю
_DEMO_POHIDNI_TABLYCI = (
    "generated_urok", "ostannya_generatsiya", "urok_log",
    "rozklad_varianty_uroky", "rozklad_varianty",
    "class_availability", "teacher_availability", "kabinet_availability",
    "grupa_navchalnyj_plan", "grupy_klasy", "grupy",
    "pidsumky_semestru", "zakryti_semestry",
    "peredachi_znimky", "peredachi_hodyn", "zatverdzheni_hodyny",
)


def _demo_master_ye(conn):
    """Чи несе поточна база знімок-майстер (стара демо-база до v0.0.29 - ні)."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (DEMO_MASTER_PREFIX + "klasy",),
    ).fetchone() is not None


def _spilni_kolonky(conn, tablytsya):
    """Колонки, спільні для робочої таблиці й її майстер-копії. Знімок робився
    на схемі свого часу; якщо застосунок відтоді додав колонку, наливаємо лише
    спільні (нова лишиться з DEFAULT), а не падаємо на SELECT *."""
    robochi = [r["name"] for r in conn.execute(f"PRAGMA table_info({tablytsya})")]
    majstra = {r["name"] for r in
               conn.execute(f"PRAGMA table_info({DEMO_MASTER_PREFIX + tablytsya})")}
    return [k for k in robochi if k in majstra]


def _nalyty_demo_z_maistra(conn, p_vid, p_do):
    """Перезаливає робочі таблиці демо-даними з майстра, лишаючи тільки класи
    паралелей p_vid..p_do і все, що на них тримається. Без перевірки режиму -
    її робить _pidlashtuvaty_demo_pid_tip (і дев-скрипт dev/perevirka_nush.py
    викликає цю функцію напряму на своїй копії). Повертає лічильники для flash."""
    M = DEMO_MASTER_PREFIX
    cur = conn.cursor()
    # PRAGMA foreign_keys всередині відкритої транзакції не діє - defer_foreign_keys
    # відкладає перевірку до COMMIT, тобто порядок наливання значення не має,
    # але підсумковий стан усе одно мусить бути цілісним
    cur.execute("PRAGMA defer_foreign_keys = ON")

    def ids(sql, params=()):
        return [r[0] for r in cur.execute(sql, params) if r[0] is not None]

    klasy_ids = ids(f"SELECT id FROM {M}klasy WHERE paralel BETWEEN ? AND ?", (p_vid, p_do))
    if not klasy_ids:
        return None
    kl = "(" + ",".join(str(i) for i in klasy_ids) + ")"

    komplekt_ids = ids(f"SELECT DISTINCT komplekt_id FROM {M}klasy WHERE id IN {kl}")
    km = "(" + ",".join(str(i) for i in komplekt_ids) + ")" if komplekt_ids else "(NULL)"

    vchyteli_ids = ids(f"""
        SELECT vchytel_id FROM {M}navchalnyj_plan WHERE klas_id IN {kl}
        UNION SELECT pidgrupa_2_vchytel_id FROM {M}navchalnyj_plan WHERE klas_id IN {kl}
        UNION SELECT vchytel_id FROM {M}pozaurochnyj_plan WHERE klas_id IN {kl}
        UNION SELECT klasnyi_kerivnyk_id FROM {M}klasy WHERE id IN {kl}
        UNION SELECT klasnyi_kerivnyk_id FROM {M}klas_komplekty WHERE id IN {km}""")
    vch = "(" + ",".join(str(i) for i in vchyteli_ids) + ")" if vchyteli_ids else "(NULL)"

    # кабінети: викидаємо ЛИШЕ домашні кабінети класів, що не потрапили в
    # діапазон. Вільні універсальні й предметні лишаються всі - вони потрібні
    # поділам на підгрупи й spets_kabinet_lyshe_pidgrupa1, а не конкретному класу.
    chuzhi_domashni = ids(f"""
        SELECT DISTINCT domashniy_kabinet_id FROM {M}klasy
        WHERE id NOT IN {kl} AND domashniy_kabinet_id IS NOT NULL
          AND domashniy_kabinet_id NOT IN (
              SELECT domashniy_kabinet_id FROM {M}klasy WHERE id IN {kl})""")
    kab_umova = ("id NOT IN (" + ",".join(str(i) for i in chuzhi_domashni) + ")"
                 if chuzhi_domashni else "1")

    umovy = {
        "vchyteli": f"id IN {vch}",
        "kabinety": kab_umova,
        "kabinety_predmety": f"kabinet_id IN (SELECT id FROM {M}kabinety WHERE {kab_umova})",
        "kabinety_vchyteli": (f"vchytel_id IN {vch} AND kabinet_id IN "
                              f"(SELECT id FROM {M}kabinety WHERE {kab_umova})"),
        "klas_komplekty": f"id IN {km}",
        "klasy": f"id IN {kl}",
        "navchalnyj_plan": f"klas_id IN {kl}",
        "mizhpredmetni_grupy": f"klas_id IN {kl}",
        "asystenty": f"klas_id IS NULL OR klas_id IN {kl}",
        "asystenty_predmety": (f"asystent_id IN (SELECT id FROM {M}asystenty "
                               f"WHERE klas_id IS NULL OR klas_id IN {kl})"),
        # довідник позаурочних занять лишається ПОВНИМ (усі 10 прикладів кожного
        # типу), навіть якщо в звуженому діапазоні частина з них нікому не
        # призначена - це каталог зразків для завуча, а не дані класу
        "pozaurochni_zanyattya": "1",
        "pozaurochnyj_plan": f"klas_id IN {kl}",
    }

    for t in _DEMO_POHIDNI_TABLYCI:
        cur.execute(f"DELETE FROM {t}")
    for t in reversed(DEMO_MASTER_TABLES):
        cur.execute(f"DELETE FROM {t}")
    for t in DEMO_MASTER_TABLES:
        kolonky = _spilni_kolonky(conn, t)
        spysok = ", ".join(kolonky)
        cur.execute(f"INSERT INTO {t} ({spysok}) SELECT {spysok} FROM {M}{t} "
                    f"WHERE {umovy[t]}")

    # закріплений кабінет вчителя міг лишитись за викинутим домашнім кабінетом
    cur.execute("""UPDATE vchyteli SET zakriplenyi_kabinet_id = NULL
                   WHERE zakriplenyi_kabinet_id IS NOT NULL
                     AND zakriplenyi_kabinet_id NOT IN (SELECT id FROM kabinety)""")

    def skilky(t):
        return cur.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]

    return {
        "klasy": skilky("klasy"), "vchyteli": skilky("vchyteli"),
        "kabinety": skilky("kabinety"), "plan": skilky("navchalnyj_plan"),
        "asystenty": skilky("asystenty"),
    }


def _pidlashtuvaty_demo_pid_tip(conn, p_vid, p_do):
    """Те саме, але з ЖОРСТКИМ запобіжником: на реальній базі не робить нічого.
    Саме цю функцію викликає маршрут налаштувань."""
    if not DEMO_REZHYM_AKTYVNYI:
        return None
    if not _demo_master_ye(conn):
        return None
    return _nalyty_demo_z_maistra(conn, p_vid, p_do)


from mon_normy_loader import osvitni_galuzi
OSVITNI_GALUZI = osvitni_galuzi()

app.jinja_env.globals["OSVITNI_GALUZI"] = OSVITNI_GALUZI

from mon_normy_loader import movy_navchannya
MOVY_NAVCHANNYA = movy_navchannya()
app.jinja_env.globals["MOVY_NAVCHANNYA"] = MOVY_NAVCHANNYA

from mon_normy_loader import normy_podilu_na_pidgrupy
NORMY_PODILU_PIDGRUPY = normy_podilu_na_pidgrupy()
app.jinja_env.globals["NORMY_PODILU_PIDGRUPY"] = NORMY_PODILU_PIDGRUPY


def _diapazon_normy_podilu(normovanyi_kod, paralel):
    """Знаходить діапазон паралелей норми поділу на підгрупи для предмета (за
    normovanyi_kod), що містить дану паралель. None, якщо норми немає або паралель
    поза жодним діапазоном."""
    norma = NORMY_PODILU_PIDGRUPY.get(normovanyi_kod)
    if not norma:
        return None
    for d in norma["diapazony"]:
        if d["vid"] <= paralel <= d["do"]:
            return {"typ": norma["typ"], "za_stattyu": norma.get("za_stattyu", False), **d}
    return None


def _efektyvna_mova_klasu(conn, klas):
    """Мова навчання класу - власна, якщо задана, інакше мова закладу за
    замовчуванням (Налаштування школи)."""
    if klas["mova_navchannya"]:
        return klas["mova_navchannya"]
    school_row = conn.execute("SELECT mova_navchannya FROM school_settings WHERE id = 1").fetchone()
    return (school_row["mova_navchannya"] if school_row else None) or "Українська"


def _porada_podilu_pidgrup(conn, klas, predmet):
    """Порада (текст) для конкретного класу+предмета, чому за нормативом МОН клас
    підлягає поділу на підгрупи - або None, якщо поділ не увімкнено ("Авто"), норми
    для предмета немає, чи фактичні дані класу порогу не перевищують."""
    if predmet["podil_pravylo"] != "avto":
        return None
    normovanyi_kod = predmet["normovanyi_kod"]
    diapazon = _diapazon_normy_podilu(normovanyi_kod, klas["paralel"])
    if not diapazon:
        return None

    school_row = conn.execute("SELECT tip_miscevosti FROM school_settings WHERE id = 1").fetchone()
    miska = not school_row or school_row["tip_miscevosti"] == "miska"
    dovidka = f"наказ МОН №128, у редакції №808, {diapazon['vid']}-{diapazon['do']} кл."

    if diapazon["typ"] == "grupy_bez_porogu":
        kilkist = klas["kilkist_uchniv"]
        potribno = diapazon["kilkist_grup"] * diapazon["min_u_grupi"]
        if kilkist < potribno:
            return None
        return (
            f"У цьому класі {kilkist} учнів — досить для {diapazon['kilkist_grup']} груп "
            f"мінімум по {diapazon['min_u_grupi']} осіб ({dovidka}). За нормою клас "
            "підлягає поділу на підгрупи — призначте вчителя підгрупи 2 нижче."
        )

    # typ == "kilkist"
    norma = NORMY_PODILU_PIDGRUPY.get(normovanyi_kod, {})
    movy_menshyn_miska = norma.get("movy_menshyn_porih_miska")
    movy_menshyn_silska = norma.get("movy_menshyn_porih_silska")
    mova_klasu = _efektyvna_mova_klasu(conn, klas) if movy_menshyn_miska is not None else None
    if mova_klasu and mova_klasu != "Українська":
        porih = movy_menshyn_miska if miska else movy_menshyn_silska
        mova_dovidka = f" (клас з мовою навчання «{mova_klasu}»)"
    else:
        porih = diapazon["porih_miska"] if miska else diapazon["porih_silska"]
        mova_dovidka = ""
    kilkist = klas["kilkist_uchniv"]
    if kilkist <= porih:
        return None

    if diapazon.get("za_stattyu"):
        chlopci = klas["kilkist_chlopciv"] or 0
        divchat = klas["kilkist_divchat"] or 0
        return (
            f"У цьому класі {kilkist} учнів (хлопців {chlopci}, дівчат {divchat}) — більше "
            f"порогу {porih}{mova_dovidka} ({dovidka}, {'місто' if miska else 'село'}). За "
            "нормою клас ділиться на підгрупи САМЕ ЗА СТАТТЮ (хлопці окремо, дівчата окремо) "
            "— призначте вчителя підгрупи 2 нижче."
        )
    return (
        f"У цьому класі {kilkist} учнів — більше порогу {porih}{mova_dovidka} ({dovidka}, "
        f"{'місто' if miska else 'село'}) для цього предмета. За нормою клас підлягає "
        "поділу на підгрупи — призначте вчителя підгрупи 2 нижче."
    )


_INOZEMNI_MOVY_KODY = ("inozemna_mova", "druga_inozemna_mova")


def _porada_riznomovni_inozemni(conn, klas_id):
    """Пункт 4 наказу МОН №128 (у редакції №808): різні іноземні мови в одному класі
    підлягають поділу - через "Міжпредметні групи" (уже наявний механізм: різні
    предмети паралельно для підгруп того самого класу), а не через "Вчитель
    підгрупи 2" (той призначений для ОДНОГО й того самого предмета). Повертає текст
    поради, або None, якщо в класі не більше однієї іноземної мови, чи всі вже
    об'єднані в одну міжпредметну групу."""
    rows = conn.execute(
        """
        SELECT np.predmet_id, np.mizhpredmetna_grupa_id, np.semestr, p.nazva
        FROM navchalnyj_plan np
        JOIN predmety p ON p.id = np.predmet_id
        WHERE np.klas_id = ? AND p.normovanyi_kod IN (?, ?)
        """,
        (klas_id, *_INOZEMNI_MOVY_KODY),
    ).fetchall()
    # "одночасно" - у межах одного семестру (v0.2.0): англійська весь рік і
    # німецька лише в ІІ семестрі - дві мови саме в ІІ семестрі; а англійська в І і
    # німецька в ІІ - жодного разу разом
    nazvy = None
    for semestr in (1, 2):
        rows_s = [r for r in rows if r["semestr"] in (0, semestr)]
        predmety_za_id = {r["predmet_id"]: r["nazva"] for r in rows_s}
        if len(predmety_za_id) < 2:
            continue
        grupy = {r["mizhpredmetna_grupa_id"] for r in rows_s}
        if len(grupy) == 1 and None not in grupy:
            continue   # усі рядки різних мов уже в ОДНІЙ спільній групі - вирішено
        nazvy = ", ".join(sorted(predmety_za_id.values(), key=ukr_klyuch))
        break
    if nazvy is None:
        return None
    return (
        f"У цьому класі кілька різних іноземних мов одночасно ({nazvy}) — за нормою "
        "(наказ МОН №128, у редакції №808, п.4) клас підлягає поділу на групи за мовами. "
        "Це не поле \"Вчитель підгрупи 2\" (те - для ОДНОГО предмета з двома вчителями), "
        "а \"Міжпредметні групи\" — об'єднайте записи цих мов в одну групу на сторінці "
        "\"Навчальний план\" → \"Міжпредметні групи поділу\"."
    )

from mon_normy_loader import normovani_kody_predmetiv
app.jinja_env.globals["NORMOVANI_KODY_PREDMETIV"] = normovani_kody_predmetiv()




@app.route("/predmety")
def predmety():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    vykorystani_ids = {
        r["predmet_id"] for r in conn.execute(
            "SELECT DISTINCT predmet_id FROM navchalnyj_plan"
        ).fetchall()
    }
    conn.close()
    conn = get_connection()
    result = []
    for p in rows:
        row = dict(p)
        row["u_plani"] = p["id"] in vykorystani_ids
        chastyny = []
        for sql, opys in (
            ("SELECT COUNT(*) c FROM navchalnyj_plan WHERE predmet_id = ?", "рядків навчального плану (у всіх класах)"),
            ("SELECT COUNT(*) c FROM kabinety_predmety WHERE predmet_id = ?", "прив'язок до кабінетів"),
            ("SELECT COUNT(*) c FROM generated_urok WHERE predmet_id = ?", "уроків у поточному розкладі"),
        ):
            n = conn.execute(sql, (p["id"],)).fetchone()["c"]
            if n:
                chastyny.append(f"{n} {opys}")
        row["naslidky"] = "; ".join(chastyny)
        result.append(row)
    conn.close()
    return render_template("predmety_list.html", active="predmety", predmety=result)


@app.route("/predmety/bulk", methods=["GET", "POST"])
def predmety_bulk():
    if request.method == "POST":
        raw = request.form.get("nazvy_list", "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]

        conn = get_connection()
        existing_nazvy = {
            row["nazva"].strip().lower() for row in conn.execute("SELECT nazva FROM predmety").fetchall()
        }

        created = 0
        skipped = []
        for nazva in lines:
            if nazva.lower() in existing_nazvy:
                skipped.append(nazva)
                continue
            # podil_pravylo ЯВНО: дефолт схеми - 'avto', а форма створення
            # ставить 'nikoly'. Без цього масово додані предмети приходили
            # з увімкненою перевіркою поділу, чого завуч не вибирав.
            conn.execute(
                "INSERT INTO predmety (nazva, podil_pravylo) VALUES (?, 'nikoly')",
                (nazva,),
            )
            existing_nazvy.add(nazva.lower())
            created += 1

        conn.commit()
        conn.close()

        message = f"Додано предметів: {created}."
        if skipped:
            message += f" Пропущено (вже існують): {len(skipped)} — {', '.join(skipped)}."
        return render_template("predmety_bulk.html", active="predmety", message=message)

    return render_template("predmety_bulk.html", active="predmety", message=None)


@app.route("/predmety/new", methods=["GET", "POST"])
def predmety_new():
    conn0 = get_connection()
    school_row = conn0.execute("SELECT tip_miscevosti FROM school_settings WHERE id = 1").fetchone()
    conn0.close()
    school_miscevist = "місто" if (not school_row or school_row["tip_miscevosti"] == "miska") else "село"

    errors = []
    if request.method == "POST":
        data = _read_predmet_form_basic(request.form)
        if not data["nazva"]:
            errors.append("Назва предмета обов'язкова.")

        if not errors:
            conn = get_connection()
            try:
                # розширені поля (складність, код тощо) - розумні дефолти при
                # створенні, редагуються окремо на сторінці "Додатково". min_u_pidgrupi/
                # maks_pidgrup/podil_za_stattyu/podil_porih більше не заповнюються з
                # форми - лишились у схемі зі своїми дефолтами (8/2/0/NULL), контроль
                # поділу тепер повністю через norma_podilu_na_pidgrupy (мінімум 8 -
                # константа з наказу, не поле вводу).
                cur = conn.execute(
                    """
                    INSERT INTO predmety
                        (nazva, skorochennya, zdvoyeni_urok, podil_pravylo,
                         vazhkist, vrahovuyetsya_u_hranychnomu, ostannim_urokom)
                    VALUES (?, ?, ?, ?, 'serednii', 1, 0)
                    """,
                    (
                        data["nazva"], data["skorochennya"],
                        data["zdvoyeni_urok"], data["podil_pravylo"],
                    ),
                )
                from db import zapovnyty_pary_pravyla_defaultamy
                zapovnyty_pary_pravyla_defaultamy(conn, predmet_id=cur.lastrowid)
                conn.commit()
                conn.close()
                return redirect(url_for("predmety"))
            except sqlite3.IntegrityError:
                conn.close()
                errors = [f'Предмет "{data["nazva"]}" вже існує.']

        return render_template("predmety_form.html", active="predmety", predmet=data, school_miscevist=school_miscevist, errors=errors)

    return render_template("predmety_form.html", active="predmety", predmet=None, school_miscevist=school_miscevist, errors=[])


@app.route("/predmety/<int:predmet_id>/edit", methods=["GET", "POST"])
def predmety_edit(predmet_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM predmety WHERE id = ?", (predmet_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    school_row = conn.execute("SELECT tip_miscevosti FROM school_settings WHERE id = 1").fetchone()
    school_miscevist = "місто" if (not school_row or school_row["tip_miscevosti"] == "miska") else "село"

    errors = []
    if request.method == "POST":
        data = _read_predmet_form_basic(request.form)
        if not data["nazva"]:
            errors.append("Назва предмета обов'язкова.")

        if not errors:
            try:
                conn.execute(
                    """
                    UPDATE predmety SET
                        nazva = ?, skorochennya = ?, zdvoyeni_urok = ?, podil_pravylo = ?
                    WHERE id = ?
                    """,
                    (
                        data["nazva"], data["skorochennya"],
                        data["zdvoyeni_urok"], data["podil_pravylo"],
                        predmet_id,
                    ),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("predmety"))
            except sqlite3.IntegrityError:
                errors = [f'Предмет "{data["nazva"]}" вже існує.']

        conn.close()
        data["id"] = predmet_id
        return render_template("predmety_form.html", active="predmety", predmet=data, school_miscevist=school_miscevist, errors=errors)

    conn.close()
    return render_template("predmety_form.html", active="predmety", predmet=dict(existing), school_miscevist=school_miscevist, errors=[])


@app.route("/predmety/<int:predmet_id>/dodatkovo", methods=["GET", "POST"])
def predmety_dodatkovo(predmet_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM predmety WHERE id = ?", (predmet_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    errors = []
    if request.method == "POST":
        data = _read_predmet_form_dodatkovo(request.form)
        conn.execute(
            """
            UPDATE predmety SET
                zaboroneni_uroky = ?, min_promizhok_dni = ?, fiksovanyi_den = ?,
                fiksovanyi_urok = ?, ostannim_urokom = ?, kategoria = ?, osvitnya_galuz = ?,
                vrahovuyetsya_u_hranychnomu = ?, vazhkist = ?, normovanyi_kod = ?,
                spets_kabinet_lyshe_pidgrupa1 = ?
            WHERE id = ?
            """,
            (
                data["zaboroneni_uroky"], data["min_promizhok_dni"], data["fiksovanyi_den"],
                data["fiksovanyi_urok"], data["ostannim_urokom"], data["kategoria"], data["osvitnya_galuz"],
                data["vrahovuyetsya_u_hranychnomu"], data["vazhkist"], data["normovanyi_kod"],
                data["spets_kabinet_lyshe_pidgrupa1"],
                predmet_id,
            ),
        )
        from db import zapovnyty_pary_pravyla_defaultamy
        zapovnyty_pary_pravyla_defaultamy(conn, predmet_id=predmet_id)
        conn.commit()
        conn.close()
        return redirect(url_for("predmety"))

    conn.close()
    return render_template("predmety_dodatkovo.html", active="predmety", predmet=dict(existing), errors=errors)


@app.route("/predmety/<int:predmet_id>/delete", methods=["POST"])
def predmety_delete(predmet_id):
    conn = get_connection()
    conn.execute("DELETE FROM predmety WHERE id = ?", (predmet_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("predmety"))




def _read_kabinet_form(form, riven_kolonky=(), existing=None):
    """Базові поля - назва, місткість, тип прив'язки, дозволені предмети/вчителі.
    minimalna_mistkist/korpus/poverh/priorytet - окрема сторінка 'Додатково'."""
    predmet_ids = form.getlist("predmet_ids")
    vchytel_ids = form.getlist("vchytel_ids")
    typ_pryvyazky = form.get("typ_pryvyazky", "universalnyi")

    # ВАЖЛИВО: примусово очищуємо неактуальні для цього типу поля тут, на сервері -
    # не покладаємось на клієнтський JS (він лише ХОВАЄ блок при зміні типу,
    # приховані-але-позначені чекбокси все одно надсилаються з формою; без цього
    # очищення стара прив'язка "жила" поруч із новою в базі при зміні типу).
    if typ_pryvyazky == "universalnyi":
        predmet_ids, vchytel_ids = [], []
    elif typ_pryvyazky == "predmet":
        vchytel_ids = []
    elif typ_pryvyazky == "vchytel":
        predmet_ids = []
    # "kombinatsiya" - лишаємо обидва як є

    result = {
        "nazva": form["nazva"].strip(),
        "mistkist": int(form.get("mistkist") or 0),
        "typ_pryvyazky": typ_pryvyazky,
        "predmet_ids": [int(x) for x in predmet_ids],
        "vchytel_ids": [int(x) for x in vchytel_ids],
    }
    result.update(_read_riven_prapory(form, riven_kolonky, existing))
    return result


def _read_kabinet_form_dodatkovo(form):
    return {
        "minimalna_mistkist": int(form["minimalna_mistkist"]) if form.get("minimalna_mistkist") else None,
        "korpus": form.get("korpus", "").strip() or None,
        "poverh": form.get("poverh", "").strip() or None,
        "priorytet": int(form["priorytet"]) if form.get("priorytet") else None,
    }


def _save_kabinet_predmety(conn, kabinet_id, predmet_ids):
    conn.execute("DELETE FROM kabinety_predmety WHERE kabinet_id = ?", (kabinet_id,))
    for pid in predmet_ids:
        conn.execute(
            "INSERT INTO kabinety_predmety (kabinet_id, predmet_id) VALUES (?, ?)",
            (kabinet_id, pid),
        )


def _save_kabinet_vchyteli(conn, kabinet_id, vchytel_ids):
    conn.execute("DELETE FROM kabinety_vchyteli WHERE kabinet_id = ?", (kabinet_id,))
    for vid in vchytel_ids:
        conn.execute(
            "INSERT INTO kabinety_vchyteli (kabinet_id, vchytel_id) VALUES (?, ?)",
            (kabinet_id, vid),
        )


@app.route("/kabinety")
def kabinety():
    conn = get_connection()
    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    rows = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    result = []
    for k in rows:
        predmety_names = conn.execute(
            """
            SELECT p.nazva FROM predmety p
            JOIN kabinety_predmety kp ON kp.predmet_id = p.id
            WHERE kp.kabinet_id = ?
            ORDER BY p.nazva COLLATE UKR
            """,
            (k["id"],),
        ).fetchall()
        vchyteli_names = conn.execute(
            """
            SELECT v.pib FROM vchyteli v
            JOIN kabinety_vchyteli kv ON kv.vchytel_id = v.id
            WHERE kv.kabinet_id = ?
            ORDER BY v.pib COLLATE UKR
            """,
            (k["id"],),
        ).fetchall()
        row = dict(k)
        row["predmety_names"] = ", ".join(r["nazva"] for r in predmety_names)
        row["vchyteli_names"] = ", ".join(r["pib"] for r in vchyteli_names)
        chastyny = []
        n_dom = conn.execute(
            "SELECT COUNT(*) c FROM klasy WHERE domashniy_kabinet_id = ?", (k["id"],)).fetchone()["c"]
        n_vch = conn.execute(
            "SELECT COUNT(*) c FROM vchyteli WHERE zakriplenyi_kabinet_id = ?", (k["id"],)).fetchone()["c"]
        n_ur = conn.execute(
            "SELECT COUNT(*) c FROM generated_urok WHERE kabinet_id = ?", (k["id"],)).fetchone()["c"]
        if n_dom:
            chastyny.append(f"домашній для {n_dom} кл. (класи лишаться без кабінету)")
        if n_vch:
            chastyny.append(f"закріплений за {n_vch} вчит.")
        if n_ur:
            chastyny.append(f"{n_ur} уроків поточного розкладу лишаться без кабінету")
        row["naslidky"] = "; ".join(chastyny)
        result.append(row)
    conn.close()
    return render_template(
        "kabinety_list.html", active="kabinety", kabinety=result, riven_kolonky=riven_kolonky,
    )


@app.route("/kabinety/bulk", methods=["GET", "POST"])
def kabinety_bulk():
    if request.method == "POST":
        raw = request.form.get("kabinety_list", "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]

        conn = get_connection()
        existing_nazvy = {
            row["nazva"].strip().lower() for row in conn.execute("SELECT nazva FROM kabinety").fetchall()
        }

        created = 0
        skipped = []
        nevaliden = []
        for line in lines:
            chastyny = line.split(",")
            if len(chastyny) != 2 or not chastyny[1].strip().isdigit():
                nevaliden.append(line)
                continue
            nazva, mistkist = chastyny[0].strip(), int(chastyny[1].strip())
            if not nazva:
                nevaliden.append(line)
                continue
            if nazva.lower() in existing_nazvy:
                skipped.append(nazva)
                continue
            conn.execute("INSERT INTO kabinety (nazva, mistkist) VALUES (?, ?)", (nazva, mistkist))
            existing_nazvy.add(nazva.lower())
            created += 1

        conn.commit()
        conn.close()

        message = f"Додано кабінетів: {created}."
        if skipped:
            message += f" Пропущено (вже існують): {len(skipped)} — {', '.join(skipped)}."
        if nevaliden:
            message += f" Не розпізнано (потрібен формат 'Назва, Місткість'): {len(nevaliden)} — {', '.join(nevaliden)}."
        return render_template("kabinety_bulk.html", active="kabinety", message=message)

    return render_template("kabinety_bulk.html", active="kabinety", message=None)


@app.route("/kabinety/new", methods=["GET", "POST"])
def kabinety_new():
    conn = get_connection()
    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_kabinet_form(request.form, riven_kolonky, None)
        if not data["nazva"]:
            errors.append("Назва/номер кабінету обов'язкові.")
        if data["mistkist"] <= 0:
            errors.append("Місткість має бути більше нуля.")

        if not errors:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO kabinety (
                        nazva, mistkist, typ_pryvyazky,
                        riven_pochatkova, riven_gimnaziya, riven_liceum
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (data["nazva"], data["mistkist"], data["typ_pryvyazky"],
                     data["riven_pochatkova"], data["riven_gimnaziya"], data["riven_liceum"]),
                )
                _save_kabinet_predmety(conn, cur.lastrowid, data["predmet_ids"])
                _save_kabinet_vchyteli(conn, cur.lastrowid, data["vchytel_ids"])
                conn.commit()
                conn.close()
                return redirect(url_for("kabinety"))
            except sqlite3.IntegrityError:
                errors = [f'Кабінет "{data["nazva"]}" вже існує.']

        conn.close()
        return render_template(
            "kabinety_form.html", active="kabinety", kabinet=data,
            all_predmety=all_predmety, all_vchyteli=all_vchyteli, errors=errors,
            riven_kolonky=riven_kolonky,
        )

    conn.close()
    return render_template(
        "kabinety_form.html", active="kabinety", kabinet=None,
        all_predmety=all_predmety, all_vchyteli=all_vchyteli, errors=[],
        riven_kolonky=riven_kolonky,
    )


@app.route("/kabinety/<int:kabinet_id>/edit", methods=["GET", "POST"])
def kabinety_edit(kabinet_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM kabinety WHERE id = ?", (kabinet_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_kabinet_form(request.form, riven_kolonky, existing)
        if not data["nazva"]:
            errors.append("Назва/номер кабінету обов'язкові.")
        if data["mistkist"] <= 0:
            errors.append("Місткість має бути більше нуля.")

        if not errors:
            try:
                conn.execute(
                    """
                    UPDATE kabinety SET
                        nazva = ?, mistkist = ?, typ_pryvyazky = ?,
                        riven_pochatkova = ?, riven_gimnaziya = ?, riven_liceum = ?
                    WHERE id = ?
                    """,
                    (data["nazva"], data["mistkist"], data["typ_pryvyazky"],
                     data["riven_pochatkova"], data["riven_gimnaziya"], data["riven_liceum"],
                     kabinet_id),
                )
                _save_kabinet_predmety(conn, kabinet_id, data["predmet_ids"])
                _save_kabinet_vchyteli(conn, kabinet_id, data["vchytel_ids"])
                conn.commit()
                conn.close()
                return redirect(url_for("kabinety"))
            except sqlite3.IntegrityError:
                errors = [f'Кабінет "{data["nazva"]}" вже існує.']

        conn.close()
        data["id"] = kabinet_id
        return render_template(
            "kabinety_form.html", active="kabinety", kabinet=data,
            all_predmety=all_predmety, all_vchyteli=all_vchyteli, errors=errors,
            riven_kolonky=riven_kolonky,
        )

    selected_predmet_ids = {
        r["predmet_id"] for r in conn.execute(
            "SELECT predmet_id FROM kabinety_predmety WHERE kabinet_id = ?", (kabinet_id,)
        ).fetchall()
    }
    selected_vchytel_ids = {
        r["vchytel_id"] for r in conn.execute(
            "SELECT vchytel_id FROM kabinety_vchyteli WHERE kabinet_id = ?", (kabinet_id,)
        ).fetchall()
    }
    conn.close()
    kabinet = dict(existing)
    kabinet["predmet_ids"] = selected_predmet_ids
    kabinet["vchytel_ids"] = selected_vchytel_ids
    return render_template(
        "kabinety_form.html", active="kabinety", kabinet=kabinet,
        all_predmety=all_predmety, all_vchyteli=all_vchyteli, errors=[],
        riven_kolonky=riven_kolonky,
    )


@app.route("/kabinety/<int:kabinet_id>/dodatkovo", methods=["GET", "POST"])
def kabinety_dodatkovo(kabinet_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM kabinety WHERE id = ?", (kabinet_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        data = _read_kabinet_form_dodatkovo(request.form)
        conn.execute(
            """
            UPDATE kabinety SET minimalna_mistkist = ?, korpus = ?, poverh = ?, priorytet = ?
            WHERE id = ?
            """,
            (data["minimalna_mistkist"], data["korpus"], data["poverh"], data["priorytet"], kabinet_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("kabinety"))

    conn.close()
    return render_template("kabinety_dodatkovo.html", active="kabinety", kabinet=dict(existing), errors=[])


@app.route("/kabinety/<int:kabinet_id>/delete", methods=["POST"])
def kabinety_delete(kabinet_id):
    conn = get_connection()
    conn.execute("DELETE FROM kabinety WHERE id = ?", (kabinet_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("kabinety"))




def _read_vchytel_form(form, riven_kolonky=(), existing=None):
    kabinet_id = form.get("zakriplenyi_kabinet_id") or None
    maks_urokiv = form.get("maksymum_urokiv_den") or None
    result = {
        "pib": form["pib"].strip(),
        "zakriplenyi_kabinet_id": int(kabinet_id) if kabinet_id else None,
        "maksymum_urokiv_den": int(maks_urokiv) if maks_urokiv else None,
    }
    result.update(_read_riven_prapory(form, riven_kolonky, existing))
    return result


@app.route("/vchyteli")
def vchyteli():
    conn = get_connection()
    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    rows = conn.execute(
        """
        SELECT v.*, k.nazva AS kabinet_nazva
        FROM vchyteli v
        LEFT JOIN kabinety k ON k.id = v.zakriplenyi_kabinet_id
        ORDER BY v.pib COLLATE UKR
        """
    ).fetchall()
    # "задіяний" = призначений будь-де, не лише як основний вчитель предмета:
    # навчальний план (осн. вчитель АБО вчитель 2-ї підгрупи поділу - окрема особа),
    # позаурочний план, класне керівництво. Раніше рахувався тільки
    # navchalnyj_plan.vchytel_id, тому вчитель 2-ї підгрупи чи корекційний педагог
    # (лише позаурочка) хибно позначався червоним як "незадіяний".
    zadiyani_ids = set()
    for query in (
        "SELECT vchytel_id FROM navchalnyj_plan WHERE vchytel_id IS NOT NULL",
        "SELECT pidgrupa_2_vchytel_id FROM navchalnyj_plan WHERE pidgrupa_2_vchytel_id IS NOT NULL",
        "SELECT vchytel_id FROM pozaurochnyj_plan WHERE vchytel_id IS NOT NULL",
        "SELECT klasnyi_kerivnyk_id FROM klasy WHERE klasnyi_kerivnyk_id IS NOT NULL",
    ):
        zadiyani_ids.update(r[0] for r in conn.execute(query).fetchall())
    result = []
    for v in rows:
        predmety_rows = conn.execute(
            """
            SELECT DISTINCT p.nazva FROM predmety p
            JOIN navchalnyj_plan np ON np.predmet_id = p.id
            WHERE np.vchytel_id = ?
            ORDER BY p.nazva COLLATE UKR
            """,
            (v["id"],),
        ).fetchall()
        row = dict(v)
        row["predmety_names"] = ", ".join(r["nazva"] for r in predmety_rows)
        row["u_plani"] = v["id"] in zadiyani_ids
        # що зникне разом із вчителем - у текст підтвердження видалення
        n_plan = conn.execute(
            "SELECT COUNT(*) c FROM navchalnyj_plan WHERE vchytel_id = ?", (v["id"],)).fetchone()["c"]
        n_pg2 = conn.execute(
            "SELECT COUNT(*) c FROM navchalnyj_plan WHERE pidgrupa_2_vchytel_id = ?", (v["id"],)).fetchone()["c"]
        n_poz = conn.execute(
            "SELECT COUNT(*) c FROM pozaurochnyj_plan WHERE vchytel_id = ?", (v["id"],)).fetchone()["c"]
        n_kk = conn.execute(
            "SELECT COUNT(*) c FROM klasy WHERE klasnyi_kerivnyk_id = ?", (v["id"],)).fetchone()["c"]
        chastyny = []
        if n_plan:
            chastyny.append(f"{n_plan} рядків навчального плану (зникнуть)")
        if n_pg2:
            chastyny.append(f"{n_pg2} рядків як 2-й вчитель підгрупи (поділ зніметься)")
        if n_poz:
            chastyny.append(f"{n_poz} позаурочних (зникнуть)")
        if n_kk:
            chastyny.append(f"класне керівництво у {n_kk} кл.")
        row["naslidky"] = "; ".join(chastyny)
        result.append(row)
    conn.close()
    return render_template(
        "vchyteli_list.html", active="vchyteli", vchyteli=result, riven_kolonky=riven_kolonky,
    )


@app.route("/vchyteli/bulk", methods=["GET", "POST"])
def vchyteli_bulk():
    if request.method == "POST":
        raw = request.form.get("pib_list", "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]

        conn = get_connection()
        existing_pib = {
            row["pib"].strip().lower() for row in conn.execute("SELECT pib FROM vchyteli").fetchall()
        }

        created = 0
        skipped = []
        for pib in lines:
            if pib.lower() in existing_pib:
                skipped.append(pib)
                continue
            conn.execute("INSERT INTO vchyteli (pib) VALUES (?)", (pib,))
            existing_pib.add(pib.lower())
            created += 1

        conn.commit()
        conn.close()

        message = f"Додано вчителів: {created}."
        if skipped:
            message += f" Пропущено (вже існують): {len(skipped)} — {', '.join(skipped)}."
        return render_template("vchyteli_bulk.html", active="vchyteli", message=message)

    return render_template("vchyteli_bulk.html", active="vchyteli", message=None)


@app.route("/vchyteli/new", methods=["GET", "POST"])
def vchyteli_new():
    conn = get_connection()
    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_vchytel_form(request.form, riven_kolonky, None)
        if not data["pib"]:
            errors.append("ПІБ вчителя обов'язкове.")

        if not errors:
            conn.execute(
                """
                INSERT INTO vchyteli (
                    pib, zakriplenyi_kabinet_id, maksymum_urokiv_den,
                    riven_pochatkova, riven_gimnaziya, riven_liceum
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (data["pib"], data["zakriplenyi_kabinet_id"], data["maksymum_urokiv_den"],
                 data["riven_pochatkova"], data["riven_gimnaziya"], data["riven_liceum"]),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("vchyteli"))

        conn.close()
        return render_template(
            "vchyteli_form.html", active="vchyteli", vchytel=data,
            all_kabinety=all_kabinety, errors=errors, riven_kolonky=riven_kolonky,
        )

    conn.close()
    return render_template(
        "vchyteli_form.html", active="vchyteli", vchytel=None,
        all_kabinety=all_kabinety, errors=[], riven_kolonky=riven_kolonky,
    )


@app.route("/vchyteli/<int:vchytel_id>/edit", methods=["GET", "POST"])
def vchyteli_edit(vchytel_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM vchyteli WHERE id = ?", (vchytel_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    riven_kolonky = _aktyvni_riven_kolonky(_potochnyi_tip_zakladu(conn))
    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_vchytel_form(request.form, riven_kolonky, existing)
        if not data["pib"]:
            errors.append("ПІБ вчителя обов'язкове.")

        if not errors:
            conn.execute(
                """
                UPDATE vchyteli SET
                    pib = ?, zakriplenyi_kabinet_id = ?, maksymum_urokiv_den = ?,
                    riven_pochatkova = ?, riven_gimnaziya = ?, riven_liceum = ?
                WHERE id = ?
                """,
                (data["pib"], data["zakriplenyi_kabinet_id"], data["maksymum_urokiv_den"],
                 data["riven_pochatkova"], data["riven_gimnaziya"], data["riven_liceum"],
                 vchytel_id),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("vchyteli"))

        conn.close()
        data["id"] = vchytel_id
        return render_template(
            "vchyteli_form.html", active="vchyteli", vchytel=data,
            all_kabinety=all_kabinety, errors=errors, riven_kolonky=riven_kolonky,
        )

    conn.close()
    return render_template(
        "vchyteli_form.html", active="vchyteli", vchytel=dict(existing),
        all_kabinety=all_kabinety, errors=[], riven_kolonky=riven_kolonky,
    )


@app.route("/vchyteli/<int:vchytel_id>/delete", methods=["POST"])
def vchyteli_delete(vchytel_id):
    conn = get_connection()
    conn.execute("DELETE FROM vchyteli WHERE id = ?", (vchytel_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("vchyteli"))




def _zlyty_hodyny_komplektu(rows):
    """Рядки навчального плану ОДНОГО вчителя й ОДНОГО предмета (з полями klas_id,
    paralel, litera, komplekt_id, parnist, godyn_tyzhden) -> список записів, де
    класи одного КЛАС-КОМПЛЕКТУ злиті в один: учитель веде їх ОДНОЧАСНО, і фізично
    (а отже й для оплати) це один урок, а не два. Правило те саме, що в генераторі
    (_obyednaty_klas_komplekty): спільна частина = мінімум годин серед членів,
    залишок кожного класу понад мінімум - його окремі уроки. Клас поза комплектом
    повертається як є. Кожен запис: {"nazva", "klas_ids", "oboydva", "parnyi",
    "neparnyi"}; nazva для комплекту - "3-В+4-В (комплект)".

    Без цього звіти "Тарифікація" і "Залишки годин" показували керівнику
    комплекту суму по класах (напр. 32 год замість реальних 16) - реальний
    випадок на демо 3-4-В."""
    okremi = {}      # klas_id -> запис
    komplekty = {}   # komplekt_id -> {parnist: {klas_id: годин}}, + nazvy
    for r in rows:
        par = r["parnist"] or "oboydva"
        if r["komplekt_id"] is None:
            z = okremi.setdefault(r["klas_id"], {
                "nazva": klas_nazva(r["paralel"], r["litera"]), "klas_ids": [r["klas_id"]],
                "paralel": r["paralel"], "litera": r["litera"],
                "oboydva": 0, "parnyi": 0, "neparnyi": 0,
            })
            z[par] += r["godyn_tyzhden"]
        else:
            k = komplekty.setdefault(r["komplekt_id"], {"nazvy": {}, "hodyny": {}, "paralel": r["paralel"], "litera": r["litera"]})
            k["nazvy"][r["klas_id"]] = klas_nazva(r["paralel"], r["litera"])
            k["hodyny"].setdefault(par, {})
            k["hodyny"][par][r["klas_id"]] = k["hodyny"][par].get(r["klas_id"], 0) + r["godyn_tyzhden"]
    result = list(okremi.values())
    for k in komplekty.values():
        z = {"nazva": "+".join(k["nazvy"][i] for i in sorted(k["nazvy"])) + " (комплект)",
             "klas_ids": sorted(k["nazvy"]), "paralel": k["paralel"], "litera": k["litera"],
             "oboydva": 0, "parnyi": 0, "neparnyi": 0}
        for par, po_klasu in k["hodyny"].items():
            h = list(po_klasu.values())
            if len(h) >= 2:
                spilne = min(h)
                z[par] += spilne + sum(x - spilne for x in h)
            else:
                z[par] += h[0]
        result.append(z)
    result.sort(key=lambda z: (z["paralel"], ukr_klyuch(z["litera"])))
    return result


# 1 ставка вчителя - 18 год/тиждень. Законної СТЕЛІ навантаження за основним
# місцем роботи немає (Інструкція МОН №102, п. 63: "граничними розмірами не
# обмежується"), тож 1,5 ставки = 27 год - лише орієнтир, після якого варто
# перевірити згоду вчителя; у звіті це попередження, а не заборона.
STAVKA_GODYN = 18
MAKS_NAVANTAZHENNYA_GODYN = 27


def _taryfikatsiya_dani(conn):
    """Дані звіту "Тарифікація" - список вчителів з рядками по предметах і
    підсумками. Спільне джерело для сторінки й для Excel-експорту."""
    settings = conn.execute("SELECT kilkist_navchalnyh_tyzhniv FROM school_settings WHERE id = 1").fetchone()
    tyzhniv = settings["kilkist_navchalnyh_tyzhniv"] if settings else 35
    # семестрові рядки плану (v0.2.0) йдуть лише свої тижні: години за рік =
    # тижневі × тижнів ЇХНЬОГО семестру, а "год/тиждень" у звіті - середнє за рік
    # (оплата - річні години, рівномірно на 10 місяців). Для рядків "весь рік"
    # числа точно ті самі, що й до семестрів.
    tyzhni_s = {0: tyzhniv}
    tyzhni_s[1], tyzhni_s[2] = _tyzhni_semestriv(conn)

    # вчитель другої підгрупи (pidgrupa_2_vchytel_id) веде ті самі години паралельно
    # з вчителем першої - для ОПЛАТИ це його повноцінне навантаження, тож рахуємо
    # обидві ролі однаково (інакше 2-й вчитель поділу взагалі не потрапляє в звіт)
    pary = conn.execute(
        """
        SELECT DISTINCT vchytel_id, predmet_id FROM navchalnyj_plan
        UNION
        SELECT DISTINCT pidgrupa_2_vchytel_id, predmet_id FROM navchalnyj_plan
        WHERE pidgrupa_2_vchytel_id IS NOT NULL
        """
    ).fetchall()

    result = []
    for pr in pary:
        vchytel = conn.execute("SELECT pib FROM vchyteli WHERE id = ?", (pr["vchytel_id"],)).fetchone()
        predmet = conn.execute("SELECT nazva FROM predmety WHERE id = ?", (pr["predmet_id"],)).fetchone()
        klasy_rows = conn.execute(
            """
            SELECT np.klas_id, k.paralel, k.litera, k.komplekt_id, np.godyn_tyzhden, np.parnist, np.semestr
            FROM navchalnyj_plan np
            JOIN klasy k ON k.id = np.klas_id
            WHERE np.predmet_id = ?
              AND (np.vchytel_id = ? OR np.pidgrupa_2_vchytel_id = ?)
            ORDER BY np.semestr, k.paralel, k.litera COLLATE UKR
            """,
            (pr["predmet_id"], pr["vchytel_id"], pr["vchytel_id"]),
        ).fetchall()

        # групуємо по класу і рахуємо СЕРЕДНЄ тижневе навантаження (oboydva +
        # (parnyi+neparnyi)/2) - Тарифікація визначає ОПЛАТУ вчителя, яка рахується
        # від середнього навантаження за рік, а не від найгіршого (пікового) тижня.
        # НЕ плутати з oboydva+MAX(parnyi,neparnyi), яка вживається у "Навантаженні
        # по класах"/"Залишках годин" для перевірки фізичної місткості розкладу -
        # це інше питання (чи влізе в конкретний тиждень), не оплата.
        # класи одного клас-комплекту - ОДИН урок для вчителя (див. хелпер)
        rozpodileno_tyzhden = 0
        godyn_rik = 0
        klasy_opys_parts = []
        for semestr in sorted({r["semestr"] for r in klasy_rows}):
            po_klasu = _zlyty_hodyny_komplektu([r for r in klasy_rows if r["semestr"] == semestr])
            for g in po_klasu:
                serednye = g["oboydva"] + (g["parnyi"] + g["neparnyi"]) / 2
                godyn_rik += serednye * tyzhni_s[semestr]
                rozpodileno_tyzhden += serednye * tyzhni_s[semestr] / tyzhniv
                pidpys_sem = f", {SEMESTR_KOROTKO[semestr]}" if semestr else ""
                klasy_opys_parts.append(f"{g['nazva']} ({serednye:g} год/тижд{pidpys_sem})")

        result.append({
            "vchytel_id": pr["vchytel_id"], "predmet_id": pr["predmet_id"],
            "vchytel_pib": vchytel["pib"] if vchytel else "?",
            "predmet_nazva": predmet["nazva"] if predmet else "?",
            "rozpodileno_tyzhden": round(rozpodileno_tyzhden, 2),
            "godyn_rik": round(godyn_rik, 2),
            "klasy_opys": ", ".join(klasy_opys_parts),
        })
    result.sort(key=lambda r: (ukr_klyuch(r["vchytel_pib"]), ukr_klyuch(r["predmet_nazva"])))

    # групуємо по вчителю (вкладки на сторінці) + рядок "Разом" на вчителя
    zapysy_po_vchytelyu = []
    for z in result:
        if not zapysy_po_vchytelyu or zapysy_po_vchytelyu[-1]["vchytel_pib"] != z["vchytel_pib"]:
            zapysy_po_vchytelyu.append({"vchytel_pib": z["vchytel_pib"], "zapysy": [], "razom_tyzhden": 0, "razom_rik": 0})
        grupa = zapysy_po_vchytelyu[-1]
        grupa["zapysy"].append(z)
        grupa["razom_tyzhden"] += z["rozpodileno_tyzhden"]
        grupa["razom_rik"] += z["godyn_rik"]
    for grupa in zapysy_po_vchytelyu:
        grupa["ponad_stelyu"] = grupa["razom_tyzhden"] > MAKS_NAVANTAZHENNYA_GODYN
        grupa["ponad_stavku"] = grupa["razom_tyzhden"] > STAVKA_GODYN
    return zapysy_po_vchytelyu, tyzhniv


@app.route("/taryfikatsiya")
def taryfikatsiya():
    conn = get_connection()
    zapysy_po_vchytelyu, tyzhniv = _taryfikatsiya_dani(conn)
    zatverdzhennya = conn.execute(
        "SELECT MIN(zatverdzheno) AS koly, COUNT(*) AS n, SUM(hodyn_rik) AS hodyn FROM zatverdzheni_hodyny"
    ).fetchone()
    ye_peredachi = conn.execute("SELECT 1 FROM peredachi_hodyn LIMIT 1").fetchone() is not None
    conn.close()

    return render_template(
        "taryfikatsiya_list.html", active="taryfikatsiya",
        zatverdzhennya=zatverdzhennya if zatverdzhennya["n"] else None, ye_peredachi=ye_peredachi,
        zapysy_po_vchytelyu=zapysy_po_vchytelyu, tyzhniv=tyzhniv,
        stavka=STAVKA_GODYN, maks_navantazhennya=MAKS_NAVANTAZHENNYA_GODYN,
        ponad_stelyu=[g["vchytel_pib"] for g in zapysy_po_vchytelyu if g["ponad_stelyu"]],
    )


@app.route("/taryfikatsiya/zatverdyty", methods=["POST"])
def taryfikatsiya_zatverdyty():
    """Затвердити річні години: зафіксувати поточну тарифікацію - від неї далі
    рахує "Залишки годин". Повторне затвердження замінює попереднє, але лише
    доки не було передач годин (інакше передані залишки задвоїлись би)."""
    conn = get_connection()
    if conn.execute("SELECT 1 FROM peredachi_hodyn LIMIT 1").fetchone():
        conn.close()
        flash("Уже є передачі годин між учителями — повторно затвердити не можна, "
              "інакше передані залишки порахуються двічі. Спершу скасуйте передачі в «Залишках годин».", "error")
        return redirect(url_for("taryfikatsiya"))
    zapysy_po_vchytelyu, _ = _taryfikatsiya_dani(conn)
    koly = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("DELETE FROM zatverdzheni_hodyny")
    n = 0
    for g in zapysy_po_vchytelyu:
        for z in g["zapysy"]:
            conn.execute(
                "INSERT INTO zatverdzheni_hodyny (vchytel_id, vchytel_pib, predmet_id, predmet_nazva, hodyn_rik, zatverdzheno) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (z["vchytel_id"], z["vchytel_pib"], z["predmet_id"], z["predmet_nazva"], z["godyn_rik"], koly),
            )
            n += 1
    conn.commit()
    conn.close()
    flash(f"Річні години затверджено: {n} пар вчитель-предмет. «Залишки годин» тепер рахують від них.", "success")
    return redirect(url_for("taryfikatsiya"))


@app.route("/taryfikatsiya/skasuvaty-zatverdzhennya", methods=["POST"])
def taryfikatsiya_skasuvaty_zatverdzhennya():
    conn = get_connection()
    if conn.execute("SELECT 1 FROM peredachi_hodyn LIMIT 1").fetchone():
        conn.close()
        flash("Уже є передачі годин між учителями — спершу скасуйте їх у «Залишках годин».", "error")
        return redirect(url_for("taryfikatsiya"))
    conn.execute("DELETE FROM zatverdzheni_hodyny")
    conn.commit()
    conn.close()
    flash("Затвердження скасовано — «Залишки годин» знову рахують заплановане з поточного плану.", "success")
    return redirect(url_for("taryfikatsiya"))


@app.route("/taryfikatsiya/excel")
def taryfikatsiya_excel():
    """Той самий звіт у Excel - для бухгалтерії. Два листи: детально по рядках
    (вчитель / предмет / год на тиждень / год на рік / класи) з рядком "Разом" на
    вчителя, і зведення по вчителях."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from flask import send_file

    conn = get_connection()
    zapysy_po_vchytelyu, tyzhniv = _taryfikatsiya_dani(conn)
    conn.close()

    wb = Workbook()
    zagolovok_fill = PatternFill(start_color="1ABC9C", end_color="1ABC9C", fill_type="solid")
    zagolovok_font = Font(bold=True, color="FFFFFF")
    razom_font = Font(bold=True)
    uvaha = Font(bold=True, color="8A6100")

    ws = wb.active
    ws.title = "Тарифікація"
    ws.append(["Вчитель", "Предмет", "Годин / тиждень", f"Годин / рік ({tyzhniv} тижнів)", "Розподілено по класах"])
    for c in ws[1]:
        c.fill, c.font = zagolovok_fill, zagolovok_font
    for g in zapysy_po_vchytelyu:
        for z in g["zapysy"]:
            ws.append([g["vchytel_pib"], z["predmet_nazva"], z["rozpodileno_tyzhden"], z["godyn_rik"], z["klasy_opys"]])
        ws.append([g["vchytel_pib"], "Разом", g["razom_tyzhden"], g["razom_rik"],
                   f"понад 1,5 ставки ({MAKS_NAVANTAZHENNYA_GODYN}) - перевірте" if g["ponad_stelyu"] else ""])
        for c in ws[ws.max_row]:
            c.font = uvaha if g["ponad_stelyu"] else razom_font
    for kol, shyryna in zip("ABCDE", (34, 30, 16, 20, 60)):
        ws.column_dimensions[kol].width = shyryna
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Зведення")
    ws2.append(["Вчитель", "Годин / тиждень", f"Годин / рік ({tyzhniv} тижнів)", "Ставок (по 18 год)", "Примітка"])
    for c in ws2[1]:
        c.fill, c.font = zagolovok_fill, zagolovok_font
    for g in zapysy_po_vchytelyu:
        prymitka = (f"понад 1,5 ставки ({MAKS_NAVANTAZHENNYA_GODYN}) - перевірте" if g["ponad_stelyu"]
                    else "понад ставку" if g["ponad_stavku"] else "")
        ws2.append([g["vchytel_pib"], g["razom_tyzhden"], g["razom_rik"],
                    round(g["razom_tyzhden"] / STAVKA_GODYN, 2), prymitka])
        if g["ponad_stelyu"]:
            for c in ws2[ws2.max_row]:
                c.font = uvaha
    ws2.append(["Разом", sum(g["razom_tyzhden"] for g in zapysy_po_vchytelyu),
                sum(g["razom_rik"] for g in zapysy_po_vchytelyu), "", ""])
    for c in ws2[ws2.max_row]:
        c.font = razom_font
    for kol, shyryna in zip("ABCDE", (34, 16, 20, 18, 26)):
        ws2.column_dimensions[kol].width = shyryna
    ws2.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    nazva = f"taryfikatsiya_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=nazva,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _vtracheno_na_nenavchalnyh(conn, na_datu, settings, semestr=None):
    """Дві мапи {(vchytel_id, predmet_id): годин} по календарю днів до na_datu:
    (втрачено, відпрацьовано).

    Свято й карантин ВІДНІМАЮТЬ уроки того дня тижня (борг), відпрацювання
    ('robochyi', звич. субота за розкладом іншого дня) ДОДАЄ. Числа повертаються
    ОКРЕМО, а не згорнутими в баланс: завуч має бачити і скільки втрачено, і
    скільки відпрацьовано, навіть коли вони збігаються й борг уже закритий.

    Джерело тижневої сітки - ЗГЕНЕРОВАНИЙ розклад: лише він знає, у який день
    тижня стоїть конкретний урок. Якщо розкладу ще немає - повертає порожньо
    (нічого відняти не можемо, і вигадувати не будемо).

    Парність врахована: запис 'parnyi' стоїть лише в парні тижні, тож свято в
    непарний тиждень його не зачіпає."""
    if not settings or not settings["semestr1_pochatok"]:
        return {}, {}
    nenavchalni_mapa = _nenavchalni_dni_mapa(conn)
    nenavchalni = sorted(nenavchalni_mapa)
    if not nenavchalni:
        return {}, {}

    # (den, parnist) -> {(vchytel_id, predmet_id): к-сть уроків того дня тижня} -
    # ОКРЕМО для кожного джерела розкладу (варіант, що діяв у той день, чи поточний),
    # бо посеред року розклад міг змінитись (v0.0.37: "діє з" у варіантів)
    po_dnyah_dzherel = {}   # ключ джерела -> po_dnyah

    def _po_dnyah(d_str):
        tbl, variant = _dzherelo_rozkladu(conn, d_str)
        klyuch_dzherela = variant["id"] if variant else None
        if klyuch_dzherela in po_dnyah_dzherel:
            return po_dnyah_dzherel[klyuch_dzherela]
        po_dnyah = {}
        # DISTINCT по слоту: урок клас-комплекту лежить окремим рядком на кожен клас,
        # а для вчителя це ОДИН урок в один час - рахуємо слоти, не рядки
        for r in conn.execute(
            f"""SELECT DISTINCT den, urok, parnist, vchytel_id, pidgrupa_2_vchytel_id, predmet_id
                FROM {tbl} WHERE predmet_id IS NOT NULL"""
        ).fetchall():
            par = r["parnist"] or "oboydva"
            for vid in (r["vchytel_id"], r["pidgrupa_2_vchytel_id"]):
                if vid is None:
                    continue
                klyuch = (r["den"], par)
                po_dnyah.setdefault(klyuch, {})
                k2 = (vid, r["predmet_id"])
                po_dnyah[klyuch][k2] = po_dnyah[klyuch].get(k2, 0) + 1
        po_dnyah_dzherel[klyuch_dzherela] = po_dnyah
        return po_dnyah

    pershyi_neparnyi = settings["pershyi_tyzhden_neparnyi"] if settings else 1
    s1_poch = settings["semestr1_pochatok"]
    s2_poch = settings["semestr2_pochatok"]
    vtracheno, vidpratsovano = {}, {}
    for d_str in nenavchalni:
        if d_str > na_datu:
            continue
        # лише дні одного семестру (v0.2.0): дата до початку ІІ семестру - це І
        if semestr is not None and _semestr_daty(d_str, s2_poch) != semestr:
            continue
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d")
        except ValueError:
            continue
        kod = nenavchalni_mapa[d_str]
        typ, _, za_den_str = kod.partition(":")
        if typ == "robochyi":
            # відпрацювання: школа працює за розкладом ІНШОГО дня тижня
            den = int(za_den_str or 1)
            kudy = vidpratsovano
        else:
            den = d.weekday() + 1                   # пн=1
            kudy = vtracheno
        par_tyzhnya = _parnist_na_datu(d_str, s1_poch, pershyi_neparnyi)
        po_dnyah = _po_dnyah(d_str)
        for par in ("oboydva", par_tyzhnya):
            for klyuch, kilkist in po_dnyah.get((den, par), {}).items():
                kudy[klyuch] = kudy.get(klyuch, 0) + kilkist
    return vtracheno, vidpratsovano


def data_ua(d):
    """'2026-10-15' (або з часом) -> '15.10.2026'."""
    return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%d.%m.%Y") if d else ""


app.jinja_env.globals["data_ua"] = data_ua


def _semestr_daty(d_str, s2_poch):
    """Семестр, якому належить дата: до початку ІІ семестру - І (і коли дату ІІ
    семестру ще не задано). Події між семестрами (зимові канікули) йдуть у І."""
    return 2 if s2_poch and d_str >= s2_poch else 1


_ZALYSHKY_POLYA = ("zaplanovano", "godyn_tyzhden", "skasovano", "vtracheno",
                   "vidpratsovano", "po_zamini", "prochytano")


def _zalyshky_semestru(conn, settings, semestr, na_datu):
    """"Залишки годин" ОДНОГО семестру станом на na_datu (v0.2.0):
    {(vchytel_id, predmet_id): {...}}.

    Рядки плану - "весь рік" + цього семестру; тижні - лише свого семестру (І + ІІ
    = тижні року з «Налаштувань», див. _tyzhni_semestriv); заміни й ненавчальні дні
    - лише дати цього семестру. Для предметів "весь рік" сума двох семестрів дає
    точно ті самі числа, що й річний підрахунок до появи семестрів.

    prochytano = max(0, прогноз за календарем - скасовано - втрачено + відпрацьовано)
                 + проведено по заміні."""
    s1p = settings["semestr1_pochatok"] if settings else None
    s1k = settings["semestr1_kinets"] if settings else None
    s2p = settings["semestr2_pochatok"] if settings else None
    s2k = settings["semestr2_kinets"] if settings else None
    kanikuly = _kanikuly_periody(settings)
    pershyi = settings["pershyi_tyzhden_neparnyi"] if settings else 1

    # тижні "щотижневих" рядків: розбиваємо річний лічильник так, щоб І + ІІ точно
    # дорівнювали старому річному підрахунку (днів / 7 від початку року)
    t1, t2 = _tyzhni_semestriv(conn)
    tyzhniv_projshlo = _calc_tyzhniv_do_daty(na_datu, s1p, s1k, s2p, s2k, kanikuly_periody=kanikuly)
    e1 = min(_calc_tyzhniv_do_daty(na_datu, s1p, s1k, None, None, kanikuly_periody=kanikuly), t1)
    e2 = max(0, min(tyzhniv_projshlo - e1, t2))
    tyzhniv = {1: t1, 2: t2}[semestr]
    minulo = {1: e1, 2: e2}[semestr]

    # тижні рядків "лише парний / лише непарний": точний потижневий підрахунок у
    # межах семестру (той самий, що й річний, лише з "вимкненим" іншим семестром)
    mezhi = {1: (s1p, s1k, None, None), 2: (s1p, None, s2p, s2k)}[semestr]
    kinets = {1: s1k, 2: s2k}[semestr]

    def _par(do_daty):
        if not (s1p and do_daty):
            return {"parnyi": 0, "neparnyi": 0}
        return _tyzhni_po_parnosti(do_daty, *mezhi, kanikuly, pershyi)

    par_vsogo, par_minulo = _par(kinets), _par(na_datu)

    rows = conn.execute(
        "SELECT vchytel_id, predmet_id, godyn_tyzhden, parnist, semestr, klas_id, paralel, litera, komplekt_id FROM ("
        "  SELECT np.vchytel_id, np.predmet_id, np.godyn_tyzhden, np.parnist, np.semestr,"
        "         np.klas_id, k.paralel, k.litera, k.komplekt_id"
        "   FROM navchalnyj_plan np JOIN klasy k ON k.id = np.klas_id"
        "   WHERE np.vchytel_id IS NOT NULL AND np.semestr IN (0, ?)"
        "  UNION ALL"
        "  SELECT np.pidgrupa_2_vchytel_id, np.predmet_id, np.godyn_tyzhden, np.parnist, np.semestr,"
        "         np.klas_id, k.paralel, k.litera, k.komplekt_id"
        "   FROM navchalnyj_plan np JOIN klasy k ON k.id = np.klas_id"
        "   WHERE np.pidgrupa_2_vchytel_id IS NOT NULL AND np.semestr IN (0, ?)"
        ")",
        (semestr, semestr),
    ).fetchall()
    po_klyuchu = {}
    for r in rows:
        po_klyuchu.setdefault((r["vchytel_id"], r["predmet_id"]), []).append(r)

    # заміни лише дат цього семестру
    if semestr == 1:
        umova_daty, param_daty = ("AND data < ?", (s2p,)) if s2p else ("", ())
    else:
        umova_daty, param_daty = ("AND data >= ?", (s2p,)) if s2p else ("AND 0", ())
    skasovano = {
        (r[0], r[1]): r[2] for r in conn.execute(
            f"""SELECT planovanyi_vchytel_id, predmet_id, COUNT(*) FROM urok_log
                WHERE status IN ('skasovano', 'zamina') AND data <= ? {umova_daty}
                GROUP BY planovanyi_vchytel_id, predmet_id""",
            (na_datu,) + param_daty)
    }
    po_zamini = {
        (r[0], r[1]): r[2] for r in conn.execute(
            f"""SELECT faktychnyi_vchytel_id, COALESCE(faktychnyi_predmet_id, predmet_id), COUNT(*)
                FROM urok_log WHERE status = 'zamina' AND data <= ? {umova_daty}
                GROUP BY faktychnyi_vchytel_id, COALESCE(faktychnyi_predmet_id, predmet_id)""",
            (na_datu,) + param_daty)
    }
    vtracheno, vidpratsovano = _vtracheno_na_nenavchalnyh(conn, na_datu, settings, semestr=semestr)
    pib = {r["id"]: r["pib"] for r in conn.execute("SELECT id, pib FROM vchyteli")}
    nazva = {r["id"]: r["nazva"] for r in conn.execute("SELECT id, nazva FROM predmety")}

    rezultat = {}
    for klyuch, rows_k in po_klyuchu.items():
        zaplanovano = proekciya = godyn_tyzhden = 0
        # класи одного клас-комплекту - ОДИН урок для вчителя (як і в Тарифікації)
        for z in _zlyty_hodyny_komplektu(rows_k):
            for par in ("oboydva", "parnyi", "neparnyi"):
                hodyn = z[par]
                if not hodyn:
                    continue
                if par == "oboydva":
                    vsogo, proishlo = tyzhniv, minulo
                else:
                    vsogo, proishlo = par_vsogo.get(par, 0), par_minulo.get(par, 0)
                godyn_tyzhden += hodyn * (1 if par == "oboydva" else 0.5)
                zaplanovano += hodyn * vsogo
                proekciya += min(hodyn * proishlo, hodyn * vsogo)
        sk = skasovano.get(klyuch, 0)
        vt = vtracheno.get(klyuch, 0)
        vp = vidpratsovano.get(klyuch, 0)
        zm = po_zamini.get(klyuch, 0)
        rezultat[klyuch] = {
            "vchytel_id": klyuch[0], "predmet_id": klyuch[1],
            "vchytel_pib": pib.get(klyuch[0], "?"), "predmet_nazva": nazva.get(klyuch[1], "?"),
            "zaplanovano": zaplanovano, "godyn_tyzhden": godyn_tyzhden,
            "skasovano": sk, "vtracheno": vt, "vidpratsovano": vp, "po_zamini": zm,
            "prochytano": max(0, proekciya - sk - vt + vp) + zm,
        }
    return rezultat


def _znimok_semestru(conn, semestr):
    """Збережений підсумок закритого семестру - у тому ж вигляді, що й
    _zalyshky_semestru."""
    return {
        (r["vchytel_id"], r["predmet_id"]): dict(r)
        for r in conn.execute("SELECT * FROM pidsumky_semestru WHERE semestr = ?", (semestr,))
    }


def _zakryti_semestry(conn):
    return {r["semestr"]: dict(r) for r in conn.execute("SELECT * FROM zakryti_semestry")}


def _zalyshky_semestru_skorygovano(conn, settings, semestr, na_datu):
    """_zalyshky_semestru + передачі годин. Після передачі завуч міняє вчителя в
    плані, і живий підрахунок (він завжди від ПОТОЧНОГО плану) переписав би
    історію: попередній учитель "нічого не прочитав", новий - "читав з початку
    семестру". Тому для кожної пари вчитель-предмет беремо ОСТАННЮ передачу, яка
    її стосується: прочитане = знімок на дату передачі + приріст живого
    підрахунку після цієї дати."""
    zhyvi = _zalyshky_semestru(conn, settings, semestr, na_datu)
    ostanni = {}
    for r in conn.execute(
        """SELECT pz.vchytel_id, ph.predmet_id, ph.predmet_nazva, pz.data_znimka, pz.prochytano,
                  CASE WHEN pz.vchytel_id = ph.vid_vchytel_id THEN ph.vid_pib ELSE ph.do_pib END AS pib
           FROM peredachi_znimky pz JOIN peredachi_hodyn ph ON ph.id = pz.peredacha_id
           WHERE pz.semestr = ? AND pz.data_znimka <= ?
           ORDER BY pz.data_znimka, ph.id""",
        (semestr, na_datu),
    ):
        ostanni[(r["vchytel_id"], r["predmet_id"])] = r
    na_daty = {}
    for klyuch, r in ostanni.items():
        d = r["data_znimka"]
        if d not in na_daty:
            na_daty[d] = _zalyshky_semestru(conn, settings, semestr, d)
        bulo_na_datu = na_daty[d].get(klyuch, {}).get("prochytano", 0)
        z = zhyvi.get(klyuch)
        if z is None:
            # учителя вже прибрали з плану - рядок лишається з прочитаним до передачі
            z = {p: 0 for p in _ZALYSHKY_POLYA}
            z.update({"vchytel_id": klyuch[0], "predmet_id": klyuch[1],
                      "vchytel_pib": r["pib"], "predmet_nazva": r["predmet_nazva"]})
            zhyvi[klyuch] = z
        z["prochytano"] = max(0, r["prochytano"] + z["prochytano"] - bulo_na_datu)
    return zhyvi


def _zatverdzheni_hodyny(conn, na_datu):
    """{(vchytel_id, predmet_id): {...}} - затверджені річні години з урахуванням
    передач до na_datu включно; None, якщо річні години не затверджували."""
    rows = conn.execute("SELECT * FROM zatverdzheni_hodyny").fetchall()
    if not rows:
        return None
    rezultat = {(r["vchytel_id"], r["predmet_id"]): {
        "hodyn": r["hodyn_rik"], "vchytel_pib": r["vchytel_pib"], "predmet_nazva": r["predmet_nazva"]} for r in rows}
    for p in conn.execute("SELECT * FROM peredachi_hodyn WHERE data <= ? ORDER BY data, id", (na_datu,)):
        for vid, pib, znak in ((p["vid_vchytel_id"], p["vid_pib"], -1), (p["do_vchytel_id"], p["do_pib"], 1)):
            z = rezultat.setdefault((vid, p["predmet_id"]), {
                "hodyn": 0, "vchytel_pib": pib, "predmet_nazva": p["predmet_nazva"]})
            z["hodyn"] = max(0, z["hodyn"] + znak * p["hodyn"])
    return rezultat


def _settings_zalyshkiv(conn):
    return conn.execute(
        "SELECT kilkist_navchalnyh_tyzhniv, semestr1_pochatok, semestr1_kinets, "
        "semestr2_pochatok, semestr2_kinets, kanikuly1_e, kanikuly1_pochatok, kanikuly1_kinets, "
        "kanikuly2_e, kanikuly2_pochatok, kanikuly2_kinets, pershyi_tyzhden_neparnyi "
        "FROM school_settings WHERE id = 1"
    ).fetchone()


def _zalyshky_dani(conn, na_datu):
    """Обидва семестри (закритий - зі знімка, відкритий - живий підрахунок з
    поправками передач) і річні підсумки по кожній парі вчитель-предмет.

    "Всього годин" за рік - затверджені річні години (з урахуванням передач), а
    якщо їх не затверджували - сума семестрів за планом (для закритого семестру -
    як було на момент закриття). Пара, якої немає серед затверджених (напр. учителя
    додали в план пізніше, без передачі), рахується за планом і позначена
    (zatverdzheno=False)."""
    settings = _settings_zalyshkiv(conn)
    zakryti = _zakryti_semestry(conn)
    t1, t2 = _tyzhni_semestriv(conn)
    tyzhniv_rik = (t1 + t2) or 1
    bloky = {}
    for semestr in (1, 2):
        bloky[semestr] = (_znimok_semestru(conn, semestr) if semestr in zakryti
                          else _zalyshky_semestru_skorygovano(conn, settings, semestr, na_datu))
    zatverdzheni = _zatverdzheni_hodyny(conn, na_datu)
    nul = {p: 0 for p in _ZALYSHKY_POLYA}
    riadky = []
    for klyuch in set(bloky[1]) | set(bloky[2]) | set(zatverdzheni or {}):
        b1, b2 = bloky[1].get(klyuch, nul), bloky[2].get(klyuch, nul)
        dzherelo = bloky[1].get(klyuch) or bloky[2].get(klyuch) or zatverdzheni[klyuch]
        z = (zatverdzheni or {}).get(klyuch)
        zaplanovano = z["hodyn"] if z else b1["zaplanovano"] + b2["zaplanovano"]
        prochytano = b1["prochytano"] + b2["prochytano"]
        riadky.append({
            "vchytel_id": klyuch[0], "predmet_id": klyuch[1],
            "vchytel_pib": dzherelo["vchytel_pib"], "predmet_nazva": dzherelo["predmet_nazva"],
            "s1": b1, "s2": b2,
            "zaplanovano": round(zaplanovano, 2),
            "zatverdzheno": z is not None or zatverdzheni is None,
            "godyn_tyzhden": (b1["godyn_tyzhden"] * t1 + b2["godyn_tyzhden"] * t2) / tyzhniv_rik,
            "prochytano": prochytano,
            "zalyshok": round(zaplanovano - prochytano, 2),
        })
    riadky.sort(key=lambda r: (ukr_klyuch(r["vchytel_pib"]), ukr_klyuch(r["predmet_nazva"])))
    return riadky, zakryti, settings


@app.route("/zalyshky-godyn", methods=["GET", "POST"])
def zalyshky_godyn():
    from generator import potochnyi_semestr
    conn = get_connection()
    na_datu = request.form.get("na_datu") or request.args.get("na_datu") or datetime.now().strftime("%Y-%m-%d")
    filter_vchytel_id = request.form.get("vchytel_id") or request.args.get("vchytel_id") or ""
    filter_predmet_id = request.form.get("predmet_id") or request.args.get("predmet_id") or ""
    # що показувати: один семестр (типово - поточний, саме він цікавить завуча
    # щодня) або весь рік обома блоками поруч
    period = request.form.get("period") or request.args.get("period") or str(potochnyi_semestr(conn))
    if period not in ("1", "2", "rik"):
        period = "1"

    riadky, zakryti, settings = _zalyshky_dani(conn, na_datu)
    if filter_vchytel_id:
        riadky = [r for r in riadky if str(r["vchytel_id"]) == filter_vchytel_id]
    if filter_predmet_id:
        riadky = [r for r in riadky if str(r["predmet_id"]) == filter_predmet_id]

    tyzhniv_vsogo = settings["kilkist_navchalnyh_tyzhniv"] if settings else 35
    tyzhniv_projshlo = _calc_tyzhniv_do_daty(
        na_datu,
        settings["semestr1_pochatok"] if settings else None,
        settings["semestr1_kinets"] if settings else None,
        settings["semestr2_pochatok"] if settings else None,
        settings["semestr2_kinets"] if settings else None,
        kanikuly_periody=_kanikuly_periody(settings),
    )
    semestry_info = []
    for semestr in (1, 2):
        poch = settings[f"semestr{semestr}_pochatok"] if settings else None
        kin = settings[f"semestr{semestr}_kinets"] if settings else None
        semestry_info.append({
            "nomer": semestr, "nazva": "І семестр" if semestr == 1 else "ІІ семестр",
            "poch": poch, "kin": kin, "zakryto": zakryti.get(semestr),
        })

    zatverdzheno_koly = conn.execute("SELECT MIN(zatverdzheno) FROM zatverdzheni_hodyny").fetchone()[0]
    peredachi = conn.execute("SELECT * FROM peredachi_hodyn ORDER BY data, id").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    conn.close()
    return render_template(
        "zalyshky_godyn.html", active="zalyshky_godyn", zapysy=riadky, period=period,
        zatverdzheno_koly=zatverdzheno_koly, peredachi=peredachi,
        na_datu=na_datu, tyzhniv_projshlo=tyzhniv_projshlo, tyzhniv_vsogo=tyzhniv_vsogo,
        semestry_zapovneni=bool(settings and settings["semestr1_pochatok"] and settings["semestr1_kinets"]),
        semestry_info=semestry_info,
        all_vchyteli=all_vchyteli, all_predmety=all_predmety,
        filter_vchytel_id=filter_vchytel_id, filter_predmet_id=filter_predmet_id,
    )


@app.route("/zalyshky-godyn/zakryty-semestr", methods=["POST"])
def zalyshky_zakryty_semestr():
    """Закриття семестру: фіксуємо "Залишки годин" цього семестру знімком станом
    на кінець семестру (або на сьогодні, якщо закривають раніше) - і далі звіт
    бере для нього знімок. "Відкрити знову" знімок прибирає, і семестр знову
    рахується наживо (напр. щоб дописати пропущену заміну й закрити ще раз)."""
    semestr = request.form.get("semestr", type=int)
    if semestr not in (1, 2):
        abort(400)
    conn = get_connection()
    if conn.execute("SELECT 1 FROM zakryti_semestry WHERE semestr = ?", (semestr,)).fetchone():
        conn.close()
        flash("Цей семестр уже закрито.", "error")
        return redirect(url_for("zalyshky_godyn", period=semestr))
    settings = _settings_zalyshkiv(conn)
    kinets = settings[f"semestr{semestr}_kinets"] if settings else None
    if not kinets:
        conn.close()
        flash("Спершу задайте дати семестрів у «Налаштуваннях школи».", "error")
        return redirect(url_for("zalyshky_godyn", period=semestr))
    sohodni = datetime.now().strftime("%Y-%m-%d")
    stanom_na = min(sohodni, kinets)
    blok = _zalyshky_semestru_skorygovano(conn, settings, semestr, stanom_na)
    conn.execute("INSERT INTO zakryti_semestry (semestr, stanom_na, zakryto_koly) VALUES (?, ?, ?)",
                 (semestr, stanom_na, datetime.now().strftime("%Y-%m-%d %H:%M")))
    for r in blok.values():
        conn.execute(
            f"""INSERT INTO pidsumky_semestru (semestr, vchytel_id, vchytel_pib, predmet_id, predmet_nazva,
                {", ".join(_ZALYSHKY_POLYA)}) VALUES (?, ?, ?, ?, ?, {", ".join("?" * len(_ZALYSHKY_POLYA))})""",
            (semestr, r["vchytel_id"], r["vchytel_pib"], r["predmet_id"], r["predmet_nazva"],
             *(r[p] for p in _ZALYSHKY_POLYA)),
        )
    conn.commit()
    conn.close()
    nazva = "І" if semestr == 1 else "ІІ"
    flash(f"{nazva} семестр закрито: прочитане зафіксовано станом на {datetime.strptime(stanom_na, "%Y-%m-%d").strftime("%d.%m.%Y")} "
          f"({len(blok)} пар вчитель-предмет).", "success")
    return redirect(url_for("zalyshky_godyn", period=semestr))


@app.route("/zalyshky-godyn/peredaty", methods=["GET", "POST"])
def zalyshky_peredaty():
    """Учитель змінився посеред року: попередній "закривається" (його річні =
    прочитане на дату передачі, рядок стає зеленим), залишок переходить новому,
    і новому рахується лише прочитане з дати передачі."""
    vid_id = request.values.get("vchytel_id", type=int)
    predmet_id = request.values.get("predmet_id", type=int)
    data = request.values.get("data") or datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    settings = _settings_zalyshkiv(conn)
    zakryti = _zakryti_semestry(conn)
    errors = []
    if not conn.execute("SELECT 1 FROM zatverdzheni_hodyny LIMIT 1").fetchone():
        conn.close()
        flash("Спершу затвердьте річні години в «Тарифікації» — передавати можна лише від затвердженого.", "error")
        return redirect(url_for("zalyshky_godyn"))
    riadky, _, _ = _zalyshky_dani(conn, data)
    riadok = next((r for r in riadky if r["vchytel_id"] == vid_id and r["predmet_id"] == predmet_id), None)
    if riadok is None:
        conn.close()
        abort(404)
    peredaty_hodyn = max(0, round(riadok["zaplanovano"] - riadok["prochytano"], 2))
    vsi_vchyteli = conn.execute(
        "SELECT id, pib FROM vchyteli WHERE id != ? ORDER BY pib COLLATE UKR", (vid_id,)).fetchall()

    if request.method == "POST":
        do_id = request.form.get("do_vchytel_id", type=int)
        do = conn.execute("SELECT id, pib FROM vchyteli WHERE id = ?", (do_id,)).fetchone()
        s2p = settings["semestr2_pochatok"] if settings else None
        semestr_daty = _semestr_daty(data, s2p)
        ostannya = conn.execute(
            "SELECT MAX(data) FROM peredachi_hodyn WHERE predmet_id = ? AND ? IN (vid_vchytel_id, do_vchytel_id)",
            (predmet_id, vid_id)).fetchone()[0]
        if not do or do_id == vid_id:
            errors.append("Оберіть учителя, якому переходять години.")
        if not (settings and settings["semestr1_pochatok"]):
            errors.append("Спершу задайте дати семестрів у «Налаштуваннях школи».")
        if semestr_daty in zakryti:
            errors.append(f"{'І' if semestr_daty == 1 else 'ІІ'} семестр, у який припадає ця дата, уже закрито — "
                          "відкрийте його знову або оберіть пізнішу дату.")
        if ostannya and data < ostannya:
            errors.append(f"Для цього предмета вже є передача від {data_ua(ostannya)} — нова не може бути раніше.")
        if not errors:
            # знімки прочитаного обома вчителями на дату передачі - до запису самої
            # передачі (тож враховують попередні передачі) і по кожному ще не
            # закритому семестру, що вже почався
            znimky = []
            for semestr in (1, 2):
                if semestr in zakryti or semestr > semestr_daty:
                    continue
                d = data if semestr == semestr_daty else min(data, settings["semestr1_kinets"] or data)
                blok = _zalyshky_semestru_skorygovano(conn, settings, semestr, d)
                for vchytel in (vid_id, do_id):
                    znimky.append((vchytel, semestr, d, blok.get((vchytel, predmet_id), {}).get("prochytano", 0)))
            kursor = conn.execute(
                "INSERT INTO peredachi_hodyn (data, predmet_id, predmet_nazva, vid_vchytel_id, vid_pib, "
                "do_vchytel_id, do_pib, hodyn, stvoreno) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (data, predmet_id, riadok["predmet_nazva"], vid_id, riadok["vchytel_pib"], do_id, do["pib"],
                 peredaty_hodyn, datetime.now().strftime("%Y-%m-%d %H:%M")))
            for vchytel, semestr, d, prochytano in znimky:
                conn.execute("INSERT INTO peredachi_znimky (peredacha_id, vchytel_id, semestr, data_znimka, prochytano) "
                             "VALUES (?, ?, ?, ?, ?)", (kursor.lastrowid, vchytel, semestr, d, prochytano))
            zaminyty = request.form.get("zaminyty_v_plani") == "on"
            n = 0
            if zaminyty:
                n += conn.execute("UPDATE navchalnyj_plan SET vchytel_id = ? WHERE vchytel_id = ? AND predmet_id = ?",
                                  (do_id, vid_id, predmet_id)).rowcount
                n += conn.execute("UPDATE navchalnyj_plan SET pidgrupa_2_vchytel_id = ? "
                                  "WHERE pidgrupa_2_vchytel_id = ? AND predmet_id = ?",
                                  (do_id, vid_id, predmet_id)).rowcount
            conn.commit()
            conn.close()
            flash(f"«{riadok['predmet_nazva']}»: з {data_ua(data)} веде {do['pib']}, передано {peredaty_hodyn:g} год. "
                  + (f"У навчальному плані замінено рядків: {n}. Розклад досі з попереднім учителем — "
                     "згенеруйте його заново або догенеруйте." if zaminyty else
                     "Не забудьте замінити вчителя в навчальному плані."), "success")
            return redirect(url_for("zalyshky_godyn", vchytel_id=do_id, predmet_id=predmet_id))

    conn.close()
    return render_template(
        "zalyshky_peredaty.html", active="zalyshky_godyn", riadok=riadok, data=data,
        peredaty_hodyn=peredaty_hodyn, vsi_vchyteli=vsi_vchyteli, errors=errors,
    )


@app.route("/zalyshky-godyn/peredachi/<int:peredacha_id>/skasuvaty", methods=["POST"])
def zalyshky_skasuvaty_peredachu(peredacha_id):
    """Скасувати можна лише останню передачу з цього предмета - пізніші
    спираються на знімки попередніх. Вчителя в плані назад не повертаємо."""
    conn = get_connection()
    p = conn.execute("SELECT * FROM peredachi_hodyn WHERE id = ?", (peredacha_id,)).fetchone()
    if p is None:
        conn.close()
        abort(404)
    ostannya = conn.execute("SELECT id FROM peredachi_hodyn WHERE predmet_id = ? ORDER BY data DESC, id DESC LIMIT 1",
                            (p["predmet_id"],)).fetchone()
    if ostannya["id"] != peredacha_id:
        conn.close()
        flash("Скасувати можна лише останню передачу з цього предмета.", "error")
        return redirect(url_for("zalyshky_godyn"))
    conn.execute("DELETE FROM peredachi_hodyn WHERE id = ?", (peredacha_id,))
    conn.commit()
    conn.close()
    flash(f"Передачу «{p['predmet_nazva']}» ({p['vid_pib']} → {p['do_pib']}) скасовано. Учителя в навчальному "
          "плані поверніть вручну, якщо треба.", "success")
    return redirect(url_for("zalyshky_godyn"))


@app.route("/zalyshky-godyn/vidkryty-semestr", methods=["POST"])
def zalyshky_vidkryty_semestr():
    semestr = request.form.get("semestr", type=int)
    if semestr not in (1, 2):
        abort(400)
    conn = get_connection()
    conn.execute("DELETE FROM zakryti_semestry WHERE semestr = ?", (semestr,))
    conn.commit()
    conn.close()
    nazva = "І" if semestr == 1 else "ІІ"
    flash(f"{nazva} семестр відкрито знову — він рахується наживо. Закрийте його ще раз, коли все виправите.",
          "success")
    return redirect(url_for("zalyshky_godyn", period=semestr))


def _read_asystent_form(form):
    predmet_ids = form.getlist("predmet_ids")
    return {
        "pib": form["pib"].strip(),
        "klas_id": int(form["klas_id"]) if form.get("klas_id") else None,
        "stavka": float(form.get("stavka") or 1.0),
        "suprovid": form.get("suprovid", "vsi"),
        "predmet_ids": [int(x) for x in predmet_ids],
    }


def _save_asystent_predmety(conn, asystent_id, predmet_ids):
    conn.execute("DELETE FROM asystenty_predmety WHERE asystent_id = ?", (asystent_id,))
    for pid in predmet_ids:
        conn.execute(
            "INSERT INTO asystenty_predmety (asystent_id, predmet_id) VALUES (?, ?)",
            (asystent_id, pid),
        )


@app.route("/asystenty")
def asystenty():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT a.*, k.paralel, k.litera
        FROM asystenty a
        JOIN klasy k ON k.id = a.klas_id
        ORDER BY a.pib COLLATE UKR
        """
    ).fetchall()
    conn.close()
    return render_template("asystenty_list.html", active="asystenty", asystenty=rows)


@app.route("/asystenty/bulk", methods=["GET", "POST"])
def asystenty_bulk():
    conn = get_connection()
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()

    if request.method == "POST":
        klas_id = request.form.get("klas_id") or None
        raw = request.form.get("pib_list", "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        errors = []
        if not klas_id:
            errors.append("Оберіть клас.")

        if errors:
            conn.close()
            return render_template(
                "asystenty_bulk.html", active="asystenty", all_klasy=all_klasy,
                message=None, errors=errors,
            )

        klas_id = int(klas_id)
        existing_pib = {
            row["pib"].strip().lower() for row in conn.execute(
                "SELECT pib FROM asystenty WHERE klas_id = ?", (klas_id,)
            ).fetchall()
        }

        created = 0
        skipped = []
        for pib in lines:
            if pib.lower() in existing_pib:
                skipped.append(pib)
                continue
            conn.execute(
                "INSERT INTO asystenty (pib, klas_id, stavka, suprovid) VALUES (?, ?, 1.0, 'vsi')",
                (pib, klas_id),
            )
            existing_pib.add(pib.lower())
            created += 1

        conn.commit()
        conn.close()

        message = f"Додано асистентів: {created}."
        if skipped:
            message += f" Пропущено (вже є в цьому класі): {len(skipped)} — {', '.join(skipped)}."
        return render_template(
            "asystenty_bulk.html", active="asystenty", all_klasy=all_klasy,
            message=message, errors=[],
        )

    conn.close()
    return render_template(
        "asystenty_bulk.html", active="asystenty", all_klasy=all_klasy, message=None, errors=[],
    )


@app.route("/asystenty/new", methods=["GET", "POST"])
def asystenty_new():
    conn = get_connection()
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_asystent_form(request.form)
        if not data["pib"]:
            errors.append("ПІБ асистента обов'язкове.")
        if not data["klas_id"]:
            errors.append("Оберіть клас.")

        if not errors:
            cur = conn.execute(
                """
                INSERT INTO asystenty (pib, klas_id, stavka, suprovid)
                VALUES (?, ?, ?, ?)
                """,
                (data["pib"], data["klas_id"], data["stavka"], data["suprovid"]),
            )
            _save_asystent_predmety(conn, cur.lastrowid, data["predmet_ids"])
            conn.commit()
            conn.close()
            return redirect(url_for("asystenty"))

        conn.close()
        return render_template(
            "asystenty_form.html", active="asystenty", asystent=data,
            all_klasy=all_klasy, all_predmety=all_predmety, errors=errors,
        )

    conn.close()
    if not all_klasy:
        errors.append("Спершу додайте хоча б один клас.")
    return render_template(
        "asystenty_form.html", active="asystenty", asystent=None,
        all_klasy=all_klasy, all_predmety=all_predmety, errors=errors,
    )


@app.route("/asystenty/<int:asystent_id>/edit", methods=["GET", "POST"])
def asystenty_edit(asystent_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM asystenty WHERE id = ?", (asystent_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_asystent_form(request.form)
        if not data["pib"]:
            errors.append("ПІБ асистента обов'язкове.")
        if not data["klas_id"]:
            errors.append("Оберіть клас.")

        if not errors:
            conn.execute(
                """
                UPDATE asystenty SET pib = ?, klas_id = ?, stavka = ?, suprovid = ?
                WHERE id = ?
                """,
                (data["pib"], data["klas_id"], data["stavka"], data["suprovid"], asystent_id),
            )
            _save_asystent_predmety(conn, asystent_id, data["predmet_ids"])
            conn.commit()
            conn.close()
            return redirect(url_for("asystenty"))

        conn.close()
        data["id"] = asystent_id
        return render_template(
            "asystenty_form.html", active="asystenty", asystent=data,
            all_klasy=all_klasy, all_predmety=all_predmety, errors=errors,
        )

    selected_ids = {
        r["predmet_id"] for r in conn.execute(
            "SELECT predmet_id FROM asystenty_predmety WHERE asystent_id = ?", (asystent_id,)
        ).fetchall()
    }
    conn.close()
    asystent = dict(existing)
    asystent["predmet_ids"] = selected_ids
    return render_template(
        "asystenty_form.html", active="asystenty", asystent=asystent,
        all_klasy=all_klasy, all_predmety=all_predmety, errors=[],
    )


@app.route("/asystenty/<int:asystent_id>/delete", methods=["POST"])
def asystenty_delete(asystent_id):
    conn = get_connection()
    conn.execute("DELETE FROM asystenty WHERE id = ?", (asystent_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("asystenty"))




# Ваги складності предмета лише для ПОРАДИ користувачу при виборі парного/непарного
# тижня для дробового навантаження (розділ нижче) - не використовуються генератором,
# який працює з категоріями vazhkyi/serednii/legkyi напряму (generator.py).
_VAZHKIST_VAHA = {"vazhkyi": 2, "serednii": 1, "legkyi": 0.5}


# ---------- Семестр у рядку навчального плану (v0.2.0) ----------
# navchalnyj_plan.semestr: 0 = весь рік, 1 = лише І семестр, 2 = лише ІІ семестр.
# Предмет у класі - АБО "весь рік", АБО по семестрах (без змішування, стежить
# _validate_plan). Тоді рядки, що діють у семестрі s, - це "semestr IN (0, s)", і
# будь-яка логіка "один предмет у класі = одна група рядків парності" лишається
# вірною в межах семестру. Річні підсумки рахуються як сума по двох семестрах.
SEMESTR_NAZVY = {0: "весь рік", 1: "лише І семестр", 2: "лише ІІ семестр"}
SEMESTR_KOROTKO = {0: "", 1: "І сем.", 2: "ІІ сем."}
app.jinja_env.globals["SEMESTR_NAZVY"] = SEMESTR_NAZVY
app.jinja_env.globals["SEMESTR_KOROTKO"] = SEMESTR_KOROTKO


def _tyzhni_semestriv(conn):
    """(навчальних тижнів у І семестрі, у ІІ семестрі) - з дат у «Налаштуваннях
    школи» мінус канікули всередині семестру. Якщо дат нема - 16 і 19, як у
    Типовій освітній програмі профільної школи (наказ МОН №765: 112 год / 7 і
    171 год / 9 на тиждень)."""
    settings = conn.execute("SELECT * FROM school_settings WHERE id = 1").fetchone()
    kanikuly = _kanikuly_periody(settings)
    rezultat = []
    for poch_k, kin_k, typovo in (("semestr1_pochatok", "semestr1_kinets", 16),
                                  ("semestr2_pochatok", "semestr2_kinets", 19)):
        tyzhniv = None
        if settings and settings[poch_k] and settings[kin_k]:
            d1 = _parse_date_safe(settings[poch_k])
            d2 = _parse_date_safe(settings[kin_k])
            if d1 and d2 and d2 >= d1:
                dniv = (d2 - d1).days + 1 - sum(_overlap_days(d1, d2, k1, k2) for k1, k2 in kanikuly)
                tyzhniv = max(1, round(max(0, dniv) / 7))
        rezultat.append(tyzhniv or typovo)
    # ІІ семестр - залишок до тижнів року з «Налаштувань»: тоді І + ІІ рівно
    # дорівнює річним тижням, якими рахують Тарифікація й «Залишки годин», і для
    # предметів "весь рік" річні години по семестрах складаються точно в ті самі
    # числа (окреме округлення кожного семестру могло б дати ±1 тиждень)
    tyzhniv_rik = settings["kilkist_navchalnyh_tyzhniv"] if settings else None
    if tyzhniv_rik and tyzhniv_rik - rezultat[0] >= 1:
        rezultat[1] = tyzhniv_rik - rezultat[0]
    return tuple(rezultat)


def _potochnyi_semestr(conn):
    """Семестр, у якому школа зараз: ІІ - від дати початку ІІ семестру в
    «Налаштуваннях», інакше І (і коли дат іще не задано)."""
    s = conn.execute("SELECT semestr2_pochatok FROM school_settings WHERE id = 1").fetchone()
    d2 = _parse_date_safe(s["semestr2_pochatok"]) if s else None
    return 2 if d2 and datetime.now() >= d2 else 1


def _hodyny_tyzhnya_klasu(conn, klas_id, vyklyuchyty_predmet_id=None, semestr=None):
    """Як _hodyny_tyzhnya_klasu_semestru, але для semestr=None - НАЙГІРШИЙ із двох
    семестрів по кожному тижню. Норма МОН - стеля, яку не можна перевищити в
    жодному тижні жодного семестру, тож для перевірок без конкретного семестру
    береться пік. Якщо в класі немає семестрових рядків, обидва семестри однакові."""
    if semestr:
        return _hodyny_tyzhnya_klasu_semestru(conn, klas_id, vyklyuchyty_predmet_id, semestr)
    h1 = _hodyny_tyzhnya_klasu_semestru(conn, klas_id, vyklyuchyty_predmet_id, 1)
    h2 = _hodyny_tyzhnya_klasu_semestru(conn, klas_id, vyklyuchyty_predmet_id, 2)
    return {
        vyd: {t: max(h1[vyd][t], h2[vyd][t]) for t in ("parnyi", "neparnyi")}
        for vyd in ("z_fizkulturoyu", "bez_fizkultury")
    }


def _hodyny_tyzhnya_klasu_semestru(conn, klas_id, vyklyuchyty_predmet_id, semestr):
    """Реальна кількість УРОКІВ класу окремо для парного й непарного тижня (не
    'вага складності', як _balans_parnosti нижче, а фізична кількість годин - для
    звірки з нормою МОН). oboydva рахується в ОБИДВА тижні однаково, parnyi/neparnyi
    лише у свій. Повертає окремо 'з фізкультурою' (усі предмети) і 'без фізкультури'
    (лише ті, де predmety.vrahovuyetsya_u_hranychnomu) - як і 'Навантаження по
    класах', звіряється з двома різними нормами МОН (граничне/фактичне)."""
    rows = conn.execute(
        """
        SELECT np.parnist, np.godyn_tyzhden, np.predmet_id, np.mizhpredmetna_grupa_id,
               p.vrahovuyetsya_u_hranychnomu
        FROM navchalnyj_plan np JOIN predmety p ON p.id = np.predmet_id
        WHERE np.klas_id = ? AND np.semestr IN (0, ?)
        """,
        (klas_id, semestr),
    ).fetchall()
    rows = [r for r in rows if r["predmet_id"] != vyklyuchyty_predmet_id]

    # члени міжпредметної групи (різні предмети, ті самі слоти класу паралельно -
    # напр. половина класу англійська, половина німецька) фізично займають ОДИН час
    # класу. У навантаженні класу лишаємо з кожної групи ЛИШЕ представника з
    # найбільшими годинами, решту членів відкидаємо
    mizh_maks = {}
    for r in rows:
        gid = r["mizhpredmetna_grupa_id"]
        if gid and r["godyn_tyzhden"] > mizh_maks.get(gid, -1):
            mizh_maks[gid] = r["godyn_tyzhden"]
    vidkynuto = {gid: False for gid in mizh_maks}
    filtered = []
    for r in rows:
        gid = r["mizhpredmetna_grupa_id"]
        if gid:
            if r["godyn_tyzhden"] == mizh_maks[gid] and not vidkynuto[gid]:
                vidkynuto[gid] = True  # представник - лишаємо один раз
            else:
                continue
        filtered.append(r)
    rows = filtered

    def _dlya(recs):
        oboydva = sum(r["godyn_tyzhden"] for r in recs if r["parnist"] == "oboydva")
        parnyi = sum(r["godyn_tyzhden"] for r in recs if r["parnist"] == "parnyi")
        neparnyi = sum(r["godyn_tyzhden"] for r in recs if r["parnist"] == "neparnyi")
        return {"parnyi": oboydva + parnyi, "neparnyi": oboydva + neparnyi}

    return {
        "z_fizkulturoyu": _dlya(rows),
        "bez_fizkultury": _dlya([r for r in rows if r["vrahovuyetsya_u_hranychnomu"]]),
    }


def _balans_parnosti(conn, klas_id, vyklyuchyty_predmet_id=None, semestr=None):
    """Сумарна 'вага складності' предметів класу окремо для парного й непарного
    тижня (лише серед записів, що мають відповідну парність - oboydva на обидва
    тижні однаково й не впливає на РІЗНИЦЮ між ними). Використовується як підказка
    (не автоматичний вибір), на який тиждень краще класти нове дробове навантаження,
    щоб два тижні лишались приблизно рівними за складністю."""
    rows = conn.execute(
        """
        SELECT np.parnist, np.godyn_tyzhden, np.predmet_id, np.semestr, p.vazhkist
        FROM navchalnyj_plan np JOIN predmety p ON p.id = np.predmet_id
        WHERE np.klas_id = ? AND np.parnist != 'oboydva'
        """,
        (klas_id,),
    ).fetchall()
    # semestr=None - пік із двох семестрів (як _hodyny_tyzhnya_klasu): підказка
    # для рядка "весь рік" має враховувати найнавантаженіший семестр
    semestry = (semestr,) if semestr else (1, 2)
    vaha = {"parnyi": 0.0, "neparnyi": 0.0}
    for s in semestry:
        v = {"parnyi": 0.0, "neparnyi": 0.0}
        for r in rows:
            if r["predmet_id"] == vyklyuchyty_predmet_id or r["semestr"] not in (0, s):
                continue
            v[r["parnist"]] += r["godyn_tyzhden"] * _VAZHKIST_VAHA.get(r["vazhkist"], 1)
        for t in v:
            vaha[t] = max(vaha[t], v[t])
    return vaha


def _obraty_parnist_dlya_ostatku(conn, klas_id, predmet_id):
    """Автоматично обирає, на який тиждень (парний/непарний) покласти залишок 0.5
    год/тиждень при дробовому вводі в матриці вчителя. Пріоритет 1 - НЕ переганяти
    тиждень класу за норму МОН (граничне навантаження без фізкультури): якщо лише
    один із двох варіантів вкладається в норму, обираємо його незалежно від
    складності. Пріоритет 2 (якщо обидва вкладаються, або жоден не вкладається) -
    баланс складності (_balans_parnosti), як і раніше.

    Повертає (parnist, chy_peredovantazhuje) - друге True, якщо ОБИДВА варіанти
    перевищують норму (клас уже перевантажений незалежно від вибору) - виклик
    може попередити про це користувача."""
    # БЕЗ винятку по предмету - ціла частина (oboydva) ЦЬОГО предмета вже записана
    # в базу на момент виклику (у матриці вставляється РАНІШЕ виклику цієї функції)
    # і має рахуватись. Але старий парний/непарний рядок цього самого предмета (той,
    # що зараз замінюємо новим вибором) може ще бути в базі - віднімаємо його
    # окремо, інакше порахували б один і той самий залишок двічі.
    hodyny = _hodyny_tyzhnya_klasu(conn, klas_id)
    stari_parity = conn.execute(
        "SELECT parnist, godyn_tyzhden FROM navchalnyj_plan "
        "WHERE klas_id = ? AND predmet_id = ? AND parnist != 'oboydva' AND semestr = 0",
        (klas_id, predmet_id),
    ).fetchall()
    predmet_row = conn.execute("SELECT vrahovuyetsya_u_hranychnomu FROM predmety WHERE id = ?", (predmet_id,)).fetchone()
    vrahovuye_u_hranychnomu = bool(predmet_row["vrahovuyetsya_u_hranychnomu"]) if predmet_row else True
    for r in stari_parity:
        hodyny["z_fizkulturoyu"][r["parnist"]] -= r["godyn_tyzhden"]
        if vrahovuye_u_hranychnomu:
            hodyny["bez_fizkultury"][r["parnist"]] -= r["godyn_tyzhden"]

    klas = conn.execute("SELECT paralel, hranychne_navantazhennya FROM klasy WHERE id = ?", (klas_id,)).fetchone()
    norma = (klas["hranychne_navantazhennya"] if klas else None) or default_hranychne_navantazhennya(klas["paralel"] if klas else 1)

    parnyi_vlazyt = hodyny["bez_fizkultury"]["parnyi"] + 1 <= norma
    neparnyi_vlazyt = hodyny["bez_fizkultury"]["neparnyi"] + 1 <= norma

    if parnyi_vlazyt and not neparnyi_vlazyt:
        return "parnyi", False
    if neparnyi_vlazyt and not parnyi_vlazyt:
        return "neparnyi", False

    balans = _balans_parnosti(conn, klas_id, vyklyuchyty_predmet_id=predmet_id)
    obraniy = "neparnyi" if balans["parnyi"] > balans["neparnyi"] else "parnyi"
    return obraniy, not (parnyi_vlazyt or neparnyi_vlazyt)


def _predmety_vaha_json(all_predmety):
    """JSON-мапа predmet_id -> вага складності (_VAZHKIST_VAHA), для живого
    перерахунку підказки балансу парності в браузері (без перезавантаження
    сторінки) - див. форму навчального плану."""
    return json.dumps({p["id"]: _VAZHKIST_VAHA.get(p["vazhkist"], 1) for p in all_predmety})


def _kontekst_normy_parnosti(conn, klas_id, predmet_id, all_predmety, semestr=0):
    """Дані для живої підказки балансу парності у формі навчального плану: норма
    класу (граничне навантаження без фізкультури), фактичні години класу окремо
    по парному/непарному тижню БЕЗ цього предмета, і мапа предмет -> чи входить
    він у граничне навантаження.

    Підказка досі радила лише за «вагою складності» й не знала, що тиждень уже
    впирається в норму МОН. Та сама логіка вже працює в матриці вчителя
    (_obraty_parnist_dlya_ostatku) - тут вона просто стає видимою завучу.

    Предмет виключаємо повністю (усі три парності): години цього запису завуч
    зараз вводить у формі, і JS додає їх до базових сам."""
    if not klas_id:
        return {}
    klas = conn.execute(
        "SELECT paralel, hranychne_navantazhennya FROM klasy WHERE id = ?", (klas_id,)
    ).fetchone()
    if not klas:
        return {}
    norma = (klas["hranychne_navantazhennya"]
             or default_hranychne_navantazhennya(klas["paralel"]))
    hodyny = _hodyny_tyzhnya_klasu(
        conn, klas_id, vyklyuchyty_predmet_id=predmet_id, semestr=semestr or None
    )["bez_fizkultury"]
    hranychne = {}
    for pr in all_predmety:
        kl = pr.keys() if hasattr(pr, "keys") else pr
        hranychne[pr["id"]] = 1 if ("vrahovuyetsya_u_hranychnomu" not in kl
                                    or pr["vrahovuyetsya_u_hranychnomu"]) else 0
    return {
        "norma_hranychna": norma,
        "bazovi_parnyi": hodyny["parnyi"],
        "bazovi_neparnyi": hodyny["neparnyi"],
        "predmety_hranychne_json": json.dumps(hranychne),
    }


def _grupuvaty_plan_zapysy(rows):
    """Групує пласкі рядки navchalnyj_plan (у БД - до трьох рядків на клас+предмет,
    по одному на парність) в один логічний запис - щоб щотижнева/парна/непарна
    частини того самого предмета завжди були видні РАЗОМ на сторінці списку, а не
    губились серед інших рядків (це і є причина, чому раніше легко було забути
    завести другу 'ногу' дробового запису - вона виглядала як окремий, нічим не
    пов'язаний рядок)."""
    grupy = {}
    poryadok = []
    for z in rows:
        key = (z["klas_id"], z["predmet_id"], z["semestr"])
        if key not in grupy:
            grupy[key] = {
                "klas_id": z["klas_id"], "predmet_id": z["predmet_id"], "semestr": z["semestr"],
                "predmet_nazva": z["predmet_nazva"], "vchytel_pib": z["vchytel_pib"],
                "pidgrupa_2_vchytel_pib": z["pidgrupa_2_vchytel_pib"],
                "zdvoyeni_pary": z["zdvoyeni_pary"], "maks_urokiv_den": z["maks_urokiv_den"],
                "fiksovanyi_den": z["fiksovanyi_den"], "predmet_fiksovanyi_den": z["predmet_fiksovanyi_den"],
                "godyn_oboydva": 0, "godyn_parnyi": 0, "godyn_neparnyi": 0,
            }
            poryadok.append(key)
        grupy[key][f"godyn_{z['parnist']}"] = z["godyn_tyzhden"]
    result = []
    for key in poryadok:
        g = grupy[key]
        g["serednye_tyzhden"] = g["godyn_oboydva"] + (g["godyn_parnyi"] + g["godyn_neparnyi"]) / 2
        g["maye_parnist"] = g["godyn_parnyi"] > 0 or g["godyn_neparnyi"] > 0
        result.append(g)
    return result


def _read_plan_form(form):
    pidgrupa_2 = form.get("pidgrupa_2_vchytel_id") or None
    fiksovanyi_den = form.get("fiksovanyi_den") or None
    return {
        "klas_id": int(form["klas_id"]) if form.get("klas_id") else None,
        "predmet_id": int(form["predmet_id"]) if form.get("predmet_id") else None,
        "vchytel_id": int(form["vchytel_id"]) if form.get("vchytel_id") else None,
        "godyn_oboydva": int(form.get("godyn_oboydva") or 0),
        "godyn_parnyi": int(form.get("godyn_parnyi") or 0),
        "godyn_neparnyi": int(form.get("godyn_neparnyi") or 0),
        "zdvoyeni_pary": int(form.get("zdvoyeni_pary") or 0),
        "maks_urokiv_den": int(form.get("maks_urokiv_den") or 1),
        "pidgrupa_2_vchytel_id": int(pidgrupa_2) if pidgrupa_2 else None,
        "fiksovanyi_den": int(fiksovanyi_den) if fiksovanyi_den else None,
        "semestr": int(form.get("semestr") or 0),
    }


def _validate_plan(data, conn=None):
    errors = []
    if not data["klas_id"]:
        errors.append("Оберіть клас.")
    if not data["predmet_id"]:
        errors.append("Оберіть предмет.")
    if not data["vchytel_id"]:
        errors.append("Оберіть вчителя.")

    legy = (data["godyn_oboydva"], data["godyn_parnyi"], data["godyn_neparnyi"])
    if any(v < 0 for v in legy):
        errors.append("Годин на тиждень не може бути від'ємним.")
    if all(v <= 0 for v in legy):
        errors.append("Вкажіть годин на тиждень хоча б для одного варіанту (щотижня / лише парний / лише непарний).")
    if data["maks_urokiv_den"] <= 0:
        errors.append("Максимум уроків на день має бути більше нуля.")
    if data["fiksovanyi_den"] is not None and not (1 <= data["fiksovanyi_den"] <= 5):
        errors.append("Закріплений день тижня має бути від 1 (понеділок) до 5 (п'ятниця).")
    if data.get("semestr", 0) not in (0, 1, 2):
        errors.append("Невідоме значення «Коли читається».")

    # Предмет у класі - АБО "весь рік", АБО по семестрах, без змішування: інакше в
    # семестрі діяли б дві групи рядків одного предмета, і ліміти "на день" та
    # здвоєні пари рахувались би для кожної окремо. semestr_bulo - семестр рядків,
    # які ця форма саме зараз замінює (їх у перевірці не враховуємо).
    if conn and data["klas_id"] and data["predmet_id"] and data.get("semestr", 0) in (0, 1, 2):
        semestr = data.get("semestr", 0)
        inshi = {
            r["semestr"] for r in conn.execute(
                "SELECT DISTINCT semestr FROM navchalnyj_plan WHERE klas_id = ? AND predmet_id = ?",
                (data["klas_id"], data["predmet_id"]),
            )
        } - {data.get("semestr_bulo")}
        if semestr == 0 and inshi & {1, 2}:
            errors.append(
                "Цей предмет у класі вже розписано по семестрах — «весь рік» додати не можна. "
                "Відредагуйте семестрові записи або видаліть їх."
            )
        elif semestr in (1, 2) and 0 in inshi:
            errors.append(
                "Цей предмет у класі вже читається весь рік. Щоб розписати його по семестрах, "
                "змініть у наявному записі «Коли читається» на І семестр, а потім додайте ІІ."
            )
        elif semestr in inshi:
            errors.append(f"Для цього предмета в класі вже є запис «{SEMESTR_NAZVY[semestr]}» — відредагуйте його.")

    if not errors:
        navchalnykh_dniv = 5
        # фактичне навантаження ЦЬОГО предмета в конкретний тиждень - "щотижня" йде
        # завжди, "лише парний"/"лише непарний" додається тільки в свій тиждень
        tyzhden_parnyi = data["godyn_oboydva"] + data["godyn_parnyi"]
        tyzhden_neparnyi = data["godyn_oboydva"] + data["godyn_neparnyi"]
        naibilshyi = max(tyzhden_parnyi, tyzhden_neparnyi)
        if naibilshyi > data["maks_urokiv_den"] * navchalnykh_dniv:
            errors.append(
                f'Неможливо розмістити {naibilshyi} год у найнавантаженіший тиждень '
                f'при максимумі {data["maks_urokiv_den"]} на день (лише {navchalnykh_dniv} навчальних днів).'
            )
        nenulovi_tyzhni = [t for t in (tyzhden_parnyi, tyzhden_neparnyi) if t > 0]
        if nenulovi_tyzhni and data["zdvoyeni_pary"] * 2 > min(nenulovi_tyzhni):
            errors.append(
                "Здвоєних пар не може бути більше, ніж дозволяє кількість годин у "
                "найменш завантаженому з тижнів, коли предмет проводиться."
            )

        # Той самий вчитель на підгрупу 2 - НЕ помилка, а свідомий рідкісний випадок
        # (одна людина фізично не може вести дві підгрупи одночасно): генератор
        # тоді планує лише одну підгрупу як реальний урок, друга - інформаційний
        # допис "0 урок" під клітинкою розкладу (generator.py, розділ "Підгрупи").

        if conn and data["zdvoyeni_pary"] and data["klas_id"] and data["predmet_id"]:
            from generator import pary_dozvoleni
            klas_row = conn.execute("SELECT paralel FROM klasy WHERE id = ?", (data["klas_id"],)).fetchone()
            predmet_row = conn.execute("SELECT nazva FROM predmety WHERE id = ?", (data["predmet_id"],)).fetchone()
            if klas_row and predmet_row:
                if not pary_dozvoleni(conn, data["predmet_id"], klas_row["paralel"]):
                    errors.append(
                        f'Здвоєні уроки для "{predmet_row["nazva"]}" у {klas_row["paralel"]} класі '
                        f'заборонені Санітарним регламентом (генератор все одно проігнорує пари тут).'
                    )

    return errors


@app.route("/navchalnyj-plan/mizhpredmetni")
def mizhpredmetni_grupy_list():
    conn = get_connection()
    grupy = conn.execute(
        """
        SELECT mg.id, mg.nazva, k.paralel, k.litera,
               GROUP_CONCAT(p.nazva || ' (' || v.pib || ')', ', ') AS zapysy_opys
        FROM mizhpredmetni_grupy mg
        JOIN klasy k ON k.id = mg.klas_id
        LEFT JOIN navchalnyj_plan np ON np.mizhpredmetna_grupa_id = mg.id
        LEFT JOIN predmety p ON p.id = np.predmet_id
        LEFT JOIN vchyteli v ON v.id = np.vchytel_id
        GROUP BY mg.id
        ORDER BY k.paralel, k.litera COLLATE UKR
        """
    ).fetchall()
    conn.close()
    return render_template("mizhpredmetni_grupy_list.html", active="navchalnyj_plan", grupy=grupy)


@app.route("/navchalnyj-plan/mizhpredmetni/new", methods=["GET", "POST"])
def mizhpredmetni_grupy_new():
    conn = get_connection()

    if request.method == "POST":
        klas_id = int(request.form["klas_id"])
        nazva = request.form.get("nazva", "").strip() or "Міжпредметна група"
        zapysy_ids = request.form.getlist("zapysy_ids")

        errors = []
        if len(zapysy_ids) < 2:
            errors.append("Оберіть щонайменше 2 записи навчального плану для об'єднання.")

        if not errors:
            cur = conn.execute(
                "INSERT INTO mizhpredmetni_grupy (klas_id, nazva) VALUES (?, ?)",
                (klas_id, nazva),
            )
            grupa_id = cur.lastrowid
            for zid in zapysy_ids:
                conn.execute(
                    "UPDATE navchalnyj_plan SET mizhpredmetna_grupa_id = ? WHERE id = ?",
                    (grupa_id, int(zid)),
                )
            conn.commit()
            conn.close()
            return redirect(url_for("mizhpredmetni_grupy_list"))

        all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
        conn.close()
        return render_template(
            "mizhpredmetni_grupy_form.html", active="navchalnyj_plan",
            all_klasy=all_klasy, zapysy_klasu=[], obranyi_klas=klas_id, errors=errors,
        )

    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    obranyi_klas = request.args.get("klas_id", type=int)
    zapysy_klasu = []
    if obranyi_klas:
        zapysy_klasu = conn.execute(
            """
            SELECT np.id, p.nazva AS predmet_nazva, v.pib AS vchytel_pib, np.godyn_tyzhden, np.semestr
            FROM navchalnyj_plan np
            JOIN predmety p ON p.id = np.predmet_id
            JOIN vchyteli v ON v.id = np.vchytel_id
            WHERE np.klas_id = ? AND np.mizhpredmetna_grupa_id IS NULL
            ORDER BY p.nazva COLLATE UKR
            """,
            (obranyi_klas,),
        ).fetchall()
    conn.close()
    return render_template(
        "mizhpredmetni_grupy_form.html", active="navchalnyj_plan",
        all_klasy=all_klasy, zapysy_klasu=zapysy_klasu, obranyi_klas=obranyi_klas, errors=[],
    )


@app.route("/navchalnyj-plan/mizhpredmetni/<int:grupa_id>/delete", methods=["POST"])
def mizhpredmetni_grupy_delete(grupa_id):
    conn = get_connection()
    conn.execute("UPDATE navchalnyj_plan SET mizhpredmetna_grupa_id = NULL WHERE mizhpredmetna_grupa_id = ?", (grupa_id,))
    conn.execute("DELETE FROM mizhpredmetni_grupy WHERE id = ?", (grupa_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("mizhpredmetni_grupy_list"))


@app.route("/navchalnyj-plan")
def navchalnyj_plan():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT np.*, k.paralel, k.litera, p.nazva AS predmet_nazva, v.pib AS vchytel_pib,
               v2.pib AS pidgrupa_2_vchytel_pib, p.fiksovanyi_den AS predmet_fiksovanyi_den
        FROM navchalnyj_plan np
        JOIN klasy k ON k.id = np.klas_id
        JOIN predmety p ON p.id = np.predmet_id
        JOIN vchyteli v ON v.id = np.vchytel_id
        LEFT JOIN vchyteli v2 ON v2.id = np.pidgrupa_2_vchytel_id
        ORDER BY k.paralel, k.litera COLLATE UKR, p.nazva COLLATE UKR, np.semestr
        """
    ).fetchall()

    # навантаження по кожному класу: скільки РЕАЛЬНО РОЗПОДІЛЕНО в навчальному плані
    # (без фізкультури і з нею), звірене з двома нормами МОН, які вносяться ВРУЧНУ:
    # "Граничне" (без фізкультури) і "Фактичне" (з фізкультурою, те що МОН оплачує)
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    navantazhennya_po_klasu = []
    for k in all_klasy:
        parnist_ye = conn.execute(
            "SELECT DISTINCT parnist FROM navchalnyj_plan WHERE klas_id = ?", (k["id"],)
        ).fetchall()
        if not parnist_ye:
            continue
        maye_parnist = any(r["parnist"] != "oboydva" for r in parnist_ye)

        hodyny = _hodyny_tyzhnya_klasu(conn, k["id"])
        # "Розподілено" для порівняння з нормою - НАЙГІРШИЙ (пік) тиждень, бо норма
        # МОН - це стеля, яку не можна перевищити В ЖОДНОМУ тижні (на відміну від
        # Тарифікації, де важливе середнє за рік, не пік) - див. _potribno_fizychnykh_slotiv
        rozpodileno_z_fizkulturoyu = max(hodyny["z_fizkulturoyu"]["parnyi"], hodyny["z_fizkulturoyu"]["neparnyi"])
        rozpodileno_bez_fizkultury = max(hodyny["bez_fizkultury"]["parnyi"], hodyny["bez_fizkultury"]["neparnyi"])
        hranychna_norma = k["hranychne_navantazhennya"] or default_hranychne_navantazhennya(k["paralel"])
        faktychna_norma = k["faktychne_navantazhennya"] or default_faktychne_navantazhennya(k["paralel"])
        navantazhennya_po_klasu.append({
            "klas_id": k["id"],
            "paralel": k["paralel"],
            "nazva": klas_nazva(k["paralel"], k["litera"]),
            "maye_parnist": maye_parnist,
            "rozpodileno_bez_fizkultury": rozpodileno_bez_fizkultury,
            "rozpodileno_z_fizkulturoyu": rozpodileno_z_fizkulturoyu,
            # окремо по тижнях - показуються ЛИШЕ якщо в класі є дробові записи
            # (maye_parnist), інакше парний і непарний тиждень все одно однакові
            "parnyi_bez_fizkultury": hodyny["bez_fizkultury"]["parnyi"],
            "neparnyi_bez_fizkultury": hodyny["bez_fizkultury"]["neparnyi"],
            "parnyi_z_fizkulturoyu": hodyny["z_fizkulturoyu"]["parnyi"],
            "neparnyi_z_fizkulturoyu": hodyny["z_fizkulturoyu"]["neparnyi"],
            "hranychna_norma": hranychna_norma,
            "faktychna_norma": faktychna_norma,
            "hranychna_vruchnu": k["hranychne_navantazhennya"] is not None,
            "faktychna_vruchnu": k["faktychne_navantazhennya"] is not None,
            "perevyshcheno_hranychne": rozpodileno_bez_fizkultury > hranychna_norma,
            "perevyshcheno_faktychne": rozpodileno_z_fizkulturoyu > faktychna_norma,
            "perevyshcheno_hranychne_parnyi": hodyny["bez_fizkultury"]["parnyi"] > hranychna_norma,
            "perevyshcheno_hranychne_neparnyi": hodyny["bez_fizkultury"]["neparnyi"] > hranychna_norma,
            "perevyshcheno_faktychne_parnyi": hodyny["z_fizkulturoyu"]["parnyi"] > faktychna_norma,
            "perevyshcheno_faktychne_neparnyi": hodyny["z_fizkulturoyu"]["neparnyi"] > faktychna_norma,
        })

    # Таблиця навантаження розбивається на 3 стовпці за освітніми рівнями -
    # суцільний список на 30-40 класів був незручно довгий. Діапазони фіксовані
    # (не з tip_zakladu_options), бо "1-11 стара школа" - це ОДИН рівень 1-11, а
    # розбивка потрібна й там. 10-12: 12-й клас зʼявиться з реформою НУШ.
    navantazhennya_grupy = []
    for pidpys, p_vid, p_do in (("1-4 класи", 1, 4), ("5-9 класи", 5, 9),
                                ("10-12 класи", 10, 12)):
        riadky = [n for n in navantazhennya_po_klasu if p_vid <= n["paralel"] <= p_do]
        if riadky:
            navantazhennya_grupy.append({"pidpys": pidpys, "riadky": riadky})

    ye_parnist = any(z["parnist"] != "oboydva" for z in rows)
    pershyi_tyzhden_neparnyi = None
    if ye_parnist:
        settings = conn.execute("SELECT pershyi_tyzhden_neparnyi FROM school_settings WHERE id = 1").fetchone()
        pershyi_tyzhden_neparnyi = bool(settings["pershyi_tyzhden_neparnyi"]) if settings else True

    conn.close()

    # спершу групуємо парність-"ноги" (oboydva/parnyi/neparnyi) у один логічний
    # запис на клас+предмет, потім - записи по класу (вкладки на сторінці) -
    # порядок класів той самий, що вже задає ORDER BY у запиті вище (paralel,
    # litera, predmet.nazva), просто збираємо їх у послідовні групи
    klas_nazvy = {z["klas_id"]: klas_nazva(z["paralel"], z["litera"]) for z in rows}
    zapysy_po_klasu = []
    for g in _grupuvaty_plan_zapysy(rows):
        nazva = klas_nazvy[g["klas_id"]]
        if not zapysy_po_klasu or zapysy_po_klasu[-1]["nazva"] != nazva:
            zapysy_po_klasu.append({"nazva": nazva, "klas_id": g["klas_id"], "zapysy": []})
        zapysy_po_klasu[-1]["zapysy"].append(g)

    # активна вкладка - клас з адреси (?klas=<id>): після правки чи видалення
    # рядка повертаємось на той самий клас, а не на перший у списку
    aktyvnyi_klas = request.args.get("klas", type=int)
    if aktyvnyi_klas not in {g["klas_id"] for g in zapysy_po_klasu}:
        aktyvnyi_klas = zapysy_po_klasu[0]["klas_id"] if zapysy_po_klasu else None

    return render_template(
        "navchalnyj_plan_list.html", active="navchalnyj_plan",
        zapysy_po_klasu=zapysy_po_klasu, aktyvnyi_klas=aktyvnyi_klas,
        navantazhennya_po_klasu=navantazhennya_po_klasu,
        navantazhennya_grupy=navantazhennya_grupy,
        ye_parnist=ye_parnist, pershyi_tyzhden_neparnyi=pershyi_tyzhden_neparnyi,
    )


@app.route("/klasy/<int:klas_id>/norma", methods=["POST"])
def klasy_zberegty_normu(klas_id):
    """Збереження граничного й фактичного навантаження прямо з таблиці
    'Навантаження по класах' - без переходу в повну картку класу."""
    hranychne_raw = request.form.get("hranychne_navantazhennya", "").strip()
    faktychne_raw = request.form.get("faktychne_navantazhennya", "").strip()
    conn = get_connection()
    conn.execute(
        "UPDATE klasy SET hranychne_navantazhennya = ?, faktychne_navantazhennya = ? WHERE id = ?",
        (int(hranychne_raw) if hranychne_raw else None,
         int(faktychne_raw) if faktychne_raw else None, klas_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("navchalnyj_plan", klas=klas_id))


def _zberegty_grupu_planu(conn, data):
    """Перезаписує ВСІ ноги (oboydva/parnyi/neparnyi) запису класу+предмета одним
    upsert - видаляє старі рядки цієї пари й заводить нові лише для тих варіантів,
    де вказано > 0 годин. Простіше й надійніше за пошук-і-оновлення трьох окремих
    рядків окремо, а users завжди редагують усі три відразу на одній формі."""
    # замінюємо лише рядки ТОГО семестру, який редагується (semestr_bulo - якщо
    # завуч щойно змінив «Коли читається», старі рядки лежать під старим семестром)
    semestr_bulo = data.get("semestr_bulo")
    conn.execute(
        "DELETE FROM navchalnyj_plan WHERE klas_id = ? AND predmet_id = ? AND semestr = ?",
        (data["klas_id"], data["predmet_id"], data["semestr"] if semestr_bulo is None else semestr_bulo),
    )
    for parnist in ("oboydva", "parnyi", "neparnyi"):
        hodyny = data[f"godyn_{parnist}"]
        if hodyny > 0:
            conn.execute(
                """
                INSERT INTO navchalnyj_plan
                    (klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary,
                     maks_urokiv_den, pidgrupa_2_vchytel_id, fiksovanyi_den, parnist, semestr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["klas_id"], data["predmet_id"], data["vchytel_id"],
                    hodyny, data["zdvoyeni_pary"], data["maks_urokiv_den"],
                    data["pidgrupa_2_vchytel_id"], data["fiksovanyi_den"], parnist,
                    data["semestr"],
                ),
            )


@app.route("/navchalnyj-plan/new", methods=["GET", "POST"])
def navchalnyj_plan_new():
    conn = get_connection()
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    errors = []

    if request.method == "POST":
        data = _read_plan_form(request.form)
        zlyty_pidtverdzheno = request.form.get("zlyty_pidtverdzheno") == "1"
        tyzhni_semestriv = _tyzhni_semestriv(conn)

        existing_legy = []
        if data["klas_id"] and data["predmet_id"]:
            existing_legy = conn.execute(
                "SELECT parnist, godyn_tyzhden FROM navchalnyj_plan "
                "WHERE klas_id = ? AND predmet_id = ? AND semestr = ?",
                (data["klas_id"], data["predmet_id"], data["semestr"]),
            ).fetchall()
        if existing_legy:
            # записи цього ж семестру зараз будуть замінені об'єднаним - їх не
            # вважаємо "іншими" в перевірці змішування
            data["semestr_bulo"] = data["semestr"]

        if existing_legy and not zlyty_pidtverdzheno:
            # для цього класу й предмета вже є запис (можливо, з іншою парністю) -
            # замість "вже існує" помилки й глухого кута об'єднуємо те, що вже є,
            # з тим, що щойно ввели, і показуємо форму ще раз для підтвердження
            existing_map = {r["parnist"]: r["godyn_tyzhden"] for r in existing_legy}
            for parnist in ("oboydva", "parnyi", "neparnyi"):
                if data[f"godyn_{parnist}"] <= 0 and parnist in existing_map:
                    data[f"godyn_{parnist}"] = existing_map[parnist]
            balans = _balans_parnosti(conn, data["klas_id"], vyklyuchyty_predmet_id=data["predmet_id"],
                                      semestr=data["semestr"] or None)
            predmety_vaha_json = _predmety_vaha_json(all_predmety)
            normy = _kontekst_normy_parnosti(conn, data["klas_id"], data["predmet_id"], all_predmety,
                                             semestr=data["semestr"])
            conn.close()
            return render_template(
                "navchalnyj_plan_form.html", active="navchalnyj_plan", zapys=data,
                all_klasy=all_klasy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
                errors=["Для цього класу й предмета вже є запис — нижче об'єднано з тим, що вже було. "
                        "Перевірте години й натисніть \"Зберегти\" ще раз."],
                zlyty_pidtverdzheno=True, balans=balans, predmety_vaha_json=predmety_vaha_json,
                tyzhni_semestriv=tyzhni_semestriv, **normy,
            )

        errors = _validate_plan(data, conn)
        if not errors:
            _zberegty_grupu_planu(conn, data)
            conn.commit()
            conn.close()
            return redirect(url_for("navchalnyj_plan", klas=data["klas_id"]))

        balans = (_balans_parnosti(conn, data["klas_id"], vyklyuchyty_predmet_id=data["predmet_id"],
                                   semestr=data["semestr"] or None) if data["klas_id"] else {})
        predmety_vaha_json = _predmety_vaha_json(all_predmety)
        normy = _kontekst_normy_parnosti(conn, data["klas_id"], data["predmet_id"], all_predmety,
                                         semestr=data["semestr"])
        conn.close()
        return render_template(
            "navchalnyj_plan_form.html", active="navchalnyj_plan", zapys=data,
            all_klasy=all_klasy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
            errors=errors, zlyty_pidtverdzheno=zlyty_pidtverdzheno, balans=balans,
            predmety_vaha_json=predmety_vaha_json, tyzhni_semestriv=tyzhni_semestriv, **normy,
        )

    tyzhni_semestriv = _tyzhni_semestriv(conn)
    conn.close()
    if not all_klasy:
        errors.append("Спершу додайте хоча б один клас.")
    if not all_predmety:
        errors.append("Спершу додайте хоча б один предмет.")
    if not all_vchyteli:
        errors.append("Спершу додайте хоча б одного вчителя.")
    return render_template(
        "navchalnyj_plan_form.html", active="navchalnyj_plan", zapys=None,
        all_klasy=all_klasy, all_predmety=all_predmety, all_vchyteli=all_vchyteli, errors=errors,
        tyzhni_semestriv=tyzhni_semestriv,
    )


@app.route("/navchalnyj-plan/klas/<int:klas_id>/predmet/<int:predmet_id>/edit", methods=["GET", "POST"])
def navchalnyj_plan_group_edit(klas_id, predmet_id):
    conn = get_connection()
    # семестр групи, що редагується: у посиланні ?semestr=N (типово 0 - весь рік);
    # при POST - з прихованого поля, бо «Коли читається» у формі можна змінити
    semestr_bulo = request.form.get("semestr_bulo", type=int)
    if semestr_bulo is None:
        semestr_bulo = request.args.get("semestr", 0, type=int)
    existing_legy = conn.execute(
        "SELECT * FROM navchalnyj_plan WHERE klas_id = ? AND predmet_id = ? AND semestr = ?",
        (klas_id, predmet_id, semestr_bulo),
    ).fetchall()
    if not existing_legy:
        conn.close()
        abort(404)

    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    klas_row = conn.execute("SELECT * FROM klasy WHERE id = ?", (klas_id,)).fetchone()
    predmet_row = conn.execute("SELECT * FROM predmety WHERE id = ?", (predmet_id,)).fetchone()
    klas_nazva_str = klas_nazva(klas_row["paralel"], klas_row["litera"]) if klas_row else "?"
    predmet_nazva_str = predmet_row["nazva"] if predmet_row else "?"
    porada_podilu = _porada_podilu_pidgrup(conn, klas_row, predmet_row) if klas_row and predmet_row else None
    porada_movy = (
        _porada_riznomovni_inozemni(conn, klas_id)
        if predmet_row and predmet_row["normovanyi_kod"] in _INOZEMNI_MOVY_KODY else None
    )
    errors = []

    if request.method == "POST":
        data = _read_plan_form(request.form)
        # клас і предмет цього запису НЕ редагуються тут (лише через видалення й
        # заведення заново) - уникає конфлікту "переніс у групу, що вже існує"
        data["klas_id"] = klas_id
        data["predmet_id"] = predmet_id
        data["semestr_bulo"] = semestr_bulo
        errors = _validate_plan(data, conn)

        if not errors:
            _zberegty_grupu_planu(conn, data)
            conn.commit()
            conn.close()
            return redirect(url_for("navchalnyj_plan", klas=klas_id))

        balans = _balans_parnosti(conn, klas_id, vyklyuchyty_predmet_id=predmet_id, semestr=data["semestr"] or None)
        predmety_vaha_json = _predmety_vaha_json(all_predmety)
        normy = _kontekst_normy_parnosti(conn, klas_id, predmet_id, all_predmety, semestr=data["semestr"])
        tyzhni_semestriv = _tyzhni_semestriv(conn)
        conn.close()
        data["klas_id"] = klas_id
        data["predmet_id"] = predmet_id
        return render_template(
            "navchalnyj_plan_form.html", active="navchalnyj_plan", zapys=data,
            all_klasy=all_klasy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
            errors=errors, redaguvaty_grupu=True, balans=balans,
            klas_nazva_str=klas_nazva_str, predmet_nazva_str=predmet_nazva_str,
            predmety_vaha_json=predmety_vaha_json, porada_podilu=porada_podilu,
            porada_movy=porada_movy, semestr_bulo=semestr_bulo,
            tyzhni_semestriv=tyzhni_semestriv, **normy,
        )

    zapys = {
        "klas_id": klas_id, "predmet_id": predmet_id,
        "vchytel_id": existing_legy[0]["vchytel_id"],
        "zdvoyeni_pary": existing_legy[0]["zdvoyeni_pary"],
        "maks_urokiv_den": existing_legy[0]["maks_urokiv_den"],
        "pidgrupa_2_vchytel_id": existing_legy[0]["pidgrupa_2_vchytel_id"],
        "fiksovanyi_den": existing_legy[0]["fiksovanyi_den"],
        "semestr": semestr_bulo,
        "godyn_oboydva": 0, "godyn_parnyi": 0, "godyn_neparnyi": 0,
    }
    for r in existing_legy:
        zapys[f"godyn_{r['parnist']}"] = r["godyn_tyzhden"]
    balans = _balans_parnosti(conn, klas_id, vyklyuchyty_predmet_id=predmet_id, semestr=semestr_bulo or None)
    predmety_vaha_json = _predmety_vaha_json(all_predmety)
    normy = _kontekst_normy_parnosti(conn, klas_id, predmet_id, all_predmety, semestr=semestr_bulo)
    tyzhni_semestriv = _tyzhni_semestriv(conn)
    conn.close()
    return render_template(
        "navchalnyj_plan_form.html", active="navchalnyj_plan", zapys=zapys,
        all_klasy=all_klasy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
        errors=[], redaguvaty_grupu=True, balans=balans,
        klas_nazva_str=klas_nazva_str, predmet_nazva_str=predmet_nazva_str,
        predmety_vaha_json=predmety_vaha_json, porada_podilu=porada_podilu,
        porada_movy=porada_movy, semestr_bulo=semestr_bulo,
        tyzhni_semestriv=tyzhni_semestriv, **normy,
    )


# ---------- Копія навчального плану з одного класу на інший ----------
# Паралельні класи майже завжди мають однаковий план - вводити 15-20 рядків
# руками на кожен клас паралелі безглуздо. Копіюються предмети, години (усі
# парності), здвоєні, макс/день, закріплений день, вчителі (основний і 2-ї
# підгрупи) - завуч потім править лише вчителів, які різняться. Міжпредметні
# групи НЕ копіюються (група - річ конкретного класу).

def _kopiyuvaty_plan_klasu(conn, z_klas_id, u_klas_id, zaminyty=False):
    """Повертає (скопійовано рядків, помилка або None). Якщо в класі-цілі вже є
    план і zaminyty=False - нічого не робить і повертає помилку."""
    if z_klas_id == u_klas_id:
        return 0, "Джерело і ціль - той самий клас."
    ye_u_tsili = conn.execute(
        "SELECT COUNT(*) c FROM navchalnyj_plan WHERE klas_id = ?", (u_klas_id,)).fetchone()["c"]
    if ye_u_tsili and not zaminyty:
        return 0, (f"У класі-цілі вже є {ye_u_tsili} рядків плану. Позначте «замінити наявний план», "
                   "якщо їх справді треба стерти й записати копію.")
    dzherelo = conn.execute(
        "SELECT * FROM navchalnyj_plan WHERE klas_id = ? ORDER BY id", (z_klas_id,)).fetchall()
    if not dzherelo:
        return 0, "У класі-джерелі план порожній - нема що копіювати."
    if ye_u_tsili:
        conn.execute("DELETE FROM navchalnyj_plan WHERE klas_id = ?", (u_klas_id,))
    for r in dzherelo:
        conn.execute(
            """
            INSERT INTO navchalnyj_plan
                (klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary,
                 maks_urokiv_den, pidgrupa_2_vchytel_id, fiksovanyi_den, parnist, semestr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (u_klas_id, r["predmet_id"], r["vchytel_id"], r["godyn_tyzhden"], r["zdvoyeni_pary"],
             r["maks_urokiv_den"], r["pidgrupa_2_vchytel_id"], r["fiksovanyi_den"], r["parnist"],
             r["semestr"]),
        )
    return len(dzherelo), None


@app.route("/navchalnyj-plan/kopiyuvaty", methods=["GET", "POST"])
def navchalnyj_plan_kopiyuvaty():
    conn = get_connection()
    klasy = conn.execute(
        """
        SELECT k.id, k.paralel, k.litera,
               (SELECT COUNT(*) FROM navchalnyj_plan np WHERE np.klas_id = k.id) AS riadkiv
        FROM klasy k ORDER BY k.paralel, k.litera COLLATE UKR
        """
    ).fetchall()
    errors = []
    z_klas = request.form.get("z_klas_id", type=int) or request.args.get("z_klas_id", type=int)
    u_klas = request.form.get("u_klas_id", type=int)

    if request.method == "POST":
        zaminyty = request.form.get("zaminyty") == "on"
        if not z_klas or not u_klas:
            errors.append("Оберіть і клас-джерело, і клас-ціль.")
        else:
            n, pomylka = _kopiyuvaty_plan_klasu(conn, z_klas, u_klas, zaminyty)
            if pomylka:
                errors.append(pomylka)
            else:
                conn.commit()
                conn.close()
                nazvy = {k["id"]: klas_nazva(k["paralel"], k["litera"]) for k in klasy}
                flash(f"План класу {nazvy[z_klas]} скопійовано у {nazvy[u_klas]}: {n} рядків. "
                      "Перевірте вчителів - вони скопійовані як у джерелі.", "success")
                return redirect(url_for("navchalnyj_plan", klas=u_klas))
    conn.close()
    return render_template(
        "navchalnyj_plan_kopiya.html", active="navchalnyj_plan",
        klasy=klasy, z_klas=z_klas, u_klas=u_klas, errors=errors,
    )


@app.route("/navchalnyj-plan/klas/<int:klas_id>/predmet/<int:predmet_id>/delete", methods=["POST"])
def navchalnyj_plan_group_delete(klas_id, predmet_id):
    semestr = request.args.get("semestr", 0, type=int)
    conn = get_connection()
    conn.execute("DELETE FROM navchalnyj_plan WHERE klas_id = ? AND predmet_id = ? AND semestr = ?",
                 (klas_id, predmet_id, semestr))
    conn.commit()
    conn.close()
    return redirect(url_for("navchalnyj_plan", klas=klas_id))




@app.route("/rozklad")
def rozklad():
    conn = get_connection()
    has_data = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()

    klas_id = request.args.get("klas_id", type=int)
    vchytel_id = request.args.get("vchytel_id", type=int)
    ye_parnist = _ye_parnisni_zapysy(conn)
    obraniy_parnist = request.args.get("parnist")
    if obraniy_parnist not in ("parnyi", "neparnyi"):
        obraniy_parnist = _potochna_parnist(conn)

    dniv, urokiv = _rozklad_grid(conn, 5, 8)
    tytul = rezhym = None
    cell_map = {}

    if klas_id:
        klas = conn.execute("SELECT * FROM klasy WHERE id = ?", (klas_id,)).fetchone()
        if klas is not None:
            rows = conn.execute(
                """
                SELECT gu.den, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
                       COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
                       COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
                       p.vazhkist,
                       v.pib AS vchytel_pib, k.nazva AS kabinet_nazva,
                       CASE WHEN gu.predmet_id IS NULL THEN 1 ELSE 0 END AS ye_pozaurochne
                FROM generated_urok gu
                LEFT JOIN predmety p ON p.id = gu.predmet_id
                LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
                JOIN vchyteli v ON v.id = gu.vchytel_id
                LEFT JOIN kabinety k ON k.id = gu.kabinet_id
                WHERE gu.klas_id = ? AND gu.parnist IN ('oboydva', ?)
                """,
                (klas_id, obraniy_parnist),
            ).fetchall()
            asystenty_klasu = _asystenty_klasu_mapa(conn, klas_id)
            for r in rows:
                row_dict = _dodaty_asystentiv_do_uroku(dict(r), asystenty_klasu)
                cell_map.setdefault((r["den"], r["urok"]), []).append(row_dict)
            tytul = f"Клас {klas_nazva(klas['paralel'], klas['litera'])}"
            rezhym = "klas"
    elif vchytel_id:
        vchytel = conn.execute("SELECT * FROM vchyteli WHERE id = ?", (vchytel_id,)).fetchone()
        if vchytel is not None:
            rows = conn.execute(
                """
                SELECT gu.den, gu.urok, gu.predmet_id, gu.parnist, gu.pidgrupa_2_vchytel_id,
                       COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
                       COALESCE(p.skorochennya, pz.nazva) AS predmet_skor,
                       p.vazhkist,
                       k.paralel, k.litera, kab.nazva AS kabinet_nazva
                FROM generated_urok gu
                LEFT JOIN predmety p ON p.id = gu.predmet_id
                LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
                JOIN klasy k ON k.id = gu.klas_id
                LEFT JOIN kabinety kab ON kab.id = gu.kabinet_id
                WHERE gu.vchytel_id = ? AND gu.parnist IN ('oboydva', ?)
                """,
                (vchytel_id, obraniy_parnist),
            ).fetchall()
            for r in rows:
                cell_map.setdefault((r["den"], r["urok"]), []).append(r)
            tytul = vchytel["pib"]
            rezhym = "vchytel"

    conn.close()
    return render_template(
        "rozklad.html", active="rozklad", has_data=has_data,
        all_klasy=all_klasy, all_vchyteli=all_vchyteli,
        klas_id=klas_id, vchytel_id=vchytel_id,
        tytul=tytul, rezhym=rezhym, cell_map=cell_map,
        dniv=dniv, urokiv=urokiv, obraniy_parnist=obraniy_parnist, ye_parnist=ye_parnist,
    )


def _rozklad_grid(conn, dniv, urokiv):
    settings = conn.execute("SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1").fetchone()
    return (
        settings["dniv_na_tyzhden"] if settings else dniv,
        settings["urokiv_na_den"] if settings else urokiv,
    )


def _asystenty_klasu_mapa(conn, klas_id):
    """Повертає список (pib, suprovid, set_predmet_id_yakscho_okremi) для класу -
    щоб визначити, який асистент супроводжує який урок."""
    asystenty = conn.execute(
        "SELECT id, pib, suprovid FROM asystenty WHERE klas_id = ?", (klas_id,)
    ).fetchall()
    result = []
    for a in asystenty:
        predmety_ids = set()
        if a["suprovid"] == "okremi":
            predmety_ids = {
                r["predmet_id"] for r in conn.execute(
                    "SELECT predmet_id FROM asystenty_predmety WHERE asystent_id = ?", (a["id"],)
                ).fetchall()
            }
        result.append({"pib": a["pib"], "suprovid": a["suprovid"], "predmety_ids": predmety_ids})
    return result


def _dodaty_asystentiv_do_uroku(row_dict, asystenty_klasu):
    """Додає до запису уроку список імен асистентів, що його супроводжують."""
    imena = []
    for a in asystenty_klasu:
        if a["suprovid"] == "vsi" or row_dict["predmet_id"] in a["predmety_ids"]:
            imena.append(a["pib"])
    row_dict["asystenty"] = imena
    return row_dict


@app.route("/rozklad/klas/<int:klas_id>")
def rozklad_klas(klas_id):
    # стара адреса окремої сторінки - лишена для збережених посилань, тепер усе
    # на одній сторінці /rozklad (фільтр + розклад одразу під ним, без переходу)
    return redirect(url_for("rozklad", klas_id=klas_id))


@app.route("/rozklad/vchytel/<int:vchytel_id>")
def rozklad_vchytel(vchytel_id):
    return redirect(url_for("rozklad", vchytel_id=vchytel_id))


def _potribno_fizychnykh_slotiv(conn, umova, params, from_extra="", semestr=None):
    """Обгортка над _potribno_fizychnykh_slotiv_semestru: для конкретного семестру -
    рядки "весь рік" + цей семестр; без семестру - більший із двох (ємність має
    вистачити в кожному семестрі, а сума двох семестрів була б хибним подвоєнням)."""
    if semestr:
        return _potribno_fizychnykh_slotiv_semestru(conn, umova, params, from_extra, semestr)
    return max(_potribno_fizychnykh_slotiv_semestru(conn, umova, params, from_extra, s) for s in (1, 2))


def _potribno_fizychnykh_slotiv_semestru(conn, umova, params, from_extra, semestr):
    """Скільки РЕАЛЬНИХ фізичних слотів розкладу (день+урок) потрібно для записів
    навчального плану, що задовольняють `umova` (WHERE-умова над таблицею np =
    navchalnyj_plan, опційно з `from_extra` - додаткові JOIN). Просте SUM(godyn_tyzhden)
    би подвоювало потребу для дробових (parnyi/neparnyi) предметів - вони фізично
    можуть ділити один і той самий слот із записом протилежної парності (як і в
    самому генераторі, див. _entry_tyzhni у generator.py), тому потрібно
    oboydva-годин + максимум(parnyi-годин, neparnyi-годин), а не пряма сума."""
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN np.parnist = 'oboydva' THEN np.godyn_tyzhden ELSE 0 END) AS oboydva,
            SUM(CASE WHEN np.parnist = 'parnyi' THEN np.godyn_tyzhden ELSE 0 END) AS parnyi,
            SUM(CASE WHEN np.parnist = 'neparnyi' THEN np.godyn_tyzhden ELSE 0 END) AS neparnyi
        FROM navchalnyj_plan np {from_extra} WHERE ({umova}) AND np.semestr IN (0, ?)
        """,
        tuple(params) + (semestr,),
    ).fetchone()
    return (row["oboydva"] or 0) + max(row["parnyi"] or 0, row["neparnyi"] or 0)


def _potribno_slotiv_komplektu(conn, klas_ids, umova_predmeta, semestr=None):
    """Як _potribno_fizychnykh_slotiv - для семестру або пік із двох семестрів."""
    if semestr:
        return _potribno_slotiv_komplektu_semestru(conn, klas_ids, umova_predmeta, semestr)
    return max(_potribno_slotiv_komplektu_semestru(conn, klas_ids, umova_predmeta, s) for s in (1, 2))


def _potribno_slotiv_komplektu_semestru(conn, klas_ids, umova_predmeta, semestr):
    """Скільки фізичних слотів домашнього кабінету потрібно КЛАС-КОМПЛЕКТУ (кілька
    класів у одному приміщенні з одним учителем). Дзеркалить правило злиття
    генератора (generator._obyednaty_klas_komplekty): уроки членів з однаковим
    (предмет, вчитель) ідуть у СПІЛЬНИЙ слот - спільна частина = мінімум годин
    серед членів, а залишок кожного класу понад мінімум - його окремі уроки.
    Предмет, який веде лише один член комплекту, не зливається ні з чим і
    рахується як звичайний. Парність - як у _potribno_fizychnykh_slotiv:
    oboydva + max(parnyi, neparnyi)."""
    placeholders = ",".join("?" for _ in klas_ids)
    rows = conn.execute(
        f"""
        SELECT np.klas_id, np.predmet_id, np.vchytel_id, np.parnist, np.godyn_tyzhden
        FROM navchalnyj_plan np
        WHERE np.klas_id IN ({placeholders}) AND ({umova_predmeta}) AND np.semestr IN (0, ?)
        """,
        tuple(klas_ids) + (semestr,),
    ).fetchall()
    # (предмет, вчитель, парність) -> {klas_id: годин}
    grupy = {}
    for r in rows:
        klyuch = (r["predmet_id"], r["vchytel_id"], r["parnist"])
        grupy.setdefault(klyuch, {})[r["klas_id"]] = r["godyn_tyzhden"]
    po_parnosti = {"oboydva": 0, "parnyi": 0, "neparnyi": 0}
    for (_, _, parnist), po_klasu in grupy.items():
        hodyny = list(po_klasu.values())
        if len(hodyny) >= 2:
            spilne = min(hodyny)
            sloty = spilne + sum(h - spilne for h in hodyny)
        else:
            sloty = hodyny[0]
        po_parnosti[parnist] = po_parnosti.get(parnist, 0) + sloty
    return po_parnosti["oboydva"] + max(po_parnosti["parnyi"], po_parnosti["neparnyi"])


def _perevirka_navantazhennya(semestr=None):
    conn = get_connection()
    settings = conn.execute("SELECT dniv_na_tyzhden, urokiv_na_den FROM school_settings WHERE id = 1").fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    urokiv = settings["urokiv_na_den"] if settings else 8

    problemy = []

    # ---- вчителі ----
    vchyteli = conn.execute("SELECT * FROM vchyteli").fetchall()
    for v in vchyteli:
        potribno = _potribno_fizychnykh_slotiv(conn, "np.vchytel_id = ?", (v["id"],), semestr=semestr)
        if potribno == 0:
            continue

        maks_den = min(v["maksymum_urokiv_den"] or urokiv, urokiv)

        nedostupni_sloty = {
            (r["den"], r["urok"]) for r in conn.execute(
                "SELECT DISTINCT den, urok FROM teacher_availability "
                "WHERE vchytel_id = ? AND status = 'nedostupnyi' AND urok <= ?",
                (v["id"], urokiv),
            ).fetchall()
        }
        # ємність рахуємо ПО ДНЯХ окремо, а не одним відніманням від "днів × ліміт" -
        # інакше вчитель, недоступний майже весь тиждень (крім кількох слотів, куди
        # якраз вкладається потрібне навантаження), хибно показував ємність 0: денний
        # ліміт і так відсікає зайві "недоступні" слоти того дня понад нього, їх не
        # можна віднімати ще раз при простому підсумовуванні за весь тиждень.
        yomkist_realna = 0
        for d in range(1, dniv + 1):
            nedostupno_v_den = sum(1 for l in range(1, urokiv + 1) if (d, l) in nedostupni_sloty)
            dostupno_v_den = urokiv - nedostupno_v_den
            yomkist_realna += max(0, min(maks_den, dostupno_v_den))

        if potribno > yomkist_realna:
            problemy.append({
                "typ": "Вчитель", "nazva": v["pib"],
                "potribno": potribno, "yomkist": yomkist_realna,
            })

    # ---- класи ----
    klasy = conn.execute("SELECT * FROM klasy").fetchall()
    for k in klasy:
        potribno = _potribno_fizychnykh_slotiv(conn, "np.klas_id = ?", (k["id"],), semestr=semestr)
        if potribno == 0:
            continue

        yomkist = dniv * urokiv
        nedostupno_slotiv = len({
            (r["den"], r["urok"]) for r in conn.execute(
                "SELECT DISTINCT den, urok FROM class_availability WHERE klas_id = ? AND urok <= ?",
                (k["id"], urokiv),
            ).fetchall()
        })
        yomkist_realna = max(0, yomkist - nedostupno_slotiv)

        if potribno > yomkist_realna:
            problemy.append({
                "typ": "Клас", "nazva": klas_nazva(k["paralel"], k["litera"]),
                "potribno": potribno, "yomkist": yomkist_realna,
            })

    # ---- кабінети: домашні (кілька класів можуть ділити один кабінет) ----
    zagalna_yomkist_kabinetu = dniv * urokiv
    domashni_kabinety = conn.execute(
        "SELECT DISTINCT domashniy_kabinet_id FROM klasy WHERE domashniy_kabinet_id IS NOT NULL"
    ).fetchall()
    for row in domashni_kabinety:
        kabinet_id = row["domashniy_kabinet_id"]
        kabinet = conn.execute("SELECT nazva FROM kabinety WHERE id = ?", (kabinet_id,)).fetchone()
        if not kabinet:
            continue

        klasy_v_kabineti = conn.execute(
            "SELECT id, komplekt_id FROM klasy WHERE domashniy_kabinet_id = ?", (kabinet_id,)
        ).fetchall()

        # рахуємо лише уроки БЕЗ спецкабінету - вони й "сидять" у домашньому кабінеті
        # (парність рахуємо ОКРЕМО в межах кожного класу - слоти різних класів
        # не можна ділити між собою, лише parnyi/neparnyi одного класу).
        # "Потребує спецкабінету" визначаємо за РЕАЛЬНОЮ прив'язкою (kabinety_predmety -
        # хто справді дозволений у спецкабінеті), а не за окремим текстовим полем на
        # предметі - воно ніколи не використовувалось генератором і лише дублювало
        # цю ж інформацію (див. видалене predmety.speckabinet_typ).
        # ВИНЯТОК: предмет зі spets_kabinet_lyshe_pidgrupa1 (мови) - його уроки
        # переважно сидять у домашньому кабінеті (нерозділені й 2-га підгрупа),
        # лише 1-ша підгрупа тягнеться у спецкабінет - тож рахуємо його ТУТ, як
        # звичайний домашній предмет (консервативно: як усі години, найгірший випадок).
        bez_spets = (
            "np.predmet_id NOT IN ("
            "  SELECT DISTINCT kp.predmet_id FROM kabinety_predmety kp"
            "  JOIN predmety p2 ON p2.id = kp.predmet_id"
            "  WHERE COALESCE(p2.spets_kabinet_lyshe_pidgrupa1, 0) = 0)"
        )

        # Члени одного КЛАС-КОМПЛЕКТУ сидять у спільному домашньому кабінеті
        # ОДНОЧАСНО - генератор зливає їхні однакові уроки в один слот. Проста
        # сума по класах рахувала б кожен спільний урок двічі-тричі й видавала
        # хибне "перевищено ємність" (реальний випадок: 3-4-В, 42 замість 21).
        potribno = 0
        po_komplektu = {}
        for k in klasy_v_kabineti:
            if k["komplekt_id"] is None:
                potribno += _potribno_fizychnykh_slotiv(
                    conn, "np.klas_id = ? AND " + bez_spets, (k["id"],), semestr=semestr,
                )
            else:
                po_komplektu.setdefault(k["komplekt_id"], []).append(k["id"])
        for chleny in po_komplektu.values():
            potribno += _potribno_slotiv_komplektu(conn, chleny, bez_spets, semestr=semestr)

        nedostupno_slotiv = len({
            (r["den"], r["urok"]) for r in conn.execute(
                "SELECT DISTINCT den, urok FROM kabinet_availability WHERE kabinet_id = ? AND urok <= ?",
                (kabinet_id, urokiv),
            ).fetchall()
        })
        yomkist_realna = max(0, zagalna_yomkist_kabinetu - nedostupno_slotiv)

        if potribno > yomkist_realna:
            klasy_nazvy = ", ".join(
                klas_nazva(kk["paralel"], kk["litera"])
                for kk in conn.execute(
                    "SELECT paralel, litera FROM klasy WHERE domashniy_kabinet_id = ?", (kabinet_id,)
                ).fetchall()
            )
            problemy.append({
                "typ": "Кабінет (домашній)", "nazva": f"{kabinet['nazva']} — класи: {klasy_nazvy}",
                "potribno": potribno, "yomkist": yomkist_realna,
            })

    # ---- кабінети: спецкабінети (пул кабінетів на предмет - всі класи школи змагаються за них).
    # "Потребує спецкабінету" = предмет реально прив'язаний до якогось кабінету через
    # kabinety_predmety - те саме джерело правди, яким керується сама генерація. ----
    predmety_zi_speckabinetom = conn.execute(
        "SELECT DISTINCT p.id, p.nazva, p.spets_kabinet_lyshe_pidgrupa1 FROM predmety p "
        "JOIN kabinety_predmety kp ON kp.predmet_id = p.id"
    ).fetchall()
    for p in predmety_zi_speckabinetom:
        # для предметів зі spets_kabinet_lyshe_pidgrupa1 (мови) спецкабінет НЕ
        # обов'язковий: нерозділений урок і 2-га підгрупа поділу йдуть у домашній
        # клас / вільний універсальний, лише 1-ша підгрупа тягнеться у спецкабінет
        # (м'яка перевага, із запасним домашнім кабінетом). Тож перевірка "усі
        # години предмета мусять влізти в пул спецкабінетів" тут не застосовна.
        if p["spets_kabinet_lyshe_pidgrupa1"]:
            continue
        potribno = _potribno_fizychnykh_slotiv(conn, "np.predmet_id = ?", (p["id"],), semestr=semestr)
        if potribno == 0:
            continue

        kabinety_dlya_predmeta = conn.execute(
            """
            SELECT k.id, k.nazva FROM kabinety k
            JOIN kabinety_predmety kp ON kp.kabinet_id = k.id
            WHERE kp.predmet_id = ?
            """,
            (p["id"],),
        ).fetchall()

        if not kabinety_dlya_predmeta:
            problemy.append({
                "typ": "Предмет без кабінету", "nazva": p["nazva"],
                "potribno": potribno, "yomkist": 0,
            })
            continue

        yomkist = 0
        for kab in kabinety_dlya_predmeta:
            nedostupno_slotiv = len({
                (r["den"], r["urok"]) for r in conn.execute(
                    "SELECT DISTINCT den, urok FROM kabinet_availability WHERE kabinet_id = ? AND urok <= ?",
                    (kab["id"], urokiv),
                ).fetchall()
            })
            yomkist += max(0, zagalna_yomkist_kabinetu - nedostupno_slotiv)

        if potribno > yomkist:
            kabinety_nazvy = ", ".join(kk["nazva"] for kk in kabinety_dlya_predmeta)
            problemy.append({
                "typ": "Спецкабінет", "nazva": f"{p['nazva']} (кабінети: {kabinety_nazvy})",
                "potribno": potribno, "yomkist": yomkist,
            })

    conn.close()
    return problemy


def _perevirka_kabinetiv_klasiv():
    """Попередження (не блокери) про домашні кабінети класів, що є в навчальному
    плані: (а) клас без домашнього кабінету - генератор мовчки ставить його уроки
    "без кабінету"; (б) кілька класів з ОДНИМ домашнім кабінетом, які не є одним
    клас-комплектом, - вони не можуть бути там одночасно, уроки конкурують за
    приміщення й частина лишається нерозміщеною без видимої причини."""
    conn = get_connection()
    klasy = conn.execute(
        """
        SELECT k.id, k.paralel, k.litera, k.domashniy_kabinet_id, k.komplekt_id, kab.nazva AS kabinet_nazva
        FROM klasy k
        LEFT JOIN kabinety kab ON kab.id = k.domashniy_kabinet_id
        WHERE k.id IN (SELECT DISTINCT klas_id FROM navchalnyj_plan)
        ORDER BY k.paralel, k.litera COLLATE UKR
        """
    ).fetchall()
    conn.close()
    bez_kabinetu = [klas_nazva(k["paralel"], k["litera"]) for k in klasy if not k["domashniy_kabinet_id"]]
    po_kabinetu = {}
    for k in klasy:
        if k["domashniy_kabinet_id"]:
            po_kabinetu.setdefault(k["domashniy_kabinet_id"], []).append(k)
    spilni = []
    for kid, grupa in po_kabinetu.items():
        if len(grupa) < 2:
            continue
        komplekty = {k["komplekt_id"] for k in grupa}
        if len(komplekty) == 1 and None not in komplekty:
            continue   # усі - один клас-комплект, так і має бути
        spilni.append({
            "kabinet": grupa[0]["kabinet_nazva"],
            "klasy": ", ".join(klas_nazva(k["paralel"], k["litera"]) for k in grupa),
        })
    return {"bez_kabinetu": bez_kabinetu, "spilni": spilni}


def _perevirka_obovyazkovogo_podilu(semestr=None):
    """Класи+предмети, де за нормативом МОН обов'язковий поділ на підгрупи ("Авто",
    норма перевищена/спрацювала), але вчитель підгрупи 2 ще не призначений у жодному
    з рядків групи (парний/непарний/щотижня) - блокує генерацію, поки не виправлено.
    Групи рахуються окремо по семестрах (v0.2.0); для semestr - лише ті, що діють
    у ньому, без semestr - усі."""
    conn = get_connection()
    umova_sem = "AND np.semestr IN (0, ?)" if semestr else ""
    grupy = conn.execute(
        f"""
        SELECT np.klas_id, np.predmet_id, np.semestr, MAX(np.pidgrupa_2_vchytel_id) AS pidgrupa_2_max
        FROM navchalnyj_plan np
        JOIN predmety p ON p.id = np.predmet_id
        WHERE p.podil_pravylo = 'avto' {umova_sem}
        GROUP BY np.klas_id, np.predmet_id, np.semestr
        HAVING pidgrupa_2_max IS NULL
        """,
        (semestr,) if semestr else (),
    ).fetchall()
    problemy = []
    for g in grupy:
        klas = conn.execute("SELECT * FROM klasy WHERE id = ?", (g["klas_id"],)).fetchone()
        predmet = conn.execute("SELECT * FROM predmety WHERE id = ?", (g["predmet_id"],)).fetchone()
        if not klas or not predmet:
            continue
        porada = _porada_podilu_pidgrup(conn, klas, predmet)
        if porada:
            problemy.append({
                "klas": klas_nazva(klas["paralel"], klas["litera"]),
                "predmet": predmet["nazva"] + (f" ({SEMESTR_KOROTKO[g['semestr']]})" if g["semestr"] else ""),
                "prychyna": porada,
                "klas_id": klas["id"],
                "predmet_id": predmet["id"],
                "semestr": g["semestr"],
                "mizhpredmetni": False,
            })

    # п.4 наказу: різні іноземні мови в одному класі - окремий тип (через
    # "Міжпредметні групи", не через pidgrupa_2), тому окрема перевірка
    klasy_z_movamy = conn.execute(
        f"""
        SELECT DISTINCT np.klas_id
        FROM navchalnyj_plan np
        JOIN predmety p ON p.id = np.predmet_id
        WHERE p.normovanyi_kod IN ({",".join("?" * len(_INOZEMNI_MOVY_KODY))})
              AND p.podil_pravylo = 'avto'
        """,
        _INOZEMNI_MOVY_KODY,
    ).fetchall()
    for row in klasy_z_movamy:
        klas = conn.execute("SELECT * FROM klasy WHERE id = ?", (row["klas_id"],)).fetchone()
        if not klas:
            continue
        porada = _porada_riznomovni_inozemni(conn, klas["id"])
        if porada:
            problemy.append({
                "klas": klas_nazva(klas["paralel"], klas["litera"]),
                "predmet": "Різні іноземні мови",
                "prychyna": porada,
                "klas_id": klas["id"],
                "predmet_id": None,
                "mizhpredmetni": True,
            })

    conn.close()
    return problemy


DAYS_LABELS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _znajty_kandydativ(conn, den, urok, predmet_id, zainyati_zavzhdy, max_kandydativ=6, pozaurochne_zanyattya_id=None,
                       tbl="generated_urok"):
    """Повертає до max_kandydativ варіантів (предмет/заняття + вчитель) для заміни
    на конкретний урок: спершу ті, хто веде ТОЙ САМИЙ предмет/заняття (ідеальна
    заміна), потім вільні вчителі з ІНШИМИ предметами. Кожен варіант - реальна
    пара (те, що справді знає цей вчитель) + (вчитель), а не будь-хто на будь-що.
    Підтримує і навчальні (predmet_id), і позаурочні (pozaurochne_zanyattya_id) -
    останні раніше взагалі не мали кандидатів (пошук був лише по navchalnyj_plan)."""
    zainyati_tut = set(zainyati_zavzhdy)
    for r in conn.execute(
        f"SELECT vchytel_id FROM {tbl} WHERE den = ? AND urok = ?", (den, urok)
    ).fetchall():
        zainyati_tut.add(r["vchytel_id"])
    # "Недоступний" у сітці доступності вчителя (працює в іншій школі, методичний
    # день) - теж не кандидат: раніше підбір дивився лише на зайнятість за
    # розкладом і міг запропонувати того, кого в цей час фізично немає в школі
    for r in conn.execute(
        "SELECT vchytel_id FROM teacher_availability "
        "WHERE den = ? AND urok = ? AND status = 'nedostupnyi'", (den, urok)
    ).fetchall():
        zainyati_tut.add(r["vchytel_id"])

    pib_mapa = {r["id"]: r["pib"] for r in conn.execute("SELECT id, pib FROM vchyteli").fetchall()}
    predmet_mapa = {r["id"]: r["nazva"] for r in conn.execute("SELECT id, nazva FROM predmety").fetchall()}
    zanyattya_mapa = {r["id"]: r["nazva"] for r in conn.execute("SELECT id, nazva FROM pozaurochni_zanyattya").fetchall()}

    navantazhennya = {}

    def _navantazhennya(vid):
        if vid not in navantazhennya:
            navantazhennya[vid] = conn.execute(
                f"SELECT COUNT(*) c FROM {tbl} WHERE vchytel_id = ? AND den = ?", (vid, den)
            ).fetchone()["c"]
        return navantazhennya[vid]

    toy_samyi = []
    inshi = []

    # усі пари (вчитель, предмет) з навчального плану
    vsi_pary_navch = conn.execute("SELECT DISTINCT vchytel_id, predmet_id FROM navchalnyj_plan").fetchall()
    for r in vsi_pary_navch:
        vid = r["vchytel_id"]
        if vid in zainyati_tut:
            continue
        variant = {
            "vchytel_id": vid,
            "vchytel_pib": pib_mapa.get(vid, "?"),
            "predmet_id": r["predmet_id"],
            "pozaurochne_zanyattya_id": None,
            "predmet_nazva": predmet_mapa.get(r["predmet_id"], "?"),
            "navantazhennya_dnya": _navantazhennya(vid),
            "toy_samyi_predmet": predmet_id is not None and r["predmet_id"] == predmet_id,
        }
        (toy_samyi if variant["toy_samyi_predmet"] else inshi).append(variant)

    # усі пари (вчитель, позаурочне заняття) з позаурочного плану - раніше взагалі
    # не враховувались, тому позаурочні уроки завжди показували "0 кандидатів"
    vsi_pary_pozaur = conn.execute(
        "SELECT DISTINCT vchytel_id, zanyattya_id FROM pozaurochnyj_plan"
    ).fetchall()
    for r in vsi_pary_pozaur:
        vid = r["vchytel_id"]
        if vid in zainyati_tut:
            continue
        variant = {
            "vchytel_id": vid,
            "vchytel_pib": pib_mapa.get(vid, "?"),
            "predmet_id": None,
            "pozaurochne_zanyattya_id": r["zanyattya_id"],
            "predmet_nazva": zanyattya_mapa.get(r["zanyattya_id"], "?") + " (позаурочне)",
            "navantazhennya_dnya": _navantazhennya(vid),
            "toy_samyi_predmet": (
                pozaurochne_zanyattya_id is not None and r["zanyattya_id"] == pozaurochne_zanyattya_id
            ),
        }
        (toy_samyi if variant["toy_samyi_predmet"] else inshi).append(variant)

    toy_samyi.sort(key=lambda v: (v["navantazhennya_dnya"], ukr_klyuch(v["vchytel_pib"])))
    inshi.sort(key=lambda v: (v["navantazhennya_dnya"], ukr_klyuch(v["vchytel_pib"])))

    return (toy_samyi + inshi)[:max_kandydativ]


@app.route("/zaminy")
def zaminy():
    conn = get_connection()

    data_str = request.args.get("data", "").strip()
    den_label = None
    variant_na_datu = None
    if data_str:
        try:
            data_obj = datetime.strptime(data_str, "%Y-%m-%d")
            den = data_obj.weekday() + 1
            if den <= 5:
                den_label = DAYS_LABELS_UA[den - 1]
                tbl, variant_na_datu = _dzherelo_rozkladu(conn, data_str)
                all_vchyteli = conn.execute(
                    f"""
                    SELECT DISTINCT v.* FROM vchyteli v
                    JOIN {tbl} gu ON gu.vchytel_id = v.id
                    WHERE gu.den = ?
                    ORDER BY v.pib COLLATE UKR
                    """,
                    (den,),
                ).fetchall()
            else:
                den_label = "вихідний"
                all_vchyteli = []
        except ValueError:
            all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    else:
        all_vchyteli = []

    ostanni = conn.execute(
        """
        SELECT ul.id, ul.data, ul.status, ul.prychyna, ul.urok_nomer,
               k.paralel, k.litera, COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
               v1.pib AS planovanyi_pib, v2.pib AS faktychnyi_pib
        FROM urok_log ul
        JOIN klasy k ON k.id = ul.klas_id
        LEFT JOIN predmety p ON p.id = ul.predmet_id
        LEFT JOIN pozaurochni_zanyattya pz ON pz.id = ul.pozaurochne_zanyattya_id
        LEFT JOIN vchyteli v1 ON v1.id = ul.planovanyi_vchytel_id
        LEFT JOIN vchyteli v2 ON v2.id = ul.faktychnyi_vchytel_id
        ORDER BY ul.data DESC, k.paralel, ul.urok_nomer
        LIMIT 100
        """
    ).fetchall()
    conn.close()
    return render_template(
        "zaminy.html", active="zaminy", all_vchyteli=all_vchyteli, ostanni=ostanni,
        data_str=data_str, den_label=den_label,
        variant_na_datu=dict(variant_na_datu) if variant_na_datu else None,
    )


@app.route("/zaminy/pidibraty", methods=["POST"])
def zaminy_pidibraty():
    data_str = request.form.get("data", "").strip()
    vidsutni_ids = [int(v) for v in request.form.getlist("vidsutni_ids")]

    errors = []
    try:
        data_obj = datetime.strptime(data_str, "%Y-%m-%d")
    except ValueError:
        errors.append("Некоректна дата.")
        data_obj = None
    if not vidsutni_ids:
        errors.append("Оберіть хоча б одного відсутнього вчителя.")

    conn = get_connection()
    if errors or (data_obj and data_obj.weekday() + 1 > 5):
        if data_obj and data_obj.weekday() + 1 > 5:
            errors.append(f"{DAYS_LABELS_UA[data_obj.weekday()]} - вихідний, уроків немає.")
        all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
        conn.close()
        return render_template(
            "zaminy.html", active="zaminy", all_vchyteli=all_vchyteli, ostanni=[],
            errors=errors, data_str=data_str,
        )

    den = data_obj.weekday() + 1

    # усі уроки всіх відсутніх вчителів цього дня (навчальні І позаурочні - раніше
    # позаурочні (виховна тощо) мовчки випадали через INNER JOIN predmety, бо в
    # них predmet_id завжди NULL - виправлено на LEFT JOIN з обома типами)
    placeholders = ",".join("?" for _ in vidsutni_ids)
    tbl, _variant = _dzherelo_rozkladu(conn, data_str)
    uroky_rows = conn.execute(
        f"""
        SELECT gu.id, gu.urok, gu.predmet_id, gu.pozaurochne_zanyattya_id, gu.klas_id, gu.vchytel_id,
               COALESCE(p.nazva, pz.nazva) AS predmet_nazva, k.paralel, k.litera, v.pib AS vidsutnii_pib
        FROM {tbl} gu
        LEFT JOIN predmety p ON p.id = gu.predmet_id
        LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
        JOIN klasy k ON k.id = gu.klas_id
        JOIN vchyteli v ON v.id = gu.vchytel_id
        WHERE gu.vchytel_id IN ({placeholders}) AND gu.den = ?
        ORDER BY gu.urok, k.paralel
        """,
        (*vidsutni_ids, den),
    ).fetchall()

    if not uroky_rows:
        all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
        conn.close()
        return render_template(
            "zaminy.html", active="zaminy", all_vchyteli=all_vchyteli, ostanni=[],
            errors=["У обраних вчителів немає жодного уроку в цей день за тижневим шаблоном."],
            data_str=data_str,
        )

    # призначені кандидати цього самого прогону не повинні перетинатись між собою -
    # відстежуємо по мірі підбору
    vzhe_pryznacheni_za_urokom = {}  # urok_nomer -> set(vchytel_id)
    uroky = []
    for u in uroky_rows:
        zainyati = set(vidsutni_ids) | vzhe_pryznacheni_za_urokom.get(u["urok"], set())
        kandydaty = _znajty_kandydativ(
            conn, den, u["urok"], u["predmet_id"], zainyati,
            pozaurochne_zanyattya_id=u["pozaurochne_zanyattya_id"], tbl=tbl,
        )
        # тимчасово резервуємо найкращого кандидата, щоб не запропонувати того самого
        # вченого одразу на два уроки в той самий час у цьому прогоні підбору
        if kandydaty:
            vzhe_pryznacheni_za_urokom.setdefault(u["urok"], set()).add(kandydaty[0]["vchytel_id"])

        uroky.append({
            "urok": u["urok"],
            "predmet_id": u["predmet_id"],
            "pozaurochne_zanyattya_id": u["pozaurochne_zanyattya_id"],
            "predmet_nazva": u["predmet_nazva"],
            "klas_id": u["klas_id"],
            "klas_nazva": klas_nazva(u["paralel"], u["litera"]),
            "vidsutnii_pib": u["vidsutnii_pib"],
            "vidsutnii_id": u["vchytel_id"],
            "kandydaty": kandydaty,
            "potrebuye_ruchnogo": len(kandydaty) == 0,
        })

    conn.close()
    return render_template(
        "zaminy_review.html", active="zaminy", data_str=data_str,
        vidsutni_ids=vidsutni_ids, uroky=uroky,
    )


def _zapysaty_zaminu(conn, data_str, klas_id, urok, predmet_id, pozaur_id, vidsutnii_id, rishennya):
    """Один запис журналу замін на дату: rishennya = 'skasovano' або
    'faktychnyi_predmet_id:faktychne_pozaurochne_id:faktychnyi_vchytel_id'.
    Спільно для підбору на один день і на період."""
    if rishennya == "skasovano":
        conn.execute(
            """
            INSERT INTO urok_log
                (data, klas_id, predmet_id, pozaurochne_zanyattya_id, planovanyi_vchytel_id,
                 urok_nomer, status, prychyna)
            VALUES (?, ?, ?, ?, ?, ?, 'skasovano', ?)
            ON CONFLICT(data, klas_id, urok_nomer) DO UPDATE SET
                status = 'skasovano', prychyna = excluded.prychyna,
                faktychnyi_vchytel_id = NULL, faktychnyi_predmet_id = NULL,
                faktychne_pozaurochne_zanyattya_id = NULL
            """,
            (data_str, klas_id, predmet_id, pozaur_id, vidsutnii_id, urok, "Вчитель відсутній"),
        )
        return
    fakt_predmet_str, fakt_pozaur_str, fakt_vchytel_str = rishennya.split(":")
    conn.execute(
        """
        INSERT INTO urok_log
            (data, klas_id, predmet_id, pozaurochne_zanyattya_id, planovanyi_vchytel_id,
             faktychnyi_vchytel_id, faktychnyi_predmet_id, faktychne_pozaurochne_zanyattya_id,
             urok_nomer, status, prychyna)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'zamina', ?)
        ON CONFLICT(data, klas_id, urok_nomer) DO UPDATE SET
            status = 'zamina', faktychnyi_vchytel_id = excluded.faktychnyi_vchytel_id,
            faktychnyi_predmet_id = excluded.faktychnyi_predmet_id,
            faktychne_pozaurochne_zanyattya_id = excluded.faktychne_pozaurochne_zanyattya_id,
            prychyna = excluded.prychyna
        """,
        (data_str, klas_id, predmet_id, pozaur_id, vidsutnii_id,
         int(fakt_vchytel_str), int(fakt_predmet_str) if fakt_predmet_str else None,
         int(fakt_pozaur_str) if fakt_pozaur_str else None, urok, "Заміна - вчитель відсутній"),
    )


def _navchalni_dni_periodu(conn, d_vid, d_do):
    """[(дата 'РРРР-ММ-ДД', день тижня за розкладом 1..6)] для кожного навчального дня
    періоду включно: вихідні (за dniv_na_tyzhden) і дні поза семестрами/на канікулах
    пропускаються, святкові й карантинні (календар) - теж, а робоча субота
    рахується за розкладом того дня, за який її відпрацьовують."""
    settings = conn.execute("SELECT * FROM school_settings WHERE id = 1").fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    kalendar = _nenavchalni_dni_mapa(conn)
    semestry = []
    for poch, kin in (("semestr1_pochatok", "semestr1_kinets"), ("semestr2_pochatok", "semestr2_kinets")):
        a, b = _parse_date_safe(settings[poch]), _parse_date_safe(settings[kin])
        if a and b:
            semestry.append((a, b))
    kanikuly = _kanikuly_periody(settings)
    result = []
    d = d_vid
    while d <= d_do:
        d_str = d.strftime("%Y-%m-%d")
        kod = kalendar.get(d_str)
        if semestry and not any(a <= d <= b for a, b in semestry):
            pass
        elif any(a <= d <= b for a, b in kanikuly):
            pass
        elif kod in ("svyato", "karantyn"):
            pass
        elif kod and kod.startswith("robochyi:"):
            result.append((d_str, int(kod.split(":")[1])))
        elif d.weekday() + 1 <= dniv:
            result.append((d_str, d.weekday() + 1))
        d += timedelta(days=1)
    return result


MAKS_DNIV_PERIODU_ZAMIN = 60


@app.route("/zaminy/pidibraty-period", methods=["POST"])
def zaminy_pidibraty_period():
    """Відсутність на кілька днів (лікарняний, відрядження): для КОЖНОГО навчального
    дня періоду на кожен урок відсутніх береться найкращий кандидат (той самий
    предмет, найменше навантаження того дня), а де нікого - скасування. Показуємо
    компактний перегляд усіх рішень; правити вручну - через одноденний підбір."""
    data_str = request.form.get("data", "").strip()
    data_do_str = request.form.get("data_do", "").strip()
    vidsutni_ids = [int(v) for v in request.form.getlist("vidsutni_ids")]
    d_vid, d_do = _parse_date_safe(data_str), _parse_date_safe(data_do_str)
    errors = []
    if not d_vid or not d_do:
        errors.append("Вкажіть обидві дати періоду.")
    elif d_do < d_vid:
        errors.append("Кінець періоду раніше за початок.")
    elif (d_do - d_vid).days > MAKS_DNIV_PERIODU_ZAMIN:
        errors.append(f"Період надто довгий (більше {MAKS_DNIV_PERIODU_ZAMIN} днів) - розбийте на частини.")
    if not vidsutni_ids:
        errors.append("Оберіть хоча б одного відсутнього вчителя.")
    conn = get_connection()
    if errors:
        all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
        conn.close()
        return render_template("zaminy.html", active="zaminy", all_vchyteli=all_vchyteli, ostanni=[],
                               errors=errors, data_str=data_str, data_do_str=data_do_str)

    dni = _navchalni_dni_periodu(conn, d_vid, d_do)
    placeholders = ",".join("?" for _ in vidsutni_ids)
    riadky = []
    for d_str, den in dni:
        tbl, _variant = _dzherelo_rozkladu(conn, d_str)   # розклад, що діяв саме цього дня
        uroky = conn.execute(
            f"""
            SELECT gu.urok, gu.predmet_id, gu.pozaurochne_zanyattya_id, gu.klas_id, gu.vchytel_id,
                   COALESCE(p.nazva, pz.nazva) AS predmet_nazva, k.paralel, k.litera, v.pib AS vidsutnii_pib
            FROM {tbl} gu
            LEFT JOIN predmety p ON p.id = gu.predmet_id
            LEFT JOIN pozaurochni_zanyattya pz ON pz.id = gu.pozaurochne_zanyattya_id
            JOIN klasy k ON k.id = gu.klas_id
            JOIN vchyteli v ON v.id = gu.vchytel_id
            WHERE gu.vchytel_id IN ({placeholders}) AND gu.den = ?
            ORDER BY gu.urok, k.paralel, k.litera COLLATE UKR
            """,
            (*vidsutni_ids, den),
        ).fetchall()
        vzhe = {}
        for u in uroky:
            zainyati = set(vidsutni_ids) | vzhe.get(u["urok"], set())
            kand = _znajty_kandydativ(conn, den, u["urok"], u["predmet_id"], zainyati,
                                      pozaurochne_zanyattya_id=u["pozaurochne_zanyattya_id"], tbl=tbl)
            if kand:
                k = kand[0]
                rishennya = f"{k['predmet_id'] or ''}:{k['pozaurochne_zanyattya_id'] or ''}:{k['vchytel_id']}"
                opys = ("✓ " if k["toy_samyi_predmet"] else "інше: ") + f"{k['predmet_nazva']} — {k['vchytel_pib']}"
                vzhe.setdefault(u["urok"], set()).add(k["vchytel_id"])
            else:
                rishennya, opys = "skasovano", "Скасувати (нікого вільного)"
            riadky.append({
                "data": d_str, "den": den, "urok": u["urok"], "klas_id": u["klas_id"],
                "klas_nazva": klas_nazva(u["paralel"], u["litera"]),
                "predmet_id": u["predmet_id"], "pozaurochne_zanyattya_id": u["pozaurochne_zanyattya_id"],
                "predmet_nazva": u["predmet_nazva"], "vidsutnii_id": u["vchytel_id"],
                "vidsutnii_pib": u["vidsutnii_pib"], "rishennya": rishennya, "opys": opys,
            })
    conn.close()
    return render_template(
        "zaminy_period_review.html", active="zaminy", data_str=data_str, data_do_str=data_do_str,
        dniv=len(dni), riadky=riadky, skasovano=sum(1 for r in riadky if r["rishennya"] == "skasovano"),
    )


@app.route("/zaminy/zberegty-period", methods=["POST"])
def zaminy_zberegty_period():
    zapysy = request.form.getlist("zapys")   # "дата|klas_id|urok|predmet_id|pozaur_id|vidsutnii_id|rishennya"
    conn = get_connection()
    n = 0
    for z in zapysy:
        data_str, klas_id, urok, predmet_id, pozaur_id, vidsutnii_id, rishennya = z.split("|", 6)
        _zapysaty_zaminu(conn, data_str, int(klas_id), int(urok),
                         int(predmet_id) if predmet_id else None, int(pozaur_id) if pozaur_id else None,
                         int(vidsutnii_id), rishennya)
        n += 1
    conn.commit()
    conn.close()
    flash(f"Записано замін і скасувань на період: {n}.", "success")
    return redirect(url_for("zaminy"))


@app.route("/zaminy/zberegty-masovo", methods=["POST"])
def zaminy_zberegty_masovo():
    data_str = request.form.get("data", "").strip()
    zapysy = request.form.getlist("urok_id")  # "klas_id:urok:predmet_id:pozaurochne_id:vidsutnii_id"

    conn = get_connection()
    for zapys in zapysy:
        klas_id_str, urok_str, predmet_id_str, pozaur_id_str, vidsutnii_id_str = zapys.split(":")
        predmet_id = int(predmet_id_str) if predmet_id_str else None
        pozaur_id = int(pozaur_id_str) if pozaur_id_str else None
        rishennya = request.form.get(f"rishennya_{zapys}", "bez_zmin")

        if rishennya == "bez_zmin":
            continue
        _zapysaty_zaminu(conn, data_str, int(klas_id_str), int(urok_str), predmet_id, pozaur_id,
                         int(vidsutnii_id_str), rishennya)
    conn.commit()
    conn.close()
    return redirect(url_for("zaminy"))


@app.route("/zaminy/druk")
def zaminy_druk():
    data_str = request.args.get("data", "").strip()
    zapysy = []
    if data_str:
        conn = get_connection()
        zapysy = conn.execute(
            """
            SELECT ul.urok_nomer, ul.status, ul.prychyna,
                   k.paralel, k.litera, COALESCE(p.nazva, pz.nazva) AS predmet_nazva,
                   v1.pib AS planovanyi_pib, v2.pib AS faktychnyi_pib
            FROM urok_log ul
            JOIN klasy k ON k.id = ul.klas_id
            LEFT JOIN predmety p ON p.id = ul.predmet_id
            LEFT JOIN pozaurochni_zanyattya pz ON pz.id = ul.pozaurochne_zanyattya_id
            LEFT JOIN vchyteli v1 ON v1.id = ul.planovanyi_vchytel_id
            LEFT JOIN vchyteli v2 ON v2.id = ul.faktychnyi_vchytel_id
            WHERE ul.data = ?
            ORDER BY k.paralel, k.litera COLLATE UKR, ul.urok_nomer
            """,
            (data_str,),
        ).fetchall()
        conn.close()
    return render_template("zaminy_druk.html", data_str=data_str, zapysy=zapysy)


@app.route("/zaminy/skasuvaty", methods=["POST"])
def zaminy_skasuvaty():
    ul_id = request.form.get("id")
    conn = get_connection()
    conn.execute("DELETE FROM urok_log WHERE id = ?", (ul_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("zaminy"))


def _stan_rozkladu(conn, rivni):
    """Що зараз лежить у generated_urok - для сторінки генерації: уроків по
    кожному рівню закладу, список збережених варіантів, чи є розклад узагалі.
    Викликається і при відкритті сторінки, і ЩЕ РАЗ після генерації (інакше
    показані числа й позначка "поточний" були б станом до неї)."""
    rivni_stan = []
    for nazva, p_vid, p_do in rivni:
        n = conn.execute(
            "SELECT COUNT(*) c FROM generated_urok gu JOIN klasy k ON k.id = gu.klas_id "
            "WHERE k.paralel BETWEEN ? AND ?",
            (p_vid, p_do),
        ).fetchone()["c"]
        rivni_stan.append({"nazva": nazva, "p_vid": p_vid, "p_do": p_do, "kilkist": n})
    varianty = _spysok_variantiv(conn)
    ye_rozklad = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"] > 0
    return rivni_stan, varianty, ye_rozklad


@app.route("/generatsiya", methods=["GET", "POST"])
def generatsiya():
    # розклад складається на ОДИН семестр (v0.2.0): "весь рік" + рядки цього
    # семестру. Вибір семестру показуємо лише тоді, коли в плані є семестрові
    # рядки - інакше обидва семестри однакові й вибір лише плутав би
    from generator import potochnyi_semestr
    conn = get_connection()
    ye_semestrovi = conn.execute("SELECT 1 FROM navchalnyj_plan WHERE semestr > 0 LIMIT 1").fetchone() is not None
    semestr = request.values.get("semestr", type=int)
    if semestr not in (1, 2):
        semestr = potochnyi_semestr(conn)
    conn.close()

    navantazhennya = _perevirka_navantazhennya(semestr)
    obovyazkovyi_podil = _perevirka_obovyazkovogo_podilu(semestr)
    kabinety_klasiv = _perevirka_kabinetiv_klasiv()

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    files = [f for f in os.listdir(SCHEDULE_DIR) if f.lower().endswith((".xlsx", ".pdf"))]
    files_info = []
    for f in files:
        full_path = os.path.join(SCHEDULE_DIR, f)
        mtime = os.path.getmtime(full_path)
        files_info.append({
            "filename": f,
            "date": datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S"),
            "mtime": mtime,
        })
    files_info.sort(key=lambda x: x["mtime"], reverse=True)

    conn = get_connection()
    settings = conn.execute("SELECT tip_zakladu, fizychni_yadra FROM school_settings WHERE id = 1").fetchone()
    tip_zakladu = settings["tip_zakladu"] if settings else "stara_shkola"
    rivni = TIP_ZAKLADU_OPTIONS.get(tip_zakladu, TIP_ZAKLADU_OPTIONS["stara_shkola"])["rivni"]

    rivni_stan, varianty, ye_rozklad = _stan_rozkladu(conn, rivni)

    usi_klasy = conn.execute("SELECT id, paralel, litera FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    usi_klasy_json = json.dumps([
        {"id": k["id"], "nazva": klas_nazva(k["paralel"], k["litera"])} for k in usi_klasy
    ])
    klasy_poza_zakladom = _klasy_poza_zakladom(conn, tip_zakladu)
    conn.close()

    if request.method == "POST":
        if obovyazkovyi_podil or klasy_poza_zakladom:
            # захист і на бекенді, не лише приховуванням кнопок в HTML - хтось міг
            # відкрити стару вкладку/надіслати запит напряму, поки перелік ще не
            # виправлено
            if klasy_poza_zakladom:
                message = (
                    "Генерацію заблоковано: є класи поза діапазоном поточного типу "
                    "закладу (див. перелік) — поверніть відповідний рівень у "
                    '"Налаштуваннях школи" або приберіть/перенесіть ці класи.'
                )
            else:
                message = (
                    "Генерацію заблоковано: за нормативом МОН потрібно призначити "
                    "вчителя підгрупи 2 для позицій нижче (див. перелік) — спочатку "
                    "виправте в Навчальному плані."
                )
            result = {"success": False, "message": message}
            _zberegty_ostanniu_generatsiyu(result)
            return render_template(
                "generatsiya.html", active="generatsiya", result=result,
                navantazhennya=navantazhennya, obovyazkovyi_podil=obovyazkovyi_podil,
                klasy_poza_zakladom=klasy_poza_zakladom,
                rivni=rivni, rivni_stan=rivni_stan,
                settings=settings, usi_klasy_json=usi_klasy_json, files=files_info,
                varianty=varianty, ye_rozklad=ye_rozklad, kabinety_klasiv=kabinety_klasiv,
                semestr=semestr, ye_semestrovi=ye_semestrovi,
            )

        from generator import generate_schedule
        riven_idx = request.form.get("riven_idx", "").strip()
        chas_vybir = request.form.get("chas_sekund", "").strip()
        maks_sekund = int(chas_vybir) if chas_vybir.isdigit() else None
        # своє число хвилин (поле поруч із кнопками 3/5/10 хв): від 1 хв до 2 год
        chas_hvylyn = request.form.get("chas_hvylyn", "").strip()
        if chas_hvylyn.isdigit():
            maks_sekund = min(max(int(chas_hvylyn), 1), 120) * 60
        if riven_idx and riven_idx.isdigit() and int(riven_idx) < len(rivni):
            _, p_vid, p_do = rivni[int(riven_idx)]
        else:
            p_vid, p_do = None, None

        # "Догенерація": розбираємо сітку заморожування (клас × день, 3 стани)
        ne_chapaty = set()
        mozhlyvo = set()
        try:
            zamorozka_raw = json.loads(request.form.get("zamorozka_data", "[]"))
        except (ValueError, TypeError):
            zamorozka_raw = []
        for item in zamorozka_raw:
            kid, den, status = item.get("klas_id"), item.get("den"), item.get("status")
            if status == "ne_chapaty":
                ne_chapaty.add((kid, den))
            elif status == "mozhlyvo":
                mozhlyvo.add((kid, den))

        if not ne_chapaty and not mozhlyvo:
            # немає заморожування - звичайна одноразова генерація, як і раніше
            result = generate_schedule(p_vid, p_do, maks_sekund, semestr=semestr)
        else:
            # прохід 1: "Можливо" ТЕЖ заморожене (жорстко) - половина бюджету часу
            povnyi_chas = maks_sekund or 300
            chas_prohodu_1 = max(10, povnyi_chas // 2)
            result = generate_schedule(
                p_vid, p_do, chas_prohodu_1, zamorozheni_klityny=ne_chapaty | mozhlyvo, semestr=semestr,
            )
            zadovilno = result.get("success") and len(result.get("unplaced", [])) == 0
            if not zadovilno:
                # прохід 2: "Можливо" тепер ВІЛЬНЕ - розв'язується наново з нуля
                # (CP-SAT не вміє "послаблювати" обмеження посеред одного запуску),
                # на решту бюджету часу
                chas_prohodu_2 = max(10, povnyi_chas - chas_prohodu_1)
                result = generate_schedule(
                    p_vid, p_do, chas_prohodu_2, zamorozheni_klityny=ne_chapaty, semestr=semestr,
                )

        conn = get_connection()
        if result.get("success"):
            # розклад щойно переписано - збережений варіант, який досі був
            # "поточним", ним більше не є (при невдачі generated_urok не чіпається)
            _skynuty_aktyvnyi_variant(conn)
            conn.commit()
        # стан розкладу (уроків по рівнях, варіанти, чи є розклад) читався ДО
        # генерації - перечитуємо, щоб сторінка показувала те, що вже в базі
        rivni_stan, varianty, ye_rozklad = _stan_rozkladu(conn, rivni)
        conn.close()

        _zberegty_ostanniu_generatsiyu(result)
        return render_template(
            "generatsiya.html", active="generatsiya", result=result,
            navantazhennya=navantazhennya, obovyazkovyi_podil=obovyazkovyi_podil,
            klasy_poza_zakladom=klasy_poza_zakladom,
            rivni=rivni, rivni_stan=rivni_stan,
            settings=settings, usi_klasy_json=usi_klasy_json, files=files_info,
            varianty=varianty, ye_rozklad=ye_rozklad, kabinety_klasiv=kabinety_klasiv,
            semestr=semestr, ye_semestrovi=ye_semestrovi,
        )

    result = _zavantazhyty_ostanniu_generatsiyu()
    return render_template(
        "generatsiya.html", active="generatsiya", result=result,
        navantazhennya=navantazhennya, obovyazkovyi_podil=obovyazkovyi_podil,
        klasy_poza_zakladom=klasy_poza_zakladom,
        rivni=rivni, rivni_stan=rivni_stan,
        settings=settings, usi_klasy_json=usi_klasy_json, files=files_info,
        varianty=varianty, ye_rozklad=ye_rozklad, kabinety_klasiv=kabinety_klasiv,
        semestr=semestr, ye_semestrovi=ye_semestrovi,
    )


@app.route("/generatsiya/vydalyty", methods=["POST"])
def generatsiya_vydalyty():
    """Явне видалення згенерованого розкладу - для конкретного рівня (p_vid/p_do з
    форми) або для всієї школи одразу (порожні значення), БЕЗ повторної генерації."""
    p_vid = request.form.get("p_vid", "").strip()
    p_do = request.form.get("p_do", "").strip()

    conn = get_connection()
    if p_vid and p_do:
        conn.execute(
            "DELETE FROM generated_urok WHERE klas_id IN "
            "(SELECT id FROM klasy WHERE paralel BETWEEN ? AND ?)",
            (int(p_vid), int(p_do)),
        )
    else:
        conn.execute("DELETE FROM generated_urok")
    # інакше сторінка "Генерація розкладу" й далі показувала б застарілі цифри
    # (класів/вчителів/годин) з ОСТАННЬОЇ генерації, хоча сам розклад уже видалено
    conn.execute("DELETE FROM ostannya_generatsiya")
    _skynuty_aktyvnyi_variant(conn)
    conn.commit()
    conn.close()
    return redirect(url_for("generatsiya"))


# ---------- Збережені варіанти розкладу ----------
# generated_urok - завжди ОДИН поточний розклад, і кожна генерація його стирає.
# Варіант - названий знімок цього розкладу (плюс підсумок генерації), який завуч
# може повернути в поточний будь-коли: далі перегляд, PDF і догенерація працюють
# від нього. Знімок навмисно без FK на довідники (видалення вчителя не має
# стирати історію); при завантаженні рядки з неіснуючими id відкидаються.

_VARIANT_UROK_KOLONKY = (
    "klas_id", "predmet_id", "pozaurochne_zanyattya_id", "vchytel_id",
    "kabinet_id", "den", "urok", "parnist", "pidgrupa_2_vchytel_id",
)
_OSTANNYA_GEN_KOLONKY = (
    "data_chas", "success", "status", "message", "vseogo_urokiv",
    "kilkist_klasiv", "kilkist_vchyteliv", "kilkist_predmetiv", "unplaced_json", "chas_sekund",
)


def _dzherelo_rozkladu(conn, d_str):
    """Розклад, що ДІЯВ на дату d_str: варіант із найпізнішим "діє з" (diye_z), не
    пізнішим за дату, або поточний generated_urok, якщо такого варіанта немає.
    Повертає (назва_таблиці, варіант_або_None). Уроки варіанта кладуться в
    ТИМЧАСОВУ таблицю з'єднання з тими самими стовпцями, що й generated_urok, тож
    наявні SQL-запити замін і звітів просто підставляють назву таблиці замість
    generated_urok - логіка не дублюється. Без жодної дати "діє з" поведінка
    така сама, як до v0.0.37."""
    variant = conn.execute(
        "SELECT id, nazva, diye_z FROM rozklad_varianty "
        "WHERE diye_z IS NOT NULL AND diye_z <= ? ORDER BY diye_z DESC, id DESC LIMIT 1",
        (d_str,),
    ).fetchone()
    if variant is None:
        return "generated_urok", None
    conn.execute("DROP TABLE IF EXISTS temp._rozklad_na_datu")
    conn.execute(
        "CREATE TEMP TABLE _rozklad_na_datu AS "
        "SELECT id, klas_id, predmet_id, pozaurochne_zanyattya_id, vchytel_id, kabinet_id, den, urok, "
        "parnist, pidgrupa_2_vchytel_id FROM rozklad_varianty_uroky WHERE variant_id = ?",
        (variant["id"],),
    )
    return "_rozklad_na_datu", variant


def _spysok_variantiv(conn):
    """Варіанти для сторінки генерації, найновіші згори, з коротким підсумком
    (статус / нерозміщені) зі знімка ostannya_generatsiya."""
    rows = conn.execute(
        "SELECT id, nazva, stvoreno, notatka, urokiv, pidsumok_json, aktyvnyi, diye_z "
        "FROM rozklad_varianty ORDER BY stvoreno DESC, id DESC"
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        pidsumok = {}
        if r["pidsumok_json"]:
            try:
                pidsumok = json.loads(r["pidsumok_json"]) or {}
            except (ValueError, TypeError):
                pidsumok = {}
        row["status"] = pidsumok.get("status") if pidsumok.get("success") else None
        nerozm = 0
        if pidsumok.get("unplaced_json"):
            try:
                nerozm = len(json.loads(pidsumok["unplaced_json"]) or [])
            except (ValueError, TypeError):
                nerozm = 0
        row["nerozmishcheno"] = nerozm
        result.append(row)
    return result


def _skynuty_aktyvnyi_variant(conn):
    """Поточний розклад змінився (генерація / видалення) - жоден збережений
    варіант більше не дорівнює йому."""
    conn.execute("UPDATE rozklad_varianty SET aktyvnyi = 0 WHERE aktyvnyi = 1")


@app.route("/rozklad-varianty/zberegty", methods=["POST"])
def rozklad_varianty_zberegty():
    nazva = (request.form.get("nazva") or "").strip()[:80]
    notatka = (request.form.get("notatka") or "").strip()[:300] or None
    diye_z = (request.form.get("diye_z") or "").strip() or None
    if diye_z and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", diye_z):
        diye_z = None

    conn = get_connection()
    urokiv = conn.execute("SELECT COUNT(*) c FROM generated_urok").fetchone()["c"]
    if urokiv == 0:
        conn.close()
        flash("Зберігати нема чого - поточного розкладу немає.", "error")
        return redirect(url_for("generatsiya"))

    chas = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not nazva:
        nazva = f"Варіант від {chas}"
    pidsumok = conn.execute("SELECT * FROM ostannya_generatsiya WHERE id = 1").fetchone()
    pidsumok_json = (
        json.dumps({k: pidsumok[k] for k in _OSTANNYA_GEN_KOLONKY if k in pidsumok.keys()},
                   ensure_ascii=False)
        if pidsumok else None
    )
    cur = conn.execute(
        "INSERT INTO rozklad_varianty (nazva, stvoreno, notatka, urokiv, pidsumok_json, aktyvnyi, diye_z) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (nazva, chas, notatka, urokiv, pidsumok_json, diye_z),
    )
    variant_id = cur.lastrowid
    kolonky = ", ".join(_VARIANT_UROK_KOLONKY)
    conn.execute(
        f"INSERT INTO rozklad_varianty_uroky (variant_id, {kolonky}) "
        f"SELECT ?, {kolonky} FROM generated_urok",
        (variant_id,),
    )
    # щойно збережений = точна копія поточного; попередній "поточний" уже не він
    conn.execute("UPDATE rozklad_varianty SET aktyvnyi = 0 WHERE id != ?", (variant_id,))
    conn.commit()
    conn.close()
    flash(f'Варіант «{nazva}» збережено ({urokiv} уроків).', "success")
    return redirect(url_for("generatsiya"))


@app.route("/rozklad-varianty/<int:variant_id>/zavantazhyty", methods=["POST"])
def rozklad_varianty_zavantazhyty(variant_id):
    conn = get_connection()
    variant = conn.execute("SELECT * FROM rozklad_varianty WHERE id = ?", (variant_id,)).fetchone()
    if variant is None:
        conn.close()
        abort(404)
    uroky = conn.execute(
        "SELECT * FROM rozklad_varianty_uroky WHERE variant_id = ? ORDER BY id", (variant_id,)
    ).fetchall()

    def _ids(tablytsya):
        return {r["id"] for r in conn.execute(f"SELECT id FROM {tablytsya}")}

    klasy, vchyteli, predmety_ids = _ids("klasy"), _ids("vchyteli"), _ids("predmety")
    zanyattya, kabinety_ids = _ids("pozaurochni_zanyattya"), _ids("kabinety")

    # одна транзакція: або весь варіант став поточним, або нічого не змінилось
    try:
        conn.execute("DELETE FROM generated_urok")
        zavantazheno = 0
        vidkynuto = 0
        for u in uroky:
            # обов'язкові посилання (generated_urok має на них FK) - рядок без них
            # завантажити неможливо; кабінет і 2-й вчитель - необов'язкові, знімаємо
            if (u["klas_id"] not in klasy or u["vchytel_id"] not in vchyteli
                    or (u["predmet_id"] is not None and u["predmet_id"] not in predmety_ids)
                    or (u["pozaurochne_zanyattya_id"] is not None
                        and u["pozaurochne_zanyattya_id"] not in zanyattya)):
                vidkynuto += 1
                continue
            kabinet_id = u["kabinet_id"] if u["kabinet_id"] in kabinety_ids else None
            pg2 = u["pidgrupa_2_vchytel_id"] if u["pidgrupa_2_vchytel_id"] in vchyteli else None
            conn.execute(
                "INSERT INTO generated_urok (klas_id, predmet_id, pozaurochne_zanyattya_id, "
                "vchytel_id, kabinet_id, den, urok, parnist, pidgrupa_2_vchytel_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (u["klas_id"], u["predmet_id"], u["pozaurochne_zanyattya_id"], u["vchytel_id"],
                 kabinet_id, u["den"], u["urok"], u["parnist"] or "oboydva", pg2),
            )
            zavantazheno += 1

        # підсумок генерації - той, що був на момент збереження варіанта; число
        # уроків - реально завантажене (частину могло бути відкинуто)
        conn.execute("DELETE FROM ostannya_generatsiya")
        if variant["pidsumok_json"]:
            try:
                p = json.loads(variant["pidsumok_json"]) or {}
            except (ValueError, TypeError):
                p = {}
            if p:
                p["vseogo_urokiv"] = zavantazheno
                conn.execute(
                    "INSERT INTO ostannya_generatsiya (id, " + ", ".join(_OSTANNYA_GEN_KOLONKY) + ") "
                    "VALUES (1, " + ", ".join("?" for _ in _OSTANNYA_GEN_KOLONKY) + ")",
                    tuple(p.get(k) for k in _OSTANNYA_GEN_KOLONKY),
                )
        conn.execute(
            "UPDATE rozklad_varianty SET aktyvnyi = CASE WHEN id = ? THEN 1 ELSE 0 END",
            (variant_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    if vidkynuto:
        flash(
            f'Варіант «{variant["nazva"]}» завантажено як поточний: {zavantazheno} уроків. '
            f"УВАГА: {vidkynuto} уроків відкинуто - їхніх класів, вчителів або предметів "
            "уже немає в довідниках (видалені після збереження варіанта).",
            "error",
        )
    else:
        flash(f'Варіант «{variant["nazva"]}» завантажено як поточний: {zavantazheno} уроків.', "success")
    return redirect(url_for("generatsiya"))


@app.route("/rozklad-varianty/<int:variant_id>/diye-z", methods=["POST"])
def rozklad_varianty_diye_z(variant_id):
    """Задати/зняти дату "діє з" у збереженого варіанта."""
    diye_z = (request.form.get("diye_z") or "").strip() or None
    if diye_z and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", diye_z):
        flash("Дата має бути у форматі РРРР-ММ-ДД.", "error")
        return redirect(url_for("generatsiya"))
    conn = get_connection()
    variant = conn.execute("SELECT nazva FROM rozklad_varianty WHERE id = ?", (variant_id,)).fetchone()
    if variant is None:
        conn.close()
        abort(404)
    conn.execute("UPDATE rozklad_varianty SET diye_z = ? WHERE id = ?", (diye_z, variant_id))
    conn.commit()
    conn.close()
    flash(f'Варіант «{variant["nazva"]}»: {"діє з " + diye_z if diye_z else "дату «діє з» знято"}.', "success")
    return redirect(url_for("generatsiya"))


@app.route("/rozklad-varianty/<int:variant_id>/vydalyty", methods=["POST"])
def rozklad_varianty_vydalyty(variant_id):
    conn = get_connection()
    variant = conn.execute("SELECT nazva FROM rozklad_varianty WHERE id = ?", (variant_id,)).fetchone()
    if variant is not None:
        # уроки варіанта підуть каскадом (FK ON DELETE CASCADE у rozklad_varianty_uroky)
        conn.execute("DELETE FROM rozklad_varianty WHERE id = ?", (variant_id,))
        conn.commit()
        flash(f'Варіант «{variant["nazva"]}» видалено. Поточний розклад не зачеплено.', "success")
    conn.close()
    return redirect(url_for("generatsiya"))


# ---------- Перехід на новий навчальний рік ----------
# Щорічна дія в серпні. Раніше єдиним варіантом було "Очистити базу" (стирає й
# довідники) або ручне перебирання 27 класів. Правила переходу (ті самі - у
# тексті попереднього перегляду на сторінці, щоб завуч бачив їх ДО підтвердження):
#   - випускний клас (найстарша паралель закладу) видаляється з усім своїм;
#   - решта класів у діапазоні закладу переходять на паралель вище зі своїми
#     вчителями, класним керівником, асистентами, позаурочним планом;
#   - навчальний план класу замінюється планом, який торік мав клас наступної
#     паралелі (предмети, години, поділи), а ВЧИТЕЛЬ лишається той, що вів цей
#     предмет у цьому класі, - окрім переходу 4->5 (початкова -> предметники),
#     де беруться вчителі торішнього 5-го; предмет, якого в класі не було, дістає
#     вчителя з торішнього плану наступної паралелі;
#   - клас-комплект, член якого виріс за 4-й клас, розпадається; комплект з
#     одним членом видаляється;
#   - за бажанням створюються нові класи наймолодшої паралелі з торішніми
#     літерами (порожні, без кабінету, з планом торішньої наймолодшої паралелі);
#   - річне стирається: розклад, варіанти, заміни, календар ненавчальних днів,
#     дати семестрів і канікул, міжпредметні групи, профільні групи, доступність
#     класів (вона прив'язана до класу, який змінився);
#   - вчителі, предмети, кабінети, доступність вчителів і кабінетів - без змін;
#   - перед усім - автоматична резервна копія.

_NOVYI_RIK_RICHNI_TABLYTSI = (
    "generated_urok", "ostannya_generatsiya", "rozklad_varianty_uroky", "rozklad_varianty",
    "urok_log", "nenavchalni_dni", "class_availability",
    "grupa_navchalnyj_plan", "grupy_klasy", "grupy",
    "pidsumky_semestru", "zakryti_semestry",
    "peredachi_znimky", "peredachi_hodyn", "zatverdzheni_hodyny",
)


def _novyi_rik_ohlyad(conn):
    """Що зробить перехід - для попереднього перегляду й для самого переходу."""
    tip = _potochnyi_tip_zakladu(conn)
    p_vid, p_do = diapazon_paraleley_zakladu(tip)
    klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    vypusk = [k for k in klasy if k["paralel"] == p_do]
    perehid = [k for k in klasy if p_vid <= k["paralel"] < p_do]
    poza = [k for k in klasy if not (p_vid <= k["paralel"] <= p_do)]
    litery_novyh = [k["litera"] for k in klasy if k["paralel"] == p_vid]
    # комплекти, що розпадуться: член із paralel 4 стане 5-м
    komplekty_rozpad = []
    for kk in conn.execute("SELECT id, nazva FROM klas_komplekty"):
        chleny = conn.execute("SELECT paralel FROM klasy WHERE komplekt_id = ?", (kk["id"],)).fetchall()
        if any(c["paralel"] >= 4 for c in chleny):
            komplekty_rozpad.append(kk["nazva"])
    # паралелі, для яких немає торішнього плану-зразка (клас туди перейде зі своїм планом)
    paraleli_z_planom = {r[0] for r in conn.execute(
        "SELECT DISTINCT k.paralel FROM navchalnyj_plan np JOIN klasy k ON k.id = np.klas_id")}
    bez_zrazka = sorted({k["paralel"] + 1 for k in perehid if (k["paralel"] + 1) not in paraleli_z_planom})
    def _n(t):
        return conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    return {
        "tip": tip, "p_vid": p_vid, "p_do": p_do,
        "vypusk": [klas_nazva(k["paralel"], k["litera"]) for k in vypusk],
        "perehid": [f'{klas_nazva(k["paralel"], k["litera"])} → {k["paralel"] + 1}{"-" + k["litera"] if k["litera"] else ""}' for k in perehid],
        "poza": [klas_nazva(k["paralel"], k["litera"]) for k in poza],
        "litery_novyh": litery_novyh,
        "novi_nazvy": [klas_nazva(p_vid, l) for l in litery_novyh],
        "komplekty_rozpad": komplekty_rozpad,
        "bez_zrazka": bez_zrazka,
        "plan_riadkiv": _n("navchalnyj_plan"),
        "rozklad": _n("generated_urok"), "varianty": _n("rozklad_varianty"),
        "zaminy": _n("urok_log"), "kalendar": _n("nenavchalni_dni"),
        "grupy": _n("grupy"), "mizhpredmetni": _n("mizhpredmetni_grupy"),
    }


def _novyi_rik_vykonaty(conn, stvoryty_novi):
    """Сам перехід - в одній транзакції (commit робить викликач). Повертає підсумок."""
    o = _novyi_rik_ohlyad(conn)
    p_vid, p_do = o["p_vid"], o["p_do"]

    # 1. знімки ДО змін: план-зразок кожної паралелі (клас із найповнішим планом)
    #    і власний план кожного класу
    zrazok = {}       # paralel -> [rows]
    vlasnyi = {}      # klas_id -> {predmet_id: row}
    for k in conn.execute("SELECT id, paralel FROM klasy"):
        rows = conn.execute("SELECT * FROM navchalnyj_plan WHERE klas_id = ? ORDER BY id", (k["id"],)).fetchall()
        vlasnyi[k["id"]] = {}
        for r in rows:
            vlasnyi[k["id"]].setdefault(r["predmet_id"], r)   # перший рядок предмета (oboydva зазвичай)
        # у зразок міжпредметна група входить ОДНИМ представником (з найбільшими
        # годинами), як і в підрахунку навантаження класу: інакше клас із групою
        # "англ.||нім." виглядав би найповнішим, і всі класи паралелі дістали б
        # обидві мови як окремі години - понад норму (реальний випадок: 9-В демо)
        mizh_maks = {}
        for r in rows:
            gid = r["mizhpredmetna_grupa_id"]
            if gid and r["godyn_tyzhden"] > mizh_maks.get(gid, (None, -1))[1]:
                mizh_maks[gid] = (r["id"], r["godyn_tyzhden"])
        dlya_zrazka = [r for r in rows
                       if not r["mizhpredmetna_grupa_id"] or mizh_maks[r["mizhpredmetna_grupa_id"]][0] == r["id"]]
        if dlya_zrazka and len(dlya_zrazka) > len(zrazok.get(k["paralel"], [])):
            zrazok[k["paralel"]] = dlya_zrazka
    stara_paralel = {k["id"]: k["paralel"] for k in conn.execute("SELECT id, paralel FROM klasy")}

    # 2. випуск
    vypusk_ids = [r[0] for r in conn.execute("SELECT id FROM klasy WHERE paralel = ?", (p_do,))]
    for kid in vypusk_ids:
        conn.execute("DELETE FROM klasy WHERE id = ?", (kid,))     # каскад: план, позаурочка, асистенти

    # 3. зсув паралелей згори донизу (UNIQUE(paralel, litera) не порушується)
    for p in range(p_do - 1, p_vid - 1, -1):
        conn.execute("UPDATE klasy SET paralel = paralel + 1 WHERE paralel = ?", (p,))

    # 4. комплекти: член понад 4-й клас виходить; комплект з <2 членами - геть
    conn.execute("UPDATE klasy SET komplekt_id = NULL, tip_klasu = 'zvychajnyj' WHERE komplekt_id IS NOT NULL AND paralel > 4")
    for kk in conn.execute("SELECT id FROM klas_komplekty").fetchall():
        n = conn.execute("SELECT COUNT(*) c FROM klasy WHERE komplekt_id = ?", (kk["id"],)).fetchone()["c"]
        if n < 2:
            conn.execute("UPDATE klasy SET komplekt_id = NULL, tip_klasu = 'zvychajnyj' WHERE komplekt_id = ?", (kk["id"],))
            conn.execute("DELETE FROM klas_komplekty WHERE id = ?", (kk["id"],))

    # 5. нові наймолодші класи
    novi_ids = []
    if stvoryty_novi:
        for litera in o["litery_novyh"]:
            cur = conn.execute(
                "INSERT INTO klasy (paralel, litera, kilkist_uchniv, kilkist_chlopciv, kilkist_divchat, tip_klasu) "
                "VALUES (?, ?, 0, 0, 0, 'zvychajnyj')", (p_vid, litera))
            novi_ids.append(cur.lastrowid)

    # 6. міжпредметні групи - річ конкретного класу й конкретних записів плану
    conn.execute("UPDATE navchalnyj_plan SET mizhpredmetna_grupa_id = NULL")
    conn.execute("DELETE FROM mizhpredmetni_grupy")

    # 7. плани: клас дістає план торішньої паралелі, на яку перейшов; вчитель -
    #    свій, якщо вів цей предмет (крім переходу 4->5), інакше зі зразка
    perebudovano, bez_zrazka = 0, 0
    for k in conn.execute("SELECT id, paralel FROM klasy WHERE paralel BETWEEN ? AND ?", (p_vid, p_do)).fetchall():
        kid, np_ = k["id"], k["paralel"]
        rows_zrazka = zrazok.get(np_)
        if not rows_zrazka:
            bez_zrazka += 1
            continue                      # зразка нема - лишаємо власний план як є
        svoi = vlasnyi.get(kid, {})
        stara = stara_paralel.get(kid)
        cherez_mezhu_4_5 = stara is not None and stara <= 4 and np_ >= 5
        conn.execute("DELETE FROM navchalnyj_plan WHERE klas_id = ?", (kid,))
        for r in rows_zrazka:
            vlasnyi_r = None if cherez_mezhu_4_5 else svoi.get(r["predmet_id"])
            vchytel = vlasnyi_r["vchytel_id"] if vlasnyi_r else r["vchytel_id"]
            pg2 = vlasnyi_r["pidgrupa_2_vchytel_id"] if vlasnyi_r else r["pidgrupa_2_vchytel_id"]
            conn.execute(
                """
                INSERT INTO navchalnyj_plan
                    (klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary,
                     maks_urokiv_den, pidgrupa_2_vchytel_id, fiksovanyi_den, parnist, semestr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kid, r["predmet_id"], vchytel, r["godyn_tyzhden"], r["zdvoyeni_pary"],
                 r["maks_urokiv_den"], pg2, r["fiksovanyi_den"], r["parnist"], r["semestr"]),
            )
        perebudovano += 1

    # 8. річне
    for t in _NOVYI_RIK_RICHNI_TABLYTSI:
        conn.execute(f"DELETE FROM {t}")
    conn.execute(
        "UPDATE school_settings SET semestr1_pochatok = NULL, semestr1_kinets = NULL, "
        "semestr2_pochatok = NULL, semestr2_kinets = NULL, kanikuly1_e = 0, kanikuly1_pochatok = NULL, "
        "kanikuly1_kinets = NULL, kanikuly2_e = 0, kanikuly2_pochatok = NULL, kanikuly2_kinets = NULL "
        "WHERE id = 1")
    return {
        "vypusk": len(vypusk_ids), "perehid": len(o["perehid"]), "novi": len(novi_ids),
        "perebudovano": perebudovano, "bez_zrazka": bez_zrazka,
        "komplekty_rozpad": len(o["komplekty_rozpad"]),
    }


@app.route("/novyi-navchalnyi-rik", methods=["GET", "POST"])
def novyi_navchalnyi_rik():
    conn = get_connection()
    if request.method == "POST":
        if request.form.get("pidtverdzhennya", "").strip() != "НОВИЙ РІК":
            conn.close()
            flash('Щоб виконати перехід, введіть слова "НОВИЙ РІК" точно як показано - нічого не змінено.', "error")
            return redirect(url_for("novyi_navchalnyi_rik"))
        stvoryty_novi = request.form.get("stvoryty_novi") == "on"
        _stvoryty_backup_faylu("pered_novym_rokom")
        try:
            pidsumok = _novyi_rik_vykonaty(conn, stvoryty_novi)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        flash(
            f"Перехід виконано. Випущено класів: {pidsumok['vypusk']}, переведено: {pidsumok['perehid']}, "
            f"створено нових: {pidsumok['novi']}, планів перебудовано за торішніми зразками: "
            f"{pidsumok['perebudovano']}"
            + (f", без зразка (лишено як було): {pidsumok['bez_zrazka']}" if pidsumok["bez_zrazka"] else "")
            + (f", клас-комплектів розформовано: {pidsumok['komplekty_rozpad']}" if pidsumok["komplekty_rozpad"] else "")
            + ". Далі: дати семестрів у «Налаштуваннях школи», кількість учнів і кабінети нових класів, "
            "вчителі в планах - і генерація.",
            "success",
        )
        return redirect(url_for("klasy_list"))

    ohlyad = _novyi_rik_ohlyad(conn)
    # навчальний рік ще йде (між початком І і кінцем ІІ семестру) - найімовірніше,
    # сюди зайшли по розклад на ІІ семестр, а не по річний перехід
    settings = conn.execute(
        "SELECT semestr1_pochatok, semestr2_kinets FROM school_settings WHERE id = 1").fetchone()
    conn.close()
    sohodni = datetime.now().strftime("%Y-%m-%d")
    rik_tryvaie = None
    if settings and settings["semestr1_pochatok"] and settings["semestr2_kinets"]             and settings["semestr1_pochatok"] <= sohodni <= settings["semestr2_kinets"]:
        rik_tryvaie = settings["semestr2_kinets"]
    return render_template("novyi_rik.html", active="rezervni_kopiyi", o=ohlyad, rik_tryvaie=rik_tryvaie)


# ---------- Резервні копії ----------
# Ручні дії користувача над УСІМ файлом бази (на відміну від автоматичного бекапу
# db.py перед міграцією версії - той спрацьовує сам, без участі користувача).
# Іменування файлів навмисно те саме, що й у db.py._backup_pered_onovlennyam
# (school_v{версія}_{дата}.db) плюс префікс дії - щоб одразу було видно причину
# копії, переглядаючи папку base/ вручну.

TABLYTSI_DLYA_OCHYSHCHENNYA = [
    "pidsumky_semestru", "zakryti_semestry",
    "peredachi_znimky", "peredachi_hodyn", "zatverdzheni_hodyny",
    "ostannya_generatsiya", "generated_urok", "urok_log",
    "rozklad_varianty_uroky", "rozklad_varianty",
    "teacher_availability", "class_availability", "kabinet_availability",
    "asystenty_predmety", "asystenty",
    "mizhpredmetni_grupy", "navchalnyj_plan",
    "pozaurochnyj_plan", "pozaurochni_zanyattya",
    "kabinety_predmety", "kabinety_vchyteli", "predmet_paralel_pravyla",
    "klasy", "klas_komplekty",
    "predmety", "vchyteli", "kabinety",
]


def _stvoryty_backup_faylu(prefix):
    """Копіює поточну school.db у новий файл у тій самій папці з міткою часу -
    спільна функція для ручного бекапу і для обов'язкового бекапу ПЕРЕД
    відновленням/імпортом/очищенням. Повертає ім'я створеного файлу, або None,
    якщо school.db ще не існує (нема що копіювати)."""
    if not os.path.isfile(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    versiya_dlya_nazvy = APP_VERSION.replace(".", "_")
    filename = f"school_{prefix}_v{versiya_dlya_nazvy}_{timestamp}.db"
    shutil.copy2(DB_PATH, os.path.join(BACKUP_DIR, filename))
    return filename


def _bezpechnyi_backup_shlyah(filename):
    """Перевіряє, що filename - це реально наявний .db файл у BACKUP_DIR, і не
    сама робоча school.db (яку через ці маршрути видаляти/підміняти напряму не
    можна - лише через окремі операції відновлення/очищення)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.lower().endswith(".db") or filename in ("school.db", "school-demo.db"):
        return None
    shlyah = os.path.join(BACKUP_DIR, filename)
    return shlyah if os.path.isfile(shlyah) else None


@app.route("/rezervni-kopiyi")
def rezervni_kopiyi():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    kopiyi = []
    for f in os.listdir(BACKUP_DIR):
        if not f.lower().endswith(".db") or f in ("school.db", "school-demo.db"):
            continue
        full = os.path.join(BACKUP_DIR, f)
        mtime = os.path.getmtime(full)
        kopiyi.append({
            "filename": f,
            "date": datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S"),
            "rozmir_kb": round(os.path.getsize(full) / 1024, 1),
            "mtime": mtime,
        })
    kopiyi.sort(key=lambda x: x["mtime"], reverse=True)
    return render_template("rezervni_kopiyi.html", active="rezervni_kopiyi", kopiyi=kopiyi)


@app.route("/rezervni-kopiyi/stvoryty", methods=["POST"])
def rezervni_kopiyi_stvoryty():
    filename = _stvoryty_backup_faylu("ruchna_kopiya")
    if not filename:
        abort(404)
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)


@app.route("/rezervni-kopiyi/zavantazhyty/<path:filename>")
def rezervni_kopiyi_zavantazhyty(filename):
    if not _bezpechnyi_backup_shlyah(filename):
        abort(400)
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)


@app.route("/rezervni-kopiyi/vydalyty/<path:filename>", methods=["POST"])
def rezervni_kopiyi_vydalyty(filename):
    shlyah = _bezpechnyi_backup_shlyah(filename)
    if shlyah:
        os.remove(shlyah)
    return redirect(url_for("rezervni_kopiyi"))


_povidomlennya_pro_perezapusk = (
    "ОБОВ'ЯЗКОВО перезапустіть застосунок (закрийте вікно сервера й запустіть "
    "run.bat заново) - без перезапуску зміни в базі можуть не підхопитись коректно."
)


@app.route("/rezervni-kopiyi/vidnovyty/<path:filename>", methods=["POST"])
def rezervni_kopiyi_vidnovyty(filename):
    shlyah = _bezpechnyi_backup_shlyah(filename)
    if not shlyah:
        abort(400)
    _stvoryty_backup_faylu("pered_vidnovlennyam")
    shutil.copy2(shlyah, DB_PATH)
    flash(f'База відновлена з копії "{filename}". {_povidomlennya_pro_perezapusk}', "success")
    return redirect(url_for("rezervni_kopiyi"))


@app.route("/rezervni-kopiyi/importuvaty", methods=["POST"])
def rezervni_kopiyi_importuvaty():
    fajl = request.files.get("fajl")
    if not fajl or not fajl.filename:
        flash("Файл не вибрано.", "error")
        return redirect(url_for("rezervni_kopiyi"))

    os.makedirs(BACKUP_DIR, exist_ok=True)
    tymchasovyi_shlyah = os.path.join(BACKUP_DIR, "_importovanyi_tymchasovo.db")
    fajl.save(tymchasovyi_shlyah)

    ye_validnyi = True
    perevirka = sqlite3.connect(tymchasovyi_shlyah)
    try:
        perevirka.execute("SELECT version FROM schema_version LIMIT 1")
    except sqlite3.Error:
        ye_validnyi = False
    finally:
        # закрити з'єднання ОБОВ'ЯЗКОВО до os.remove() нижче - на Windows
        # видалити файл із відкритим SQLite-з'єднанням не можна (WinError 32)
        perevirka.close()

    if not ye_validnyi:
        os.remove(tymchasovyi_shlyah)
        flash(
            "Файл не схожий на резервну копію цього застосунку (немає таблиці "
            "schema_version) - імпорт скасовано, поточну базу НЕ зачеплено.",
            "error",
        )
        return redirect(url_for("rezervni_kopiyi"))

    _stvoryty_backup_faylu("pered_importom")
    shutil.move(tymchasovyi_shlyah, DB_PATH)
    flash(f"Базу імпортовано. {_povidomlennya_pro_perezapusk}", "success")
    return redirect(url_for("rezervni_kopiyi"))


@app.route("/rezervni-kopiyi/ochystyty", methods=["POST"])
def rezervni_kopiyi_ochystyty():
    if request.form.get("pidtverdzhennya", "").strip() != "ВИДАЛИТИ":
        flash('Щоб очистити базу, введіть слово "ВИДАЛИТИ" точно як показано - нічого не змінено.', "error")
        return redirect(url_for("rezervni_kopiyi"))

    _stvoryty_backup_faylu("pered_ochyshchennyam")

    conn = get_connection()
    for table in TABLYTSI_DLYA_OCHYSHCHENNYA:
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN ({})".format(
            ",".join("?" * len(TABLYTSI_DLYA_OCHYSHCHENNYA))
        ),
        TABLYTSI_DLYA_OCHYSHCHENNYA,
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    flash(
        "Базу очищено - лишились лише налаштування школи (тип закладу, дзвінки, "
        "навчальний рік). Перезапуск не потрібен.",
        "success",
    )
    return redirect(url_for("rezervni_kopiyi"))


def _zberegty_ostanniu_generatsiyu(result):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO ostannya_generatsiya
            (id, data_chas, success, status, message, vseogo_urokiv,
             kilkist_klasiv, kilkist_vchyteliv, kilkist_predmetiv, unplaced_json, chas_sekund)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            data_chas = excluded.data_chas, success = excluded.success, status = excluded.status,
            message = excluded.message, vseogo_urokiv = excluded.vseogo_urokiv,
            kilkist_klasiv = excluded.kilkist_klasiv, kilkist_vchyteliv = excluded.kilkist_vchyteliv,
            kilkist_predmetiv = excluded.kilkist_predmetiv, unplaced_json = excluded.unplaced_json,
            chas_sekund = excluded.chas_sekund
        """,
        (
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            1 if result.get("success") else 0,
            result.get("status"),
            result.get("message"),
            result.get("vseogo_urokiv"),
            result.get("kilkist_klasiv"),
            result.get("kilkist_vchyteliv"),
            result.get("kilkist_predmetiv"),
            json.dumps(result.get("unplaced") or [], ensure_ascii=False),
            result.get("chas_sekund"),
        ),
    )
    conn.commit()
    conn.close()


def _zavantazhyty_ostanniu_generatsiyu():
    conn = get_connection()
    row = conn.execute("SELECT * FROM ostannya_generatsiya WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return None
    return {
        "success": bool(row["success"]),
        "status": row["status"],
        "message": row["message"],
        "vseogo_urokiv": row["vseogo_urokiv"],
        "kilkist_klasiv": row["kilkist_klasiv"],
        "kilkist_vchyteliv": row["kilkist_vchyteliv"],
        "kilkist_predmetiv": row["kilkist_predmetiv"],
        "unplaced": json.loads(row["unplaced_json"]) if row["unplaced_json"] else [],
        "chas_sekund": row["chas_sekund"],
        "data_chas": row["data_chas"],
        "zberezheno": True,  # позначка, що це збережений (не щойно згенерований) результат
    }


@app.route("/vchyteli/<int:vchytel_id>/dostupnist", methods=["GET", "POST"])
def vchytel_dostupnist(vchytel_id):
    conn = get_connection()
    vchytel = conn.execute("SELECT * FROM vchyteli WHERE id = ?", (vchytel_id,)).fetchone()
    if vchytel is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        raw = request.form.get("data", "[]")
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = []

        conn.execute("DELETE FROM teacher_availability WHERE vchytel_id = ?", (vchytel_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO teacher_availability (vchytel_id, den, urok, status)
                VALUES (?, ?, ?, ?)
                """,
                (vchytel_id, item["den"], item["urok"], item["status"]),
            )
        conn.commit()
        conn.close()
        return redirect(url_for("vchyteli"))

    rows = conn.execute(
        "SELECT den, urok, status FROM teacher_availability WHERE vchytel_id = ?",
        (vchytel_id,),
    ).fetchall()
    conn.close()

    existing_data = [dict(r) for r in rows]
    return render_template(
        "dostupnist.html", active="vchyteli",
        entity_name=vchytel["pib"],
        back_url=url_for("vchyteli"), existing_data_json=json.dumps(existing_data),
        allow_nebazhanyi=True,
    )


@app.route("/klasy/<int:klas_id>/dostupnist", methods=["GET", "POST"])
def klas_dostupnist(klas_id):
    conn = get_connection()
    klas = conn.execute("SELECT * FROM klasy WHERE id = ?", (klas_id,)).fetchone()
    if klas is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        raw = request.form.get("data", "[]")
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = []

        conn.execute("DELETE FROM class_availability WHERE klas_id = ?", (klas_id,))
        for item in items:
            # єдиний стан - "недоступний": сам факт рядка і є недоступність
            conn.execute(
                "INSERT OR IGNORE INTO class_availability (klas_id, den, urok) VALUES (?, ?, ?)",
                (klas_id, item["den"], item["urok"]),
            )
        conn.commit()
        conn.close()
        return redirect(url_for("klasy_list"))

    rows = conn.execute(
        "SELECT den, urok FROM class_availability WHERE klas_id = ?",
        (klas_id,),
    ).fetchall()
    conn.close()

    existing_data = [{"den": r["den"], "urok": r["urok"], "status": "nedostupnyi"} for r in rows]
    nazva = klas_nazva(klas['paralel'], klas['litera'])
    return render_template(
        "dostupnist.html", active="klasy",
        entity_name=f"клас {nazva}",
        back_url=url_for("klasy_list"), existing_data_json=json.dumps(existing_data),
        allow_nebazhanyi=False,
    )



@app.route("/kabinety/<int:kabinet_id>/dostupnist", methods=["GET", "POST"])
def kabinet_dostupnist(kabinet_id):
    conn = get_connection()
    kabinet = conn.execute("SELECT * FROM kabinety WHERE id = ?", (kabinet_id,)).fetchone()
    if kabinet is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        raw = request.form.get("data", "[]")
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = []

        conn.execute("DELETE FROM kabinet_availability WHERE kabinet_id = ?", (kabinet_id,))
        for item in items:
            conn.execute(
                "INSERT OR IGNORE INTO kabinet_availability (kabinet_id, den, urok) VALUES (?, ?, ?)",
                (kabinet_id, item["den"], item["urok"]),
            )
        conn.commit()
        conn.close()
        return redirect(url_for("kabinety"))

    rows = conn.execute(
        "SELECT den, urok FROM kabinet_availability WHERE kabinet_id = ?",
        (kabinet_id,),
    ).fetchall()
    conn.close()

    existing_data = [{"den": r["den"], "urok": r["urok"], "status": "nedostupnyi"} for r in rows]
    return render_template(
        "dostupnist.html", active="kabinety",
        entity_name=f"кабінет {kabinet['nazva']}",
        back_url=url_for("kabinety"), existing_data_json=json.dumps(existing_data),
        allow_nebazhanyi=False,
    )



@app.route("/vchyteli/<int:vchytel_id>/matrytsya", methods=["GET", "POST"])
def vchytel_matrytsya(vchytel_id):
    conn = get_connection()
    vchytel = conn.execute("SELECT * FROM vchyteli WHERE id = ?", (vchytel_id,)).fetchone()
    if vchytel is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        all_predmety_ids = [p["id"] for p in conn.execute("SELECT id FROM predmety").fetchall()]
        all_klasy_ids = [k["id"] for k in conn.execute("SELECT id FROM klasy").fetchall()]
        konflikty = []  # (klas, predmet, inshyi_vchytel) - пропущено, бо вже веде хтось інший
        nevaliden = []  # клітинки з нечисловим/дробовим вводом - пропущено, НЕ трактовано як 0
        peredovantazheno = []  # клітинки, де залишок 0.5 довелось покласти на вже перевантажений тиждень

        for predmet_id in all_predmety_ids:
            for klas_id in all_klasy_ids:
                field = f"tyzhnevi_{predmet_id}_{klas_id}"
                raw = request.form.get(field, "").strip().replace(",", ".")
                if raw == "":
                    tsile, zalyshok = 0, False
                else:
                    try:
                        znachennya = float(raw)
                        pivodynytsi = round(znachennya * 2)
                        if znachennya < 0 or abs(znachennya * 2 - pivodynytsi) > 1e-6:
                            raise ValueError
                        tsile, zalyshok = pivodynytsi // 2, bool(pivodynytsi % 2)
                    except ValueError:
                        # єдиний дробовий крок, який ця клітинка розуміє - рівно
                        # 0.5 (залишок в один урок раз на два тижні). Будь-що інше
                        # (нечислове, або крок не 0.5) - НЕ трактувати як 0, це
                        # раніше мовчки ВИДАЛЯЛО наявний запис. Пропускаємо клітинку.
                        k = conn.execute("SELECT paralel, litera FROM klasy WHERE id=?", (klas_id,)).fetchone()
                        p = conn.execute("SELECT nazva FROM predmety WHERE id=?", (predmet_id,)).fetchone()
                        nevaliden.append(f"{klas_nazva(k['paralel'], k['litera'])} / {p['nazva']} - '{raw}'")
                        continue

                # предмет у цьому класі розписано по семестрах (v0.2.0) - матриця
                # таке не редагує (у клітинці лише текст і посилання на форму плану),
                # і не має права завести поруч рядок "весь рік" (змішування)
                if conn.execute(
                    "SELECT 1 FROM navchalnyj_plan WHERE klas_id = ? AND predmet_id = ? AND semestr > 0 LIMIT 1",
                    (klas_id, predmet_id),
                ).fetchone():
                    continue

                # ВАЖЛИВО: перевіряємо за (klas_id, predmet_id) БЕЗ вчителя - саме
                # так влаштоване UNIQUE обмеження в базі (один предмет у класі -
                # рівно один вчитель, хто б не заповнював цю матрицю). Раніше
                # перевірка додатково фільтрувала за vchytel_id, тому якщо предмет
                # уже вів ІНШИЙ вчитель, код вважав "запису не існує" і намагався
                # INSERT - падав на UNIQUE constraint.
                existing_oboydva = conn.execute(
                    "SELECT navchalnyj_plan.*, v.pib AS vchytel_pib FROM navchalnyj_plan "
                    "JOIN vchyteli v ON v.id = navchalnyj_plan.vchytel_id "
                    "WHERE klas_id = ? AND predmet_id = ? AND navchalnyj_plan.parnist = 'oboydva' "
                    "AND navchalnyj_plan.semestr = 0",
                    (klas_id, predmet_id),
                ).fetchone()
                existing_parity_all = conn.execute(
                    "SELECT navchalnyj_plan.*, v.pib AS vchytel_pib FROM navchalnyj_plan "
                    "JOIN vchyteli v ON v.id = navchalnyj_plan.vchytel_id "
                    "WHERE klas_id = ? AND predmet_id = ? AND navchalnyj_plan.parnist != 'oboydva' "
                    "AND navchalnyj_plan.semestr = 0",
                    (klas_id, predmet_id),
                ).fetchall()

                if (tsile > 0 or zalyshok) and (
                    (existing_oboydva and existing_oboydva["vchytel_id"] != vchytel_id)
                    or any(r["vchytel_id"] != vchytel_id for r in existing_parity_all)
                ):
                    # цей предмет у цьому класі вже веде ІНШИЙ вчитель - не
                    # перезаписуємо мовчки (це відібрало б клас у колеги без
                    # його відома) - пропускаємо, повідомляємо після збереження
                    k = conn.execute("SELECT paralel, litera FROM klasy WHERE id=?", (klas_id,)).fetchone()
                    p = conn.execute("SELECT nazva FROM predmety WHERE id=?", (predmet_id,)).fetchone()
                    inshyi = (existing_oboydva["vchytel_pib"] if existing_oboydva and existing_oboydva["vchytel_id"] != vchytel_id
                              else next(r["vchytel_pib"] for r in existing_parity_all if r["vchytel_id"] != vchytel_id))
                    konflikty.append(f"{klas_nazva(k['paralel'], k['litera'])} / {p['nazva']} - вже веде {inshyi}")
                    continue

                # ціла частина (Щотижня) - як і раніше, лише свій запис не чіпаємо
                if tsile > 0:
                    if existing_oboydva:
                        conn.execute(
                            "UPDATE navchalnyj_plan SET godyn_tyzhden = ? WHERE id = ?",
                            (tsile, existing_oboydva["id"]),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO navchalnyj_plan
                                (klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary, maks_urokiv_den, parnist)
                            VALUES (?, ?, ?, ?, 0, 1, 'oboydva')
                            """,
                            (klas_id, predmet_id, vchytel_id, tsile),
                        )
                elif existing_oboydva and existing_oboydva["vchytel_id"] == vchytel_id:
                    conn.execute("DELETE FROM navchalnyj_plan WHERE id = ?", (existing_oboydva["id"],))

                # залишок 0.5 (парний/непарний, рівно 1 урок раз на два тижні) -
                # парність обирається автоматично за балансом складності класу
                # (_obraty_parnist_dlya_ostatku), користувач тут її не вказує.
                # ВАЖЛИВО: форма пересилає ВСІ клітинки матриці одночасно, навіть
                # ті, які користувач не чіпав - якщо тут уже є коректний стандартний
                # залишок (рівно один рядок, 1 год), лишаємо його як є й НЕ
                # перевибираємо парність, інакше збереження однієї клітинки могло б
                # непомітно "перекинути" вже зафіксовану парність інших предметів.
                svoyi_parity = [r for r in existing_parity_all if r["vchytel_id"] == vchytel_id]
                if zalyshok:
                    vzhe_ye_standartnyi = len(svoyi_parity) == 1 and svoyi_parity[0]["godyn_tyzhden"] == 1
                    if not vzhe_ye_standartnyi:
                        obraniy, chy_peredovantazhuje = _obraty_parnist_dlya_ostatku(conn, klas_id, predmet_id)
                        if chy_peredovantazhuje:
                            k = conn.execute("SELECT paralel, litera FROM klasy WHERE id=?", (klas_id,)).fetchone()
                            p = conn.execute("SELECT nazva FROM predmety WHERE id=?", (predmet_id,)).fetchone()
                            peredovantazheno.append(f"{klas_nazva(k['paralel'], k['litera'])} / {p['nazva']}")
                        for r in svoyi_parity:
                            conn.execute("DELETE FROM navchalnyj_plan WHERE id = ?", (r["id"],))
                        conn.execute(
                            """
                            INSERT INTO navchalnyj_plan
                                (klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary, maks_urokiv_den, parnist)
                            VALUES (?, ?, ?, 1, 0, 1, ?)
                            """,
                            (klas_id, predmet_id, vchytel_id, obraniy),
                        )
                else:
                    for r in svoyi_parity:
                        conn.execute("DELETE FROM navchalnyj_plan WHERE id = ?", (r["id"],))

        conn.commit()
        conn.close()
        if konflikty:
            flash(
                "Збережено, але деякі клітинки НЕ застосовано (предмет уже веде інший "
                "вчитель, не перезаписано автоматично): " + "; ".join(konflikty),
                "error",
            )
        if nevaliden:
            flash(
                "Ця матриця приймає цілі числа або дробові з кроком 0.5 (напр. 3.5) - "
                "клітинки з іншим значенням пропущено, нічого в них не змінено: " + "; ".join(nevaliden),
                "error",
            )
        if peredovantazheno:
            flash(
                "Збережено, але для деяких предметів ОБИДВА тижні (парний і непарний) уже "
                "перевищують граничну норму класу незалежно від вибору - перевірте вручну "
                'в "Навантаженні по класах": ' + "; ".join(peredovantazheno),
                "error",
            )
        return redirect(url_for("vchyteli"))

    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()

    existing_rows = conn.execute(
        "SELECT klas_id, predmet_id, godyn_tyzhden FROM navchalnyj_plan "
        "WHERE vchytel_id = ? AND parnist = 'oboydva' AND semestr = 0",
        (vchytel_id,),
    ).fetchall()
    # дробові (parnyi/neparnyi) записи цього вчителя - матриця їх не редагує (лише
    # цілі щотижневі числа), але раніше вони були зовсім невидимі: клітинка
    # показувала порожньо/0, навіть якщо предмет насправді ведеться через тиждень.
    # Тепер показуємо як посилання на детальну форму замість тихого "нема даних".
    drobovi_rows = conn.execute(
        "SELECT klas_id, predmet_id, parnist, godyn_tyzhden FROM navchalnyj_plan "
        "WHERE vchytel_id = ? AND parnist != 'oboydva' AND semestr = 0",
        (vchytel_id,),
    ).fetchall()
    # семестрові рядки цього вчителя (v0.2.0) - матриця їх не редагує, показує
    # текстом "І сем. 5 · ІІ сем. 7" з посиланням на форму плану
    semestrovi_rows = conn.execute(
        "SELECT klas_id, predmet_id, semestr, parnist, godyn_tyzhden FROM navchalnyj_plan "
        "WHERE vchytel_id = ? AND semestr > 0",
        (vchytel_id,),
    ).fetchall()
    # рядки, де цей вчитель - вчитель ДРУГОЇ підгрупи поділу (окрема особа, не
    # "підгрупи одного вчителя"). У матриці НЕ редагуються (форма пише vchytel_id
    # основного вчителя), тому показуємо їх окремим read-only списком - інакше
    # 2-й вчитель поділу взагалі не бачить свого навантаження тут.
    pidgrupa2_rows = conn.execute(
        """
        SELECT k.paralel, k.litera, p.nazva AS predmet_nazva,
               np.godyn_tyzhden, np.parnist, np.semestr, v1.pib AS vchytel1_pib
        FROM navchalnyj_plan np
        JOIN klasy k ON k.id = np.klas_id
        JOIN predmety p ON p.id = np.predmet_id
        JOIN vchyteli v1 ON v1.id = np.vchytel_id
        WHERE np.pidgrupa_2_vchytel_id = ?
        ORDER BY k.paralel, k.litera COLLATE UKR, p.nazva COLLATE UKR
        """,
        (vchytel_id,),
    ).fetchall()
    # тижні семестрів - для середнього за рік у рядку суми (як у Тарифікації)
    t1, t2 = _tyzhni_semestriv(conn)
    conn.close()

    def _serednye_za_rik(hodyny, semestr):
        if not semestr:
            return hodyny
        return hodyny * (t1 if semestr == 1 else t2) / ((t1 + t2) or 1)

    pidgrupa2_razom = sum(
        _serednye_za_rik(r["godyn_tyzhden"] * (1 if r["parnist"] == "oboydva" else 0.5), r["semestr"])
        for r in pidgrupa2_rows
    )

    existing_map = {
        (r["klas_id"], r["predmet_id"]): r["godyn_tyzhden"] for r in existing_rows
    }
    drobovi_za_grupamy = {}
    for r in drobovi_rows:
        drobovi_za_grupamy.setdefault((r["klas_id"], r["predmet_id"]), []).append(r)

    # "стандартний" залишок (рівно один рядок parnyi/neparnyi, годин=1) тепер
    # показуємо як ОДНЕ десяткове число прямо в клітинці матриці - можна й
    # редагувати напряму (напр. ввести "3.5"). "Нестандартні" (два рядки, чи
    # годин != 1) в одну клітинку не влазять - лишаються посиланням нижче.
    drobovi_map = {}
    for key, legy in drobovi_za_grupamy.items():
        if len(legy) == 1 and legy[0]["godyn_tyzhden"] == 1:
            existing_map[key] = existing_map.get(key, 0) + 0.5
        else:
            drobovi_map[key] = {"parnyi": 0, "neparnyi": 0}
            for r in legy:
                drobovi_map[key][r["parnist"]] = r["godyn_tyzhden"]

    semestrovi_hodyny = {}   # (klas_id, predmet_id) -> {semestr: середнє год/тиждень}
    for r in semestrovi_rows:
        h = semestrovi_hodyny.setdefault((r["klas_id"], r["predmet_id"]), {})
        h[r["semestr"]] = h.get(r["semestr"], 0) + r["godyn_tyzhden"] * (1 if r["parnist"] == "oboydva" else 0.5)
    semestrovi_map = {
        klyuch: {"opys": " · ".join(f"{SEMESTR_KOROTKO[sem]} {hod:g}" for sem, hod in sorted(h.items())),
                 "semestr": min(h),
                 "serednye": sum(_serednye_za_rik(hod, sem) for sem, hod in h.items())}
        for klyuch, h in semestrovi_hodyny.items()
    }

    predmety_z_danymy_ids = ({r["predmet_id"] for r in existing_rows} | {k[1] for k in drobovi_za_grupamy}
                             | {k[1] for k in semestrovi_map})
    predmety_z_danymy = [p for p in all_predmety if p["id"] in predmety_z_danymy_ids]

    all_klasy_json = json.dumps([
        {"id": k["id"], "nazva": f"{k['paralel']}{'-' + k['litera'] if k['litera'] else ''}"}
        for k in all_klasy
    ])

    return render_template(
        "vchytel_matrytsya.html", active="vchyteli",
        vchytel=vchytel, all_predmety=all_predmety, all_klasy=all_klasy,
        predmety_z_danymy=predmety_z_danymy, all_klasy_json=all_klasy_json,
        existing_map=existing_map, drobovi_map=drobovi_map, semestrovi_map=semestrovi_map,
        pidgrupa2_rows=pidgrupa2_rows, pidgrupa2_razom=pidgrupa2_razom,
    )



def _overlap_days(d1, d2, k_start, k_end):
    """Кількість днів перетину періоду [d1,d2] з канікулами [k_start,k_end], включно."""
    lo = max(d1, k_start)
    hi = min(d2, k_end)
    if hi < lo:
        return 0
    return (hi - lo).days + 1


def _parse_date_safe(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _kanikuly_periody(settings):
    """Повертає список (початок, кінець) канікул, що дійсно увімкнені й коректні."""
    periody = []
    for e_key, poch_key, kin_key in [
        ("kanikuly1_e", "kanikuly1_pochatok", "kanikuly1_kinets"),
        ("kanikuly2_e", "kanikuly2_pochatok", "kanikuly2_kinets"),
    ]:
        if not settings or not settings[e_key]:
            continue
        k1 = _parse_date_safe(settings[poch_key])
        k2 = _parse_date_safe(settings[kin_key])
        if k1 and k2 and k2 >= k1:
            periody.append((k1, k2))
    return periody


def _calc_navchalni_tyzhni(s1_poch, s1_kin, s2_poch, s2_kin, kanikuly_periody=None):
    """Рахує кількість навчальних тижнів як суму тривалості двох семестрів, мінус дні
    канікул усередині них. Якщо дати не задані чи некоректні - повертає дефолт 35."""
    kanikuly_periody = kanikuly_periody or []
    total_days = 0
    valid = False
    for poch, kin in [(s1_poch, s1_kin), (s2_poch, s2_kin)]:
        if poch and kin:
            try:
                d1 = datetime.strptime(poch, "%Y-%m-%d")
                d2 = datetime.strptime(kin, "%Y-%m-%d")
                if d2 >= d1:
                    semestr_days = (d2 - d1).days + 1
                    for k1, k2 in kanikuly_periody:
                        semestr_days -= _overlap_days(d1, d2, k1, k2)
                    total_days += max(0, semestr_days)
                    valid = True
            except ValueError:
                pass
    if not valid or total_days <= 0:
        return 35
    return max(1, round(total_days / 7))


def _calc_tyzhniv_do_daty(na_datu, s1_poch, s1_kin, s2_poch, s2_kin, kanikuly_periody=None):
    """Рахує кількість навчальних тижнів, що минули від початку навчального року
    до вказаної дати (включно), з урахуванням меж обох семестрів і канікул усередині них."""
    kanikuly_periody = kanikuly_periody or []
    try:
        d_target = datetime.strptime(na_datu, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0

    total_days = 0
    for poch, kin in [(s1_poch, s1_kin), (s2_poch, s2_kin)]:
        if not (poch and kin):
            continue
        try:
            d1 = datetime.strptime(poch, "%Y-%m-%d")
            d2 = datetime.strptime(kin, "%Y-%m-%d")
        except ValueError:
            continue
        if d_target < d1:
            continue
        d_end = min(d_target, d2)
        if d_end >= d1:
            semestr_days = (d_end - d1).days + 1
            for k1, k2 in kanikuly_periody:
                semestr_days -= _overlap_days(d1, d_end, k1, k2)
            total_days += max(0, semestr_days)

    return max(0, round(total_days / 7))


def _tyzhni_po_parnosti(na_datu, s1_poch, s1_kin, s2_poch, s2_kin, kanikuly_periody, pershyi_tyzhden_neparnyi):
    """Рахує, скільки НАВЧАЛЬНИХ тижнів кожної парності (парний/непарний) минуло
    від початку навчального року (s1_poch = тиждень 1) до na_datu включно.

    На відміну від _calc_tyzhniv_do_daty (що просто ділить дні на 7), тут РЕАЛЬНО
    проходимо тижні один за одним - інакше немає джерела правди "тиждень із датою
    Х - парний чи непарний". Парність рахується за календарем від s1_poch і НЕ
    призупиняється на канікулах (рішення закладу - парність суцільна). Тиждень
    рахується "навчальним" (і йде в підсумок), якщо хоч один його день припадає на
    семестр і не є канікулярним."""
    kanikuly_periody = kanikuly_periody or []
    try:
        d_target = datetime.strptime(na_datu, "%Y-%m-%d")
        d_start = datetime.strptime(s1_poch, "%Y-%m-%d")
    except (ValueError, TypeError):
        return {"neparnyi": 0, "parnyi": 0}
    if d_target < d_start:
        return {"neparnyi": 0, "parnyi": 0}

    semestry = []
    for poch, kin in [(s1_poch, s1_kin), (s2_poch, s2_kin)]:
        if not (poch and kin):
            continue
        try:
            semestry.append((datetime.strptime(poch, "%Y-%m-%d"), datetime.strptime(kin, "%Y-%m-%d")))
        except ValueError:
            pass

    tyzhni = {"neparnyi": 0, "parnyi": 0}
    tyzhden_pochatok = d_start
    idx = 0
    while tyzhden_pochatok <= d_target:
        tyzhden_kinets = min(tyzhden_pochatok + timedelta(days=6), d_target)
        je_navchalnyi = False
        for s_poch, s_kin in semestry:
            overlap_pochatok = max(tyzhden_pochatok, s_poch)
            overlap_kinets = min(tyzhden_kinets, s_kin)
            if overlap_pochatok > overlap_kinets:
                continue
            dni_v_peretyni = (overlap_kinets - overlap_pochatok).days + 1
            for k1, k2 in kanikuly_periody:
                dni_v_peretyni -= _overlap_days(overlap_pochatok, overlap_kinets, k1, k2)
            if dni_v_peretyni > 0:
                je_navchalnyi = True
                break
        if je_navchalnyi:
            neparnyi_indeks = (idx % 2 == 0) == bool(pershyi_tyzhden_neparnyi)
            tyzhni["neparnyi" if neparnyi_indeks else "parnyi"] += 1
        tyzhden_pochatok += timedelta(days=7)
        idx += 1

    return tyzhni


def _parnist_na_datu(na_datu, s1_poch, pershyi_tyzhden_neparnyi):
    """Парність (parnyi/neparnyi) тижня, що містить дату na_datu, за тим самим
    численням від s1_poch, що й _tyzhni_po_parnosti (тиждень 1 = тиждень з
    semestr1_pochatok). На відміну від _tyzhni_po_parnosti (кумулятивний підрахунок
    для звітів), тут повертається парність ОДНОГО конкретного тижня - для дефолту
    перемикача тижня в переглядах розкладу."""
    try:
        d_target = datetime.strptime(na_datu, "%Y-%m-%d") if isinstance(na_datu, str) else na_datu
        d_start = datetime.strptime(s1_poch, "%Y-%m-%d")
    except (ValueError, TypeError):
        return "parnyi"
    idx = max(0, (d_target - d_start).days) // 7
    neparnyi = (idx % 2 == 0) == bool(pershyi_tyzhden_neparnyi)
    return "neparnyi" if neparnyi else "parnyi"


def _potochna_parnist(conn):
    """Парність поточного тижня (за сьогоднішньою датою) - дефолт для перемикача
    тижня в переглядах розкладу, якщо користувач явно не обрав через ?parnist=."""
    settings = conn.execute("SELECT semestr1_pochatok, pershyi_tyzhden_neparnyi FROM school_settings WHERE id = 1").fetchone()
    if not settings or not settings["semestr1_pochatok"]:
        return "parnyi"
    return _parnist_na_datu(datetime.now(), settings["semestr1_pochatok"], settings["pershyi_tyzhden_neparnyi"])


def _ye_parnisni_zapysy(conn):
    """Чи є в навчальному плані хоч один дробовий запис (parnyi/neparnyi) - якщо
    немає жодного, школа не використовує парність тижнів узагалі, і перемикач
    тижня/теги парності в UI лише плутали б завуча (показ, PDF-кнопка тощо)."""
    return conn.execute(
        "SELECT COUNT(*) c FROM navchalnyj_plan WHERE parnist != 'oboydva'"
    ).fetchone()["c"] > 0


# типи ненавчальних днів: код -> (підпис, CSS-клас). Додати новий тип = додати
# рядок сюди й колір у style.css; решта коду типів не знає поіменно.
NENAVCHALNI_TYPY = ("svyato", "karantyn", "robochyi")
DNI_SKOROCHENO = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб"}


def _nenavchalni_dni_mapa(conn):
    """{'РРРР-ММ-ДД': 'svyato' | 'karantyn' | 'robochyi:N'} - дні, позначені завучем.

    Для відпрацювання (typ='robochyi') до коду додається номер дня тижня, за
    розкладом якого школа працює: 'robochyi:1' = субота за розкладом понеділка."""
    mapa = {}
    for r in conn.execute("SELECT data, typ, za_den FROM nenavchalni_dni"):
        if r["typ"] == "robochyi":
            mapa[r["data"]] = f"robochyi:{r['za_den'] or 1}"
        else:
            mapa[r["data"]] = r["typ"]
    return mapa


def _misyatsi_navchalnogo_roku(settings):
    """Сітка календаря, згрупована по календарних роках:
    [{"rik": 2026, "misyatsi": [{"nazva", "tyzhni", "rik"}, ...]}, ...].
    Тижні - списки по 7 днів (пн-нд); клітинка None = дня в календарі немає.

    Календар ОБРІЗАНИЙ по семестрах: дні до початку 1-го, після кінця 2-го і в
    проміжку між семестрами не показуються взагалі - їх у навчальному році немає,
    показувати й розфарбовувати їх нема сенсу.

    Повертає [] якщо дати семестрів ще не заповнені: без них немає що малювати."""
    def _d(x):
        return x.date() if hasattr(x, "date") else x

    s1 = _d(_parse_date_safe(settings["semestr1_pochatok"] if settings else None))
    s2 = _d(_parse_date_safe(settings["semestr2_kinets"] if settings else None))
    if not s1 or not s2 or s2 < s1:
        return []
    s1_kin = _d(_parse_date_safe(settings["semestr1_kinets"] if settings else None))
    s2_poch = _d(_parse_date_safe(settings["semestr2_pochatok"] if settings else None))

    def u_semestri(d):
        if s1_kin and s2_poch:
            return (s1 <= d <= s1_kin) or (s2_poch <= d <= s2)
        return s1 <= d <= s2

    MISYATSI = ["січень", "лютий", "березень", "квітень", "травень", "червень",
                "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
    misyatsi = []
    rik, mis = s1.year, s1.month
    while (rik, mis) <= (s2.year, s2.month):
        pershe = date(rik, mis, 1)
        dniv_u_misyatsi = calendar.monthrange(rik, mis)[1]
        tyzhni, tyzhden = [], [None] * pershe.weekday()   # weekday(): пн=0
        ye_dni = False
        for d in range(1, dniv_u_misyatsi + 1):
            den = date(rik, mis, d)
            if u_semestri(den):
                tyzhden.append(den)
                ye_dni = True
            else:
                tyzhden.append(None)      # день поза семестрами - порожня клітинка
            if len(tyzhden) == 7:
                tyzhni.append(tyzhden)
                tyzhden = []
        if tyzhden:
            tyzhni.append(tyzhden + [None] * (7 - len(tyzhden)))
        if ye_dni:                        # місяць цілком поза семестрами не показуємо
            # прибираємо тижні, що вийшли зовсім порожні (напр. перші дні січня)
            tyzhni = [t for t in tyzhni if any(t)]
            misyatsi.append({"nazva": MISYATSI[mis - 1], "tyzhni": tyzhni, "rik": rik})
        rik, mis = (rik + 1, 1) if mis == 12 else (rik, mis + 1)

    # Групуємо по КАЛЕНДАРНИХ РОКАХ: кожен рік - окремий блок із підзаголовком.
    # Так перелом року видно однозначно, а січень завжди починає новий блок,
    # незалежно від ширини екрана (при простому переносі рядка грудень міг
    # лишатись самотнім у рядку).
    roky = []
    for m in misyatsi:
        if not roky or roky[-1]["rik"] != m["rik"]:
            roky.append({"rik": m["rik"], "misyatsi": []})
        roky[-1]["misyatsi"].append(m)
    return roky


@app.route("/nenavchalni-dni", methods=["GET", "POST"])
def nenavchalni_dni():
    """Календар навчального року: завуч клікає по днях, позначаючи ненавчальні -
    святкові або карантинні. Уроки, що припали на них, у "Залишках годин"
    рахуються як борг учителя."""
    conn = get_connection()

    if request.method == "POST":
        try:
            mapa = json.loads(request.form.get("data", "{}"))
        except (ValueError, TypeError):
            mapa = {}
        conn.execute("DELETE FROM nenavchalni_dni")
        if isinstance(mapa, dict):
            for d, kod in mapa.items():
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)):
                    continue
                typ, _, za_den_str = str(kod).partition(":")
                if typ not in NENAVCHALNI_TYPY:
                    continue
                za_den = None
                if typ == "robochyi":
                    if not za_den_str.isdigit() or not (1 <= int(za_den_str) <= 6):
                        continue
                    za_den = int(za_den_str)
                conn.execute(
                    "INSERT OR IGNORE INTO nenavchalni_dni (data, typ, za_den) VALUES (?, ?, ?)",
                    (d, typ, za_den),
                )
        conn.commit()
        kilkist = conn.execute("SELECT COUNT(*) c FROM nenavchalni_dni").fetchone()["c"]
        conn.close()
        flash(f"Збережено позначених днів: {kilkist}.", "success")
        return redirect(url_for("nenavchalni_dni"))

    settings = conn.execute("SELECT * FROM school_settings WHERE id = 1").fetchone()
    roky = _misyatsi_navchalnogo_roku(settings)
    obrani = _nenavchalni_dni_mapa(conn)
    dniv_na_tyzhden = settings["dniv_na_tyzhden"] if settings else 5

    # Кожен день календаря дістає СВІЙ тип - завуч одразу бачить, де канікули,
    # де вихідні, а де день просто поза семестрами. Клікати можна лише навчальні:
    # позначати святом вихідний чи канікули безглуздо, вони й так не рахуються.
    # _parse_date_safe / _kanikuly_periody віддають datetime, а клітинки календаря -
    # date. Зводимо все до date, інакше порівняння падає з TypeError.
    def _d(x):
        return x.date() if hasattr(x, "date") else x

    s1_poch = _d(_parse_date_safe(settings["semestr1_pochatok"] if settings else None))
    s1_kin = _d(_parse_date_safe(settings["semestr1_kinets"] if settings else None))
    s2_poch = _d(_parse_date_safe(settings["semestr2_pochatok"] if settings else None))
    s2_kin = _d(_parse_date_safe(settings["semestr2_kinets"] if settings else None))
    kanikuly = [(_d(k1), _d(k2)) for (k1, k2) in _kanikuly_periody(settings)]

    def typ_dnya(d):
        # дні поза семестрами в календар уже не потрапляють (обрізано вище),
        # тож лишається три природні стани
        if any(k1 <= d <= k2 for (k1, k2) in kanikuly):
            return "kanikuly"
        if d.weekday() + 1 > dniv_na_tyzhden:
            return "vyhidnyi"
        return "navchalnyi"

    typy = {d.strftime("%Y-%m-%d"): typ_dnya(d)
            for r in roky for m in r["misyatsi"] for t in m["tyzhni"] for d in t if d}
    conn.close()

    return render_template(
        "nenavchalni_dni.html", active="nalashtuvannya",
        roky=roky, obrani_json=json.dumps(obrani),
        typy_json=json.dumps(typy),
        kilkist_svyat=sum(1 for t in obrani.values() if t == "svyato"),
        kilkist_karantyn=sum(1 for t in obrani.values() if t == "karantyn"),
        kilkist_robochyh=sum(1 for t in obrani.values() if t.startswith("robochyi")),
        dni_skorocheno=DNI_SKOROCHENO,
        navchalnyh_dniv=sum(1 for t in typy.values() if t == "navchalnyi"),
        daty_zadani=bool(roky),
    )


@app.route("/nalashtuvannya", methods=["GET", "POST"])
def nalashtuvannya():
    conn = get_connection()

    if request.method == "POST":
        dniv = int(request.form.get("dniv_na_tyzhden") or 5)
        urokiv = int(request.form.get("urokiv_na_den") or 8)
        mova = request.form.get("mova_navchannya", "").strip() or "Українська"
        # Друга зміна заблокована: солвер про неї не знає (generator.py не читає
        # school_settings.druga_zmina), тож увімкнений прапорець лише додавав би
        # порожню таблицю дзвінків і хибне відчуття, що розклад її враховує.
        # Колонка в БД лишається - повернути прапорець буде однією правкою.
        druga_zmina = 0
        tip_miscevosti = request.form.get("tip_miscevosti", "miska")
        # Ліцей (10-12) заблоковано: профільні групи старшої школи ще не
        # підтримуються генератором (див. PROJECT_OVERVIEW, відкрита задача 1). Чекбокс
        # у формі disabled, але сюди прапорець міг прийти зі старої вкладки або
        # ручного POST - примусово знімаємо й попереджаємо.
        lic_prosyly = request.form.get("tz_liczei") == "on"
        tip_zakladu, tz_fallback = _tip_zakladu_z_checkboxiv(
            request.form.get("tz_pochatkova") == "on",
            request.form.get("tz_gimnaziya") == "on",
            False,
            request.form.get("tz_stara") == "on",
        )
        if lic_prosyly:
            flash(
                'Рівень "Ліцей (10-12)" поки не підтримується — прапорець '
                "проігноровано.",
                "error",
            )
        if tz_fallback:
            flash(
                'Не позначено жодного рівня закладу - застосовано типове значення '
                '"1-11 (стара школа)".',
                "error",
            )

        pershyi_tyzhden_neparnyi = 1 if request.form.get("pershyi_tyzhden_neparnyi") == "on" else 0
        vikna_vchyteliv = 1 if request.form.get("vikna_vchyteliv") == "on" else 0

        s1_poch = request.form.get("semestr1_pochatok", "").strip() or None
        s1_kin = request.form.get("semestr1_kinets", "").strip() or None
        s2_poch = request.form.get("semestr2_pochatok", "").strip() or None
        s2_kin = request.form.get("semestr2_kinets", "").strip() or None

        kanikuly1_e = 1 if request.form.get("kanikuly1_e") == "on" else 0
        kanikuly1_poch = request.form.get("kanikuly1_pochatok", "").strip() or None
        kanikuly1_kin = request.form.get("kanikuly1_kinets", "").strip() or None
        kanikuly2_e = 1 if request.form.get("kanikuly2_e") == "on" else 0
        kanikuly2_poch = request.form.get("kanikuly2_pochatok", "").strip() or None
        kanikuly2_kin = request.form.get("kanikuly2_kinets", "").strip() or None

        kanikuly_periody = []
        for e, poch, kin in [
            (kanikuly1_e, kanikuly1_poch, kanikuly1_kin),
            (kanikuly2_e, kanikuly2_poch, kanikuly2_kin),
        ]:
            if e:
                k1 = _parse_date_safe(poch)
                k2 = _parse_date_safe(kin)
                if k1 and k2 and k2 >= k1:
                    kanikuly_periody.append((k1, k2))

        tyzhniv = _calc_navchalni_tyzhni(s1_poch, s1_kin, s2_poch, s2_kin, kanikuly_periody=kanikuly_periody)

        tip_zakladu_buv = _potochnyi_tip_zakladu(conn)

        conn.execute(
            """
            UPDATE school_settings
            SET dniv_na_tyzhden = ?, urokiv_na_den = ?, mova_navchannya = ?, druga_zmina = ?,
                tip_miscevosti = ?, tip_zakladu = ?,
                semestr1_pochatok = ?, semestr1_kinets = ?, semestr2_pochatok = ?, semestr2_kinets = ?,
                kanikuly1_e = ?, kanikuly1_pochatok = ?, kanikuly1_kinets = ?,
                kanikuly2_e = ?, kanikuly2_pochatok = ?, kanikuly2_kinets = ?,
                kilkist_navchalnyh_tyzhniv = ?, pershyi_tyzhden_neparnyi = ?, vikna_vchyteliv = ?
            WHERE id = 1
            """,
            (dniv, urokiv, mova, druga_zmina, tip_miscevosti, tip_zakladu, s1_poch, s1_kin, s2_poch, s2_kin,
             kanikuly1_e, kanikuly1_poch, kanikuly1_kin, kanikuly2_e, kanikuly2_poch, kanikuly2_kin,
             tyzhniv, pershyi_tyzhden_neparnyi, vikna_vchyteliv),
        )

        zminy = [1, 2] if druga_zmina else [1]
        for zmina in zminy:
            for urok in range(1, urokiv + 1):
                pochatok = request.form.get(f"pochatok_{zmina}_{urok}", "").strip() or None
                kinets = request.form.get(f"kinets_{zmina}_{urok}", "").strip() or None
                conn.execute(
                    """
                    INSERT INTO dzvinky (zmina, urok, pochatok, kinets)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(zmina, urok) DO UPDATE SET pochatok = excluded.pochatok, kinets = excluded.kinets
                    """,
                    (zmina, urok, pochatok, kinets),
                )
        # прибираємо зайві рядки дзвінків, якщо кількість уроків зменшили
        conn.execute("DELETE FROM dzvinky WHERE urok > ?", (urokiv,))
        if not druga_zmina:
            conn.execute("DELETE FROM dzvinky WHERE zmina = 2")

        # У ДЕМО-режимі зміна типу закладу перебудовує демо-дані під новий
        # діапазон паралелей (реальну базу це не чіпає - запобіжник усередині).
        if tip_zakladu != tip_zakladu_buv:
            p_vid, p_do = diapazon_paraleley_zakladu(tip_zakladu)
            pidsumok = _pidlashtuvaty_demo_pid_tip(conn, p_vid, p_do)
            if pidsumok:
                nazva_tipu = TIP_ZAKLADU_OPTIONS.get(tip_zakladu, {}).get("nazva", tip_zakladu)
                flash(
                    f'Демо-школу підлаштовано під "{nazva_tipu}" (класи {p_vid}-{p_do}). '
                    f'Класів: {pidsumok["klasy"]}, вчителів: {pidsumok["vchyteli"]}, '
                    f'кабінетів: {pidsumok["kabinety"]}, асистентів: {pidsumok["asystenty"]}, '
                    f'рядків навчального плану: {pidsumok["plan"]}. Повний набір 1-11 '
                    'збережено — повернути можна вибором "1-11 (стара школа)".',
                    "success",
                )

        conn.commit()
        conn.close()
        return redirect(url_for("nalashtuvannya"))

    settings = conn.execute("SELECT * FROM school_settings WHERE id = 1").fetchone()
    dzvinky_rows = conn.execute("SELECT * FROM dzvinky ORDER BY zmina, urok").fetchall()
    ye_parnist = _ye_parnisni_zapysy(conn)
    tip_zakladu_potochnyi = settings["tip_zakladu"] if settings else "stara_shkola"
    tz_checked = _tip_zakladu_do_checkboxiv(tip_zakladu_potochnyi)
    klasy_poza = _klasy_poza_zakladom(conn, tip_zakladu_potochnyi)
    # чи має сенс попереджати про перебудову демо-даних (демо-режим + є майстер)
    demo_pidlashtuvannya = DEMO_REZHYM_AKTYVNYI and _demo_master_ye(conn)
    conn.close()

    dzvinky = {}
    for r in dzvinky_rows:
        dzvinky[(r["zmina"], r["urok"])] = r

    return render_template(
        "nalashtuvannya.html", active="nalashtuvannya",
        settings=settings, dzvinky=dzvinky, ye_parnist=ye_parnist,
        tz_checked=tz_checked, klasy_poza_zakladom=klasy_poza,
        demo_pidlashtuvannya=demo_pidlashtuvannya,
    )


POZAUROCHNI_TYPY = {
    "vyhovna": "Виховна година",
    "gpd": "ГПД (група подовженого дня)",
    "konsultatsiya": "Консультація",
    "fakultatyv": "Факультатив",
    "gurtok": "Гурток",
    "korekciyno_rozvytkove": "Корекційно-розвиткове заняття",
    "indyvidualne_navchannya": "Індивідуальне навчання",
    "inshe": "Інше",
}
app.jinja_env.globals["POZAUROCHNI_TYPY"] = POZAUROCHNI_TYPY


@app.route("/pozaurochni-zanyattya")
def pozaurochni_zanyattya():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM pozaurochni_zanyattya ORDER BY nazva COLLATE UKR").fetchall()
    conn.close()
    return render_template("pozaurochni_zanyattya_list.html", active="pozaurochni_zanyattya", zanyattya=rows)


def _normalizuvaty_fiksovanyi_den(raw):
    """Розбирає й нормалізує ввід дня(-ів) тижня (напр. '1, 3-5') у канонічний
    вигляд (відсортовано, через кому) або None, якщо порожньо. Повертає
    (normalized, valid) - valid=False, якщо щось ввели, але жодного дня 1-5 не
    розпізнано (щоб форма могла показати помилку, а не тихо трактувати як "без дня")."""
    from generator import _parse_dni_tyzhnya
    raw = (raw or "").strip()
    dni_mnozhyna = _parse_dni_tyzhnya(raw)
    normalized = ",".join(str(d) for d in sorted(dni_mnozhyna)) if dni_mnozhyna else None
    valid = not raw or bool(dni_mnozhyna)
    return normalized, valid


@app.route("/pozaurochni-zanyattya/new", methods=["GET", "POST"])
def pozaurochni_zanyattya_new():
    conn = get_connection()
    if request.method == "POST":
        nazva = request.form.get("nazva", "").strip()
        typ = request.form.get("typ", "inshe")
        fiksovanyi_den, den_valid = _normalizuvaty_fiksovanyi_den(request.form.get("fiksovanyi_den", ""))
        errors = []
        if not nazva:
            errors.append("Назва обов'язкова.")
        if not den_valid:
            errors.append("Дні тижня некоректні - введіть числа 1-5 через кому і/або діапазон через дефіс (напр. 1,3 або 1-3).")
        if not errors:
            try:
                conn.execute(
                    "INSERT INTO pozaurochni_zanyattya (nazva, typ, fiksovanyi_den) VALUES (?, ?, ?)",
                    (nazva, typ, fiksovanyi_den),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("pozaurochni_zanyattya"))
            except sqlite3.IntegrityError:
                errors.append("Заняття з такою назвою вже існує.")
        conn.close()
        return render_template("pozaurochni_zanyattya_form.html", active="pozaurochni_zanyattya", zanyattya=None, errors=errors)
    conn.close()
    return render_template("pozaurochni_zanyattya_form.html", active="pozaurochni_zanyattya", zanyattya=None, errors=[])


@app.route("/pozaurochni-zanyattya/<int:zanyattya_id>/edit", methods=["GET", "POST"])
def pozaurochni_zanyattya_edit(zanyattya_id):
    conn = get_connection()
    zanyattya = conn.execute("SELECT * FROM pozaurochni_zanyattya WHERE id = ?", (zanyattya_id,)).fetchone()
    if zanyattya is None:
        conn.close()
        abort(404)
    if request.method == "POST":
        nazva = request.form.get("nazva", "").strip()
        typ = request.form.get("typ", "inshe")
        fiksovanyi_den, den_valid = _normalizuvaty_fiksovanyi_den(request.form.get("fiksovanyi_den", ""))
        errors = []
        if not nazva:
            errors.append("Назва обов'язкова.")
        if not den_valid:
            errors.append("Дні тижня некоректні - введіть числа 1-5 через кому і/або діапазон через дефіс (напр. 1,3 або 1-3).")
        if not errors:
            try:
                conn.execute(
                    "UPDATE pozaurochni_zanyattya SET nazva = ?, typ = ?, fiksovanyi_den = ? WHERE id = ?",
                    (nazva, typ, fiksovanyi_den, zanyattya_id),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("pozaurochni_zanyattya"))
            except sqlite3.IntegrityError:
                errors.append("Заняття з такою назвою вже існує.")
        conn.close()
        return render_template("pozaurochni_zanyattya_form.html", active="pozaurochni_zanyattya", zanyattya=zanyattya, errors=errors)
    conn.close()
    return render_template("pozaurochni_zanyattya_form.html", active="pozaurochni_zanyattya", zanyattya=zanyattya, errors=[])


@app.route("/pozaurochni-zanyattya/<int:zanyattya_id>/delete", methods=["POST"])
def pozaurochni_zanyattya_delete(zanyattya_id):
    conn = get_connection()
    conn.execute("DELETE FROM pozaurochni_zanyattya WHERE id = ?", (zanyattya_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("pozaurochni_zanyattya"))


@app.route("/pozaurochni-zanyattya/<int:zanyattya_id>/toggle", methods=["POST"])
def pozaurochni_zanyattya_toggle(zanyattya_id):
    """ВКЛ/ВИКЛ заняття: вимкнене генератор повністю пропускає (усі рядки його
    позаурочного плану). Записи не видаляються - лише не потрапляють у генерацію."""
    conn = get_connection()
    conn.execute("UPDATE pozaurochni_zanyattya SET aktyvne = 1 - COALESCE(aktyvne, 1) WHERE id = ?", (zanyattya_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("pozaurochni_zanyattya"))


@app.route("/pozaurochnyj-plan")
def pozaurochnyj_plan():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT pp.*, k.paralel, k.litera, z.nazva AS zanyattya_nazva, z.typ AS zanyattya_typ,
               z.fiksovanyi_den AS zanyattya_fiksovanyi_den,
               v.pib AS vchytel_pib, kab.nazva AS kabinet_nazva
        FROM pozaurochnyj_plan pp
        JOIN klasy k ON k.id = pp.klas_id
        JOIN pozaurochni_zanyattya z ON z.id = pp.zanyattya_id
        JOIN vchyteli v ON v.id = pp.vchytel_id
        LEFT JOIN kabinety kab ON kab.id = pp.domashniy_kabinet_id
        ORDER BY k.paralel, k.litera COLLATE UKR, z.nazva COLLATE UKR
        """
    ).fetchall()
    conn.close()
    return render_template("pozaurochnyj_plan_list.html", active="pozaurochnyj_plan", zapysy=rows)


def _read_pozaurochnyj_form(form):
    kabinet_id = form.get("domashniy_kabinet_id") or None
    klas_id_raw = form.get("klas_id", "")
    fiksovanyi_den, den_valid = _normalizuvaty_fiksovanyi_den(form.get("fiksovanyi_den", ""))
    return {
        # "komplekt-N" обробляється окремо в pozaurochnyj_plan_new - тут лише
        # звичайний числовий klas_id (або None, якщо префікс комплекту чи порожньо)
        "klas_id": int(klas_id_raw) if klas_id_raw.isdigit() else None,
        "zanyattya_id": int(form["zanyattya_id"]) if form.get("zanyattya_id") else None,
        "vchytel_id": int(form["vchytel_id"]) if form.get("vchytel_id") else None,
        "domashniy_kabinet_id": int(kabinet_id) if kabinet_id else None,
        "godyn_tyzhden": int(form.get("godyn_tyzhden") or 1),
        "fiksovanyi_den": fiksovanyi_den,
        "fiksovanyi_den_valid": den_valid,
    }


@app.route("/pozaurochnyj-plan/new", methods=["GET", "POST"])
def pozaurochnyj_plan_new():
    conn = get_connection()
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_zanyattya = conn.execute("SELECT * FROM pozaurochni_zanyattya ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()
    vsi_komplekty = _zavantazhyty_komplekty_z_chlenamy(conn)

    if request.method == "POST":
        klas_id_raw = request.form.get("klas_id", "")
        # клас-комплект обраний - розгортаємо на всіх членів групи, а не один клас
        if klas_id_raw.startswith("komplekt-"):
            komplekt_id = int(klas_id_raw.replace("komplekt-", ""))
            chleny = conn.execute("SELECT id FROM klasy WHERE komplekt_id = ?", (komplekt_id,)).fetchall()
            klas_ids = [c["id"] for c in chleny]
        elif klas_id_raw:
            klas_ids = [int(klas_id_raw)]
        else:
            klas_ids = []

        data = _read_pozaurochnyj_form(request.form)
        errors = []
        if not klas_ids:
            errors.append("Оберіть клас або клас-комплект.")
        if not data["zanyattya_id"]:
            errors.append("Оберіть заняття.")
        if not data["vchytel_id"]:
            errors.append("Оберіть вчителя.")
        if data["godyn_tyzhden"] <= 0:
            errors.append("Годин на тиждень має бути більше нуля.")
        if not data["fiksovanyi_den_valid"]:
            errors.append("Дні тижня некоректні - введіть числа 1-5 через кому і/або діапазон через дефіс (напр. 1,3 або 1-3).")
        if not errors:
            try:
                for klas_id in klas_ids:
                    conn.execute(
                        """
                        INSERT INTO pozaurochnyj_plan
                            (klas_id, zanyattya_id, vchytel_id, domashniy_kabinet_id, godyn_tyzhden, fiksovanyi_den)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (klas_id, data["zanyattya_id"], data["vchytel_id"],
                         data["domashniy_kabinet_id"], data["godyn_tyzhden"], data["fiksovanyi_den"]),
                    )
                conn.commit()
                conn.close()
                return redirect(url_for("pozaurochnyj_plan"))
            except sqlite3.IntegrityError:
                conn.rollback()
                errors.append("Для одного з класів це заняття вже призначене.")
        conn.close()
        return render_template(
            "pozaurochnyj_plan_form.html", active="pozaurochnyj_plan", zapys=None, errors=errors,
            all_klasy=all_klasy, all_zanyattya=all_zanyattya, all_vchyteli=all_vchyteli,
            all_kabinety=all_kabinety, vsi_komplekty=vsi_komplekty,
        )
    conn.close()
    return render_template(
        "pozaurochnyj_plan_form.html", active="pozaurochnyj_plan", zapys=None, errors=[],
        all_klasy=all_klasy, all_zanyattya=all_zanyattya, all_vchyteli=all_vchyteli,
        all_kabinety=all_kabinety, vsi_komplekty=vsi_komplekty,
    )


@app.route("/pozaurochnyj-plan/<int:zapys_id>/edit", methods=["GET", "POST"])
def pozaurochnyj_plan_edit(zapys_id):
    conn = get_connection()
    zapys = conn.execute("SELECT * FROM pozaurochnyj_plan WHERE id = ?", (zapys_id,)).fetchone()
    if zapys is None:
        conn.close()
        abort(404)
    all_klasy = conn.execute("SELECT * FROM klasy ORDER BY paralel, litera COLLATE UKR").fetchall()
    all_zanyattya = conn.execute("SELECT * FROM pozaurochni_zanyattya ORDER BY nazva COLLATE UKR").fetchall()
    all_vchyteli = conn.execute("SELECT * FROM vchyteli ORDER BY pib COLLATE UKR").fetchall()
    all_kabinety = conn.execute("SELECT * FROM kabinety ORDER BY nazva COLLATE UKR").fetchall()

    if request.method == "POST":
        data = _read_pozaurochnyj_form(request.form)
        errors = []
        if not data["klas_id"]:
            errors.append("Оберіть клас.")
        if not data["zanyattya_id"]:
            errors.append("Оберіть заняття.")
        if not data["vchytel_id"]:
            errors.append("Оберіть вчителя.")
        if data["godyn_tyzhden"] <= 0:
            errors.append("Годин на тиждень має бути більше нуля.")
        if not data["fiksovanyi_den_valid"]:
            errors.append("Дні тижня некоректні - введіть числа 1-5 через кому і/або діапазон через дефіс (напр. 1,3 або 1-3).")
        if not errors:
            try:
                conn.execute(
                    """
                    UPDATE pozaurochnyj_plan SET klas_id = ?, zanyattya_id = ?, vchytel_id = ?,
                        domashniy_kabinet_id = ?, godyn_tyzhden = ?, fiksovanyi_den = ?
                    WHERE id = ?
                    """,
                    (data["klas_id"], data["zanyattya_id"], data["vchytel_id"],
                     data["domashniy_kabinet_id"], data["godyn_tyzhden"], data["fiksovanyi_den"], zapys_id),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("pozaurochnyj_plan"))
            except sqlite3.IntegrityError:
                errors.append("Для цього класу й заняття запис уже існує.")
        conn.close()
        return render_template(
            "pozaurochnyj_plan_form.html", active="pozaurochnyj_plan", zapys=zapys, errors=errors,
            all_klasy=all_klasy, all_zanyattya=all_zanyattya, all_vchyteli=all_vchyteli,
            all_kabinety=all_kabinety, vsi_komplekty=[],
        )
    conn.close()
    return render_template(
        "pozaurochnyj_plan_form.html", active="pozaurochnyj_plan", zapys=zapys, errors=[],
        all_klasy=all_klasy, all_zanyattya=all_zanyattya, all_vchyteli=all_vchyteli,
        all_kabinety=all_kabinety, vsi_komplekty=[],
    )


@app.route("/pozaurochnyj-plan/<int:zapys_id>/delete", methods=["POST"])
def pozaurochnyj_plan_delete(zapys_id):
    conn = get_connection()
    conn.execute("DELETE FROM pozaurochnyj_plan WHERE id = ?", (zapys_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("pozaurochnyj_plan"))


@app.route("/normatyvni-dokumenty")
def normatyvni_dokumenty():
    return render_template("normatyvni_dokumenty.html", active="normatyvni_dokumenty")


# ---------- Вбудована довідка ("man") ----------
# Файли data/man/<slug>.md - по одному на пункт меню. Slug = значення `active`,
# яке передає відповідний маршрут (кнопка "?" біля заголовка сторінки бере його
# з <body data-dovidka>). Підсторінки ("Додатково", "Доступність", "Матриця
# вчителя" тощо) успадковують `active` батька, тож ведуть на той самий файл.
MAN_DIR = os.path.join(BASE_DIR, "man")
_MAN_ALIASY = {
    # каталог "Позаурочні заняття" має власний active, але описаний у файлі плану
    "pozaurochni_zanyattya": "pozaurochnyj_plan",
}


@app.route("/dovidka/<slug>")
def dovidka_rozdil(slug):
    import dovidka_md

    if not re.fullmatch(r"[a-z0-9_-]+", slug or ""):
        abort(404)
    slug = _MAN_ALIASY.get(slug, slug)

    kandydaty = [slug, slug.replace("-", "_"), slug.replace("_", "-")]
    shlyah = None
    for k in kandydaty:
        p = os.path.join(MAN_DIR, k + ".md")
        if os.path.isfile(p):
            shlyah = p
            break

    if shlyah is None:
        return render_template(
            "dovidka.html", active=None,
            nazva="Довідку ще не написано",
            html_vmist="<p>Для цього розділу інструкцію ще не додано. "
                       'Загальний перелік — у файлі <code>data/man/README.md</code>.</p>',
        )

    with open(shlyah, "r", encoding="utf-8") as f:
        tekst = f.read()
    nazva = dovidka_md.pershyj_zagolovok(tekst)
    # заголовок 1-го рівня показує сам шаблон (як назву сторінки) - прибираємо його
    # з тіла, щоб не дублювався
    telo = re.sub(r"^\s*#\s+.*\n?", "", tekst, count=1)
    return render_template(
        "dovidka.html", active=None,
        nazva=nazva,
        html_vmist=dovidka_md.render(telo),
    )


# ---------- Типові навчальні плани МОН ----------
# Лише перегляд - дані живуть у json/mon_normy.json (як решта МОН-норм), НЕ в БД,
# і не редагуються через UI. Кожен документ показується як є, без жодного
# обчислення застосунком (різні документи рахують підсумки по-різному - уніфікація
# лише плутала б, а не допомагала).

from mon_normy_loader import typovi_navchalni_plany
TYPOVI_NAVCHALNI_PLANY = typovi_navchalni_plany()


@app.route("/typovi-plany")
def typovi_plany():
    return render_template("typovi_plany_list.html", active="typovi_plany", plany=TYPOVI_NAVCHALNI_PLANY)


@app.route("/typovi-plany/<plan_kod>")
def typovi_plany_perehlyad(plan_kod):
    if plan_kod not in TYPOVI_NAVCHALNI_PLANY:
        abort(404)
    return render_template(
        "typovi_plany_perehlyad.html", active="typovi_plany",
        plan=TYPOVI_NAVCHALNI_PLANY[plan_kod],
    )


# Базові "Групи" (профільна старша школа, 10-12 кл.) - довідник кластерів/профілів
# наказу МОН №1674 для генерації коду назви групи (10-МЛК-МЛ-1). Скорочення (МЛК/МЛ/...)
# - НАШЕ рішення (МОН їх не визначає), profil_kod тут = ключ у
# json/mon_normy.json -> typovi_navchalni_plany (єдине джерело правди про сам план).
# tuple: (klaster_kod, klaster_nazva, profil_kod_korotkyi, profil_nazva)
PROFILY_GRUP = {
    "10_12_mlk_ukr_mova_literatura": ("МЛК", "Мовно-літературний кластер", "МЛ", "Поглиблена українська мова та література"),
    "10_12_mlk_inozemna_mova": ("МЛК", "Мовно-літературний кластер", "ІМ", "Поглиблена іноземна мова"),
    "10_12_shg_istoriya_gromadyanska": ("СГК", "Соціально-гуманітарний кластер", "ІГ", "Історія та громадянська освіта"),
    "10_12_shg_geografiya_inozemna": ("СГК", "Соціально-гуманітарний кластер", "ГІ", "Географія та іноземна мова"),
    "10_12_shg_mystetstvo": ("СГК", "Соціально-гуманітарний кластер", "МИ", "Мистецтво"),
    "10_12_stem_himiya_biologiya": ("STEM", "STEM кластер", "ХБ", "Хімія та біологія"),
    "10_12_stem_matematyka_fizyka": ("STEM", "STEM кластер", "МФ", "Математика та фізика"),
    "10_12_stem_matematyka_informatyka_tehnologiyi": ("STEM", "STEM кластер", "МІТ", "Математика, інформатика, технології"),
}


def _grupa_nazva(paralel, profil_kod, variant):
    info = PROFILY_GRUP.get(profil_kod)
    klaster_kod = info[0] if info else "?"
    profil_kod_korotkyi = info[2] if info else "?"
    return f"{paralel}-{klaster_kod}-{profil_kod_korotkyi}-{variant}".strip("-")


def _read_grupa_form(form):
    paralel = int(form.get("paralel") or 10)
    profil_kod = form.get("profil_kod", "")
    variant = (form.get("variant") or "1").strip()
    return {
        "paralel": paralel,
        "profil_kod": profil_kod,
        "variant": variant,
        "aktyvna": 1 if form.get("aktyvna") else 0,
        "nazva": _grupa_nazva(paralel, profil_kod, variant),
    }


@app.route("/grupy")
def grupy():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM grupy ORDER BY paralel, nazva COLLATE UKR").fetchall()
    result = []
    for g in rows:
        sklad = conn.execute(
            """
            SELECT k.paralel, k.litera, gk.kilkist_uchniv
            FROM grupy_klasy gk JOIN klasy k ON k.id = gk.klas_id
            WHERE gk.grupa_id = ?
            ORDER BY k.paralel, k.litera COLLATE UKR
            """,
            (g["id"],),
        ).fetchall()
        row = dict(g)
        info = PROFILY_GRUP.get(g["profil_kod"])
        row["profil_nazva"] = info[3] if info else g["profil_kod"]
        row["klaster_nazva"] = info[1] if info else "?"
        row["sklad_opys"] = ", ".join(
            f"{klas_nazva(s['paralel'], s['litera'])} ({s['kilkist_uchniv']})" for s in sklad
        )
        row["razom_uchniv"] = sum(s["kilkist_uchniv"] for s in sklad)
        result.append(row)
    conn.close()
    return render_template("grupy_list.html", active="grupy", grupy=result)


@app.route("/grupy/new", methods=["GET", "POST"])
def grupy_new():
    errors = []
    if request.method == "POST":
        data = _read_grupa_form(request.form)
        if data["profil_kod"] not in PROFILY_GRUP:
            errors.append("Оберіть профіль.")

        if not errors:
            conn = get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO grupy (paralel, profil_kod, variant, nazva, aktyvna)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (data["paralel"], data["profil_kod"], data["variant"], data["nazva"], data["aktyvna"]),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("grupy"))
            except sqlite3.IntegrityError:
                conn.close()
                errors = [f'Група "{data["nazva"]}" вже існує (така сама паралель/профіль/варіант).']

        return render_template(
            "grupy_form.html", active="grupy", grupa=data,
            profily=PROFILY_GRUP, errors=errors,
        )

    return render_template(
        "grupy_form.html", active="grupy", grupa=None,
        profily=PROFILY_GRUP, errors=[],
    )


@app.route("/grupy/<int:grupa_id>/edit", methods=["GET", "POST"])
def grupy_edit(grupa_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM grupy WHERE id = ?", (grupa_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    errors = []
    if request.method == "POST":
        data = _read_grupa_form(request.form)
        if data["profil_kod"] not in PROFILY_GRUP:
            errors.append("Оберіть профіль.")

        if not errors:
            try:
                conn.execute(
                    """
                    UPDATE grupy SET paralel = ?, profil_kod = ?, variant = ?, nazva = ?, aktyvna = ?
                    WHERE id = ?
                    """,
                    (data["paralel"], data["profil_kod"], data["variant"], data["nazva"], data["aktyvna"], grupa_id),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("grupy"))
            except sqlite3.IntegrityError:
                errors = [f'Група "{data["nazva"]}" вже існує (така сама паралель/профіль/варіант).']

        conn.close()
        data["id"] = grupa_id
        return render_template(
            "grupy_form.html", active="grupy", grupa=data,
            profily=PROFILY_GRUP, errors=errors,
        )

    conn.close()
    return render_template(
        "grupy_form.html", active="grupy", grupa=dict(existing),
        profily=PROFILY_GRUP, errors=[],
    )


@app.route("/grupy/<int:grupa_id>/delete", methods=["POST"])
def grupy_delete(grupa_id):
    conn = get_connection()
    conn.execute("DELETE FROM grupy WHERE id = ?", (grupa_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("grupy"))


def _vilni_mistsya_po_klasah(conn, klasy_paraleli, grupa_id):
    """Скільки учнів кожного класу ще НЕ розподілено в жодну іншу групу (не цю) -
    щоб не доводилось вручну вираховувати, скільки ще можна додати сюди."""
    result = {}
    for k in klasy_paraleli:
        inshi_sum = conn.execute(
            "SELECT COALESCE(SUM(kilkist_uchniv), 0) AS s FROM grupy_klasy WHERE klas_id = ? AND grupa_id != ?",
            (k["id"], grupa_id),
        ).fetchone()["s"]
        result[k["id"]] = k["kilkist_uchniv"] - inshi_sum
    return result


@app.route("/grupy/<int:grupa_id>/sklad", methods=["GET", "POST"])
def grupy_sklad(grupa_id):
    """Склад групи - з яких реальних класів (тієї самої паралелі) і скільки учнів.
    Один клас може постачати учнів у кілька різних груп одночасно - тому при
    збереженні перевіряємо суму по кожному класу ПО ВСІХ групах разом, не лише в
    цій."""
    conn = get_connection()
    grupa = conn.execute("SELECT * FROM grupy WHERE id = ?", (grupa_id,)).fetchone()
    if grupa is None:
        conn.close()
        abort(404)

    klasy_paraleli = conn.execute(
        "SELECT * FROM klasy WHERE paralel = ? ORDER BY litera COLLATE UKR", (grupa["paralel"],)
    ).fetchall()

    errors = []
    if request.method == "POST":
        nova_kilkist = {}
        for k in klasy_paraleli:
            znachennya = request.form.get(f"kilkist_{k['id']}", "").strip()
            if znachennya:
                nova_kilkist[k["id"]] = int(znachennya)

        for klas_id, kilkist in nova_kilkist.items():
            klas = next(k for k in klasy_paraleli if k["id"] == klas_id)
            inshi_grupy_sum = conn.execute(
                "SELECT COALESCE(SUM(kilkist_uchniv), 0) AS s FROM grupy_klasy WHERE klas_id = ? AND grupa_id != ?",
                (klas_id, grupa_id),
            ).fetchone()["s"]
            if inshi_grupy_sum + kilkist > klas["kilkist_uchniv"]:
                errors.append(
                    f"Клас {klas_nazva(klas['paralel'], klas['litera'])}: "
                    f"{inshi_grupy_sum} учнів уже розподілено в інші групи, "
                    f"+{kilkist} перевищує реальну кількість ({klas['kilkist_uchniv']})."
                )

        razom = sum(nova_kilkist.values())
        poperedzhennya = None
        if razom and razom < 8:
            poperedzhennya = (
                f"Разом у групі {razom} учнів — менше мінімального складу міжкласної "
                f"групи (8, Положення про ліцей). Збережено, але перевірте набір."
            )

        if not errors:
            conn.execute("DELETE FROM grupy_klasy WHERE grupa_id = ?", (grupa_id,))
            for klas_id, kilkist in nova_kilkist.items():
                conn.execute(
                    "INSERT INTO grupy_klasy (grupa_id, klas_id, kilkist_uchniv) VALUES (?, ?, ?)",
                    (grupa_id, klas_id, kilkist),
                )
            conn.commit()
            vilni = _vilni_mistsya_po_klasah(conn, klasy_paraleli, grupa_id)
            conn.close()
            if poperedzhennya:
                return render_template(
                    "grupy_sklad.html", active="grupy", grupa=dict(grupa),
                    klasy_paraleli=klasy_paraleli, sklad=nova_kilkist, errors=[],
                    poperedzhennya=poperedzhennya, vilni=vilni,
                )
            return redirect(url_for("grupy"))

        vilni = _vilni_mistsya_po_klasah(conn, klasy_paraleli, grupa_id)
        conn.close()
        return render_template(
            "grupy_sklad.html", active="grupy", grupa=dict(grupa),
            klasy_paraleli=klasy_paraleli, sklad=nova_kilkist, errors=errors,
            poperedzhennya=None, vilni=vilni,
        )

    sklad = {
        r["klas_id"]: r["kilkist_uchniv"] for r in conn.execute(
            "SELECT klas_id, kilkist_uchniv FROM grupy_klasy WHERE grupa_id = ?", (grupa_id,)
        ).fetchall()
    }
    vilni = _vilni_mistsya_po_klasah(conn, klasy_paraleli, grupa_id)
    conn.close()
    return render_template(
        "grupy_sklad.html", active="grupy", grupa=dict(grupa),
        klasy_paraleli=klasy_paraleli, sklad=sklad, errors=[], poperedzhennya=None,
        vilni=vilni,
    )


# "Профілі навчання" - власний навчальний план КОЖНОЇ групи (не спільний на профіль).
# Вчитель на рядок плану вже є (vchytel_id) - НЕОБОВ'ЯЗКОВИЙ у БД (старі рядки
# без вчителя лишаються робочими), але форма вимагає його для нових/редагованих
# записів. "Тарифікація"/"Матриця вчителя" ЦІ години поки НЕ враховують - свідомо
# відкладений окремий крок (рішення користувача 2026-08-29).
@app.route("/profily-navchannya")
def profily_navchannya():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT gnp.*, g.nazva AS grupa_nazva, g.aktyvna AS grupa_aktyvna,
               p.nazva AS predmet_nazva, v.pib AS vchytel_pib
        FROM grupa_navchalnyj_plan gnp
        JOIN grupy g ON g.id = gnp.grupa_id
        JOIN predmety p ON p.id = gnp.predmet_id
        LEFT JOIN vchyteli v ON v.id = gnp.vchytel_id
        ORDER BY g.paralel, g.nazva COLLATE UKR, p.nazva COLLATE UKR
        """
    ).fetchall()
    conn.close()

    zapysy_po_grupi = []
    for r in rows:
        if not zapysy_po_grupi or zapysy_po_grupi[-1]["grupa_nazva"] != r["grupa_nazva"]:
            zapysy_po_grupi.append({
                "grupa_nazva": r["grupa_nazva"], "grupa_aktyvna": r["grupa_aktyvna"],
                "zapysy": [], "razom": 0,
            })
        zapysy_po_grupi[-1]["zapysy"].append(r)
        zapysy_po_grupi[-1]["razom"] += r["godyn_tyzhden"]

    return render_template(
        "profily_navchannya_list.html", active="profily_navchannya",
        zapysy_po_grupi=zapysy_po_grupi,
    )


def _read_profil_plan_form(form):
    godyn_raw = form.get("godyn_tyzhden", "").strip()
    vchytel_raw = form.get("vchytel_id") or ""
    return {
        "grupa_id": int(form.get("grupa_id") or 0),
        "predmet_id": int(form.get("predmet_id") or 0),
        "vchytel_id": int(vchytel_raw) if vchytel_raw else None,
        "godyn_tyzhden": int(godyn_raw) if godyn_raw.isdigit() else 0,
    }


@app.route("/profily-navchannya/new", methods=["GET", "POST"])
def profily_navchannya_new():
    conn = get_connection()
    all_grupy = conn.execute("SELECT * FROM grupy ORDER BY paralel, nazva COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    # Групи завжди 10-12 (рівень "Ліцей") - показуємо лише вчителів, позначених
    # для цього рівня (riven_liceum), щоб не гортати весь список вчителів школи.
    all_vchyteli = conn.execute(
        "SELECT * FROM vchyteli WHERE riven_liceum = 1 ORDER BY pib COLLATE UKR"
    ).fetchall()
    errors = []

    if request.method == "POST":
        data = _read_profil_plan_form(request.form)
        if not data["grupa_id"]:
            errors.append("Оберіть групу.")
        if not data["predmet_id"]:
            errors.append("Оберіть предмет.")
        if not data["vchytel_id"]:
            errors.append("Оберіть вчителя.")
        if data["godyn_tyzhden"] <= 0:
            errors.append("Кількість годин має бути більше нуля.")

        if not errors:
            try:
                conn.execute(
                    """
                    INSERT INTO grupa_navchalnyj_plan (grupa_id, predmet_id, vchytel_id, godyn_tyzhden)
                    VALUES (?, ?, ?, ?)
                    """,
                    (data["grupa_id"], data["predmet_id"], data["vchytel_id"], data["godyn_tyzhden"]),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("profily_navchannya"))
            except sqlite3.IntegrityError:
                errors = ["Цей предмет уже є в плані цієї групи — відредагуйте наявний рядок."]

        conn.close()
        return render_template(
            "profily_navchannya_form.html", active="profily_navchannya",
            all_grupy=all_grupy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
            errors=errors, zapys=data,
        )

    conn.close()
    return render_template(
        "profily_navchannya_form.html", active="profily_navchannya",
        all_grupy=all_grupy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
        errors=[], zapys=None,
    )


@app.route("/profily-navchannya/<int:zapys_id>/edit", methods=["GET", "POST"])
def profily_navchannya_edit(zapys_id):
    conn = get_connection()
    existing = conn.execute("SELECT * FROM grupa_navchalnyj_plan WHERE id = ?", (zapys_id,)).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    all_grupy = conn.execute("SELECT * FROM grupy ORDER BY paralel, nazva COLLATE UKR").fetchall()
    all_predmety = conn.execute("SELECT * FROM predmety ORDER BY nazva COLLATE UKR").fetchall()
    # Групи завжди 10-12 (рівень "Ліцей") - показуємо вчителів, позначених для
    # цього рівня, + вже призначеного (якщо раптом не позначений - щоб не губити
    # поточний вибір при редагуванні).
    all_vchyteli = conn.execute(
        "SELECT * FROM vchyteli WHERE riven_liceum = 1 OR id = ? ORDER BY pib COLLATE UKR",
        (existing["vchytel_id"],),
    ).fetchall()
    errors = []

    if request.method == "POST":
        data = _read_profil_plan_form(request.form)
        if not data["grupa_id"]:
            errors.append("Оберіть групу.")
        if not data["predmet_id"]:
            errors.append("Оберіть предмет.")
        if not data["vchytel_id"]:
            errors.append("Оберіть вчителя.")
        if data["godyn_tyzhden"] <= 0:
            errors.append("Кількість годин має бути більше нуля.")

        if not errors:
            try:
                conn.execute(
                    """
                    UPDATE grupa_navchalnyj_plan SET grupa_id = ?, predmet_id = ?,
                        vchytel_id = ?, godyn_tyzhden = ?
                    WHERE id = ?
                    """,
                    (data["grupa_id"], data["predmet_id"], data["vchytel_id"],
                     data["godyn_tyzhden"], zapys_id),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("profily_navchannya"))
            except sqlite3.IntegrityError:
                errors = ["Цей предмет уже є в плані цієї групи."]

        conn.close()
        data["id"] = zapys_id
        return render_template(
            "profily_navchannya_form.html", active="profily_navchannya",
            all_grupy=all_grupy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
            errors=errors, zapys=data,
        )

    conn.close()
    return render_template(
        "profily_navchannya_form.html", active="profily_navchannya",
        all_grupy=all_grupy, all_predmety=all_predmety, all_vchyteli=all_vchyteli,
        errors=[], zapys=dict(existing),
    )


@app.route("/profily-navchannya/<int:zapys_id>/delete", methods=["POST"])
def profily_navchannya_delete(zapys_id):
    conn = get_connection()
    conn.execute("DELETE FROM grupa_navchalnyj_plan WHERE id = ?", (zapys_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("profily_navchannya"))


if __name__ == "__main__":
    init_db()
    # щоденна автокопія бази - захист від "три тижні вводив план і жодного разу
    # не натиснув Створити копію" (у демо-режимі не робиться)
    from db import avtobekap_pry_starti
    avtobekap_pry_starti()
    # чи вийшла нова версія - тихо у фоні, раз на добу (див. onovlennya.py)
    onovlennya.zapustyty_perevirku(os.path.dirname(DB_PATH))
    # debug вимкнено за замовчуванням: у школи це бойовий запуск (run.bat), і
    # debug=True дав би сторінку з трейсбеком замість зрозумілої помилки плюс
    # авто-перезавантажувач, який запускає ДРУГИЙ процес (саме він колись обійшов
    # SCHOOL_BASE_DIR і мігрував не ту базу - розд. 5 PROJECT_OVERVIEW).
    # Для розробки: SCHOOL_DEBUG=1 у змінних середовища.
    debug = os.environ.get("SCHOOL_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
