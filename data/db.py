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

import sqlite3
import os
import subprocess
import json


def _vyznachyty_fizychni_yadra():
    """Фізичні ядра CPU через PowerShell/CIM - без залежності psutil.
    Викликається ОДИН РАЗ при старті застосунку (init_db), не при генерації.
    Windows-специфічно (Get-CimInstance) - якщо колись перенесете на Linux,
    цю функцію треба буде переписати (напр. через /proc/cpuinfo)."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Processor | "
                "Select-Object NumberOfCores | ConvertTo-Json"
            ],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        # якщо кілька фізичних CPU в системі - Get-CimInstance поверне список,
        # а не один об'єкт; на звичайному ПК зі школи це малоймовірно, але
        # обробляємо обидва варіанти на всяк випадок
        if isinstance(data, list):
            cores = sum(item["NumberOfCores"] for item in data)
        else:
            cores = data["NumberOfCores"]
        return int(cores)
    except Exception:
        # PowerShell недоступний, парсинг не вдався тощо - fallback на
        # приблизну оцінку через стандартну бібліотеку
        return max(1, (os.cpu_count() or 4) // 2)
import shutil
from datetime import datetime

from version import APP_VERSION

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Папка з базами школи. За замовчуванням - <корінь>/base. Змінна оточення
# SCHOOL_BASE_DIR дозволяє перенаправити її на іншу папку (використовується в
# автотестах, щоб жодна дія не чіпала реальну base/ поруч із проєктом).
BASE_SUBDIR = os.environ.get("SCHOOL_BASE_DIR") or os.path.join(BASE_DIR, "base")

# Дві незалежні бази, що ІСНУЮТЬ ОДНОЧАСНО: school.db - реальні дані школи,
# school-demo.db - навчальний приклад. Демо-база НЕ генерується кодом: під час
# збірки релізу build_release.bat просто копіює поточну school.db розробника у
# school-demo.db (тому вона завжди на актуальній схемі й з реалістичними даними;
# розробник перед релізом тримає school.db у придатному для публікації стані).
# Перемикання між базами - через файл-мітку aktyvna_baza.txt (одне слово: "demo"
# або "realna"). Мітка читається РІВНО ОДИН РАЗ при старті процесу - зміна режиму
# потребує перезапуску застосунку (той самий принцип "перезапуск обовʼязковий",
# що вже всюди в застосунку).
REAL_DB_PATH = os.path.join(BASE_SUBDIR, "school.db")
DEMO_DB_PATH = os.path.join(BASE_SUBDIR, "school-demo.db")
AKTYVNA_BAZA_MITKA = os.path.join(BASE_SUBDIR, "aktyvna_baza.txt")


def prochytaty_rezhym_bazy():
    """Повертає 'demo' або 'realna' за вмістом файлу-мітки. Будь-що незрозуміле
    (файл відсутній, порожній, пошкоджений, чужий текст) - це 'realna': режим за
    замовчуванням має бути безпечним, щоб випадково не працювати з демо як з
    реальною базою."""
    try:
        with open(AKTYVNA_BAZA_MITKA, "r", encoding="utf-8") as f:
            if f.read().strip().lower() == "demo":
                return "demo"
    except OSError:
        pass
    return "realna"


def zapysaty_rezhym_bazy(rezhym):
    """Перезаписує файл-мітку. rezhym: 'demo' або будь-що інше = 'realna'.
    Нічого не копіює - обидва файли баз лишаються недоторканими. Зміна набуде
    чинності лише після перезапуску процесу."""
    os.makedirs(BASE_SUBDIR, exist_ok=True)
    znachennya = "demo" if rezhym == "demo" else "realna"
    with open(AKTYVNA_BAZA_MITKA, "w", encoding="utf-8") as f:
        f.write(znachennya + "\n")
    return znachennya


def _vyznachyty_db_path():
    # SCHOOL_DB_PATH - прямий примусовий шлях до файлу бази (обходить мітку); для
    # автотестів, щоб працювати на тимчасовій копії, не чіпаючи реальні файли.
    override = os.environ.get("SCHOOL_DB_PATH")
    if override:
        return os.path.abspath(override)
    return DEMO_DB_PATH if prochytaty_rezhym_bazy() == "demo" else REAL_DB_PATH


# Обчислюється один раз при імпорті модуля - решта коду користується як константою.
DB_PATH = _vyznachyty_db_path()

# Чи активна зараз демо-база (для банерів/індикаторів в UI).
DEMO_REZHYM_AKTYVNYI = os.path.abspath(DB_PATH) == os.path.abspath(DEMO_DB_PATH)

# Демо-база несе ще й ЗНІМОК повної школи 1-11 у таблицях _demo_master_<назва>.
# Його кладе dev/build_demo_data.py наприкінці прогону, а застосунок наливає з
# нього робочі таблиці, коли завуч у демо-режимі міняє тип закладу (напр. лишає
# саму початкову). Знімок - єдине джерело: будь-який перехід між типами наливає
# заново, тому повний набір 1-11 ніколи не втрачається. Порядок списку = порядок
# наливання (батьківські таблиці раніше за дочірні).
DEMO_MASTER_PREFIX = "_demo_master_"
DEMO_MASTER_TABLES = (
    "vchyteli",
    "kabinety",
    "kabinety_predmety",
    "kabinety_vchyteli",
    "klas_komplekty",
    "klasy",
    # mizhpredmetni_grupy ОБОВ'ЯЗКОВО перед navchalnyj_plan: план посилається на
    # групу через mizhpredmetna_grupa_id. Порядок тут - єдина гарантія цілісності;
    # PRAGMA defer_foreign_keys діє лише всередині відкритої транзакції й у режимі
    # автокоміту мовчки скидається, тож покладатись на нього не можна.
    "mizhpredmetni_grupy",
    "navchalnyj_plan",
    "asystenty",
    "asystenty_predmety",
    "pozaurochni_zanyattya",
    "pozaurochnyj_plan",
)


# Стовпці таблиці predmety - ЄДИНЕ джерело для CREATE TABLE у init_db і для
# перебудови таблиці в міграції v0.0.36 (SQLite не вміє змінювати CHECK на льоту).
# podil_pravylo: лише 'avto' / 'nikoly' - саме ці два значення знає UI й код.
# Історичне 'zavzhdy' прибрано з CHECK (v0.0.36). Дефолт 'nikoly' збігається з
# формою створення предмета (раніше був 'avto', і масово додані предмети
# приходили з увімкненою перевіркою поділу, чого завуч не обирав).
_PREDMETY_STOVPTSI_SQL = """
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL UNIQUE,
            skorochennya TEXT,
            zdvoyeni_urok INTEGER NOT NULL DEFAULT 0,
            podil_pravylo TEXT NOT NULL DEFAULT 'nikoly' CHECK (podil_pravylo IN ('avto', 'nikoly')),
            zaboroneni_uroky TEXT,
            min_promizhok_dni INTEGER,
            fiksovanyi_den INTEGER,
            fiksovanyi_urok INTEGER,
            ostannim_urokom INTEGER NOT NULL DEFAULT 0,
            kategoria TEXT,
            osvitnya_galuz TEXT,
            vrahovuyetsya_u_hranychnomu INTEGER NOT NULL DEFAULT 1,
            vazhkist TEXT NOT NULL DEFAULT 'serednii' CHECK (vazhkist IN ('vazhkyi', 'serednii', 'legkyi')),
            normovanyi_kod TEXT,
            spets_kabinet_lyshe_pidgrupa1 INTEGER NOT NULL DEFAULT 0
