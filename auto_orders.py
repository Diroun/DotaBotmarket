import threading
import traceback


class AutoOrderManager(threading.Thread):
    """
    Менеджер автоматических ордеров.

    PAI цены: копейки.
    База данных: рубли.

    Legacy API:
      - один ордер на classid + instanceid;
      - InsertOrder создаёт один ордер;
      - UpdateOrder изменяет цену;
      - UpdateOrder с ценой 0 отменяет конкретный вариант.

    Менеджер изменяет и отменяет только ордера,
    для которых существует резерв в базе данных.
    """

    def __init__(
        self,
        api,
        database,
        parser,
        settings=None,
        callback=None,
    ):
        super().__init__(daemon=False)

        self.api = api
        self.db = database
        self.parser = parser
        self.settings = settings or {}
        self.callback = callback

        self.stop_event = threading.Event()
        self.running = False
        self._lock = threading.RLock()
        self._cleanup_started = False

        self.interval = max(
            5,
            int(self.settings.get("auto_order_interval", 15))
        )

        self.step = max(
            0.01,
            float(self.settings.get("auto_order_step", 0.01))
        )

        self.total_budget = float(
            self.settings.get("total_budget", 0) or 0
        )

        self.allow_cancel = bool(
            self.settings.get("auto_order_allow_cancel", True)
        )

    # ==================================================
    # ЛОГИРОВАНИЕ
    # ==================================================

    def log(self, message):
        message = f"[AUTO ORDER] {message}"
        print(message, flush=True)

        if self.callback:
            try:
                self.callback(message)
            except Exception:
                pass

    def stop(self):
        if not self.stop_event.is_set():
            self.stop_event.set()
            self.log("Получена команда остановки.")

    def stopped(self):
        return self.stop_event.is_set()

    # ==================================================
    # РАЗБОР ОТВЕТОВ API
    # ==================================================

    @staticmethod
    def _get_rows(response):
        if isinstance(response, list):
            return response

        if not isinstance(response, dict):
            return None

        keys = (
            "data",
            "items",
            "orders",
            "result",
            "Orders",
            "log",
            "offers",
        )

        for key in keys:
            value = response.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                for nested_key in keys:
                    nested = value.get(nested_key)

                    if isinstance(nested, list):
                        return nested

        if response.get("success") is True:
            return []

        return None

    @staticmethod
    def _api_success(response):
        if not isinstance(response, dict):
            return False

        return response.get("success") is True

    @staticmethod
    def _get_hash_name(order):
        if not isinstance(order, dict):
            return ""

        return str(
            order.get("market_hash_name")
            or order.get("hash_name")
            or order.get("market_hashname")
            or order.get("name")
            or ""
        ).strip()

    @staticmethod
    def _get_phase(order):
        if not isinstance(order, dict):
            return None

        phase = order.get("phase")

        if phase is None:
            extra = order.get("extra")

            if isinstance(extra, dict):
                phase = extra.get("phase")

        return phase

    @staticmethod
    def _same_phase(first, second):
        if first is None and second is None:
            return True

        if first is None or second is None:
            return False

        return str(first) == str(second)

    @staticmethod
    def _get_classid(order):
        if not isinstance(order, dict):
            return None

        value = (
            order.get("classid")
            or order.get("i_classid")
            or order.get("class")
        )

        return str(value) if value is not None else None

    @staticmethod
    def _get_instanceid(order):
        if not isinstance(order, dict):
            return None

        value = (
            order.get("instanceid")
            or order.get("i_instanceid")
            or order.get("instance")
        )

        return str(value) if value is not None else None

    @staticmethod
    def _get_count(order):
        if not isinstance(order, dict):
            return 0

        value = order.get("count")

        if value is None:
            value = order.get("c")

        if value is None:
            value = 1

        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _get_price_rub(order):
        """
        GetOrders возвращает o_price в копейках.
        """

        if not isinstance(order, dict):
            return 0.0

        value = order.get("o_price")

        if value is None:
            value = order.get("price")

        if value is None:
            value = order.get("price_rub")

        if value is None:
            value = order.get("amount")

        try:
            return float(value or 0) / 100
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _get_fill_price_rub(fill):
        if not isinstance(fill, dict):
            return 0.0

        try:
            return float(fill.get("price", 0)) / 100
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_kopecks(price_rub):
        return int(round(float(price_rub) * 100))

    # ==================================================
    # РЕЗЕРВЫ БАЗЫ
    # ==================================================

    def _get_reservations(self):
        try:
            rows = self.db.get_auto_order_reservations()
            return rows if isinstance(rows, list) else []

        except Exception as error:
            self.log(f"Ошибка чтения резервов: {error}")
            return []

    def _get_item_reservations(self, tracked_item_id):
        result = []

        for row in self._get_reservations():
            try:
                if int(row.get("tracked_item_id", -1)) == int(
                    tracked_item_id
                ):
                    result.append(row)
            except (TypeError, ValueError):
                continue

        return result

    def _get_pair_reservation(self, classid, instanceid):
        for row in self._get_reservations():
            if (
                self._get_classid(row) == str(classid)
                and self._get_instanceid(row) == str(instanceid)
            ):
                return row

        return None

    def _save_reservation(
        self,
        tracked_item_id,
        market_hash_name,
        phase,
        count,
        price,
        classid,
        instanceid,
    ):
        self.db.save_auto_order_reservation(
            tracked_item_id=tracked_item_id,
            market_hash_name=market_hash_name,
            phase=phase,
            count=count,
            price=price,
            classid=classid,
            instanceid=instanceid,
        )

    def _remove_reservation(self, classid, instanceid):
        self.db.remove_auto_order_reservation(
            classid=classid,
            instanceid=instanceid,
        )

    # ==================================================
    # СОБСТВЕННЫЕ ОРДЕРА
    # ==================================================

    def _get_all_pages(
        self,
        method,
        label,
        ignore_stop=False,
        page_size=500,
    ):
        result = []

        for page in range(100):
            if self.stopped() and not ignore_stop:
                return None

            try:
                response = method(page=page)

            except Exception as error:
                self.log(
                    f"Ошибка {label}, страница {page}: {error}"
                )
                return None

            if not isinstance(response, dict):
                self.log(
                    f"Некорректный ответ {label}, страница {page}: "
                    f"{response}"
                )
                return None

            if response.get("success") is not True:
                self.log(
                    f"API не подтвердил {label}: {response}"
                )
                return None

            rows = self._get_rows(response)

            if rows is None:
                self.log(
                    f"Неизвестная структура ответа {label}: "
                    f"{response}"
                )
                return None

            result.extend(rows)

            if len(rows) < page_size:
                return result

        self.log(f"{label}: достигнут лимит страниц.")
        return None

    def get_my_orders(self, ignore_stop=False):
        return self._get_all_pages(
            self.api.get_variant_orders,
            "GetOrders",
            ignore_stop=ignore_stop,
            page_size=500,
        )

    def get_order_log(self, ignore_stop=False):
        if self.stopped() and not ignore_stop:
            return None

        try:
            response = self.api.get_variant_orders_log()

        except Exception as error:
            self.log(f"Ошибка GetOrdersLog: {error}")
            return None

        if not isinstance(response, dict):
            self.log("Некорректный ответ GetOrdersLog.")
            return None

        if response.get("success") is False:
            self.log(f"Ошибка GetOrdersLog: {response}")
            return None

        rows = self._get_rows(response)

        if rows is None:
            self.log(
                f"Неизвестная структура GetOrdersLog: {response}"
            )
            return None

        return rows

    # ==================================================
    # ИСПОЛНЕНИЯ
    # ==================================================

    @staticmethod
    def _get_fill_id(fill):
        """
        Используем ID самого предмета/исполнения.
        Не используем order_id: один ордер может
        исполниться несколько раз.
        """

        if not isinstance(fill, dict):
            return None

        for key in (
            "item_id",
            "i_id",
            "id",
            "item",
        ):
            value = fill.get(key)

            if isinstance(value, (str, int)):
                value = str(value).strip()

                if value:
                    return value

        return None

    def process_order_fills(self):
        self.log("Проверяю исполнения ордеров.")

        fills = self.get_order_log()

        if fills is None:
            self.log("История исполнений недоступна.")
            return

        if not fills:
            self.log("Новых исполнений в истории нет.")
            return

        reservations = self._get_reservations()

        for fill in fills:
            if self.stopped():
                return

            if not isinstance(fill, dict):
                continue

            fill_id = self._get_fill_id(fill)

            if not fill_id:
                continue

            hash_name = self._get_hash_name(fill)
            phase = self._get_phase(fill)

            classid = self._get_classid(fill)
            instanceid = self._get_instanceid(fill)

            candidates = []

            for reservation in reservations:
                if self._get_hash_name(reservation) != hash_name:
                    continue

                if not self._same_phase(
                    self._get_phase(reservation),
                    phase,
                ):
                    continue

                reserve_classid = self._get_classid(reservation)
                reserve_instanceid = self._get_instanceid(
                    reservation
                )

                if classid and instanceid:
                    if (
                        reserve_classid != classid
                        or reserve_instanceid != instanceid
                    ):
                        continue

                try:
                    tracked_id = int(
                        reservation["tracked_item_id"]
                    )
                except (KeyError, TypeError, ValueError):
                    continue

                candidates.append(tracked_id)

            candidates = list(set(candidates))

            if len(candidates) != 1:
                if len(candidates) > 1:
                    self.log(
                        f"Исполнение {fill_id} неоднозначно: "
                        f"{hash_name}. Пропускаю."
                    )
                continue

            price = self._get_fill_price_rub(fill)

            if price <= 0:
                self.log(
                    f"Некорректная цена исполнения {fill_id}."
                )
                continue

            try:
                registered = self.db.register_order_fill(
                    item_id=fill_id,
                    tracked_item_id=candidates[0],
                    price=price,
                )

            except Exception as error:
                self.log(
                    f"Ошибка регистрации исполнения "
                    f"{fill_id}: {error}"
                )
                continue

            if registered:
                self.log(
                    f"Учтено исполнение: {hash_name}, "
                    f"{price:.2f} ₽."
                )

    # ==================================================
    # ПОИСК ВАРИАНТОВ
    # ==================================================

    def find_market_items(self, item):
        name = str(item["name"])

        try:
            response = self.api.search_item(name)

        except Exception as error:
            self.log(f"Ошибка поиска {name}: {error}")
            return None

        if not isinstance(response, dict):
            self.log(f"{name}: некорректный ответ поиска.")
            return None

        if response.get("success") is not True:
            self.log(
                f"{name}: API не подтвердил поиск: {response}"
            )
            return None

        rows = self._get_rows(response)

        if rows is None:
            self.log(
                f"{name}: неизвестная структура ответа поиска."
            )
            return None

        variants = {}

        for row in rows:
            if not isinstance(row, dict):
                continue

            classid = self._get_classid(row)
            instanceid = self._get_instanceid(row)

            if not classid or not instanceid:
                continue

            hash_name = (
                row.get("market_hash_name")
                or row.get("hash_name")
                or name
            )

            key = (classid, instanceid)

            variants[key] = {
                "market_hash_name": str(hash_name),
                "classid": classid,
                "instanceid": instanceid,
                "phase": self._get_phase(row),
            }

        return list(variants.values())

    # ==================================================
    # ЛУЧШАЯ ЧУЖАЯ ЗАЯВКА
    # ==================================================

    def get_best_competitor_price(self, classid, instanceid):
        try:
            response = self.api.get_buy_orders(
                classid,
                instanceid,
            )

        except Exception as error:
            self.log(
                f"Ошибка BuyOffers {classid}/{instanceid}: "
                f"{error}"
            )
            return None

        if not isinstance(response, dict):
            self.log(
                f"BuyOffers {classid}/{instanceid}: "
                "некорректный ответ."
            )
            return None

        if response.get("success") is not True:
            self.log(f"Ошибка BuyOffers: {response}")
            return None

        offers = response.get("offers")

        if not isinstance(offers, list):
            offers = self._get_rows(response)

        if not isinstance(offers, list):
            self.log(
                f"BuyOffers {classid}/{instanceid}: "
                "список заявок не найден."
            )
            return None

        competitor_prices = []

        for offer in offers:
            if not isinstance(offer, dict):
                continue

            try:
                price = int(offer.get("o_price", 0))
                total_count = int(offer.get("c", 0))
                my_count = int(offer.get("my_count", 0))

            except (TypeError, ValueError):
                self.log(
                    f"Некорректная заявка BuyOffers: {offer}"
                )
                return None

            if price <= 0:
                continue

            if total_count < 0 or my_count < 0:
                return None

            if total_count > my_count:
                competitor_prices.append(price)

        if not competitor_prices:
            return 0.0

        return max(competitor_prices) / 100

    # ==================================================
    # ПОИСК СОБСТВЕННЫХ ОРДЕРОВ
    # ==================================================

    def find_variant_orders(self, orders, classid, instanceid):
        matches = []

        for order in orders or []:
            if not isinstance(order, dict):
                continue

            if self._get_classid(order) != str(classid):
                continue

            if self._get_instanceid(order) != str(instanceid):
                continue

            matches.append(order)

        return matches

    # ==================================================
    # БЮДЖЕТ
    # ==================================================

    def _check_budget(
        self,
        classid,
        instanceid,
        count,
        price,
    ):
        if self.total_budget <= 0:
            return True

        try:
            details = self.db.get_budget_details()

            if isinstance(details, dict):
                total = details.get("total")

                if total is None:
                    total = details.get("used")

                if total is None:
                    total = details.get("total_usage")

                if total is None:
                    self.log(
                        "Неизвестный формат get_budget_details(). "
                        "Выставление ордера отменено для безопасности."
                    )
                    return False

                total = float(total)

            else:
                total = float(details)

        except Exception as error:
            self.log(f"Ошибка проверки бюджета: {error}")
            return False

        old_reserve = 0.0

        reservation = self._get_pair_reservation(
            classid,
            instanceid,
        )

        if reservation:
            try:
                old_reserve = (
                    int(reservation.get("count", 0))
                    * float(reservation.get("price", 0))
                )
            except (TypeError, ValueError):
                old_reserve = 0.0

        new_reserve = count * price
        used_without_this_order = total - old_reserve

        if used_without_this_order + new_reserve > self.total_budget:
            self.log(
                f"Недостаточно бюджета: требуется "
                f"{new_reserve:.2f} ₽."
            )
            return False

        return True

    # ==================================================
    # ОТМЕНА ОДНОГО УПРАВЛЯЕМОГО ВАРИАНТА
    # ==================================================

    def cancel_variant(
        self,
        classid,
        instanceid,
        tracked_item_id=None,
        ignore_stop=False,
    ):
        if not self.allow_cancel:
            self.log("Отмена ордеров отключена настройкой.")
            return False

        if self.stopped() and not ignore_stop:
            return False

        reservation = self._get_pair_reservation(
            classid,
            instanceid,
        )

        if reservation is None:
            self.log(
                f"Не отменяю {classid}/{instanceid}: "
                "резерв менеджера не найден."
            )
            return False

        if (
            tracked_item_id is not None
            and str(reservation.get("tracked_item_id"))
            != str(tracked_item_id)
        ):
            self.log(
                f"Не отменяю {classid}/{instanceid}: "
                "резерв принадлежит другой позиции."
            )
            return False

        try:
            response = self.api.cancel_variant_order(
                classid,
                instanceid,
            )

        except Exception as error:
            self.log(
                f"Ошибка отмены {classid}/{instanceid}: {error}"
            )
            return False

        if not self._api_success(response):
            self.log(
                f"API не подтвердил отмену "
                f"{classid}/{instanceid}: {response}"
            )
            return False

        self.log(
            f"Отмена отправлена: {classid}/{instanceid}."
        )

        orders = self.get_my_orders(ignore_stop=ignore_stop)

        if orders is None:
            self.log(
                f"Не удалось проверить отмену "
                f"{classid}/{instanceid}. Резерв оставлен."
            )
            return False

        remaining = self.find_variant_orders(
            orders,
            classid,
            instanceid,
        )

        if remaining:
            self.log(
                f"Ордер {classid}/{instanceid} "
                "пока присутствует в GetOrders."
            )
            return False

        try:
            self._remove_reservation(classid, instanceid)

        except Exception as error:
            self.log(f"Ошибка удаления резерва: {error}")
            return False

        self.log(
            f"Ордер {classid}/{instanceid} отменён."
        )
        return True

    # ==================================================
    # ОТМЕНА ВСЕХ ОРДЕРОВ ПО ПОЗИЦИИ
    # ==================================================

    def cancel_all_orders_for_item(
        self,
        tracked_item_id,
        ignore_stop=False,
    ):
        reservations = self._get_item_reservations(
            tracked_item_id
        )

        if not reservations:
            return True

        result = True

        for reservation in reservations:
            if self.stopped() and not ignore_stop:
                return False

            classid = self._get_classid(reservation)
            instanceid = self._get_instanceid(reservation)

            # Старые резервы без идентификаторов
            # нельзя безопасно отменять по названию.
            if not classid or not instanceid:
                hash_name = self._get_hash_name(reservation)

                self.log(
                    f"Старый резерв {tracked_item_id} "
                    f"({hash_name}) не содержит classid/instanceid. "
                    "Автоматическая отмена пропущена."
                )
                result = False
                continue

            if not self.cancel_variant(
                classid,
                instanceid,
                tracked_item_id=tracked_item_id,
                ignore_stop=ignore_stop,
            ):
                result = False

        return result

    # ==================================================
    # СТАРЫЕ РЕЗЕРВЫ
    # ==================================================

    def cancel_legacy_reservation(
        self,
        tracked_item_id,
        hash_name,
        phase,
        ignore_stop=False,
    ):
        """
        Старый резерв не содержит classid/instanceid.
        Не отменяем ордера только по названию:
        нельзя гарантировать, что это не ручной ордер.
        """

        self.log(
            f"Старый резерв {tracked_item_id}: "
            f"проверяю {hash_name}."
        )

        orders = self.get_my_orders(ignore_stop=ignore_stop)

        if orders is None:
            self.log(
                f"Не удалось проверить старый резерв "
                f"{tracked_item_id}. Оставляю его."
            )
            return False

        matches = []

        for order in orders:
            if not isinstance(order, dict):
                continue

            if self._get_hash_name(order) != hash_name:
                continue

            if not self._same_phase(
                self._get_phase(order),
                phase,
            ):
                continue

            matches.append(order)

        if matches:
            self.log(
                f"Старый резерв {tracked_item_id}: "
                f"найдено {len(matches)} ордеров без точной "
                "привязки. Не отменяю их, чтобы не задеть "
                "ручные ордера. Резерв сохранён."
            )
            return False

        try:
            self.db.remove_auto_order_reservation(
                tracked_item_id
            )

            self.log(
                f"Старый резерв {tracked_item_id} "
                "удалён: активных ордеров не найдено."
            )
            return True

        except Exception as error:
            self.log(
                f"Ошибка удаления старого резерва: {error}"
            )
            return False

    # ==================================================
    # ОТМЕНА УПРАВЛЯЕМЫХ ОРДЕРОВ ПРИ ЗАВЕРШЕНИИ
    # ==================================================

    def cancel_all_managed_orders(self):
        if self._cleanup_started:
            return

        self._cleanup_started = True

        self.log("Начинаю отмену управляемых ордеров.")

        reservations = self._get_reservations()

        if not reservations:
            self.log("Активных резервов нет.")
            return

        tracked_ids = set()

        for reservation in reservations:
            try:
                tracked_ids.add(
                    int(reservation["tracked_item_id"])
                )
            except (KeyError, TypeError, ValueError):
                continue

        for tracked_id in tracked_ids:
            self.cancel_all_orders_for_item(
                tracked_id,
                ignore_stop=True,
            )

        self.log("Завершена обработка управляемых резервов.")

    # ==================================================
    # ОБРАБОТКА ОДНОГО ВАРИАНТА
    # ==================================================

    def process_variant(
        self,
        item,
        variant,
        current_orders,
        allow_create=True,
    ):
        if self.stopped():
            return current_orders, False

        tracked_id = int(item["id"])
        name = str(item["name"])

        hash_name = variant["market_hash_name"]
        classid = str(variant["classid"])
        instanceid = str(variant["instanceid"])
        phase = variant.get("phase")

        reservation = self._get_pair_reservation(
            classid,
            instanceid,
        )

        # Если резерв принадлежит другой позиции,
        # этот вариант не трогаем.
        if reservation is not None:
            if str(reservation.get("tracked_item_id")) != str(
                tracked_id
            ):
                self.log(
                    f"{name}: вариант {classid}/{instanceid} "
                    "уже зарезервирован другой позицией."
                )
                return current_orders, False

        own_orders = self.find_variant_orders(
            current_orders,
            classid,
            instanceid,
        )

        if len(own_orders) > 1:
            self.log(
                f"{name}: несколько собственных ордеров "
                f"для {classid}/{instanceid}. Пропускаю."
            )
            return current_orders, False

        own_order = own_orders[0] if own_orders else None

        # Найден ордер, но резерва нет.
        # Это может быть ручной ордер — не изменяем его.
        if own_order is not None and reservation is None:
            self.log(
                f"{name}: найден ордер без резерва менеджера "
                f"{classid}/{instanceid}. Считаю его ручным."
            )
            return current_orders, False

        # Резерв есть, а ордера нет.
        # Проверяем вариант заново и при возможности создаём.
        if reservation is not None and own_order is None:
            self.log(
                f"{name}: резерв есть, но ордер "
                f"{classid}/{instanceid} не найден."
            )

            try:
                self._remove_reservation(classid, instanceid)
            except Exception as error:
                self.log(f"Ошибка удаления устаревшего резерва: {error}")
                return current_orders, False

            reservation = None

        if own_order is None and not allow_create:
            return current_orders, False

        competitor_price = self.get_best_competitor_price(
            classid,
            instanceid,
        )

        if competitor_price is None:
            return current_orders, False

        max_price = float(item.get("max_price", 0) or 0)

        if max_price <= 0:
            return current_orders, False

        own_price = (
            self._get_price_rub(own_order)
            if own_order else 0.0
        )

        if competitor_price <= 0:
            if own_order:
                target_price = own_price
            else:
                self.log(
                    f"{name} [{classid}/{instanceid}]: "
                    "нет чужих заявок. Ордер не выставляю."
                )
                return current_orders, False

        elif own_order and competitor_price <= own_price:
            target_price = own_price

        else:
            target_price = competitor_price + self.step

        target_price = min(target_price, max_price)

        # Не понижаем текущую цену автоматически.
        if own_order:
            target_price = max(target_price, own_price)

        target_price = round(target_price, 2)

        if target_price <= 0:
            return current_orders, False

        if (
            competitor_price > 0
            and target_price <= competitor_price
        ):
            self.log(
                f"{name}: чужая заявка "
                f"{competitor_price:.2f} ₽ выше лимита "
                f"{max_price:.2f} ₽."
            )
            return current_orders, False

        own_count = self._get_count(own_order) if own_order else 0

        if (
            own_order
            and abs(own_price - target_price) < 0.001
            and own_count == 1
        ):
            if reservation is None:
                self._save_reservation(
                    tracked_id,
                    hash_name,
                    phase,
                    1,
                    own_price,
                    classid,
                    instanceid,
                )

            return current_orders, False

        # Legacy InsertOrder создаёт один предмет.
        order_count = 1

        if not self._check_budget(
            classid,
            instanceid,
            order_count,
            target_price,
        ):
            return current_orders, False

        target_kopecks = self._to_kopecks(target_price)

        try:
            if own_order:
                response = self.api.update_variant_order(
                    classid,
                    instanceid,
                    target_kopecks,
                )
            else:
                response = self.api.create_variant_order(
                    classid,
                    instanceid,
                    target_kopecks,
                    item_hash="",
                )

        except Exception as error:
            self.log(
                f"Ошибка установки ордера {name}: {error}"
            )
            return current_orders, False

        if not self._api_success(response):
            self.log(
                f"API не подтвердил ордер {name} "
                f"[{classid}/{instanceid}]: {response}"
            )
            return current_orders, False

        try:
            self._save_reservation(
                tracked_id,
                hash_name,
                phase,
                order_count,
                target_price,
                classid,
                instanceid,
            )

        except Exception as error:
            self.log(
                f"Ордер отправлен, но резерв не сохранён: {error}"
            )
            return current_orders, False

        action = "изменён" if own_order else "создан"

        self.log(
            f"{name}: ордер {action}; "
            f"вариант {classid}/{instanceid}; "
            f"1 шт.; {target_price:.2f} ₽."
        )

        refreshed = self.get_my_orders()

        if refreshed is None:
            self.log(
                f"{name}: не удалось проверить ордер. "
                "Резерв сохранён."
            )
            return current_orders, not own_order

        verified = self.find_variant_orders(
            refreshed,
            classid,
            instanceid,
        )

        if verified:
            self.log(
                f"{name}: ордер подтверждён "
                f"{classid}/{instanceid}."
            )
        else:
            self.log(
                f"{name}: ордер пока не появился в GetOrders. "
                "Резерв сохранён для следующей проверки."
            )

        return refreshed, not own_order

    # ==================================================
    # ОБРАБОТКА ОДНОГО ТОВАРА
    # ==================================================

    def process_item(self, item, current_orders):
        if self.stopped():
            return current_orders

        item_id = int(item["id"])
        name = str(item["name"])

        if str(item.get("gem") or "").strip():
            self.log(
                f"{name}: указан gem. "
                "Обычные ордера не выставляются."
            )
            return current_orders

        quantity = max(0, int(item.get("quantity", 1)))
        purchased = max(
            0,
            int(item.get("purchased_quantity", 0)),
        )

        try:
            pending = max(
                0,
                int(self.db.get_pending_count(item_id)),
            )

            pending_rows = self.db.get_pending_purchase_attempts()

            if isinstance(pending_rows, list):
                item_pending_rows = [
                    row for row in pending_rows
                    if str(row.get("tracked_item_id")) == str(item_id)
                ]
            else:
                item_pending_rows = []


        except Exception as error:
            self.log(
                f"{name}: ошибка получения pending-покупок: {error}"
            )
            return current_orders

        remaining = max(
            0,
            quantity - purchased - pending,
        )

        # Цель уже достигнута фактическими покупками.
        if purchased >= quantity:
            self.log(
                f"{name}: цель достигнута. "
                "Отменяю управляемые ордера."
            )

            self.cancel_all_orders_for_item(item_id)
            return current_orders

        # Есть незавершённые прямые покупки или уже достигнут лимит.
        if remaining <= 0:
            self.log(
                f"{name}: свободного количества нет "
                f"(цель {quantity}, куплено {purchased}, "
                f"ожидает {pending})."
            )

            self.cancel_all_orders_for_item(item_id)
            return current_orders

        max_price = float(item.get("max_price", 0) or 0)

        if max_price <= 0:
            self.log(f"{name}: максимальная цена не задана.")
            return current_orders

        variants = self.find_market_items(item)

        if variants is None:
            self.log(f"{name}: не удалось получить варианты.")
            return current_orders

        if not variants:
            self.log(f"{name}: варианты предмета не найдены.")
            return current_orders

        self.log(
            f"{name}: найдено вариантов: {len(variants)}; "
            f"доступно к заказу: {remaining}."
        )

        # Считаем только активные управляемые ордера,
        # которые реально присутствуют в GetOrders.
        reservations = self._get_item_reservations(item_id)

        active_managed_pairs = set()

        for reservation in reservations:
            classid = self._get_classid(reservation)
            instanceid = self._get_instanceid(reservation)

            if not classid or not instanceid:
                continue

            if self.find_variant_orders(
                current_orders,
                classid,
                instanceid,
            ):
                active_managed_pairs.add((classid, instanceid))

        # Не создаём ордеров сверх оставшегося количества.
        slots = max(
            0,
            remaining - len(active_managed_pairs),
        )

        self.log(
            f"{name}: активных управляемых ордеров "
            f"{len(active_managed_pairs)}; "
            f"свободных мест для новых: {slots}."
        )

        for variant in variants:
            if self.stopped():
                break

            classid = str(variant["classid"])
            instanceid = str(variant["instanceid"])

            reservation = self._get_pair_reservation(
                classid,
                instanceid,
            )

            is_managed = (
                reservation is not None
                and str(reservation.get("tracked_item_id"))
                == str(item_id)
            )

            # Если уже есть управляемый ордер — обновляем его.
            # Новый ордер можно создать только при свободном слоте.
            allow_create = slots > 0

            current_orders, created = self.process_variant(
                item,
                variant,
                current_orders,
                allow_create=allow_create,
            )

            if created:
                slots = max(0, slots - 1)

        return current_orders

    # ==================================================
    # ОБРАБОТКА ВСЕХ ТОВАРОВ
    # ==================================================

    def process_all_items(self):
        self.log("Начинаю рабочий цикл.")

        self.process_order_fills()

        if self.stopped():
            return

        self.log("Запрашиваю собственные ордера.")

        current_orders = self.get_my_orders()

        if current_orders is None:
            self.log(
                "Цикл пропущен: список собственных ордеров "
                "недоступен."
            )
            return

        self.log(
            f"Получено собственных ордеров: "
            f"{len(current_orders)}."
        )

        try:
            items = self.db.get_all_items()
        except Exception as error:
            self.log(f"Ошибка чтения списка товаров: {error}")
            return

        if not isinstance(items, list):
            self.log("База вернула некорректный список товаров.")
            return

        active_ids = set()

        for item in items:
            if self.stopped():
                return

            if not isinstance(item, dict):
                continue

            if not bool(item.get("is_active", 1)):
                continue

            if bool(item.get("is_deleted", 0)):
                continue

            try:
                item_id = int(item["id"])
                active_ids.add(item_id)

                current_orders = self.process_item(
                    item,
                    current_orders,
                )

            except Exception as error:
                self.log(
                    f"Ошибка обработки {item.get('name')}: {error}"
                )
                traceback.print_exc()

        # Выключенные или удалённые позиции.
        reservations = self._get_reservations()
        processed_ids = set()

        for reservation in reservations:
            if self.stopped():
                return

            try:
                tracked_id = int(
                    reservation["tracked_item_id"]
                )
            except (KeyError, TypeError, ValueError):
                continue

            if tracked_id in active_ids:
                continue

            if tracked_id in processed_ids:
                continue

            processed_ids.add(tracked_id)

            self.log(
                f"Позиция {tracked_id} выключена или удалена. "
                "Отменяю управляемые ордера."
            )

            self.cancel_all_orders_for_item(tracked_id)

        self.log("Рабочий цикл завершён.")

    # ==================================================
    # ЗАПУСК И ОСТАНОВКА
    # ==================================================

    def run(self):
        self.running = True

        self.log("Менеджер автоордеров запущен.")

        try:
            while not self.stopped():
                try:
                    with self._lock:
                        self.process_all_items()

                except Exception as error:
                    self.log(
                        f"Ошибка рабочего цикла: {error}"
                    )
                    traceback.print_exc()

                if not self.stopped():
                    self.stop_event.wait(self.interval)

        finally:
            self.log(
                "Менеджер останавливается. "
                "Обрабатываю управляемые ордера."
            )

            try:
                with self._lock:
                    self.cancel_all_managed_orders()

            except Exception as error:
                self.log(
                    f"Ошибка завершения менеджера: {error}"
                )
                traceback.print_exc()

            self.running = False

            self.log("Менеджер автоордеров остановлен.")