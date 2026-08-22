#!/usr/bin/env python3
"""
docs2html.py — собирает HTML-версию документации из markdown.

    python3 docs2html.py DOCS.md -o site/docs.html
    python3 docs2html.py DOCS.md CLAUDE.md -o site

Оформление то же, что у читалки. Оглавление собирается само из заголовков
второго уровня, ссылки внутри работают без JavaScript.

Запускать после правки .md — иначе выложенная версия разойдётся с исходником.
"""

import argparse
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("Нужен markdown:  pip install markdown")

CSS = """
:root{--fg:#1a1a1a;--dim:#8a8a8a;--acc:#b06000;--line:#e6e2da;--bg:#fffdfa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:16px/1.68 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{display:flex;align-items:flex-start;max-width:74rem;margin:0 auto}

nav{position:sticky;top:0;flex:0 0 17rem;max-height:100vh;overflow:auto;
 padding:2.5rem 1rem 3rem 1.5rem;font-size:.86rem;line-height:1.5}
nav .t{font-weight:700;font-size:.95rem;margin-bottom:.4rem}
nav .home{display:block;color:var(--acc);text-decoration:none;font-size:.85rem;
 margin-bottom:1rem}
nav .home:hover{text-decoration:underline}
.homebar{display:none}
@media(max-width:900px){.homebar{display:block;margin:0 0 1.2rem}
 .homebar a{color:var(--acc);text-decoration:none;font-size:.9rem}}
nav .other{display:block;color:#555;text-decoration:none;font-size:.85rem;
 margin:1.2rem 0 0;padding-top:.8rem;border-top:1px solid var(--line)}
nav .other:hover{color:var(--acc)}
nav a{display:block;color:#555;text-decoration:none;padding:.24rem 0}
nav a:hover{color:var(--acc)}
nav a.sub{padding-left:1rem;font-size:.82rem;color:#777}

main{flex:1;min-width:0;padding:2.5rem 2rem 8rem;max-width:52rem}
h1{font-size:1.9rem;line-height:1.25;margin:0 0 .4rem;letter-spacing:-.01em}
h1+p{color:#555;margin-top:0}
h2{font-size:1.28rem;margin:3.2rem 0 1rem;padding-top:1.2rem;
 border-top:1px solid var(--line);scroll-margin-top:1rem}
h3{font-size:1.02rem;margin:2rem 0 .6rem;scroll-margin-top:1rem}
h2:first-of-type{border-top:0;padding-top:0}
p,ul,ol{margin:0 0 1rem}
li{margin:.2rem 0}
a{color:var(--acc)}
hr{display:none}

code{font:.87em ui-monospace,SFMono-Regular,Menlo,monospace;
 background:#f1ece3;padding:.1em .34em;border-radius:4px}
pre{background:#f7f4ee;border:1px solid var(--line);border-radius:8px;
 padding:.85rem 1rem;overflow:auto;margin:0 0 1.2rem}
pre code{background:0;padding:0;font-size:.83rem;line-height:1.55}

table{border-collapse:collapse;width:100%;margin:0 0 1.4rem;font-size:.9rem}
th,td{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--line);
 vertical-align:top}
th{font-weight:600;background:#f7f4ee;white-space:nowrap}
td:first-child code,th:first-child{white-space:nowrap}

blockquote{margin:0 0 1.2rem;padding:.1rem 0 .1rem 1rem;
 border-left:3px solid #ddd;color:#444}
strong{font-weight:600}

#burger{display:none;position:fixed;top:.7rem;right:.7rem;z-index:60;
 width:2.6rem;height:2.6rem;border:1px solid var(--line);border-radius:9px;
 background:var(--bg);cursor:pointer;align-items:center;justify-content:center;
 flex-direction:column;gap:.22rem;padding:0}
#burger i{display:block;width:1.1rem;height:2px;background:var(--fg);
 border-radius:2px;transition:transform .18s,opacity .18s}
#burger.on i:nth-child(1){transform:translateY(.42rem) rotate(45deg)}
#burger.on i:nth-child(2){opacity:0}
#burger.on i:nth-child(3){transform:translateY(-.42rem) rotate(-45deg)}
#veil{display:none;position:fixed;inset:0;background:#0006;z-index:55}
#veil.on{display:block}

@media(max-width:900px){
  #burger{display:flex}
  #veil{display:none}
  nav{position:fixed;top:0;right:0;bottom:0;width:min(20rem,84vw);z-index:58;
   background:var(--bg);border-left:1px solid var(--line);
   padding:4.2rem 1.2rem 2rem;transform:translateX(102%);
   transition:transform .22s ease;max-height:100vh;overflow:auto;
   box-shadow:-8px 0 26px #0002}
  nav.on{transform:none}
  main{padding:3.6rem 1.2rem 6rem}
  table{display:block;overflow-x:auto}
}
"""


