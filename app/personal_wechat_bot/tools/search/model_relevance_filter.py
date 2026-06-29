from __future__ import annotations


class FakeModelRelevanceFilter:
    def keep(self, query: str, result: dict) -> tuple[bool, str]:
        if result.get("irrelevant"):
            return False, "fake model judged unrelated"
        return True, "fake model judged relevant"
