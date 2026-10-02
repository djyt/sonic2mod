"""What a conversion reports: warnings (problems, each with its fix in core/report.py) and infos
(what was decided).  Every part of the converter writes here; convert.py's report reads it.
"""


class Diagnostics:
    def __init__(self) -> None:
        self.warnings: list[dict] = []
        self.infos: list[dict] = []
        self._seen: set = set()

    def warn(self, w: dict) -> None:
        """A warning, once per (type, channel, context, note): a note clamped 40 times is one line."""
        key = (
            w['type'],
            w.get('channel'),
            w.get('extra_ctx'),
            w.get('src_name') or w.get('note_name') or w.get('source'),
        )
        if key in self._seen:
            return
        self._seen.add(key)
        self.warnings.append(w)

    def info(self, i: dict) -> None:
        self.infos.append(i)