def slug(text):
    t = re.sub(r"<[^>]+>", "", text).lower().strip()
    t = re.sub(r"[^\wа-яё\s-]", "", t)
    return re.sub(r"\s+", "-", t)[:60] or "x"


JS = r"""
<script>
(function(){
  var b=document.getElementById('burger'),n=document.querySelector('nav'),
      v=document.getElementById('veil');
  if(!b||!n) return;
  function set(on){
    b.classList.toggle('on',on); n.classList.toggle('on',on);
    v.classList.toggle('on',on);
    document.body.style.overflow = on ? 'hidden' : '';
  }
  b.addEventListener('click',function(){ set(!n.classList.contains('on')); });
  v.addEventListener('click',function(){ set(false); });
  /* клик по ссылке в оглавлении закрывает меню, иначе оно закрывает текст,
     к которому только что перешли */
  n.addEventListener('click',function(e){ if(e.target.closest('a')) set(false); });
  addEventListener('keydown',function(e){ if(e.key==='Escape') set(false); });
})();
</script>"""


def build(md_path, title=None, other=None):
    src = Path(md_path).read_text(encoding="utf-8")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists",
                                       "attr_list"])
    body = md.convert(src)

    # проставляем id заголовкам и попутно собираем оглавление
    toc = []

    def add_id(m):
        lvl, attrs, text = m.group(1), m.group(2), m.group(3)
        if "id=" in attrs:
            return m.group(0)
        s = slug(text)
        if lvl in ("2", "3"):
            toc.append((lvl, s, re.sub(r"<[^>]+>", "", text)))
        return f'<h{lvl}{attrs} id="{s}">{text}</h{lvl}>'

    body = re.sub(r"<h([1-6])([^>]*)>(.*?)</h\1>", add_id, body, flags=re.S)

    nav = "".join(
        f'<a href="#{s}" class="{"sub" if lvl == "3" else ""}">{t}</a>'
        for lvl, s, t in toc)

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    doc_title = title or (re.sub(r"<[^>]+>", "", h1.group(1)) if h1 else
                          Path(md_path).stem)

    home = '<a class="home" href="index.html">&larr; Книги</a>'
    if other:
        home += f'<a class="other" href="{other[0]}">{other[1]}</a>'
    return (f'<!doctype html><html lang="ru"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{doc_title}</title><style>{CSS}</style>'
            f'<div class="wrap"><nav><div class="t">{doc_title}</div>'
            f'{home}{nav}</nav>'
            f'<main><div class="homebar">'
            f'<a href="index.html">&larr; Книги</a></div>'
            f'{body}</main></div>'
            f'<div id="veil"></div>'
            f'<button id="burger" aria-label="Оглавление">'
            f'<i></i><i></i><i></i></button>' + JS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out", default="site",
                    help="файл (для одного входа) или папка")
    a = ap.parse_args()

    out = Path(a.out)
    many = len(a.files) > 1 or out.suffix == ""
    if many:
        out.mkdir(parents=True, exist_ok=True)

    CROSS = {'docs': ('reader.html', 'Как делать заметки →'),
             'reader': ('docs.html', 'Документация для сопровождающего →')}
    for f in a.files:
        html = build(f, other=CROSS.get(Path(f).stem.lower()))
        dst = out / (Path(f).stem.lower() + ".html") if many else out
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(html, encoding="utf-8")
        print(f"{f} → {dst}  ({len(html)//1024} КБ)")


if __name__ == "__main__":
    main()
