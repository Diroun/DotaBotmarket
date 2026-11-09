import re
from html import unescape


class GemParser:

    def extract_gems(self, description):
        if not isinstance(description, dict):
            return []

        entries = description.get("description") or []

        if not isinstance(entries, list):
            return []

        result = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            value = entry.get("value")

            if not isinstance(value, str) or not value:
                continue

            value = unescape(value)
            spans = re.findall(
                r"<span[^>]*>(.*?)</span>",
                value,
                flags=re.IGNORECASE | re.DOTALL
            )

            for span in spans:
                span = re.sub(r"<[^>]+>", "", span).strip()

                if not span:
                    continue

                ignore = [
                    "потусторонний самоцвет",
                    "призматический самоцвет",
                    "ethereal gem",
                    "prismatic gem",
                    "gem"
                ]

                if span.casefold() in ignore:
                    continue

                result.append(span)

        return result

    def find_gem(self, description, target):
        gems = self.extract_gems(description)

        if not gems:
            return ""

        target = target.casefold().strip()

        for item in gems:
            if item.casefold().strip() == target:
                return item

        return ""