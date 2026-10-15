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

"""
Генератор розкладу уроків на основі Google OR-Tools CP-SAT.

Враховує:
  - клас / вчитель / кабінет без накладок в одному слоті
  - доступність вчителя, класу, кабінету (жорстка "недоступний", м'яка "небажаний")
  - максимум уроків предмета на день (з навчального плану)
  - індивідуальний максимум уроків на день для кожного класу (за нормативом МОН
    або вручну заданий) - різний для різних паралелей
  - заборонені номери уроків для предмета
  - мінімальний проміжок (у днях) між уроками одного предмета
  - потребу в спецкабінеті (якщо є прив'язані кабінети)
  - домашній кабінет класу (жорстка прив'язка "клас -> один кабінет")
  - фіксований слот предмета (день+урок, однаковий для всіх класів)
  - "завжди останнім уроком дня" (відносно, обчислюється окремо для кожного класу
    за його власним максимумом уроків/день)
  - здвоєні уроки (пари поспіль, той самий кабінет на обидва слоти)
  - поділ на підгрупи (2 вчителі одночасно в тому самому слоті класу; особливий
    випадок - той самий вчитель на обидві підгрупи)
  - міжпредметні групи поділу (різні предмети паралельно для підгруп класу)
  - клас-комплекти (той самий предмет одночасно кільком класам групи)
  - парність тижнів (парний/непарний) для дробового навантаження

Асистенти вчителя показуються на відповідних уроках у виводі розкладу (довідково),
але НЕ є жорстким обмеженням для solver-а - їхня доступність не перевіряється.
"""

import os
import time
from datetime import datetime

from ortools.sat.python import cp_model
from mon_normy_loader import (
    default_hranychne_navantazhennya,
    default_faktychne_navantazhennya,
    default_maks_urokiv_den,
    default_maks_urokiv_den_z_fizkulturoyu,
    vikovi_pravyla_vazhkosti,
)
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

from db import get_connection, ukr_klyuch


def klas_nazva(paralel, litera):
    return f"{paralel}-{litera}" if litera else f"{paralel}"


def skorochene_pib(pib):
    """'Іванов Іван Іванович' -> 'Іванов І.І.' - для компактних клітинок Excel."""
    if not pib:
        return pib
    chastyny = pib.split()
    if len(chastyny) < 2:
        return pib
    return f"{chastyny[0]} {''.join(f'{ch[0]}.' for ch in chastyny[1:] if ch)}"


# Раніше тут були дубльовані копії цих функцій/констант (з коментарем "Дублює логіку
# з app.py" - реальний ризик розсинхрону, виявлений і виправлений). Тепер обидва
# файли читають з одного джерела - data/json/mon_normy.json.


def realna_vazhkist(predmet_nazva, vazhkist_z_bazy, paralel):
    """Реальна складність предмета. Українська мова лишається C2 (середня) для ВСІХ
    класів без винятку - віковий override для 1-4 розглядався, але скасований через
    математичну несумісність із денними лімітами C1 саме для цієї вікової групи."""
    return vazhkist_z_bazy


def pary_dozvoleni(conn, predmet_id, paralel):
    """Чи дозволені здвоєні уроки для цього предмета в цій паралелі. Читає з
    predmet_paralel_pravyla (за id, не за назвою - перейменування предмета більше
    не ламає цю перевірку мовчки). Безпечний дефолт - False (заборонено), якщо
    рядка немає: санітарна помилка "випадково дозволили" гірша за наслідками, ніж
    "забули дозволити" (лише трохи менш оптимальний розклад)."""
    row = conn.execute(
        "SELECT dozvoleno_zdvoyeni_pary FROM predmet_paralel_pravyla "
        "WHERE predmet_id = ? AND paralel = ?",
        (predmet_id, paralel),
    ).fetchone()
    return bool(row["dozvoleno_zdvoyeni_pary"]) if row else False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
SCHEDULE_DIR = os.path.join(PROJECT_DIR, "schedule")

MAX_SOLVE_SECONDS = 300  # 5 хв - якість важливіша за швидкість (свідомий вибір)
DEN_KOROTKO = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
PENALTY_UNPLACED = 1000
PENALTY_EMPTY_DAY = 4000
PENALTY_NEBAZHANYI = 5
PENALTY_GAP = 700
PENALTY_ROZKYD = 15
WEIGHT_LATE_SLOT = 8
# "ostannim_urokom" сам по собі лише ЗАБОРОНЯЄ щось після обраного слота - без
# зустрічного заохочення солвер (який і так тяжіє до РАННІХ слотів через
# WEIGHT_LATE_SLOT вище) міг поставити такий запис рано і тим самим заборонити
# решту дня, штучно створюючи брак місця. Вага має явно переважати звичайні
# WEIGHT_LATE_SLOT/WEIGHT_VAZHKYI_PIZNIY_UROK (щоб дійсно тягнути на кінець дня),
# але лишатись набагато меншою за PENALTY_UNPLACED (щоб "пізність" не важила
# більше за саме розміщення уроку).
WEIGHT_OSTANNIM_UROKOM_PIZNO = 40

# Гігієнічна шкала важкості (орієнтир ДСанПіН 5.5.2.008-01): важкі предмети раніше в
# день і на пік працездатності (вт/ср), легкі - ближче до кінця дня і п'ятниці, без
# двох важких поспіль. Усі три - м'які цілі (штрафи), не жорсткі обмеження.
WEIGHT_VAZHKYI_PIZNIY_UROK = 12   # штраф за важкий предмет на пізньому уроці (за номер уроку)
WEIGHT_LEGKYI_RANNIY_UROK = 4     # штраф за легкий предмет на ранньому уроці (за номер уроку)
# Для ГРУПОВИХ записів клас-комплекту цей штраф НЕ застосовується (див. нижче) -
# спроба додати слабший зустрічний штраф ("тяжіння до раннього заповнення вікон")
# ЕМПІРИЧНО дала ГІРШИЙ результат (велика діра на старті дня замість малої
# посередині) - відкинуто, лишаємо групові повністю без тяжіння в obj. функції.
WEIGHT_VAZHKYI_1_UROK_M_YAKYI = 25  # 10-12: м'який штраф за C1 на 1-му уроці (не заборона)
WEIGHT_MAKSYMALNYI_UROK_STARSHI = 15  # 6+ класи: м'який штраф за сягання ВЛАСНОГО денного
# максимуму (не фіксоване число для всіх - особистий ліміт кожного класу). Мета - не
# "розтягувати" день до максимуму без потреби, коли можна вкластись у менше уроків.
# Скромна вага для першого тесту (менша за WEIGHT_VAZHKYI_1_UROK_M_YAKYI=25) -
# тестуємо емпірично, піднімаємо чи відкочуємо за результатом, як і решту штрафів цієї сесії.
# орієнтовна вага дня за шкалою працездатності: 1=Пн(середнє) 2,3=Вт,Ср(пік) 4=Чт(спад) 5=Пт(найнижче)
# Ранг "важкості" дня для розкладання навантаження по тижню (гігієнічна крива
# працездатності): пік - вт/ср, четвер трохи нижче, а понеділок і П'ЯТНИЦЯ -
# однакові "плечові" дні (втягування на початку тижня / спад у кінці). Раніше
# п'ятниця мала ранг 2 (найлегший) і збирала на себе всі легкі предмети - тепер
# симетрична з понеділком (ранг 3).
DEN_VAZHKOSTI_RANG = {1: 3, 2: 5, 3: 5, 4: 4, 5: 3}
WEIGHT_DEN_VAZHKOSTI = 10
# слабкий бонус за 1-шу підгрупу поділу у справжньому спецкабінеті (мовний тощо)
# для предметів зі spets_kabinet_lyshe_pidgrupa1. Лише тай-брейкер: набагато
# менший за PENALTY_GAP/PENALTY_ROZKYD, щоб не спотворював розміщення уроків.
WEIGHT_SPETS_KABINET_PG1 = 12
# штраф за ВІКНО У ВЧИТЕЛЯ (порожній урок між двома його уроками того ж дня;
# пізній початок чи ранній кінець дня вікном НЕ є). Свідомо менший за вікно
# класу (PENALTY_GAP=700) і за нерозміщений урок (1000): вчительське вікно -
# незручність, а не порушення, і не має витісняти уроки чи робити вікна дітям.
# Але більший за гігієнічні побажання (8-12 за урок), щоб солвер реально
# зсував уроки заради вчителя, коли це нічого не коштує класу. Вмикається
# перемикачем school_settings.vikna_vchyteliv (типово вимкнено): за A/B на демо
# 27 класів (сесія E, 2026-09-11) вікон вчителів удвічі менше (258 -> 135 за
# 10 хв), але модель більша й на коротких прогонах більше нерозміщених
# (60 с: 18 -> 31) - вага 25 і 60 дали те саме, тож справа в розмірі моделі,
# не у вазі. Деталі - PROJECT_OVERVIEW.
PENALTY_VIKNO_VCHYTELYA = 60


def potochnyi_semestr(conn):
    """Семестр, у якому школа зараз: ІІ - від дати початку ІІ семестру в
    «Налаштуваннях школи», інакше І (і коли дат іще не задано)."""
    from datetime import datetime
    s = conn.execute("SELECT semestr2_pochatok FROM school_settings WHERE id = 1").fetchone()
    try:
        d2 = datetime.strptime(s["semestr2_pochatok"], "%Y-%m-%d") if s and s["semestr2_pochatok"] else None
    except ValueError:
        d2 = None
    return 2 if d2 and datetime.now() >= d2 else 1


