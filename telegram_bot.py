
import threading
import requests

from PySide6.QtCore import QObject, Signal


class TelegramBot(QObject):
    command_received = Signal(str)

    def __init__(self, token, chat_id):
        super().__init__()

        self.allowed_user_id = int(chat_id)
        self.token = token
        self.chat_id = int(chat_id)

        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

        self._stop_event = threading.Event()
        self._thread = None
        self._offset = 0

    def send_message(self, text):
        """Отправляет обычное сообщение в разрешённый чат."""
        try:
            response = self.session.post(
                f"{self.base_url}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": str(text)
                },
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                print("Ошибка Telegram:", result)

        except requests.RequestException as exc:
            print("Не удалось отправить сообщение Telegram:", exc)

    def send_autobuy_confirmation(self):
        """Отправляет подтверждение включения автопокупки."""

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "▶ Продолжить",
                        "callback_data": "autobuy_continue"
                    },
                    {
                        "text": "⛔ Отключить автопокупку",
                        "callback_data": "autobuy_cancel"
                    }
                ]
            ]
        }

        try:
            response = self.session.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": (
                        "⚠️ Запрошено включение автопокупки.\n\n"
                        "После нажатия «Продолжить» программа сможет "
                        "автоматически покупать подходящие предметы "
                        "по заданным настройкам.\n\n"
                        "Подтверди действие:"
                    ),
                    "reply_markup": keyboard
                },
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                print("Ошибка отправки кнопок Telegram:", result)

        except requests.RequestException as exc:
            print("Не удалось отправить кнопки Telegram:", exc)

    def send_scan_confirmation(self):
        """Отправляет подтверждение запуска сканирования с автопокупкой."""
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "▶ Продолжить",
                        "callback_data": "scan_continue"
                    },
                    {
                        "text": "❌ Отмена",
                        "callback_data": "scan_cancel"
                    }
                ]
            ]
        }

        try:
            response = self.session.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": (
                        "⚠️ Автопокупка включена.\n\n"
                        "При запуске сканирования программа сможет "
                        "автоматически совершать реальные покупки "
                        "подходящих предметов.\n\n"
                        "Запустить сканирование?"
                    ),
                    "reply_markup": keyboard
                },
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                print("Ошибка отправки подтверждения запуска:", result)

        except requests.RequestException as exc:
            print("Не удалось отправить подтверждение запуска:", exc)
    def answer_callback(self, callback_query_id, text):
        """Отвечает Telegram после нажатия кнопки."""
        try:
            response = self.session.post(
                f"{self.base_url}/answerCallbackQuery",
                data={
                    "callback_query_id": callback_query_id,
                    "text": text
                },
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                print("Ошибка ответа на кнопку:", result)

        except requests.RequestException as exc:
            print("Ошибка ответа на нажатие кнопки:", exc)

    def remove_inline_keyboard(self, chat_id, message_id):
        """Удаляет кнопки после нажатия."""
        try:
            response = self.session.post(
                f"{self.base_url}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {
                        "inline_keyboard": []
                    }
                },
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                print("Ошибка удаления кнопок Telegram:", result)

        except requests.RequestException as exc:
            print("Не удалось убрать кнопки Telegram:", exc)

    def start_polling(self):
        """Запускает получение команд и нажатий кнопок."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._poll_updates,
            daemon=True
        )
        self._thread.start()

    def stop_polling(self):
        """Останавливает получение обновлений."""
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6)

        if self._thread is None or not self._thread.is_alive():
            self.session.close()

    def _poll_updates(self):
        """Получает команды и callback от Telegram."""

        while not self._stop_event.is_set():
            try:
                response = self.session.get(
                    f"{self.base_url}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": 2,
                        "allowed_updates": '["message","callback_query"]'
                    },
                    timeout=5
                )
                response.raise_for_status()

                result = response.json()

                if not result.get("ok"):
                    print("Telegram вернул ошибку:", result)
                    self._stop_event.wait(3)
                    continue

                for update in result.get("result", []):

                    self._offset = max(
                        self._offset,
                        update["update_id"] + 1
                    )

                    # ==================================
                    # ОБРАБОТКА INLINE-КНОПОК
                    # ==================================

                    callback = update.get("callback_query")

                    if callback:
                        user = callback.get("from", {})
                        message = callback.get("message", {})
                        chat = message.get("chat", {})

                        # Проверка разрешённого пользователя
                        if (
                            chat.get("id") != self.chat_id
                            or user.get("id") != self.allowed_user_id
                        ):
                            self.answer_callback(
                                callback.get("id"),
                                "Нет доступа."
                            )
                            continue

                        callback_data = callback.get("data", "")

                        allowed_callbacks = {
                            "autobuy_continue",
                            "autobuy_cancel",
                            "scan_continue",
                            "scan_cancel"
                        }

                        if callback_data in allowed_callbacks:

                            print(
                                "Получено нажатие Telegram:",
                                callback_data
                            )

                            self.answer_callback(
                                callback.get("id"),
                                "Команда принята."
                            )

                            self.remove_inline_keyboard(
                                chat.get("id"),
                                message.get("message_id")
                            )

                            self.command_received.emit(
                                f"callback:{callback_data}"
                            )

                        continue

                    # ==================================
                    # ОБРАБОТКА ОБЫЧНЫХ КОМАНД
                    # ==================================

                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    user = message.get("from", {})

                    if (
                        chat.get("id") != self.chat_id
                        or user.get("id") != self.allowed_user_id
                    ):
                        continue

                    text = message.get("text", "").strip()

                    if not text.startswith("/"):
                        continue

                    command = text.split()[0].split("@")[0].lower()

                    allowed_commands = {
                        "/help",
                        "/status",
                        "/start",
                        "/stop",
                        "/pause",
                        "/resume",
                        "/autobuy_on",
                        "/autobuy_off"
                    }

                    if command in allowed_commands:
                        print("Получена команда Telegram:", command)
                        self.command_received.emit(command)

            except requests.RequestException as exc:
                if not self._stop_event.is_set():
                    print("Ошибка связи с Telegram:", exc)
                    self._stop_event.wait(3)

            except (ValueError, KeyError, TypeError) as exc:
                if not self._stop_event.is_set():
                    print("Ошибка обработки ответа Telegram:", exc)
                    self._stop_event.wait(3)