"""


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table, column, definition):
    """Додає колонку, якщо її ще немає (проста міграція для SQLite)."""
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _drop_column_if_exists(conn, table, column):
    """Видаляє колонку, якщо вона ще є (SQLite 3.35+ підтримує DROP COLUMN напряму,
    без перебудови таблиці) - для мертвих колонок, залишених з попередніх версій."""
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column in existing:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _backup_pered_onovlennyam(stara_versiya):
    """Копіює поточний файл бази в резервну копію з номером старої версії й датою,
    ПЕРЕД тим як застосувати нові міграції. Стара копія лишається недоторканою назавжди -
    дані ніколи не втрачаються навіть якщо нова версія має проблему."""
    if not os.path.isfile(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return  # немає що резервувати - файл ще не існував чи порожній

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    versiya_dlya_nazvy = stara_versiya.replace(".", "_") if stara_versiya else "do-versiyuvannya"
    backup_path = os.path.join(
        os.path.dirname(DB_PATH), f"school_v{versiya_dlya_nazvy}_{timestamp}.db"
    )
    if not os.path.isfile(backup_path):
        shutil.copy2(DB_PATH, backup_path)


def _perevirka_versiyi_i_backup():
    """Перевіряє версію схеми, збережену в базі, і якщо вона відрізняється від поточної
    версії застосунку - робить резервну копію ПЕРЕД тим, як продовжити з міграціями."""
    # фіксуємо стан файлу ДО будь-яких дій - якщо він щойно створюється (порожній чи
    # не існує), резервувати нема чого, це перший запуск, а не оновлення версії
    buv_svizhyi = not os.path.isfile(DB_PATH) or os.path.getsize(DB_PATH) == 0

    conn = get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version TEXT)"
    )
    conn.commit()
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    stara_versiya = row["version"] if row else None
    conn.close()

    if not buv_svizhyi and stara_versiya != APP_VERSION:
        _backup_pered_onovlennyam(stara_versiya)

    return stara_versiya


def _zapysaty_versiyu():
    conn = get_connection()
    conn.execute(
        "INSERT INTO schema_version (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
        (APP_VERSION,),
    )
    conn.commit()
    conn.close()


# Звіт "нічого не втрачено" після оновлення версії / імпорту бекапу: рахуємо
# рядки в УСІХ таблицях даних ДО і ПІСЛЯ міграцій, щоб користувач бачив повну
# картину, а не вибірку. Перелік таблиць береться З САМОЇ БАЗИ (sqlite_master),
# а не з захардкодженого списку - інакше нову таблицю легко забути додати сюди,
# і саме вона мовчки випаде зі звіту.
_TEHNICHNI_TABLYTSI = {"schema_version", "sqlite_sequence"}


def _tablytsi_danyh(conn):
    """Імена таблиць даних у базі: без службових і без знімка _demo_master_*."""
    return sorted(
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if r["name"] not in _TEHNICHNI_TABLYTSI
        and not r["name"].startswith(DEMO_MASTER_PREFIX)
    )


def _polichyty_osnovni_tablytsi(conn):
    """Рахує рядки в кожній таблиці даних. Толерантно до того, що таблиці ще
    немає (стара база ДО міграції новіших таблиць не мала) - тоді None."""
    result = {}
    for tablytsya in _tablytsi_danyh(conn):
        try:
            result[tablytsya] = conn.execute(
                f"SELECT COUNT(*) AS c FROM {tablytsya}"
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            result[tablytsya] = None
    return result


def _zapysaty_zvit_pro_migratsiyu(stara_versiya, do, pislya):
    """Пише короткий JSON-звіт про останню міграцію версії бази - app.py показує
    його один раз користувачу (банер "базу оновлено, ось що перевірено") і видаляє
    файл, щоб не показувати повторно."""
    zvit_path = os.path.join(BASE_SUBDIR, "ostannya_migratsiya.json")
    try:
        with open(zvit_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "stara_versiya": stara_versiya,
                    "nova_versiya": APP_VERSION,
                    "chas": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "do": do,
                    "pislya": pislya,
                },
                f, ensure_ascii=False, indent=2,
            )
    except OSError:
        pass  # звіт - лише зручність для користувача, не критична дія


AVTO_BEKAP_PREFIX = "school_avto_"
AVTO_BEKAP_ZBERIGATY = 7   # скільки щоденних автокопій тримати


def avtobekap_pry_starti():
    """Щоденна автоматична копія бази при запуску застосунку (раз на день:
    якщо сьогоднішня вже є - нічого не робимо). Старіші за AVTO_BEKAP_ZBERIGATY
    автокопії видаляються - ЛИШЕ з префіксом school_avto_; ручні копії й копії
    перед міграцією не чіпаються ніколи. У демо-режимі не працює (демо - без
    цінних даних). Повертає ім'я створеного файлу або None."""
    if DEMO_REZHYM_AKTYVNYI or not os.path.isfile(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return None
    papka = os.path.dirname(DB_PATH)
    sohodni = datetime.now().strftime("%Y-%m-%d")
    versiya = APP_VERSION.replace(".", "_")
    nazva = f"{AVTO_BEKAP_PREFIX}v{versiya}_{sohodni}.db"
    shlyah = os.path.join(papka, nazva)
    if os.path.isfile(shlyah):
        return None
    try:
        shutil.copy2(DB_PATH, shlyah)
        stari = sorted(
            f for f in os.listdir(papka)
            if f.startswith(AVTO_BEKAP_PREFIX) and f.lower().endswith(".db")
        )
        for zayvyi in stari[:-AVTO_BEKAP_ZBERIGATY]:
            try:
                os.remove(os.path.join(papka, zayvyi))
            except OSError:
                pass
    except OSError:
        return None   # бекап - зручність; збій копіювання не має зупиняти запуск
    return nazva


def zapovnyty_pary_pravyla_defaultamy(conn, predmet_id=None):
    """Заповнює predmet_paralel_pravyla дефолтними значеннями (з JSON-довідника
    pary_dozvoleni_5_6/7_9) - для одного предмета (predmet_id) чи для всіх одразу
    (predmet_id=None). INSERT OR IGNORE - НЕ перезаписує вже наявні рядки (могли
    бути вручну скориговані завучем під конкретну школу)."""
    from mon_normy_loader import pary_dozvoleni_5_6, pary_dozvoleni_7_9
    dozv_5_6 = pary_dozvoleni_5_6()
    dozv_7_9 = pary_dozvoleni_7_9()

    if predmet_id is not None:
        predmety_rows = conn.execute("SELECT id, nazva FROM predmety WHERE id = ?", (predmet_id,)).fetchall()
    else:
        predmety_rows = conn.execute("SELECT id, nazva FROM predmety").fetchall()

    for p in predmety_rows:
        for paralel in range(1, 13):
            if paralel <= 4:
                dozvoleno = 0  # 1-4 класи - здвоєні уроки заборонені завжди
            elif paralel <= 6:
                dozvoleno = 1 if p["nazva"] in dozv_5_6 else 0
            elif paralel <= 9:
                dozvoleno = 1 if p["nazva"] in dozv_7_9 else 0
            else:
                dozvoleno = 1  # 10-12 класи - дозволено для всіх предметів
            conn.execute(
                "INSERT OR IGNORE INTO predmet_paralel_pravyla "
                "(predmet_id, paralel, dozvoleno_zdvoyeni_pary) VALUES (?, ?, ?)",
                (p["id"], paralel, dozvoleno),
            )
    conn.commit()


def init_db():
    stara_versiya = _perevirka_versiyi_i_backup()

    # Рахуємо ДО міграцій - лише якщо це справжнє оновлення версії (не перший
    # запуск і не той самий APP_VERSION), інакше рахувати нема сенсу.
    onovlennya_versiyi = stara_versiya is not None and stara_versiya != APP_VERSION
    if onovlennya_versiyi:
        conn_do = get_connection()
        lichylnyk_do = _polichyty_osnovni_tablytsi(conn_do)
        conn_do.close()

    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS klas_komplekty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL,
            klasnyi_kerivnyk_id INTEGER REFERENCES vchyteli(id)
        );

        CREATE TABLE IF NOT EXISTS klasy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paralel INTEGER NOT NULL,
            litera TEXT,
            kilkist_uchniv INTEGER NOT NULL DEFAULT 0,
            kilkist_chlopciv INTEGER DEFAULT 0,
            kilkist_divchat INTEGER DEFAULT 0,
            tip_klasu TEXT NOT NULL DEFAULT 'zvychajnyj' CHECK (tip_klasu IN ('zvychajnyj', 'komplekt')),
            komplekt_id INTEGER REFERENCES klas_komplekty(id) ON DELETE SET NULL,
            klasnyi_kerivnyk_id INTEGER REFERENCES vchyteli(id),
            domashniy_kabinet_id INTEGER,
            mova_navchannya TEXT,
            maks_urokiv_den INTEGER,
            maks_urokiv_den_z_fizkulturoyu INTEGER,
            hranychne_navantazhennya INTEGER,
            faktychne_navantazhennya INTEGER,
            UNIQUE (paralel, litera)
        );

        CREATE TABLE IF NOT EXISTS predmety (""" + _PREDMETY_STOVPTSI_SQL + """);

        CREATE TABLE IF NOT EXISTS predmet_paralel_pravyla (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
            paralel INTEGER NOT NULL,
            dozvoleno_zdvoyeni_pary INTEGER NOT NULL DEFAULT 0,
            UNIQUE (predmet_id, paralel)
        );

        CREATE TABLE IF NOT EXISTS pozaurochni_zanyattya (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL UNIQUE,
            typ TEXT NOT NULL DEFAULT 'inshe' CHECK (typ IN (
                'vyhovna', 'gpd', 'konsultatsiya', 'fakultatyv', 'gurtok',
                'korekciyno_rozvytkove', 'indyvidualne_navchannya', 'inshe'
            )),
            fiksovanyi_den TEXT
        );

        CREATE TABLE IF NOT EXISTS pozaurochnyj_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            zanyattya_id INTEGER NOT NULL REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            domashniy_kabinet_id INTEGER,
            godyn_tyzhden INTEGER NOT NULL DEFAULT 1,
            UNIQUE (klas_id, zanyattya_id)
        );

        CREATE TABLE IF NOT EXISTS kabinety (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL UNIQUE,
            mistkist INTEGER NOT NULL,
            typ_pryvyazky TEXT NOT NULL DEFAULT 'universalnyi'
                CHECK (typ_pryvyazky IN ('universalnyi', 'predmet', 'vchytel', 'kombinatsiya')),
            minimalna_mistkist INTEGER,
            korpus TEXT,
            poverh TEXT,
            priorytet INTEGER
        );

        CREATE TABLE IF NOT EXISTS kabinety_predmety (
            kabinet_id INTEGER NOT NULL REFERENCES kabinety(id) ON DELETE CASCADE,
            predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
            PRIMARY KEY (kabinet_id, predmet_id)
        );

        CREATE TABLE IF NOT EXISTS kabinety_vchyteli (
            kabinet_id INTEGER NOT NULL REFERENCES kabinety(id) ON DELETE CASCADE,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            PRIMARY KEY (kabinet_id, vchytel_id)
        );

        CREATE TABLE IF NOT EXISTS vchyteli (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pib TEXT NOT NULL,
            zakriplenyi_kabinet_id INTEGER REFERENCES kabinety(id) ON DELETE SET NULL,
            maksymum_urokiv_den INTEGER
        );

        CREATE TABLE IF NOT EXISTS asystenty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pib TEXT NOT NULL,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            stavka REAL NOT NULL DEFAULT 1.0,
            suprovid TEXT NOT NULL DEFAULT 'vsi' CHECK (suprovid IN ('vsi', 'okremi'))
        );

        CREATE TABLE IF NOT EXISTS asystenty_predmety (
            asystent_id INTEGER NOT NULL REFERENCES asystenty(id) ON DELETE CASCADE,
            predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
            PRIMARY KEY (asystent_id, predmet_id)
        );

        CREATE TABLE IF NOT EXISTS mizhpredmetni_grupy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            nazva TEXT NOT NULL
        );

        -- Базові "Групи" (профільна старша школа, 10-12 кл.) - віртуальні навчальні
        -- одиниці, складені з ЧАСТИН одного чи кількох реальних класів. profil_kod -
        -- ключ типового плану з json/mon_normy.json (typovi_navchalni_plany), НЕ FK
        -- (довідник у JSON, не в БД). variant - вільний текст (поки не формалізовано,
        -- див. PROJECT_OVERVIEW.md - можуть бути 2 незалежні АБО в одному профілі).
        -- nazva - згенерований код (10-МЛК-МЛ-1), унікальний.
        CREATE TABLE IF NOT EXISTS grupy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paralel INTEGER NOT NULL CHECK (paralel BETWEEN 10 AND 12),
            profil_kod TEXT NOT NULL,
            variant TEXT NOT NULL DEFAULT '1',
            nazva TEXT NOT NULL UNIQUE,
            aktyvna INTEGER NOT NULL DEFAULT 1
        );

        -- Джерело складу групи: з яких реальних класів і скільки учнів. Один клас
        -- може постачати учнів у кілька різних груп одночасно.
        CREATE TABLE IF NOT EXISTS grupy_klasy (
            grupa_id INTEGER NOT NULL REFERENCES grupy(id) ON DELETE CASCADE,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            kilkist_uchniv INTEGER NOT NULL CHECK (kilkist_uchniv > 0),
            PRIMARY KEY (grupa_id, klas_id)
        );

        -- "Профілі навчання" - власний навчальний план ГРУПИ (не спільний на профіль -
        -- 10-МЛК-МЛ-1 і 11-МЛК-МЛ-1 мають незалежні плани, різні роки/варіанти можуть
        -- мати різні години). Поки лише предмет+години, БЕЗ вчителя (на відміну від
        -- navchalnyj_plan класу) - призначення вчителя додамо окремим кроком пізніше.
        CREATE TABLE IF NOT EXISTS grupa_navchalnyj_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grupa_id INTEGER NOT NULL REFERENCES grupy(id) ON DELETE CASCADE,
            predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
            godyn_tyzhden INTEGER NOT NULL,
            UNIQUE (grupa_id, predmet_id)
        );

        CREATE TABLE IF NOT EXISTS navchalnyj_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            godyn_tyzhden INTEGER NOT NULL,
            zdvoyeni_pary INTEGER NOT NULL DEFAULT 0,
            maks_urokiv_den INTEGER NOT NULL DEFAULT 1,
            pidgrupa_2_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
            mizhpredmetna_grupa_id INTEGER REFERENCES mizhpredmetni_grupy(id) ON DELETE SET NULL,
            fiksovanyi_den INTEGER,
            parnist TEXT NOT NULL DEFAULT 'oboydva' CHECK (parnist IN ('oboydva', 'parnyi', 'neparnyi')),
            UNIQUE (klas_id, predmet_id, parnist)
        );

        CREATE TABLE IF NOT EXISTS teacher_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            den INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('nedostupnyi', 'nebazhanyi')),
            UNIQUE (vchytel_id, den, urok)
        );

        -- доступність класу/кабінету має ЛИШЕ один стан ("недоступний"), тож
        -- сам факт рядка = недоступність; колонки status тут немає з v0.0.37
        -- (у teacher_availability вона є - там ще "небажаний")
        CREATE TABLE IF NOT EXISTS class_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            den INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            UNIQUE (klas_id, den, urok)
        );

        CREATE TABLE IF NOT EXISTS kabinet_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kabinet_id INTEGER NOT NULL REFERENCES kabinety(id) ON DELETE CASCADE,
            den INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            UNIQUE (kabinet_id, den, urok)
        );

        CREATE TABLE IF NOT EXISTS ostannya_generatsiya (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            data_chas TEXT,
            success INTEGER,
            status TEXT,
            message TEXT,
            vseogo_urokiv INTEGER,
            kilkist_klasiv INTEGER,
            kilkist_vchyteliv INTEGER,
            kilkist_predmetiv INTEGER,
            unplaced_json TEXT,
            chas_sekund REAL
        );

        -- Збережені ВАРІАНТИ розкладу (v0.0.36). generated_urok - завжди один
        -- "поточний" розклад, який кожна генерація стирає; тут завуч тримає
        -- скільки завгодно названих знімків ("варіант 1 - від 3 вересня") і
        -- може повернути будь-який у поточний (далі догенерація/перегляд/PDF
        -- працюють від нього). pidsumok_json - знімок ostannya_generatsiya на
        -- момент збереження (статус, нерозміщені), відновлюється разом з уроками.
        -- aktyvnyi=1 - цей варіант зараз завантажено як поточний і відтоді
        -- розклад не перегенеровувався (будь-яка генерація/видалення скидає).
        CREATE TABLE IF NOT EXISTS rozklad_varianty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL,
            stvoreno TEXT NOT NULL,
            notatka TEXT,
            urokiv INTEGER NOT NULL DEFAULT 0,
            pidsumok_json TEXT,
            aktyvnyi INTEGER NOT NULL DEFAULT 0,
            diye_z TEXT                     -- 'РРРР-ММ-ДД': з якої дати цей варіант діяв (v0.0.37)
        );

        -- Уроки варіанта - копія рядків generated_urok. НАВМИСНО без зовнішніх
        -- ключів на klasy/vchyteli/predmety/kabinety: знімок - це історія, і
        -- видалення вчителя з довідника не повинно стирати з нього рядки. При
        -- завантаженні рядки з уже неіснуючими id відкидаються, і застосунок
        -- каже скільки (generated_urok свої FK має, тож туди вони не потраплять).
        CREATE TABLE IF NOT EXISTS rozklad_varianty_uroky (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id INTEGER NOT NULL REFERENCES rozklad_varianty(id) ON DELETE CASCADE,
            klas_id INTEGER NOT NULL,
            predmet_id INTEGER,
            pozaurochne_zanyattya_id INTEGER,
            vchytel_id INTEGER NOT NULL,
            kabinet_id INTEGER,
            den INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            parnist TEXT NOT NULL DEFAULT 'oboydva',
            pidgrupa_2_vchytel_id INTEGER
        );

        -- Ненавчальні дні навчального року: дні, коли школа не працювала, хоч
        -- за розкладом мала б. Заповнює завуч на окремій сторінці-календарі
        -- ("Налаштування школи" -> "Календар ненавчальних днів").
        --   typ = 'svyato'   - державне свято або перенесений день
        --   typ = 'karantyn' - карантин, морози (актовані дні), інше зупинення занять
        --   typ = 'robochyi' - НАВПАКИ: вихідний (звич. субота), який школа
        --                      відпрацьовує; za_den = за розкладом якого дня
        --                      тижня (1=Пн..6=Сб) вона працює
        -- Використовується у звіті "Залишки годин": уроки, що припали на такий
        -- день, НЕ вважаються проведеними - це борг учителя, як і скасований урок.
        -- Щоб додати ще один тип - досить нового значення typ і кольору в UI.
        CREATE TABLE IF NOT EXISTS nenavchalni_dni (
            data TEXT PRIMARY KEY,          -- 'РРРР-ММ-ДД'
            typ TEXT NOT NULL DEFAULT 'svyato',
            za_den INTEGER,                 -- лише для typ='robochyi': 1=Пн..6=Сб
            nazva TEXT
        );

        CREATE TABLE IF NOT EXISTS urok_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            predmet_id INTEGER REFERENCES predmety(id) ON DELETE CASCADE,
            pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
            planovanyi_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
            faktychnyi_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
            faktychnyi_predmet_id INTEGER REFERENCES predmety(id) ON DELETE SET NULL,
            faktychne_pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE SET NULL,
            urok_nomer INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'proveden' CHECK (status IN ('proveden', 'skasovano', 'zamina')),
            prychyna TEXT,
            UNIQUE (data, klas_id, urok_nomer)
        );

        CREATE TABLE IF NOT EXISTS generated_urok (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            predmet_id INTEGER REFERENCES predmety(id) ON DELETE CASCADE,
            pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            kabinet_id INTEGER REFERENCES kabinety(id) ON DELETE SET NULL,
            den INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            parnist TEXT NOT NULL DEFAULT 'oboydva' CHECK (parnist IN ('oboydva', 'parnyi', 'neparnyi'))
        );

        CREATE TABLE IF NOT EXISTS school_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dniv_na_tyzhden INTEGER NOT NULL DEFAULT 5,
            urokiv_na_den INTEGER NOT NULL DEFAULT 8,
            mova_navchannya TEXT NOT NULL DEFAULT 'Українська',
            druga_zmina INTEGER NOT NULL DEFAULT 0,
            semestr1_pochatok TEXT,
            semestr1_kinets TEXT,
            semestr2_pochatok TEXT,
            semestr2_kinets TEXT,
            kanikuly1_e INTEGER NOT NULL DEFAULT 0,
            kanikuly1_pochatok TEXT,
            kanikuly1_kinets TEXT,
            kanikuly2_e INTEGER NOT NULL DEFAULT 0,
            kanikuly2_pochatok TEXT,
            kanikuly2_kinets TEXT,
            tip_miscevosti TEXT NOT NULL DEFAULT 'miska' CHECK (tip_miscevosti IN ('miska', 'silska')),
            kilkist_navchalnyh_tyzhniv INTEGER NOT NULL DEFAULT 35,
            -- НОВА база стартує як "1-11 стара школа": це найпоширеніший тип
            -- української школи і єдиний повністю підтримуваний. Дефолт
            -- 'povnyi_liczei' (1-12) після блокування рівня "Ліцей" давав би
            -- новій школі одразу жовте попередження й класи 12, що випадають
            -- поза заклад. Наявні бази не чіпаються - у них колонка вже є.
            tip_zakladu TEXT NOT NULL DEFAULT 'stara_shkola',
            fizychni_yadra INTEGER,
            vikna_vchyteliv INTEGER NOT NULL DEFAULT 0   -- генератор уникає вікон у вчителів (v0.0.37, типово вимкнено)
        );

        CREATE TABLE IF NOT EXISTS dzvinky (
            zmina INTEGER NOT NULL,
            urok INTEGER NOT NULL,
            pochatok TEXT,
            kinets TEXT,
            PRIMARY KEY (zmina, urok)
        );
        """
    )

    # Міграція: окреме поле "Корпус" (школи з кількома будівлями)
    _ensure_column(conn, "kabinety", "korpus", "TEXT")

    # Міграція: кількість навчальних тижнів у році (для конвертації річних годин у тижневі)
    _ensure_column(conn, "school_settings", "kilkist_navchalnyh_tyzhniv", "INTEGER NOT NULL DEFAULT 35")

    # Міграція: дати семестрів (для автоматичного розрахунку кількості навчальних тижнів)
    _ensure_column(conn, "school_settings", "semestr1_pochatok", "TEXT")
    _ensure_column(conn, "school_settings", "semestr1_kinets", "TEXT")
    _ensure_column(conn, "school_settings", "semestr2_pochatok", "TEXT")
    _ensure_column(conn, "school_settings", "semestr2_kinets", "TEXT")

    # Міграція: канікули всередині семестрів (опційно - галочка як у другої зміни,
    # бо не в усіх закладах вони чітко вписані в дати чи взагалі відомі заздалегідь)
    _ensure_column(conn, "school_settings", "kanikuly1_e", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "school_settings", "kanikuly1_pochatok", "TEXT")
    _ensure_column(conn, "school_settings", "kanikuly1_kinets", "TEXT")
    _ensure_column(conn, "school_settings", "kanikuly2_e", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "school_settings", "kanikuly2_pochatok", "TEXT")
    _ensure_column(conn, "school_settings", "kanikuly2_kinets", "TEXT")

    # Міграція: тип закладу (початкова/гімназія/ліцей та їхні комбінації) - визначає
    # реальний діапазон паралелей і дозволяє генерувати розклад окремо по рівнях
    # (різна тривалість уроку 35/40/45 хв робить єдину наскрізну генерацію по всій
    # школі проблематичною для вчителів, що працюють у кількох рівнях одночасно)
    _ensure_column(conn, "school_settings", "tip_zakladu", "TEXT NOT NULL DEFAULT 'stara_shkola'")

    # Міграція: тип місцевості школи (впливає на пороги поділу з наказу МОН №128)
    _ensure_column(conn, "school_settings", "tip_miscevosti", "TEXT NOT NULL DEFAULT 'miska'")

    # Міграція: предмет завжди останнім уроком дня (напр. виховна година) - відносно
    # до кожного класу окремо, бо максимум уроків/день різний для різних паралелей
    _ensure_column(conn, "predmety", "ostannim_urokom", "INTEGER NOT NULL DEFAULT 0")

    # Міграція: рівень складності предмета (гігієнічна шкала важкості за орієнтиром
    # ДСанПіН 5.5.2.008-01, Додаток 6 - досі використовується методистами як науково
    # обґрунтований орієнтир, хоча формально документ втратив чинність)
    _ensure_column(conn, "predmety", "vazhkist", "TEXT NOT NULL DEFAULT 'serednii'")

    # Міграція: граничне допустиме навантаження учня (окремо від фактичного розкладу -
    # фізкультура НЕ входить у цей ліміт, хоча фізично займає слоти в розкладі)
    _ensure_column(conn, "klasy", "hranychne_navantazhennya", "INTEGER")
    _ensure_column(conn, "klasy", "faktychne_navantazhennya", "INTEGER")
    _ensure_column(conn, "klasy", "maks_urokiv_den_z_fizkulturoyu", "INTEGER")
    _ensure_column(conn, "klasy", "komplekt_id", "INTEGER REFERENCES klas_komplekty(id) ON DELETE SET NULL")
    _ensure_column(conn, "klas_komplekty", "klasnyi_kerivnyk_id", "INTEGER REFERENCES vchyteli(id)")
    _ensure_column(conn, "klasy", "klasnyi_kerivnyk_id", "INTEGER REFERENCES vchyteli(id)")

    # Міграція: urok_log раніше вимагав predmet_id NOT NULL - неможливо було
    # зберегти скасування/заміну позаурочного уроку (виховна тощо), бо в нього
    # predmet_id завжди NULL (є pozaurochne_zanyattya_id натомість). SQLite не
    # підтримує зняття NOT NULL через ALTER TABLE - перебудовуємо таблицю, ЛИШЕ
    # якщо стара версія (predmet_id усе ще NOT NULL) виявлена.
    urok_log_info = conn.execute("PRAGMA table_info(urok_log)").fetchall()
    predmet_id_stovpets = next((c for c in urok_log_info if c["name"] == "predmet_id"), None)
    if predmet_id_stovpets is not None and predmet_id_stovpets["notnull"] == 1:
        conn.execute(
            """
            CREATE TABLE urok_log_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
                predmet_id INTEGER REFERENCES predmety(id) ON DELETE CASCADE,
                pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
                planovanyi_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
                faktychnyi_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
                faktychnyi_predmet_id INTEGER REFERENCES predmety(id) ON DELETE SET NULL,
                faktychne_pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE SET NULL,
                urok_nomer INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'proveden' CHECK (status IN ('proveden', 'skasovano', 'zamina')),
                prychyna TEXT,
                UNIQUE (data, klas_id, urok_nomer)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO urok_log_new
                (id, data, klas_id, predmet_id, planovanyi_vchytel_id, faktychnyi_vchytel_id,
                 faktychnyi_predmet_id, urok_nomer, status, prychyna)
            SELECT id, data, klas_id, predmet_id, planovanyi_vchytel_id, faktychnyi_vchytel_id,
                   faktychnyi_predmet_id, urok_nomer, status, prychyna
            FROM urok_log
            """
        )
        conn.execute("DROP TABLE urok_log")
        conn.execute("ALTER TABLE urok_log_new RENAME TO urok_log")
    _ensure_column(conn, "predmety", "osvitnya_galuz", "TEXT")
    _ensure_column(conn, "predmety", "vrahovuyetsya_u_hranychnomu", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "school_settings", "fizychni_yadra", "INTEGER")

    # Міграція: стабільний код предмета (normovanyi_kod) - НЕ залежить від того, як
    # завуч перейменує предмет в інтерфейсі. NULLABLE навмисно (не NOT NULL) - у
    # наявних школах уже є реальні предмети, і колонка без дефолту на існуючу таблицю
    # або впала б на міграції, або мовчки проставила порожній рядок усім. Замість
    # цього - додаємо nullable, і ОДИН РАЗ (лише якщо колонка щойно з'явилась)
    # намагаємось підставити код за точним збігом назви зі словником типового
    # переліку МОН. Що не збіглось (кастомні назви предметів) - лишається NULL,
    # завуч довизначить вручну через випадаючий список в інтерфейсі.
    existing_cols_predmety = [row["name"] for row in conn.execute("PRAGMA table_info(predmety)")]
    normovanyi_kod_shchoyno_dodano = "normovanyi_kod" not in existing_cols_predmety
    _ensure_column(conn, "predmety", "normovanyi_kod", "TEXT")
    if normovanyi_kod_shchoyno_dodano:
        from mon_normy_loader import normovani_kody_predmetiv
        slovnyk = normovani_kody_predmetiv()
        for nazva, kod in slovnyk.items():
            conn.execute(
                "UPDATE predmety SET normovanyi_kod = ? WHERE nazva = ? AND normovanyi_kod IS NULL",
                (kod, nazva),
            )
        conn.commit()

    # Міграція: predmet_paralel_pravyla - заповнюємо дефолтами ОДИН РАЗ для вже
    # наявних предметів (лише якщо таблиця щойно з'явилась, тобто порожня одразу
    # після CREATE TABLE - на оновлюваній школі, не на свіжій базі без предметів).
    pravyla_porozhnya = conn.execute("SELECT COUNT(*) c FROM predmet_paralel_pravyla").fetchone()["c"] == 0
    if pravyla_porozhnya:
        zapovnyty_pary_pravyla_defaultamy(conn)

    # Міграція: generated_urok мусить підтримувати і навчальні уроки (predmet_id), і
    # позаурочні заняття (pozaurochne_zanyattya_id) - потрібно зняти NOT NULL з
    # predmet_id, а SQLite не дозволяє ALTER COLUMN, тож перестворюємо таблицю. Дані
    # тут завжди regenerable (це "кеш" останньої генерації, не первинні дані), тож
    # втрата при міграції не критична - завуч просто перегенерує розклад.
    existing_cols_gu = [row["name"] for row in conn.execute("PRAGMA table_info(generated_urok)")]
    if existing_cols_gu and "pozaurochne_zanyattya_id" not in existing_cols_gu:
        conn.executescript(
            """
            DROP TABLE IF EXISTS generated_urok;
            CREATE TABLE generated_urok (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
                predmet_id INTEGER REFERENCES predmety(id) ON DELETE CASCADE,
                pozaurochne_zanyattya_id INTEGER REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
                vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
                kabinet_id INTEGER REFERENCES kabinety(id) ON DELETE SET NULL,
                den INTEGER NOT NULL,
                urok INTEGER NOT NULL,
                parnist TEXT NOT NULL DEFAULT 'oboydva' CHECK (parnist IN ('oboydva', 'parnyi', 'neparnyi'))
            );
            """
        )

    # Міграція: позаурочні заняття (виховна, ГПД, консультації, факультативи) - окрема
    # сутність від навчальних предметів, НЕ входить у денний ліміт уроків, але так само
    # перевіряється на конфлікт вчителя/кабінету/класу
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pozaurochni_zanyattya (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nazva TEXT NOT NULL UNIQUE,
            typ TEXT NOT NULL DEFAULT 'inshe' CHECK (typ IN (
                'vyhovna', 'gpd', 'konsultatsiya', 'fakultatyv', 'gurtok',
                'korekciyno_rozvytkove', 'indyvidualne_navchannya', 'inshe'
            )),
            fiksovanyi_den TEXT
        );

        CREATE TABLE IF NOT EXISTS pozaurochnyj_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
            zanyattya_id INTEGER NOT NULL REFERENCES pozaurochni_zanyattya(id) ON DELETE CASCADE,
            vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
            domashniy_kabinet_id INTEGER,
            godyn_tyzhden INTEGER NOT NULL DEFAULT 1,
            UNIQUE (klas_id, zanyattya_id)
        );
        """
    )

    # Міграція: другий вчитель для підгрупи (поділ класу на 2 паралельні групи
    # в тому самому слоті розкладу - напр. дві мовні підгрупи одночасно)
    _ensure_column(conn, "navchalnyj_plan", "pidgrupa_2_vchytel_id", "INTEGER")

    # Міграція: міжпредметний поділ - різні предмети паралельно для підгруп того самого
    # класу (напр. половина класу на інформатиці, половина на іноземній, потім чергування)
    _ensure_column(conn, "navchalnyj_plan", "mizhpredmetna_grupa_id", "INTEGER")

    # Міграція: закріплений день тижня ОКРЕМО для цього класу (перевизначає
    # predmety.fiksovanyi_den, який спільний для всіх класів) - напр. Захист України
    # у 10 класі щочетверга, а в 11 класі щоп'ятниці, той самий предмет
    _ensure_column(conn, "navchalnyj_plan", "fiksovanyi_den", "INTEGER")

    # Міграція: фактично проведений предмет при заміні може відрізнятись від планового
    # (напр. хворіє математик, замість уроку математики провели українську мову)
    _ensure_column(conn, "urok_log", "faktychnyi_predmet_id", "INTEGER")

    # Міграція: мова навчання класу (nullable перевизначення дефолту школи)
    _ensure_column(conn, "klasy", "mova_navchannya", "TEXT")

    # Міграція: індивідуальний ліміт уроків/день для класу (різний для різних паралелей
    # за рекомендаціями МОН - 1 клас 4, старші класи до 7)
    _ensure_column(conn, "klasy", "maks_urokiv_den", "INTEGER")

    # Історична примітка: тут раніше було ALTER TABLE klasy DROP COLUMN klasnyi_kerivnyk_id
    # (рішення "відкладено"). Тепер поле повернуто свідомо - потрібне для узгодженості
    # з клас-комплектами (там воно теж є) і для генератора клас-комплектів, який
    # призначає цього вчителя на класні предмети всім членам групи.

    # Міграція: "Тиждень парності" (parnist) в доступності вчителя/класу/кабінету і
    # "Режим навчання" (schedule_mode, дистанційно) прибрані - обидва були лише в базі
    # й UI, генератор їх ніколи не читав (перевірено: 0 реальних записів з парністю
    # відмінною від 'oboydva', 0 записів schedule_mode на момент рішення). Розклад тут
    # не динамічний (генерується раз на семестр), критичні винятки закриваються
    # "Замінами" - двотижнева парність визнана зайвою складністю, а не бажаною фічею.
    conn.execute("DROP TABLE IF EXISTS schedule_mode")

    if "parnist" in [row["name"] for row in conn.execute("PRAGMA table_info(teacher_availability)")]:
        conn.executescript(
            """
            CREATE TABLE teacher_availability_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
                den INTEGER NOT NULL,
                urok INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('nedostupnyi', 'nebazhanyi')),
                UNIQUE (vchytel_id, den, urok)
            );
            INSERT OR IGNORE INTO teacher_availability_new (id, vchytel_id, den, urok, status)
            SELECT id, vchytel_id, den, urok, status FROM teacher_availability;
            DROP TABLE teacher_availability;
            ALTER TABLE teacher_availability_new RENAME TO teacher_availability;
            """
        )

    if "parnist" in [row["name"] for row in conn.execute("PRAGMA table_info(class_availability)")]:
        conn.executescript(
            """
            CREATE TABLE class_availability_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
                den INTEGER NOT NULL,
                urok INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('nedostupnyi')),
                UNIQUE (klas_id, den, urok)
            );
            INSERT OR IGNORE INTO class_availability_new (id, klas_id, den, urok, status)
            SELECT id, klas_id, den, urok, status FROM class_availability;
            DROP TABLE class_availability;
            ALTER TABLE class_availability_new RENAME TO class_availability;
            """
        )

    if "parnist" in [row["name"] for row in conn.execute("PRAGMA table_info(kabinet_availability)")]:
        conn.executescript(
            """
            CREATE TABLE kabinet_availability_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kabinet_id INTEGER NOT NULL REFERENCES kabinety(id) ON DELETE CASCADE,
                den INTEGER NOT NULL,
                urok INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('nedostupnyi')),
                UNIQUE (kabinet_id, den, urok)
            );
            INSERT OR IGNORE INTO kabinet_availability_new (id, kabinet_id, den, urok, status)
            SELECT id, kabinet_id, den, urok, status FROM kabinet_availability;
            DROP TABLE kabinet_availability;
            ALTER TABLE kabinet_availability_new RENAME TO kabinet_availability;
            """
        )

    # Міграція: парність тижня для дробового навантаження (0.5 год/тиждень = повний
    # урок раз на два тижні). generated_urok.parnist - просте додавання колонки (нема
    # UNIQUE, який заважав би). navchalnyj_plan.parnist вимагає перебудови таблиці -
    # дозволяємо кілька рядків плану на (клас, предмет), що відрізняються парністю
    # (UNIQUE був (klas_id, predmet_id), стає (klas_id, predmet_id, parnist)).
    _ensure_column(conn, "generated_urok", "parnist", "TEXT NOT NULL DEFAULT 'oboydva'")

    # Міграція: підгрупа-2 на ТОГО САМОГО вчителя - рідкісний випадок, коли клас
    # ділиться на 2 підгрупи одного предмета, але вчитель на обидві один. Не можна
    # ставити паралельно (той самий вчитель фізично не може вести дві підгрупи
    # одночасно) - тому генератор планує лише ОДНУ (підгрупа 2, як реальний урок),
    # а підгрупа 1 - суто інформаційний допис "0 урок" під клітинкою (не
    # генерується/не перевіряється). Позначку зберігаємо на самому уроці, щоб усі
    # відображення (розклад/PDF) могли її показати без додаткового JOIN на план.
    _ensure_column(conn, "generated_urok", "pidgrupa_2_vchytel_id", "INTEGER")

    existing_cols_np = [row["name"] for row in conn.execute("PRAGMA table_info(navchalnyj_plan)")]
    if "parnist" not in existing_cols_np:
        conn.executescript(
            """
            CREATE TABLE navchalnyj_plan_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE,
                predmet_id INTEGER NOT NULL REFERENCES predmety(id) ON DELETE CASCADE,
                vchytel_id INTEGER NOT NULL REFERENCES vchyteli(id) ON DELETE CASCADE,
                godyn_tyzhden INTEGER NOT NULL,
                zdvoyeni_pary INTEGER NOT NULL DEFAULT 0,
                maks_urokiv_den INTEGER NOT NULL DEFAULT 1,
                pidgrupa_2_vchytel_id INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL,
                mizhpredmetna_grupa_id INTEGER REFERENCES mizhpredmetni_grupy(id) ON DELETE SET NULL,
                fiksovanyi_den INTEGER,
                parnist TEXT NOT NULL DEFAULT 'oboydva' CHECK (parnist IN ('oboydva', 'parnyi', 'neparnyi')),
                UNIQUE (klas_id, predmet_id, parnist)
            );
            INSERT INTO navchalnyj_plan_new
                (id, klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary,
                 maks_urokiv_den, pidgrupa_2_vchytel_id, mizhpredmetna_grupa_id, fiksovanyi_den)
            SELECT id, klas_id, predmet_id, vchytel_id, godyn_tyzhden, zdvoyeni_pary,
                   maks_urokiv_den, pidgrupa_2_vchytel_id, mizhpredmetna_grupa_id, fiksovanyi_den
            FROM navchalnyj_plan;
            DROP TABLE navchalnyj_plan;
            ALTER TABLE navchalnyj_plan_new RENAME TO navchalnyj_plan;
            """
        )

    # Міграція: яка парність вважається "тижнем 1" навчального року - рішення закладу,
    # не фіксоване. Канікули НЕ зсувають лічильник - парність рахується за реальним
    # календарним тижнем від semestr1_pochatok, суцільно, без пауз.
    _ensure_column(conn, "school_settings", "pershyi_tyzhden_neparnyi", "INTEGER NOT NULL DEFAULT 1")

    # Міграція: закріплений день тижня ОКРЕМО для цього класу (перевизначає
    # pozaurochni_zanyattya.fiksovanyi_den, який спільний для всіх класів) - той самий
    # патерн, що й navchalnyj_plan.fiksovanyi_den для звичайних предметів. Напр.
    # "Виховна година" щовівторка в одному класі, але щосереди в іншому.
    _ensure_column(conn, "pozaurochnyj_plan", "fiksovanyi_den", "TEXT")

    # Міграція: розширення переліку типів позаурочних занять - корекційно-розвиткові
    # (логопед/психолог/дефектолог для дітей з ООП) та індивідуальне навчання
    # (педагогічний патронаж) не входили в старий CHECK. SQLite не вміє змінювати
    # CHECK на льоту - перебудовуємо таблицю. FK ТИМЧАСОВО вимикаємо на час
    # DROP TABLE - інакше, з увімкненим PRAGMA foreign_keys, SQLite виконує
    # неявний ON DELETE CASCADE і стирає всі пов'язані записи в pozaurochnyj_plan/
    # urok_log/generated_urok ще ДО перейменування нової таблиці.
    zanyattya_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pozaurochni_zanyattya'"
    ).fetchone()
    if zanyattya_sql and "korekciyno_rozvytkove" not in zanyattya_sql["sql"]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            CREATE TABLE pozaurochni_zanyattya_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nazva TEXT NOT NULL UNIQUE,
                typ TEXT NOT NULL DEFAULT 'inshe' CHECK (typ IN (
                    'vyhovna', 'gpd', 'konsultatsiya', 'fakultatyv', 'gurtok',
                    'korekciyno_rozvytkove', 'indyvidualne_navchannya', 'inshe'
                )),
                fiksovanyi_den TEXT
            );
            INSERT INTO pozaurochni_zanyattya_new (id, nazva, typ, fiksovanyi_den)
            SELECT id, nazva, typ, fiksovanyi_den FROM pozaurochni_zanyattya;
            DROP TABLE pozaurochni_zanyattya;
            ALTER TABLE pozaurochni_zanyattya_new RENAME TO pozaurochni_zanyattya;
            """
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    # Міграція: видаляємо мертву колонку speckabinet_typ (предмети й позаурочні
    # заняття) - генератор ніколи її не використовував, реальна прив'язка кабінетів
    # до предметів іде через kabinety_predmety, це поле лише дублювало ту саму
    # інформацію текстом. DROP COLUMN безпечний з SQLite 3.35+, дані інших полів не чіпає.
    _drop_column_if_exists(conn, "predmety", "speckabinet_typ")
    _drop_column_if_exists(conn, "pozaurochni_zanyattya", "speckabinet_typ")

    # Міграція: видаляємо мертві поля ООП по рівнях підтримки на класі - жодного
    # зв'язку з "Асистентами" (ті призначаються повністю вручну, незалежно від цих
    # цифр), і генератор їх ніде не використовує - лише довідкова цифра в списку
    # класів, яку вирішено прибрати.
    _drop_column_if_exists(conn, "klasy", "oop_riven_4_5")
    _drop_column_if_exists(conn, "klasy", "oop_riven_3")
    _drop_column_if_exists(conn, "klasy", "oop_riven_2")
    _drop_column_if_exists(conn, "klasy", "oop_riven_1")

    # Міграція: видаляємо мертві поля старого механізму поділу на підгрупи (поріг,
    # мінімум/максимум підгруп, поділ за статтю) - форма їх більше не читає й не
    # пише з часу переходу на нормативну таблицю norma_podilu_na_pidgrupy
    # (mon_normy_loader), керовану через normovanyi_kod. Аудитом підтверджено:
    # жодних SELECT/INSERT/UPDATE цих полів більше немає ніде в коді.
    _drop_column_if_exists(conn, "predmety", "podil_porih")
    _drop_column_if_exists(conn, "predmety", "min_u_pidgrupi")
    _drop_column_if_exists(conn, "predmety", "maks_pidgrup")
    _drop_column_if_exists(conn, "predmety", "podil_za_stattyu")

    # Міграція: 3 прапорці рівня (Початкова/Гімназія/Ліцей) на вчителі й кабінеті -
    # захист від помилки призначення в багаторівневій школі, коротший список у
    # формах, компактні колонки у списках. За замовчуванням усі три увімкнені
    # (доступний для всіх рівнів) - звужує сам користувач вручну, ніде мовчки
    # не звужується. Використовується лише коли в закладу активовано 2+ рівні
    # (tip_zakladu_options) - для однорівневої школи чи "1-11 стара школа"
    # ці поля існують, але ніде не показуються й не читаються.
    _ensure_column(conn, "vchyteli", "riven_pochatkova", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "vchyteli", "riven_gimnaziya", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "vchyteli", "riven_liceum", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "kabinety", "riven_pochatkova", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "kabinety", "riven_gimnaziya", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "kabinety", "riven_liceum", "INTEGER NOT NULL DEFAULT 1")

    # Міграція: вчитель у плані групи ("Профілі навчання") - НЕ NOT NULL (на відміну
    # від navchalnyj_plan класу), бо таблиця вже має реальні рядки без вчителя -
    # обов'язковість перевіряємо лише на рівні форми для нових/редагованих записів,
    # старі рядки без вчителя лишаються робочими (позначені "—" у списку) до
    # ручного редагування. Тарифікація/Матриця вчителя - НЕ враховують ці години
    # поки що (свідомо відкладено, окремий крок).
    _ensure_column(conn, "grupa_navchalnyj_plan", "vchytel_id", "INTEGER REFERENCES vchyteli(id) ON DELETE SET NULL")

    # Міграція: прапорець "активне" на позаурочному занятті. Вимкнене заняття
    # генератор ПОВНІСТЮ пропускає (усі рядки позаурочного плану цього типу) - для
    # швидкої перевірки генерації без частини позаурочки, без видалення записів.
    _ensure_column(conn, "pozaurochni_zanyattya", "aktyvne", "INTEGER NOT NULL DEFAULT 1")

    # Міграція: "спецкабінет потрібен лише 1-й підгрупі поділу". Для предметів, де
    # спеціальне приміщення - радше зручність, ніж обладнання (мови): при поділі
    # класу 1-ша підгрупа йде у спецкабінет, 2-га - у домашній клас або будь-який
    # вільний універсальний кабінет; нерозділений урок - у домашній клас. Так
    # школі достатньо ОДНОГО мовного кабінету. За замовчуванням 0 (спецкабінет для
    # всього предмета й обох підгруп - як для інформатики/фізкультури/майстерні).
    _ensure_column(conn, "predmety", "spets_kabinet_lyshe_pidgrupa1", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "nenavchalni_dni", "typ", "TEXT NOT NULL DEFAULT 'svyato'")
    _ensure_column(conn, "nenavchalni_dni", "za_den", "INTEGER")
    conn.execute("DROP TABLE IF EXISTS svyatkovi_dni")   # проміжна назва, у релізі не була

    # Міграція v0.0.36: перебудова predmety - CHECK на podil_pravylo досі допускав
    # історичне 'zavzhdy' (UI й код знають лише avto/nikoly), а дефолт 'avto'
    # розходився з формою створення ('nikoly'). SQLite не вміє змінювати ні
    # CHECK, ні DEFAULT на льоту - перестворюємо таблицю за документованою
    # процедурою (sqlite.org/lang_altertable.html, "Making Other Kinds Of Table
    # Schema Changes"). Робиться РІВНО ОДИН РАЗ - лише поки в DDL таблиці ще є
    # 'zavzhdy'. Копіюємо спільні стовпці (на дуже старій базі частини колонок
    # могло не бути - вони дістануть DEFAULT нової таблиці).
    #
    # НЕБЕЗПЕКА, заради якої тут запобіжники: на predmety посилаються 8 таблиць
    # (навчальний план, прив'язки кабінетів, журнал замін, розклад...). Якщо
    # DROP TABLE predmety виконати з увімкненими foreign_keys, SQLite зробить
    # неявний DELETE усіх рядків і КАСКАДНО зітре ці 8 таблиць. Тому FK
    # вимикаємо явно, ПЕРЕВІРЯЄМО, що вимкнулись (PRAGMA всередині відкритої
    # транзакції мовчки ігнорується - тому спершу commit), а після перебудови
    # звіряємо foreign_key_check.
    predmety_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'predmety'"
    ).fetchone()
    if predmety_ddl and "'zavzhdy'" in predmety_ddl["sql"]:
        # 'zavzhdy' ("завжди ділити") найближче до 'avto' - але в реальних базах
        # таких рядків не було; UPDATE лише страхує CHECK нової таблиці
        conn.execute("UPDATE predmety SET podil_pravylo = 'avto' WHERE podil_pravylo = 'zavzhdy'")
        conn.commit()
        # порушення FK, що були в базі ЩЕ ДО перебудови (реальний випадок: у базі
        # 0.0.19 два сирітські рядки asystenty_predmety з часів до увімкнення FK) -
        # не наша справа й не привід зупиняти міграцію. Ловимо лише НОВІ.
        porushennya_do = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
        conn.execute("PRAGMA foreign_keys = OFF")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError(
                "Міграція predmety: не вдалося вимкнути foreign_keys - перебудову "
                "скасовано, щоб не стерти каскадом навчальний план."
            )
        stari_kolonky = [r["name"] for r in conn.execute("PRAGMA table_info(predmety)")]
        novi_kolonky = [
            riadok.strip().split()[0]
            for riadok in _PREDMETY_STOVPTSI_SQL.strip().splitlines()
        ]
        spilni = ", ".join(k for k in stari_kolonky if k in novi_kolonky)
        conn.executescript(
            f"""
            CREATE TABLE predmety_new ({_PREDMETY_STOVPTSI_SQL});
            INSERT INTO predmety_new ({spilni}) SELECT {spilni} FROM predmety;
            DROP TABLE predmety;
            ALTER TABLE predmety_new RENAME TO predmety;
            """
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        porushennya_pislya = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
        novi_porushennya = porushennya_pislya - porushennya_do
        if novi_porushennya:
            raise RuntimeError(
                f"Міграція predmety: перебудова додала {len(novi_porushennya)} нових "
                f"порушень foreign_key_check - перевірте базу (бекап зроблено перед міграцією)."
            )

    # Міграція v0.0.37: "діє з" у варіанта розкладу - звіти, яким потрібен розклад
    # на конкретну дату (втрачено/відпрацьовано, заміни), беруть варіант із
    # найпізнішою датою <= дати; без дат - поточний розклад, як і раніше.
    _ensure_column(conn, "rozklad_varianty", "diye_z", "TEXT")
    # Міграція v0.0.37: перемикач "уникати вікон у вчителів" (м'який штраф у солвері).
    # На демо 27 класів: вікон вчителів удвічі менше, але модель більша, і на
    # коротких прогонах помітно більше нерозміщених (60 с: 18 -> 31; 10 хв: 0 -> 1-2).
    # Тому ТИПОВО ВИМКНЕНО - школа вмикає на фінальному 10-хвилинному прогоні,
    # коли розклад для класів уже чистий, і вимикає назад, якщо з'явились нерозміщені.
    _ensure_column(conn, "school_settings", "vikna_vchyteliv", "INTEGER NOT NULL DEFAULT 0")

    # Міграція v0.0.37: мертва таблиця taryfikatsiya - звіт "Тарифікація" завжди
    # рахувався з навчального плану, у таблицю ніхто ніколи не писав; вона лише
    # плутала в банері міграції ("Тарифікація (збережені рядки)") і в очищенні.
    conn.execute("DROP TABLE IF EXISTS taryfikatsiya")

    # Міграція v0.0.37: мертва колонка status у class_availability і
    # kabinet_availability (єдине значення 'nedostupnyi'; генератор дивився лише
    # на факт рядка). SQLite не дозволяє DROP COLUMN колонки з CHECK - таблиці
    # перебудовуються (вони без дочірніх, тож FK-каскаду нема чого боятись).
    for tablytsya, batko in (("class_availability", "klas_id INTEGER NOT NULL REFERENCES klasy(id) ON DELETE CASCADE"),
                             ("kabinet_availability", "kabinet_id INTEGER NOT NULL REFERENCES kabinety(id) ON DELETE CASCADE")):
        kolonky = [r["name"] for r in conn.execute(f"PRAGMA table_info({tablytsya})")]
        if "status" in kolonky:
            klyuch = batko.split()[0]
            conn.executescript(
                f"""
                CREATE TABLE {tablytsya}_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    {batko},
                    den INTEGER NOT NULL,
                    urok INTEGER NOT NULL,
                    UNIQUE ({klyuch}, den, urok)
                );
                INSERT OR IGNORE INTO {tablytsya}_new (id, {klyuch}, den, urok)
                    SELECT id, {klyuch}, den, urok FROM {tablytsya};
                DROP TABLE {tablytsya};
                ALTER TABLE {tablytsya}_new RENAME TO {tablytsya};
                """
            )

    conn.commit()

    # Гарантуємо наявність єдиного рядка налаштувань школи (id=1)
    conn.execute(
        "INSERT OR IGNORE INTO school_settings (id, dniv_na_tyzhden, urokiv_na_den, mova_navchannya, druga_zmina, kilkist_navchalnyh_tyzhniv) "
        "VALUES (1, 5, 8, 'Українська', 0, 35)"
    )
    conn.commit()

    # Визначаємо кількість фізичних ядер ОДИН РАЗ (лише якщо ще не визначено раніше) -
    # PowerShell-виклик має помітну затримку (~0.1-0.5с), тож не варто робити це при
    # кожному запуску застосунку чи, тим паче, при кожній генерації розкладу.
    potribno_vyznachyty = conn.execute(
        "SELECT fizychni_yadra FROM school_settings WHERE id = 1"
    ).fetchone()["fizychni_yadra"] is None
    if potribno_vyznachyty:
        yadra = _vyznachyty_fizychni_yadra()
        conn.execute("UPDATE school_settings SET fizychni_yadra = ? WHERE id = 1", (yadra,))
        conn.commit()

    # v0.0.15: типові навчальні плани переїхали в json/mon_normy.json (лише
    # перегляд, без редагування через UI) - таблиця БД більше не потрібна.
    conn.execute("DROP TABLE IF EXISTS typovi_plany_riadky")
    conn.commit()

    if onovlennya_versiyi:
        lichylnyk_pislya = _polichyty_osnovni_tablytsi(conn)
        _zapysaty_zvit_pro_migratsiyu(stara_versiya, lichylnyk_do, lichylnyk_pislya)

    conn.close()

    _zapysaty_versiyu()


if __name__ == "__main__":
    init_db()
    print(f"Базу даних ініціалізовано: {DB_PATH}")