def _load_data(conn, paralel_vid=None, paralel_do=None, semestr=1):
    settings = conn.execute(
        "SELECT dniv_na_tyzhden, urokiv_na_den, vikna_vchyteliv FROM school_settings WHERE id = 1"
    ).fetchone()
    dniv = settings["dniv_na_tyzhden"] if settings else 5
    urokiv = settings["urokiv_na_den"] if settings else 8
    vikna_vchyteliv = bool(settings["vikna_vchyteliv"]) if settings else False

    # розклад складається на ОДИН семестр: рядки плану "весь рік" (semestr=0) і
    # рядки саме цього семестру (v0.2.0) - інакше предмет із різними годинами в
    # І і ІІ семестрі потрапив би в тиждень двічі
    diapazon_umova = f"WHERE np.semestr IN (0, {int(semestr)})"
    if paralel_vid is not None and paralel_do is not None:
        diapazon_umova += f" AND k.paralel BETWEEN {int(paralel_vid)} AND {int(paralel_do)}"

    entries_raw = conn.execute(
        f"""
        SELECT np.id, np.klas_id, np.predmet_id, np.vchytel_id,
               np.godyn_tyzhden, np.maks_urokiv_den, np.zdvoyeni_pary,
               np.pidgrupa_2_vchytel_id, np.mizhpredmetna_grupa_id,
               np.fiksovanyi_den AS klas_fiksovanyi_den, np.parnist,
               p.zaboroneni_uroky, p.min_promizhok_dni,
               p.fiksovanyi_den, p.fiksovanyi_urok, p.ostannim_urokom, p.vazhkist,
               p.vrahovuyetsya_u_hranychnomu, p.zdvoyeni_urok, p.spets_kabinet_lyshe_pidgrupa1,
               k.domashniy_kabinet_id, k.kilkist_uchniv,
               k.paralel, k.litera, k.maks_urokiv_den AS klas_maks_urokiv_den,
               k.maks_urokiv_den_z_fizkulturoyu AS klas_maks_urokiv_den_z_fizkulturoyu,
               k.hranychne_navantazhennya, k.faktychne_navantazhennya,
               p.nazva AS predmet_nazva, v.pib AS vchytel_pib
        FROM navchalnyj_plan np
        JOIN predmety p ON p.id = np.predmet_id
        JOIN klasy k ON k.id = np.klas_id
        JOIN vchyteli v ON v.id = np.vchytel_id
        {diapazon_umova}
        """
    ).fetchall()

    vchyteli_by_id = {r["id"]: r["pib"] for r in conn.execute("SELECT id, pib FROM vchyteli").fetchall()}
    # "Максимум уроків на день" з картки вчителя - ЖОРСТКИЙ ліміт солвера (нижче) і
    # жадібного розміщення позаурочних. Раніше поле лише оцінювало "ємність" на
    # сторінці генерації, а сама генерація його не виконувала.
    vchyteli_maks_den = {
        r["id"]: r["maksymum_urokiv_den"]
        for r in conn.execute("SELECT id, maksymum_urokiv_den FROM vchyteli")
        if r["maksymum_urokiv_den"]
    }

    # Підгрупи: якщо у записі вказано другого вчителя (pidgrupa_2_vchytel_id), будуємо
    # "тіньовий" другий запис - той самий клас/предмет/години, інший вчитель. Обидва
    # мають стояти РІВНО в тих самих слотах розкладу (клас не подвоюється), але кожен
    # веде свій облік учителя/кабінету окремо. pidgrupa_link вказує на пару.
    entries = []
    pidgrupa_link = {}  # id одного запису пари -> id іншого
    for r in entries_raw:
        row = dict(r)
        row["pidgrupa_link"] = None
        row["is_pidgrupa_shadow"] = False
        # той самий вчитель на обидві підгрупи (розділ "Підгрупи" нижче) - підгрупа 1
        # ніде не планується (лише допис у показі), тому цей урок ЗАВЖДИ мусить бути
        # останнім уроком дня для класу - інакше довелось би показувати допис "(2
        # підгрупа)"/"(0 урок)" на кожній можливій клітинці, а не лише там, де урок
        # реально стоїть. Форсуємо через уже наявний механізм ostannim_urokom - лише
        # для ЦЬОГО запису (клас+предмет), не для предмета в інших класах.
        if r["pidgrupa_2_vchytel_id"] and r["pidgrupa_2_vchytel_id"] == r["vchytel_id"]:
            row["ostannim_urokom"] = 1
        entries.append(row)

        # Якщо вчитель підгрупи 2 - ТОЙ САМИЙ, що основний, "тіньовий" паралельний
        # запис НЕ заводимо: одна й та сама людина фізично не може вести дві
        # підгрупи одночасно, а рівно такий паралельний слот тіньовий запис і
        # моделює. У цьому випадку запис поводиться як ЗВИЧАЙНИЙ (одна підгрупа
        # 2 - реально запланований урок; підгрупа 1 - лише інформаційний допис,
        # додається при показі розкладу, генератора не стосується).
        if r["pidgrupa_2_vchytel_id"] and r["pidgrupa_2_vchytel_id"] != r["vchytel_id"]:
            shadow_id = r["id"] * 1_000_000 + 1
            shadow = dict(r)
            shadow["id"] = shadow_id
            shadow["vchytel_id"] = r["pidgrupa_2_vchytel_id"]
            shadow["vchytel_pib"] = vchyteli_by_id.get(r["pidgrupa_2_vchytel_id"], "?")
            shadow["pidgrupa_link"] = r["id"]
            shadow["is_pidgrupa_shadow"] = True
            # НЕ несе pidgrupa_2_vchytel_id у generated_urok: інакше на тіньовому
            # записі vchytel_id == pidgrupa_2_vchytel_id, і перевірка "той самий
            # вчитель" нижче хибно спрацьовує - клітинка дістає напис "(0 урок 1
            # підгрупа)", який чинний ЛИШЕ для одного вчителя на обидві підгрупи.
            shadow["pidgrupa_2_vchytel_id"] = None
            # так само зачищаємо основний запис пари - обидві підгрупи ведуть РІЗНІ
            # вчителі паралельно, ніякого "0 уроку" тут нема
            row["pidgrupa_2_vchytel_id"] = None
            # позначаємо ОБИДВА записи пари посиланням один на одного (раніше
            # row["pidgrupa_link"] лишався None - невідрізнити основний запис
            # поділу від звичайного нерозділеного; тепер потрібно для логіки
            # кабінетів "спецкабінет лише 1-й підгрупі"). _sync_pair дедуплікує.
            row["pidgrupa_link"] = shadow_id
            entries.append(shadow)
            pidgrupa_link[r["id"]] = shadow_id
            pidgrupa_link[shadow_id] = r["id"]

    kabinety = conn.execute("SELECT * FROM kabinety").fetchall()

    # "вільні універсальні" - кабінети типу 'universalnyi', які НЕ закріплені за
    # жодним класом як домашній. Це резерв, куди генератор може покласти 2-гу
    # підгрупу поділу для предметів із прапорцем pidgrupa2_vilnyi_kabinet
    # (напр. англійська: 1-ша підгрупа - у мовний кабінет, 2-га - у будь-який
    # вільний, бо для мовного уроку окреме обладнання не потрібне).
    domashni_kab_ids = {
        r["domashniy_kabinet_id"] for r in conn.execute(
            "SELECT domashniy_kabinet_id FROM klasy WHERE domashniy_kabinet_id IS NOT NULL"
        )
    }
    vilni_universalni_kab = [
        k["id"] for k in kabinety
        if k["typ_pryvyazky"] == "universalnyi" and k["id"] not in domashni_kab_ids
    ]

    kabinety_predmety = conn.execute("SELECT kabinet_id, predmet_id FROM kabinety_predmety").fetchall()
    kabinet_dozvoleni_predmety = {}  # kabinet_id -> set(predmet_id) - з перспективи кабінету
    for r in kabinety_predmety:
        kabinet_dozvoleni_predmety.setdefault(r["kabinet_id"], set()).add(r["predmet_id"])

    kabinety_vchyteli = conn.execute("SELECT kabinet_id, vchytel_id FROM kabinety_vchyteli").fetchall()
    kabinet_dozvoleni_vchyteli = {}  # kabinet_id -> set(vchytel_id) - раніше НЕ завантажувалось
    # взагалі, тому прив'язка кабінету "за вчителем" повністю ігнорувалась генератором
    for r in kabinety_vchyteli:
        kabinet_dozvoleni_vchyteli.setdefault(r["kabinet_id"], set()).add(r["vchytel_id"])

    teacher_unavail = set()
    teacher_nebazhanyi = set()
    for r in conn.execute("SELECT vchytel_id, den, urok, status FROM teacher_availability").fetchall():
        if r["status"] == "nedostupnyi":
            teacher_unavail.add((r["vchytel_id"], r["den"], r["urok"]))
        else:
            teacher_nebazhanyi.add((r["vchytel_id"], r["den"], r["urok"]))

    # Якщо генеруємо ОКРЕМО по рівню закладу (напр. лише 5-9), автоматично враховуємо
    # зайнятість вчителів з УЖЕ згенерованого розкладу ІНШИХ рівнів (напр. якщо той самий
    # вчитель фізкультури вже стоїть у понеділок 1-й урок в 2 класі (інший рівень),
    # він фізично не може бути одночасно і в 7 класі цього ж дня й уроку) - захист від
    # накладок при почерговій генерації по рівнях.
    # ці ж слоти рахуються і в денний ліміт вчителя (урок в іншому рівні - теж урок)
    vchytel_sloty_inshyh_rivniv = set()
    if paralel_vid is not None and paralel_do is not None:
        for r in conn.execute(
            """
            SELECT DISTINCT gu.vchytel_id, gu.den, gu.urok
            FROM generated_urok gu
            JOIN klasy k ON k.id = gu.klas_id
            WHERE k.paralel NOT BETWEEN ? AND ?
            """,
            (paralel_vid, paralel_do),
        ).fetchall():
            teacher_unavail.add((r["vchytel_id"], r["den"], r["urok"]))
            vchytel_sloty_inshyh_rivniv.add((r["vchytel_id"], r["den"], r["urok"]))

    class_unavail = set()
    for r in conn.execute("SELECT klas_id, den, urok FROM class_availability").fetchall():
        class_unavail.add((r["klas_id"], r["den"], r["urok"]))

    kabinet_unavail = set()
    for r in conn.execute("SELECT kabinet_id, den, urok FROM kabinet_availability").fetchall():
        kabinet_unavail.add((r["kabinet_id"], r["den"], r["urok"]))

    # той самий захист, що й для вчителів вище: якщо генеруємо ОКРЕМО по рівню, кабінети,
    # уже зайняті в УЖЕ згенерованому розкладі ІНШИХ рівнів, теж стають недоступними -
    # інакше два рівні можуть незалежно обрати той самий кабінет в той самий момент
    if paralel_vid is not None and paralel_do is not None:
        for r in conn.execute(
            """
            SELECT DISTINCT gu.kabinet_id, gu.den, gu.urok
            FROM generated_urok gu
            JOIN klasy k ON k.id = gu.klas_id
            WHERE k.paralel NOT BETWEEN ? AND ? AND gu.kabinet_id IS NOT NULL
            """,
            (paralel_vid, paralel_do),
        ).fetchall():
            kabinet_unavail.add((r["kabinet_id"], r["den"], r["urok"]))

    # Позаурочні заняття - окреме джерело, НЕ рахується в денний ліміт навчальних
    # уроків, але так само перевіряється на конфлікт вчителя/кабінету/класу
    pozaurochni_diapazon_umova = ""
    if paralel_vid is not None and paralel_do is not None:
        pozaurochni_diapazon_umova = f"AND k.paralel BETWEEN {int(paralel_vid)} AND {int(paralel_do)}"
    # z.aktyvne = 0 -> заняття вимкнене в довіднику "Позаурочні заняття", генератор
    # пропускає всі його рядки плану (перевірка розкладу без частини позаурочки)
    pozaurochni_raw = conn.execute(
        f"""
        SELECT pp.id, pp.zanyattya_id, pp.klas_id, pp.vchytel_id, pp.domashniy_kabinet_id, pp.godyn_tyzhden,
               pp.fiksovanyi_den AS klas_fiksovanyi_den,
               z.nazva AS zanyattya_nazva, z.fiksovanyi_den AS zanyattya_fiksovanyi_den,
               k.paralel, k.litera, k.maks_urokiv_den AS klas_maks_urokiv_den, k.komplekt_id,
               v.pib AS vchytel_pib
        FROM pozaurochnyj_plan pp
        JOIN pozaurochni_zanyattya z ON z.id = pp.zanyattya_id
        JOIN klasy k ON k.id = pp.klas_id
        JOIN vchyteli v ON v.id = pp.vchytel_id
        WHERE z.aktyvne = 1
        {pozaurochni_diapazon_umova}
        """
    ).fetchall()
    pozaurochni_entries = [dict(r) for r in pozaurochni_raw]
    # закріплений день ОКРЕМО для цього класу (pp.fiksovanyi_den) перевизначає
    # спільний день заняття (z.fiksovanyi_den) - той самий патерн, що й для
    # звичайних предметів (navchalnyj_plan.fiksovanyi_den перевизначає predmety)
    for pe in pozaurochni_entries:
        pe["fiksovanyi_den"] = pe["klas_fiksovanyi_den"] or pe["zanyattya_fiksovanyi_den"]

    return {
        "dniv": dniv,
        "urokiv": urokiv,
        "entries": entries,
        "pidgrupa_link": pidgrupa_link,
        "kabinety": kabinety,
        "vilni_universalni_kab": vilni_universalni_kab,
        "kabinet_dozvoleni_predmety": kabinet_dozvoleni_predmety,
        "kabinet_dozvoleni_vchyteli": kabinet_dozvoleni_vchyteli,
        "teacher_unavail": teacher_unavail,
        "vchyteli_maks_den": vchyteli_maks_den,
        "vchytel_sloty_inshyh_rivniv": vchytel_sloty_inshyh_rivniv,
        "vikna_vchyteliv": vikna_vchyteliv,
        "teacher_nebazhanyi": teacher_nebazhanyi,
        "class_unavail": class_unavail,
        "kabinet_unavail": kabinet_unavail,
        "pozaurochni_entries": pozaurochni_entries,
    }


def _parse_zaboroneni(raw):
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def _parse_dni_tyzhnya(raw):
    """Розбирає значення на множину днів тижня (1-5) - через кому і/або діапазон
    через дефіс: '1,3,5' -> {1,3,5}; '1-3' -> {1,2,3}; '1,3-5' -> {1,3,4,5}.
    Значення поза межами 1-5 ігноруються. Приймає і int (зворотна сумісність з
    наявними записами, де раніше зберігалось просто ціле число, не текст), і str."""
    if raw is None:
        return set()
    if isinstance(raw, int):
        return {raw} if 1 <= raw <= 5 else set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            mezhi = part.split("-")
            if len(mezhi) == 2 and mezhi[0].strip().isdigit() and mezhi[1].strip().isdigit():
                vid, do = int(mezhi[0].strip()), int(mezhi[1].strip())
                if vid <= do:
                    result.update(range(vid, do + 1))
        elif part.isdigit():
            result.add(int(part))
    return {d for d in result if 1 <= d <= 5}


def _obyednaty_klas_komplekty(conn, entries):
    """Для класів, що входять в один клас-комплект, об'єднує записи навчального
    плану з ОДНАКОВИМ (predmet_id, vchytel_id) у СПІЛЬНИЙ "груповий" запис -
    той самий слот розкладу, той самий вчитель, усі класи групи одночасно.
    "Залишкові" години (коли в одного класу їх більше, ніж в інших членів
    групи) лишаються звичайними окремими записами.

    Груповий запис відрізняється полем "klas_ids" (список) замість одиничного
    "klas_id" - решта логіки генератора перевіряє наявність цього поля через
    entry_klas_ids() і обробляє відповідно."""
    komplekt_po_klasu = {
        r["id"]: r["komplekt_id"]
        for r in conn.execute("SELECT id, komplekt_id FROM klasy WHERE komplekt_id IS NOT NULL")
    }
    if not komplekt_po_klasu:
        return entries, komplekt_po_klasu  # немає жодного клас-комплекту - нічого об'єднувати

    # групуємо ЗВИЧАЙНІ (не тіньові підгрупи) записи членів комплектів за
    # (komplekt_id, predmet_id, vchytel_id) - кандидати на синхронізацію
    kandydaty = {}
    inshi = []
    for e in entries:
        kid = komplekt_po_klasu.get(e["klas_id"])
        if kid is None or e["is_pidgrupa_shadow"]:
            inshi.append(e)
            continue
        klyuch = (kid, e["predmet_id"], e["vchytel_id"])
        kandydaty.setdefault(klyuch, []).append(e)

    result = list(inshi)
    for (kid, predmet_id, vchytel_id), grupa in kandydaty.items():
        if len(grupa) < 2:
            # лише один клас групи веде цей предмет із цим вчителем - синхронізувати
            # нема з ким, лишається звичайним окремим записом
            result.extend(grupa)
            continue

        min_godyn = min(e["godyn_tyzhden"] for e in grupa)
        bazovyi = grupa[0]  # службові поля (день/урок обмеження тощо) беремо з першого
        grupovyi_zapys = dict(bazovyi)
        grupovyi_zapys["klas_ids"] = [e["klas_id"] for e in grupa]
        grupovyi_zapys["klas_id"] = grupa[0]["klas_id"]  # для коду, що ще не адаптований
        grupovyi_zapys["godyn_tyzhden"] = min_godyn
        grupovyi_zapys["id"] = f"grupa_{kid}_{predmet_id}_{vchytel_id}"
        grupovyi_zapys["komplekt_id"] = kid
        # для групового запису денний ліміт - НАЙСУВОРІШИЙ серед членів (щоб не
        # порушити санітарний ліміт жодного з класів групи одночасно). ВАЖЛИВО:
        # резолвимо РЕАЛЬНЕ значення кожного члена (явне поле АБО дефолт з JSON за
        # паралеллю) ПЕРЕД min - раніше тут стояло "or 99" як заглушка на None,
        # що для типового випадку (жоден член не має явного перевизначення) робило
        # ліміт фактично необмеженим (99) замість справжнього дефолту за віком.
        grupovyi_zapys["klas_maks_urokiv_den"] = min(
            e["klas_maks_urokiv_den"] or default_maks_urokiv_den(e["paralel"]) for e in grupa
        )
        grupovyi_zapys["klas_maks_urokiv_den_z_fizkulturoyu"] = min(
            e["klas_maks_urokiv_den_z_fizkulturoyu"] or default_maks_urokiv_den_z_fizkulturoyu(e["paralel"])
            for e in grupa
        )
        grupovyi_zapys["paralel"] = min(e["paralel"] for e in grupa)  # наймолодша - найсуворіші вікові правила
        grupovyi_zapys["kilkist_uchniv"] = sum(e["kilkist_uchniv"] for e in grupa)
        result.append(grupovyi_zapys)

        for e in grupa:
            zalyshok = e["godyn_tyzhden"] - min_godyn
            if zalyshok > 0:
                okremyi = dict(e)
                okremyi["godyn_tyzhden"] = zalyshok
                result.append(okremyi)

    return result, komplekt_po_klasu


def _dni_pozaurochnogo(pe):
    """Дні, у які жадібне розміщення пробує ставити цей позаурочний запис
    (без закріпленого дня - понеділок)."""
    return sorted(_parse_dni_tyzhnya(pe["fiksovanyi_den"])) or [1]


def _rezerv_pozaurochnyh_po_dnyah(pozaurochni_entries):
    """{(vchytel_id, den): годин} - скільки слотів позаурочних припаде вчителю на
    день при жадібному розміщенні (по одній годині на кожен вказаний день по
    колу). Групування те саме, що й у самому розміщенні: записи одного заняття
    в класах одного комплекту в одного вчителя йдуть спільно, тож спільна
    частина рахується один раз, залишок понад неї - окремо."""
    po_klyuchu = {}
    for i, pe in enumerate(pozaurochni_entries):
        if pe.get("komplekt_id") is None:
            klyuch = ("okremo", i)
        else:
            klyuch = (pe["komplekt_id"], pe["zanyattya_id"], pe["vchytel_id"])
        po_klyuchu.setdefault(klyuch, []).append(pe)
    rezerv = {}

    def dodaty(vid, dni, godyn):
        for i in range(int(godyn)):
            d = dni[i % len(dni)]
            rezerv[(vid, d)] = rezerv.get((vid, d), 0) + 1

    for grupa in po_klyuchu.values():
        if len(grupa) < 2:
            pe = grupa[0]
            dodaty(pe["vchytel_id"], _dni_pozaurochnogo(pe), pe["godyn_tyzhden"])
            continue
        min_godyn = min(pe["godyn_tyzhden"] for pe in grupa)
        dodaty(grupa[0]["vchytel_id"], _dni_pozaurochnogo(grupa[0]), min_godyn)
        for pe in grupa:
            zalyshok = pe["godyn_tyzhden"] - min_godyn
            if zalyshok > 0:
                dodaty(pe["vchytel_id"], _dni_pozaurochnogo(pe), zalyshok)
    return rezerv


