#!/usr/bin/env python3
"""
bookreg.py — реестр глава/абзац/предложение для книжного клуба.

    pip install lxml razdel pymorphy3 pymorphy3-dicts-ru

Команды:
    build   собрать реестр из fb2 / epub / html / txt
    check   отчёт о качестве разбора — ЗАПУСКАТЬ ВСЕГДА перед раздачей
    freeze  заморозить нумерацию (пишет .lock рядом с реестром)
    verify  сравнить текущий реестр с замороженным, показать сдвиги
    search  поиск цитаты из терминала — проверить, что панель будет находить

Обычный цикл на новую книгу:

    python3 bookreg.py build книга.fb2 --id kapital -o ./books
    python3 bookreg.py check ./books/kapital.json      # чинить, пока есть ошибки
    python3 bookreg.py freeze ./books/kapital.json     # раздать участникам

Позже, если пришлось что-то поправить в разборе:

    python3 bookreg.py build книга.fb2 --id kapital -o ./books
    python3 bookreg.py verify ./books/kapital.json     # что съехало и на сколько

Нумерация:
    глава        путь-строка: "0" (текст до первой главы), "1", "3.2"
    абзац        <глава>.<n>          1.14
    предложение  <глава>.<n>.<k>      1.14.3
"""

import argparse
import base64
import hashlib
import html as html_mod
import json
import re
import sys
import zipfile
from pathlib import Path

try:
    from lxml import etree, html as lxml_html
except ImportError:
    sys.exit("Нужен lxml:  pip install lxml")
try:
    from razdel import sentenize
except ImportError:
    sys.exit("Нужен razdel:  pip install razdel")


# ===========================================================================
#  Нормализация и поиск
# ===========================================================================

DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
QUOTES = dict.fromkeys(map(ord, "«»„“”‘’\"'"), "")


def clean(t):
    """Чистка видимого текста: неразрывные пробелы, мягкие переносы, склейка."""
    t = (t.replace("\xa0", " ").replace("\u00ad", "")
          .replace("\u200b", "").replace("\ufeff", ""))
    return re.sub(r"\s+", " ", t).strip()


def normalize(t):
    """
    Текст в форме, по которой ищется цитата.

    Тире между словами выбрасывается: в разных изданиях «Европе – призрак»,
    «Европе — призрак», а человек наберёт дефис или ничего. Дефис внутри
    слова («какой-то») остаётся — он часть слова.
    """
    t = t.lower().replace("ё", "е")
    t = t.translate(DASHES).translate(QUOTES)
    t = re.sub(r"(?<![^\W\d_])-|-(?![^\W\d_])", " ", t)
    t = re.sub(r"[^\w\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# перед этими символами прямая кавычка считается открывающей
BEFORE_OPEN = set(' \t\n(\u005b{\u2013\u2014\u2015-\u2026«„')
MARK = "\ue010"   # метка «эту кавычку поставила догадка»


def typo(t):
    """
    Прямые кавычки → ёлочки, многоточие заводится внутрь цитаты.

    В FB2 кавычка одна на оба случая, поэтому открывающая опознаётся по
    соседу слева. Уровень вложенности считается, чтобы внутренняя цитата
    получила лапки по русской традиции: «текст „внутри“ текст».

    Многоточие у Ленина стоит снаружи кавычек — …"текст"… — потому что
    отмечает пропуск в цитируемом. Читать удобнее, когда оно внутри:
    «…текст…». Замена перестановочная, длина текста не меняется, поэтому
    смещения сносок и оформления остаются в силе.
    """
    # Вложенные лапки — только если кавычки в абзаце сходятся. При нечётном
    # числе (обрыв цитаты через абзац или, чаще, дефект распознавания в
    # исходнике) вложенность даст «„» посреди текста, а это выглядит ошибкой
    # заметнее, чем просто не туда повёрнутая ёлочка.
    nest = t.count('"') % 2 == 0
    out, depth = [], 0
    for i, ch in enumerate(t):
        if ch not in '"\u201c\u201d\u201e\u2018\u2019':
            out.append(ch)
            continue
        prev = out[-1] if out else " "
        # MARK помечает кавычку, поставленную догадкой. Перестановка
        # многоточия применяется только к помеченным: если в исходнике уже
        # стоит «…забыл», это осознанное решение автора или редактора, и
        # трогать его нельзя.
        out.append(MARK)
        if prev in BEFORE_OPEN:
            out.append("«" if depth == 0 or not nest else "„")
            depth += 1
        else:
            depth = max(0, depth - 1)
            out.append("»" if depth == 0 or not nest else "“")
    s = "".join(out)
    # Порядок важен. Между двумя цитатами — «конец»… «начало» — многоточие
    # относится к КОНЦУ первой: пропуск сделан в ней. Поэтому закрывающее
    # правило идёт первым, иначе многоточие уедет в следующую цитату.
    # Пробел сохраняется: без него соседние цитаты слипаются в »« и razdel
    # перестаёт видеть границу предложения.
    M = re.escape(MARK)
    s = re.sub(M + "(»|\u201c)(\\s*)\u2026",
               MARK + "\u2026\\1\\2", s)              # »…  →  …»
    s = re.sub("\u2026(\\s*)" + M + "(«|\u201e)",
               "\\1" + MARK + "\\2\u2026", s)         # …«  →  «…
    return s.replace(MARK, "")


_morph = None


def lemmas(t):
    """
    Начальные формы слов: «пролетариев всех стран» должно находить
    «ПРОЛЕТАРИИ ВСЕХ СТРАН». Цитату набирают по памяти, и падеж почти
    никогда не совпадает.
    """
    global _morph
    if _morph is None:
        try:
            import pymorphy3
        except ImportError:
            sys.exit("Нужен pymorphy3:  pip install pymorphy3 pymorphy3-dicts-ru")
        _morph = pymorphy3.MorphAnalyzer()
    return " ".join(_morph.parse(w)[0].normal_form for w in t.split())


def search(sentences, query, limit=8, field="lemma"):
    """
    Поиск цитаты. Не подстрокой: набирающий по памяти пропускает слова —
    «Пролетариям нечего терять» должно находить «Пролетариям нечего в ней
    терять». Ищем слова запроса по порядку с пропусками, ранжируем по
    плотности: чем меньше пропущено, тем выше.
    """
    q = normalize(query)
    if field == "lemma":
        q = lemmas(q)
    words = q.split()
    if not words:
        return []
    hits = []
    for s in sentences:
        sw = s.get(field, s["norm"]).split()
        i, pos = 0, []
        for j, w in enumerate(sw):
            if i < len(words) and w.startswith(words[i]):
                pos.append(j)
                i += 1
        if i < len(words):
            continue
        hits.append((pos[0], (pos[-1] - pos[0] + 1) / len(words), s))
    hits.sort(key=lambda h: (h[1], h[0]))
    return [h[2] for h in hits[:limit]]


# ===========================================================================
#  Чтение форматов.  Каждый ридер отдаёт плоский список:
#     {"kind": "chapter", "level": int, "text": str}
#     {"kind": "para",    "text": str}
#     {"kind": "mark",    "text": str}     подзаголовок, без нумерации
# ===========================================================================

JUNK = re.compile(
    r"royallib|litres|литрес|флибуст|loveread|скачали книгу|приятного чтения|"
    r"все книги автора|в других форматах|оставить отзыв|бесплатной электронной|"
    r"версия для печати", re.I)

FB2NS = "http://www.gribuser.ru/xml/fictionbook/2.0"


NOTE_OPEN, NOTE_CLOSE = "\ue000", "\ue001"
FMT_OPEN, FMT_CLOSE = "\ue002", "\ue003"

# встроенное оформление: тег → однобуквенный код в реестре → тег в читалке
FMT = {"emphasis": "e", "italic": "e", "i": "e", "em": "e",
       "strong": "s", "b": "s", "bold": "s",
       "strikethrough": "k", "s": "k", "del": "k",
       "sub": "b", "sup": "p", "code": "c", "style": "y"}
FMT_TAG = {"e": "em", "s": "strong", "k": "s", "b": "sub", "p": "sup",
           "c": "code", "y": "span"}


def _parse_marks(text):
    """
    Разбирает строку с сентинелами и отдаёт (чистый текст, сноски, оформление).

    Сентинелы, а не заранее посчитанные позиции: clean() схлопывает пробелы и
    сдвинул бы всё, что посчитано до него. Поэтому текст сначала чистится
    вместе с метками, и только потом метки снимаются.
    """
    out, refs, fmt, stack = [], [], [], []
    pos, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == NOTE_OPEN:
            j = text.find(NOTE_CLOSE, i)
            if j < 0:
                i += 1
                continue
            refs.append({"pos": pos, "note": text[i + 1:j]})
            i = j + 1
        elif ch == FMT_OPEN:
            stack.append((text[i + 1] if i + 1 < n else "y", pos))
            i += 2
        elif ch == FMT_CLOSE:
            if stack:
                t, start = stack.pop()
                if pos > start:
                    fmt.append({"pos": start, "len": pos - start, "t": t})
            i += 1
        else:
            out.append(ch)
            pos += 1
            i += 1
    fmt.sort(key=lambda x: (x["pos"], -x["len"]))
    return "".join(out), refs, fmt


def _inline(el, localname, skip_note_text=True):
    """
    Собирает текст элемента, расставляя сентинелы сносок и оформления.

    Маркер сноски «[4]» из текста убирается: попав в цитату, он ломает и
    поиск, и саму цитату. Позиция запоминается.
    """
    parts = []

    def walk(node):
        if node.text:
            parts.append(node.text)
        for ch in node:
            tag = localname(ch)
            if tag == "a" and (ch.get("type") == "note" or skip_note_text):
                nid = (ch.get("{http://www.w3.org/1999/xlink}href")
                       or ch.get("href") or "").lstrip("#")
                txt = "".join(ch.itertext()).strip()
                if ch.get("type") == "note" or re.fullmatch(r"\[?\d{1,4}\]?", txt):
                    if nid:
                        parts.append(NOTE_OPEN + nid + NOTE_CLOSE)
                else:
                    walk(ch)
            elif tag in ("image", "img"):
                pass
            else:
                code = FMT.get(tag)
                if code:
                    parts.append(FMT_OPEN + code)
                walk(ch)
                if code:
                    parts.append(FMT_CLOSE)
            if ch.tail:
                parts.append(ch.tail)

    walk(el)
    return _parse_marks(clean("".join(parts)))


def _fb2_text(el, keep_refs=False):
    """Текст элемента FB2. keep_refs → отдать ещё сноски и оформление."""
    t, refs, fmt = _inline(el, lambda e: etree.QName(e).localname)
    return (t, refs, fmt) if keep_refs else t



# картинки библиотек: экслибрис, логотип — тот же мусор, что и рекламный текст
IMG_JUNK = re.compile(r"exlibris|fbw|royallib|litres|logo|banner", re.I)


def read_fb2(path):
    ns = {"f": FB2NS}
    root = etree.parse(str(path)).getroot()
    items, notes = [], {}

    def local(e):
        return etree.QName(e).localname

    def section(sec, level):
        ti = sec.find("f:title", ns)
        if ti is not None:
            t, refs, fmt = _fb2_text(ti, keep_refs=True)
            items.append({"kind": "chapter", "level": level,
                          "text": t, "refs": refs, "fmt": fmt})
        subs = sec.findall("f:section", ns)
        if subs:
            for sub in subs:
                section(sub, level + 1)
            return
        for el in sec:
            tag = local(el)
            if tag == "title":
                continue
            if tag == "subtitle":
                items.append({"kind": "mark", "text": _fb2_text(el)})
                continue
            if tag == "table":
                rows = []
                for tr in el.iter("{%s}tr" % FB2NS):
                    cells = []
                    for td in tr:
                        if local(td) not in ("td", "th"):
                            continue
                        cells.append((local(td), _fb2_text(td)))
                    if cells:
                        rows.append(cells)
                if rows:
                    items.append({"kind": "table", "rows": rows})
                continue
            if tag == "image":
                href = (el.get("{http://www.w3.org/1999/xlink}href")
                        or el.get("href") or "").lstrip("#")
                if href and not IMG_JUNK.search(href):
                    items.append({"kind": "image", "href": href})
                continue
            if tag not in ("p", "poem", "cite", "epigraph", "stanza"):
                continue
            # текст в cite/epigraph бывает не в <p>: финал «Манифеста» лежит
            # в <cite><subtitle>, атрибуция цитаты — в <text-author>
            blocks = [el] if tag == "p" else [
                x for x in el.iter()
                if local(x) in ("p", "subtitle", "v", "text-author")]
            for b in blocks:
                t, refs, fmt = _fb2_text(b, keep_refs=True)
                items.append({"kind": "para", "text": t, "refs": refs,
                              "fmt": fmt,
                              "cite": tag in ("cite", "epigraph"),
                              "author": local(b) == "text-author",
                              # строка стиха: номер получает как абзац, но
                              # не выключается по ширине и не отбивается
                              "verse": local(b) == "v"})

    body = root.find("f:body", ns)
    for sec in body.findall("f:section", ns):
        section(sec, 1)

    for b in root.findall("f:body", ns):
        if b.get("name") != "notes":
            continue
        for sec in b.iter("{%s}section" % FB2NS):
            nid = sec.get("id")
            if not nid:
                continue
            ti = sec.find("f:title", ns)
            body_t, refs, fmt, off = "", [], [], 0
            for t, rs, fs in [_fb2_text(x, keep_refs=True)
                              for x in sec.findall("f:p", ns)]:
                if body_t:
                    body_t += " "
                    off += 1
                for r in rs:
                    refs.append({"pos": off + r["pos"], "note": r["note"]})
                for f in fs:
                    fmt.append({"pos": off + f["pos"], "len": f["len"],
                                "t": f["t"]})
                body_t += t
                off += len(t)
            notes[nid] = {"num": _fb2_text(ti) if ti is not None else "",
                          "text": body_t, "refs": refs, "fmt": fmt}

    binaries = {}
    for b in root.findall("f:binary", ns):
        bid = b.get("id")
        if bid and b.text:
            binaries[bid] = (b.get("content-type", "image/jpeg"),
                             b.text.strip())

    meta = {}
    ti = root.find(".//f:title-info", ns)
    if ti is not None:
        bt = ti.find("f:book-title", ns)
        meta["title"] = _fb2_text(bt) if bt is not None else ""
        meta["authors"] = [
            clean(" ".join(_fb2_text(a.find(f"f:{k}", ns))
                           for k in ("first-name", "middle-name", "last-name")
                           if a.find(f"f:{k}", ns) is not None))
            for a in ti.findall("f:author", ns)]
    meta["binaries"] = binaries
    return items, notes, meta


BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "div"}
DROP = {"script", "style", "nav", "header", "footer", "aside", "noscript",
        "table", "form", "figcaption"}


