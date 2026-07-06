"""
Build the single-file quiz embed for Shopify.

Concatenates quiz/quiz-config.js + quiz/quiz.css + quiz/quiz.js into
quiz/dist/oq-quiz-embed.html — one block you paste into a Custom Liquid
section (or an HTML block) on any page of the Old Quarter store.

Run from the repo root after editing quiz-config.js:

    python3 scripts/build_quiz_embed.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUIZ = ROOT / "quiz"
DIST = QUIZ / "dist"

HEADER = """<!-- ============================================================
  OLD QUARTER COFFEE — PRODUCT RECOMMENDATION QUIZ (self-contained embed)
  Paste this entire block into a "Custom Liquid" section in the theme
  editor, on whichever page should host the quiz.
  Built from the /quiz folder of the OQ repo — edit there, rebuild with
  scripts/build_quiz_embed.py, and re-paste to update.
============================================================= -->
"""


def build() -> None:
    config = (QUIZ / "quiz-config.js").read_text()
    css = (QUIZ / "quiz.css").read_text()
    js = (QUIZ / "quiz.js").read_text()

    embed = (
        HEADER
        + '<div id="oq-coffee-quiz"></div>\n'
        + "<style>\n" + css + "</style>\n"
        + "<script>\n" + config + "</script>\n"
        + "<script>\n" + js + "</script>\n"
    )

    DIST.mkdir(exist_ok=True)
    out = DIST / "oq-quiz-embed.html"
    out.write_text(embed)
    print(f"wrote {out} ({len(embed) / 1024:.1f} KB)")


if __name__ == "__main__":
    build()
