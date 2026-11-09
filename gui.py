
import json
import logging
import time
import webbrowser
import uuid
import sys
import threading

from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtCore import QObject, Signal, QTimer, Qt, QThread
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QLineEdit,
    QMessageBox, QTabWidget, QDoubleSpinBox, QSpinBox, QDialog,
    QDialogButtonBox, QFileDialog, QCheckBox, QTextEdit, QHeaderView,
    QGroupBox, QProgressBar, QComboBox
)

import database
import settings
from parser import GemParser
from api import MarketAPI
from scanner import MarketScanner
from auto_orders import AutoOrderManager
from telegram_bot import TelegramBot


PREFERENCES_PATH = Path("data/gui_preferences.json")
LOG_PATH = Path("data/bot.log")


class ScannerSignal(QObject):
    update = Signal(dict)
    purchase_finished = Signal(dict)


class AutoOrderSignal(QObject):
    log_message = Signal(str)


class PurchaseWorker(QThread):
    finished_result = Signal(dict)

    def __init__(
        self,
        api,
        database_module,
        attempt_id,
        listing_id,
        price_kopecks,
        custom_id
    ):
        super().__init__()

        self.api = api
        self.database = database_module
        self.attempt_id = attempt_id
        self.listing_id = listing_id
        self.price_kopecks = price_kopecks
        self.custom_id = custom_id

    def run(self):
        result = {
            "attempt_id": self.attempt_id,
            "custom_id": self.custom_id,
            "status": "unknown",
            "error": None
        }

        try:
            response = self.api.buy_item(
                self.listing_id,
                self.price_kopecks,
                self.custom_id
            )

            if not isinstance(response, dict):
                status = "unknown"
                error = "Некорректный ответ API."

            elif response.get("request_error"):
                status = "unknown"
                error = response.get(
                    "error",
                    "Неизвестно, обработала ли биржа запрос."
                )

            elif response.get("success") is True:
                status = "pending"
                error = None

            elif response.get("success") is False:
                status = "failed"
                error = response.get(
                    "error",
                    "Биржа отклонила покупку."
                )

            else:
                status = "unknown"
                error = "Биржа вернула неоднозначный ответ."

            data = (
                response.get("data")
                if isinstance(response, dict)
                else None
            )

            api_item_id = (
                data.get("item_id")
                if isinstance(data, dict)
                else None
            )

            self.database.update_purchase_attempt(
                self.attempt_id,
                status,
                response=response,
                error=error,
                api_item_id=api_item_id
            )

            result.update({
                "status": status,
                "error": error,
                "response": response
            })

        except Exception as exc:
            error = str(exc)

            try:
                self.database.update_purchase_attempt(
                    self.attempt_id,
                    "unknown",
                    error=error
                )
            except Exception:
                logging.exception(
                    "Не удалось сохранить состояние попытки покупки"
                )

            result.update({
                "status": "unknown",
                "error": error
            })

        self.finished_result.emit(result)


class ConsoleStream(QObject):
    text_written = Signal(str, str)

    def __init__(self, stream_name):
        super().__init__()
        self.stream_name = stream_name
        self.buffer = ""
        self.lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0

        with self.lock:
            self.buffer += str(text)

            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                self.text_written.emit(
                    self.stream_name,
                    line.rstrip("\r")
                )

        return len(text)

    def flush(self):
        with self.lock:
            if self.buffer:
                self.text_written.emit(
                    self.stream_name,
                    self.buffer.rstrip("\r")
                )
                self.buffer = ""

    @property
    def encoding(self):
        return "utf-8"

    def isatty(self):
        return False

    def writable(self):
        return True


