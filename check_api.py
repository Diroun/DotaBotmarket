
import json
import pprint

from api import MarketAPI


def print_response(title, response):
    print(f"\n========== {title} ==========")
    pprint.pp(response, sort_dicts=False, width=120)


def get_rows(response):
    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        return []

    for key in ("data", "items", "orders", "result"):
        value = response.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for nested_key in ("items", "orders", "data"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested

    return []


def get_all_pages(method, title):
    all_rows = []

    for page in range(100):
        try:
            response = method(page=page)
        except Exception as error:
            print(f"Ошибка {title}, страница {page}: {error}")
            break

        print_response(f"{title} — СТРАНИЦА {page}", response)

        if not isinstance(response, dict):
            break

        if response.get("success") is not True:
            break

        rows = get_rows(response)
        all_rows.extend(rows)

        if len(rows) < 100:
            break

    print(f"\nВсего записей {title}: {len(all_rows)}")
    return all_rows


def main():
    with open("data/config.json", "r", encoding="utf-8") as file:
        config = json.load(file)

    api_key = config.get("api_key")

    if not api_key:
        print("Не найден api_key в config.json")
        return

    api = MarketAPI(api_key)

    # 1. Собственные активные ордера
    orders = get_all_pages(
        api.get_my_orders,
        "МОИ ОРДЕРА"
    )

    # 2. История исполнений
    fills = get_all_pages(
        api.get_orders_log,
        "ИСТОРИЯ ИСПОЛНЕНИЙ"
    )

    # 3. Поиск предмета и BuyOffers
    name = input(
        "\nВведи точное название предмета для проверки BuyOffers: "
    ).strip()

    if name:
        try:
            result = api.search_item(name)
            print_response("ПОИСК ПРЕДМЕТА", result)

            rows = get_rows(result)
            item = next(
                (
                    row for row in rows
                    if isinstance(row, dict)
                    and (row.get("classid") or row.get("class"))
                    and (row.get("instanceid") or row.get("instance"))
                ),
                None
            )

            if item:
                classid = item.get("classid") or item.get("class")
                instanceid = (
                    item.get("instanceid") or item.get("instance")
                )

                print("\nНайдены идентификаторы:")
                print("classid:", classid)
                print("instanceid:", instanceid)

                offers = api.get_buy_orders(classid, instanceid)
                print_response("BUY OFFERS", offers)
            else:
                print("Не удалось найти classid/instanceid.")
                print("\n========== BUY OFFERS ПО CLASSID/INSTANCEID ==========")

                offers = api.get_buy_orders(
                    "5873037694",
                    "9129617465"
                )

                pprint.pp(offers, sort_dicts=False)
        except Exception as error:
            print("Ошибка проверки предмета:", error)

    print("\n========== ИТОГ ==========")
    print("Активных ордеров:", len(orders))
    print("Записей истории:", len(fills))
    print("Тест завершён.")
    print("Покупок, выставления и отмены ордеров не выполнялось.")


if __name__ == "__main__":
    main()
