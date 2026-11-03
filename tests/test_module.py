"""Tests for meta/Module_AI_dashboard.lua in a Lua 5.1 runtime with a stub of Scribunto's mw.

Needs lupa (pip install lupa); skipped without it. The page fed to the module is the one
collect.py builds, so the two stay in step.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402

try:
    from lupa import lua51
except ImportError:  # pragma: no cover
    lua51 = None

MODULE = Path(collect.HERE) / "meta" / "Module_AI_dashboard.lua"

STUB = r"""
local data = ...
mw = {
  text = { trim = function(s) return (s:gsub('^%s+', ''):gsub('%s+$', '')) end },
  ext = { data = { get = function(title)
    if title ~= 'Wikimedia AI/Latest.tab' then return false end
    return data
  end } },
  getContentLanguage = function()
    return {
      formatNum = function(self, n)
        local s = string.format('%d', n)
        local out = s:reverse():gsub('(%d%d%d)', '%1,'):reverse()
        return (out:gsub('^,', ''))
      end,
      formatDate = function(self, fmt, date)
        return 'formatted(' .. fmt .. ',' .. date .. ')'
      end,
    }
  end,
}
"""


@unittest.skipIf(lua51 is None, "lupa is not installed")
class ModuleTest(unittest.TestCase):
    def setUp(self):
        results = {
            "content_translation": {"en": 67909, "de": 21093, "fa": None},
            "editcheck_tone": {"en": 16830, "de": 0},
        }
        page = collect.latest_page("2026-11-02", results)
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("function _set(d) _data = d end")
        self.lua.globals()._set(self.to_lua(page))
        self.lua.execute(STUB.replace("local data = ...", "local data = _data"))
        self.p = self.lua.execute(MODULE.read_text(encoding="utf-8"))

    def to_lua(self, value):
        """Python JSON value -> Lua table, with JSON null as a hole, as mw.ext.data.get gives it."""
        if isinstance(value, dict):
            t = self.lua.table()
            for k, v in value.items():
                if v is not None:
                    t[k] = self.to_lua(v)
            return t
        if isinstance(value, list):
            t = self.lua.table()
            for i, v in enumerate(value, 1):
                if v is not None:
                    t[i] = self.to_lua(v)
            return t
        return value

    def call(self, fn, *args, **named):
        frame = self.lua.table(args=self.lua.table(*args, **named))
        return self.p[fn](frame)

    def test_totals_and_wikis(self):
        self.assertEqual(self.call("value", "content_translation"), "89,002")
        self.assertEqual(self.call("value", "content_translation", "en"), "67,909")
        self.assertEqual(self.call("value", " content_translation ", " de "), "21,093")
        self.assertEqual(self.call("value", "content_translation", "wikis_checked"), "2")
        self.assertEqual(self.call("value", "editcheck_tone", "wikis_nonzero"), "1")

    def test_missing_zero_and_raw(self):
        self.assertEqual(self.call("value", "content_translation", "fa"), "–")
        self.assertEqual(self.call("value", "content_translation", "fa", missing="n/a"), "n/a")
        self.assertEqual(self.call("value", "content_translation", "xx"), "–")
        self.assertEqual(self.call("value", "editcheck_tone", "de"), "0")
        self.assertEqual(self.call("value", "editcheck_tone", "de", zero="–"), "–")
        self.assertEqual(self.call("value", "content_translation", raw="1"), "89002")

    def test_link(self):
        url = "https://en.wikipedia.org/wiki/Special:RecentChanges?tagfilter=editcheck-tone"
        self.assertEqual(self.call("value", "editcheck_tone", "en", link=url), f"[{url} 16,830]")
        self.assertEqual(self.call("value", "editcheck_tone", "de", link=url, zero="–"), "–")

    def test_errors(self):
        self.assertIn('no metric "nope"', self.call("value", "nope"))
        self.assertIn("needs a metric", self.call("value"))

    def test_date(self):
        self.assertEqual(self.call("date"), "formatted(j F Y,2026-11-02)")
        self.assertEqual(self.call("date", raw="1"), "2026-11-02")
        self.assertEqual(self.call("date", format="F Y"), "formatted(F Y,2026-11-02)")


if __name__ == "__main__":
    unittest.main()
