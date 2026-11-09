
import threading
import time
import uuid
from urllib.parse import quote

import requests


BASE_URL = "https://market.dota2.net/api/"
V2_URL = BASE_URL + "v2/"


class RateLimiter:
    """Не более пяти запросов в секунду."""

    def __init__(self, limit=5):
        self.limit = limit
        self.timestamps = []
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()

                self.timestamps = [
                    t for t in self.timestamps
                    if now - t < 1.0
                ]

                if len(self.timestamps) < self.limit:
                    self.timestamps.append(now)
                    return

                delay = max(
                    0.02,
                    1.0 - (now - self.timestamps[0])
                )

            time.sleep(delay)


class MarketAPI:

    def __init__(self, api_key):
        if not api_key:
            raise ValueError("Не задан API key Market Dota2")

        self.api_key = api_key
        self.limiter = RateLimiter(5)
        self.session = requests.Session()

    # ==================================================
    # ОБЩИЕ HTTP-ЗАПРОСЫ
    # ==================================================

    def _request_url(
        self,
        method,
        url,
        params=None,
        retry=True
    ):
        params = dict(params or {})
        params["key"] = self.api_key

        attempts = 3 if retry else 1
        last_error = None

        for attempt in range(attempts):
            self.limiter.wait()

            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    timeout=(5, 25)
                )

                response.raise_for_status()

                if not response.text.strip():
                    return {
                        "success": False,
                        "request_error": True,
                        "error": "Пустой ответ API"
                    }

                try:
                    return response.json()
                except ValueError:
                    return {
                        "success": False,
                        "request_error": True,
                        "error": "API вернул некорректный JSON",
                        "response": response.text[:1000]
                    }

            except requests.RequestException as exc:
                last_error = exc

                if attempt + 1 < attempts:
                    time.sleep(0.6 * (attempt + 1))

        print(f"Ошибка Market Dota2 API: {last_error}")

        return {
            "success": False,
            "request_error": True,
            "error": str(last_error)
        }

    def _request(
        self,
        method,
        path,
        params=None,
        retry=True
    ):
        return self._request_url(
            method,
            V2_URL + path.lstrip("/"),
            params=params,
            retry=retry
        )

    # ==================================================
    # ПОИСК ПРЕДМЕТОВ
    # ==================================================

    def search_item(self, name):
        return self._request(
            "GET",
            "search-item-by-hash-name-specific",
            {
                "hash_name": name,
                "lang": "ru",
                "with_alfaskins": 0
            }
        )

    # ==================================================
    # ОПИСАНИЕ ПРЕДМЕТА
    # ==================================================

    def get_description(self, classid, instanceid):
        url = (
            BASE_URL
            + "GetItemDescription/"
            + f"{quote(str(classid), safe='')}_"
            + f"{quote(str(instanceid), safe='')}/"
        )

        result = self._request_url("GET", url)

        if not isinstance(result, dict):
            return {}

        if result.get("success") is False:
            return {}

        if isinstance(result.get("description"), list):
            result.setdefault("success", True)
            return result

        return {}

    # ==================================================
    # BUY OFFERS — ЗАЯВКИ ПО КОНКРЕТНОМУ ВАРИАНТУ
    # ==================================================

    def get_buy_orders(self, classid, instanceid):
        """
        Получить публичные заявки на покупку
        конкретного варианта предмета.
        """

        url = (
            BASE_URL
            + "BuyOffers/"
            + f"{quote(str(classid), safe='')}_"
            + f"{quote(str(instanceid), safe='')}/"
        )

        return self._request_url("GET", url)

    # ==================================================
    # LEGACY API — МОИ ОРДЕРА С CLASSID / INSTANCEID
    # ==================================================

    def get_variant_orders(self, page=0):
        """
        Получить список собственных ордеров.
        Ответ legacy API содержит classid и instanceid.
        """

        url = BASE_URL + f"GetOrders/{int(page)}/"

        return self._request_url("GET", url)

    def get_variant_orders_log(self):
        """
        Получить историю исполненных ордеров.
        """

        url = BASE_URL + "GetOrdersLog/"

        return self._request_url("GET", url)

    # ==================================================
    # СОЗДАНИЕ LEGACY ОРДЕРА
    # ==================================================

    def create_variant_order(
        self,
        classid,
        instanceid,
        price_kopecks,
        item_hash=""
    ):
        """
        Создать ордер на один предмет конкретного варианта.

        Цена передаётся в копейках.
        Один запрос создаёт один ордер на один предмет.
        """

        if not classid or not instanceid:
            return {
                "success": False,
                "error": "Не указан classid или instanceid"
            }

        try:
            price_kopecks = int(price_kopecks)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Некорректная цена"
            }

        if price_kopecks <= 0:
            return {
                "success": False,
                "error": "Цена должна быть больше нуля"
            }

        url = (
            BASE_URL
            + f"InsertOrder/{quote(str(classid), safe='')}/"
            + f"{quote(str(instanceid), safe='')}/"
            + f"{price_kopecks}/"
            + f"{quote(str(item_hash), safe='')}/"
        )

        return self._request_url(
            "GET",
            url,
            retry=False
        )

    # ==================================================
    # ИЗМЕНЕНИЕ LEGACY ОРДЕРА
    # ==================================================

    def update_variant_order(
        self,
        classid,
        instanceid,
        price_kopecks
    ):
        """
        Изменить цену ордера конкретного варианта.

        Цена 0 удаляет ордер.
        Цена указывается в копейках.
        """

        if not classid or not instanceid:
            return {
                "success": False,
                "error": "Не указан classid или instanceid"
            }

        try:
            price_kopecks = int(price_kopecks)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Некорректная цена"
            }

        if price_kopecks < 0:
            return {
                "success": False,
                "error": "Цена не может быть отрицательной"
            }

        url = (
            BASE_URL
            + f"UpdateOrder/{quote(str(classid), safe='')}/"
            + f"{quote(str(instanceid), safe='')}/"
            + f"{price_kopecks}/"
        )

        return self._request_url(
            "GET",
            url,
            retry=False
        )

    def cancel_variant_order(self, classid, instanceid):
        """Отменить ордер конкретного варианта."""

        return self.update_variant_order(
            classid=classid,
            instanceid=instanceid,
            price_kopecks=0
        )

    # ==================================================
    # V2 API — МОИ ОРДЕРА
    # ==================================================

    def get_my_orders(self, page=0):
        """Получить собственные активные ордера v2."""

        return self._request(
            "GET",
            "get-orders",
            {"page": int(page)}
        )

    def get_orders_log(self, page=0):
        """Получить историю исполненных ордеров v2."""

        return self._request(
            "GET",
            "get-orders-log",
            {"page": int(page)}
        )

    # ==================================================
    # V2 API — СОЗДАНИЕ / ОБНОВЛЕНИЕ ОРДЕРА
    # ==================================================

    def set_order(
        self,
        market_hash_name,
        count,
        price_kopecks,
        phase=None
    ):
        """
        Создать или обновить v2-ордер.

        count — количество предметов.
        price_kopecks — цена в копейках.

        Важно: v2 идентифицирует ордер по названию
        и phase, а не по classid + instanceid.
        """

        try:
            count = int(count)
            price_kopecks = int(price_kopecks)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Некорректное количество или цена"
            }

        if not market_hash_name:
            return {
                "success": False,
                "error": "Не указано название предмета"
            }

        if count < 0:
            return {
                "success": False,
                "error": "Количество не может быть отрицательным"
            }

        if count > 0 and price_kopecks <= 0:
            return {
                "success": False,
                "error": "Некорректная цена ордера"
            }

        params = {
            "market_hash_name": market_hash_name,
            "count": count,
            "price": price_kopecks
        }

        if phase:
            params["phase"] = phase

        return self._request(
            "GET",
            "set-order",
            params,
            retry=False
        )

    def cancel_order(self, market_hash_name, phase=None):
        """
        Удалить v2-ордер по названию и phase.

        Внимание: этот метод не различает classid/instanceid.
        """

        return self.set_order(
            market_hash_name=market_hash_name,
            count=0,
            price_kopecks=0,
            phase=phase
        )

    # ==================================================
    # ПРЯМАЯ ПОКУПКА ЛОТА
    # ==================================================

    def buy_item(
        self,
        listing_id,
        price_kopecks,
        custom_id=None
    ):
        """
        Купить конкретный лот.

        Один запрос, без автоматических повторов.
        Цена указывается в копейках.
        """

        if not listing_id:
            return {
                "success": False,
                "error": "Не указан уникальный ID лота"
            }

        try:
            price_kopecks = int(price_kopecks)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Некорректная цена"
            }

        if price_kopecks <= 0:
            return {
                "success": False,
                "error": "Некорректная цена"
            }

        custom_id = custom_id or uuid.uuid4().hex[:40]

        return self._request(
            "GET",
            "buy",
            {
                "id": str(listing_id),
                "price": price_kopecks,
                "custom_id": custom_id,
                "buy_alfaskin": 0
            },
            retry=False
        )

    # ==================================================
    # ПРОВЕРКА РЕЗУЛЬТАТА ПРЯМОЙ ПОКУПКИ
    # ==================================================

    def get_buy_info(self, custom_id):
        return self._request(
            "GET",
            "get-buy-info-by-custom-id",
            {"custom_id": custom_id},
            retry=True
        )

    # ==================================================
    # ЗАКРЫТИЕ СЕССИИ
    # ==================================================

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
