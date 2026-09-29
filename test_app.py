"""Run: python3 test_app.py (no servers needed)."""
from app import extract_paragraphs, keyword_scores, top

page = """<p class="mw-empty-elt"></p>
<p><b>Augustus</b> was the first Roman emperor, reigning from 27&nbsp;BC until his death in AD 14.<sup class="reference">[1]</sup></p>
<style>.x{}</style><p>Short.</p>
<p>His reign initiated an era of relative peace known as the <a href="Pax_Romana">Pax Romana</a>, lasting over two centuries.</p>"""
paras = extract_paragraphs(page)
assert paras == ["Augustus was the first Roman emperor, reigning from 27 BC until his death in AD 14.",
                 "His reign initiated an era of relative peace known as the Pax Romana, lasting over two centuries."], paras

scores = keyword_scores("Who was the first Roman emperor?", paras)
assert scores == [3, 0], scores  # first, roman, emperor; filler words don't count
assert top(["a", "b", "c"], [0.1, 0.9, 0.5], 2) == ["b", "c"]
print("ok")
