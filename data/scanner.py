import threading
import time
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed


class MarketScanner(threading.Thread):

    def __init__(
            self,
            api,
            database,
            parser,
            callback,
            settings
    ):
        super().__init__(daemon=True)

        self.api = api
        self.database = database
        self.parser = parser
        self.callback = callback
        self.settings = settings

        self.executor = ThreadPoolExecutor(max_workers=10)

        self.description_cache = {}
        self.items = {}

        self.running = False
        self.stop_event = threading.Event()

        self.interval = max(
            0.1,
            settings.get("scan_interval", 200) / 1000
        )

        self.items_lock = threading.Lock()

    # =========================
    # запуск непрерывного сканирования
    # =========================

    def run(self):
        self.running = True
        self.stop_event.clear()

        print("Сканер запущен.")

        try:
            while self.running and not self.stop_event.is_set():

                cycle_start = time.time()

                try:
                    self.scan()
                except Exception as e:
                    print("Ошибка сканирования:", e)

                elapsed = time.time() - cycle_start
                wait_time = max(0, self.interval - elapsed)

                if self.stop_event.wait(wait_time):
                    break

        finally:
            self.running = False
            print("Сканер остановлен.")

    # =========================
    # остановка
    # =========================

    def stop(self):
        print("Остановка сканера...")

        self.running = False
        self.stop_event.set()

    # =========================
    # проверка состояния
    # =========================

    def is_active(self):
        return self.running and not self.stop_event.is_set()

    # =========================
    # основной проход по списку
    # =========================

    def scan(self):

        if not self.is_active():
            return

        targets = self.database.get_all_items()

        targets = [
            target for target in targets
            if target.get("is_active", 1)
               and target.get(
                "remaining",
                target.get("quantity", 1)
            ) > 0
        ]

        if not targets:
            print("Нет активных предметов для сканирования.")
            return

        print("Начинаю проход. Позиций:", len(targets))

        futures = {}

        for target in targets:

            if not self.is_active():
                break

            future = self.executor.submit(
                self.scan_one_item,
                target
            )

            futures[future] = target

        # Обрабатываем результаты всех заданий
        for future in as_completed(futures):

            if not self.is_active():
                break

            target = futures[future]

            try:
                result = future.result()

                if not result:
                    continue

                for item_id, item in result.items():

                    with self.items_lock:
                        old = self.items.get(item_id)

                        if (
                                old is None
                                or item["price"] < old["price"]
                        ):
                            self.items[item_id] = item

            except Exception as e:
                print(
                    "Ошибка обработки:",
                    target.get("name"),
                    e
                )

        print("Проход по списку завершён.")

    # =========================
    # поиск одного предмета
    # =========================

    def scan_one_item(self, target):

        found = {}

        try:

            if not self.is_active():
                return found

            name = target["name"]

            print("Проверяю:", name)

            wanted_gems = [
                gem.strip().lower()
                for gem in (target.get("gem") or "").split(",")
                if gem.strip()
            ]

            result = self.api.search_item(name)

            if not self.is_active():
                return found

            offers = self.parse_search_result(result)

            print("Всего лотов:", len(offers), "|", name)

            for offer in offers:

                if not self.is_active():
                    break

                price = offer["price"]

                # Проверяем максимальную цену
                if price > float(target["max_price"]):
                    continue

                gem = ""

                # Проверка гемов нужна только при заполненном поле
                if wanted_gems:

                    desc_id = (
                        offer["classid"],
                        offer["instanceid"]
                    )

                    if desc_id in self.description_cache:
                        description = self.description_cache[desc_id]

                    else:
                        description = self.api.get_description(
                            offer["classid"],
                            offer["instanceid"]
                        )

                        self.description_cache[desc_id] = description

                    if not description:
                        continue

                    description_text = description.get("description", "")

                    if not description_text:
                        continue

                    gems = self.parser.extract_gems(
                        description_text
                    )

                    found_gem = next(
                        (
                            item_gem
                            for item_gem in gems
                            if item_gem.strip().lower() in wanted_gems
                        ),
                        None
                    )

                    if not found_gem:
                        continue

                    gem = found_gem

                offer["name"] = name
                offer["gem"] = gem
                offer["target_id"] = target["id"]
                offer["remaining"] = target.get("remaining", 1)

                offer["link"] = (
                    "https://market.dota2.net/item/"
                    f"{offer['classid']}-"
                    f"{offer['instanceid']}-"
                    f"{quote_plus(name)}"
                )

                item_id = (
                    f"{offer['classid']}_"
                    f"{offer['instanceid']}"
                )

                found[item_id] = offer

                print(
                    "НАЙДЕН ПРЕДМЕТ:",
                    name,
                    "| Гем:",
                    gem or "не проверялся",
                    "| Цена:",
                    price
                )

                is_new = False

                with self.items_lock:

                    if item_id not in self.items:
                        self.items[item_id] = offer
                        is_new = True

                if is_new and self.is_active():
                    self.callback({item_id: offer})

        except Exception as e:

            print(
                "Ошибка проверки предмета:",
                target.get("name"),
                e
            )

        return found

    # =========================
    # обработка ответа API
    # =========================

    def parse_search_result(self, result):

        items = []

        if not result:
            return items

        if not result.get("success"):
            return items

        offers = result.get("list", [])

        for offer in offers:

            try:

                items.append({

                    "id": (
                        f"{offer.get('i_classid')}_"
                        f"{offer.get('i_instanceid')}"
                    ),

                    "price": (
                            float(offer.get("price", 0)) / 100
                    ),

                    "classid": offer.get("i_classid"),

                    "instanceid": offer.get("i_instanceid")

                })

            except (TypeError, ValueError) as e:

                print("Пропущен некорректный лот:", e)

        return items
