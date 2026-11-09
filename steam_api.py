import requests


class SteamMarketAPI:


    def __init__(self):

        self.url = (
            "https://steamcommunity.com/"
            "market/search/render/"
        )


    def search_item(self, name):

        params = {

            "query":
                name,

            "start":
                0,

            "count":
                100,

            "appid":
                570,

            "norender":
                1

        }


        try:

            r = requests.get(

                self.url,

                params=params,

                headers={
                    "User-Agent":
                    "Mozilla/5.0"
                },

                timeout=10

            )


            data = r.json()


        except Exception as e:

            print(
                "Steam ошибка:",
                e
            )

            return []


        result = []


        for item in data.get(
                "results",
                []
        ):


            result.append({

                "name":
                    name,


                "market_hash_name":
                    item.get(
                        "hash_name"
                    ),


                "price":

                    item.get(
                        "sell_price",
                        0
                    )
                    /100,


                "steam":
                    True

            })


        return result
def get_listing_info(
        self,
        hash_name
):

    url = (
        "https://steamcommunity.com/"
        "market/listings/570/"
        + hash_name
    )


    try:

        r = requests.get(
            url,
            headers={
                "User-Agent":
                "Mozilla/5.0"
            },
            timeout=10
        )


        text = r.text


        print(
            "Получил Steam страницу"
        )


        return text


    except Exception as e:

        print(
            "Steam listing ошибка:",
            e
        )

        return None