def _zastosuvaty_zamorozku(conn, entries, pozaurochni_entries, zamorozheni_klityny):
    """Для заморожених клітинок (klas_id, den): читає наявні generated_urok рядки й
    зберігає їх ЯК Є (fixed_results), зменшує потрібні для генерації години у
    відповідних записів навчального ПЛАНУ і позаурочних, і повертає попередню
    зайнятість вчителів/кабінетів/класів у ці слоти (щоб решта генерації їх не займала).

    zamorozheni_klityny - множина (klas_id, den), яку слід трактувати як
    незмінну (наявний розклад цього класу в цей день копіюється як є)."""
    if not zamorozheni_klityny:
        return [], entries, pozaurochni_entries, set(), set(), set()

    fixed_results = []
    pre_occ_vchytel = set()
    pre_occ_kabinet = set()
    pre_occ_klas = set()
    frozen_hours_by_key = {}  # (klas_id, predmet_id) -> скільки годин уже заморожено
    frozen_pozaur_by_key = {}  # (klas_id, zanyattya_id) -> скільки годин уже заморожено

    for (klas_id, den) in zamorozheni_klityny:
        rows = conn.execute(
            "SELECT * FROM generated_urok WHERE klas_id = ? AND den = ?",
            (klas_id, den),
        ).fetchall()
        for r in rows:
            fixed_results.append({
                "klas_id": r["klas_id"], "predmet_id": r["predmet_id"],
                "pozaurochne_zanyattya_id": r["pozaurochne_zanyattya_id"],
                "vchytel_id": r["vchytel_id"], "kabinet_id": r["kabinet_id"],
                "den": r["den"], "urok": r["urok"],
                "parnist": r["parnist"] if "parnist" in r.keys() else "oboydva",
                "pidgrupa_2_vchytel_id": r["pidgrupa_2_vchytel_id"] if "pidgrupa_2_vchytel_id" in r.keys() else None,
            })
            pre_occ_vchytel.add((r["vchytel_id"], r["den"], r["urok"]))
            if r["kabinet_id"]:
                pre_occ_kabinet.add((r["kabinet_id"], r["den"], r["urok"]))
            if r["predmet_id"]:
                # рахуємо заморожені години ОКРЕМО по парності - інакше предмет з
                # кількома рядками плану (oboydva + parnyi/neparnyi для дробового
                # 0.5 год) отримав би подвійне віднімання: одна заморожена година
                # зменшила б ОБИДВА рядки замість лише того, якому вона реально належить
                parnist = r["parnist"] if "parnist" in r.keys() else "oboydva"
                key = (r["klas_id"], r["predmet_id"], parnist)
                frozen_hours_by_key[key] = frozen_hours_by_key.get(key, 0) + 1
            elif r["pozaurochne_zanyattya_id"]:
                key = (r["klas_id"], r["pozaurochne_zanyattya_id"])
                frozen_pozaur_by_key[key] = frozen_pozaur_by_key.get(key, 0) + 1
        # блокуємо ВЕСЬ день для цього класу (не лише зайняті слоти!) - "не чіпати"
        # означає день лишається ТОЧНО таким, як є, без жодних НОВИХ додавань генератором
        for l in range(1, 13):  # із запасом понад будь-яку реальну сітку уроків
            pre_occ_klas.add((klas_id, den, l))

    reduced_entries = []
    for e in entries:
        if e.get("predmet_id") is None:
            reduced_entries.append(e)
            continue
        klas_ids_zapysu = entry_klas_ids(e)
        e_parnist = e.get("parnist") or "oboydva"
        frozen_counts = [
            frozen_hours_by_key.get((kid, e["predmet_id"], e_parnist), 0) for kid in klas_ids_zapysu
        ]
        min_frozen = min(frozen_counts) if frozen_counts else 0
        if min_frozen <= 0:
            reduced_entries.append(e)
            continue
        novi_hodyny = e["godyn_tyzhden"] - min_frozen
        if novi_hodyny > 0:
            novyi = dict(e)
            novyi["godyn_tyzhden"] = novi_hodyny
            reduced_entries.append(novyi)

    reduced_pozaurochni = []
    for pe in pozaurochni_entries:
        key = (pe["klas_id"], pe["zanyattya_id"])
        zamorozheno = frozen_pozaur_by_key.get(key, 0)
        if zamorozheno <= 0:
            reduced_pozaurochni.append(pe)
            continue
        novi_hodyny = pe["godyn_tyzhden"] - zamorozheno
        if novi_hodyny > 0:
            novyi = dict(pe)
            novyi["godyn_tyzhden"] = novi_hodyny
            reduced_pozaurochni.append(novyi)

    return fixed_results, reduced_entries, reduced_pozaurochni, pre_occ_vchytel, pre_occ_kabinet, pre_occ_klas


def entry_klas_ids(e):
    """Список klas_id, зайнятих цим записом - [один] для звичайного запису,
    [кілька] для групового запису клас-комплекту."""
    return e["klas_ids"] if "klas_ids" in e else [e["klas_id"]]


