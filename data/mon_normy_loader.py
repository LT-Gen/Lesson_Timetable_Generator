"""Єдиний завантажувач нормативних довідників МОН з data/json/mon_normy.json.

Незалежний модуль без залежностей від app.py чи generator.py - безпечний для
імпорту з обох (уникає циклічного імпорту, який був причиною дублювання функцій
default_hranychne_navantazhennya/vikovi_pravyla_vazhkosti окремо в кожному файлі).

Завантажується ОДИН РАЗ (кешується в модульній змінній), не при кожному виклику.
"""

import json
import os

_SHLYAH_DO_JSON = os.path.join(os.path.dirname(__file__), "json", "mon_normy.json")
_kesh = None


def _zavantazhyty():
    global _kesh
    if _kesh is None:
        with open(_SHLYAH_DO_JSON, encoding="utf-8") as f:
            _kesh = json.load(f)
    return _kesh


def pochatkovi_klasni_predmety():
    """Множина предметів, які традиційно веде один класний керівник початкової
    школи (використовується при формуванні клас-комплектів - автопризначення
    вчителя на його ж предмети)."""
    return set(_zavantazhyty()["pochatkovi_klasni_predmety"]["predmety"])


def default_hranychne_navantazhennya(paralel):
    """Гранично допустиме тижневе навантаження (без фізкультури) - орієнтир,
    якщо не задано вручну в картці класу."""
    dovidnyk = _zavantazhyty()["hranychne_navantazhennya"]
    return dovidnyk.get(str(paralel), 33)


def default_faktychne_navantazhennya(paralel):
    """Фактичне тижневе навантаження (з фізкультурою) - орієнтир, якщо не
    задано вручну в картці класу."""
    dovidnyk = _zavantazhyty()["faktychne_navantazhennya"]
    return dovidnyk.get(str(paralel), 38)


def default_maks_urokiv_den(paralel):
    """Максимум навчальних уроків на день БЕЗ фізкультури."""
    dovidnyk = _zavantazhyty()["maks_urokiv_den"]
    return dovidnyk.get(str(paralel), 8)


def default_maks_urokiv_den_z_fizkulturoyu(paralel):
    """Максимум уроків на день З фізкультурою (на 1 більше за базовий ліміт)."""
    dovidnyk = _zavantazhyty()["maks_urokiv_den_z_fizkulturoyu"]
    return dovidnyk.get(str(paralel), 9)


def vikovi_pravyla_vazhkosti(paralel):
    """Вікові правила розміщення важких предметів (C1) за паралеллю.
    Повертає словник з порогами й перемикачами жорсткий/м'який для вікової групи."""
    dovidnyk = _zavantazhyty()["vikovi_pravyla_vazhkosti"]
    if paralel <= 4:
        return dovidnyk["4"]
    if paralel <= 9:
        return dovidnyk["9"]
    return dovidnyk["12"]


def pary_dozvoleni_5_6():
    """Множина назв предметів, для яких дозволені здвоєні уроки в 5-6 класах."""
    return set(_zavantazhyty()["pary_dozvoleni_5_6"]["predmety"])


def pary_dozvoleni_7_9():
    """Множина назв предметів, для яких дозволені здвоєні уроки в 7-9 класах."""
    return set(_zavantazhyty()["pary_dozvoleni_7_9"]["predmety"])


def normovani_kody_predmetiv():
    """Словник {назва предмета з типового переліку -> стабільний код}. Використовується
    для випадаючого списку при створенні предмета - не автогенерація транслітерацією
    (ризик колізій), а вибір із наперед визначеного скінченного переліку."""
    dovidnyk = dict(_zavantazhyty()["normovani_kody_predmetiv"])
    dovidnyk.pop("_opys", None)
    return dovidnyk


def typovyi_perelik_predmetiv():
    """Типовий перелік предметів МОН - для 'Наповнити типовим переліком'."""
    return list(_zavantazhyty()["typovyi_perelik_predmetiv"])


def vazhkist_mapa():
    """Словник {назва предмета -> категорія C1/C2/C3 (vazhkyi/serednii/legkyi)}."""
    return dict(_zavantazhyty()["vazhkist_mapa"])


def predmet_skorochennya_mapa():
    """Словник {назва предмета -> типове скорочення для компактного розкладу}."""
    return dict(_zavantazhyty()["predmet_skorochennya_mapa"])


def normy_podilu_na_pidgrupy():
    """Норми обов'язкового поділу класу на підгрупи (наказ МОН №128, у редакції №808) -
    словник {normovanyi_kod предмета -> норма}. Кожна норма: typ 'kilkist' (поріг учнів,
    різний для міста/села, за діапазоном паралелей; za_stattyu=true - після перевищення
    порогу поділ саме за статтю; для української мови є окремий, нижчий поріг
    movy_menshyn_porih_* для класів з мовою навчання нацменшини/корінного народу) або
    'grupy_bez_porogu' (завжди 2 групи мінімум по 8 осіб, щойно учнів досить - без
    окремого числового порогу: Захист України, інформатика/робототехніка/STEM,
    мова корінного народу як предмет)."""
    dovidnyk = dict(_zavantazhyty()["normy_podilu_na_pidgrupy"])
    dovidnyk.pop("_opys", None)
    return dovidnyk


def movy_navchannya():
    """Перелік мов навчання (наказ МОН №128, у редакції №808) - мови нацменшин
    і корінних народів України, які впливають на пороги поділу класів на підгрупи."""
    return list(_zavantazhyty()["movy_navchannya"])


def osvitni_galuzi():
    """Перелік освітніх галузей за Державним стандартом (постанова КМУ №898)."""
    return list(_zavantazhyty()["osvitni_galuzi"])


def osvitnya_galuz_mapa():
    """Словник {назва предмета -> освітня галузь}."""
    return dict(_zavantazhyty()["osvitnya_galuz_mapa"])


def tip_zakladu_options():
    """Словник типів закладу (початкова/гімназія/ліцей/стара школа) з діапазонами
    паралелей для кожного рівня. Кортежі rivni зберігаються як списки в JSON -
    перетворюємо назад на tuple для сумісності з рештою коду."""
    syrivets = _zavantazhyty()["tip_zakladu_options"]
    result = {}
    for kod, opys in syrivets.items():
        result[kod] = {
            "nazva": opys["nazva"],
            "rivni": [tuple(r) for r in opys["rivni"]],
        }
    return result


def diapazon_paraleley_zakladu(tip_zakladu):
    """Повертає (min_paralel, max_paralel) для повного діапазону закладу."""
    options = tip_zakladu_options()
    # запасний варіант для невідомого коду - "1-11 стара школа" (найширший
    # ПІДТРИМУВАНИЙ діапазон; ліцей 10-12 у генерації поки не задіяний)
    rivni = options.get(tip_zakladu, options["stara_shkola"])["rivni"]
    return min(r[1] for r in rivni), max(r[2] for r in rivni)


def typovi_navchalni_plany():
    """Довідник офіційних типових навчальних планів МОН - лише для перегляду
    (не редагуються через UI, не зберігаються в БД). Ключ - код плану
    (напр. '1_2_savchenko'), значення - nazva/sektsiyi/prymitky."""
    dovidnyk = dict(_zavantazhyty()["typovi_navchalni_plany"])
    dovidnyk.pop("_opys", None)
    return dovidnyk