def _html_items(tree):
    items = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower().split("}")[-1]
        if tag in DROP:
            el.clear()
            continue
        if tag not in BLOCK:
            continue
        # div берём только если внутри нет других блоков
        if tag == "div" and any(
                isinstance(c.tag, str) and c.tag.lower().split("}")[-1] in BLOCK
                for c in el.iter() if c is not el):
            continue
        t, refs, fmt = _inline(
            el, lambda e: (e.tag.lower().split("}")[-1]
                           if isinstance(e.tag, str) else ""))
        if len(t) < 2:
            continue
        if tag[0] == "h" and tag[1:].isdigit():
            items.append({"kind": "chapter", "level": int(tag[1]),
                          "text": t, "refs": refs, "fmt": fmt})
        else:
            items.append({"kind": "para", "text": t, "refs": refs,
                          "fmt": fmt,
                          "cite": tag == "blockquote"})
    return items


def decode_bytes(raw):
    """
    Кодировка HTML из библиотек объявлена не всегда, а бывает и cp1251.
    Пробуем по очереди; latin-1 не пробуем никогда — он «декодирует»
    что угодно и молча превращает русский текст в кракозябры.
    """
    m = re.search(rb'charset=["\']?\s*([\w-]+)', raw[:4000], re.I)
    order = []
    if m:
        order.append(m.group(1).decode("ascii", "ignore"))
    order += ["utf-8", "cp1251", "koi8-r"]
    for enc in order:
        try:
            t = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # кириллица, прочитанная как латиница, даёт много Ã/Ð/Ñ
        if len(re.findall(r"[ÃÐÑÂ]", t)) > len(t) / 200:
            continue
        return t
    return raw.decode("utf-8", "replace")


def drop_junk(items):
    """Реклама библиотеки. Только короткие блоки: в длинном абзаце
    совпадение почти наверняка ложное."""
    out = []
    for i in items:
        t = i.get("text")
        if t is None:                       # картинка — текста нет
            out.append(i)
        elif not (len(t) < 200 and JUNK.search(t)):
            out.append(i)
    return out


def demote_title_h1(items):
    """
    Единственный <h1> в начале — это название книги, а не глава.
    Если его не убрать, все главы уезжают на уровень ниже и якорь
    становится «1.2.14» вместо «2.14» — то есть у HTML-версии книги
    нумерация не совпадёт с FB2-версией той же книги.

    Считать «начало» надо уже после выброса рекламной шапки, иначе
    «Спасибо, что скачали книгу» оказывается первым абзацем и <h1>
    перестаёт быть первым.
    """
    items = drop_junk(items)
    h1 = [i for i, x in enumerate(items) if x["kind"] == "chapter"
          and x["level"] == 1]
    first_para = next((i for i, x in enumerate(items)
                       if x["kind"] == "para"), len(items))
    if len(h1) == 1 and h1[0] < first_para:
        items = items[:h1[0]] + items[h1[0] + 1:]
        for x in items:
            if x["kind"] == "chapter":
                x["level"] = max(1, x["level"] - 1)
    return items


def read_html(path):
    raw = Path(path).read_bytes()
    tree = lxml_html.fromstring(decode_bytes(raw))
    title = ""
    t = tree.find(".//title")
    if t is not None:
        title = clean(t.text_content())
    body = tree.find(".//body")
    items = _html_items(body if body is not None else tree)
    return demote_title_h1(items), {}, {"title": title}


def read_epub(path):
    """EPUB: container.xml → OPF → spine, файлы читаются в порядке чтения."""
    items, meta = [], {}
    with zipfile.ZipFile(path) as z:
        cont = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = cont.find(".//{*}rootfile").get("full-path")
        opf = etree.fromstring(z.read(opf_path))
        base = str(Path(opf_path).parent)

        ttl = opf.find(".//{http://purl.org/dc/elements/1.1/}title")
        meta["title"] = clean(ttl.text) if ttl is not None else ""
        meta["authors"] = [clean(a.text) for a in opf.findall(
            ".//{http://purl.org/dc/elements/1.1/}creator") if a.text]

        ids = {it.get("id"): it.get("href")
               for it in opf.findall(".//{*}manifest/{*}item")}
        for ref in opf.findall(".//{*}spine/{*}itemref"):
            href = ids.get(ref.get("idref"))
            if not href:
                continue
            name = str(Path(base) / href) if base != "." else href
            name = name.replace("\\", "/")
            try:
                raw = z.read(name)
            except KeyError:
                continue
            try:
                doc = lxml_html.fromstring(decode_bytes(raw))
            except Exception:
                continue
            items += _html_items(doc)
    return demote_title_h1(items), {}, meta


