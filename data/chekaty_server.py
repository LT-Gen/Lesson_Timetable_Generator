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

"""Чекає, поки веб-сервер застосунку справді почне приймати з'єднання.

run.bat раніше просто чекав фіксовані 3 секунди й відкривав браузер. На
повільному комп'ютері школи (перший запуск, антивірус, створення бази) сервер
за 3 секунди піднятись не встигає - і завуч бачить помилку браузера замість
застосунку. Тут ми стукаємо в порт, доки він не відповість.

Вихід: 0 - сервер відповів, 1 - не дочекались за відведений час.
"""

import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 5000
MAKS_SEKUND = 60          # перший запуск створює базу - буває довго
KROK = 0.25


def main():
    krainiy = time.time() + MAKS_SEKUND
    while time.time() < krainiy:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex((HOST, PORT)) == 0:
                return 0
        time.sleep(KROK)
    return 1


if __name__ == "__main__":
    sys.exit(main())