class MainWindow(QWidget):

    # ---------------------------------------------------------
    # Автоматическое обновление через GitHub Releases
    # ---------------------------------------------------------

    def check_for_updates(self, manual=False):
        try:
            from updater import check_latest_release, CURRENT_VERSION, version_is_newer

            release = check_latest_release()
            if not release:
                if manual:
                    QMessageBox.information(
                        self, "Обновления",
                        "Не удалось получить информацию о последнем релизе GitHub."
                    )
                return

            latest = str(release.get("tag_name", "")).lstrip("vV")
            if not latest or not version_is_newer(latest, CURRENT_VERSION):
                if manual:
                    QMessageBox.information(
                        self, "Обновления",
                        f"Установлена актуальная версия ({CURRENT_VERSION})."
                    )
                return

            scanner_active = self.scanner is not None and self.scanner.is_alive()
            orders_active = (
                self.auto_order_manager is not None
                and self.auto_order_manager.is_alive()
            )
            purchase_active = (
                self.purchase_worker is not None
                and self.purchase_worker.isRunning()
            )
            if scanner_active or orders_active or purchase_active:
                QMessageBox.warning(
                    self,
                    "Нельзя обновить сейчас",
                    "Перед обновлением останови сканер и автоордера "
                    "и дождись завершения покупки."
                )
                return

            answer = QMessageBox.question(
                self,
                "Доступно обновление",
                f"Доступна новая версия {latest} (текущая {CURRENT_VERSION}).\\n"
                "Скачать и установить обновление сейчас?\\n\\n"
                "Настройки, токены, база данных и история будут сохранены.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if answer != QMessageBox.Yes:
                return

            from updater import download_and_launch_update
            self.log(f"Загружается обновление {latest}...")
            ok, message = download_and_launch_update(release)
            if not ok:
                QMessageBox.warning(self, "Обновление", message)
                return

            QMessageBox.information(
                self, "Обновление",
                "Обновление загружено. Программа закроется и запустит установщик. "
                "После обновления открой Dota Market Bot снова."
            )
            self.close()

        except Exception as exc:
            self.log(f"Проверка обновлений не удалась: {exc}")
            if manual:
                QMessageBox.warning(self, "Обновления", str(exc))

    def check_for_updates_manually(self):
        self.check_for_updates(manual=True)

    # ---------------------------------------------------------
    # Telegram
    # ---------------------------------------------------------

    def handle_telegram_command(self, command):
        if not self.tg_bot:
            return

        if command == "callback:scan_continue":
            self.toggle_scan(from_telegram=True, confirmed=True)
            return

        if command == "callback:scan_cancel":
            self.tg_bot.send_message(
                "❌ Запуск сканирования отменён. "
                "Автопокупка осталась включённой."
            )
            return

        if command == "callback:autobuy_continue":
            self.config["auto_buy"] = True

            try:
                settings.save(self.config)

                if self.scanner and self.scanner.is_alive():
                    self.scanner.set_auto_buy_enabled(True)

                self.update_auto_buy_button()
                self.log("Автопокупка подтверждена через Telegram.")

                self.tg_bot.send_message(
                    "✅ Автопокупка включена.\n"
                    "Сканирование: "
                    + (
                        "работает."
                        if self.scanner and self.scanner.is_alive()
                        else "не запущено. Используй /start."
                    )
                )

            except Exception as exc:
                self.config["auto_buy"] = False
                self.update_auto_buy_button()
                self.log(f"Ошибка включения автопокупки: {exc}")
                self.tg_bot.send_message(
                    f"❌ Не удалось включить автопокупку: {exc}"
                )

            return

        if command == "callback:autobuy_cancel":
            self.disable_auto_buy_telegram()
            return

        if command == "/help":
            self.tg_bot.send_message(
                "Команды управления:\n"
                "/status — состояние программы\n"
                "/start — запустить сканирование\n"
                "/stop — остановить сканирование\n"
                "/pause — приостановить сканирование\n"
                "/resume — возобновить сканирование\n"
                "/autobuy_on — запросить включение автопокупки\n"
                "/autobuy_off — отключить автопокупку"
            )

        elif command == "/status":
            auto_order_running = (
                self.auto_order_manager is not None
                and self.auto_order_manager.is_alive()
            )

            self.tg_bot.send_message(
                f"Сканер: {'работает' if self.running else 'остановлен'}\n"
                f"Автопокупка: "
                f"{'включена' if self.config.get('auto_buy', False) else 'выключена'}\n"
                f"Автоордеры: {'работают' if auto_order_running else 'остановлены'}"
            )

        elif command in ("/start", "/resume"):
            if self.scanner and self.scanner.is_alive():
                self.tg_bot.send_message("Сканирование уже работает.")
                return

            self.toggle_scan(from_telegram=True)

        elif command in ("/stop", "/pause"):
            if self.scanner and self.scanner.is_alive():
                self.stop_scan()
                self.tg_bot.send_message(
                    "⏹ Остановка сканирования запрошена."
                )
            else:
                self.tg_bot.send_message(
                    "Сканирование уже остановлено."
                )

        elif command == "/autobuy_on":
            self.tg_bot.send_autobuy_confirmation()
            self.log(
                "Запрошено включение автопокупки через Telegram."
            )

        elif command == "/autobuy_off":
            self.disable_auto_buy_telegram()

    def disable_auto_buy_telegram(self):
        old_value = bool(self.config.get("auto_buy", False))
        self.config["auto_buy"] = False

        try:
            settings.save(self.config)

            if self.scanner and self.scanner.is_alive():
                self.scanner.set_auto_buy_enabled(False)

            self.update_auto_buy_button()
            self.log("Автопокупка отключена через Telegram.")

            self.tg_bot.send_message(
                "⛔ Автопокупка отключена.\n"
                "Сканирование при этом может продолжаться."
            )

        except Exception as exc:
            self.config["auto_buy"] = old_value
            self.update_auto_buy_button()
            self.log(f"Ошибка отключения автопокупки: {exc}")
            self.tg_bot.send_message(
                f"❌ Не удалось отключить автопокупку: {exc}"
            )

    # ---------------------------------------------------------
    # Инициализация
    # ---------------------------------------------------------

    def __init__(self, tg_bot=None):
        super().__init__()

        self.database = database
        self.tg_bot = None

        self.config = settings.load() or {}
        self.settings = dict(self.config)

        self.parser = GemParser()
        self.api = None

        api_key = self.config.get("api_key", "")
        if api_key:
            try:
                self.api = MarketAPI(api_key)
            except Exception:
                logging.exception(
                    "Не удалось инициализировать MarketAPI"
                )

        self.scanner = None
        self.running = False

        # Независимый менеджер автоордеров
        self.auto_order_manager = None
        self.auto_order_signal = AutoOrderSignal()
        self.auto_order_signal.log_message.connect(
            self.on_auto_order_log
        )

        self.found_items = {}
        self.last_seen = {}
        self.log_messages = []

        self.signal = ScannerSignal()
        self.signal.update.connect(self.update_found_items)
        self.signal.purchase_finished.connect(
            self.on_purchase_finished
        )

        self.purchase_worker = None

        self.preferences = self.load_preferences()

        self.setWindowTitle("Dota Market Bot")
        self.resize(1440, 950)
        self.setMinimumSize(1440, 950)
        self.setStyleSheet(self.dark_stylesheet())

        self.build_ui()

        if tg_bot is not None:
            self.tg_bot = tg_bot
            self.tg_bot.command_received.connect(self.handle_telegram_command)
            self.tg_bot.start_polling()
        elif self.config.get("telegram_enabled", False):
            self.start_telegram_bot()

        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

        self.console_stdout = ConsoleStream("stdout")
        self.console_stderr = ConsoleStream("stderr")

        self.console_stdout.text_written.connect(
            self.append_console_output
        )
        self.console_stderr.text_written.connect(
            self.append_console_output
        )

        sys.stdout = self.console_stdout
        sys.stderr = self.console_stderr

        self.console_file_lock = threading.Lock()

        self.refresh_all()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(
            self.periodic_refresh
        )
        self.refresh_timer.start(1500)

        # Проверка обновлений выполняется после показа окна и не блокирует запуск.
        QTimer.singleShot(1800, self.check_for_updates)

        self.log("Интерфейс запущен.")

        if self.api is None:
            self.log(
                "Не найден API key в настройках. "
                "Запросы к рынку недоступны."
            )

    # ---------------------------------------------------------
    # Настройки интерфейса
    # ---------------------------------------------------------

    def load_preferences(self):
        defaults = {
            "total_budget": 0.0,
            "scan_interval": int(
                self.config.get("scan_interval", 1000)
            ),
            "auto_order_step": float(
                self.config.get("auto_order_step", 1.0)
            ),
            "auto_order_interval": int(
                self.config.get("auto_order_interval", 15)
            ),
            "auto_order_allow_cancel": bool(
                self.config.get("auto_order_allow_cancel", False)
            )
        }

        try:
            if PREFERENCES_PATH.exists():
                data = json.loads(
                    PREFERENCES_PATH.read_text(
                        encoding="utf-8"
                    )
                )
                defaults.update(data)
        except Exception:
            logging.exception(
                "Не удалось прочитать настройки интерфейса"
            )

        return defaults

    def save_preferences(self):
        try:
            PREFERENCES_PATH.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            PREFERENCES_PATH.write_text(
                json.dumps(
                    self.preferences,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )

        except Exception as exc:
            self.log(
                f"Ошибка сохранения настроек: {exc}"
            )

    # ---------------------------------------------------------
    # Журнал
    # ---------------------------------------------------------

    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"

        self.log_messages.append(line)
        self.log_messages = self.log_messages[-1000:]

        try:
            LOG_PATH.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            with LOG_PATH.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except Exception:
            pass

        if hasattr(self, "logs_view"):
            self.logs_view.append(line)

        if hasattr(self, "recent_logs"):
            self.recent_logs.append(line)

            document = self.recent_logs.document()

            while document.blockCount() > 30:
                cursor = self.recent_logs.textCursor()
                cursor.movePosition(
                    QTextCursor.MoveOperation.Start
                )
                cursor.select(
                    QTextCursor.SelectionType.BlockUnderCursor
                )
                cursor.removeSelectedText()
                cursor.deleteChar()

    def append_console_output(self, stream_name, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        if stream_name == "stderr":
            line = f"[{timestamp}] [ОШИБКА] {message}"
        else:
            line = f"[{timestamp}] [КОНСОЛЬ] {message}"

        if hasattr(self, "logs_view"):
            self.logs_view.append(line)

        try:
            LOG_PATH.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            with self.console_file_lock:
                with LOG_PATH.open(
                    "a",
                    encoding="utf-8"
                ) as file:
                    file.write(line + "\n")
        except Exception:
            pass

    def on_auto_order_log(self, message):
        self.log(f"[АВТООРДЕРА] {message}")

    # ---------------------------------------------------------
    # Оформление
    # ---------------------------------------------------------

    def dark_stylesheet(self):
        return """
        QWidget {
            background: #171a21;
            color: #e7eaf0;
            font-family: Segoe UI, Arial;
            font-size: 10pt;
        }

        QTabWidget::pane {
            border: 1px solid #303744;
            border-radius: 8px;
            background: #1d222c;
        }

        QTabBar::tab {
            background: #222936;
            color: #aeb8c8;
            padding: 11px 18px;
            margin-right: 4px;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
        }

        QTabBar::tab:selected {
            background: #303b4d;
            color: #ffffff;
            border-bottom: 2px solid #54c78a;
        }

        QLineEdit, QDoubleSpinBox, QSpinBox,
        QTextEdit, QTableWidget {
            background: #11151c;
            border: 1px solid #343d4b;
            border-radius: 6px;
            padding: 7px;
            selection-background-color: #347b60;
        }

        QHeaderView::section {
            background: #293241;
            color: #dce3ed;
            border: none;
            padding: 8px;
            font-weight: 600;
        }

        QPushButton {
            background: #303b4d;
            border: 1px solid #465368;
            border-radius: 6px;
            padding: 8px 14px;
        }

        QPushButton:hover {
            background: #3b4960;
        }

        QPushButton:disabled {
            color: #707987;
            background: #242a34;
            border-color: #303744;
        }

        QGroupBox {
            border: 1px solid #343d4b;
            border-radius: 8px;
            margin-top: 12px;
            padding: 12px;
            font-weight: 600;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 5px;
            color: #9fb0c7;
        }

        QProgressBar {
            background: #11151c;
            border: 1px solid #343d4b;
            border-radius: 6px;
            text-align: center;
            min-height: 18px;
        }

        QProgressBar::chunk {
            background: #39a875;
            border-radius: 5px;
        }

        QCheckBox {
            spacing: 8px;
        }
        """

    # ---------------------------------------------------------
    # Построение интерфейса
    # ---------------------------------------------------------

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()

        title = QLabel("DOTA MARKET BOT")
        title.setStyleSheet(
            "font-size: 21pt; font-weight: 800; letter-spacing: 1px;"
        )

        subtitle = QLabel(
            "Мониторинг рынка и управление списком закупки"
        )
        subtitle.setStyleSheet("color: #8f9bad;")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header.addLayout(title_box)
        header.addStretch()

        self.status_badge = QLabel("● Остановлен")
        self.status_badge.setStyleSheet(
            "background:#332b2d;color:#ff9b9b;"
            "padding:8px 13px;border-radius:8px;font-weight:700;"
        )

        header.addWidget(self.status_badge)
        root.addLayout(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.build_dashboard_tab()
        self.build_items_tab()
        self.build_found_tab()
        self.build_history_tab()
        self.build_pending_tab()
        self.build_logs_tab()
        self.build_settings_tab()

        footer = QHBoxLayout()

        self.footer_status = QLabel("Готов.")
        self.footer_status.setStyleSheet(
            "color:#8f9bad;"
        )

        footer.addWidget(self.footer_status)
        footer.addStretch()

        root.addLayout(footer)

    def make_stat_card(self, caption, value):
        box = QGroupBox()
        box.setMinimumHeight(95)

        layout = QVBoxLayout(box)

        value_label = QLabel(value)
        value_label.setStyleSheet(
            "font-size:23pt;font-weight:800;color:#65d69b;"
        )

        caption_label = QLabel(caption)
        caption_label.setStyleSheet(
            "color:#9aa7b8;"
        )

        layout.addWidget(value_label)
        layout.addWidget(caption_label)

        box.value_label = value_label
        return box

    # ---------------------------------------------------------
    # Главная
    # ---------------------------------------------------------

    def build_dashboard_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        cards = QGridLayout()

        self.card_items = self.make_stat_card(
            "Позиций в списке", "0"
        )
        self.card_remaining = self.make_stat_card(
            "Осталось купить", "0"
        )
        self.card_purchased = self.make_stat_card(
            "Куплено", "0"
        )
        self.card_found = self.make_stat_card(
            "Найдено лотов", "0"
        )

        cards.addWidget(self.card_items, 0, 0)
        cards.addWidget(self.card_remaining, 0, 1)
        cards.addWidget(self.card_purchased, 0, 2)
        cards.addWidget(self.card_found, 0, 3)

        layout.addLayout(cards)

        controls = QGroupBox("Управление сканированием")
        control_layout = QHBoxLayout(controls)

        self.start_button = QPushButton(
            "▶  Запустить сканирование"
        )
        self.start_button.setStyleSheet(
            "QPushButton{background:#24764f;"
            "border-color:#319968;font-weight:700;padding:11px;}"
        )
        self.start_button.clicked.connect(self.toggle_scan)

        self.stop_button = QPushButton("■  Остановить")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_scan)

        self.auto_buy_button = QPushButton()
        self.auto_buy_button.setCheckable(True)
        self.auto_buy_button.setMinimumWidth(190)
        self.auto_buy_button.clicked.connect(
            self.toggle_auto_buy
        )

        self.update_auto_buy_button()

        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addStretch()
        control_layout.addWidget(self.auto_buy_button)

        layout.addWidget(controls)

        # Отдельное управление автоордерами
        order_group = QGroupBox("Управление автоордерами")
        order_layout = QHBoxLayout(order_group)

        self.start_orders_button = QPushButton(
            "▶  Запустить автоордера"
        )
        self.start_orders_button.setStyleSheet(
            "QPushButton{background:#285c83;"
            "border-color:#397eae;font-weight:700;padding:11px;}"
        )
        self.start_orders_button.clicked.connect(
            self.start_auto_orders
        )

        self.stop_orders_button = QPushButton(
            "■  Остановить автоордера"
        )
        self.stop_orders_button.setEnabled(False)
        self.stop_orders_button.clicked.connect(
            self.stop_auto_orders
        )

        self.auto_order_status = QLabel(
            "● Автоордера остановлены"
        )
        self.auto_order_status.setStyleSheet(
            "color:#ff9b9b;font-weight:700;"
        )

        order_layout.addWidget(self.start_orders_button)
        order_layout.addWidget(self.stop_orders_button)
        order_layout.addStretch()
        order_layout.addWidget(self.auto_order_status)

        layout.addWidget(order_group)

        progress_group = QGroupBox(
            "Прогресс по количеству"
        )
        progress_layout = QVBoxLayout(progress_group)

        self.total_progress_label = QLabel(
            "Пока нет позиций с количеством."
        )

        self.total_progress = QProgressBar()
        self.total_progress.setRange(0, 100)
        self.total_progress.setValue(0)

        progress_layout.addWidget(
            self.total_progress_label
        )
        progress_layout.addWidget(
            self.total_progress
        )

        layout.addWidget(progress_group)

        recent_group = QGroupBox("Последние события")
        recent_layout = QVBoxLayout(recent_group)

        self.recent_logs = QTextEdit()
        self.recent_logs.setReadOnly(True)
        self.recent_logs.setMaximumHeight(190)

        recent_layout.addWidget(self.recent_logs)
        layout.addWidget(recent_group)

        layout.addStretch()

        self.tabs.addTab(tab, "⌂  Главная")

    # ---------------------------------------------------------
    # Таблица закупки
    # ---------------------------------------------------------

    def build_items_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        form_group = QGroupBox("Добавить позицию")
        form = QGridLayout(form_group)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(
            "Название предмета"
        )

        self.price_input = QDoubleSpinBox()
        self.price_input.setRange(
            0.01,
            1_000_000_000
        )
        self.price_input.setDecimals(2)
        self.price_input.setSuffix(" ₽")

        self.quantity_input = QSpinBox()
        self.quantity_input.setRange(1, 1_000_000)
        self.quantity_input.setValue(1)

        self.gem_input = QLineEdit()
        self.gem_input.setPlaceholderText(
            "Необязательно; несколько через запятую"
        )

        form.addWidget(QLabel("Название"), 0, 0)
        form.addWidget(self.name_input, 0, 1)
        form.addWidget(QLabel("Макс. цена"), 0, 2)
        form.addWidget(self.price_input, 0, 3)

        form.addWidget(QLabel("Количество"), 1, 0)
        form.addWidget(self.quantity_input, 1, 1)
        form.addWidget(QLabel("Гем"), 1, 2)
        form.addWidget(self.gem_input, 1, 3)

        layout.addWidget(form_group)

        actions = QHBoxLayout()

        add_button = QPushButton("＋ Добавить")
        add_button.clicked.connect(self.add_item)

        import_button = QPushButton("Импорт Excel")
        import_button.clicked.connect(self.import_excel)

        select_all_button = QPushButton("Выбрать все")
        select_all_button.clicked.connect(
            self.db_table_select_all
        )

        bulk_edit_button = QPushButton(
            "Массово изменить"
        )
        bulk_edit_button.clicked.connect(
            self.bulk_edit_items
        )

        edit_button = QPushButton("Редактировать одну")
        edit_button.clicked.connect(self.edit_item)

        delete_button = QPushButton(
            "Удалить выбранные"
        )
        delete_button.clicked.connect(
            self.delete_selected_items
        )

        delete_all_button = QPushButton(
            "Очистить список"
        )
        delete_all_button.clicked.connect(
            self.delete_all_items
        )

        refresh_button = QPushButton("Обновить")
        refresh_button.clicked.connect(
            self.load_database_items
        )

        actions.addWidget(add_button)
        actions.addWidget(import_button)
        actions.addStretch()
        actions.addWidget(select_all_button)
        actions.addWidget(bulk_edit_button)
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        actions.addWidget(delete_all_button)
        actions.addWidget(refresh_button)

        layout.addLayout(actions)

        self.db_table = QTableWidget(0, 9)
        self.db_table.setHorizontalHeaderLabels([
            "Название",
            "Цена",
            "Задано",
            "Куплено",
            "Осталось",
            "В ожидании",
            "Участвует в сканировании",
            "Гем",
            "ID"
        ])

        self.configure_table(self.db_table)

        self.db_table.setSelectionBehavior(
            QTableWidget.SelectRows
        )
        self.db_table.setSelectionMode(
            QTableWidget.ExtendedSelection
        )
        self.db_table.setColumnHidden(8, True)

        layout.addWidget(self.db_table)

        note = QLabel(
            "Выбирай несколько строк через Ctrl или Shift. "
            "Пустое поле «Гем» отключает фильтр по гемам."
        )
        note.setStyleSheet("color:#91a0b4;")
        layout.addWidget(note)

        self.tabs.addTab(tab, "▤  Закупка")

    def db_table_select_all(self):
        self.db_table.selectAll()

    # ---------------------------------------------------------
    # Найденные лоты
    # ---------------------------------------------------------

    def build_found_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel(
            "Лоты, найденные сканером. "
            "Ручная покупка доступна при работающем сканере."
        ))

        self.found_table = QTableWidget(0, 6)
        self.found_table.setHorizontalHeaderLabels([
            "Предмет",
            "Цена",
            "Гем",
            "Заявка",
            "Ссылка",
            "Действие"
        ])

        self.configure_table(self.found_table)
        layout.addWidget(self.found_table)

        self.tabs.addTab(tab, "⌕  Найденные лоты")

    # ---------------------------------------------------------
    # История
    # ---------------------------------------------------------

    def build_history_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        top = QHBoxLayout()
        top.addWidget(QLabel(
            "История подтверждённых покупок"
        ))
        top.addStretch()

        refresh = QPushButton("Обновить историю")
        refresh.clicked.connect(self.load_history)

        top.addWidget(refresh)
        layout.addLayout(top)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels([
            "Предмет",
            "Цена",
            "ID объявления",
            "Дата покупки"
        ])

        self.configure_table(self.history_table)
        layout.addWidget(self.history_table)

        self.tabs.addTab(tab, "◷  История")

    # ---------------------------------------------------------
    # Незавершённые покупки
    # ---------------------------------------------------------

    def build_pending_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        info = QLabel(
            "Здесь отображаются попытки покупки, которые ещё "
            "не завершены. Отмена меняет только локальную запись "
            "в базе и освобождает резерв количества/бюджета. "
            "Перед отменой обязательно проверь, что покупка "
            "действительно отменена на маркете."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#e5b45f;")
        layout.addWidget(info)

        actions = QHBoxLayout()

        refresh_button = QPushButton("Обновить")
        refresh_button.clicked.connect(self.refresh_pending_attempts)

        self.cancel_attempt_button = QPushButton(
            "Отменить выбранные попытки"
        )
        self.cancel_attempt_button.setStyleSheet(
            "QPushButton{background:#713b42;"
            "border-color:#a45460;font-weight:700;}"
        )
        self.cancel_attempt_button.clicked.connect(
            self.cancel_selected_attempt
        )

        actions.addWidget(refresh_button)
        actions.addWidget(self.cancel_attempt_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.pending_table = QTableWidget(0, 7)
        self.pending_table.setHorizontalHeaderLabels([
            "Предмет",
            "Цена",
            "ID лота",
            "Статус",
            "Создана",
            "Обновлена",
            "ID попытки"
        ])

        self.configure_table(self.pending_table)

        # Разрешаем выбирать несколько строк через Ctrl и Shift.
        self.pending_table.setSelectionBehavior(
            QTableWidget.SelectRows
        )
        self.pending_table.setSelectionMode(
            QTableWidget.ExtendedSelection
        )

        self.pending_table.setColumnHidden(6, True)

        layout.addWidget(self.pending_table)

        self.tabs.addTab(tab, "◷  Ожидающие покупки")

    def refresh_pending_attempts(self):
        if not hasattr(self, "pending_table"):
            return

        try:
            attempts = self.database.get_pending_purchase_attempts()

            self.pending_table.setRowCount(0)

            status_names = {
                "submitting": "Отправляется",
                "pending": "Ожидает подтверждения",
                "checking": "Проверяется",
                "unknown": "Результат неизвестен",
                "ready_to_confirm": "Готова к подтверждению"
            }

            for attempt in attempts:
                row = self.pending_table.rowCount()
                self.pending_table.insertRow(row)

                values = [
                    str(attempt.get("item_name", "")),
                    self.format_price(attempt.get("price", 0)),
                    str(attempt.get("market_listing_id", "—")),
                    status_names.get(
                        attempt.get("status"),
                        str(attempt.get("status", ""))
                    ),
                    str(attempt.get("created_at", "")),
                    str(attempt.get("updated_at", "")),
                    str(attempt.get("id", ""))
                ]

                for column, value in enumerate(values):
                    self.pending_table.setItem(
                        row,
                        column,
                        QTableWidgetItem(value)
                    )

        except Exception as exc:
            self.log(
                f"Не удалось обновить список ожидающих покупок: {exc}"
            )

    def cancel_selected_attempt(self):
        selected_rows = (
            self.pending_table.selectionModel().selectedRows()
        )

        if not selected_rows:
            QMessageBox.information(
                self,
                "Отмена попыток",
                "Сначала выбери одну или несколько попыток."
            )
            return

        selected_attempts = []

        for index in selected_rows:
            row = index.row()

            id_cell = self.pending_table.item(row, 6)
            status_cell = self.pending_table.item(row, 3)
            name_cell = self.pending_table.item(row, 0)

            if not id_cell or not status_cell:
                continue

            try:
                attempt_id = int(id_cell.text())
            except (ValueError, TypeError):
                continue

            status = status_cell.text()
            name = name_cell.text() if name_cell else ""

            # Эти статусы нельзя отменять через интерфейс.
            if status in (
                    "Отправляется",
                    "Готова к подтверждению"
            ):
                continue

            selected_attempts.append({
                "id": attempt_id,
                "name": name,
                "status": status
            })

        if not selected_attempts:
            QMessageBox.warning(
                self,
                "Отмена недоступна",
                "Среди выбранных строк нет попыток, "
                "которые можно отменить."
            )
            return

        skipped = len(selected_rows) - len(selected_attempts)

        names = "\n".join(
            f"• {item['name']} (ID: {item['id']})"
            for item in selected_attempts
        )

        message = (
            f"Отменить локально {len(selected_attempts)} попыток?\n\n"
            f"{names}\n\n"
            "Это действие НЕ отменяет покупки на маркете. "
            "Оно только снимает локальный резерв количества и бюджета.\n\n"
            "Продолжай, только если лично проверил, "
            "что эти покупки действительно отменены на маркете."
        )

        if skipped:
            message += (
                f"\n\nПропущено попыток с неподходящим статусом: "
                f"{skipped}."
            )

        answer = QMessageBox.question(
            self,
            "Подтверждение отмены",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if answer != QMessageBox.Yes:
            return

        try:
            cancel_func = getattr(
                self.database,
                "cancel_purchase_attempt",
                None
            )

            if cancel_func is None:
                raise RuntimeError(
                    "В database.py отсутствует функция "
                    "cancel_purchase_attempt()."
                )

            cancelled = 0
            failed = 0

            for item in selected_attempts:
                try:
                    success = cancel_func(
                        item["id"],
                        "Покупка вручную проверена и отменена на маркете"
                    )

                    if success:
                        cancelled += 1
                        self.log(
                            f"Попытка покупки ID {item['id']} отменена. "
                            "Локальный резерв освобождён."
                        )
                    else:
                        failed += 1
                        self.log(
                            f"Не удалось отменить попытку ID {item['id']}."
                        )

                except Exception as exc:
                    failed += 1
                    self.log(
                        f"Ошибка отмены попытки {item['id']}: {exc}"
                    )

            self.refresh_all()

            self.log(
                f"Массовая отмена завершена: "
                f"успешно {cancelled}, ошибок {failed}."
            )

            QMessageBox.information(
                self,
                "Результат отмены",
                f"Успешно отменено: {cancelled}\n"
                f"Не удалось отменить: {failed}"
            )

        except Exception as exc:
            self.log(f"Ошибка массовой отмены: {exc}")

            QMessageBox.critical(
                self,
                "Ошибка",
                str(exc)
            )

    # ---------------------------------------------------------
    # Журнал
    # ---------------------------------------------------------

    def build_logs_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        bar = QHBoxLayout()
        bar.addWidget(
            QLabel("События, предупреждения и ошибки")
        )
        bar.addStretch()

        clear_button = QPushButton("Очистить окно")
        clear_button.clicked.connect(
            lambda: self.logs_view.clear()
        )

        bar.addWidget(clear_button)
        layout.addLayout(bar)

        self.logs_view = QTextEdit()
        self.logs_view.setReadOnly(True)

        layout.addWidget(self.logs_view)
        self.tabs.addTab(tab, "☷  Журнал")

    def start_telegram_bot(self):
        token = str(self.config.get("telegram_token", "")).strip()
        chat_id = str(self.config.get("telegram_chat_id", "")).strip()
        if not token or not chat_id:
            self.tg_bot = None
            if hasattr(self, "telegram_status"):
                self.telegram_status.setText("Telegram включён в настройках, но не заданы токен и Chat ID.")
            self.log("Telegram не запущен: укажи токен и Chat ID в настройках.")
            return False
        try:
            bot = TelegramBot(token=token, chat_id=int(chat_id))
            bot.command_received.connect(self.handle_telegram_command)
            bot.start_polling()
            self.tg_bot = bot
            if hasattr(self, "telegram_status"):
                self.telegram_status.setText("Telegram-бот запущен.")
            self.log("Telegram-бот запущен.")
            return True
        except Exception as exc:
            self.tg_bot = None
            if hasattr(self, "telegram_status"):
                self.telegram_status.setText(f"Ошибка запуска Telegram: {exc}")
            self.log(f"Не удалось запустить Telegram-бота: {exc}")
            return False

    def stop_telegram_bot(self):
        bot = self.tg_bot
        self.tg_bot = None
        if bot:
            try:
                bot.stop_polling()
            except Exception:
                logging.exception("Ошибка остановки Telegram polling")
        if hasattr(self, "telegram_status"):
            self.telegram_status.setText("Telegram выключен.")
        self.log("Telegram-бот остановлен.")

    # ---------------------------------------------------------
    # Настройки
    # ---------------------------------------------------------

    def build_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        api_group = QGroupBox("API Market Dota2")
        api_layout = QVBoxLayout(api_group)

        api_form = QFormLayout()

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText(
            "Введи новый API-ключ"
        )
        self.api_key_input.setEchoMode(
            QLineEdit.Password
        )
        self.api_key_input.setText(
            str(self.config.get("api_key", ""))
        )

        self.show_api_key_checkbox = QCheckBox(
            "Показать API-ключ"
        )
        self.show_api_key_checkbox.toggled.connect(
            lambda checked:
            self.api_key_input.setEchoMode(
                QLineEdit.Normal
                if checked
                else QLineEdit.Password
            )
        )

        api_form.addRow(
            "API-ключ:",
            self.api_key_input
        )

        api_layout.addLayout(api_form)
        api_layout.addWidget(
            self.show_api_key_checkbox
        )

        api_hint = QLabel(
            "Новый ключ будет сохранён и использован "
            "при следующем запуске запросов."
        )
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet(
            "color:#91a0b4;"
        )

        api_layout.addWidget(api_hint)
        layout.addWidget(api_group)

        budget_group = QGroupBox("Бюджет")
        budget_form = QFormLayout(budget_group)

        self.budget_input = QDoubleSpinBox()
        self.budget_input.setRange(
            0,
            1_000_000_000
        )
        self.budget_input.setDecimals(2)
        self.budget_input.setSuffix(" ₽")
        self.budget_input.setSpecialValueText(
            "Без ограничения"
        )
        self.budget_input.setValue(
            float(
                self.preferences.get(
                    "total_budget",
                    0
                )
            )
        )

        budget_form.addRow(
            "Общий бюджет:",
            self.budget_input
        )
        layout.addWidget(budget_group)

        scan_group = QGroupBox("Сканирование")
        scan_form = QFormLayout(scan_group)

        self.interval_input = QSpinBox()
        self.interval_input.setRange(
            100,
            3_600_000
        )
        self.interval_input.setSuffix(" мс")
        self.interval_input.setValue(
            int(
                self.preferences.get(
                    "scan_interval",
                    1000
                )
            )
        )

        scan_form.addRow(
            "Интервал между проходами:",
            self.interval_input
        )
        layout.addWidget(scan_group)

        # Настройки автоордеров
        order_group = QGroupBox("Автоордера")
        order_form = QFormLayout(order_group)

        self.auto_order_step_input = QDoubleSpinBox()
        self.auto_order_step_input.setRange(
            0.01,
            1_000_000
        )
        self.auto_order_step_input.setDecimals(2)
        self.auto_order_step_input.setSuffix(" ₽")
        self.auto_order_step_input.setValue(
            float(
                self.preferences.get(
                    "auto_order_step",
                    self.config.get(
                        "auto_order_step",
                        1.0
                    )
                )
            )
        )

        self.auto_order_interval_input = QSpinBox()
        self.auto_order_interval_input.setRange(15, 3600)
        self.auto_order_interval_input.setSuffix(" сек")
        self.auto_order_interval_input.setValue(
            max(
                15,
                int(
                    self.preferences.get(
                        "auto_order_interval",
                        self.config.get(
                            "auto_order_interval",
                            15
                        )
                    )
                )
            )
        )

        self.auto_order_cancel_checkbox = QCheckBox(
            "Разрешить отмену активных ордеров"
        )
        self.auto_order_cancel_checkbox.setChecked(
            bool(
                self.preferences.get(
                    "auto_order_allow_cancel",
                    False
                )
            )
        )

        cancel_hint = QLabel(
            "Оставь выключенным, пока не проверишь "
            "на реальном API способ отмены ордера. "
            "Автоматическая отмена может зависеть от поведения биржи."
        )
        cancel_hint.setWordWrap(True)
        cancel_hint.setStyleSheet(
            "color:#e5b45f;"
        )

        order_form.addRow(
            "Шаг перебивания цены:",
            self.auto_order_step_input
        )
        order_form.addRow(
            "Интервал проверки:",
            self.auto_order_interval_input
        )
        order_form.addRow(
            "",
            self.auto_order_cancel_checkbox
        )
        order_form.addRow("", cancel_hint)

        layout.addWidget(order_group)

        telegram_group = QGroupBox("Telegram")
        telegram_layout = QVBoxLayout(telegram_group)

        self.telegram_enabled_checkbox = QCheckBox("Использовать Telegram-бота")
        self.telegram_enabled_checkbox.setChecked(
            bool(self.config.get("telegram_enabled", False))
        )
        telegram_layout.addWidget(self.telegram_enabled_checkbox)

        telegram_form = QFormLayout()
        self.telegram_token_input = QLineEdit(
            str(self.config.get("telegram_token", ""))
        )
        self.telegram_token_input.setEchoMode(QLineEdit.Password)
        self.telegram_token_input.setPlaceholderText("Токен от BotFather")
        self.show_telegram_token_checkbox = QCheckBox("Показать токен Telegram")
        self.show_telegram_token_checkbox.toggled.connect(
            lambda checked: self.telegram_token_input.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        self.telegram_chat_id_input = QLineEdit(
            str(self.config.get("telegram_chat_id", ""))
        )
        self.telegram_chat_id_input.setPlaceholderText("Ваш Telegram chat_id")
        telegram_form.addRow("Токен:", self.telegram_token_input)
        telegram_form.addRow("Chat ID:", self.telegram_chat_id_input)
        telegram_layout.addLayout(telegram_form)
        telegram_layout.addWidget(self.show_telegram_token_checkbox)

        self.telegram_status = QLabel(
            "Telegram подключён." if self.tg_bot else "Telegram выключен."
        )
        telegram_layout.addWidget(self.telegram_status)

        telegram_hint = QLabel(
            "Переключатель действует после нажатия «Сохранить настройки». "
            "Для безопасности вставь новый токен, старый токен из архива нужно отозвать."
        )
        telegram_hint.setWordWrap(True)
        telegram_hint.setStyleSheet("color:#91a0b4;")
        telegram_layout.addWidget(telegram_hint)
        layout.addWidget(telegram_group)

        auto_group = QGroupBox("Автопокупка")
        auto_layout = QVBoxLayout(auto_group)

        auto_text = QLabel(
            "Автопокупка сканера управляется отдельной "
            "кнопкой на главной вкладке."
        )
        auto_text.setWordWrap(True)
        auto_text.setStyleSheet(
            "color:#e5b45f;"
        )

        auto_layout.addWidget(auto_text)
        layout.addWidget(auto_group)

        update_button = QPushButton("Проверить обновления")
        update_button.clicked.connect(self.check_for_updates_manually)
        layout.addWidget(update_button)

        save_button = QPushButton(
            "Сохранить настройки"
        )
        save_button.clicked.connect(
            self.save_settings
        )

        layout.addWidget(save_button)
        layout.addStretch()

        self.tabs.addTab(tab, "⚙  Настройки")

    # ---------------------------------------------------------
    # Настройка таблиц
    # ---------------------------------------------------------

    def configure_table(self, table):
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(
            QTableWidget.SelectRows
        )
        table.setSelectionMode(
            QTableWidget.SingleSelection
        )
        table.setEditTriggers(
            QTableWidget.NoEditTriggers
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        table.horizontalHeader().setStretchLastSection(
            True
        )

    # ---------------------------------------------------------
    # Работа с позициями
    # ---------------------------------------------------------

    def add_item(self):
        name = self.name_input.text().strip()
        gem = self.gem_input.text().strip()
        price = self.price_input.value()
        quantity = self.quantity_input.value()

        if not name:
            QMessageBox.warning(
                self,
                "Проверка",
                "Укажи название предмета."
            )
            return

        try:
            self.database.add_item(
                name,
                price,
                gem,
                quantity
            )

            self.log(
                f"Добавлена позиция: {name}."
            )

            self.name_input.clear()
            self.gem_input.clear()
            self.price_input.setValue(0.01)
            self.quantity_input.setValue(1)

            self.refresh_all()

        except Exception as exc:
            self.log(
                f"Ошибка добавления: {exc}"
            )
            QMessageBox.critical(
                self,
                "Ошибка базы данных",
                str(exc)
            )

    def selected_item_ids(self):
        rows = (
            self.db_table
            .selectionModel()
            .selectedRows()
        )

        result = []

        for index in rows:
            cell = self.db_table.item(
                index.row(),
                8
            )

            if cell:
                result.append(int(cell.text()))

        return result

    def selected_item_id(self):
        ids = self.selected_item_ids()
        return ids[0] if ids else None

    def edit_item(self):
        item_id = self.selected_item_id()

        if item_id is None:
            QMessageBox.information(
                self,
                "Редактирование",
                "Выбери позицию в таблице."
            )
            return

        item = self.database.get_item_by_id(
            item_id
        )

        if not item:
            QMessageBox.warning(
                self,
                "Редактирование",
                "Позиция больше не существует."
            )
            self.refresh_all()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(
            "Редактировать позицию"
        )
        form = QFormLayout(dialog)

        name = QLineEdit(
            str(item["name"])
        )

        price = QDoubleSpinBox()
        price.setRange(
            0.01,
            1_000_000_000
        )
        price.setDecimals(2)
        price.setValue(
            float(item["max_price"])
        )

        quantity = QSpinBox()
        quantity.setRange(1, 1_000_000)
        quantity.setValue(
            int(item.get("quantity", 1))
        )

        gem = QLineEdit(
            str(item.get("gem") or "")
        )

        active = QCheckBox(
            "Участвует в сканировании"
        )
        active.setChecked(
            bool(item.get("is_active", 1))
        )

        form.addRow("Название:", name)
        form.addRow("Максимальная цена:", price)
        form.addRow("Общее количество:", quantity)
        form.addRow("Гем:", gem)
        form.addRow("", active)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save |
            QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        try:
            self.database.update_item(
                item_id,
                name.text().strip(),
                price.value(),
                gem.text().strip(),
                quantity.value()
            )

            if hasattr(
                self.database,
                "set_item_active"
            ):
                self.database.set_item_active(
                    item_id,
                    active.isChecked()
                )

            self.log(
                f"Обновлена позиция ID {item_id}."
            )
            self.refresh_all()

        except Exception as exc:
            QMessageBox.critical(
                self,
                "Ошибка",
                str(exc)
            )
            self.log(
                f"Ошибка редактирования: {exc}"
            )

    def bulk_edit_items(self):
        item_ids = self.selected_item_ids()

        if not item_ids:
            QMessageBox.information(
                self,
                "Массовое редактирование",
                "Выбери несколько строк через Ctrl или Shift."
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"Массовое изменение: {len(item_ids)} позиций"
        )

        form = QFormLayout(dialog)

        price = QDoubleSpinBox()
        price.setRange(0, 1_000_000_000)
        price.setDecimals(2)
        price.setSpecialValueText("Не менять")
        price.setValue(0)

        quantity = QSpinBox()
        quantity.setRange(0, 1_000_000)
        quantity.setSpecialValueText("Не менять")
        quantity.setValue(0)

        gem = QLineEdit()
        gem.setPlaceholderText("Пусто — не менять")

        clear_gem = QCheckBox(
            "Очистить гем у выбранных позиций"
        )

        active = QComboBox()
        active.addItem("Не менять", "unchanged")
        active.addItem("Да — участвует", "active")
        active.addItem("Нет — не участвует", "inactive")

        form.addRow("Новая максимальная цена:", price)
        form.addRow("Новое количество:", quantity)
        form.addRow("Новый гем:", gem)
        form.addRow("", clear_gem)
        form.addRow(
            "Участие в сканировании:",
            active
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save |
            QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        active_mode = active.currentData()

        if (
            price.value() == 0
            and quantity.value() == 0
            and not gem.text().strip()
            and not clear_gem.isChecked()
            and active_mode == "unchanged"
        ):
            QMessageBox.information(
                self,
                "Изменение",
                "Не указано ни одного изменения."
            )
            return

        changed = 0

        try:
            for item_id in item_ids:
                item = self.database.get_item_by_id(
                    item_id
                )

                if not item:
                    continue

                new_price = (
                    price.value()
                    if price.value() > 0
                    else float(item["max_price"])
                )

                new_quantity = (
                    quantity.value()
                    if quantity.value() > 0
                    else int(item["quantity"])
                )

                if clear_gem.isChecked():
                    new_gem = ""
                elif gem.text().strip():
                    new_gem = gem.text().strip()
                else:
                    new_gem = str(
                        item.get("gem") or ""
                    )

                self.database.update_item(
                    item_id,
                    item["name"],
                    new_price,
                    new_gem,
                    new_quantity
                )

                if active_mode != "unchanged":
                    self.database.set_item_active(
                        item_id,
                        active_mode == "active"
                    )

                changed += 1

            self.log(
                f"Массово изменено позиций: {changed}."
            )

            self.refresh_all()

            QMessageBox.information(
                self,
                "Готово",
                f"Изменено позиций: {changed}."
            )

        except Exception as exc:
            self.log(
                f"Ошибка массового изменения: {exc}"
            )
            QMessageBox.critical(
                self,
                "Ошибка",
                str(exc)
            )

    def delete_item(self):
        self.delete_selected_items()

    def delete_selected_items(self):
        item_ids = self.selected_item_ids()

        if not item_ids:
            QMessageBox.information(
                self,
                "Удаление",
                "Сначала выдели позиции в таблице."
            )
            return

        answer = QMessageBox.question(
            self,
            "Удаление позиций",
            f"Удалить выбранные позиции: {len(item_ids)} шт.?"
        )

        if answer != QMessageBox.Yes:
            return

        deleted = 0

        try:
            for item_id in item_ids:
                self.database.remove_item(item_id)
                deleted += 1

            self.log(
                f"Удалено позиций: {deleted}."
            )
            self.refresh_all()

            QMessageBox.information(
                self,
                "Готово",
                f"Удалено позиций: {deleted}."
            )

        except Exception as exc:
            self.log(
                f"Ошибка массового удаления: {exc}"
            )
            QMessageBox.critical(
                self,
                "Ошибка",
                str(exc)
            )

    def delete_all_items(self):
        answer = QMessageBox.question(
            self,
            "Очистить список",
            "Удалить ВСЕ позиции из списка закупки?"
        )

        if answer != QMessageBox.Yes:
            return

        try:
            items = self.database.get_all_items()

            for item in items:
                self.database.remove_item(
                    int(item["id"])
                )

            self.log(
                "Список очищен. "
                f"Удалено позиций: {len(items)}."
            )
            self.refresh_all()

        except Exception as exc:
            self.log(
                f"Ошибка очистки списка: {exc}"
            )
            QMessageBox.critical(
                self,
                "Ошибка",
                str(exc)
            )

    def load_database_items(self):
        items = self.database.get_all_items()

        self.db_table.setRowCount(0)

        for item in items:
            row = self.db_table.rowCount()
            self.db_table.insertRow(row)

            quantity = int(
                item.get("quantity", 0)
            )
            purchased = int(
                item.get("purchased_quantity", 0)
            )

            pending = self.database.get_pending_count(
                int(item["id"])
            )

            # В остатке учитываются уже купленные
            # и находящиеся в процессе покупки предметы.
            remaining = max(
                0,
                quantity - purchased - pending
            )

            is_active = bool(
                item.get("is_active", 1)
            )

            values = [
                str(item.get("name", "")),
                self.format_price(
                    item.get("max_price", 0)
                ),
                str(quantity),
                str(purchased),
                str(remaining),
                str(pending),
                "Да" if is_active else "Нет",
                str(item.get("gem") or "—"),
                str(item.get("id", "")),
            ]

            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)

                if column in (2, 3, 4, 5, 6):
                    cell.setTextAlignment(
                        Qt.AlignCenter
                    )

                self.db_table.setItem(
                    row,
                    column,
                    cell
                )

        self.last_database_refresh = time.monotonic()

    # ---------------------------------------------------------
    # Импорт Excel
    # ---------------------------------------------------------

    def import_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт предметов из Excel",
            "",
            "Excel Files (*.xlsx *.xls)"
        )

        if not file_path:
            return

        try:
            from openpyxl import load_workbook

            workbook = load_workbook(
                file_path,
                read_only=True,
                data_only=True
            )

            sheet = workbook.active

            imported = 0
            skipped = 0
            errors = []

            try:
                existing = self.database.get_all_items()

                for row_number, row in enumerate(
                    sheet.iter_rows(
                        min_row=1,
                        min_col=1,
                        max_col=4,
                        values_only=True
                    ),
                    start=1
                ):
                    name, price, quantity, gem = row

                    if all(value is None for value in row):
                        continue

                    if str(name).strip().lower() in (
                        "название",
                        "название предмета",
                        "предмет",
                        "name",
                        "item"
                    ):
                        continue

                    try:
                        name = str(name).strip()

                        if not name:
                            skipped += 1
                            continue

                        price = float(price)
                        quantity = int(quantity)

                        gem_value = (
                            str(gem).strip()
                            if gem is not None
                            else ""
                        )

                        if price <= 0 or quantity <= 0:
                            raise ValueError(
                                "Цена и количество должны быть больше нуля"
                            )

                        gems = [
                            value.strip()
                            for value in gem_value.split(",")
                            if value.strip()
                        ] if gem_value else [""]

                        for current_gem in gems:
                            duplicate = any(
                                str(
                                    item.get("name", "")
                                ).strip().lower() == name.lower()
                                and str(
                                    item.get("gem") or ""
                                ).strip().lower()
                                == current_gem.lower()
                                for item in existing
                            )

                            if duplicate:
                                skipped += 1
                                continue

                            self.database.add_item(
                                name=name,
                                max_price=price,
                                quantity=quantity,
                                gem=current_gem
                            )

                            imported += 1

                            existing.append({
                                "name": name,
                                "gem": current_gem
                            })

                    except Exception as exc:
                        skipped += 1
                        errors.append(
                            f"Строка {row_number}: {exc}"
                        )

            finally:
                workbook.close()

            self.refresh_all()

            message = (
                "Импорт завершён!\n\n"
                f"Добавлено: {imported}\n"
                f"Пропущено: {skipped}"
            )

            if errors:
                message += (
                    "\n\nПервые ошибки:\n"
                    + "\n".join(errors[:10])
                )

            QMessageBox.information(
                self,
                "Импорт Excel",
                message
            )

        except Exception as exc:
            QMessageBox.critical(
                self,
                "Ошибка импорта",
                f"Не удалось импортировать Excel:\n{exc}"
            )

    @staticmethod
    def normalize_header(value):
        return " ".join(
            str(value or "")
            .strip()
            .lower()
            .replace("_", " ")
            .split()
        )

    # ---------------------------------------------------------
    # Управление сканером
    # ---------------------------------------------------------

    def toggle_scan(
        self,
        from_telegram=False,
        confirmed=False
    ):
        if self.scanner and self.scanner.is_alive():
            if from_telegram:
                self.tg_bot.send_message(
                    "Сканирование уже работает."
                )
            else:
                self.stop_scan()
            return

        if (
            self.config.get("auto_buy", False)
            and not confirmed
        ):
            if from_telegram:
                self.tg_bot.send_scan_confirmation()
                self.log(
                    "Ожидается подтверждение запуска "
                    "сканера в Telegram."
                )
                return

            answer = QMessageBox.warning(
                self,
                "Автопокупка включена",
                "Автопокупка включена и при запуске сканера "
                "может совершать реальные покупки.\n\n"
                "Ты проверил настройки и готов продолжить?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if answer != QMessageBox.Yes:
                return

        if self.api is None:
            message = (
                "Не найден api_key в настройках."
            )

            if from_telegram:
                self.tg_bot.send_message(
                    "❌ " + message
                )
            else:
                QMessageBox.warning(
                    self,
                    "API не настроен",
                    message
                )

            return

        try:
            self.database.init()

        except Exception as exc:
            self.log(
                f"Ошибка инициализации базы: {exc}"
            )

            if from_telegram:
                self.tg_bot.send_message(
                    f"❌ Ошибка базы данных: {exc}"
                )
            else:
                QMessageBox.critical(
                    self,
                    "База данных",
                    str(exc)
                )

            return

        scanner_settings = dict(self.settings)

        scanner_settings["auto_buy"] = bool(
            self.config.get("auto_buy", False)
        )

        scanner_settings["scan_interval"] = int(
            self.preferences.get(
                "scan_interval",
                1000
            )
        )

        scanner_settings["total_budget"] = float(
            self.preferences.get(
                "total_budget",
                0
            ) or 0
        )

        self.scanner = MarketScanner(
            self.api,
            self.database,
            self.parser,
            self.signal.update.emit,
            scanner_settings
        )

        self.running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.status_badge.setText(
            "● Сканирование"
        )
        self.status_badge.setStyleSheet(
            "background:#203c31;color:#79e0a8;"
            "padding:8px 13px;border-radius:8px;font-weight:700;"
        )

        self.footer_status.setText(
            "Сканирование активно."
        )

        self.log(
            "Запущено сканирование активных позиций."
        )

        self.scanner.start()

        if from_telegram:
            self.tg_bot.send_message(
                "✅ Сканирование запущено.\n"
                f"Автопокупка: "
                f"{'включена' if scanner_settings['auto_buy'] else 'выключена'}."
            )

    def stop_scan(self):
        if (
            self.scanner
            and self.scanner.is_alive()
        ):
            self.scanner.stop()

            self.running = False
            self.stop_button.setEnabled(False)
            self.start_button.setEnabled(False)

            self.status_badge.setText(
                "● Остановка..."
            )
            self.footer_status.setText(
                "Ожидание завершения API-запросов."
            )

            self.log(
                "Запрошена остановка сканера."
            )

        else:
            self.running = False
            self.set_scanner_stopped()

    def set_scanner_stopped(self):
        self.running = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        self.status_badge.setText(
            "● Остановлен"
        )
        self.status_badge.setStyleSheet(
            "background:#332b2d;color:#ff9b9b;"
            "padding:8px 13px;border-radius:8px;font-weight:700;"
        )

        self.footer_status.setText(
            "Сканер остановлен."
        )

    # ---------------------------------------------------------
    # Управление автоордерами
    # ---------------------------------------------------------

    def start_auto_orders(self):
        if self.api is None:
            QMessageBox.warning(
                self,
                "API не настроен",
                "Сначала укажи API-ключ и сохрани настройки."
            )
            return

        if (
            self.auto_order_manager is not None
            and self.auto_order_manager.is_alive()
        ):
            self.log(
                "Менеджер автоордеров уже работает."
            )
            return

        try:
            self.database.init()

            order_settings = dict(self.config)
            order_settings.update(self.preferences)

            order_settings["total_budget"] = float(
                self.preferences.get(
                    "total_budget",
                    0
                ) or 0
            )

            order_settings["auto_order_step"] = float(
                self.auto_order_step_input.value()
            )

            order_settings["auto_order_interval"] = max(
                15,
                int(
                    self.auto_order_interval_input.value()
                )
            )

            order_settings["auto_order_allow_cancel"] = (
                self.auto_order_cancel_checkbox.isChecked()
            )

            self.auto_order_manager = AutoOrderManager(
                api=self.api,
                database=self.database,
                parser=self.parser,
                settings=order_settings,
                callback=self.auto_order_signal.log_message.emit
            )

            self.auto_order_manager.start()

            self.start_orders_button.setEnabled(False)
            self.stop_orders_button.setEnabled(True)

            self.auto_order_status.setText(
                "● Автоордера работают"
            )
            self.auto_order_status.setStyleSheet(
                "color:#79e0a8;font-weight:700;"
            )

            self.log(
                "Запущен независимый поток управления автоордерами."
            )

            self.footer_status.setText(
                "Менеджер автоордеров работает независимо от сканера."
            )

        except Exception as exc:
            self.log(
                f"Не удалось запустить автоордера: {exc}"
            )
            QMessageBox.critical(
                self,
                "Ошибка запуска автоордеров",
                str(exc)
            )

    def stop_auto_orders(self):
        manager = self.auto_order_manager

        if manager is None:
            self.set_auto_orders_stopped()
            return

        if manager.is_alive():
            try:
                manager.stop()
            except Exception as exc:
                self.log(
                    f"Ошибка отправки команды остановки автоордеров: {exc}"
                )
                return

            self.stop_orders_button.setEnabled(False)
            self.start_orders_button.setEnabled(False)

            self.auto_order_status.setText(
                "● Остановка автоордеров..."
            )
            self.auto_order_status.setStyleSheet(
                "color:#e5b45f;font-weight:700;"
            )

            self.log(
                "Отправлена команда остановки автоордеров."
            )

        else:
            self.set_auto_orders_stopped()

    def set_auto_orders_stopped(self):
        self.start_orders_button.setEnabled(True)
        self.stop_orders_button.setEnabled(False)

        self.auto_order_status.setText(
            "● Автоордера остановлены"
        )
        self.auto_order_status.setStyleSheet(
            "color:#ff9b9b;font-weight:700;"
        )

    # ---------------------------------------------------------
    # Автопокупка сканера
    # ---------------------------------------------------------

    def update_auto_buy_button(self):
        enabled = bool(
            self.config.get("auto_buy", False)
        )

        self.auto_buy_button.blockSignals(True)
        self.auto_buy_button.setChecked(enabled)
        self.auto_buy_button.blockSignals(False)

        if enabled:
            self.auto_buy_button.setText(
                "Автопокупка: ВКЛ"
            )
            self.auto_buy_button.setStyleSheet(
                "background:#24764f;"
                "border:1px solid #319968;"
                "color:white;"
                "font-weight:700;"
                "padding:10px;"
            )
        else:
            self.auto_buy_button.setText(
                "Автопокупка: ВЫКЛ"
            )
            self.auto_buy_button.setStyleSheet(
                "background:#493337;"
                "border:1px solid #87515a;"
                "color:white;"
                "font-weight:700;"
                "padding:10px;"
            )

    def toggle_auto_buy(self, checked):
        old_value = bool(
            self.config.get("auto_buy", False)
        )

        self.config["auto_buy"] = bool(checked)

        try:
            settings.save(self.config)

            if (
                self.scanner
                and self.scanner.is_alive()
            ):
                self.scanner.set_auto_buy_enabled(
                    checked
                )

            self.update_auto_buy_button()

            status = (
                "включена"
                if checked
                else "отключена"
            )

            self.log(
                f"Автопокупка {status} через интерфейс."
            )

            if self.tg_bot:
                self.tg_bot.send_message(
                    f"Автопокупка {status} через программу."
                )

        except Exception as exc:
            self.config["auto_buy"] = old_value
            self.update_auto_buy_button()

            QMessageBox.critical(
                self,
                "Ошибка автопокупки",
                str(exc)
            )

    # ---------------------------------------------------------
    # Найденные лоты
    # ---------------------------------------------------------

    def update_found_items(self, items):
        print(
            "GUI ПОЛУЧИЛО ЛОТЫ:",
            len(items)
        )

        now = time.time()

        for item_id, item in items.items():
            old_item = self.found_items.get(item_id)

            self.found_items[item_id] = dict(item)
            self.last_seen[item_id] = now

            if old_item is None:
                self.log(
                    f"Найден лот: {item.get('name', '')} — "
                    f"{self.format_price(item.get('price', 0))}."
                )
            else:
                old_price = old_item.get("price")
                new_price = item.get("price")

                if old_price != new_price:
                    self.log(
                        f"Изменилась цена: {item.get('name', '')} — "
                        f"{self.format_price(old_price)} → "
                        f"{self.format_price(new_price)}."
                    )

        self.refresh_found_table()
        self.refresh_stats()

    def refresh_found_table(self):
        self.found_table.setRowCount(0)

        for item_id, item in self.found_items.items():
            row = self.found_table.rowCount()
            self.found_table.insertRow(row)

            values = [
                item.get("name", ""),
                self.format_price(
                    item.get("price", 0)
                ),
                item.get("gem") or "—",
                str(item.get("target_id", "")),
            ]

            for column, value in enumerate(values):
                self.found_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(str(value))
                )

            link_button = QPushButton(
                "Открыть лот"
            )
            link_button.clicked.connect(
                lambda checked=False,
                link=item.get("link"):
                self.open_link(link)
            )

            self.found_table.setCellWidget(
                row,
                4,
                link_button
            )

            buy_button = QPushButton("Купить")
            buy_button.setStyleSheet(
                "QPushButton{background:#24764f;"
                "border-color:#319968;font-weight:700;}"
            )

            price_allowed = item.get(
                "can_buy",
                True
            )

            buy_button.setEnabled(
                self.api is not None
                and self.scanner is not None
                and self.scanner.is_alive()
                and self.purchase_worker is None
                and price_allowed
            )

            buy_button.clicked.connect(
                lambda checked=False,
                current_item=dict(item):
                self.purchase_item(current_item)
            )

            self.found_table.setCellWidget(
                row,
                5,
                buy_button
            )

    # ---------------------------------------------------------
    # Ручная покупка
    # ---------------------------------------------------------

    def purchase_item(self, item):
        if self.api is None:
            QMessageBox.warning(
                self,
                "Покупка",
                "API не настроен."
            )
            return

        if (
            not self.scanner
            or not self.scanner.is_alive()
        ):
            QMessageBox.warning(
                self,
                "Сканирование остановлено",
                "Для покупки сначала запусти сканирование."
            )
            return

        if self.purchase_worker is not None:
            QMessageBox.information(
                self,
                "Покупка",
                "Дождись завершения предыдущего запроса."
            )
            return

        listing_id = (
            item.get("listing_id")
            or item.get("id")
        )
        price_kopecks = item.get(
            "price_kopecks"
        )
        target_id = item.get("target_id")

        if (
            not listing_id
            or not target_id
            or not price_kopecks
        ):
            QMessageBox.warning(
                self,
                "Недостаточно данных",
                "У лота отсутствует ID, цена или ID заявки."
            )
            return

        try:
            price_kopecks = int(price_kopecks)
            price = price_kopecks / 100

            if price_kopecks <= 0:
                raise ValueError(
                    "Некорректная цена лота."
                )

            tracked_item = self.database.get_item_by_id(
                int(target_id)
            )

            if not tracked_item:
                raise ValueError(
                    "Заявка больше не существует."
                )

            if (
                not bool(tracked_item["is_active"])
                or bool(tracked_item["is_deleted"])
            ):
                raise ValueError(
                    "Заявка выключена или удалена."
                )

            # Учитываем ожидающие покупки.
            pending_count = self.database.get_pending_count(
                int(target_id)
            )

            remaining = max(
                0,
                int(tracked_item.get("quantity", 0))
                - int(
                    tracked_item.get(
                        "purchased_quantity",
                        0
                    )
                )
                - int(pending_count)
            )

            if remaining <= 0:
                raise ValueError(
                    "Количество по заявке уже исчерпано "
                    "или ожидает подтверждения."
                )

            if price > float(
                tracked_item["max_price"]
            ):
                raise ValueError(
                    "Цена лота превышает максимальную цену заявки."
                )

            budget = float(
                self.preferences.get(
                    "total_budget",
                    0
                )
            )

            if budget > 0:
                used = self.database.get_budget_usage()

                if used + price > budget:
                    raise ValueError(
                        "Недостаточно бюджета.\n"
                        f"Бюджет: {self.format_price(budget)}\n"
                        f"Использовано и зарезервировано: "
                        f"{self.format_price(used)}\n"
                        f"Цена лота: {self.format_price(price)}"
                    )

            answer = QMessageBox.question(
                self,
                "Подтверждение покупки",
                (
                    f"Купить предмет?\n\n"
                    f"Название: {item.get('name', '')}\n"
                    f"Цена: {self.format_price(price)}\n"
                    f"ID лота: {listing_id}\n\n"
                    "Биржа спишет средства при успешной покупке."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if answer != QMessageBox.Yes:
                return

            custom_id = uuid.uuid4().hex

            attempt_id = (
                self.database.create_purchase_attempt(
                    int(target_id),
                    str(
                        item.get(
                            "name",
                            tracked_item["name"]
                        )
                    ),
                    str(listing_id),
                    custom_id,
                    price,
                    price_kopecks
                )
            )

            if attempt_id is None:
                QMessageBox.warning(
                    self,
                    "Покупка не начата",
                    "Не удалось зарезервировать покупку. "
                    "Возможно, этот лот уже обрабатывается, "
                    "количество исчерпано или заявка изменилась."
                )
                return

            self.purchase_worker = PurchaseWorker(
                self.api,
                self.database,
                attempt_id,
                str(listing_id),
                price_kopecks,
                custom_id
            )

            self.purchase_worker.finished_result.connect(
                self.on_purchase_finished
            )
            self.purchase_worker.finished.connect(
                self.on_purchase_thread_stopped
            )

            self.log(
                f"Отправляется запрос покупки: "
                f"{item.get('name', '')}, "
                f"{self.format_price(price)}, "
                f"лот {listing_id}."
            )

            self.footer_status.setText(
                "Отправляется запрос покупки..."
            )

            self.refresh_found_table()
            self.purchase_worker.start()

        except Exception as exc:
            self.log(
                f"Покупка не начата: {exc}"
            )
            QMessageBox.warning(
                self,
                "Покупка не начата",
                str(exc)
            )

    def on_purchase_finished(self, result):
        status = result.get("status")
        error = result.get("error")

        if status == "pending":
            self.log(
                "Биржа приняла запрос покупки. "
                "Ожидается подтверждение получения предмета."
            )

        elif status == "failed":
            self.log(
                f"Биржа отклонила покупку: "
                f"{error or 'Причина не указана'}"
            )

        elif status == "unknown":
            self.log(
                "Результат покупки пока неизвестен. "
                "Повторный запрос не отправляется. "
                f"Причина: {error or 'Ожидается проверка биржи'}"
            )

        else:
            self.log(
                f"Получен неизвестный статус покупки: {status}"
            )

        self.load_history()
        self.refresh_stats()

    def on_purchase_thread_stopped(self):
        self.purchase_worker = None

        self.refresh_found_table()
        self.refresh_stats()
        self.load_history()

        if (
            self.scanner
            and self.scanner.is_alive()
        ):
            self.footer_status.setText(
                "Сканирование активно. "
                "Проверка результата покупки продолжается."
            )
        else:
            self.footer_status.setText(
                "Запрос покупки завершён. "
                "Проверка результата продолжается."
            )

    def open_link(self, link):
        if link:
            webbrowser.open(link)

    # ---------------------------------------------------------
    # История
    # ---------------------------------------------------------

    def load_history(self):
        self.history_table.setRowCount(0)

        if not hasattr(
            self.database,
            "get_purchase_history"
        ):
            return

        try:
            history = (
                self.database.get_purchase_history()
            )

            for entry in history:
                row = self.history_table.rowCount()
                self.history_table.insertRow(row)

                values = [
                    str(
                        entry.get("item_name", "")
                    ),
                    self.format_price(
                        entry.get("price", 0)
                    ),
                    str(
                        entry.get(
                            "market_listing_id"
                        ) or "—"
                    ),
                    str(
                        entry.get("purchased_at", "")
                    ),
                ]

                for column, value in enumerate(values):
                    self.history_table.setItem(
                        row,
                        column,
                        QTableWidgetItem(value)
                    )

        except Exception as exc:
            self.log(
                f"Не удалось загрузить историю: {exc}"
            )

    # ---------------------------------------------------------
    # Сохранение настроек
    # ---------------------------------------------------------

    def save_settings(self):
        new_api_key = (
            self.api_key_input.text().strip()
        )
        old_api_key = str(
            self.config.get("api_key", "")
        )

        scanner_active = (
            self.scanner is not None
            and self.scanner.is_alive()
        )

        orders_active = (
            self.auto_order_manager is not None
            and self.auto_order_manager.is_alive()
        )

        purchase_active = (
            self.purchase_worker is not None
            and self.purchase_worker.isRunning()
        )

        # Менять API-клиент во время работы потоков нельзя.
        if new_api_key != old_api_key:
            if scanner_active or orders_active:
                QMessageBox.warning(
                    self,
                    "Потоки работают",
                    "Сначала останови сканер и автоордера, "
                    "затем меняй API-ключ."
                )
                return

            if purchase_active:
                QMessageBox.warning(
                    self,
                    "Покупка выполняется",
                    "Дождись завершения запроса покупки."
                )
                return

        old_preferences = dict(self.preferences)
        old_config = dict(self.config)

        self.preferences["total_budget"] = (
            self.budget_input.value()
        )
        self.preferences["scan_interval"] = (
            self.interval_input.value()
        )
        self.preferences["auto_order_step"] = (
            self.auto_order_step_input.value()
        )
        self.preferences["auto_order_interval"] = (
            max(
                15,
                self.auto_order_interval_input.value()
            )
        )
        self.preferences["auto_order_allow_cancel"] = (
            self.auto_order_cancel_checkbox.isChecked()
        )

        self.config["auto_order_step"] = (
            self.preferences["auto_order_step"]
        )
        self.config["auto_order_interval"] = (
            self.preferences["auto_order_interval"]
        )
        self.config["auto_order_allow_cancel"] = (
            self.preferences["auto_order_allow_cancel"]
        )

        self.settings["scan_interval"] = (
            self.interval_input.value()
        )

        self.config["api_key"] = new_api_key
        self.config["telegram_enabled"] = self.telegram_enabled_checkbox.isChecked()
        self.config["telegram_token"] = self.telegram_token_input.text().strip()
        self.config["telegram_chat_id"] = self.telegram_chat_id_input.text().strip()

        try:
            settings.save(self.config)
            self.save_preferences()

            # Пересоздаём API только при фактической смене ключа.
            if new_api_key != old_api_key:
                if new_api_key:
                    self.api = MarketAPI(new_api_key)
                    self.log("API-ключ обновлён.")
                else:
                    self.api = None
                    self.log(
                        "API-ключ удалён. "
                        "Запросы к рынку недоступны."
                    )

            # Telegram включается/выключается независимо от сканера.
            if self.config["telegram_enabled"]:
                if self.tg_bot:
                    self.stop_telegram_bot()
                self.start_telegram_bot()
            else:
                self.stop_telegram_bot()

        except Exception as exc:
            self.preferences = old_preferences
            self.config = old_config
            self.api_key_input.setText(old_api_key)

            self.log(
                f"Ошибка сохранения настроек: {exc}"
            )

            QMessageBox.critical(
                self,
                "Ошибка",
                f"Не удалось сохранить настройки:\n{exc}"
            )
            return

        if scanner_active:
            self.scanner.total_budget = float(
                self.preferences.get(
                    "total_budget",
                    0
                ) or 0
            )

            self.scanner.interval = max(
                1.0,
                float(
                    self.preferences.get(
                        "scan_interval",
                        1000
                    )
                ) / 1000.0
            )

        self.log(
            "Настройки интерфейса сохранены."
        )

        QMessageBox.information(
            self,
            "Настройки",
            "Настройки сохранены."
        )

    # ---------------------------------------------------------
    # Статистика
    # ---------------------------------------------------------

    def refresh_stats(self):
        items = self.database.get_all_items()

        count_items = len(items)

        remaining = 0
        purchased = 0

        for item in items:
            item_id = int(item["id"])

            item_purchased = int(
                item.get("purchased_quantity", 0)
            )

            pending = self.database.get_pending_count(
                item_id
            )

            item_remaining = max(
                0,
                int(item.get("quantity", 0))
                - item_purchased
                - int(pending)
            )

            remaining += item_remaining
            purchased += item_purchased

        self.card_items.value_label.setText(
            str(count_items)
        )

        self.card_remaining.value_label.setText(
            str(remaining)
        )

        self.card_purchased.value_label.setText(
            str(purchased)
        )

        self.card_found.value_label.setText(
            str(len(self.found_items))
        )

        total = remaining + purchased

        done_percent = (
            int(purchased * 100 / total)
            if total
            else 0
        )

        self.total_progress.setValue(
            done_percent
        )

        if total:
            self.total_progress_label.setText(
                f"Выполнено {purchased} из {total} шт. "
                f"({done_percent}%)"
            )
        else:
            self.total_progress_label.setText(
                "Пока нет позиций с количеством."
            )

    def refresh_all(self):
        self.load_database_items()
        self.load_history()
        self.refresh_pending_attempts()
        self.refresh_stats()

    # ---------------------------------------------------------
    # Периодическое обновление интерфейса
    # ---------------------------------------------------------

    def periodic_refresh(self):
        if (
            self.scanner
            and not self.scanner.is_alive()
            and (
                self.running
                or not self.start_button.isEnabled()
            )
        ):
            self.set_scanner_stopped()
            self.log(
                "Поток сканера завершился."
            )

        manager = self.auto_order_manager

        if (
            manager is not None
            and not manager.is_alive()
            and not self.start_orders_button.isEnabled()
        ):
            self.set_auto_orders_stopped()
            self.log(
                "Поток автоордеров завершился."
            )

        self.refresh_stats()

        scanner_active = (
            self.scanner is not None
            and self.scanner.is_alive()
        )

        orders_active = (
            self.auto_order_manager is not None
            and self.auto_order_manager.is_alive()
        )

        if scanner_active or orders_active:
            self.load_database_items()

        elif (
            time.monotonic()
            - getattr(
                self,
                "last_database_refresh",
                0
            )
            >= 360
        ):
            self.load_database_items()

    # ---------------------------------------------------------
    # Вспомогательные функции
    # ---------------------------------------------------------

    @staticmethod
    def format_price(price):
        try:
            return (
                f"{float(price):,.2f} ₽"
                .replace(",", " ")
            )
        except (TypeError, ValueError):
            return "—"

    # ---------------------------------------------------------
    # Закрытие приложения
    # ---------------------------------------------------------

    def closeEvent(self, event):
        scanner_active = (
            self.scanner is not None
            and self.scanner.is_alive()
        )

        orders_active = (
            self.auto_order_manager is not None
            and self.auto_order_manager.is_alive()
        )

        if scanner_active or orders_active:
            answer = QMessageBox.question(
                self,
                "Выход",
                "Сканер или автоордера ещё работают. "
                "Остановить их и закрыть программу?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if answer != QMessageBox.Yes:
                event.ignore()
                return

        # Останавливаем сканер.
        if (
            self.scanner
            and self.scanner.is_alive()
        ):
            self.scanner.stop()
            self.scanner.join(5)

            if self.scanner.is_alive():
                QMessageBox.warning(
                    self,
                    "Сканер ещё работает",
                    "Поток сканера пока не завершился. "
                    "Подожди и попробуй закрыть окно снова."
                )
                event.ignore()
                return

        # Останавливаем автоордера независимо от сканера.
        if (
            self.auto_order_manager
            and self.auto_order_manager.is_alive()
        ):
            self.auto_order_manager.stop()
            self.auto_order_manager.join(5)

            if self.auto_order_manager.is_alive():
                QMessageBox.warning(
                    self,
                    "Автоордера ещё работают",
                    "Менеджер автоордеров пока не завершился. "
                    "Подожди и попробуй закрыть окно снова."
                )
                event.ignore()
                return

        # Не закрываем приложение во время отправки покупки.
        if (
            self.purchase_worker is not None
            and self.purchase_worker.isRunning()
        ):
            QMessageBox.warning(
                self,
                "Покупка выполняется",
                "Дождись завершения запроса покупки, "
                "затем закрой программу."
            )
            event.ignore()
            return

        self.stop_telegram_bot()

        self.save_preferences()

        if hasattr(self, "console_stdout"):
            self.console_stdout.flush()

        if hasattr(self, "console_stderr"):
            self.console_stderr.flush()

        if hasattr(self, "original_stdout"):
            sys.stdout = self.original_stdout

        if hasattr(self, "original_stderr"):
            sys.stderr = self.original_stderr

        event.accept()