def read_txt(path):
    """Простой текст: абзацы разделены пустой строкой."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    items = []
    for chunk in re.split(r"\n\s*\n", raw):
        t = clean(chunk)
        if len(t) < 2:
            continue
        # короткая строка без точки на конце — считаем заголовком
        if len(t) < 90 and not re.search(r"[.!?…]$", t):
            items.append({"kind": "chapter", "level": 1, "text": t})
        else:
            items.append({"kind": "para", "text": t})
    return items, {}, {}


READERS = {".fb2": read_fb2, ".epub": read_epub, ".html": read_html,
           ".htm": read_html, ".xhtml": read_html, ".txt": read_txt}


# ===========================================================================
#  Сборка реестра
# ===========================================================================

def build(path, book_id, lemmatize=True, typo_on=False, front=""):
    path = Path(path)
    reader = READERS.get(path.suffix.lower())
    if not reader:
        sys.exit(f"Неизвестный формат {path.suffix}. "
                 f"Поддерживаются: {', '.join(sorted(READERS))}")

    items, notes, meta = reader(path)
    items = drop_junk(items)

    reg = {"book": {"id": book_id,
                    "title": meta.get("title", path.stem),
                    "authors": meta.get("authors", []),
                    "source_file": path.name,
                    "source_format": path.suffix.lower().lstrip(".")},
           "chapters": [], "paragraphs": [], "sentences": [],
           "marks": [], "images": [], "tables": [], "notes": notes}

    front_re = re.compile(front, re.I) if front else None
    counters, cur, n = [], "0", 0
    reg["chapters"].append({"id": "0", "title": "", "level": 0})

    for it in items:
        if it["kind"] == "chapter":
            lvl = it["level"]
            # Передняя часть — предисловия, введения, посвящения. Номера главы
            # не получают: иначе «Глава I» окажется второй, и нумерация
            # разойдётся с книгой. Заголовок остаётся видимым как пометка.
            # Только до первой настоящей главы и только на верхнем уровне.
            if (front_re and lvl == 1 and not counters
                    and front_re.search(it["text"])):
                if typo_on:
                    it["text"] = typo(it["text"])
                reg["marks"].append({"chapter": "0", "after": n,
                                     "text": it["text"], "front": True})
                cur = "0"
                continue
            # спуск на уровень выше — лишние разряды отбрасываем
            while len(counters) > lvl:
                counters.pop()
            if len(counters) == lvl:
                counters[-1] += 1          # соседняя глава того же уровня
            else:
                while len(counters) < lvl - 1:
                    counters.append(1)     # пропущенный уровень (h1→h3)
                counters.append(1)         # первая глава нового уровня
            cur = ".".join(map(str, counters))
            n = 0
            if typo_on:
                it["text"] = typo(it["text"])
            reg["chapters"].append({"id": cur, "title": it["text"],
                                    "level": lvl,
                                    "refs": it.get("refs", []),
                                    "fmt": it.get("fmt", [])})
            continue
        if it["kind"] == "mark":
            reg["marks"].append({"chapter": cur, "after": n,
                                 "text": it["text"]})
            continue
        if it["kind"] == "table":
            # номера не получает — иначе добавление таблиц двигало бы
            # нумерацию, ровно как было бы с картинками
            reg["tables"].append({"chapter": cur, "after": n,
                                  "rows": it["rows"]})
            continue
        if it["kind"] == "image":
            # своего номера не получает: иначе добавление картинок сдвинуло бы
            # нумерацию всех абзацев после неё
            reg["images"].append({"chapter": cur, "after": n,
                                  "href": it["href"]})
            continue

        if typo_on:
            it["text"] = typo(it["text"])
        n += 1
        pid = f"{cur}.{n}"
        sids = []
        refs = it.get("refs", [])
        fmts = it.get("fmt", [])
        for k, s in enumerate(sentenize(it["text"]), 1):
            sid = f"{pid}.{k}"
            nm = normalize(s.text)
            rec = {"id": sid, "para": pid, "chapter": cur, "n": k,
                   "text": s.text, "norm": nm,
                   "start": s.start, "stop": s.stop}
            # сноски, попавшие в границы этого предложения
            mine = [{"pos": r["pos"] - s.start, "note": r["note"]}
                    for r in refs if s.start <= r["pos"] <= s.stop]
            if mine:
                rec["notes"] = mine
            # диапазон оформления обрезается по границам предложения:
            # курсив может начаться в одном и кончиться в следующем
            mf = []
            for f in fmts:
                a = max(f["pos"], s.start)
                b = min(f["pos"] + f["len"], s.stop)
                if b > a:
                    mf.append({"pos": a - s.start, "len": b - a, "t": f["t"]})
            if mf:
                rec["fmt"] = mf
            if lemmatize:
                rec["lemma"] = lemmas(nm)
            reg["sentences"].append(rec)
            sids.append(sid)
        rec_p = {"id": pid, "chapter": cur, "n": n,
                 "text": it["text"], "sentences": sids}
        if it.get("cite"):
            rec_p["cite"] = True
        if it.get("author"):
            rec_p["author"] = True
        if it.get("verse"):
            rec_p["verse"] = True
        if fmts:
            rec_p["fmt"] = fmts
        reg["paragraphs"].append(rec_p)

    reg["chapters"] = [c for c in reg["chapters"]
                       if c["id"] == "0" or
                       any(p["chapter"] == c["id"] for p in reg["paragraphs"])
                       or c["title"]]
    reg["_binaries"] = meta.get("binaries", {})
    reg["book"]["quotes"] = bool(typo_on)
    reg["book"]["front"] = front
    reg["book"]["text_sha256"] = hashlib.sha256(
        "\n".join(p["text"] for p in reg["paragraphs"]).encode()).hexdigest()
    return reg


# ===========================================================================
#  Проверка
# ===========================================================================

def check(reg):
    """Отчёт о качестве разбора. ERROR — чинить, WARN — посмотреть глазами."""
    P, S = reg["paragraphs"], reg["sentences"]
    out = []

    def add(sev, code, msg, ex=()):
        out.append({"sev": sev, "code": code, "msg": msg, "ex": list(ex)[:6]})

    if not P:
        add("ERROR", "empty", "Абзацев не найдено вообще")
        return out

    junk = [p["id"] for p in P
            if len(p["text"]) < 200 and JUNK.search(p["text"])]
    if junk:
        add("ERROR", "junk", f"Реклама библиотеки в тексте: {len(junk)}", junk)

    marks = [s["id"] for s in S if re.search(r"\[\d{1,3}\]", s["text"])]
    if marks:
        add("ERROR", "notemark",
            f"Маркеры сносок остались в тексте: {len(marks)} — "
            f"они попадут в цитату и сломают поиск", marks)

    moji = [p["id"] for p in P if re.search(r"[�]|&[a-z]+;|Ã.|Ð.", p["text"])]
    if moji:
        add("ERROR", "encoding", f"Битая кодировка или HTML-сущности: {len(moji)}",
            moji)

    hyph = [p["id"] for p in P if re.search(r"\w- \w", p["text"])]
    if hyph:
        add("WARN", "hyphen",
            f"Похоже на неснятый перенос («слово- вое»): {len(hyph)}", hyph)

    low = [s["id"] for s in S if re.match(r"^[а-яё]", s["text"])]
    if low:
        add("WARN", "lowstart",
            f"Предложение начинается со строчной — вероятно, разрыв не там: "
            f"{len(low)}", low)

    noend = [s["id"] for s in S
             if not re.search(r"[.!?…:;»)\"]$", s["text"]) and len(s["text"]) > 40]
    if noend:
        add("WARN", "noend",
            f"Длинное предложение без знака в конце — возможно, обрыв: "
            f"{len(noend)}", noend)

    if S:
        L = sorted(len(s["text"]) for s in S)
        p95 = L[int(len(L) * 0.95)]
        longs = [s["id"] for s in S if len(s["text"]) > max(600, p95 * 2)]
        if longs:
            add("WARN", "toolong",
                f"Очень длинные предложения — возможно, пропущен разрыв: "
                f"{len(longs)}", longs)

    heads = [p["id"] for p in P
             if len(p["text"]) < 70 and not re.search(r"[.!?…]$", p["text"])
             and len(reg["paragraphs"]) > 20]
    if len(heads) > len(P) * 0.05:
        add("WARN", "headings",
            f"Много коротких абзацев без точки ({len(heads)}) — возможно, "
            f"это заголовки, не опознанные как главы", heads)

    seen = {}
    dup = []
    for s in S:
        if len(s["norm"]) > 40:
            if s["norm"] in seen:
                dup.append(f'{seen[s["norm"]]} = {s["id"]}')
            seen[s["norm"]] = s["id"]
    if dup:
        add("WARN", "dup", f"Одинаковые предложения: {len(dup)}", dup)

    # неоднозначность для панели: одинаковое начало у разных предложений
    starts = {}
    for s in S:
        key = " ".join(s["norm"].split()[:5])
        if len(key.split()) == 5:
            starts.setdefault(key, []).append(s["id"])
    amb = {k: v for k, v in starts.items() if len(v) > 1}
    if amb:
        add("INFO", "ambiguous",
            f"Одинаковые первые 5 слов у разных предложений: {len(amb)} групп "
            f"— в панели придётся выбирать из нескольких",
            [f'{v[0]}…: «{k[:40]}»' for k, v in list(amb.items())])

    # подсказка про переднюю часть: её легко не заметить, а последствие —
    # «Глава I» под номером 2, и это уже не поправить после заморозки
    FRONT_HINT = re.compile(
        r"^\s*(предислов|введени|от автора|от редакц|вместо предислов|"
        r"посвящ|к читател|preface|introduction)", re.I)
    if not reg["book"].get("front"):
        tops = [c for c in reg["chapters"]
                if c["id"] != "0" and "." not in c["id"] and c.get("title")]
        if tops and FRONT_HINT.search(tops[0]["title"]):
            add("WARN", "front",
                f"Первая глава похожа на переднюю часть: "
                f"«{tops[0]['title'][:44]}». Из-за неё нумерация глав "
                f"сдвинута на единицу относительно книги. "
                f"Если это так — пересоберите с "
                f"--front '{tops[0]['title'].split()[0]}'")

    empty = [c["id"] for c in reg["chapters"]
             if c["id"] != "0" and c.get("title")
             and not any(p["chapter"] == c["id"] for p in P)
             and not any(x["id"].startswith(c["id"] + ".")
                         for x in reg["chapters"])]
    if empty:
        add("WARN", "emptychap", f"Главы без абзацев: {len(empty)}", empty)

    QP = re.compile(r"[«»\u201e\u201c]")
    unbal = []
    for p_ in P:
        q = QP.findall(p_["text"])
        if q.count("«") + q.count("\u201e") != q.count("»") + q.count("\u201c"):
            unbal.append(p_["id"])
    if unbal:
        add("WARN", "quotes",
            f"Кавычки не сходятся в абзаце: {len(unbal)} — цитата тянется "
            f"через абзацы либо кавычка потеряна в исходнике", unbal)

    N = reg.get("notes", {})
    if N:
        used = [x["note"] for s in S for x in s.get("notes", [])]
        used += [x["note"] for c in reg["chapters"] for x in c.get("refs", [])]
        used += [x["note"] for v in N.values() for x in v.get("refs", [])]
        dangling = sorted({x for x in used if x not in N})
        if dangling:
            add("ERROR", "note_dangling",
                f"Ссылки на несуществующие сноски: {len(dangling)}", dangling)
        orphan = sorted(k for k in N if k not in used)
        if orphan:
            add("WARN", "note_orphan",
                f"Сноски, на которые никто не ссылается: {len(orphan)} — "
                f"в читалке они не появятся", orphan)

    pre = sum(1 for p in P if p["chapter"] == "0")
    if pre > len(P) * 0.3:
        add("WARN", "nochapters",
            f"{pre} из {len(P)} абзацев вне глав — заголовки, скорее всего, "
            f"не распознались")
    return out


def print_report(reg, rep):
    P, S = reg["paragraphs"], reg["sentences"]
    b = reg["book"]
    print(f'{b["title"]} — {", ".join(b.get("authors") or []) or "?"}')
    print(f'формат: {b.get("source_format")}   sha256: {b["text_sha256"][:16]}…')
    real = [c for c in reg["chapters"] if c["id"] != "0" or
            any(p["chapter"] == "0" for p in P)]
    print(f'глав: {len(real)}   абзацев: {len(P)}   предложений: {len(S)}   '
          f'сносок: {len(reg.get("notes", {}))}')
    if S:
        L = sorted(len(s["text"]) for s in S)
        print(f'длина предложения: медиана {L[len(L)//2]}, '
              f'95% {L[int(len(L)*.95)]}, макс {L[-1]}\n')

    ICON = {"ERROR": "✗", "WARN": "!", "INFO": "·"}
    if not rep:
        print("✓ проверки пройдены")
    for r in rep:
        print(f'{ICON[r["sev"]]} [{r["code"]}] {r["msg"]}')
        for e in r["ex"]:
            print(f'      {e}')
    errs = sum(1 for r in rep if r["sev"] == "ERROR")
    warns = sum(1 for r in rep if r["sev"] == "WARN")
    print(f'\nошибок: {errs}   предупреждений: {warns}')
    return errs


# ===========================================================================
#  Заморозка нумерации
# ===========================================================================

def lockfile(reg_path):
    return Path(reg_path).with_suffix(".lock.json")


def freeze(reg, reg_path):
    lock = {"book": reg["book"]["id"],
            "text_sha256": reg["book"]["text_sha256"],
            "anchors": {s["id"]: s["norm"][:70] for s in reg["sentences"]}}
    lockfile(reg_path).write_text(
        json.dumps(lock, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f'Заморожено: {len(lock["anchors"])} якорей → '
          f'{lockfile(reg_path).name}')
    print("С этого момента нумерация раздана участникам и меняться не должна.")


def verify(reg, reg_path, map_out=None):
    lp = lockfile(reg_path)
    if not lp.exists():
        sys.exit(f"Нет {lp} — нумерация ещё не заморожена.\n"
                 f"  Заморозить:  python3 bookreg.py freeze {reg_path}")
    lock = json.loads(lp.read_text(encoding="utf-8"))
    old = lock["anchors"]
    new = {s["id"]: s["norm"][:70] for s in reg["sentences"]}

    # Сравнивать надо якоря, а не хеш текста. Перенос предисловия в переднюю
    # часть (--front) сдвигает все номера, не меняя ни одного слова: хеш при
    # этом совпадает, и проверка по нему сказала бы «всё цело», когда сломаны
    # все ссылки до единой. Ложное «всё хорошо» опаснее ложной тревоги.
    if old == new:
        print("✓ ничего не изменилось, все ссылки в заметках целы")
        return 0

    if map_out:
        # полная карта «старый якорь → новый»: по ней правятся заметки
        rev = {}
        for k, v in new.items():
            rev.setdefault(v, k)
        rows = ["старый\tновый\tтекст"]
        moved_n = 0
        for k in sorted(old, key=lambda x: [int(i) for i in x.split(".")]):
            dst = rev.get(old[k])
            if dst == k:
                continue
            moved_n += 1
            rows.append(f"{k}\t{dst or 'ПОТЕРЯНО'}\t{old[k][:60]}")
        Path(map_out).write_text("\n".join(rows), encoding="utf-8")
        print(f"Карта переносов: {moved_n} строк → {map_out}\n")

    gone = [k for k in old if k not in new]
    added = [k for k in new if k not in old]
    moved = [k for k in old if k in new and old[k] != new[k]]

    if not gone and not added and not moved:
        # текст правили, но границы предложений не двинулись: опечатка,
        # кавычки, курсив. Ссылки в заметках целы, нужна только перезаморозка
        print("✓ текст изменился, но все якоря на месте — ссылки в заметках "
              "целы")
        print("  Так бывает после правки опечаток, кавычек или оформления.")
        # пути даём те же, что человек ввёл, а не голые имена файлов:
        # реестры лежат в books/, и команда должна работать копипастом
        print(f"  Перезаморозьте:")
        print(f"    rm {lockfile(reg_path)}")
        print(f"    python3 bookreg.py freeze {reg_path}")
        return 0

    # Частый случай — не «поехали отдельные ссылки», а вся книга сдвинута
    # на одну главу: подключили --front, добавили или убрали предисловие.
    # Тогда 446 строк списка бесполезны, полезна одна.
    rev_all = {}
    for k, v in new.items():
        rev_all.setdefault(v, k)
    pairs = [(k, rev_all[old[k]]) for k in old if old[k] in rev_all]
    shift = None
    if pairs and len(pairs) > len(old) * 0.9:
        # без допуска не обойтись: у пары предложений совпадают первые 70
        # знаков, они сопоставляются не с тем якорем и портят картину.
        # Смотрим не «все до одного», а подавляющее большинство.
        counts = {}
        for a, b in pairs:
            pa, pb = a.split("."), b.split(".")
            d = (int(pb[0]) - int(pa[0])
                 if len(pa) == len(pb) and pa[1:] == pb[1:] else None)
            counts[d] = counts.get(d, 0) + 1
        top, n_top = max(counts.items(), key=lambda x: x[1])
        if top and n_top >= len(pairs) * 0.97:
            shift = top

    if shift:
        znak = "уменьшилась" if shift < 0 else "увеличилась"
        print(f"! вся нумерация сдвинута: первая цифра якоря {znak} "
              f"на {abs(shift)}\n")
        print(f"  Затронуты все {len(pairs)} якорей. Текст книги не менялся —")
        print(f"  сдвинулось только деление на главы. Так бывает от --front,")
        print(f"  от добавленного или убранного предисловия.\n")
        print(f"  Сравнение идёт с {lockfile(reg_path).name}, а не с сайтом и")
        print(f"  не с заметками: про них инструмент ничего не знает. Пока")
        print(f"  снимок не обновлён, сообщение будет повторяться.\n")
        ex = sorted(pairs, key=lambda x: [int(i) for i in x[0].split(".")])[:3]
        for a, b in ex:
            print(f"    {a}  →  {b}")
        print(f"\n  Если в заметках уже стоят новые номера — перезаморозьте:")
        print(f"    rm {lockfile(reg_path)}")
        print(f"    python3 bookreg.py freeze {reg_path}")
        print(f"  Если нет — правьте по карте:  --map перенос.tsv")
        return 1

    print("! текст изменился с момента заморозки\n")
    print(f"  исчезли якоря:      {len(gone)}")
    print(f"  появились новые:    {len(added)}")
    print(f"  номер стал другим:  {len(moved)}"
          + ("   ← ЭТО ОПАСНО" if moved else ""))
    if moved:
        print("\n  Эти ссылки в заметках теперь указывают не туда:")
        # ищем, куда уехал старый текст
        rev = {v: k for k, v in new.items()}
        for k in moved[:15]:
            dst = rev.get(old[k])
            print(f'    {k:<12} было «{old[k][:44]}»')
            print(f'    {"":<12} стало {"→ теперь " + dst if dst else "потеряно"}')
    if gone and not moved:
        print("\n  Сдвигов нет, только пропажи — вероятно, вы что-то удалили.")
    return 1 if moved else 0


# ===========================================================================
#  Вывод читалки
# ===========================================================================

CSS = """
:root{--fg:#1a1a1a;--dim:#9a9a9a;--hi:#fff3bf;--acc:#b06000}
*{box-sizing:border-box}
body{max-width:40em;margin:0 auto;padding:5rem 1.5rem 8rem;
 font:1.06rem/1.7 Georgia,'PT Serif',serif;color:var(--fg)}
h1{font-size:1.5rem;margin:0 0 2.5rem;scroll-margin-top:5rem}
h2{font-size:1.3rem;margin:3.4rem 0 1.4rem;padding-bottom:.4rem;
 border-bottom:1px solid #e6e2da}
h3{font-size:1.08rem;margin:2.6rem 0 1.1rem}
h4.mark{font-size:1rem;font-weight:400;font-style:italic;margin:2rem 0 1rem}
h2.ch,h3.ch{scroll-margin-top:5rem}
/* передняя часть — заголовок уровня главы, но без нижней черты:
   номера у неё нет, и черта делала бы её равной настоящим главам */
h2.ch.front{border-bottom:0;padding-bottom:0;color:#555}
span.hook{display:block;height:0;scroll-margin-top:5rem}
p.subh{margin:2.6rem 0 1.1rem;text-align:left;font-size:1.02rem}
p.subh strong{font-weight:600;letter-spacing:.02em}

/* --- оглавление по главам --- */
#toc{position:fixed;left:0;top:0;bottom:0;width:16rem;overflow:auto;z-index:52;
 padding:4.6rem 1rem 3rem 1.4rem;background:#fffdfaf7;
 border-right:1px solid #e6e2da;font:.84rem/1.4 system-ui,sans-serif;
 display:none;backdrop-filter:blur(6px)}
#toc.show{display:block}
#toc a{display:block;color:#555;text-decoration:none;padding:.3rem .4rem;
 border-radius:5px;border-left:2px solid transparent;margin-bottom:.1rem}
#toc a:hover{background:#f2ede3;color:var(--acc)}
#toc a.sub{padding-left:1.1rem;font-size:.8rem;color:#777}
#toc a.top{font-weight:600;color:var(--fg);margin-bottom:.7rem;
 padding-bottom:.55rem;border-bottom:1px solid #e6e2da;border-left:0;
 border-radius:0}
