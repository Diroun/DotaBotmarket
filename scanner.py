
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote


class MarketScanner(threading.Thread):

    def __init__(self, api, database, parser, callback, settings):
        super().__init__(daemon=True)

        self.api = api
        self.database = database
        self.parser = parser
        self.callback = callback
        self.settings = settings or {}

        # Независимые пулы поиска и покупки.
        self.scan_executor = ThreadPoolExecutor(max_workers=15)
        self.buy_executor = ThreadPoolExecutor(max_workers=15)

        # Только один поток проверяет ожидающие заявки.
        self.pending_executor = ThreadPoolExecutor(max_workers=1)

        # Не более 15 одновременно выполняемых покупок.
        self.max_parallel_buys = 15
        self.buy_slots = threading.BoundedSemaphore(
            self.max_parallel_buys
        )

        self.stop_event = threading.Event()
        self.running = False

        # Результаты для интерфейса.
        self.items = {}
        self.items_lock = threading.Lock()

        # Активные поисковые задачи.
        self.scan_futures = {}
        self.scan_targets_lock = threading.Lock()

        # Кэш описаний предметов.
        self.description_cache = {}
        self.description_lock = threading.Lock()

        # Автопокупка.
        self.auto_buy_lock = threading.Lock()
        self.auto_buy_enabled = bool(
            self.settings.get("auto_buy", False)
        )

        # Защита от повторной покупки одного лота.
        self.buy_inflight = set()
        self.buy_inflight_lock = threading.Lock()

        # Проверка ожидающих заявок.
        self.pending_future = None
        self.pending_lock = threading.Lock()

        # Не проверяем одну заявку слишком часто.
        self.pending_check_interval = max(
            1,
            float(self.settings.get("pending_check_interval", 5))
        )

        # Паузы между повторами временных ошибок API.
        self.pending_retry_delays = (2, 4, 6)
        self.pending_not_found_delays = (2, 4, 6)

        # Время последней проверки каждой заявки.
        self.pending_last_check = {}
        self.pending_last_check_lock = threading.Lock()

        self.total_budget = float(
            self.settings.get("total_budget", 0) or 0
        )

        self.interval = max(
            0.2,
            float(self.settings.get("scan_interval", 200)) / 1000
        )

    # =====================================================
    # ОСНОВНОЙ ЦИКЛ
    # =====================================================

    def run(self):
        self.running = True

        print("Сканер запущен.")

        try:
            while not self.stop_event.is_set():

                started = time.monotonic()

                try:
                    self.schedule_pending_check()
                    self.scan()

                except Exception as exc:
                    print("Ошибка основного цикла:", exc)

                elapsed = time.monotonic() - started

                self.stop_event.wait(
                    max(0, self.interval - elapsed)
                )

        finally:
            self.running = False

            print("Останавливаю сканер...")

            self.scan_executor.shutdown(
                wait=False,
                cancel_futures=True
            )

            self.buy_executor.shutdown(
                wait=False,
                cancel_futures=True
            )

            self.pending_executor.shutdown(
                wait=False,
                cancel_futures=True
            )

            print("Сканер остановлен.")

    def stop(self):
        self.stop_event.set()

    def is_active(self):
        return not self.stop_event.is_set()

    # =====================================================
    # АВТОПОКУПКА
    # =====================================================

    def set_auto_buy_enabled(self, enabled):
        with self.auto_buy_lock:
            self.auto_buy_enabled = bool(enabled)

        print(
            "Автопокупка включена"
            if enabled
            else "Автопокупка выключена"
        )

    def is_auto_buy_enabled(self):
        with self.auto_buy_lock:
            return self.auto_buy_enabled

    # =====================================================
    # ОСТАТОК КОЛИЧЕСТВА
    # =====================================================

    @staticmethod
    def get_remaining(item):
        return int(
            item.get(
                "remaining",
                int(item.get("quantity", 0))
                - int(item.get("purchased_quantity", 0))
            )
        )

    # =====================================================
    # ПРЕДМЕТЫ ДЛЯ СКАНИРОВАНИЯ
    # =====================================================

    def get_items_with_available_slots(self):
        targets = self.database.get_all_items()

        try:
            pending_attempts = (
                self.database.get_pending_purchase_attempts()
            )
        except Exception as exc:
            print("Ошибка получения ожидающих заявок:", exc)
            return targets

        pending_counts = {}

        for attempt in pending_attempts:
            item_id = attempt.get("tracked_item_id")

            if item_id is None:
                item_id = attempt.get("item_id")

            if item_id is None:
                continue

            item_id = int(item_id)

            pending_counts[item_id] = (
                pending_counts.get(item_id, 0) + 1
            )

        available_targets = []

        for item in targets:
            item_id = int(item["id"])

            if (
                not item.get("is_active", 1)
                or item.get("is_deleted", 0)
            ):
                continue

            remaining = self.get_remaining(item)
            pending = pending_counts.get(item_id, 0)

            if remaining - pending <= 0:
                continue

            available_targets.append(item)

        return available_targets

    # =====================================================
    # НЕБЛОКИРУЮЩЕЕ СКАНИРОВАНИЕ
    # =====================================================

    def scan(self):
        if not self.is_active():
            return

        try:
            targets = self.get_items_with_available_slots()
        except Exception as exc:
            print("Ошибка получения предметов:", exc)
            return

        targets = [
            item for item in targets
            if item.get("is_active", 1)
               and not item.get("is_deleted", 0)
               and self.get_remaining(item) > 0
        ]

        active_target_ids = {
            int(item["id"]) for item in targets
        }

        snapshot_changed = False

        # Удаляем лоты предметов, которые больше не сканируются.
        with self.items_lock:
            for key, item in list(self.items.items()):
                target_id = item.get("target_id")

                if target_id is None:
                    continue

                if int(target_id) not in active_target_ids:
                    del self.items[key]
                    snapshot_changed = True

        with self.scan_targets_lock:
            active_search_ids = {
                int(target["id"])
                for target in self.scan_futures.values()
            }

            # Запускаем поиск по предметам, которые ещё не сканируются.
            for target in targets:
                if not self.is_active():
                    break

                target_id = int(target["id"])

                if target_id in active_search_ids:
                    continue

                try:
                    future = self.scan_executor.submit(
                        self.scan_one_item,
                        target
                    )

                    self.scan_futures[future] = target
                    active_search_ids.add(target_id)

                except Exception as exc:
                    print(
                        f"Не удалось запустить поиск "
                        f"{target.get('name')}: {exc}"
                    )

            # Забираем только завершённые поисковые задачи.
            completed = [
                future
                for future in self.scan_futures
                if future.done()
            ]

            for future in completed:
                target = self.scan_futures.pop(future)
                target_id = int(target["id"])

                try:
                    found, complete = future.result()

                    # При ошибке API оставляем прежние лоты.
                    if not complete:
                        continue

                    current = self.database.get_item_by_id(target_id)

                    if (
                            not current
                            or not current.get("is_active", 1)
                            or current.get("is_deleted", 0)
                            or self.get_remaining(current) <= 0
                    ):
                        continue

                    # Заменяем старые лоты этого предмета новыми.
                    with self.items_lock:
                        old_keys = [
                            key
                            for key, item in self.items.items()
                            if int(item.get("target_id", -1)) == target_id
                        ]

                        for key in old_keys:
                            del self.items[key]
                            snapshot_changed = True

                        for key, item in found.items():
                            self.items[key] = item
                            snapshot_changed = True

                    print(
                        f"Обновлён {target.get('name')}: "
                        f"{len(found)} актуальных объявлений"
                    )

                except Exception as exc:
                    print(
                        f"Ошибка сканирования "
                        f"{target.get('name')}: {exc}"
                    )

        if snapshot_changed:
            with self.items_lock:
                callback_snapshot = dict(self.items)

            try:
                self.callback(callback_snapshot)
            except Exception as exc:
                print("Ошибка обновления интерфейса:", exc)

    # =====================================================
    # ПОИСК ПО ОДНОМУ ПРЕДМЕТУ
    # =====================================================

    def scan_one_item(self, target):
        found = {}

        name = str(target.get("name") or "").strip()

        if not name:
            return found, False

        try:
            max_price = float(target.get("max_price", 0))
            target_id = int(target["id"])
        except (TypeError, ValueError, KeyError):
            return found, False

        wanted_gems = {
            value.strip().casefold()
            for value in str(target.get("gem") or "").split(",")
            if value.strip()
        }

        try:
            result = self.api.search_item(name)
        except Exception as exc:
            print(f"Ошибка поиска {name}: {exc}")
            return found, False

        if (
            not isinstance(result, dict)
            or result.get("success") is not True
        ):
            print(f"Неудачный ответ поиска {name}: {result}")
            return found, False

        offers = result.get("data")

        if offers is None:
            offers = result.get("list")

        if not isinstance(offers, list):
            return found, False

        for raw in offers:

            if not self.is_active():
                break

            try:
                listing_id = raw.get("id")

                if listing_id is None:
                    continue

                listing_id = str(listing_id)

                price_kopecks = int(raw.get("price", 0))
                price = price_kopecks / 100

                if price <= 0 or price > max_price:
                    continue

                classid = (
                    raw.get("class")
                    or raw.get("classid")
                )

                instanceid = (
                    raw.get("instance")
                    or raw.get("instanceid")
                )

                gem = ""

                if wanted_gems:

                    if not classid or instanceid is None:
                        continue

                    description = self.get_cached_description(
                        classid,
                        instanceid
                    )

                    if not description:
                        continue

                    gems = self.parser.extract_gems(description)

                    if isinstance(gems, str):
                        gems = [gems]

                    matched = next(
                        (
                            value for value in gems
                            if str(value).strip().casefold()
                            in wanted_gems
                        ),
                        None
                    )

                    if not matched:
                        continue

                    gem = matched

                candidate = {
                    "id": listing_id,
                    "listing_id": listing_id,
                    "target_id": target_id,
                    "name": name,
                    "price": price,
                    "price_kopecks": price_kopecks,
                    "max_price": max_price,
                    "classid": classid,
                    "instanceid": instanceid,
                    "gem": gem,
                    "market_hash_name": raw.get(
                        "market_hash_name",
                        name
                    ),
                    "link": (
                        "https://market.dota2.net/item/"
                        f"{classid}-{instanceid}-"
                        f"{quote(name, safe='')}/"
                    )
                }

                key = f"{target_id}:{listing_id}"
                found[key] = candidate

                self.enqueue_candidate(candidate)

            except Exception as exc:
                print(
                    "Ошибка обработки объявления:",
                    exc
                )

        return found, True

    # =====================================================
    # НЕМЕДЛЕННЫЙ ЗАПУСК ПОКУПКИ
    # =====================================================

    def enqueue_candidate(self, candidate):

        if (
            not self.is_active()
            or not self.is_auto_buy_enabled()
        ):
            return False

        listing_id = str(
            candidate.get("listing_id")
            or candidate.get("id")
            or ""
        )

        if not listing_id:
            return False

        with self.buy_inflight_lock:

            if listing_id in self.buy_inflight:
                return False

            if not self.buy_slots.acquire(blocking=False):
                return False

            self.buy_inflight.add(listing_id)

        try:
            self.buy_executor.submit(
                self._process_auto_buy_candidate_safe,
                candidate
            )

            print(
                f"ЛОТ ПЕРЕДАН НА ПОКУПКУ: "
                f"{candidate.get('name')} "
                f"| цена {candidate.get('price')} "
                f"| ID {listing_id}"
            )

            return True

        except Exception as exc:

            with self.buy_inflight_lock:
                self.buy_inflight.discard(listing_id)

            self.buy_slots.release()

            print("Не удалось запустить покупку:", exc)
            return False

    # =====================================================
    # КЭШ ОПИСАНИЙ
    # =====================================================

    def get_cached_description(self, classid, instanceid):

        key = (str(classid), str(instanceid))

        with self.description_lock:
            cached = self.description_cache.get(key)

        if cached is not None:
            return cached

        try:
            description = self.api.get_description(
                classid,
                instanceid
            )
        except Exception as exc:
            print("Ошибка получения описания:", exc)
            return None

        if (
            not isinstance(description, dict)
            or description.get("success") is not True
        ):
            return None

        with self.description_lock:
            self.description_cache[key] = description

        return description

    # =====================================================
    # БЕЗОПАСНАЯ ОБРАБОТКА ПОКУПКИ
    # =====================================================

    def _process_auto_buy_candidate_safe(self, candidate):

        listing_id = str(
            candidate.get("listing_id")
            or candidate.get("id")
            or ""
        )

        try:
            self._process_auto_buy_candidate(candidate)

        except Exception as exc:
            print(
                f"Ошибка покупки объявления {listing_id}: {exc}"
            )

        finally:
            with self.buy_inflight_lock:
                self.buy_inflight.discard(listing_id)

            self.buy_slots.release()

    # =====================================================
    # БЫСТРАЯ ПОКУПКА БЕЗ ПОВТОРНОГО ПОИСКА
    # =====================================================

    def _process_auto_buy_candidate(self, candidate):

        if (
            not self.is_active()
            or not self.is_auto_buy_enabled()
        ):
            return

        attempt_id = None
        request_started = False

        try:
            listing_id = str(
                candidate.get("listing_id")
                or candidate.get("id")
                or ""
            )

            target_id = int(candidate["target_id"])

            target = self.database.get_item_by_id(target_id)

            if (
                not target
                or not target.get("is_active", 1)
                or target.get("is_deleted", 0)
                or self.get_remaining(target) <= 0
            ):
                print(
                    f"Пропуск {listing_id}: "
                    "предмет больше не требуется."
                )
                return

            name = str(target.get("name") or "").strip()

            price_kopecks = int(candidate["price_kopecks"])
            price = price_kopecks / 100

            current_max_price = float(
                target.get("max_price", 0)
            )

            if price <= 0 or price > current_max_price:
                print(
                    f"Пропуск {listing_id}: "
                    f"цена {price} превышает лимит "
                    f"{current_max_price}"
                )
                return

            custom_id = uuid.uuid4().hex[:40]

            attempt_id = self.database.create_purchase_attempt(
                item_id=target_id,
                item_name=name,
                market_listing_id=listing_id,
                custom_id=custom_id,
                price=price,
                price_kopecks=price_kopecks,
                total_budget=self.total_budget
            )

            if attempt_id is None:
                print(
                    f"База отклонила покупку: {name}, "
                    f"лот {listing_id}. "
                    "Закончился лимит количества или бюджета."
                )
                return

            if (
                not self.is_active()
                or not self.is_auto_buy_enabled()
            ):
                self.database.update_purchase_attempt(
                    attempt_id,
                    "cancelled",
                    error="Автопокупка выключена до отправки"
                )
                return

            print(
                f"ОТПРАВЛЯЮ ЗАПРОС ПОКУПКИ: "
                f"{name} | цена {price} | ID {listing_id}"
            )

            request_started = True

            response = self.api.buy_item(
                listing_id,
                price_kopecks,
                custom_id
            )

            if not isinstance(response, dict):
                self.database.update_purchase_attempt(
                    attempt_id,
                    "unknown",
                    error="Некорректный ответ API"
                )
                return

            if response.get("request_error"):
                error = str(
                    response.get("error") or response
                )

                self.database.update_purchase_attempt(
                    attempt_id,
                    "unknown",
                    response=response,
                    error=error
                )
                return

            if response.get("success") is True:

                self.database.update_purchase_attempt(
                    attempt_id,
                    "pending",
                    response=response
                )

                print(
                    f"ЗАПРОС ПОКУПКИ ПРИНЯТ: "
                    f"{name}, лот {listing_id}"
                )
                return

            if response.get("success") is False:
                error = str(
                    response.get("error")
                    or response.get("message")
                    or response.get("data")
                    or "Причина не указана API"
                )

                self.database.update_purchase_attempt(
                    attempt_id,
                    "failed",
                    response=response,
                    error=error
                )

                print(
                    f"\n{'=' * 60}\n"
                    f"ПОКУПКА ОТКЛОНЕНА\n"
                    f"Предмет: {name}\n"
                    f"Цена: {price}\n"
                    f"ID лота: {listing_id}\n"
                    f"Ответ API: {response}\n"
                    f"{'=' * 60}"
                )
                return

            self.database.update_purchase_attempt(
                attempt_id,
                "unknown",
                response=response,
                error="Неизвестный результат покупки"
            )

        except Exception as exc:

            print("Ошибка обработки покупки:", exc)

            if attempt_id is not None:
                try:
                    self.database.update_purchase_attempt(
                        attempt_id,
                        "unknown" if request_started else "cancelled",
                        error=str(exc)
                    )
                except Exception as update_exc:
                    print(
                        "Ошибка обновления попытки:",
                        update_exc
                    )

    # =====================================================
    # ОБНАРУЖЕНИЕ ВРЕМЕННЫХ ОШИБОК API
    # =====================================================

    @staticmethod
    def is_temporary_api_error(result):

        if result is None:
            return True

        if not isinstance(result, dict):
            return True

        error_text = str(
            result.get("error", "")
        ).casefold()

        full_text = str(result).casefold()

        temporary_markers = (
            "too many request",
            "too many requests",
            "rate limit",
            "429",
            "request_error",
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "temporary",
            "лимит запросов",
            "слишком много запросов",
            "временно недоступ",
            "таймаут",
            "превышено время ожидания",
        )

        return any(
            marker in error_text or marker in full_text
            for marker in temporary_markers
        )

    @staticmethod
    def is_not_found_response(result):

        if result is None:
            return False

        text = str(result).casefold()

        markers = (
            "not found",
            "not_found",
            "не найден",
            "не найдена",
            "не найдено",
        )

        return any(marker in text for marker in markers)

    # =====================================================
    # ПОВТОРНАЯ ПРОВЕРКА СТАТУСА ПОКУПКИ
    # =====================================================

    def get_buy_info_with_retry(self, custom_id):

        last_result = None

        # Сначала обычный запрос, затем ограниченное число повторов.
        max_attempts = len(self.pending_retry_delays) + 1

        for attempt_number in range(max_attempts):

            if not self.is_active():
                return None

            try:
                result = self.api.get_buy_info(custom_id)
                last_result = result

            except Exception as exc:
                result = {
                    "success": False,
                    "request_error": True,
                    "error": str(exc),
                }
                last_result = result

            # Успешный ответ — возвращаем сразу.
            if (
                isinstance(result, dict)
                and result.get("success") is True
                and not self.is_not_found_response(result)
            ):
                return result

            # Not found иногда появляется до обновления информации API.
            if self.is_not_found_response(result):
                if attempt_number >= len(
                    self.pending_not_found_delays
                ):
                    print(
                        f"{custom_id}: API пока не находит покупку. "
                        "Оставляем заявку ожидающей."
                    )
                    return last_result

                delay = self.pending_not_found_delays[
                    attempt_number
                ]

                print(
                    f"{custom_id}: покупка пока не найдена. "
                    f"Повтор через {delay} сек. "
                    f"({attempt_number + 1}/{max_attempts - 1})"
                )

                if self.stop_event.wait(delay):
                    return None

                continue

            # Временная ошибка или ограничение запросов.
            if self.is_temporary_api_error(result):

                if attempt_number >= len(
                    self.pending_retry_delays
                ):
                    print(
                        f"{custom_id}: API не ответило после "
                        "повторных попыток. Оставляем pending."
                    )
                    return last_result

                delay = self.pending_retry_delays[
                    attempt_number
                ]

                print(
                    f"{custom_id}: временная ошибка API. "
                    f"Повтор через {delay} сек. "
                    f"({attempt_number + 1}/{max_attempts - 1})"
                )

                if self.stop_event.wait(delay):
                    return None

                continue

            # Не временная ошибка. Не делаем лишние запросы.
            return result

        return last_result

    # =====================================================
    # ЗАЩИТА ОТ СЛИШКОМ ЧАСТЫХ ПРОВЕРОК
    # =====================================================

    def can_check_pending(self, custom_id):

        now = time.monotonic()

        with self.pending_last_check_lock:

            last_check = self.pending_last_check.get(
                custom_id,
                0
            )

            if now - last_check < self.pending_check_interval:
                return False

            self.pending_last_check[custom_id] = now

        return True

    # =====================================================
    # ПРОВЕРКА ОЖИДАЮЩИХ ЗАЯВОК
    # =====================================================

    def schedule_pending_check(self):

        with self.pending_lock:

            if (
                self.pending_future is not None
                and not self.pending_future.done()
            ):
                return

            self.pending_future = (
                self.pending_executor.submit(
                    self.check_pending_purchases
                )
            )

    def check_pending_purchases(self):

        try:
            attempts = self.database.get_pending_purchase_attempts()
        except Exception as exc:
            print("Ошибка получения заявок:", exc)
            return

        for attempt in attempts:

            if not self.is_active():
                return

            custom_id = attempt.get("custom_id")
            attempt_id = attempt.get("id")

            if not custom_id or attempt_id is None:
                continue

            try:
                # Если предыдущая проверка уже установила,
                # что предмет передан, повторяем подтверждение БД.
                if attempt["status"] == "ready_to_confirm":

                    confirmed = self.database.confirm_purchase_attempt(
                        custom_id
                    )

                    if confirmed:
                        print(
                            f"ПОКУПКА ПОДТВЕРЖДЕНА: {custom_id}"
                        )

                    continue

                # Не отправляем повторный запрос слишком часто.
                if not self.can_check_pending(custom_id):
                    continue

                # Получаем статус с ограниченными повторами.
                result = self.get_buy_info_with_retry(custom_id)

                if not self.is_active():
                    return

                # Ошибки API не означают, что покупка провалилась.
                if not isinstance(result, dict):
                    print(
                        f"Временная ошибка проверки {custom_id}. "
                        "Оставляем pending."
                    )
                    continue

                # Покупка ещё не появилась в информации API.
                if self.is_not_found_response(result):
                    print(
                        f"Покупка {custom_id} пока не найдена API. "
                        "Оставляем pending."
                    )
                    continue

                # Временная ошибка API.
                if (
                    result.get("request_error")
                    or result.get("success") is not True
                ):
                    print(
                        f"Ошибка проверки {custom_id}: "
                        f"{result.get('error', result)}. "
                        "Оставляем pending."
                    )
                    continue

                data = result.get("data")

                if not isinstance(data, dict):
                    print(
                        f"Нет данных о покупке {custom_id}. "
                        "Оставляем pending."
                    )
                    continue

                stage = str(data.get("stage", ""))

                # Этап 1: сделка ещё выполняется.
                if stage == "1":

                    self.database.update_purchase_attempt(
                        attempt_id,
                        "pending",
                        response=result
                    )

                    continue

                # Этап 2: предмет передан.
                elif stage == "2":

                    self.database.update_purchase_attempt(
                        attempt_id,
                        "ready_to_confirm",
                        response=result,
                        api_item_id=data.get("item_id")
                    )

                    confirmed = self.database.confirm_purchase_attempt(
                        custom_id
                    )

                    if confirmed:
                        print(
                            f"ПОКУПКА УСПЕШНА: "
                            f"{attempt.get('item_name')} | "
                            f"item_id={data.get('item_id')}"
                        )
                    else:
                        print(
                            f"Покупка {custom_id} передана на "
                            "подтверждение, но БД пока не подтвердила её."
                        )

                    continue

                # Этап 5: сделка завершилась неудачно.
                elif stage == "5":

                    reason = (
                        data.get("cancellation_reason")
                        or "Сделка завершилась неудачей"
                    )

                    self.database.update_purchase_attempt(
                        attempt_id,
                        "failed",
                        response=result,
                        error=str(reason)
                    )

                    print(
                        f"ПОКУПКА НЕ СОСТОЯЛАСЬ: "
                        f"{attempt.get('item_name')} | {reason}"
                    )

                    continue

                # Неизвестный этап не должен освобождать резерв.
                else:

                    self.database.update_purchase_attempt(
                        attempt_id,
                        "pending",
                        response=result,
                        error=f"Ожидание распознавания stage: {stage}"
                    )

                    print(
                        f"Неизвестный stage={stage} для {custom_id}. "
                        "Оставляем pending."
                    )

            except Exception as exc:

                print(
                    f"Ошибка проверки заявки {custom_id}: {exc}"
                )

                # Не переводим покупку в failed или cancelled:
                # запрос покупки уже мог успешно выполниться.
                try:
                    self.database.update_purchase_attempt(
                        attempt_id,
                        "pending",
                        error=str(exc)
                    )
                except Exception as update_exc:
                    print(
                        f"Ошибка сохранения статуса {custom_id}: "
                        f"{update_exc}"
                    )