def generate_schedule(paralel_vid=None, paralel_do=None, maks_sekund=None, zamorozheni_klityny=None,
                      semestr=None):
    _chas_startu = time.time()
    conn = get_connection()
    if semestr not in (1, 2):
        semestr = potochnyi_semestr(conn)
    data = _load_data(conn, paralel_vid, paralel_do, semestr)

    # довідник паралель/літера/норми ПО КОЖНОМУ КЛАСУ окремо - будуємо ДО групування
    # клас-комплектів, бо груповий запис несе дані лише ОДНОГО представника групи,
    # а перевірка навантаження нижче має рахуватись для КОЖНОГО класу окремо
    klas_info_by_id = {}
    for e in data["entries"]:
        if e["klas_id"] not in klas_info_by_id:
            klas_info_by_id[e["klas_id"]] = {
                "paralel": e["paralel"], "litera": e["litera"],
                "hranychne_navantazhennya": e["hranychne_navantazhennya"],
                "faktychne_navantazhennya": e["faktychne_navantazhennya"],
            }

    data["entries"], komplekt_po_klasu_dlya_poryadku = _obyednaty_klas_komplekty(conn, data["entries"])

    # заморожені клітинки (клас, день) - "Догенерація": наявний розклад цих
    # (клас, день) лишається незмінним, решта генерується вільно навколо нього
    fixed_results, data["entries"], data["pozaurochni_entries"], pre_occ_vchytel, pre_occ_kabinet, pre_occ_klas = (
        _zastosuvaty_zamorozku(conn, data["entries"], data["pozaurochni_entries"], zamorozheni_klityny or set())
    )
    data["teacher_unavail"] = data["teacher_unavail"] | pre_occ_vchytel
    # уроки вчителя, що вже стоять поза цим прогоном (інші рівні, заморожені дні) -
    # рахуються в його денний ліміт; слоти унікальні, тож комплект не подвоюється
    zainyato_vch_den = {}
    sloty_vch_poza_prohonom = data["vchytel_sloty_inshyh_rivniv"] | pre_occ_vchytel
    for (vid, d, _l) in sloty_vch_poza_prohonom:
        zainyato_vch_den[(vid, d)] = zainyato_vch_den.get((vid, d), 0) + 1
    # резерв під позаурочні: вони ставляться ПІСЛЯ солвера жадібно і теж
    # рахуються в денний ліміт вчителя. Якби солвер заповнив ліміт навчальними,
    # виховній годині місця б не лишилось - тому в дні, де вчитель має
    # позаурочне, ліміт для навчальних зменшуємо на його години
    rezerv_pozaur_den = _rezerv_pozaurochnyh_po_dnyah(data["pozaurochni_entries"])
    data["class_unavail"] = data["class_unavail"] | pre_occ_klas

    days = list(range(1, data["dniv"] + 1))
    lessons = list(range(1, data["urokiv"] + 1))

    entries = data["entries"]
    entries_by_id = {e["id"]: e for e in entries}

    # ---- парність тижнів (парний/непарний) - для дробового навантаження (0.5 год/тиждень
    # реалізується як повний урок раз на два тижні). Запис з parnist='oboydva' (типовий
    # випадок) стоїть в ОБОХ тижнях одночасно - як і раніше, без змін поведінки. Запис
    # 'parnyi'/'neparnyi' займає слот лише в одному з двох "паралельних" тижнів - тому
    # може ділити той самий фізичний слот (день+урок) із записом ІНШОЇ парності без
    # реального конфлікту (вони ніколи не відбуваються одночасно). Зайнятість
    # (клас/вчитель/кабінет) тому рахується як ДВІ окремі доріжки, не одна.
    #
    # Якщо в плані НЕМАЄ жодного parnyi/neparnyi запису (типова школа без дробових
    # предметів) - доріжка лишається ОДНА ("oboydva"), а не дві однакові. Увесь код
    # нижче однаково працює з будь-якою кількістю доріжок (просто ітерує по TYZHNI),
    # тому це не окрема гілка логіки - лише вдвічі менше змінних/обмежень для солвера,
    # коли парність узагалі не використовується. ----
    maye_paritetni_zapysy = any((e.get("parnist") or "oboydva") != "oboydva" for e in entries)
    TYZHNI = ("parnyi", "neparnyi") if maye_paritetni_zapysy else ("oboydva",)

    def _entry_tyzhni(e):
        par = e.get("parnist") or "oboydva"
        return TYZHNI if par == "oboydva" else (par,)

    if not entries:
        conn.close()
        return {
            "success": False,
            "message": "Навчальний план порожній - немає що розкладати.",
            "chas_sekund": round(time.time() - _chas_startu, 1),
        }

    # ---- перевірка ПЕРЕД генерацією: навчальний план не повинен перевищувати ані
    # граничне навантаження (без фізкультури), ані фактичне (з фізкультурою) - обидві
    # межі вносяться вручну з офіційного плану МОН у картці класу. Якщо перевищено -
    # відмовляємо одразу, а не намагаємось усе одно розкласти зайві години. ----
    # члени міжпредметної групи (різні предмети, ті самі слоти класу паралельно -
    # напр. половина класу англійська, половина німецька) фізично займають ОДИН час
    # класу. Для навантаження класу рахуємо групу один раз - за представником з
    # найбільшими годинами; решту членів пропускаємо (як і тіньові записи підгруп).
    _mizh_chleny = {}
    for e in entries:
        gid = e.get("mizhpredmetna_grupa_id")
        if gid:
            _mizh_chleny.setdefault(gid, []).append(e)
    mizh_ne_rakhuvaty = set()
    for chleny in _mizh_chleny.values():
        predstavnyk = max(chleny, key=lambda x: (x["godyn_tyzhden"], x["id"]))
        mizh_ne_rakhuvaty.update(c["id"] for c in chleny if c["id"] != predstavnyk["id"])

    po_klasu = {}
    for e in entries:
        if e["is_pidgrupa_shadow"] or e["id"] in mizh_ne_rakhuvaty:
            continue  # паралельний час (2-га підгрупа / міжпредметна група) - не додаткова година
        # запис з парністю (parnyi/neparnyi) відбувається лише РАЗ на два тижні - у
        # середньому тижневому навантаженні враховується як ПОЛОВИНА своїх годин
        # (0.5 год/тиждень), інакше "0.5+0.5" (дві половинки одного дробового
        # предмета) хибно порахувались би як +2 замість +1 год/тиждень
        efektyvni_hodyny = e["godyn_tyzhden"] * (1 if (e.get("parnist") or "oboydva") == "oboydva" else 0.5)
        for klas_id in entry_klas_ids(e):
            info_klasu = klas_info_by_id[klas_id]
            if klas_id not in po_klasu:
                po_klasu[klas_id] = {
                    "paralel": info_klasu["paralel"], "litera": info_klasu["litera"],
                    "hranychne_navantazhennya": info_klasu["hranychne_navantazhennya"],
                    "faktychne_navantazhennya": info_klasu["faktychne_navantazhennya"],
                    "bez_fizkultury": 0, "z_fizkulturoyu": 0,
                }
            po_klasu[klas_id]["z_fizkulturoyu"] += efektyvni_hodyny
            if e["vrahovuyetsya_u_hranychnomu"]:
                po_klasu[klas_id]["bez_fizkultury"] += efektyvni_hodyny

    perevyshchennya = []
    for klas_id, info in po_klasu.items():
        hranychna = info["hranychne_navantazhennya"] or default_hranychne_navantazhennya(info["paralel"])
        faktychna = info["faktychne_navantazhennya"] or default_faktychne_navantazhennya(info["paralel"])
        nazva = klas_nazva(info["paralel"], info["litera"])
        if info["bez_fizkultury"] > hranychna:
            perevyshchennya.append(
                f"{nazva}: розподілено без фізкультури {info['bez_fizkultury']} год, "
                f"перевищує граничне навантаження {hranychna} год"
            )
        if info["z_fizkulturoyu"] > faktychna:
            perevyshchennya.append(
                f"{nazva}: розподілено з фізкультурою {info['z_fizkulturoyu']} год, "
                f"перевищує фактичне навантаження {faktychna} год"
            )

    if perevyshchennya:
        conn.close()
        return {
            "success": False,
            "message": (
                "Навчальний план перевищує норми МОН для деяких класів - виправте план "
                "або норми в картці класу перед генерацією:\n" + "\n".join(perevyshchennya)
            ),
            "chas_sekund": round(time.time() - _chas_startu, 1),
        }

    model = cp_model.CpModel()

    x = {}          # (entry_id, d, l) -> BoolVar - одиночний урок у слоті l
    p = {}          # (entry_id, d, l) -> BoolVar - здвоєна пара починається у слоті l (займає l і l+1)
    unplaced = {}   # entry_id -> IntVar (у годинах: одиночний=1, пара=2)
    y = {}          # (entry_id, d, l, kabinet_id) -> BoolVar - кабінет для одиночного уроку
    y_pair = {}     # (entry_id, d, l, kabinet_id) -> BoolVar - кабінет для пари (той самий на обидва слоти)

    entry_allowed_slots = {}       # (eid) -> [(d,l), ...] - де можна одиночний урок
    entry_allowed_pair_starts = {}  # (eid) -> [(d,l), ...] - де можна почати пару (l і l+1 обидва вільні)
    klas_maks_l_by_entry = {}       # (eid) -> klas_maks_l - для обмеження "виховна останньою"

    # Дні, коли для класу заплановане позаурочне з фіксованим днем (виховна тощо) -
    # обчислюємо ЗАЗДАЛЕГІДЬ (до цього циклу), щоб фізкультура могла врахувати це при
    # виборі своєї позиції (правило нижче: не займати "бонусний" слот понад ліміт
    # саме в цей день, щоб позаурочне не мусило "стекатись" ще на слот вище).
    dni_z_pozaurochnymy_po_klasu = {}
    for pe in data.get("pozaurochni_entries", []):
        dni_pe = _parse_dni_tyzhnya(pe["fiksovanyi_den"])
        if dni_pe:
            dni_z_pozaurochnymy_po_klasu.setdefault(pe["klas_id"], set()).update(dni_pe)

    for e in entries:
        eid = e["id"]
        forbidden = _parse_zaboroneni(e["zaboroneni_uroky"])
        # день предмета в цьому КОНКРЕТНОМУ класі - перевизначення з навчального плану
        # має пріоритет над спільним для всіх класів fiksovanyi_den предмета (напр.
        # Захист України в 10 класі щочетверга, а в 11 класі щоп'ятниці - той самий
        # предмет, різні класи)
        efektyvnyi_fiksovanyi_den = e["klas_fiksovanyi_den"] or e["fiksovanyi_den"]

        # загальна фізична межа дня для класу - завжди РОЗШИРЕНА (з фізкультурою),
        # бо це справжній максимум можливих слотів того дня для будь-якого класу
        klas_overall_maks_l = min(
            e["klas_maks_urokiv_den_z_fizkulturoyu"] or default_maks_urokiv_den_z_fizkulturoyu(e["paralel"]),
            data["urokiv"],
        )
        # а ось МІСЦЕ САМОГО ЗАПИСУ - звужене для звичайних предметів (базовий ліміт),
        # і розширене лише для тих, що позначені "не враховується у граничному
        # навантаженні" (фізкультура) - саме вони дозволені як додатковий урок понад
        # звичайний ліміт, коли вони є того дня
        if e["vrahovuyetsya_u_hranychnomu"]:
            klas_maks_l = min(
                e["klas_maks_urokiv_den"] or default_maks_urokiv_den(e["paralel"]),
                data["urokiv"],
            )
        else:
            klas_maks_l = klas_overall_maks_l

        pary_potribno = e["zdvoyeni_pary"] or 0
        if pary_potribno > 0 and not pary_dozvoleni(conn, e["predmet_id"], e["paralel"]):
            # Санітарний регламент забороняє здвоєні уроки для цього предмета в цій
            # паралелі - ігноруємо запит на пари, розкладаємо як одиночні уроки
            pary_potribno = 0

        # Правило "важкий предмет не на 1-му уроці" (жорстка заборона для 1-9 класів,
        # довгий день) - СТАТИЧНЕ, залежить лише від даних цього запису, тож фільтруємо
        # на етапі allowed (для x), а не створюємо змінну й одразу обнулюємо. Для пар
        # (p) свідомо НЕ переносимо - окрема, менш критична логіка, лишається як була.
        _rv_dlya_1_uroku = realna_vazhkist(e["predmet_nazva"], e["vazhkist"], e["paralel"])
        _pravyla_dlya_1_uroku = vikovi_pravyla_vazhkosti(e["paralel"])
        vazhkyi_1_urok_zaboroneno = (
            _rv_dlya_1_uroku == "vazhkyi"
            and _pravyla_dlya_1_uroku["c1_zaboroneno_1_zhorstko"]
            and klas_overall_maks_l >= 5
        )

        # Правило: предмет "понад ліміт" (фізкультура тощо, не враховується у
        # граничному навантаженні) НЕ може займати саме БОНУСНИЙ слот (klas_overall_maks_l)
        # у день, коли для цього класу заплановане позаурочне з фіксованим днем
        # (виховна) - інакше позаурочне мусило б "стекатись" ще на слот вище
        # (фізкультура на 5-му, виховна аж на 6-му). У такий день фізкультура має
        # вміститись у звичайні слоти (конкуруючи як звичайний предмет), а не
        # розширювати день додатково.
        dni_pozaurochnyh_cogo_klasu = set()
        for kid in entry_klas_ids(e):
            dni_pozaurochnyh_cogo_klasu |= dni_z_pozaurochnymy_po_klasu.get(kid, set())
        bonus_zaboroneno_v_deni_pozaurochnyh = (
            not e["vrahovuyetsya_u_hranychnomu"] and bool(dni_pozaurochnyh_cogo_klasu)
        )

        def _slot_ok(d, l):
            if l > klas_maks_l:
                return False
            if (
                bonus_zaboroneno_v_deni_pozaurochnyh
                and l == klas_overall_maks_l
                and d in dni_pozaurochnyh_cogo_klasu
            ):
                return False
            if l == 1 and vazhkyi_1_urok_zaboroneno:
                return False
            # закріплений день (якщо заданий) діє НЕЗАЛЕЖНО від того, чи заданий ще й
            # конкретний урок - раніше перевірка вимагала ОБИДВА одночасно, і "лише
            # день без уроку" мовчки нічого не обмежував (11 клас "випадково" потрапляв
            # на п'ятницю через м'який штраф для легких предметів, а не через саму
            # прив'язку - жорсткого обмеження дня фактично не було).
            if efektyvnyi_fiksovanyi_den and d != efektyvnyi_fiksovanyi_den:
                return False
            if e["ostannim_urokom"]:
                # НЕ фіксуємо конкретний номер уроку тут - "останній" означає "останній
                # ЗАЙНЯТИЙ урок дня для цього класу", який може бути різним щодня залежно
                # від того, скільки реально вийшло звичайних уроків. Сама умова "нічого
                # після" накладається окремим обмеженням нижче, після побудови occupied_klas.
                pass
            elif e["fiksovanyi_urok"]:
                # якщо предмету потрібна пара (здвоєні уроки) - fiksovanyi_urok задає
                # СТАРТ пари, дозволяємо і сам слот, і наступний за ним (інакше пара
                # ніколи не сформується: другий слот пари завжди не дорівнює
                # fiksovanyi_urok і одразу відсіювався б цією ж перевіркою).
                if pary_potribno > 0:
                    if l not in (e["fiksovanyi_urok"], e["fiksovanyi_urok"] + 1):
                        return False
                elif l != e["fiksovanyi_urok"]:
                    return False
            elif l in forbidden:
                return False
            if (e["vchytel_id"], d, l) in data["teacher_unavail"]:
                return False
            # для групового запису (клас-комплект) - недоступність БУДЬ-ЯКОГО члена
            # групи блокує спільний слот для всіх (фізично разом, не можуть розділитись)
            for kid in entry_klas_ids(e):
                if (kid, d, l) in data["class_unavail"]:
                    return False
            return True

        allowed = [(d, l) for d in days for l in lessons if _slot_ok(d, l)]
        entry_allowed_slots[eid] = allowed
        klas_maks_l_by_entry[eid] = klas_overall_maks_l
        for (d, l) in allowed:
            x[eid, d, l] = model.NewBoolVar(f"x_{eid}_{d}_{l}")

        allowed_set = set(allowed)
        pair_starts = []
        if pary_potribno > 0:
            for (d, l) in allowed:
                if (d, l + 1) in allowed_set:
                    pair_starts.append((d, l))
        entry_allowed_pair_starts[eid] = pair_starts
        for (d, l) in pair_starts:
            p[eid, d, l] = model.NewBoolVar(f"p_{eid}_{d}_{l}")

        H = e["godyn_tyzhden"]
        unplaced[eid] = model.NewIntVar(0, H, f"unplaced_{eid}")
        model.Add(
            sum(x[eid, d, l] for (d, l) in allowed)
            + 2 * sum(p[eid, d, l] for (d, l) in pair_starts)
            + unplaced[eid] == H
        )
        # рівно стільки пар, скільки заплановано (якщо можливо - інакше піде в unplaced
        # через нестачу одиночних+парних відповідно до рівняння вище). Якщо в предметі
        # увімкнено "Дозволені здвоєні уроки" (zdvoyeni_urok) - пара ОБОВ'ЯЗКОВА, а не
        # просто дозволена: солвер не може підмінити її двома окремими одиночними
        # уроками, навіть якщо це для нього десь вигідніше (напр. Захист України -
        # 2 год мають стояти РАЗОМ, а не порізно як "останній урок" різних днів).
        if pair_starts:
            if e["zdvoyeni_urok"] and pary_potribno > 0:
                model.Add(sum(p[eid, d, l] for (d, l) in pair_starts) == pary_potribno)
            else:
                model.Add(sum(p[eid, d, l] for (d, l) in pair_starts) <= pary_potribno)

        # у слоті l не може одночасно початись пара і стояти одиночний урок з неї ж -
        # природно забороняється через obмеження occ (клас/вчитель) <= 1 нижче

        M = e["maks_urokiv_den"] or 1
        for d in days:
            single_terms = [x[eid, d, l] for l in lessons if (eid, d, l) in x]
            pair_terms = [p[eid, d, l] for l in lessons if (eid, d, l) in p]
            if single_terms or pair_terms:
                model.Add(sum(single_terms) + 2 * sum(pair_terms) <= M)

    def _occ_terms(eid, d, l):
        """Всі внески цього запису плану в 'зайнятість' слоту (d,l):
        одиночний урок тут, пара, що починається тут, або пара з попереднього слоту."""
        terms = []
        if (eid, d, l) in x:
            terms.append(x[eid, d, l])
        if (eid, d, l) in p:
            terms.append(p[eid, d, l])
        if l > 1 and (eid, d, l - 1) in p:
            terms.append(p[eid, d, l - 1])
        return terms

    # ---- члени міжпредметних груп (крім першого-"якоря") НЕ рахуються в зайнятості
    # класу окремо - вони йдуть паралельно з якорем в той самий слот (різні предмети,
    # той самий фізичний час класу). Синхронізація - окремим обмеженням нижче. ----
    mizhpredmetni_ne_yakir = set()
    _grupy_dlya_occ = {}
    for e in entries:
        gid = e.get("mizhpredmetna_grupa_id")
        if gid:
            _grupy_dlya_occ.setdefault(gid, []).append(e["id"])
    for gid, eids in _grupy_dlya_occ.items():
        for other_id in eids[1:]:
            mizhpredmetni_ne_yakir.add(other_id)

    # ---- клас: не більше одного уроку в слот (одиночний або частина пари);
    # збираємо occupied для мінімізації вікон. Тіньові записи підгруп (is_pidgrupa_shadow)
    # НЕ враховуються тут - вони йдуть паралельно з основним записом в той самий слот
    # класу (клас не подвоюється), синхронізація забезпечується окремим обмеженням нижче ----
    occupied_klas = {}  # (klas_id, d, l, tydzhen) -> вираз (сума, має бути 0 або 1)
    klasy_ids = set()
    for e in entries:
        klasy_ids.update(entry_klas_ids(e))
    for klas_id in klasy_ids:
        klas_entries = [
            e for e in entries
            if klas_id in entry_klas_ids(e)
            and not e["is_pidgrupa_shadow"]
            and e["id"] not in mizhpredmetni_ne_yakir
        ]
        for d in days:
            for l in lessons:
                for tydzhen in TYZHNI:
                    vars_here = []
                    for e in klas_entries:
                        if tydzhen not in _entry_tyzhni(e):
                            continue
                        vars_here.extend(_occ_terms(e["id"], d, l))
                    if vars_here:
                        if len(vars_here) > 1:
                            model.Add(sum(vars_here) <= 1)
                        occupied_klas[klas_id, d, l, tydzhen] = sum(vars_here)

    # ---- "останнім уроком дня" (виховна тощо) - РЕАЛЬНО останній ЗАЙНЯТИЙ урок, а не
    # фіксований номер за паралеллю. Якщо клас має, скажімо, лише 5 звичайних уроків
    # того дня - виховна стає 6-м, а не штучно "7-м із дірою перед нею". Якщо запис
    # розміщений на слоті l, забороняємо будь-яку зайнятість класу на пізніших слотах
    # того ж дня (до klas_maks_l включно). ----
    for e in entries:
        if not e["ostannim_urokom"]:
            continue
        eid = e["id"]
        klas_maks_l = klas_maks_l_by_entry.get(eid, data["urokiv"])
        e_tyzhni = _entry_tyzhni(e)
        for (d, l) in entry_allowed_slots.get(eid, []):
            if (eid, d, l) not in x:
                continue
            for klas_id in entry_klas_ids(e):
                for tydzhen in e_tyzhni:
                    pislya = [
                        occupied_klas[klas_id, d, l2, tydzhen]
                        for l2 in range(l + 1, klas_maks_l + 1)
                        if (klas_id, d, l2, tydzhen) in occupied_klas
                    ]
                    for term in pislya:
                        model.Add(term == 0).OnlyEnforceIf(x[eid, d, l])

        # той самий принцип для ЗДВОЄНОЇ пари (напр. Захист України 2 год поспіль) -
        # пара займає l і l+1, тож "нічого після" рахуємо від l+2 (останній фізичний
        # слот пари - l+1, а не l). Раніше ця перевірка стосувалась лише одиночних
        # уроків (x) - для пар (p) не діяла взагалі, "останнім уроком" мовчки ігнорувалось.
        for (d, l) in entry_allowed_pair_starts.get(eid, []):
            if (eid, d, l) not in p:
                continue
            for klas_id in entry_klas_ids(e):
                for tydzhen in e_tyzhni:
                    pislya = [
                        occupied_klas[klas_id, d, l2, tydzhen]
                        for l2 in range(l + 2, klas_maks_l + 1)
                        if (klas_id, d, l2, tydzhen) in occupied_klas
                    ]
                    for term in pislya:
                        model.Add(term == 0).OnlyEnforceIf(p[eid, d, l])

    # ---- гігієнічна шкала важкості (C1/C2/C3) за timetable_generator_constraints.md -
    # ВІКОВІ правила (різні для 1-4 / 5-9 / 10-12), а не однакові для всіх ----

    # ---- Мінімальний проміжок між уроками одного предмета (у днях) - поле в базі
    # існувало давно, але генератор його ІГНОРУВАВ (реалізовано зараз). Якщо
    # заданий N - два уроки того самого запису НЕ можуть стояти на днях, відстань
    # між якими МЕНША за N (напр. N=2: понеділок+вівторок заборонено, потрібно
    # щонайменше через день). ЖОРСТКЕ правило (санітарна логіка, не побажання). ----
    for e in entries:
        eid = e["id"]
        min_prom = e.get("min_promizhok_dni")
        if not min_prom or min_prom <= 0:
            continue

        # "день використано" - true, якщо хоч один урок цього запису стоїть у цей день
        den_vykorystano = {}
        for d in days:
            terms_dnya = [x[eid, d, l] for l in lessons if (eid, d, l) in x]
            terms_dnya += [p[eid, d, l] for l in lessons if (eid, d, l) in p]
            if not terms_dnya:
                continue
            dv = model.NewBoolVar(f"den_vykor_{eid}_{d}")
            model.AddMaxEquality(dv, terms_dnya)
            den_vykorystano[d] = dv

        for d1 in days:
            if d1 not in den_vykorystano:
                continue
            for d2 in days:
                if d2 <= d1 or d2 not in den_vykorystano:
                    continue
                if (d2 - d1) < min_prom:
                    model.Add(den_vykorystano[d1] + den_vykorystano[d2] <= 1)

    def _rv(e):
        """Реальна складність запису з урахуванням вікового винятку (укр. мова в 1-4)."""
        return realna_vazhkist(e["predmet_nazva"], e["vazhkist"], e["paralel"])

    # ---- Клас-комплект: СПІЛЬНІ записи (klas_ids, кілька членів) мають стояти
    # СТРОГО РАНІШЕ за ІНДИВІДУАЛЬНІ записи (звичайний klas_id, предмет, якого
    # немає в інших членів групи) того самого дня для того самого класу. Це
    # відповідає початковому задуму: перші уроки - спільна програма, пізніше -
    # те, чого немає в інших. Жорстке правило (не штраф) - м'які штрафи в цій
    # моделі вже не раз виявлялись ненадійними при тісних обмеженнях. ----
    komplekt_grupovi_za_klasom = {}   # klas_id -> [group entry, ...]
    komplekt_indyvidualni_za_klasom = {}  # klas_id -> [individual entry, ...]
    for e in entries:
        if "klas_ids" in e:
            for kid in e["klas_ids"]:
                komplekt_grupovi_za_klasom.setdefault(kid, []).append(e)
        elif e["klas_id"] in komplekt_po_klasu_dlya_poryadku:
            komplekt_indyvidualni_za_klasom.setdefault(e["klas_id"], []).append(e)

    for klas_id, grupovi in komplekt_grupovi_za_klasom.items():
        indyvidualni = komplekt_indyvidualni_za_klasom.get(klas_id, [])
        if not indyvidualni:
            continue
        for e_grp in grupovi:
            # легкі спільні записи (фізкультура тощо) звільняються від вимоги бути
            # СТРОГО спереду - інакше вони "притискають" індивідуальні C1/C2 записи
            # до кінця дня, де ті потрапляють під окрему заборону "C1/C2 не останнім
            # уроком" (правило з санітарного регламенту, встановлене раніше) -
            # конфлікт двох правил без цього винятку робив розклад нерозв'язним
            if _rv(e_grp) == "legkyi":
                continue
            for (d_grp, l_grp) in entry_allowed_slots.get(e_grp["id"], []):
                if (e_grp["id"], d_grp, l_grp) not in x:
                    continue
                for e_ind in indyvidualni:
                    for (d_ind, l_ind) in entry_allowed_slots.get(e_ind["id"], []):
                        if d_ind != d_grp or l_ind >= l_grp:
                            continue
                        if (e_ind["id"], d_ind, l_ind) not in x:
                            continue
                        # індивідуальний запис НЕ може стояти раніше за спільний
                        # того самого дня для того самого класу - забороняємо
                        # обом бути true одночасно
                        model.AddBoolOr([
                            x[e_grp["id"], d_grp, l_grp].Not(),
                            x[e_ind["id"], d_ind, l_ind].Not(),
                        ])

    # Правило: C1 на 1-му уроці - жорстка заборона (для 1-4/5-9, довгий день) тепер
    # застосовується РАНІШЕ, на етапі формування allowed (_slot_ok вище) - такі
    # слоти для x просто НІКОЛИ не створюються, тож тут для x лишається тільки
    # м'який випадок (10-12 класи чи короткий день) - збираємо його в штраф.
    # Для p (пари) свідомо НЕ переносимо цю оптимізацію - окремий, менш критичний
    # випадок, лишаємо як було раніше.
    vazhkyi_na_1_urotsi_terms = []  # для 10-12 - м'який штраф замість жорсткої заборони
    for (eid, d, l), var in x.items():
        e = entries_by_id[eid]
        if l != 1 or _rv(e) != "vazhkyi":
            continue
        vazhkyi_na_1_urotsi_terms.append(var)
    for (eid, d, l), var in p.items():
        e = entries_by_id[eid]
        if l != 1 or _rv(e) != "vazhkyi":
            continue
        pravyla = vikovi_pravyla_vazhkosti(e["paralel"])
        if pravyla["c1_zaboroneno_1_zhorstko"] and klas_maks_l_by_entry.get(eid, 8) >= 5:
            model.Add(var == 0)
        else:
            vazhkyi_na_1_urotsi_terms.append(var)

    # персистентні мапи зайнятості по кожній категорії складності - потрібні для решти
    # правил. Теж по доріжках парності - лічильники C1/C2 на день рахуються ОКРЕМО для
    # кожного тижня (різний реальний вміст).
    vazhkyi_occupied_all = {}   # C1: (klas_id, d, l, tydzhen)
    serednii_occupied_all = {}  # C2: (klas_id, d, l, tydzhen)
    for klas_id in klasy_ids:
        for kategoriya, mapa in [("vazhkyi", vazhkyi_occupied_all), ("serednii", serednii_occupied_all)]:
            entries_kat = [
                e for e in entries
                if klas_id in entry_klas_ids(e)
                and not e["is_pidgrupa_shadow"]
                and e["id"] not in mizhpredmetni_ne_yakir
                and _rv(e) == kategoriya
            ]
            if not entries_kat:
                continue
            for d in days:
                for l in lessons:
                    for tydzhen in TYZHNI:
                        terms_tut = []
                        for e in entries_kat:
                            if tydzhen not in _entry_tyzhni(e):
                                continue
                            terms_tut.extend(_occ_terms(e["id"], d, l))
                        if terms_tut:
                            mapa[klas_id, d, l, tydzhen] = sum(terms_tut)

    klas_maks_l_po_klasu = {}
    klas_paralel_po_klasu = {}
    for e in entries:
        for klas_id in entry_klas_ids(e):
            if klas_id not in klas_maks_l_po_klasu:
                klas_maks_l_po_klasu[klas_id] = klas_maks_l_by_entry.get(e["id"], data["urokiv"])
                klas_paralel_po_klasu[klas_id] = klas_info_by_id[klas_id]["paralel"]

    # Правило: на останньому зайнятому уроці дня - заборонено C1 завжди; для 1-4 класів
    # ДОДАТКОВО забороняється й C2 (дозволено лише C3).
    for e in entries:
        if e["is_pidgrupa_shadow"] or e["id"] in mizhpredmetni_ne_yakir:
            continue
        rv = _rv(e)
        if rv not in ("vazhkyi", "serednii"):
            continue
        pravyla = vikovi_pravyla_vazhkosti(e["paralel"])
        potribno_zaboronyty = (
            (rv == "vazhkyi" and pravyla["c1_zaboroneno_ostannii_zhorstko"]) or
            (rv == "serednii" and pravyla["c2_zaboroneno_ostannii_zhorstko"])
        )
        if not potribno_zaboronyty:
            continue
        eid = e["id"]
        klas_maks_l = klas_maks_l_by_entry.get(eid, data["urokiv"])
        if klas_maks_l < 5:
            continue
        e_tyzhni = _entry_tyzhni(e)
        for (d, l) in entry_allowed_slots.get(eid, []):
            if (eid, d, l) not in x:
                continue
            if l == klas_maks_l:
                model.Add(x[eid, d, l] == 0)
                continue
            for klas_id in entry_klas_ids(e):
                for tydzhen in e_tyzhni:
                    pislya_terms = [
                        occupied_klas[klas_id, d, l2, tydzhen]
                        for l2 in range(l + 1, klas_maks_l + 1)
                        if (klas_id, d, l2, tydzhen) in occupied_klas
                    ]
                    if pislya_terms:
                        model.Add(sum(pislya_terms) >= 1).OnlyEnforceIf(x[eid, d, l])

    # Правило (ЖОРСТКЕ, конкретні числа за документом, не відсотки): максимум C1 на
    # Пн/Пт і окремо на Вт/Ср/Чт, за віковою групою класу.
    for klas_id in klasy_ids:
        paralel = klas_paralel_po_klasu.get(klas_id, 5)
        pravyla = vikovi_pravyla_vazhkosti(paralel)
        for d in days:
            for tydzhen in TYZHNI:
                vazhki_dnya = [
                    vazhkyi_occupied_all[klas_id, d, l, tydzhen] for l in lessons
                    if (klas_id, d, l, tydzhen) in vazhkyi_occupied_all
                ]
                if not vazhki_dnya:
                    continue
                limit = pravyla["maks_c1_pn_pt"] if d in (1, 5) else pravyla["maks_c1_vt_sr_cht"]
                model.Add(sum(vazhki_dnya) <= limit)

    # Правило (ЖОРСТКЕ): не більше N важких поспіль (1-4: заборонено навіть 2 поспіль;
    # 5-9 і 10-12: дозволено 2, забороняється 3+) - перевіряємо кожне вікно довжиною
    # (ліміт+1) послідовних уроків, забороняючи суму понад ліміт у ньому.
    for klas_id in klasy_ids:
        paralel = klas_paralel_po_klasu.get(klas_id, 5)
        pravyla = vikovi_pravyla_vazhkosti(paralel)
        limit_pospil = pravyla["maks_c1_pospil"]
        vikno = limit_pospil + 1
        for d in days:
            for tydzhen in TYZHNI:
                for l_start in lessons:
                    l_kinets = l_start + vikno - 1
                    if l_kinets > max(lessons):
                        break
                    terms_vikna = [
                        vazhkyi_occupied_all[klas_id, d, l2, tydzhen] for l2 in range(l_start, l_kinets + 1)
                        if (klas_id, d, l2, tydzhen) in vazhkyi_occupied_all
                    ]
                    if len(terms_vikna) == vikno:  # усі уроки вікна реально можливі для цього класу
                        model.Add(sum(terms_vikna) <= limit_pospil)

    # ---- рівномірність: мінімізуємо розкид (макс. - мін.) кількості уроків на день
    # для кожного класу, щоб не було "7 уроків в один день, 5 в інший" без потреби ----
    rozkyd_terms = []
    for klas_id in klasy_ids:
        for tydzhen in TYZHNI:
            maye_zapysy = any((klas_id, d, l, tydzhen) in occupied_klas for d in days for l in lessons)
            if not maye_zapysy:
                continue
            denni_sumy = []
            for d in days:
                terms_dnya = [
                    occupied_klas[klas_id, d, l, tydzhen] for l in lessons
                    if (klas_id, d, l, tydzhen) in occupied_klas
                ]
                denni_sumy.append(sum(terms_dnya) if terms_dnya else 0)
            min_denna = model.NewIntVar(0, data["urokiv"], f"min_denna_{klas_id}_{tydzhen}")
            max_denna = model.NewIntVar(0, data["urokiv"], f"max_denna_{klas_id}_{tydzhen}")
            for suma_dnya in denni_sumy:
                model.Add(min_denna <= suma_dnya)
                model.Add(max_denna >= suma_dnya)
            rozkyd_terms.append(max_denna - min_denna)

    # ---- підгрупи: синхронізуємо розміщення пари записів - обидва мають стояти
    # РІВНО в тих самих слотах (клас на одному й тому ж уроці, різні вчителі/кабінети).
    # Використовуємо ОБ'ЄДНАННЯ дозволених слотів обох записів (кожен може мати свої
    # особливості доступності вчителя) - інакше частина слотів другого запису
    # лишається неврахованою. ----
    def _sync_pair(eid_a, eid_b):
        slots_union = set(entry_allowed_slots.get(eid_a, [])) | set(entry_allowed_slots.get(eid_b, []))
        for (d, l) in slots_union:
            x_a = x.get((eid_a, d, l))
            x_b = x.get((eid_b, d, l))
            if x_a is not None and x_b is not None:
                model.Add(x_a == x_b)
            elif x_a is not None:
                model.Add(x_a == 0)
            elif x_b is not None:
                model.Add(x_b == 0)

        pair_union = set(entry_allowed_pair_starts.get(eid_a, [])) | set(entry_allowed_pair_starts.get(eid_b, []))
        for (d, l) in pair_union:
            p_a = p.get((eid_a, d, l))
            p_b = p.get((eid_b, d, l))
            if p_a is not None and p_b is not None:
                model.Add(p_a == p_b)
            elif p_a is not None:
                model.Add(p_a == 0)
            elif p_b is not None:
                model.Add(p_b == 0)

    already_synced = set()
    for e in entries:
        eid = e["id"]
        link_id = e["pidgrupa_link"]
        if link_id is None or (eid, link_id) in already_synced or (link_id, eid) in already_synced:
            continue
        already_synced.add((eid, link_id))
        _sync_pair(eid, link_id)

    # ---- міжпредметні групи поділу: N записів (РІЗНІ предмети) того самого класу мають
    # стояти в тих самих слотах - половина/третина класу вивчає один предмет, решта -
    # інший, одночасно, потім чергування визначає сам solver. Синхронізуємо ланцюжком
    # (перший запис групи == кожен наступний), що еквівалентно повній рівності всіх. ----
    for gid, eids in _grupy_dlya_occ.items():
        if len(eids) < 2:
            continue
        anchor = eids[0]
        for other in eids[1:]:
            _sync_pair(anchor, other)

    # ---- вчитель: не більше одного уроку в слот (без мінімізації вікон для вчителя -
    # спрощення). По доріжках парності - вчитель МОЖЕ вести парний-тижневий урок і
    # непарний-тижневий урок у той самий слот шаблону (різні реальні тижні). ----
    vchyteli_ids = {e["vchytel_id"] for e in entries}
    vikna_vchyteliv_terms = []   # s_l (+1 за початок блоку) і -h (за сам факт роботи в день)
    # перемикач "уникати вікон у вчителів" з Налаштувань школи - вимкнено = 0 змінних
    vaha_vikon_vchyteliv = PENALTY_VIKNO_VCHYTELYA if data.get("vikna_vchyteliv", False) else 0
    for vchytel_id in vchyteli_ids:
        v_entries = [e for e in entries if e["vchytel_id"] == vchytel_id]
        for d in days:
            for l in lessons:
                for tydzhen in TYZHNI:
                    vars_here = []
                    for e in v_entries:
                        if tydzhen not in _entry_tyzhni(e):
                            continue
                        vars_here.extend(_occ_terms(e["id"], d, l))
                    if len(vars_here) > 1:
                        model.Add(sum(vars_here) <= 1)

        # ---- вікна у вчителя (м'який штраф). Для кожного дня й доріжки парності:
        # b_l = чи веде вчитель урок l (бул, дорівнює сумі його внесків у слот, яка
        # й так <= 1 за правилом вище; урок з іншого рівня чи заморожений - константа 1);
        # s_l = "тут починається новий блок уроків" (s_l >= b_l - b_{l-1});
        # h = чи взагалі працює того дня. Кількість вікон = (сума s_l) - h, і саме
        # це йде у штраф. Вчитель з одним уроком на тиждень вікон мати не може -
        # для нього змінних не створюємо. ----
        if vaha_vikon_vchyteliv > 0 and (
            sum(e["godyn_tyzhden"] for e in v_entries) + sum(1 for (v, _d, _l) in sloty_vch_poza_prohonom if v == vchytel_id) >= 2
        ):
            for d in days:
                for tydzhen in TYZHNI:
                    b = {}
                    for l in lessons:
                        if (vchytel_id, d, l) in sloty_vch_poza_prohonom:
                            b[l] = model.NewConstant(1)
                            continue
                        terms = []
                        for e in v_entries:
                            if tydzhen not in _entry_tyzhni(e):
                                continue
                            terms.extend(_occ_terms(e["id"], d, l))
                        if terms:
                            bl = model.NewBoolVar(f"vb_{vchytel_id}_{d}_{l}_{tydzhen}")
                            model.Add(sum(terms) == bl)
                            b[l] = bl
                    if len(b) < 2:
                        continue
                    h = model.NewBoolVar(f"vh_{vchytel_id}_{d}_{tydzhen}")
                    model.AddMaxEquality(h, list(b.values()))
                    poperednii = None
                    for l in lessons:
                        if l not in b:
                            poperednii = None      # тут уроку бути не може - блок перервано
                            continue
                        s_l = model.NewBoolVar(f"vs_{vchytel_id}_{d}_{l}_{tydzhen}")
                        if poperednii is None:
                            model.Add(s_l >= b[l])
                        else:
                            model.Add(s_l >= b[l] - poperednii)
                        vikna_vchyteliv_terms.append(s_l)
                        poperednii = b[l]
                    vikna_vchyteliv_terms.append(-h)

        # ---- "Максимум уроків на день" з картки вчителя - ЖОРСТКО. Рахуємо всі
        # уроки вчителя за день (пара дає 2 через _occ_terms), окремо по доріжках
        # парності; від ліміту віднімаємо те, що вже стоїть поза цим прогоном
        # (інші рівні, заморожені дні) і резерв під позаурочні цього дня (їх
        # ставить жадібне розміщення нижче тим самим лімітом). Якщо ліміт
        # замалий - зайве піде в нерозміщені, а не порушить правило. ----
        maks_den = data["vchyteli_maks_den"].get(vchytel_id)
        if maks_den:
            for d in days:
                dostupno = max(0, maks_den - zainyato_vch_den.get((vchytel_id, d), 0)
                               - rezerv_pozaur_den.get((vchytel_id, d), 0))
                for tydzhen in TYZHNI:
                    terms_dnya = []
                    for e in v_entries:
                        if tydzhen not in _entry_tyzhni(e):
                            continue
                        for l in lessons:
                            terms_dnya.extend(_occ_terms(e["id"], d, l))
                    if terms_dnya:
                        model.Add(sum(terms_dnya) <= dostupno)

    # ---- клас: заборона порожніх днів (сильний штраф), якщо годин вистачає на кожен день ----
    empty_day_terms = []
    klas_total_hours = {}
    for klas_id in klasy_ids:
        klas_total_hours[klas_id] = sum(
            e["godyn_tyzhden"] for e in entries if klas_id in entry_klas_ids(e)
        )

    for klas_id in klasy_ids:
        if klas_total_hours[klas_id] < len(days):
            continue  # у класу об'єктивно замало годин, щоб бути кожен день - не караємо
        for d in days:
            for tydzhen in TYZHNI:
                day_terms = [
                    occupied_klas[klas_id, d, l, tydzhen] for l in lessons
                    if (klas_id, d, l, tydzhen) in occupied_klas
                ]
                if not day_terms:
                    continue
                # М'ЯКИЙ, але дуже сильний штраф (не жорстка вимога - та ламала фізичну
                # здійсненність генерації через конфлікт з іншими обмеженнями типу
                # фіксованих днів/недоступності). Вага підвищена з 200 до 800, щоб
                # реально переважувала інші м'які цілі (розкид, вікна тощо).
                has_lesson = model.NewBoolVar(f"has_lesson_{klas_id}_{d}_{tydzhen}")
                day_sum = sum(day_terms)
                model.Add(day_sum >= has_lesson)
                model.Add(day_sum <= len(lessons) * has_lesson)
                empty_day_terms.append(1 - has_lesson)

    # ---- ЖОРСТКА вимога: день, на який заплановане позаурочне заняття з фіксованим
    # днем (виховна тощо), НЕ може лишитись зовсім без навчальних уроків - інакше
    # клас "приходить у школу" лише на 1 позаурочний урок, що нереалістично. Солвер
    # сам по собі НЕ знає про позаурочні (вони розміщуються ПІСЛЯ основного
    # розв'язку), тож без цього обмеження він може випадково спорожнити саме той
    # день, залишивши позаурочне єдиним уроком дня. ----
    dni_z_pozaurochnymy = {
        (pe["klas_id"], d)
        for pe in data.get("pozaurochni_entries", [])
        for d in _parse_dni_tyzhnya(pe["fiksovanyi_den"])
    }
    for klas_id, d in dni_z_pozaurochnymy:
        if klas_id not in klasy_ids or klas_total_hours.get(klas_id, 0) < len(days):
            continue
        for tydzhen in TYZHNI:
            day_terms = [
                occupied_klas[klas_id, d, l, tydzhen] for l in lessons
                if (klas_id, d, l, tydzhen) in occupied_klas
            ]
            if day_terms:
                model.Add(sum(day_terms) >= 1)


    # ---- мінімізація вікон (і "дірок на старті дня") для класів ----
    # (для вчителів свідомо не робимо - обчислювально значно важче і менш критично для v1)
    # gap_start[l] >= occupied[l+1] - occupied[l]: змушує gap_start=1, якщо урок l вільний,
    # а урок l+1 зайнятий (тобто після "дірки" щось є). Мінімізація сама притисне gap_start
    # до 0 всюди, де це можливо - реальний ефект: розклад стискається до початку дня.
    gap_terms = []
    for group_id in klasy_ids:
        for d in days:
            for tydzhen in TYZHNI:
                for l in lessons[:-1]:
                    if (group_id, d, l + 1, tydzhen) not in occupied_klas:
                        continue
                    occ_l = occupied_klas.get((group_id, d, l, tydzhen), 0)
                    occ_l1 = occupied_klas[group_id, d, l + 1, tydzhen]
                    gap_var = model.NewBoolVar(f"gap_klas_{group_id}_{d}_{l}_{tydzhen}")
                    model.Add(gap_var >= occ_l1 - occ_l)
                    gap_terms.append(gap_var)

    # ---- кабінети: предмети зі спецкабінетом вибирають з пулу (кілька класів можуть
    # потребувати ту саму лабораторію/спортзал у різний час), а звичайні предмети - строго
    # у СВОЄМУ фіксованому домашньому кабінеті (жорстка прив'язка "клас -> один кабінет",
    # бо там і виховна година, і клас має бути завжди на своєму місці). Кожен клас повинен
    # мати УНІКАЛЬНИЙ домашній кабінет - це забезпечується на етапі заповнення довідників,
    # не тут; якщо два класи випадково отримають той самий домашній кабінет, це буде видно
    # як конфлікт при генерації.
    # ---- кабінети: якщо для уроку (предмет+вчитель) знайдено СПЕЦІАЛЬНО прив'язаний
    # кабінет (тип "за предметом"/"за вчителем"/"комбінація" на самому кабінеті) -
    # генератор ЗОБОВ'ЯЗАНИЙ використати один із них, не домашній кабінет класу.
    # Якщо жодної спеціальної прив'язки немає -
    # як і раніше, звичайний предмет іде у СВІЙ фіксований домашній кабінет класу.
    # "Універсальний" кабінет - вільний слот, нічого специфічно не претендує на нього
    # тут (сам не бере участі в цій перевірці, лишається доступним іншим механізмам).
    kabinety_by_id = {k["id"]: k for k in data["kabinety"]}
    room_bookings = {}  # (kabinet_id, d, l, tydzhen) -> list of BoolVar
    y_by_slot = {}       # (entry_id, d, l) -> list of kabinet_id (спеціальна прив'язка)
    y_pair_by_slot = {}  # (entry_id, d, l) -> list of kabinet_id (пари, спеціальна прив'язка)
    spets_kabinet_bonus_terms = []  # y-вар 1-ї підгрупи поділу у справжньому спецкабінеті

    for e in entries:
        eid = e["id"]
        e_tyzhni = _entry_tyzhni(e)

        spetsyalni_kabinety = []
        for kid, kab in kabinety_by_id.items():
            typ = kab["typ_pryvyazky"]
            if typ == "predmet":
                if e["predmet_id"] in data["kabinet_dozvoleni_predmety"].get(kid, set()):
                    spetsyalni_kabinety.append(kid)
            elif typ == "vchytel":
                if e["vchytel_id"] in data["kabinet_dozvoleni_vchyteli"].get(kid, set()):
                    spetsyalni_kabinety.append(kid)
            elif typ == "kombinatsiya":
                if (
                    e["predmet_id"] in data["kabinet_dozvoleni_predmety"].get(kid, set())
                    and e["vchytel_id"] in data["kabinet_dozvoleni_vchyteli"].get(kid, set())
                ):
                    spetsyalni_kabinety.append(kid)
            # "universalnyi" - нічого специфічно не претендує, не додається сюди

        # Предмет із прапорцем spets_kabinet_lyshe_pidgrupa1 (напр. англійська):
        # спецкабінет (мовний) - це БОНУС лише для 1-ї підгрупи поділеного класу,
        # а не вимога для всього предмета. Реальна школа часто має ОДИН такий
        # кабінет.
        #   - нерозділений урок  -> домашній клас (skidaemo spets -> else-гілка);
        #   - 1-ша підгрупа поділу -> спецкабінет, запасний варіант домашній клас;
        #   - 2-га підгрупа (тіньовий запис) -> домашній клас АБО вільний
        #     універсальний кабінет (спецкабінет їй не потрібен).
        # Синхронність слота тримає _sync_pair, room_bookings <= 1 розводить
        # підгрупи по різних кабінетах.
        # роль запису при м'якій прив'язці спецкабінету (див. нижче); за замовч.
        # None - предмет без прапорця, звичайна жорстка прив'язка
        spets_lyshe_pg1_rol = None
        spets_realni_dlya_nagorody = []
        if spetsyalni_kabinety and e.get("spets_kabinet_lyshe_pidgrupa1"):
            home = e["domashniy_kabinet_id"]
            je_podil = e.get("pidgrupa_link") is not None
            if not je_podil:
                spets_lyshe_pg1_rol = "nerozdilenyi"
                spetsyalni_kabinety = []          # нерозділений -> домашній клас (else)
            elif e.get("is_pidgrupa_shadow"):
                spets_lyshe_pg1_rol = "pidgrupa2"
                spetsyalni_kabinety = ([home] if home else []) + data.get("vilni_universalni_kab", [])
            else:
                spets_lyshe_pg1_rol = "pidgrupa1"
                spets_realni_dlya_nagorody = list(spetsyalni_kabinety)  # справжній спецкабінет
                spetsyalni_kabinety = spetsyalni_kabinety + ([home] if home else [])

        # МІЖПРЕДМЕТНА ГРУПА: усі її записи стоять в ОДНОМУ слоті класу, тобто
        # клас фізично розходиться по РІЗНИХ кабінетах одночасно (напр. частина на
        # англійській, частина на німецькій). Один запис може лишитись у домашньому
        # класі, решті потрібен вільний універсальний кабінет. Без цього обидва
        # записи претендують на ТОЙ САМИЙ домашній кабінет, room_bookings <= 1
        # робить слот неможливим - і група не розкладається взагалі (реальний
        # випадок: 9-В англ.||нім., обидва записи нерозміщені).
        # Якщо в запису є СПРАВЖНІЙ спецкабінет (інформатика тощо) - не чіпаємо,
        # він і так іде не в домашній клас.
        if e.get("mizhpredmetna_grupa_id") and not spetsyalni_kabinety:
            _home = e["domashniy_kabinet_id"]
            spetsyalni_kabinety = (([_home] if _home else [])
                                   + data.get("vilni_universalni_kab", []))

        if spetsyalni_kabinety:
            # для запису поділу (1-ша чи 2-га підгрупа) у кабінеті сидить ЛИШЕ
            # половина класу - інакше спецкабінет на 18 місць хибно відсіювався
            # для класу з 32 учнів, хоч підгрупа з ~16 туди влазить
            je_podil_zap = e.get("pidgrupa_link") is not None
            efekt_uchniv = (e["kilkist_uchniv"] or 0)
            if je_podil_zap and efekt_uchniv:
                efekt_uchniv = -(-efekt_uchniv // 2)  # ceil(uchniv/2)
            rooms = [
                kid for kid in spetsyalni_kabinety
                if kabinety_by_id[kid]["mistkist"] >= efekt_uchniv
            ] or spetsyalni_kabinety

            for (d, l) in entry_allowed_slots[eid]:
                if (eid, d, l) not in x:
                    continue
                available_rooms = [r for r in rooms if (r, d, l) not in data["kabinet_unavail"]]
                if not available_rooms:
                    model.Add(x[eid, d, l] == 0)
                    continue
                for r in available_rooms:
                    y[eid, d, l, r] = model.NewBoolVar(f"y_{eid}_{d}_{l}_{r}")
                    for tydzhen in e_tyzhni:
                        room_bookings.setdefault((r, d, l, tydzhen), []).append(y[eid, d, l, r])
                    y_by_slot.setdefault((eid, d, l), []).append(r)
                    # м'яка перевага: 1-шу підгрупу поділу для предмета з
                    # spets_kabinet_lyshe_pidgrupa1 тягнемо У спецкабінет (мовний),
                    # а не в домашній клас - слабкий бонус, не жорстко, щоб не
                    # створити вікна, коли кабінет зайнятий іншим класом
                    if r in spets_realni_dlya_nagorody:
                        spets_kabinet_bonus_terms.append(y[eid, d, l, r])
                model.Add(sum(y[eid, d, l, r] for r in available_rooms) == x[eid, d, l])

            for (d, l) in entry_allowed_pair_starts.get(eid, []):
                if (eid, d, l) not in p:
                    continue
                available_rooms = [
                    r for r in rooms
                    if (r, d, l) not in data["kabinet_unavail"] and (r, d, l + 1) not in data["kabinet_unavail"]
                ]
                if not available_rooms:
                    model.Add(p[eid, d, l] == 0)
                    continue
                for r in available_rooms:
                    y_pair[eid, d, l, r] = model.NewBoolVar(f"yp_{eid}_{d}_{l}_{r}")
                    for tydzhen in e_tyzhni:
                        room_bookings.setdefault((r, d, l, tydzhen), []).append(y_pair[eid, d, l, r])
                        room_bookings.setdefault((r, d, l + 1, tydzhen), []).append(y_pair[eid, d, l, r])
                    y_pair_by_slot.setdefault((eid, d, l), []).append(r)
                model.Add(sum(y_pair[eid, d, l, r] for r in available_rooms) == p[eid, d, l])

        else:
            home_kabinet = e["domashniy_kabinet_id"]
            if not home_kabinet:
                continue
            for (d, l) in entry_allowed_slots[eid]:
                if (home_kabinet, d, l) in data["kabinet_unavail"]:
                    if (eid, d, l) in x:
                        model.Add(x[eid, d, l] == 0)
                    continue
                if (eid, d, l) in x:
                    for tydzhen in e_tyzhni:
                        room_bookings.setdefault((home_kabinet, d, l, tydzhen), []).append(x[eid, d, l])

            for (d, l) in entry_allowed_pair_starts.get(eid, []):
                if (eid, d, l) not in p:
                    continue
                if (home_kabinet, d, l) in data["kabinet_unavail"] or (home_kabinet, d, l + 1) in data["kabinet_unavail"]:
                    model.Add(p[eid, d, l] == 0)
                    continue
                for tydzhen in e_tyzhni:
                    room_bookings.setdefault((home_kabinet, d, l, tydzhen), []).append(p[eid, d, l])
                    room_bookings.setdefault((home_kabinet, d, l + 1, tydzhen), []).append(p[eid, d, l])

    for key, vars_here in room_bookings.items():
        if len(vars_here) > 1:
            model.Add(sum(vars_here) <= 1)
    # кабінет, зайнятий замороженою клітинкою (Догенерація) - жоден ІНШИЙ (вільний
    # для генерації) запис не може забронювати той самий кабінет у той самий слот.
    # Заморожені клітинки поки не несуть парності (Догенерація й парність - окрема
    # майбутня доробка) - блокуємо ОБИДВІ доріжки консервативно.
    for (kabinet_id, d, l) in pre_occ_kabinet:
        for tydzhen in TYZHNI:
            key = (kabinet_id, d, l, tydzhen)
            if key in room_bookings:
                for var in room_bookings[key]:
                    model.Add(var == 0)

    # ---- цільова функція ----
    objective_terms = []
    for eid, var in unplaced.items():
        objective_terms.append(var * PENALTY_UNPLACED)

    for (eid, d, l), var in x.items():
        entry = entries_by_id[eid]
        if (entry["vchytel_id"], d, l) in data["teacher_nebazhanyi"]:
            objective_terms.append(var * PENALTY_NEBAZHANYI)
        objective_terms.append(var * (l * WEIGHT_LATE_SLOT))
        if entry["ostannim_urokom"]:
            objective_terms.append(var * (-l * WEIGHT_OSTANNIM_UROKOM_PIZNO))
        # гігієнічна шкала важкості: важкі раніше в день і на вт/ср, легкі пізніше і на пт.
        # ВИНЯТОК: груповий запис клас-комплекту (klas_ids) - НЕ штрафуємо за ранню
        # позицію, навіть якщо легкий (напр. фізкультура) - інакше він "монополізує"
        # пізню позицію через цей штраф, залишаючи замало місця для індивідуальних
        # записів членів комплекту, які мають йти СТРОГО після групових (окреме
        # жорстке правило вище) - конфлікт цих двох механізмів забирав місце.
        rv = _rv(entry)
        is_grupovyi = "klas_ids" in entry
        if rv == "vazhkyi":
            objective_terms.append(var * (l * WEIGHT_VAZHKYI_PIZNIY_UROK))
            objective_terms.append(var * ((5 - DEN_VAZHKOSTI_RANG.get(d, 3)) * WEIGHT_DEN_VAZHKOSTI))
        elif rv == "legkyi" and not is_grupovyi:
            objective_terms.append(var * ((data["urokiv"] - l) * WEIGHT_LEGKYI_RANNIY_UROK))
            objective_terms.append(var * (DEN_VAZHKOSTI_RANG.get(d, 3) * WEIGHT_DEN_VAZHKOSTI))
        # НОВИЙ штраф: класи 6+ - неохота сягати ВЛАСНОГО денного максимуму (не
        # фіксоване число для всіх, особистий ліміт кожного класу). Мета - не
        # "розтягувати" день до максимуму без потреби. Скромна вага для першого тесту.
        if entry["paralel"] >= 6 and l == klas_maks_l_po_klasu.get(entry["klas_id"], -1):
            objective_terms.append(var * WEIGHT_MAKSYMALNYI_UROK_STARSHI)

    for (eid, d, l), var in p.items():
        entry = entries_by_id[eid]
        if (entry["vchytel_id"], d, l) in data["teacher_nebazhanyi"]:
            objective_terms.append(var * PENALTY_NEBAZHANYI)
        if (entry["vchytel_id"], d, l + 1) in data["teacher_nebazhanyi"]:
            objective_terms.append(var * PENALTY_NEBAZHANYI)
        # пара займає два слоти - враховуємо "пізність" обох
        objective_terms.append(var * ((l + (l + 1)) * WEIGHT_LATE_SLOT))
        if entry["ostannim_urokom"]:
            objective_terms.append(var * (-(l + (l + 1)) * WEIGHT_OSTANNIM_UROKOM_PIZNO))
        rv = _rv(entry)
        is_grupovyi = "klas_ids" in entry
        if rv == "vazhkyi":
            objective_terms.append(var * ((l + (l + 1)) * WEIGHT_VAZHKYI_PIZNIY_UROK))
            objective_terms.append(var * (2 * (5 - DEN_VAZHKOSTI_RANG.get(d, 3)) * WEIGHT_DEN_VAZHKOSTI))
        elif rv == "legkyi" and not is_grupovyi:
            objective_terms.append(var * ((data["urokiv"] - l + data["urokiv"] - l - 1) * WEIGHT_LEGKYI_RANNIY_UROK))
            objective_terms.append(var * (2 * DEN_VAZHKOSTI_RANG.get(d, 3) * WEIGHT_DEN_VAZHKOSTI))
        if entry["paralel"] >= 6 and (l + 1) == klas_maks_l_po_klasu.get(entry["klas_id"], -1):
            objective_terms.append(var * WEIGHT_MAKSYMALNYI_UROK_STARSHI)

    for gap_var in gap_terms:
        objective_terms.append(gap_var * PENALTY_GAP)

    # вікна у вчителів: сума (початків блоків - днів з уроками) = кількість вікон
    for term in vikna_vchyteliv_terms:
        objective_terms.append(term * vaha_vikon_vchyteliv)

    for term in empty_day_terms:
        objective_terms.append(term * PENALTY_EMPTY_DAY)

    for rozkyd_var in rozkyd_terms:
        objective_terms.append(rozkyd_var * PENALTY_ROZKYD)

    # 10-12 класи: C1 на 1-му уроці - лише м'який штраф (не жорстка заборона, за документом)
    for var in vazhkyi_na_1_urotsi_terms:
        objective_terms.append(var * WEIGHT_VAZHKYI_1_UROK_M_YAKYI)

    # м'яка перевага: 1-ша підгрупа поділу предмета зі spets_kabinet_lyshe_pidgrupa1
    # СТАЄ у справжній спецкабінет (мовний), а не в домашній клас. Слабка вага -
    # лише тай-брейкер, набагато менша за PENALTY_GAP, щоб ніколи не створити вікно
    # заради кабінету, коли той зайнятий іншим класом.
    for var in spets_kabinet_bonus_terms:
        objective_terms.append(var * (-WEIGHT_SPETS_KABINET_PG1))

    model.Minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = maks_sekund or MAX_SOLVE_SECONDS
    # кількість workers = реальна кількість фізичних ядер цього комп'ютера, визначена
    # ОДИН РАЗ при старті застосунку (init_db) через PowerShell/CIM і збережена в
    # школі - замість жорстко закодованого числа, яке могло б не відповідати
    # реальному залізу (менше ядер - зайве навантаження й гальмування ОС; більше -
    # недовикористана паралельність)
    fizychni_yadra_row = conn.execute("SELECT fizychni_yadra FROM school_settings WHERE id = 1").fetchone()
    workers = (fizychni_yadra_row["fizychni_yadra"] if fizychni_yadra_row else None) or 8
    solver.parameters.num_search_workers = workers
    # ВАЖЛИВО (рішення прийняте свідомо, не забути при майбутніх правках):
    # random_seed + interleave_search НЕ використовуються тут навмисно. Перевірено
    # емпірично: вони дають відтворюваність (той самий вхід -> той самий вихід), АЛЕ
    # (а) коштують реальної якості розкладу - на важкому кейсі (5-12 класи, 150с) давали
    #     стабільно 8-9 нерозміщених уроків проти 1-2 без них;
    # (б) сама відтворюваність НЕ гарантована на 100% при час-лімітованому пошуку -
    #     на довшому бюджеті (150с) навіть із цими параметрами результат "поплив" (9->8).
    # Відтворюваність важлива лише для ДІАГНОСТИКИ (порівняння до/після змін коду) -
    # для цього тримайте ці два параметри в ОКРЕМІЙ тестовій копії generator.py, не тут.
    # Завучу, який просто тисне "Згенерувати" один раз, потрібна найкраща якість за
    # відведений час, а не повторюваність - тож тут пріоритет однозначно якості.
    # Якщо колись знадобиться "повторити той самий розклад" як реальна фіча - варто
    # зберігати конкретний random_seed вдалого прогону в БД і давати можливість
    # "повторити з цим seed" за запитом, а не робити детермінізм дефолтною поведінкою.

    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        conn.close()
        return {
            "success": False,
            "message": "Розв'язок не знайдено. Перевірте, чи не занадто суперечливі обмеження "
                       "(доступність вчителів/класів, максимум уроків на день тощо).",
            "chas_sekund": round(time.time() - _chas_startu, 1),
        }

    # ---- вилучаємо результат ----
    results = list(fixed_results)  # заморожені (Догенерація) - вже готові, без участі солвера
    unplaced_report = []

    for eid, entry in entries_by_id.items():
        for (d, l) in entry_allowed_slots[eid]:
            if (eid, d, l) in x and solver.Value(x[eid, d, l]) == 1:
                kabinet_id = entry["domashniy_kabinet_id"]
                for r in y_by_slot.get((eid, d, l), []):
                    if solver.Value(y[eid, d, l, r]) == 1:
                        kabinet_id = r
                        break
                # груповий запис клас-комплекту - той самий урок одразу ВСІМ членам
                # групи (фізично разом, той самий вчитель/кабінет/час)
                for klas_id in entry_klas_ids(entry):
                    results.append({
                        "klas_id": klas_id,
                        "predmet_id": entry["predmet_id"],
                        "vchytel_id": entry["vchytel_id"],
                        "kabinet_id": kabinet_id,
                        "den": d,
                        "urok": l,
                        "parnist": entry.get("parnist") or "oboydva",
                        "pidgrupa_2_vchytel_id": entry.get("pidgrupa_2_vchytel_id")
                            if entry.get("pidgrupa_2_vchytel_id") == entry.get("vchytel_id") else None,
                    })

        for (d, l) in entry_allowed_pair_starts.get(eid, []):
            if (eid, d, l) in p and solver.Value(p[eid, d, l]) == 1:
                kabinet_id = entry["domashniy_kabinet_id"]
                for r in y_pair_by_slot.get((eid, d, l), []):
                    if solver.Value(y_pair[eid, d, l, r]) == 1:
                        kabinet_id = r
                        break
                # пара - два записи розкладу поспіль (l і l+1), той самий кабінет;
                # і, як і вище, кожному члену групи, якщо це груповий запис
                for urok in (l, l + 1):
                    for klas_id in entry_klas_ids(entry):
                        results.append({
                            "klas_id": klas_id,
                            "predmet_id": entry["predmet_id"],
                            "vchytel_id": entry["vchytel_id"],
                            "kabinet_id": kabinet_id,
                            "den": d,
                            "urok": urok,
                            "parnist": entry.get("parnist") or "oboydva",
                            "pidgrupa_2_vchytel_id": entry.get("pidgrupa_2_vchytel_id")
                                if entry.get("pidgrupa_2_vchytel_id") == entry.get("vchytel_id") else None,
                    })

        n_unplaced = solver.Value(unplaced[eid])
        if n_unplaced > 0:
            unplaced_report.append({
                "klas": klas_nazva(entry["paralel"], entry["litera"]),
                "predmet": entry["predmet_nazva"],
                "vchytel": entry["vchytel_pib"],
                "kilkist": n_unplaced,
                "parnist": entry.get("parnist") or "oboydva",
                "entry_id": eid,   # для діагностики нижче; у звіт не йде
            })

    # ---- позаурочні заняття (виховна, ГПД тощо) - пост-обробка ПІСЛЯ основного
    # розв'язку: НЕ рахуються в денний ліміт, ставляться одразу після останнього
    # навчального уроку дня для класу. Простий жадібний алгоритм (не CP-SAT) - їхня
    # позиція детермінована й не потребує оптимізації.
    #
    # Члени клас-комплекту з ОДНАКОВИМ заняттям і вчителем розміщуються РАЗОМ,
    # синхронно (як і звичайні предмети) - інакше кожен клас шукає собі місце
    # незалежно, і при спільному вчителі (класний керівник комплекту) вони буквально
    # конфліктують за той самий слот, і другий клас відсувається на урок пізніше
    # (саме так виникав розсинхрон, знайдений і виправлений). ----
    pozaurochni_unplaced = []

    zainyatyi_vchytel = {(r["vchytel_id"], r["den"], r["urok"]) for r in results}
    zainyatyi_kabinet = {(r["kabinet_id"], r["den"], r["urok"]) for r in results if r["kabinet_id"]}
    # уроки з інших рівнів теж рахуються в денний ліміт вчителя (слоти, не рядки)
    zainyatyi_vchytel_inshi = set(data["vchytel_sloty_inshyh_rivniv"])

    def _vchytel_den_povnyi(vid, den):
        """Чи вичерпано "Максимум уроків на день" вчителя цього дня (з урахуванням
        уроків цього прогону, інших рівнів і вже розставлених позаурочних)."""
        maks_den = data["vchyteli_maks_den"].get(vid)
        if not maks_den:
            return False
        sloty = {(v, d, l) for (v, d, l) in zainyatyi_vchytel | zainyatyi_vchytel_inshi if v == vid and d == den}
        return len(sloty) >= maks_den

    ostannii_urok_klasu_dnya = {}
    for r in results:
        key = (r["klas_id"], r["den"])
        ostannii_urok_klasu_dnya[key] = max(ostannii_urok_klasu_dnya.get(key, 0), r["urok"])

    PEREShKODY_POZAUR = {
        "vchytel_zainyatyi": "вчитель зайнятий після останнього уроку класу",
        "vchytel_nedostupnyi": "вчитель недоступний після останнього уроку класу",
        "vchytel_limit": "вчитель досяг свого ліміту {maks} уроків/день",
        "kabinet": "кабінет зайнятий після останнього уроку класу",
        "klas_nedostupnyi": "клас недоступний після останнього уроку",
        "nema_mistsya": "після останнього уроку класу не лишається слота в сітці дня",
        "zamorozheno": "день заморожено (догенерація)",
    }

    def _pozaur_pereshkoda(vid, kab_id, klas_ids, den, l):
        """Чому позаурочне не можна поставити у (den, l); None - можна. Солвер
        для навчальних усе це враховує сам, жадібний крок мусить перевіряти
        явно: зайнятість і недоступність вчителя, його денний ліміт, кабінет,
        недоступність класу."""
        if (vid, den, l) in zainyatyi_vchytel:
            return "vchytel_zainyatyi"
        if (vid, den, l) in data["teacher_unavail"]:
            return "vchytel_nedostupnyi"
        if _vchytel_den_povnyi(vid, den):
            return "vchytel_limit"
        if kab_id and (kab_id, den, l) in zainyatyi_kabinet:
            return "kabinet"
        if any((kid, den, l) in data["class_unavail"] for kid in klas_ids):
            return "klas_nedostupnyi"
        return None

    def _prychyna_pozaur(vid, kab_id, klas_ids, dni, maks_sloti):
        """Текст "ймовірної причини" для нерозміщеного позаурочного: по кожному
        дозволеному дню - що саме заважає в усіх слотах після останнього уроку."""
        pereshkody = {}
        for den in dni:
            if any((kid, den) in (zamorozheni_klityny or set()) for kid in klas_ids):
                pereshkody.setdefault("zamorozheno", set()).add(den)
                continue
            pochatok = max(ostannii_urok_klasu_dnya.get((kid, den), 0) for kid in klas_ids) + 1
            if pochatok > maks_sloti:
                pereshkody.setdefault("nema_mistsya", set()).add(den)
                continue
            for l in range(pochatok, maks_sloti + 1):
                p = _pozaur_pereshkoda(vid, kab_id, klas_ids, den, l)
                if p is None:
                    return ""
                pereshkody.setdefault(p, set()).add(den)
        maks = data["vchyteli_maks_den"].get(vid)
        chastyny = []
        for p, dni_p in pereshkody.items():
            dni_txt = ", ".join(DEN_KOROTKO[d - 1] for d in sorted(dni_p))
            chastyny.append(PEREShKODY_POZAUR[p].format(maks=maks) + f" ({dni_txt})")
        return "; ".join(chastyny)

    # групуємо за (komplekt_id, zanyattya_id, vchytel_id) - кандидати на спільне розміщення
    pozaur_kandydaty = {}
    pozaur_inshi = []
    for pe in data.get("pozaurochni_entries", []):
        kid = pe.get("komplekt_id")
        if kid is None:
            pozaur_inshi.append(pe)
            continue
        klyuch = (kid, pe["zanyattya_id"], pe["vchytel_id"])
        pozaur_kandydaty.setdefault(klyuch, []).append(pe)

    pozaur_grupy = []  # (grupa, min_godyn)
    for klyuch, grupa in pozaur_kandydaty.items():
        if len(grupa) < 2:
            pozaur_inshi.extend(grupa)
            continue
        min_godyn = min(pe["godyn_tyzhden"] for pe in grupa)
        pozaur_grupy.append((grupa, min_godyn))
        for pe in grupa:
            zalyshok = pe["godyn_tyzhden"] - min_godyn
            if zalyshok > 0:
                okremyi = dict(pe)
                okremyi["godyn_tyzhden"] = zalyshok
                pozaur_inshi.append(okremyi)

    # 1. спершу розміщуємо СПІЛЬНІ (групові) записи - той самий день/урок усім членам
    for grupa, min_godyn in pozaur_grupy:
        bazovyi = grupa[0]
        dni_dlya_grupy = _dni_pozaurochnogo(bazovyi)
        chleny_ids = [pe["klas_id"] for pe in grupa]
        maks_sloti = data["urokiv"]   # у межах сітки "уроків на день" школи
        rozmishcheno = 0

        while rozmishcheno < min_godyn:
            rozmishcheno_na_pochatku_kola = rozmishcheno
            for den in dni_dlya_grupy:
                if rozmishcheno >= min_godyn:
                    break
                if any((kid, den) in (zamorozheni_klityny or set()) for kid in chleny_ids):
                    continue
                # стартова позиція - НАЙПІЗНІША серед усіх членів (потрібне вільне
                # місце одночасно для ВСІХ, не лише для одного)
                pochatok = max(ostannii_urok_klasu_dnya.get((kid, den), 0) for kid in chleny_ids) + 1
                for l in range(pochatok, maks_sloti + 1):
                    if _pozaur_pereshkoda(bazovyi["vchytel_id"], bazovyi["domashniy_kabinet_id"],
                                          chleny_ids, den, l) is None:
                        for kid in chleny_ids:
                            results.append({
                                "klas_id": kid,
                                "predmet_id": None,
                                "pozaurochne_zanyattya_id": bazovyi["zanyattya_id"],
                                "vchytel_id": bazovyi["vchytel_id"],
                                "kabinet_id": bazovyi["domashniy_kabinet_id"],
                                "den": den,
                                "urok": l,
                            })
                            ostannii_urok_klasu_dnya[(kid, den)] = max(
                                ostannii_urok_klasu_dnya.get((kid, den), 0), l
                            )
                        zainyatyi_vchytel.add((bazovyi["vchytel_id"], den, l))
                        if bazovyi["domashniy_kabinet_id"]:
                            zainyatyi_kabinet.add((bazovyi["domashniy_kabinet_id"], den, l))
                        rozmishcheno += 1
                        break  # лише 1 година на цей день у цьому колі
            if rozmishcheno == rozmishcheno_na_pochatku_kola:
                break  # жодного прогресу цього кола - далі марно

        if rozmishcheno < min_godyn:
            prychyna = _prychyna_pozaur(bazovyi["vchytel_id"], bazovyi["domashniy_kabinet_id"],
                                        chleny_ids, dni_dlya_grupy, maks_sloti)
            for pe in grupa:
                pozaurochni_unplaced.append({
                    "klas": klas_nazva(pe["paralel"], pe["litera"]),
                    "predmet": pe["zanyattya_nazva"] + " (позаурочне, спільно з комплектом)",
                    "vchytel": pe["vchytel_pib"],
                    "kilkist": min_godyn - rozmishcheno,
                    "prychyna": prychyna,
                })

    # 2. потім розміщуємо решту (окремі класи, залишкові години) - незалежно, як і раніше
    for pe in pozaur_inshi:
        dni_dlya_pe = _dni_pozaurochnogo(pe)  # без дня - типово пн
        klas_id = pe["klas_id"]
        maks_sloti = data["urokiv"]   # у межах сітки "уроків на день" школи
        potribno = pe["godyn_tyzhden"]
        rozmishcheno = 0

        # розподіляємо по одній годині на кожен вказаний день; якщо годин більше,
        # ніж днів у списку - повторюємо коло(а) по тих самих днях, доки не
        # розмістимо все або доки коло не дасть жодного прогресу (запобіжник від
        # нескінченного циклу, якщо дні вже повністю зайняті)
        while rozmishcheno < potribno:
            rozmishcheno_na_pochatku_kola = rozmishcheno
            for den in dni_dlya_pe:
                if rozmishcheno >= potribno:
                    break
                if (klas_id, den) in (zamorozheni_klityny or set()):
                    continue
                pochatok = ostannii_urok_klasu_dnya.get((klas_id, den), 0) + 1
                for l in range(pochatok, maks_sloti + 1):
                    if _pozaur_pereshkoda(pe["vchytel_id"], pe["domashniy_kabinet_id"],
                                          [klas_id], den, l) is None:
                        results.append({
                            "klas_id": klas_id,
                            "predmet_id": None,
                            "pozaurochne_zanyattya_id": pe["zanyattya_id"],
                            "vchytel_id": pe["vchytel_id"],
                            "kabinet_id": pe["domashniy_kabinet_id"],
                            "den": den,
                            "urok": l,
                        })
                        zainyatyi_vchytel.add((pe["vchytel_id"], den, l))
                        if pe["domashniy_kabinet_id"]:
                            zainyatyi_kabinet.add((pe["domashniy_kabinet_id"], den, l))
                        ostannii_urok_klasu_dnya[(klas_id, den)] = max(
                            ostannii_urok_klasu_dnya.get((klas_id, den), 0), l
                        )
                        rozmishcheno += 1
                        break  # лише 1 година на цей день у цьому колі
            if rozmishcheno == rozmishcheno_na_pochatku_kola:
                break  # жодного прогресу цього кола - далі марно, виходимо

        if rozmishcheno < potribno:
            pozaurochni_unplaced.append({
                "klas": klas_nazva(pe["paralel"], pe["litera"]),
                "predmet": pe["zanyattya_nazva"] + " (позаурочне)",
                "vchytel": pe["vchytel_pib"],
                "kilkist": potribno - rozmishcheno,
                "prychyna": _prychyna_pozaur(pe["vchytel_id"], pe["domashniy_kabinet_id"],
                                             [klas_id], dni_dlya_pe, maks_sloti),
            })

    unplaced_report.extend(pozaurochni_unplaced)

    # ---- ймовірна причина для кожного нерозміщеного НАВЧАЛЬНОГО уроку (позаурочні
    # свою причину дістали вище, у _prychyna_pozaur). Солвер сам не
    # каже, "чому", але прості речі порахувати можна з готового розкладу: чи є в
    # тижні слот, де ОДНОЧАСНО вільні клас (у межах свого денного ліміту), вчитель
    # (не зайнятий і не "недоступний") і, якщо треба, спецкабінет. Це не доказ, а
    # підказка завучу, де шукати. ----
    zainyati_vch_sloty = {(r["vchytel_id"], r["den"], r["urok"]) for r in results}
    zainyati_kab_sloty = {(r["kabinet_id"], r["den"], r["urok"]) for r in results if r["kabinet_id"]}
    # позаурочні тут не рахуємо: солвер їх не бачив, коли ставив навчальні, вони
    # стали ПІСЛЯ уроків і могли б посунутись; інакше клас із 4 уроками + виховною
    # годиною хибно виглядав би "заповненим до ліміту 5"
    klas_uroky_dnya = {}
    for r in results:
        if r["predmet_id"] is not None:
            klas_uroky_dnya.setdefault((r["klas_id"], r["den"]), set()).add(r["urok"])
    for u, entry in zip([x for x in unplaced_report if "entry_id" in x],
                        [entries_by_id[x["entry_id"]] for x in unplaced_report if "entry_id" in x]):
        prychyny = []
        vid = entry["vchytel_id"]
        klas_ids = entry_klas_ids(entry)
        # ліміт класу для ЦЬОГО предмета (звичайний / понад-лімітний, як у _slot_ok)
        if entry["vrahovuyetsya_u_hranychnomu"]:
            lim = entry["klas_maks_urokiv_den"] or default_maks_urokiv_den(entry["paralel"])
        else:
            lim = entry["klas_maks_urokiv_den_z_fizkulturoyu"] or default_maks_urokiv_den_z_fizkulturoyu(entry["paralel"])
        lim = min(lim, data["urokiv"])
        vilni_vch = [(d, l) for d in days for l in lessons
                     if (vid, d, l) not in zainyati_vch_sloty and (vid, d, l) not in data["teacher_unavail"]]
        dni_klasu_povni = [d for d in days
                           if all(len(klas_uroky_dnya.get((kid, d), set())) >= lim for kid in klas_ids)]
        vilni_klas = [(d, l) for d in days for l in lessons
                      if d not in dni_klasu_povni and l <= lim
                      and all(l not in klas_uroky_dnya.get((kid, d), set()) and (kid, d, l) not in data["class_unavail"]
                              for kid in klas_ids)]
        spilni = sorted(set(vilni_vch) & set(vilni_klas))
        # "Максимум уроків на день" вчителя: дні, де він уже впритул до ліміту,
        # для нього закриті, навіть якщо слот формально вільний
        maks_vch = data["vchyteli_maks_den"].get(vid)
        if maks_vch and spilni:
            uroky_vch_dnya = {}
            for (v, d, l) in zainyati_vch_sloty:
                if v == vid:
                    uroky_vch_dnya[d] = uroky_vch_dnya.get(d, 0) + 1
            spilni_z_limitom = [(d, l) for (d, l) in spilni if uroky_vch_dnya.get(d, 0) < maks_vch]
            if not spilni_z_limitom:
                prychyny.append(f"вчитель досяг свого ліміту {maks_vch} уроків/день у всі дні, де є вільні спільні уроки")
            spilni = spilni_z_limitom
        if not vilni_vch:
            prychyny.append("вчитель зайнятий або недоступний у всі уроки тижня")
        if len(dni_klasu_povni) == len(days):
            prychyny.append(f"клас заповнений до денного ліміту ({lim}) у всі дні")
        if vilni_vch and vilni_klas and not spilni:
            prychyny.append("немає жодного уроку тижня, де вільні одночасно і клас, і вчитель")
        if spilni:
            # кабінет: чи є вільний спецкабінет у спільних слотах (лише для предмета зі спецкабінетом)
            spets = [kid for kid, kab in kabinety_by_id.items()
                     if kab["typ_pryvyazky"] == "predmet" and entry["predmet_id"] in data["kabinet_dozvoleni_predmety"].get(kid, set())]
            if spets and not entry.get("spets_kabinet_lyshe_pidgrupa1"):
                ye_kab = any((k, d, l) not in zainyati_kab_sloty and (k, d, l) not in data["kabinet_unavail"]
                             for (d, l) in spilni for k in spets)
                if not ye_kab:
                    prychyny.append("спецкабінети предмета зайняті в усі спільні вільні уроки")
            if not prychyny:
                dni_txt = ", ".join(f"{DEN_KOROTKO[d - 1]}{l}" for d, l in spilni[:4])
                prychyny.append(
                    f"вільні спільні уроки є ({dni_txt}{'…' if len(spilni) > 4 else ''}) - "
                    "заважають правила предмета (макс. на день, важкість/номер уроку, "
                    "проміжок між уроками, здвоєна пара), ліміт вчителя на день або "
                    "позаурочне, що стало одразу після уроків"
                )
        u["prychyna"] = "; ".join(prychyny)
    for u in unplaced_report:
        u.pop("entry_id", None)
        u.setdefault("prychyna", "")

    # ---- зберігаємо в БД (активний розклад). Якщо генеруємо ОКРЕМО по рівню - очищаємо
    # ЛИШЕ класи цього діапазону, не чіпаючи вже згенерований розклад інших рівнів ----
    if paralel_vid is not None and paralel_do is not None:
        conn.execute(
            "DELETE FROM generated_urok WHERE klas_id IN "
            "(SELECT id FROM klasy WHERE paralel BETWEEN ? AND ?)",
            (paralel_vid, paralel_do),
        )
    else:
        conn.execute("DELETE FROM generated_urok")
    for r in results:
        conn.execute(
            """
            INSERT INTO generated_urok
                (klas_id, predmet_id, pozaurochne_zanyattya_id, vchytel_id, kabinet_id, den, urok, parnist, pidgrupa_2_vchytel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r["klas_id"], r["predmet_id"], r.get("pozaurochne_zanyattya_id"),
             r["vchytel_id"], r["kabinet_id"], r["den"], r["urok"], r.get("parnist") or "oboydva",
             r.get("pidgrupa_2_vchytel_id")),
        )
    conn.commit()

    excel_path = _export_excel(conn, results, unplaced_report, data["dniv"], data["urokiv"])
    conn.close()

    kilkist_klasiv = len({r["klas_id"] for r in results})
    kilkist_vchyteliv = len({r["vchytel_id"] for r in results})
    # позаурочні заняття лежать у results з predmet_id=None - без фільтра
    # None потрапляв у множину як окремий "предмет" і завищував число на 1
    kilkist_predmetiv = len({r["predmet_id"] for r in results if r["predmet_id"]})

    return {
        "success": True,
        "status": "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        "vseogo_urokiv": len(results),
        "unplaced": unplaced_report,
        "excel_path": excel_path,
        "kilkist_klasiv": kilkist_klasiv,
        "kilkist_vchyteliv": kilkist_vchyteliv,
        "kilkist_predmetiv": kilkist_predmetiv,
        "chas_sekund": round(time.time() - _chas_startu, 1),
        "semestr": semestr,
    }


def _export_excel(conn, results, unplaced_report, dniv, urokiv):
    DAYS_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"][:dniv]

    klasy = {r["id"]: r for r in conn.execute("SELECT * FROM klasy").fetchall()}
    predmety = {r["id"]: r for r in conn.execute("SELECT * FROM predmety").fetchall()}
    pozaurochni = {r["id"]: r for r in conn.execute("SELECT * FROM pozaurochni_zanyattya").fetchall()}
    vchyteli = {r["id"]: r for r in conn.execute("SELECT * FROM vchyteli").fetchall()}
    kabinety = {r["id"]: r for r in conn.execute("SELECT * FROM kabinety").fetchall()}

    def _nazva_zapysu(u):
        """Назва предмета АБО позаурочного заняття - результат може бути будь-яким із двох."""
        if u["predmet_id"] is not None:
            p_row = predmety.get(u["predmet_id"])
            return (p_row["skorochennya"] or p_row["nazva"]) if p_row else "?"
        pz_row = pozaurochni.get(u["pozaurochne_zanyattya_id"])
        return pz_row["nazva"] if pz_row else "?"

    # асистенти по класах - для показу в клітинках Excel поруч із предметом і вчителем
    asystenty_po_klasu = {}
    for a in conn.execute("SELECT id, pib, klas_id, suprovid FROM asystenty").fetchall():
        predmety_ids = set()
        if a["suprovid"] == "okremi":
            predmety_ids = {
                r["predmet_id"] for r in conn.execute(
                    "SELECT predmet_id FROM asystenty_predmety WHERE asystent_id = ?", (a["id"],)
                ).fetchall()
            }
        asystenty_po_klasu.setdefault(a["klas_id"], []).append(
            {"pib": a["pib"], "suprovid": a["suprovid"], "predmety_ids": predmety_ids}
        )

    def _asystenty_dlya(klas_id, predmet_id):
        imena = []
        for a in asystenty_po_klasu.get(klas_id, []):
            if a["suprovid"] == "vsi" or predmet_id in a["predmety_ids"]:
                imena.append(skorochene_pib(a["pib"]))
        return imena

    wb = Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill(start_color="1ABC9C", end_color="1ABC9C", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # ---- листи по класах ----
    by_klas = {}
    for r in results:
        by_klas.setdefault(r["klas_id"], []).append(r)

    for klas_id, uroky in sorted(by_klas.items(), key=lambda kv: (klasy[kv[0]]["paralel"], ukr_klyuch(klasy[kv[0]]["litera"]))):
        k = klasy[klas_id]
        title = f"Клас {klas_nazva(k['paralel'], k['litera'])}"
        ws = wb.create_sheet(title[:31])
        ws.cell(row=1, column=1, value="Урок")
        for col, day_label in enumerate(DAYS_LABELS, start=2):
            c = ws.cell(row=1, column=col, value=day_label)
            c.fill = header_fill
            c.font = header_font
            c.alignment = center
        for row, l in enumerate(range(1, urokiv + 1), start=2):
            ws.cell(row=row, column=1, value=l).font = Font(bold=True)

        cell_map_multi = {}
        for u in uroky:
            cell_map_multi.setdefault((u["den"], u["urok"]), []).append(u)
        for l in range(1, urokiv + 1):
            for d in range(1, dniv + 1):
                zapysy = cell_map_multi.get((d, l), [])
                if zapysy:
                    blocks = []
                    for u in zapysy:
                        predmet_nazva = _nazva_zapysu(u)
                        parnist = u.get("parnist") or "oboydva"
                        if parnist != "oboydva":
                            predmet_nazva += f" ({'парн.' if parnist == 'parnyi' else 'непарн.'})"
                        vchytel_pib = skorochene_pib(vchyteli[u["vchytel_id"]]["pib"])
                        kabinet_nazva = kabinety[u["kabinet_id"]]["nazva"] if u["kabinet_id"] and u["kabinet_id"] in kabinety else ""
                        block = f"{predmet_nazva}\n{vchytel_pib}"
                        asystenty_imena = _asystenty_dlya(klas_id, u["predmet_id"]) if u["predmet_id"] else []
                        if asystenty_imena:
                            block += f"\n+ асист. {', '.join(asystenty_imena)}"
                        if kabinet_nazva:
                            block += f"\n{kabinet_nazva}"
                        blocks.append(block)
                    text = "\n---\n".join(blocks)
                    cell = ws.cell(row=l + 1, column=d + 1, value=text)
                    cell.alignment = center
        for col in range(1, dniv + 2):
            ws.column_dimensions[chr(64 + col)].width = 20
        for row in range(2, urokiv + 2):
            ws.row_dimensions[row].height = 45

    # ---- листи по вчителях ----
    by_vchytel = {}
    for r in results:
        by_vchytel.setdefault(r["vchytel_id"], []).append(r)

    for vchytel_id, uroky in sorted(by_vchytel.items(), key=lambda kv: ukr_klyuch(vchyteli[kv[0]]["pib"])):
        v = vchyteli[vchytel_id]
        title = v["pib"][:31]
        ws = wb.create_sheet(title)
        ws.cell(row=1, column=1, value="Урок")
        for col, day_label in enumerate(DAYS_LABELS, start=2):
            c = ws.cell(row=1, column=col, value=day_label)
            c.fill = header_fill
            c.font = header_font
            c.alignment = center
        for row, l in enumerate(range(1, urokiv + 1), start=2):
            ws.cell(row=row, column=1, value=l).font = Font(bold=True)

        cell_map = {}
        for u in uroky:
            cell_map.setdefault((u["den"], u["urok"]), []).append(u)
        for l in range(1, urokiv + 1):
            for d in range(1, dniv + 1):
                zapysy = cell_map.get((d, l), [])
                if zapysy:
                    blocks = []
                    for u in zapysy:
                        k = klasy[u["klas_id"]]
                        predmet_nazva = _nazva_zapysu(u)
                        parnist = u.get("parnist") or "oboydva"
                        if parnist != "oboydva":
                            predmet_nazva += f" ({'парн.' if parnist == 'parnyi' else 'непарн.'})"
                        kabinet_nazva = kabinety[u["kabinet_id"]]["nazva"] if u["kabinet_id"] and u["kabinet_id"] in kabinety else ""
                        block = f"{klas_nazva(k['paralel'], k['litera'])}\n{predmet_nazva}"
                        if kabinet_nazva:
                            block += f"\n{kabinet_nazva}"
                        blocks.append(block)
                    text = "\n---\n".join(blocks)
                    cell = ws.cell(row=l + 1, column=d + 1, value=text)
                    cell.alignment = center
        for col in range(1, dniv + 2):
            ws.column_dimensions[chr(64 + col)].width = 20
        for row in range(2, urokiv + 2):
            ws.row_dimensions[row].height = 45

    # ---- лист конфліктів ----
    _parnist_nazvy = {"oboydva": "щотижня", "parnyi": "парний тижд.", "neparnyi": "непарний тижд."}
    ws = wb.create_sheet("Конфлікти")
    ws.append(["Клас", "Предмет", "Вчитель", "Тиждень", "Не розміщено уроків", "Ймовірна причина"])
    for c in ws[1]:
        c.fill = header_fill
        c.font = header_font
    if unplaced_report:
        for u in unplaced_report:
            # позаурочні unplaced-записи не несуть "parnist" - .get(), не [ ]
            ws.append([u["klas"], u["predmet"], u["vchytel"],
                       _parnist_nazvy.get(u.get("parnist"), "—"), u["kilkist"], u.get("prychyna", "")])
    else:
        ws.append(["—", "—", "—", "—", "Усі уроки розміщено успішно", ""])
    for col in "ABCDE":
        ws.column_dimensions[col].width = 25
    ws.column_dimensions["F"].width = 70

    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"rozklad_{timestamp}.xlsx"
    filepath = os.path.join(SCHEDULE_DIR, filename)
    wb.save(filepath)
    return filepath