#toc a.top:hover{color:var(--acc);background:transparent}
#toc a.now{color:var(--acc);border-left-color:var(--acc);background:#f7f2e8}
#tocbtn{border:0;background:0;cursor:pointer;padding:.4rem .25rem;
 display:flex;flex-direction:column;gap:.22rem;align-items:center;flex:0 0 auto}
#tocbtn i{display:block;width:1.05rem;height:2px;background:#666;border-radius:2px}
#tocbtn:hover i{background:var(--acc)}
#tocveil{display:none;position:fixed;inset:0;background:#0005;z-index:51}
#tocveil.show{display:block}
@media(min-width:1180px){
 #toc{display:block;background:transparent;border-right:0;backdrop-filter:none}
 #tocveil{display:none!important}
 #tocbtn{display:none}
}
p{margin:0 0 1.1em;text-align:justify;position:relative}
p>.num{position:absolute;left:-4.6em;top:.25em;width:4em;text-align:right;
 font:.7rem/1.6 ui-monospace,monospace;color:var(--dim);user-select:none}
p:has(span.s.hl)>.num{color:#c48a00;font-weight:700}
em{font-style:italic}
p.verse{text-align:left;margin:0 0 .2em;padding-left:2em;
 font-style:italic;text-indent:0}
p.verse+p:not(.verse){margin-top:1.2em}
table{border-collapse:collapse;margin:1.6rem 0;font-size:.94em;width:100%}
td,th{border:1px solid #ddd;padding:.4em .6em;text-align:left;
 vertical-align:top}
th{background:#f7f4ee;font-weight:600}
figure{margin:2rem 0;text-align:center}
figure img{max-width:100%;height:auto;border-radius:4px;
 box-shadow:0 1px 8px #0002}
p.cite{margin-left:1.6em;padding-left:1em;border-left:2px solid #ddd;
 font-size:.96em;color:#333;text-align:left}
/* после p.cite: атрибуция приходит с обоими классами, и при равной
   специфичности выигрывает то правило, что стоит ниже */
p.auth{margin-left:1.6em;padding-left:1em;border-left:0;text-align:right;
 font-size:.86em;color:#777;font-style:italic}
span.s{scroll-margin-top:40vh}
span.s:target,span.s.hl{background:var(--hi)}
/* Бледная заливка — что попадёт в ссылку (всегда целое предложение,
   на половину сослаться нельзя). Поверх — накладка на том, что реально
   выделено мышкой. Два разных факта, два разных цвета. */
span.s.pick{background:#eef4fd;box-shadow:inset 0 -1px 0 #b9d0ee}
.rawsel{position:absolute;background:rgba(120,170,255,.34);border-radius:2px;
 pointer-events:none;z-index:1;mix-blend-mode:multiply}
sup.nt{font-size:.62em;line-height:0;user-select:none}
sup.nt a{color:var(--acc);text-decoration:none;padding:0 .15em}
ol.notes{margin-top:1rem;font-size:.9rem;color:#444;list-style:none;padding:0}
ol.notes li{margin:0 0 .7em;scroll-margin-top:40vh}
ol.notes li:target{background:var(--hi)}
a.back{color:var(--acc);text-decoration:none;font-weight:700;margin-right:.4em}
@media(max-width:820px){p>.num{position:static;display:block;text-align:left;
 margin-bottom:.2em}}

/* --- узкое окно: сначала просто сжимаем поиск (так же, как раньше),
   и только когда ужимать уже нечего — переносим метку на второй ряд --- */
@media(max-width:420px){
 #bar .in{flex-wrap:wrap;row-gap:.45rem}
 #home{order:-1}
 #q{flex:1 1 100%}
 #tagbox{flex:1 1 100%;order:9}
 #tag{width:100%}
 #tagmenu{left:0;right:0;min-width:0}
 #res{max-height:38vh}
}

/* --- сенсорный ввод: панель снизу, крупные кнопки, выбор тапом --- */
body.touch p span.s{cursor:pointer}
body.touch p{user-select:none;-webkit-user-select:none}
body.touch #tb{position:fixed!important;left:0!important;right:0!important;
 bottom:0!important;top:auto!important;border-radius:14px 14px 0 0;
 justify-content:center;padding:.55rem .6rem
 calc(.55rem + env(safe-area-inset-bottom));gap:.4rem;
 box-shadow:0 -4px 20px #0003}
body.touch #tb button{padding:.72rem .95rem;font-size:1rem}
body.touch #tb kbd{display:none}
body.touch #tb .anc{font-size:.8rem;padding-right:.5rem}
body.touch #tb .trg{display:none}
body.touch #acc{bottom:calc(4.6rem + env(safe-area-inset-bottom))}
body.touch #ok{bottom:calc(5.4rem + env(safe-area-inset-bottom))}
body.touch .rawsel{display:none}
body.touch span.s.pick{background:#cfe0ff;box-shadow:inset 0 -2px 0 #7ba7e8}

/* --- панель поиска (режим чтения с бумаги) --- */
#bar{position:fixed;top:0;left:0;right:0;z-index:40;background:#fffffff2;
 backdrop-filter:blur(6px);border-bottom:1px solid #e5e5e5;padding:.5rem 1rem;
 font-family:system-ui,sans-serif}
#bar .in{max-width:44em;margin:0 auto;display:flex;gap:.6rem;align-items:center}
#home{color:var(--acc);text-decoration:none;font-size:.85rem;white-space:nowrap;
 padding:.4rem .2rem}
#home:hover{text-decoration:underline}
/* min-width:0 обязателен: у flex-элемента минимальная ширина по умолчанию
   равна ширине содержимого, поэтому длинный placeholder распирал строку и
   выдавливал поле метки за край вместо того, чтобы дать поиску сжаться. */
#q{flex:1 1 8rem;min-width:0;font:.92rem system-ui,sans-serif;
 padding:.42rem .7rem;border:1px solid #ccc;border-radius:6px;outline:none}
#q:focus{border-color:var(--acc)}
#cnt{font-size:.78rem;color:var(--dim);white-space:nowrap}
#res{max-width:40em;margin:.4rem auto 0;max-height:46vh;overflow:auto}
#res div{padding:.45rem .6rem;border-radius:6px;cursor:pointer;
 font:.86rem/1.45 system-ui,sans-serif}
#res div:hover,#res div.on{background:#f2ede3}
#res b{font:.72rem ui-monospace,monospace;color:var(--acc);margin-right:.5em}
#res i{font-style:normal;background:var(--hi)}
#miss{padding:.5rem .6rem;font:.86rem system-ui,sans-serif;color:#a33}

/* --- всплывающая кнопка при выделении --- */
#tb{position:absolute;z-index:50;display:none;gap:.28rem;padding:.3rem;
 background:#1a1a1a;border-radius:9px;box-shadow:0 4px 16px #0004}
#tb.on{display:flex}
#tb button{font:.85rem/1 system-ui,sans-serif;color:#fff;background:#ffffff1a;
 border:0;border-radius:6px;padding:.42rem .6rem;cursor:pointer;
 display:flex;align-items:center;gap:.35rem;white-space:nowrap}
#tb button:hover{background:#ffffff33}
#tb button kbd{font:.62rem ui-monospace,monospace;opacity:.55}
#tb .anc{color:#ffd479;font:.72rem ui-monospace,monospace;padding:.42rem .3rem
 .42rem .5rem;align-self:center}
#tb .trg{color:#bbb;font-size:.78rem;max-width:11em;overflow:hidden;
 text-overflow:ellipsis}
#tb .add{background:#ffffff26}
#tb .add.has{background:#ffd47933;color:#ffd479}
#tagbox{position:relative;display:flex;align-items:center}
#tag{font:.85rem system-ui,sans-serif;width:9.5em;padding:.4rem 1.5rem .4rem .6rem;
 border:1px solid #ccc;border-radius:6px;outline:none;background:#fff}
#tag:focus{border-color:var(--acc)}
#tag::placeholder{color:#aaa}
#tagx{position:absolute;right:.35rem;border:0;background:0;cursor:pointer;
 color:#aaa;font-size:1rem;line-height:1;padding:.1rem .2rem;display:none}
#tagx:hover{color:#333}
#tagbox.filled #tagx{display:block}
#tagmenu{position:absolute;top:calc(100% + .3rem);right:0;min-width:12rem;
 max-height:15rem;overflow:auto;background:#fff;border:1px solid #ddd;
 border-radius:8px;box-shadow:0 6px 20px #0002;z-index:50;display:none;
 padding:.25rem;font:.85rem system-ui,sans-serif}
#tagmenu.on{display:block}
#tagmenu .row{display:flex;align-items:center;gap:.4rem;padding:.34rem .5rem;
 border-radius:5px;cursor:pointer}
#tagmenu .row:hover{background:#f2ede3}
#tagmenu .row span{flex:1;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap}
#tagmenu .row b{color:#bbb;font-weight:400;padding:0 .25rem}
#tagmenu .row b:hover{color:#c33}
#tagmenu .all{border-top:1px solid #eee;margin-top:.25rem;padding:.4rem .5rem;
 color:#c33;cursor:pointer;font-size:.8rem;border-radius:5px}
#tagmenu .all:hover{background:#fdeeee}
#tagmenu .none{padding:.4rem .5rem;color:#999;font-size:.8rem}

/* --- накопитель цитат --- */
#acc{position:fixed;left:50%;bottom:1.1rem;transform:translateX(-50%);z-index:45;
 display:none;align-items:center;gap:.45rem;max-width:min(94vw,50rem);
 padding:.45rem .55rem;background:#1a1a1a;border-radius:11px;
 box-shadow:0 6px 22px #0005;font:.84rem system-ui,sans-serif;color:#fff}
#acc.on{display:flex}
#acc .list{display:flex;gap:.3rem;overflow-x:auto;max-width:26rem;padding:.1rem}
#acc .chip{display:flex;align-items:center;gap:.3rem;white-space:nowrap;
 background:#ffffff1a;border-radius:6px;padding:.28rem .3rem .28rem .5rem;
 font:.74rem ui-monospace,monospace;color:#ffd479}
#acc .chip b{cursor:pointer;color:#ffffff8c;font-weight:400;padding:0 .25rem}
#acc .chip b:hover{color:#fff}
#acc button{font:.83rem system-ui,sans-serif;color:#fff;background:#ffffff1a;
 border:0;border-radius:6px;padding:.36rem .58rem;cursor:pointer}
#acc button:hover{background:#ffffff33}
#acc .clr{color:#ffffff8c;font-size:.78rem}

#hash{display:flex;align-items:center;gap:.3rem;font-size:.78rem;color:#666;
 cursor:pointer;user-select:none;white-space:nowrap}
#hash input{accent-color:var(--acc)}
#ok{position:fixed;left:50%;bottom:5.2rem;transform:translateX(-50%);z-index:60;
 background:#1a1a1a;color:#fff;padding:.55rem 1rem;border-radius:8px;
 font:.85rem system-ui,sans-serif;opacity:0;transition:opacity .18s;
 pointer-events:none}
#ok.on{opacity:.94}
@media print{#bar,#tb,#ok{display:none!important}body{padding-top:1rem}}
"""

JS = r"""
(function(){
var SENT = [].slice.call(document.querySelectorAll('span.s'));
/* Сенсорный ввод определяется возможностями, а не шириной экрана: на
   планшете экран широкий, а наведения и клавиатуры всё равно нет. */
var TOUCH = matchMedia('(hover: none) and (pointer: coarse)').matches;
if(TOUCH) addEventListener('DOMContentLoaded', function(){
  document.body.classList.add('touch');
});
var BASE = location.href.split('#')[0];
var byId = {}; SENT.forEach(function(e){ byId[e.id] = e; });

/* ---------- нормализация: зеркало normalize() из bookreg.py ---------- */
function norm(t){
  return t.toLowerCase().replace(/ё/g,'е')
    .replace(/[«»„“”‘’"']/g,'')
    .replace(/[\u2010-\u2015\u2212]/g,'-')
    .replace(/(^|\s)-+|-+(\s|$)/g,' ')
    .replace(/[^0-9a-zа-я\s-]/g,' ')
    .replace(/\s+/g,' ').trim();
}
/* pymorphy в браузере нет; обрезка основы до 5 букв ловит падежи */
function stem(w){ return w.length > 5 ? w.slice(0,5) : w; }
function like(a,b){ return b.indexOf(stem(a))===0 || a.indexOf(stem(b))===0; }

/* ---------- порядок и якоря ---------- */
function paraOf(id){ return id.split('.').slice(0,-1).join('.'); }

function anchorFor(list){
  if(!list.length) return null;
  var a = list[0].id, b = list[list.length-1].id;
  if(a === b) return a;
  return paraOf(a) === paraOf(b) ? a + '-' + b.split('.').pop() : a + '-' + b;
}

function resolve(h){
  var parts = h.split('-'), a = parts[0].trim(), b = (parts[1]||'').trim();
  var pel = document.getElementById(a);
  if(!b && pel && pel.tagName === 'P')
    return [].slice.call(pel.querySelectorAll('span.s'));
  var ia = SENT.indexOf(byId[a]);
  if(ia < 0) return [];
  if(!b) return [SENT[ia]];
  if(b.indexOf('.') === -1) b = a.split('.').slice(0,-1).concat(b).join('.');
  var ib = SENT.indexOf(byId[b]);
  if(ib < 0){                       /* конца нет — до конца абзаца */
    var p = SENT[ia].closest('p'), last = ia;
    while(last+1 < SENT.length && SENT[last+1].closest('p') === p) last++;
    ib = last;
  }
  if(ib < ia){ var t = ia; ia = ib; ib = t; }
  return SENT.slice(ia, ib+1);
}

function applyHash(){
  SENT.forEach(function(e){ e.classList.remove('hl'); });
  var h = decodeURIComponent(location.hash.slice(1));
  if(!h) return;
  var sel = resolve(h);
  sel.forEach(function(e){ e.classList.add('hl'); });
  if(sel.length) sel[0].scrollIntoView({block:'center'});
}
applyHash();
addEventListener('hashchange', applyHash);   /* иначе правка адреса не сработает */

/* ---------- буфер обмена ---------- */
function copyBoth(html, text){
  if(navigator.clipboard && window.ClipboardItem && location.protocol !== 'file:'){
    return navigator.clipboard.write([ new ClipboardItem({
      'text/html':  new Blob([html], {type:'text/html'}),
      'text/plain': new Blob([text], {type:'text/plain'})
    })]).catch(function(){ return legacy(html); });
  }
  return legacy(html);          /* file:// и старые браузеры */
}
function legacy(html){
  var d = document.createElement('div');
  d.setAttribute('contenteditable','true');
  d.style.cssText = 'position:fixed;left:-9999px;top:0;white-space:pre-wrap';
  d.innerHTML = html;
  document.body.appendChild(d);
  var s = getSelection(), keep = s.rangeCount ? s.getRangeAt(0) : null;
  var r = document.createRange(); r.selectNodeContents(d);
  s.removeAllRanges(); s.addRange(r);
  try { document.execCommand('copy'); } catch(e){}
  s.removeAllRanges(); if(keep) s.addRange(keep);   /* вернуть выделение */
  d.remove();
  return Promise.resolve();
}

/* ---------- выделение ---------- */
function picked(){
  var s = getSelection();
  if(!s.rangeCount || s.isCollapsed) return [];
  var r = s.getRangeAt(0);
  return SENT.filter(function(e){ return r.intersectsNode(e); });
}

/* триггер для М: если выделена часть предложения — берём именно её */
function trigger(list){
  var raw = (getSelection().toString() || lastRaw).replace(/\s+/g,' ').trim();
  var full = list.map(function(e){ return e.textContent; })
                 .join(' ').replace(/\s+/g,' ').trim();
  var t = (raw && raw.length < full.length) ? raw : full;
  var w = t.split(' ');
  return w.length > 8 ? w.slice(0,8).join(' ') + '…' : t;
}

/* ---------- панель над выделением ---------- */
var tb = document.createElement('div'); tb.id = 'tb';
tb.innerHTML = '<span class="anc"></span>' +
  '<button class="add" data-add="1">+ <kbd>+</kbd></button>' +
  '<button data-t="К">К <kbd>1</kbd></button>' +
  '<button data-t="?">? <kbd>2</kbd></button>' +
  '<button data-t="М">М <span class="trg"></span><kbd>3</kbd></button>';
document.body.appendChild(tb);
var acc = document.createElement('div'); acc.id = 'acc';
document.body.appendChild(acc);
var ok = document.createElement('div'); ok.id = 'ok';
document.body.appendChild(ok);

/* ---------- личное хранилище ----------
   Накопитель и метки не общие: у каждого участника свои, поэтому лежат
   в браузере, а не в репозитории. Накопитель переживает перезагрузку —
   собранные цитаты легко потерять случайным обновлением страницы. */
var KACC = 'br.acc.' + BOOK, KTAG = 'br.tags', KHASH = 'br.hash';
function ls(k, d){ try { return JSON.parse(localStorage.getItem(k)) ?? d; }
                   catch(e){ return d; } }
function save(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); }
                     catch(e){} }

var bag = ls(KACC, []);          /* якоря в накопителе */
var tags = ls(KTAG, []);         /* использованные метки */
var useHash = ls(KHASH, false);

/* Свой список вместо <datalist>: тот не даёт удалять записи по одной, а
   именно это и нужно — за полгода в личных метках копятся опечатки. */
function refreshTags(){
  var m = document.getElementById('tagmenu');
  if(!m) return;
  var q = (document.getElementById('tag').value || '').trim().toLowerCase();
  var show = tags.filter(function(t){ return !q || t.toLowerCase().indexOf(q) >= 0; });
  if(!tags.length){
    m.innerHTML = '<div class="none">Меток пока нет — впишите первую</div>';
    return;
  }
  m.innerHTML = show.map(function(t){
    return '<div class="row" data-pick="' + t + '"><span>' + t + '</span>' +
           '<b data-forget="' + t + '" title="Забыть метку">×</b></div>';
  }).join('') +
  (show.length ? '' : '<div class="none">Совпадений нет</div>') +
  '<div class="all" data-forgetall="1">Забыть все метки (' + tags.length + ')</div>';
}
function rememberTag(t){
  if(!t) return;
  tags = [t].concat(tags.filter(function(x){ return x !== t; })).slice(0, 60);
  save(KTAG, tags); refreshTags();
}
function forgetTag(t){
  tags = tags.filter(function(x){ return x !== t; });
  save(KTAG, tags); refreshTags();
}

var cur = [];          /* выбранные предложения — своё состояние, не выделение */
var lastRaw = '';      /* что именно было выделено внутри предложения */

/* Клик в поле метки или поиска снимает системное выделение — это поведение
   браузера, отменить его нельзя. Поэтому выбор хранится сам по себе и
   подсвечивается своим классом: визуально ничего не пропадает, и всё, что
   нужно для вставки, уже посчитано. */
function markPick(list){
  SENT.forEach(function(e){ e.classList.remove('pick'); });
  (list || []).forEach(function(e){ e.classList.add('pick'); });
}

/* Накладка поверх выделенного куска. Рисуется прямоугольниками из
   range.getClientRects(), а не оборачиванием в тег: DOM не трогается,
   значит не ломается ни разметка курсива, ни разбиение на предложения.
   Координаты документные, поэтому накладка едет вместе с текстом. */
function drawRaw(range){
  clearRaw();
  if(!range) return;
  var rects = range.getClientRects();
  for(var i = 0; i < rects.length; i++){
    var r = rects[i];
    if(r.width < 1 || r.height < 1) continue;
    var d = document.createElement('div');
    d.className = 'rawsel';
    d.style.left = (r.left + scrollX) + 'px';
    d.style.top = (r.top + scrollY) + 'px';
    d.style.width = r.width + 'px';
    d.style.height = r.height + 'px';
    document.body.appendChild(d);
  }
}
function clearRaw(){
  var old = document.querySelectorAll('.rawsel');
  for(var i = 0; i < old.length; i++) old[i].remove();
}

function hide(){
  tb.classList.remove('on');
  cur = []; lastRaw = '';
  markPick([]); clearRaw();
}

/* Панель ставится по координатам самих предложений, а не выделения:
   выделения может уже не быть, а предложения на месте всегда. */
function place(){
  if(!cur.length) return;
  var a = cur[0].getBoundingClientRect();
  var b = cur[cur.length - 1].getBoundingClientRect();
  var top = Math.min(a.top, b.top), bottom = Math.max(a.bottom, b.bottom);
  var left = cur.length === 1 ? a.left : Math.min(a.left, b.left);
  var right = cur.length === 1 ? a.right : Math.max(a.right, b.right);
  var w = tb.offsetWidth, h = tb.offsetHeight;
  var x = Math.min(Math.max(8, (left + right) / 2 - w / 2), innerWidth - w - 8);
  var y = top - h - 8;
  tb.style.left = (x + scrollX) + 'px';
  tb.style.top = ((y > 4 ? y : bottom + 8) + scrollY) + 'px';
}

/* Ядро выбора: одинаково для мыши и для тапа. Отличается только тем,
   откуда берётся список предложений и есть ли частичное выделение. */
function setCur(list, range){
  cur = list;
  lastRaw = range ? String(getSelection()) : '';
  markPick(list);
  drawRaw(range || null);
  tb.querySelector('.anc').textContent = anchorFor(list);
  tb.querySelector('.trg').textContent = '«' + trigger(list) + '»';
  tb.querySelector('.add').classList.toggle('has', bag.length > 0);
  tb.classList.add('on');
  if(!TOUCH) place();          /* на сенсоре панель прибита к низу экрана */
}

function show(){
  var list = picked();
  if(!list.length) return;        /* пусто — просто ничего не меняем */
  var sel = getSelection();
  setCur(list, sel.rangeCount ? sel.getRangeAt(0) : null);
}

function toast(t){ ok.textContent = t; ok.classList.add('on');
  clearTimeout(ok._t); ok._t = setTimeout(function(){ ok.classList.remove('on'); }, 1400); }

/* Якоря для вставки: сначала накопленные, потом текущее выделение.
   Один путь на оба случая, чтобы поведение не зависело от того, откуда
   нажали — из панели над текстом или из полоски накопителя. */
function anchors(){
  var a = bag.slice();
  if(cur.length){
    var one = anchorFor(cur);
    if(a.indexOf(one) < 0) a.push(one);
  }
  return a;
}

function emit(type){
  var list = anchors();
  if(!list.length) return;
  var tag = useHash ? (document.getElementById('tag').value || '')
                        .trim().replace(/^#+/, '') : null;
  /* метка у М со многими цитатами вместо триггера: три цитаты не держатся
     на одном обороте, и выписывать три триггера бессмысленно */
  var tail = (type === 'М' && list.length === 1 && cur.length)
             ? ' «' + trigger(cur) + '»' : '';
  var hash = useHash ? ' #' + tag : '';

  var links = list.map(function(a){
    return '[<a href="' + BASE + '#' + a + '">' + a + '</a>]';
  }).join(' ');
  var plain = list.map(function(a){
    return '[' + a + '](' + BASE + '#' + a + ')';
  }).join(' ');

  /* Метка включена, но не задана — строка кончается решёткой без пробела,
     чтобы метку можно было дописать прямо в документе, вплотную к #.
     Иначе — &nbsp;, а не пробел: обычный пробел в конце HTML-фрагмента
     считается незначащим и при вставке в Docs схлопывается. */
  var open = useHash && !tag;
  var html = type + ' ' + links + tail + hash + (open ? '' : '&nbsp;');
  var text = type + ' ' + plain + tail + hash + (open ? '' : ' ');
  copyBoth(html, text).then(function(){
    rememberTag(tag);
    toast('Скопировано:  ' + type + ' ' + list.join(' ') + hash);
    if(bag.length){ bag = []; save(KACC, bag); drawAcc(); }
    getSelection().removeAllRanges(); hide();
  });
}

/* ---------- накопитель ---------- */
function drawAcc(){
  acc.classList.toggle('on', bag.length > 0);
  if(!bag.length) return;
  acc.innerHTML =
    '<span class="clr">Собрано: ' + bag.length + '</span>' +
    '<div class="list">' + bag.map(function(a, i){
      return '<span class="chip">' + a + '<b data-rm="' + i + '">×</b></span>';
    }).join('') + '</div>' +
    '<button data-t="К">К</button><button data-t="?">?</button>' +
    '<button data-t="М">М</button>' +
    '<button class="clr" data-clear="1">Очистить</button>';
}
function addToBag(){
  if(!cur.length) return;
  var a = anchorFor(cur);
  if(bag.indexOf(a) < 0){ bag.push(a); save(KACC, bag); }
  drawAcc();
  toast('В накопителе: ' + bag.length);
  getSelection().removeAllRanges(); hide();
}
acc.addEventListener('mousedown', function(e){ e.preventDefault(); });
acc.addEventListener('click', function(e){
  var rm = e.target.closest('[data-rm]');
  if(rm){ bag.splice(+rm.dataset.rm, 1); save(KACC, bag); drawAcc(); return; }
  if(e.target.closest('[data-clear]')){ bag = []; save(KACC, bag); drawAcc(); return; }
  var b = e.target.closest('button[data-t]');
  if(b) emit(b.dataset.t);
});
drawAcc();

tb.addEventListener('mousedown', function(e){ e.preventDefault(); });
tb.addEventListener('click', function(e){
  if(e.target.closest('[data-add]')){ addToBag(); return; }
  var b = e.target.closest('button[data-t]');
  if(b) emit(b.dataset.t);
});


/* Клик в верхнюю строку снимает выделение в тексте. Проверять
   document.activeElement в selectionchange бесполезно: выделение снимается
   раньше, чем переезжает фокус. Поэтому флаг взводится на pointerdown —
   он приходит до всего остального. */
var barBusy = false;
var bar = document.getElementById('bar');
bar && bar.addEventListener('pointerdown', function(){ barBusy = true; });
bar && bar.addEventListener('focusout', function(){
  setTimeout(function(){
    if(!document.activeElement || !document.activeElement.closest('#bar'))
      barBusy = false;
  }, 0);
});
if(!TOUCH){
  document.addEventListener('selectionchange', function(){
    if(barBusy) return;
    clearTimeout(show._t); show._t = setTimeout(show, 60);
  });
  /* Явный сброс: щелчок по тексту без протяжки. Только так выбор снимается
     сам — всё остальное (фокус в поле, прокрутка) его не трогает. */
  document.addEventListener('pointerup', function(e){
    if(e.target.closest('#bar,#tb,#acc')) return;
    setTimeout(function(){
      var s = getSelection();
      if(cur.length && (!s.rangeCount || s.isCollapsed)) hide();
    }, 10);
  });
}

/* ---------- выбор тапом (сенсорные экраны) ----------
   Протягивать текст пальцем неудобно, а системное меню выделения перекрывает
   нашу панель. Поэтому на сенсоре текст помечен как невыделяемый, а выбор
   делается нажатиями: тап — предложение, второй тап — диапазон до него.
   Ссылка всё равно указывает на целое предложение, точнее нельзя, так что
   ограничение модели данных здесь совпадает с тем, что удобно пальцем. */
if(TOUCH){
  var tap = null;
  document.addEventListener('pointerdown', function(e){
    if(e.target.closest('#bar,#tb,#acc')) { tap = null; return; }
    tap = { x: e.clientX, y: e.clientY, t: Date.now(), sy: scrollY };
  }, {passive: true});

  document.addEventListener('pointerup', function(e){
    var st = tap; tap = null;
    if(!st) return;
    /* Отсев прокрутки: сдвинулся палец или уехала страница — это не тап.
       Долгое нажатие тоже пропускаем: оно вызывает системное меню. */
    if(Math.abs(e.clientX - st.x) > 12 || Math.abs(e.clientY - st.y) > 12) return;
    if(Math.abs(scrollY - st.sy) > 4) return;
    if(Date.now() - st.t > 500) return;

    var el = e.target.closest('span.s');
    if(!el){ if(cur.length) hide(); return; }
    tapSentence(el);
  });
}

function tapSentence(el){
  var i = SENT.indexOf(el);
  if(i < 0) return;
  if(!cur.length){ setCur([el]); return; }
  var a = SENT.indexOf(cur[0]), b = SENT.indexOf(cur[cur.length - 1]);
  if(i >= a && i <= b){
    /* тап внутри уже выбранного: одно предложение — снять, диапазон — сжать */
    if(cur.length === 1) hide(); else setCur([el]);
    return;
  }
  setCur(SENT.slice(Math.min(i, a), Math.max(i, b) + 1));
}
addEventListener('scroll', function(){ if(cur.length) place(); }, {passive:true});

/* Подсказка в поиске одна и та же везде, просто укорачивается ровно
   настолько, насколько нужно. Раньше на узком экране появлялось «Поиск» —
   другое слово, из-за чего казалось, что на телефоне другая программа.
   Ширина замеряется по-настоящему, а не по порогам: шрифт и масштаб у всех
   разные. */
var PH = ['Найти цитату или номер — 1.14.2 …',
          'Найти цитату или номер',
          'Найти цитату'];
function fitPlaceholder(){
  if(!q) return;
  var st = getComputedStyle(q);
  var room = q.clientWidth
           - parseFloat(st.paddingLeft) - parseFloat(st.paddingRight) - 4;
  var ctx = fitPlaceholder.ctx ||
            (fitPlaceholder.ctx = document.createElement('canvas').getContext('2d'));
  ctx.font = st.fontSize + ' ' + st.fontFamily;
  for(var i = 0; i < PH.length; i++){
    if(ctx.measureText(PH[i]).width <= room || i === PH.length - 1){
      q.placeholder = PH[i];
      return;
    }
  }
}
addEventListener('resize', fitPlaceholder);
addEventListener('orientationchange', function(){ setTimeout(fitPlaceholder, 120); });

/* Флажок — тоже <input>, поэтому проверка по tagName глушила клавиши,
   пока фокус стоял на нём: нажатие не проходило, а на флажке оставалась
   рамка фокуса. Отличаем поля ввода текста от переключателей. */
function typing(){
  var a = document.activeElement;
  if(!a) return false;
  if(a.isContentEditable || a.tagName === 'TEXTAREA') return true;
  if(a.tagName !== 'INPUT') return false;
  return !/^(checkbox|radio|button|submit|reset|range|file|color)$/i
          .test(a.type || 'text');
}

/* ---------- клавиши: обе раскладки и цифры ---------- */
/* «/» отдан поиску, а вопросу оставлены «2» и «?» (то есть Shift+/):
   раньше «/» значил то одно, то другое в зависимости от того, выделено
   что-нибудь или нет, и это путало. */
var KEY = { '1':'К','k':'К','к':'К', '2':'?','?':'?', '3':'М','m':'М','м':'М' };
var ADD = { '+':1, '=':1, '0':1 };
addEventListener('keydown', function(e){
  if(e.metaKey || e.ctrlKey || e.altKey) return;
  if(typing()) return;
  if(ADD[e.key] && cur.length){ e.preventDefault(); addToBag(); return; }
  var t = KEY[e.key.toLowerCase()];
  if(t && (cur.length || bag.length)){ e.preventDefault(); emit(t); }
  if(e.key === 'Escape'){
    if(cur.length){ getSelection().removeAllRanges(); hide(); }
    else if(bag.length){ bag = []; save(KACC, bag); drawAcc(); }
  }
});

/* ---------- оглавление ---------- */
var toc = document.getElementById('toc'),
    tocBtn = document.getElementById('tocbtn'),
    tocVeil = document.getElementById('tocveil');
var WIDE = matchMedia('(min-width: 1180px)');

function tocSet(on){
  if(!toc) return;
  toc.classList.toggle('show', on);
  tocVeil && tocVeil.classList.toggle('show', on && !WIDE.matches);
  document.body.style.overflow = (on && !WIDE.matches) ? 'hidden' : '';
}
tocBtn && tocBtn.addEventListener('click', function(){
  tocSet(!toc.classList.contains('show'));
});
tocVeil && tocVeil.addEventListener('click', function(){ tocSet(false); });
/* переход по ссылке закрывает панель, иначе она заслоняет то место,
   к которому только что перешли */
toc && toc.addEventListener('click', function(e){
  if(e.target.closest('a') && !WIDE.matches) tocSet(false);
});

/* подсветка текущей главы: следим за заголовками, а не за прокруткой —
   так не приходится пересчитывать положение на каждый пиксель */
var heads = [].slice.call(
  document.querySelectorAll('h2.ch, h3.ch, span.hook'));
var links = {};
toc && [].forEach.call(toc.querySelectorAll('a[data-c]'), function(a){
  links[a.dataset.c] = a;
});
function markNow(id){
  for(var k in links) links[k].classList.toggle('now', k === id);
  var a = links[id];
  if(a && toc && toc.classList.contains('show')){
    var r = a.getBoundingClientRect(), t = toc.getBoundingClientRect();
    if(r.top < t.top + 40 || r.bottom > t.bottom - 40)
      a.scrollIntoView({block:'center'});
  }
}
/* Текущая глава — последний заголовок выше верхней трети экрана.
   Считаем на прокрутке с ограничением по кадру: заголовков десятки,
   перебор дешевле наблюдателя и не зависит от порядка событий. */
function tocSync(){
  if(!heads.length) return;
  var line = innerHeight * 0.3, cur = heads[0];
  for(var i = 0; i < heads.length; i++){
    if(heads[i].getBoundingClientRect().top <= line) cur = heads[i];
    else break;
  }
  /* id заголовка главы — «c1», якоря подзаголовка — «p0.1»; в оглавлении
     ключи хранятся без первой буквы у глав и целиком у подзаголовков */
  var id = cur.id;
  markNow(links[id] ? id : id.slice(1));
}
var tocTick = false;
addEventListener('scroll', function(){
  if(tocTick) return;
  tocTick = true;
  requestAnimationFrame(function(){ tocTick = false; tocSync(); });
}, {passive: true});
tocSync();

/* ---------- переключатель меток ---------- */
var hashon = document.getElementById('hashon');
var tagIn = document.getElementById('tag');
function applyHash_(){
  /* Поле прячется, но галочка видна всегда — она и служит подсказкой, что
     возможность есть. Это важно: localStorage у каждого адреса свой, и на
     новом домене галочка снята. Пропади ещё и она, выглядело бы, будто
     метки вовсе не предусмотрены. */
  if(tagBox) tagBox.style.display = useHash ? '' : 'none';
  if(tagIn) tagIn.disabled = !useHash;
  if(hashon) hashon.checked = useHash;
  if(!useHash) closeTags();
}
function setHash(on){
  useHash = !!on;
  save(KHASH, useHash);
  applyHash_();
}
if(hashon){
  hashon.addEventListener('change', function(){
    setHash(hashon.checked);
    if(useHash && !cur.length) tagIn.focus();
  });
}
/* инициализация перенесена в конец: здесь tagBox ещё не присвоен */

/* ---------- выпадающий список меток ---------- */
var tagBox = document.getElementById('tagbox');
var tagMenu = document.getElementById('tagmenu');
var tagX = document.getElementById('tagx');

function tagState(){
  if(tagBox) tagBox.classList.toggle('filled', !!tagIn.value.trim());
}
function openTags(){ refreshTags(); tagMenu && tagMenu.classList.add('on'); }
function closeTags(){ tagMenu && tagMenu.classList.remove('on'); }

tagIn && tagIn.addEventListener('focus', openTags);
tagIn && tagIn.addEventListener('input', function(){ tagState(); openTags(); });

tagX && tagX.addEventListener('click', function(e){
  e.preventDefault();
  tagIn.value = ''; tagState(); refreshTags(); tagIn.focus();
});

tagMenu && tagMenu.addEventListener('mousedown', function(e){ e.preventDefault(); });
tagMenu && tagMenu.addEventListener('click', function(e){
  var f = e.target.closest('[data-forget]');
  if(f){ forgetTag(f.dataset.forget); return; }
  if(e.target.closest('[data-forgetall]')){
    if(confirm('Забыть все метки? Уже вставленные заметки не изменятся.')){
      tags = []; save(KTAG, tags); refreshTags();
    }
    return;
  }
  var r = e.target.closest('[data-pick]');
  if(r){
    tagIn.value = r.dataset.pick; tagState(); closeTags();
    rememberTag(tagIn.value);
    tagIn.blur(); barBusy = false;
  }
});

document.addEventListener('pointerdown', function(e){
  if(!e.target.closest('#tagbox')) closeTags();
});

/* Enter в поле метки только закрывает набор: метка запоминается, фокус
   уходит из поля, и дальше работают 1/2/3. Раньше Enter вставлял сразу
   и всегда как «К» — тип за человека выбирать нельзя. */
tagIn && tagIn.addEventListener('keydown', function(e){
  /* та же Shift+3 внутри поля — выключить метки и вернуться к тексту */
  if(e.key === '#' || e.key === '№'){
    e.preventDefault();
    /* без stopPropagation событие дойдёт до window, там фокус уже снят,
       охрана пропустит — и метки включатся обратно тем же нажатием */
    e.stopPropagation();
    closeTags(); tagIn.blur(); barBusy = false;
    setHash(false);
    toast('Метки выключены');
    return;
  }
  if(e.key === 'Enter' || e.key === 'Escape'){
    e.preventDefault();
    rememberTag(tagIn.value.trim().replace(/^#+/, ''));
    tagState(); closeTags();
    tagIn.blur();
    barBusy = false;
    if(cur.length) toast('Метка: ' + (tagIn.value.trim() || '—') +
                         '   теперь 1, 2 или 3');
  }
});

/* ---------- поиск (чтение с бумаги) ---------- */
var q = document.getElementById('q'),
    res = document.getElementById('res'),
    cnt = document.getElementById('cnt');

function find(query){
  var ws = norm(query).split(' ').filter(Boolean);
  if(!ws.length) return [];
  var out = [];
  for(var i=0;i<IDX.length;i++){
    var sw = IDX[i][1].split(' '), k = 0, first = -1, last = -1;
    for(var j=0;j<sw.length && k<ws.length;j++){
      if(like(ws[k], sw[j])){ if(first<0) first=j; last=j; k++; }
    }
    if(k < ws.length) continue;
    out.push([ (last-first+1)/ws.length, first, IDX[i][0] ]);
  }
  out.sort(function(a,b){ return a[0]-b[0] || a[1]-b[1]; });
  return out.slice(0, 12).map(function(h){ return h[2]; });
}

function mark(text, query){
  var ws = norm(query).split(' ').filter(Boolean);
  return text.split(/(\s+)/).map(function(tok){
    var n = norm(tok);
    return n && ws.some(function(w){ return like(w, n); })
      ? '<i>' + tok.replace(/[<>&]/g,'') + '</i>'
      : tok.replace(/[<>&]/g,'');
  }).join('');
}

function pickList(list){
  if(!list || !list.length) return;
  list[0].scrollIntoView({block:'center'});
  var r = document.createRange();
  r.setStartBefore(list[0]);
  r.setEndAfter(list[list.length-1]);
  var s = getSelection(); s.removeAllRanges(); s.addRange(r);
  res.innerHTML = ''; cnt.textContent = '';
  setTimeout(show, 30);          /* выделение → та же панель, что и от мыши */
}
function pick(id){ pickList(byId[id] ? [byId[id]] : []); }

/* ---------- поиск по номеру ---------- */
/* запрос вида 1.14 / 1.14.3 / 1.14.2-4 / 4.11.3-4.12.1, а также
   целый адрес со ссылкой — из него берётся часть после # */
function asAnchor(v){
  var h = v.indexOf('#');
  if(h >= 0) v = v.slice(h+1);
  v = v.replace(/[\u2010-\u2015\u2212]/g,'-').replace(/[\s,]/g,'').replace(/\?.*$/,'');
  return /^\d+(\.\d+)*(-\d+(\.\d+)*)?$/.test(v) ? v : null;
}
function byNumber(v){
  var out = [], seen = {};
  var exact = resolve(v);
  if(exact.length){
    out.push({ anchor: v, list: exact, exact: true });
    seen[v] = 1;
  }
  /* всё, что начинается с введённого на границе точки: 1.14 → 1.14.1, 1.14.2 */
  for(var i=0;i<SENT.length && out.length<13;i++){
    var id = SENT[i].id;
    if(seen[id]) continue;
    if(id === v || id.indexOf(v + '.') === 0)
      out.push({ anchor: id, list: [SENT[i]], exact: false });
  }
  return out;
}

var sel = -1, ids = [], hits = [];
q && q.addEventListener('input', function(){
  var v = q.value.trim();
  sel = -1;

  var num = asAnchor(v);
  if(num){
    hits = byNumber(num);
    ids = hits.map(function(h){ return h.anchor; });
    cnt.textContent = hits.length ? 'по номеру' : '';
    if(!hits.length){
      res.innerHTML = '<div id="miss">Нет такого номера.</div>';
      return;
    }
    res.innerHTML = hits.map(function(h){
      var txt = h.list.map(function(e){ return e.textContent; }).join(' ');
      var tag = h.exact && h.list.length > 1
                ? ' <span style="color:#9a9a9a">' + h.list.length + ' предл.</span>' : '';
      return '<div data-id="' + h.anchor + '"><b>' + h.anchor + '</b>' + tag + ' ' +
             txt.slice(0,150).replace(/[<>&]/g,'') + (txt.length>150?'…':'') + '</div>';
    }).join('');
    return;
  }

  if(v.length < 3){ res.innerHTML=''; cnt.textContent=''; return; }
  hits = [];
  ids = find(v);
  cnt.textContent = ids.length ? ids.length + ' совпад.' : '';
  if(!ids.length){
    res.innerHTML = '<div id="miss">Ничего не найдено. ' +
      'Попробуйте другие слова — возможно, в книге они иные.</div>';
    return;
  }
  res.innerHTML = ids.map(function(id){
    return '<div data-id="' + id + '"><b>' + id + '</b>' +
           mark(byId[id].textContent, v) + '</div>';
  }).join('');
});
function choose(anchor){
  var list = resolve(anchor);
  pickList(list.length ? list : (byId[anchor] ? [byId[anchor]] : []));
}
res && res.addEventListener('click', function(e){
  var d = e.target.closest('div[data-id]');
  if(d) choose(d.dataset.id);
});
q && q.addEventListener('keydown', function(e){
  var items = res.querySelectorAll('div[data-id]');
  if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
    e.preventDefault();
    if(!items.length) return;
    sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items.forEach(function(x,i){ x.classList.toggle('on', i===sel); });
    items[sel].scrollIntoView({block:'nearest'});
  } else if(e.key === 'Enter'){
    e.preventDefault();
    if(items.length) choose(items[sel < 0 ? 0 : sel].dataset.id);
  } else if(e.key === 'Escape'){
    q.value = ''; res.innerHTML = ''; cnt.textContent = ''; q.blur();
  }
});
addEventListener('keydown', function(e){
  if(e.metaKey || e.ctrlKey || e.altKey) return;
  if(typing()) return;

  if(e.key === '/'){ e.preventDefault(); q && q.focus(); q && q.select(); return; }

  /* Shift+3 — физически одна клавиша в обеих раскладках, но символ разный:
     «#» на латинице, «№» на кириллице. Принимаем оба, чтобы не заставлять
     переключать язык. Повторное нажатие в поле метки — выключает. */
  if(e.key === '#' || e.key === '№'){
    e.preventDefault();
    if(!useHash){ setHash(true); }
    tagIn && tagIn.focus(); tagIn && tagIn.select();
    return;
  }
});

/* Инициализация — последней строкой. Раньше applyHash_() вызывался выше
   объявления tagBox: из-за подъёма var переменная была undefined, проверка
   не проходила, и поле метки не пряталось при загрузке. Молча. */
refreshTags(); tagState(); applyHash_(); fitPlaceholder();
})();
"""
def write_images(reg, out):
    """
    Картинки кладутся отдельными файлами рядом с читалкой, а не встраиваются
    в HTML. У «Манифеста» их 111 КБ — в base64 это 152 КБ поверх страницы,
    и на книге с полусотней иллюстраций читалка стала бы неподъёмной.
    Отдельные файлы к тому же кешируются браузером по одному.
    """
    used = {i["href"] for i in reg.get("images", [])}
    if not used:
        return {}
    d = Path(out).parent / (reg["book"]["id"] + "_img")
    d.mkdir(parents=True, exist_ok=True)
    written = {}
    for href in sorted(used):
        got = reg.get("_binaries", {}).get(href)
        if not got:
            continue
        ctype, data = got
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/gif": ".gif", "image/svg+xml": ".svg",
               "image/webp": ".webp"}.get(ctype, ".bin")
        name = re.sub(r"[^\w.-]", "_", href)
        if not name.lower().endswith(ext):
            name = Path(name).stem + ext
        try:
            (d / name).write_bytes(base64.b64decode(data))
        except Exception:
            continue
        written[href] = d.name + "/" + name
    return written


def write_html(reg, out):
    e = html_mod.escape
    chap = {c["id"]: c for c in reg["chapters"]}
    marks = {}
    for m in reg["marks"]:
        marks.setdefault((m["chapter"], m["after"]), []).append(m)
    sent = {s["id"]: s for s in reg["sentences"]}
    notes = reg.get("notes", {})
    used = []
    imgs = write_images(reg, out)
    pics = {}
    for i in reg.get("images", []):
        pics.setdefault((i["chapter"], i["after"]), []).append(i["href"])
    tabs = {}
    for i in reg.get("tables", []):
        tabs.setdefault((i["chapter"], i["after"]), []).append(i["rows"])

    def render(t, refs, fmts, back):
        """
        Текст с врезанным оформлением и маркерами сносок.

        Собирается по событиям в позициях, а не вложенной заменой: курсив,
        сноска и конец другого курсива могут прийтись на одну точку, и порядок
        должен быть один — сначала закрыть, потом сноска, потом открыть.
        """
        if not refs and not fmts:
            return e(t)
        ev = {}
        for f in fmts or []:
            ev.setdefault(f["pos"], {"c": 0, "o": [], "n": []})["o"].append(f)
            k = min(f["pos"] + f["len"], len(t))
            ev.setdefault(k, {"c": 0, "o": [], "n": []})["c"] += 1
        for r in refs or []:
            k = max(0, min(r["pos"], len(t)))
            ev.setdefault(k, {"c": 0, "o": [], "n": []})["n"].append(r)

        out, stack, prev = [], [], 0
        for i in sorted(ev):
            out.append(e(t[prev:i]))
            prev = i
            v = ev[i]
            for _ in range(v["c"]):
                if stack:
                    out.append("</" + stack.pop() + ">")
            for r in v["n"]:
                nid = r["note"]
                nd = notes.get(nid) or {}
                label = (nd.get("num") or "*").strip("[] ") or "*"
                used.append((nid, label, back))
                out.append(f'<sup class="nt"><a href="#{nid}" '
                           f'title="{e(nd.get("text", ""))[:400]}">'
                           f'{e(label)}</a></sup>')
            for f in sorted(v["o"], key=lambda x: -x["len"]):
                tag = FMT_TAG.get(f["t"], "span")
                out.append("<" + tag + ">")
                stack.append(tag)
        out.append(e(t[prev:]))
        while stack:
            out.append("</" + stack.pop() + ">")
        return "".join(out)

    L = ['<!doctype html><html lang="ru"><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         f'<title>{e(reg["book"]["title"])}</title>', f"<style>{CSS}</style>",
         '<div id="bar"><div class="in">'
         '<button id="tocbtn" title="Оглавление" aria-label="Оглавление">'
         '<i></i><i></i><i></i></button>'
         '<a id="home" href="../index.html" title="Список книг">&larr; Книги</a>'
         '<input id="q" placeholder="Найти цитату или номер — 1.14.2 …" '
         'autocomplete="off"><span id="cnt"></span>'
         '<label id="hash" title="Добавлять метку в заметку">'
         '<input type="checkbox" id="hashon">метка</label>'
         '<span id="tagbox" style="display:none">'
         '<input id="tag" placeholder="Без метки" '
         'autocomplete="off">'
         '<button id="tagx" title="Очистить поле">&times;</button>'
         '<div id="tagmenu"></div></span></div>'
         '<div id="res"></div></div>',
         f'<h1 id="top">{e(reg["book"]["title"])}</h1>']
    seen, toc, fronts, marklist = set(), [], [], []
    for p in reg["paragraphs"]:
        c = p["chapter"]
        if c not in seen:
            # Заголовки всех глав-предков, а не только той, которой принадлежит
            # абзац: глава-контейнер своих абзацев не имеет, и её название
            # иначе не выводится вообще.
            parts = c.split(".")
            for k in range(1, len(parts) + 1):
                anc = ".".join(parts[:k])
                if anc in seen:
                    continue
                seen.add(anc)
                ch = chap.get(anc, {})
                t = ch.get("title", "")
                if not t:
                    continue
                tag = "h2" if k == 1 else "h3"
                toc.append({"id": anc, "title": t, "sub": k > 1})
                L.append(f'<{tag} id="c{anc}" class="ch">'
                         + render(t, ch.get("refs", []), ch.get("fmt", []),
                                  f"c{anc}") + f"</{tag}>")
        for mk in marks.get((c, p["n"] - 1), []):
            # Передняя часть — заголовок раздела, а не заметка на полях:
            # иначе получается мелкий курсив перед крупным жирным, и в
            # оглавление она не попадает.
            if mk.get("front"):
                fid = "cf%d" % len(fronts)
                fronts.append(fid)
                toc.append({"id": fid, "title": mk["text"], "sub": False})
                L.append(f'<h2 id="{fid}" class="ch front">'
                         f'{e(mk["text"])}</h2>')
            else:
                # подзаголовок вроде «а) Феодальный социализм»: номера не
                # получает, но в оглавлении нужен — иначе третья глава
                # «Манифеста» выглядит сплошным куском
                mid = "m%d" % len(marklist)
                marklist.append(mid)
                toc.append({"id": mid, "title": mk["text"], "sub": True})
                L.append(f'<h4 id="{mid}" class="mark">{e(mk["text"])}</h4>')
        for href in pics.get((c, p["n"] - 1), []):
            if href in imgs:
                L.append(f'<figure><img src="{imgs[href]}" alt="" '
                         f'loading="lazy"></figure>')
        for rows in tabs.get((c, p["n"] - 1), []):
            L.append("<table>" + "".join(
                "<tr>" + "".join(f"<{k}>{e(v)}</{k}>" for k, v in row)
                + "</tr>" for row in rows) + "</table>")
        spans = " ".join(
            f'<span class="s" id="{sid}">'
            + render(sent[sid]["text"], sent[sid].get("notes", []),
                     sent[sid].get("fmt", []), sid)
            + "</span>"
            for sid in p["sentences"])
        # Абзац целиком полужирный и короткий — это заголовок, набранный
        # абзацем: в FB2 так часто оформляют подразделы. Своего номера при
        # этом не теряет, на него можно ссылаться.
        fm = p.get("fmt") or []
        subh = (len(fm) == 1 and fm[0]["t"] == "s" and fm[0]["pos"] == 0
                and fm[0]["len"] >= len(p["text"]) - 1
                and len(p["text"]) < 90)
        if subh:
            toc.append({"id": "p" + p["id"], "title": p["text"], "sub": True})
        cl = (["cite"] if p.get("cite") else []) + \
             (["auth"] if p.get("author") else []) + \
             (["subh"] if subh else []) + \
             (["verse"] if p.get("verse") else [])
        cls = f' class="{" ".join(cl)}"' if cl else ""
        anchor = f'<span id="p{p["id"]}" class="hook"></span>' if subh else ""
        L.append(f'{anchor}<p id="{p["id"]}"{cls}>'
                 f'<span class="num">{p["id"]}</span>{spans}</p>')

    # Картинка выводится перед абзацем с номером after+1. Если такого абзаца
    # нет — она стоит в конце главы, и её нужно вывести отдельно, иначе она
    # молча потеряется.
    placed = {(p["chapter"], p["n"] - 1) for p in reg["paragraphs"]}
    for (c, after), hrefs in sorted(pics.items()):
        if (c, after) in placed:
            continue
        for href in hrefs:
            if href in imgs:
                L.append(f'<figure><img src="{imgs[href]}" alt="" '
                         f'loading="lazy"></figure>')

    if used:
        L.append('<h2 id="notes">Примечания</h2><ol class="notes">')
        seen_n = set()
        for nid, label, back in used:
            if nid in seen_n:
                continue
            seen_n.add(nid)
            nd = notes.get(nid) or {}
            L.append(f'<li id="{nid}"><a class="back" href="#{back}">'
                     f'{e(label)}</a> '
                     + render(nd.get("text", ""), nd.get("refs", []),
                              nd.get("fmt", []), nid)
                     + "</li>")
        L.append("</ol>")
    # Первым пунктом — название книги: возврат в начало. Нужен всегда, но
    # особенно там, где вступление без заголовка и своего пункта не имеет
    # (так устроен «Манифест»).
    nav = ('<a href="#top" data-c="top" class="top">'
           + e(reg["book"]["title"]) + '</a>')
    nav += "".join(
        '<a href="#{h}" data-c="{h}"{cls}>{t}</a>'.format(
            # «c» приписывается только чистым номерам глав: у подзаголовков
            # («p1.1») и передней части («f0») id уже готовый
            h=x["id"] if x["id"][0].isalpha() else "c" + x["id"],
            cls=' class="sub"' if x["sub"] else "",
            t=e(re.sub(r"<[^>]+>", "", x["title"])))
        for x in toc)
    L.insert(5, f'<div id="tocveil"></div><nav id="toc">{nav}</nav>')

    idx = [[s["id"], s["norm"]] for s in reg["sentences"]]
    L.append("<script>var BOOK=" +
             json.dumps(reg["book"]["id"]) + ";</script>")
    L.append("<script>var IDX=" +
             json.dumps(idx, ensure_ascii=False, separators=(",", ":")) +
             ";</script>")
    L.append(f"<script>{JS}</script>")
    Path(out).write_text("\n".join(L), encoding="utf-8")


# ===========================================================================

INDEX_CSS = """
:root{--fg:#1a1a1a;--dim:#8a8a8a;--acc:#b06000;--line:#e6e2da}
body{max-width:44rem;margin:0 auto;padding:4rem 1.5rem 6rem;background:#fffdfa;
 color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',
 Roboto,sans-serif}
h1{font-size:1.6rem;margin:0 0 .4rem}
.sub{color:#666;margin:0 0 2.5rem}
a.bk{display:block;text-decoration:none;color:inherit;padding:1rem 1.1rem;
 margin:0 0 .7rem;border:1px solid var(--line);border-radius:10px;
 background:#fff;transition:border-color .15s}
a.bk:hover{border-color:var(--acc)}
a.bk .t{font-size:1.08rem;font-weight:600;margin-bottom:.15rem}
a.bk .a{color:#666;font-size:.9rem}
a.bk .n{color:var(--dim);font-size:.8rem;margin-top:.45rem;
 font-family:ui-monospace,monospace}
.lock{color:#2a7f3e}.nolock{color:#b06000}
footer{margin-top:2.5rem;padding-top:1.2rem;border-top:1px solid var(--line);
 font-size:.88rem;color:#666}
footer a{color:var(--acc);text-decoration:none}
footer a:hover{text-decoration:underline}
footer .sep{color:var(--dim);margin:0 .6rem}
"""


def write_index(regs, out):
    """Страница со списком книг — точка входа для участников."""
    e = html_mod.escape
    cards = []
    for reg, locked in regs:
        b = reg["book"]
        ch = len([c for c in reg["chapters"] if c["id"] != "0"])
        cards.append(
            f'<a class="bk" href="books/{b["id"]}.html">'
            f'<div class="t">{e(b["title"])}</div>'
            f'<div class="a">{e(", ".join(b.get("authors") or []))}</div>'
            f'<div class="n">глав {ch} · абзацев {len(reg["paragraphs"])} · '
            f'предложений {len(reg["sentences"])} · '
            + ('<span class="lock">номера закреплены</span>' if locked
               else '<span class="nolock">номера ещё могут измениться</span>')
            + "</div></a>")
    Path(out).write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Книги</title><style>' + INDEX_CSS + '</style>'
        '<h1>Книги</h1><p class="sub">Выделите текст — появится панель '
        'с кнопками К, ? и М. Или найдите цитату по словам или номеру.</p>'
        + "".join(cards)
        + '<footer><a href="reader.html">Как делать заметки</a>'
          '<span class="sep">·</span>'
          '<a href="docs.html">Документация для сопровождающего</a></footer>',
        encoding="utf-8")


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(prog="bookreg")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="собрать реестр из книги")
    b.add_argument("file")
    b.add_argument("--id", default="")
    b.add_argument("-o", "--outdir", default=".")
    b.add_argument("--no-lemma", action="store_true",
                   help="без лемматизации (быстрее, хуже ищет)")
    b.add_argument("--front", default="", metavar="РЕГВЫР",
                   help="заголовки передней части (предисловия, введения): "
                        "они не получают номера главы, чтобы «Глава I» стала "
                        "первой. Например: --front 'Предисловие|Введение'")
    b.add_argument("--quotes", action="store_true",
                   help="привести кавычки к ёлочкам и завести многоточие "
                        "внутрь цитаты (по умолчанию текст не трогается)")

    c = sub.add_parser("check", help="проверить качество разбора")
    c.add_argument("registry")

    f = sub.add_parser("freeze", help="заморозить нумерацию")
    f.add_argument("registry")

    v = sub.add_parser("verify", help="сравнить с замороженной нумерацией")
    v.add_argument("registry")
    v.add_argument("--map", metavar="ФАЙЛ",
                   help="выгрузить полную карту «старый якорь → новый» в TSV")

    rb = sub.add_parser("rebuild",
                        help="пересобрать все книги папки из их исходников")
    rb.add_argument("dir", nargs="?", default="books")

    s = sub.add_parser("search", help="поиск цитаты")
    s.add_argument("registry")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", type=int, default=5)

    a = ap.parse_args()

    if a.cmd == "build":
        src = Path(a.file)
        bid = a.id or re.sub(r"\W+", "_", src.stem).strip("_").lower()
        outdir = Path(a.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        reg = build(src, bid, lemmatize=not a.no_lemma,
                    typo_on=a.quotes, front=a.front)
        write_html(reg, outdir / f"{bid}.html")
        reg.pop("_binaries", None)
        (outdir / f"{bid}.json").write_text(
            json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
        (outdir / f"{bid}.txt").write_text(
            "\n".join(f'{x["id"]}\t{x["text"]}' for x in reg["sentences"]),
            encoding="utf-8")
        errs = print_report(reg, check(reg))
        print(f'\n→ {bid}.json  {bid}.html  {bid}.txt')
        if errs:
            print("Есть ошибки — почините до freeze.")
        sys.exit(1 if errs else 0)

    if a.cmd == "check":
        reg = load(a.registry)
        sys.exit(1 if print_report(reg, check(reg)) else 0)

    if a.cmd == "freeze":
        reg = load(a.registry)
        if print_report(reg, check(reg)):
            sys.exit("\nОтказ: сначала почините ошибки.")
        freeze(reg, a.registry)

    if a.cmd == "verify":
        sys.exit(verify(load(a.registry), a.registry, a.map))

    if a.cmd == "rebuild":
        d = Path(a.dir)
        regs = sorted(x for x in d.glob("*.json")
                      if not x.name.endswith(".lock.json"))
        if not regs:
            sys.exit(f"В {d} нет реестров")
        bad = 0
        for rp in regs:
            old = load(rp)
            src = d / old["book"]["source_file"]
            bid = old["book"]["id"]
            if not src.exists():
                print(f"! {bid}: нет исходника {src.name}, пропуск")
                bad += 1
                continue
            reg = build(
                src, bid,
                lemmatize=any("lemma" in x for x in old["sentences"][:1]),
                typo_on=old["book"].get("quotes", False),
                front=old["book"].get("front", ""))
            write_html(reg, d / f"{bid}.html")
            reg.pop("_binaries", None)
            rp.write_text(json.dumps(reg, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            (d / f"{bid}.txt").write_text(
                "\n".join(f'{x["id"]}\t{x["text"]}' for x in reg["sentences"]),
                encoding="utf-8")
            errs = len([r for r in check(reg) if r["sev"] == "ERROR"])
            same_text = old["book"]["text_sha256"] == reg["book"]["text_sha256"]
            # якоря важнее хеша: правка кавычек или опечатки меняет текст,
            # но границы предложений оставляет на месте
            same_ids = ({x["id"] for x in old["sentences"]}
                        == {x["id"] for x in reg["sentences"]})
            if same_text:
                note = "исходник не менялся"
            elif same_ids:
                note = "текст правили, якоря целы"
            else:
                note = "ЯКОРЯ СЪЕХАЛИ"
            flag = "✓" if same_ids and not errs else ("!" if same_ids else "✗")
            print(f'{flag} {bid:<12} абз. {len(reg["paragraphs"]):>4}  '
                  f'предл. {len(reg["sentences"]):>5}  {note}'
                  + (f"  ошибок: {errs}" if errs else ""))
            print(f'{"":<15}из {src.name}')
            if not same_ids or errs:
                bad += 1
        site = [(load(rp), lockfile(rp).exists()) for rp in regs]
        write_index(site, d.parent / "index.html")
        print(f'\n→ {(d.parent / "index.html")}')
        for rp in regs:
            if lockfile(rp).exists():
                print(f"--- verify {rp.name} ---")
                verify(load(rp), rp)
        sys.exit(1 if bad else 0)

    if a.cmd == "search":
        reg = load(a.registry)
        hits = search(reg["sentences"], " ".join(a.query), a.n)
        if not hits:
            print("не найдено — переформулируйте")
            sys.exit(1)
        for h in hits:
            print(f'[{h["id"]:<12}] {h["text"]}')


if __name__ == "__main__":
    main()
