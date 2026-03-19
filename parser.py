"""Парсер AFDparts.ru — авторизация и поиск запчастей по артикулу."""
import re
import time
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup


class AFDPartsParser:
    """Клиент для входа на AFDparts.ru и поиска по артикулу."""

    BASE_URL = "https://afdparts.ru"
    BRANDS_LIST_URL = "https://afdparts.ru/brandslist"

    def __init__(self, username: str, password: str, debug_save_html: bool = False):
        self.username = username
        self.password = password
        self.debug_save_html = debug_save_html
        self.session = requests.Session()
        self._brands_loaded = False
        self.known_brands: set[str] = set()
        self.brands_slug_to_name: dict[str, str] = {}
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Origin": self.BASE_URL,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        })
        self.is_authorized = False

    def authorize(self, session: requests.Session | None = None) -> bool:
        """
        Вход на сайт. Если session не передана — используется self.session и при успехе
        выставляется self.is_authorized (для обратной совместимости).
        Временная сессия (отдельный объект) — только для разового запроса под аккаунтом.
        """
        sess = session or self.session
        response = None
        try:
            # Пробуем страницу входа или главную
            for path in ("/login", "/user/login", "/auth", "/"):
                url = f"{self.BASE_URL}{path}"
                main = sess.get(url, timeout=15, headers={"Referer": self.BASE_URL + "/"})
                if main.status_code != 200:
                    continue
                soup = BeautifulSoup(main.text, "html.parser")
                login_data = self._get_login_form_data(soup)
                if not login_data:
                    continue
                time.sleep(1)
                form = soup.find("form", method=re.compile(r"post", re.I))
                post_url = url
                if form and form.get("action"):
                    action = form["action"].strip()
                    post_url = action if action.startswith("http") else urljoin(url, action)
                response = sess.post(
                    post_url,
                    data=login_data,
                    timeout=15,
                    allow_redirects=True,
                    headers={
                        "Referer": main.url or url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                if self._check_login_success(response):
                    if session is None:
                        self.is_authorized = True
                    return True
            if self.debug_save_html and response is not None:
                try:
                    with open("debug_afdparts_login.html", "w", encoding="utf-8") as f:
                        f.write(response.text or "")
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _get_login_form_data(self, soup: BeautifulSoup) -> dict | None:
        """Извлекает данные формы входа и подставляет логин/пароль."""
        login_names = ("login", "email", "username", "user", "e-mail")
        for form in soup.find_all("form"):
            inputs = form.find_all("input")
            has_login = any(
                inp.get("name") and (inp.get("name", "").lower() in login_names or "login" in (inp.get("name") or "").lower())
                for inp in inputs
            )
            has_pass = any(
                inp.get("type", "").lower() == "password" and inp.get("name") for inp in inputs
            )
            if not (has_login and has_pass):
                continue
            data = {}
            for inp in inputs:
                name = inp.get("name")
                if not name:
                    continue
                if inp.get("type", "").lower() == "password":
                    data[name] = self.password
                elif name.lower() in login_names or "login" in (name or "").lower() or name == "email":
                    data[name] = self.username
                elif inp.get("value") is not None:
                    data[name] = inp["value"]
                elif inp.get("type", "").lower() in ("submit", "image"):
                    data[name] = inp.get("value") or "Войти"
            if data:
                return data
        return None

    def _check_login_success(self, response: requests.Response) -> bool:
        """Проверяет успешный вход."""
        if response.status_code != 200:
            return False
        text = (response.text or "").lower()
        if "выход" in text or "выйти" in text or "logout" in text:
            return True
        if self.username and (self.username.split("@")[0].lower() in text or self.username.lower() in text):
            return True
        if "личный кабинет" in text or "кабинет" in text:
            return True
        if 'name="login"' in response.text and ('name="pass"' in response.text or 'type="password"' in response.text):
            return False
        return False

    @staticmethod
    def parse_price(price_text: str) -> float | None:
        """Извлекает число из строки с ценой."""
        cleaned = (price_text or "").replace("\xa0", " ").replace("&nbsp;", " ")
        match = re.search(r"([\d\s,.]+)", cleaned)
        if match:
            clean = match.group(1).replace(" ", "").replace(",", ".")
            try:
                return float(clean)
            except ValueError:
                return None
        return None

    @staticmethod
    def _normalize_code(code: str) -> str:
        """Нормализация артикула для сравнения."""
        return re.sub(r"[\s\-]", "", (code or "").upper())

    @staticmethod
    def _looks_obfuscated_or_masked_label(s: str) -> bool:
        """Маскированный «код детали» / заглушки вида «F * * * P» — не использовать как бренд."""
        if not s or len(s) > 40:
            return False
        if "*" in s:
            return True
        if re.search(r"^[\d\s\*\.•·]+$", s):
            return True
        return False

    @staticmethod
    def _afd_header_column_map(row) -> dict[str, int] | None:
        """
        По строке заголовка таблицы (th/td) — индексы колонок: brand, detail_code, article, manufacturer, description.
        """
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            return None
        if not row.find("th"):
            joined = " ".join((c.get_text(strip=True) or "").lower() for c in cells)
            if "бренд" not in joined and "код" not in joined:
                return None
        mapping: dict[str, int] = {}
        for i, c in enumerate(cells):
            t = re.sub(r"\s+", " ", (c.get_text(strip=True) or "").lower().replace("ё", "е")).strip()
            if not t:
                continue
            if t == "бренд" or t.startswith("бренд "):
                mapping["brand"] = i
            elif t == "brand" or t.startswith("brand "):
                mapping["brand"] = i
            elif "код детали" in t or t.endswith("код детали"):
                mapping["detail_code"] = i
            elif "detail" in t and "code" in t:
                mapping["detail_code"] = i
            elif t == "артикул" or (t.startswith("артикул") and "код" not in t):
                mapping["article"] = i
            elif "производитель" in t:
                mapping["manufacturer"] = i
            elif "описание" in t or t.startswith("описание"):
                mapping["description"] = i
            # Колонка закупа / опта (не путать с «описание»)
            elif "закуп" in t or "закупоч" in t:
                mapping["purchase"] = i
            elif "оптов" in t:
                mapping["purchase"] = i
            elif re.search(r"(^|\s)опт(\s|$)", t) or t == "опт" or t.startswith("опт "):
                mapping["purchase"] = i
            elif "вход" in t and "цен" in t:
                mapping["purchase"] = i
            elif "purchase" in t or "wholesale" in t or "dealer" in t:
                mapping["purchase"] = i
            elif "розниц" in t or ("цен" in t and "закуп" not in t and "опт" not in t and "оптов" not in t):
                mapping["retail_price"] = i
            elif "налич" in t or t == "остаток":
                mapping["availability"] = i
            elif "склад" in t or "возврат" in t:
                mapping["warehouse"] = i
        if "brand" in mapping:
            return mapping
        if "purchase" in mapping and ("article" in mapping or "detail_code" in mapping):
            return mapping
        return None

    @staticmethod
    def _cell_class_is_brand_column(cell_cls: str) -> bool:
        c = (cell_cls or "").lower()
        if re.search(r"(part|detail|article|request)p?code", c):
            return False
        if "бренд" in c:
            return True
        if re.search(r"(^|[-_])brand($|[-_])", c):
            return True
        if "resultbrand" in c or "casebrand" in c:
            return True
        if re.search(r"brand(?!code|num)", c):
            return True
        return False

    @staticmethod
    def _cell_class_is_article_or_detail_code_column(cell_cls: str) -> bool:
        """
        Колонка артикула / кода детали. Не использовать голое вхождение 'code' — оно цепляет лишние ячейки.
        """
        c = (cell_cls or "").lower()
        if AFDPartsParser._cell_class_is_brand_column(cell_cls) and "code" not in c:
            return False
        if "артикул" in c:
            return True
        if "код" in c and "детал" in c:
            return True
        if re.search(
            r"partcode|partnumber|detailcode|requestarticle|pcode|articlenum|"
            r"searchresults.*part|resultpart|casepart|detailnum",
            c,
        ):
            return True
        return False

    @staticmethod
    def _cell_class_is_manufacturer_column(cell_cls: str) -> bool:
        c = (cell_cls or "").lower()
        return (
            "производитель" in c
            or "manufacturer" in c
            or "maker" in c
            or "resultmanufacturer" in c
        )

    @staticmethod
    def _classify_item_type(code: str, article: str, name: str = "", description: str = "", row_classes: str = "") -> str:
        """
        Классификация: requested (запрашиваемый), original (оригинальная замена), analog (аналог).
        """
        clean_article = AFDPartsParser._normalize_code(article)
        clean_code = AFDPartsParser._normalize_code(code)
        if clean_code == clean_article:
            return "requested"
        text = f"{row_classes} {name} {description}".lower()
        if "оригинал" in text or "original" in text or "оригинальн" in text:
            return "original"
        if "аналог" in text or "analog" in text or "замен" in text:
            return "analog"
        return "analog"

    def _load_brands_list(self) -> None:
        """Загружает список брендов с https://afdparts.ru/brandslist для корректного определения бренда."""
        if self._brands_loaded:
            return
        self._brands_loaded = True
        try:
            resp = self.session.get(self.BRANDS_LIST_URL, timeout=15, headers={"Referer": self.BASE_URL + "/"})
            if resp.status_code != 200:
                return
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                text = (a.get_text(strip=True) or "").strip()
                if not text or len(text) > 60:
                    continue
                if "/brand" in href.lower() or "/brands" in href.lower() or "brandslist" in href:
                    self.known_brands.add(text)
                    slug = href.rstrip("/").split("/")[-1].split("?")[0]
                    if slug and slug != "brandslist":
                        self.brands_slug_to_name[slug.lower()] = text
            for li in soup.find_all(["li", "td", "span"], class_=re.compile(r"brand|item|name", re.I)):
                t = (li.get_text(strip=True) or "").strip()
                if 2 <= len(t) <= 50 and t.isprintable():
                    self.known_brands.add(t)
            if self.debug_save_html:
                try:
                    with open("debug_afdparts_brandslist.html", "w", encoding="utf-8") as f:
                        f.write(resp.text)
                except Exception:
                    pass
        except Exception:
            pass

    def _is_known_brand(self, value: str) -> bool:
        """Проверяет, что значение есть в списке брендов с сайта."""
        if not value or not self.known_brands:
            return False
        v = value.strip().lower()
        return any(v == b.lower() for b in self.known_brands)

    def _resolve_brand(self, raw: str, slug: str = "") -> str:
        """Возвращает название бренда: по слагу из списка или сырое значение, если оно в списке брендов."""
        raw = (raw or "").strip()
        if slug and slug.lower() in self.brands_slug_to_name:
            return self.brands_slug_to_name[slug.lower()]
        if self._is_known_brand(raw):
            for b in self.known_brands:
                if b.lower() == raw.lower():
                    return b
        if raw and re.search(r"\d", raw) and re.match(r"^[A-Za-z0-9\-]+$", raw):
            return ""
        return raw

    def search(self, article: str) -> dict | None:
        """
        Поиск по артикулу без авторизации (публичные цены на сайте).
        """
        return self._search_with_session(self.session, article)

    def _search_with_session(self, session: requests.Session, article: str) -> dict | None:
        """
        Поиск с указанной сессией (анонимной или уже залогиненной).
        """
        article = (article or "").strip()
        if not article:
            return None
        self._load_brands_list()
        search_paths = [
            f"/search?q={article}",
            f"/search?article={article}",
            f"/search?pcode={article}",
            f"/catalog/search?q={article}",
            f"/?s={article}",
        ]
        for path in search_paths:
            url = f"{self.BASE_URL}{path}" if not path.startswith("http") else path
            result = self._parse_search_page(url, article, session=session)
            if result is not None:
                return self._split_by_type(result, article)
            time.sleep(0.3)
        return None

    def fetch_prices_for_order(self, article: str, selected_item: dict) -> dict:
        """
        Отдельная сессия: вход на сайт, повторный поиск, сопоставление позиции.
        Возвращает цены для менеджера (не для показа клиенту в чате).
        Ключи: ok, customer_visible_price, site_account_price, purchase_price, error (опц.).
        """
        out = {
            "ok": False,
            "customer_visible_price": selected_item.get("price"),
            "site_account_price": None,
            "purchase_price": None,
            "error": None,
        }
        auth_sess = requests.Session()
        auth_sess.headers.update({k: v for k, v in self.session.headers.items()})
        if not self.authorize(session=auth_sess):
            out["error"] = "auth_failed"
            return out
        result = self._search_with_session(auth_sess, article)
        if not result:
            out["error"] = "search_failed"
            return out
        matched = self._match_item_in_result(result, selected_item, search_article=article)
        if not matched:
            # Резерв: одна строка с той же розничной ценой, что видел клиент (закуп парсится из неё же)
            matched = self._match_item_by_retail_price_only(result, selected_item)
        if not matched:
            out["error"] = "no_match"
            out["ok"] = True
            return out
        out["ok"] = True
        out["site_account_price"] = matched.get("price")
        out["purchase_price"] = matched.get("purchase_price")
        return out

    def _match_item_in_result(
        self, result: dict, selected: dict, search_article: str = ""
    ) -> dict | None:
        """
        Находит ту же позицию в выдаче под аккаунтом.
        У анонимной и авторизованной выдачи в поле code может быть разная колонка
        («Артикул» vs «Код детали»), поэтому сравниваем также артикул запроса, цену, URL.
        """
        items: list[dict] = []
        for key in ("requested", "originals", "analogs"):
            items.extend(result.get(key) or [])

        if not items:
            return None

        art_norm = self._normalize_code((search_article or "").strip())
        code_norm = self._normalize_code(selected.get("code") or "")
        brand_sel = (selected.get("brand") or "").strip().lower()
        sel_price = selected.get("price")
        sel_url = (selected.get("url") or "").strip()
        sel_desc = re.sub(
            r"\s+",
            " ",
            (selected.get("description") or "").strip().lower().replace("ё", "е"),
        )[:80]

        def brand_ok(it: dict) -> bool:
            b = (it.get("brand") or "").strip().lower()
            if not brand_sel or not b:
                return True
            if brand_sel in b or b in brand_sel:
                return True
            if len(brand_sel) >= 4 and len(b) >= 4 and brand_sel[:4] == b[:4]:
                return True
            return False

        def item_code_norm(it: dict) -> str:
            return self._normalize_code(it.get("code") or "")

        def code_row_matches(it: dict) -> bool:
            ic = item_code_norm(it)
            if not ic:
                return False
            if code_norm and ic == code_norm:
                return True
            if art_norm and ic == art_norm:
                return True
            return False

        def price_close(it: dict, ref: float | None) -> bool:
            if ref is None or it.get("price") is None:
                return False
            try:
                return abs(float(it["price"]) - float(ref)) < 0.02
            except (TypeError, ValueError):
                return False

        def desc_similar(it: dict) -> bool:
            if not sel_desc or len(sel_desc) < 12:
                return True
            d = re.sub(
                r"\s+",
                " ",
                (it.get("description") or "").strip().lower().replace("ё", "е"),
            )[:80]
            if not d:
                return False
            return sel_desc in d or d in sel_desc or sel_desc[:40] == d[:40]

        # 1) Код строки = код позиции или артикул запроса; бренд совместим
        cands = [it for it in items if code_row_matches(it) and brand_ok(it)]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1 and sel_price is not None:
            by_price = [it for it in cands if price_close(it, sel_price)]
            if len(by_price) == 1:
                return by_price[0]
            if len(by_price) >= 1:
                return by_price[0]
        if len(cands) > 1:
            return cands[0]

        # 2) Только по коду (без бренда)
        cands2 = [it for it in items if code_row_matches(it)]
        if len(cands2) == 1:
            return cands2[0]
        if len(cands2) > 1 and sel_price is not None:
            by_price = [it for it in cands2 if price_close(it, sel_price)]
            if len(by_price) == 1:
                return by_price[0]
            if len(by_price) >= 1:
                return by_price[0]

        # 3) Тот же URL карточки
        if sel_url:
            for it in items:
                if (it.get("url") or "").strip() == sel_url:
                    return it

        # 4) Однозначное совпадение цены (как у клиента в списке)
        if sel_price is not None:
            by_price = [it for it in items if price_close(it, sel_price)]
            if len(by_price) == 1:
                return by_price[0]
            if len(by_price) > 1:
                by_desc = [it for it in by_price if desc_similar(it)]
                if len(by_desc) == 1:
                    return by_desc[0]
                if brand_ok(by_price[0]):
                    return by_price[0]

        # 5) Упоминание артикула запроса в тексте строки + цена
        if art_norm:
            mention = [
                it
                for it in items
                if art_norm in re.sub(r"\s+", "", (it.get("description") or "").upper())
                or art_norm in re.sub(r"\s+", "", (it.get("name") or "").upper())
                or art_norm in re.sub(r"\s+", "", (it.get("code") or "").upper())
            ]
            if sel_price is not None:
                mp = [it for it in mention if price_close(it, sel_price)]
                if len(mp) == 1:
                    return mp[0]
            if len(mention) == 1:
                return mention[0]

        return None

    def _match_item_by_retail_price_only(self, result: dict, selected: dict) -> dict | None:
        """Если основной матч не сработал — ровно одна строка с той же розничной ценой (из неё читается закуп)."""
        ref = selected.get("price")
        if ref is None:
            return None
        try:
            ref_f = float(ref)
        except (TypeError, ValueError):
            return None
        items: list[dict] = []
        for key in ("requested", "originals", "analogs"):
            items.extend(result.get(key) or [])
        cands = [
            it
            for it in items
            if it.get("price") is not None and abs(float(it["price"]) - ref_f) < 0.02
        ]
        if len(cands) != 1:
            return None
        return cands[0]

    def _split_by_type(self, result: dict, article: str) -> dict:
        """Распределяет items по requested, originals, analogs."""
        items = result.get("items") or []
        requested = []
        originals = []
        analogs = []
        page_brand = (result.get("brand") or "").strip()
        for it in items:
            it["brand"] = (it.get("brand") or "").strip() or page_brand
            t = it.get("type") or self._classify_item_type(
                it.get("code", ""),
                article,
                it.get("name", ""),
                it.get("description", ""),
                it.get("row_classes", ""),
            )
            it["type"] = t
            if t == "requested":
                requested.append(it)
            elif t == "original":
                originals.append(it)
            else:
                analogs.append(it)
        return {
            "part_number": result.get("part_number", article),
            "brand": result.get("brand", ""),
            "requested": requested,
            "originals": originals,
            "analogs": analogs,
            "min_price": result.get("min_price"),
        }

    def _parse_search_page(
        self, url: str, article: str, session: requests.Session | None = None
    ) -> dict | None:
        """Парсит страницу результатов поиска; возвращает структуру result или None."""
        try:
            sess = session or self.session
            resp = sess.get(url, timeout=15, headers={"Referer": self.BASE_URL + "/"})
            if resp.status_code != 200:
                return None
            if self.debug_save_html:
                try:
                    with open(f"debug_afdparts_search_{article[:20]}.html", "w", encoding="utf-8") as f:
                        f.write(resp.text)
                except Exception:
                    pass
            soup = BeautifulSoup(resp.text, "html.parser")
            items = []

            # Бренд из meta keywords: content="Бренд Febi, Артикул 43540, Купить ..."
            page_brand = ""
            meta_kw = soup.find("meta", attrs={"name": "keywords"})
            if meta_kw and meta_kw.get("content"):
                m = re.search(r"Бренд\s+([^,]+)", meta_kw["content"], re.I)
                if m:
                    page_brand = m.group(1).strip()

            # Текущий раздел по заголовкам AFDparts (Запрашиваемый / Оригинальные замены / Аналоги)
            current_section = "analog"

            # Вариант 1: таблица с результатами (классы типа result, search-result, product-list)
            tables = soup.find_all("table", class_=re.compile(r"result|search|product|price|list", re.I))
            for table in tables:
                use_section_from_headers = False
                column_map: dict[str, int] | None = None
                for row in table.find_all("tr"):
                    row_classes = row.get("class") or []
                    row_class = " ".join(row_classes)
                    # Заголовки секций AFDparts — обновляем текущий раздел и не парсим строку как данные
                    if "searchResultsRequestArticlesHeader" in row_classes:
                        current_section = "requested"
                        use_section_from_headers = True
                        continue
                    if "searchResultsOriginalAnalogArticlesHeader" in row_classes:
                        current_section = "original"
                        use_section_from_headers = True
                        continue
                    if "searchResultsAnalogArticlesHeader" in row_classes:
                        current_section = "analog"
                        use_section_from_headers = True
                        continue
                    hm = self._afd_header_column_map(row)
                    if hm is not None:
                        column_map = hm
                        continue

                    cells = row.find_all(["td", "th"])
                    if len(cells) < 2:
                        continue
                    link = row.find("a", href=True)
                    name = ""
                    code = article
                    price_text = ""
                    price_texts_in_row: list[str] = []
                    desc = ""
                    availability = ""
                    warehouse_info = ""
                    row_brand_raw = ""
                    row_manufacturer_cell = ""

                    def _cell_at(idx_key: str) -> str:
                        if not column_map or idx_key not in column_map:
                            return ""
                        idx = column_map[idx_key]
                        if idx < 0 or idx >= len(cells):
                            return ""
                        return (cells[idx].get_text(strip=True) or "").strip()

                    if column_map:
                        row_brand_raw = _cell_at("brand")
                        row_manufacturer_cell = _cell_at("manufacturer")
                        t_art = _cell_at("article")
                        t_det = _cell_at("detail_code")
                        if t_art:
                            code = t_art
                        elif t_det:
                            code = t_det
                        desc = desc or _cell_at("description")
                        availability = availability or _cell_at("availability")
                        warehouse_info = warehouse_info or _cell_at("warehouse")

                    for i, cell in enumerate(cells):
                        text = cell.get_text(strip=True)
                        text_nb = (text or "").replace("\xa0", " ")
                        cell_cls = str(cell.get("class", "")).lower()
                        if link and cell.find("a") == link:
                            name = text or (link.get_text(strip=True))
                        # Несколько сумм в одной ячейке: «289 ₽» и «250 ₽»
                        if re.search(r"[\d\s,.]+\s*₽|руб|руб\.|р\.", text_nb, re.I):
                            found_pm = list(
                                re.finditer(
                                    r"([\d\s,.]+)\s*(?:₽|руб\.?|р\.)",
                                    text_nb,
                                    re.I,
                                )
                            )
                            if found_pm:
                                for m in found_pm:
                                    price_texts_in_row.append(m.group(0).strip())
                            else:
                                price_texts_in_row.append(text_nb.strip())
                        if column_map is None:
                            if self._cell_class_is_article_or_detail_code_column(cell_cls):
                                code = text or code
                            if self._cell_class_is_brand_column(cell_cls):
                                row_brand_raw = text or row_brand_raw
                            if self._cell_class_is_manufacturer_column(cell_cls):
                                row_manufacturer_cell = text or row_manufacturer_cell
                            if "описание" in cell_cls or "description" in cell_cls or "resultdescription" in cell_cls:
                                desc = text
                            if "налич" in cell_cls or "availability" in cell_cls or "остаток" in cell_cls or "stock" in cell_cls:
                                availability = text or availability
                            if "склад" in cell_cls or "возврат" in cell_cls or "warehouse" in cell_cls or "return" in cell_cls:
                                warehouse_info = text or warehouse_info
                    if not availability:
                        for cell in cells:
                            if re.search(r"налич|в наличии|остаток|шт\.|штук", cell.get_text(), re.I):
                                availability = cell.get_text(strip=True)
                                break
                    if not warehouse_info:
                        for cell in cells:
                            if re.search(r"склад|возврат возможен|возврат в течении|в течении", cell.get_text(), re.I):
                                warehouse_info = cell.get_text(strip=True)
                                break
                    if not name and link:
                        name = link.get_text(strip=True)
                    # Бренд: из ячейки «Бренд»; «Код детали» / маски (* * *) — не путать с брендом
                    def _looks_like_part_code(s: str) -> bool:
                        if not s or len(s) < 3:
                            return False
                        s = s.strip()
                        if re.search(r"\d", s) and re.match(r"^[A-Za-z0-9\-]+$", s):
                            return True
                        if s.isdigit() and len(s) <= 15:
                            return True
                        return False

                    brand_cand = ""
                    if row_brand_raw and not _looks_like_part_code(row_brand_raw):
                        if not self._looks_obfuscated_or_masked_label(row_brand_raw):
                            brand_cand = row_brand_raw.strip()
                    # Слаг бренда из ссылки в строке (напр. /brand/stellox)
                    brand_slug = ""
                    for a in row.find_all("a", href=True):
                        h = (a.get("href") or "").strip()
                        if "/brand" in h.lower():
                            parts = h.rstrip("/").split("/")
                            slug = parts[-1].split("?")[0] if parts else ""
                            if slug and slug.lower() != "brand" and slug.lower() != "brandslist":
                                brand_slug = slug
                                break
                    if (not brand_cand or self._looks_obfuscated_or_masked_label(brand_cand)) and brand_slug:
                        row_brand = self._resolve_brand("", slug=brand_slug)
                    else:
                        row_brand = self._resolve_brand(brand_cand, slug=brand_slug)

                    if not row_brand and column_map is None:
                        for cell in cells:
                            t = (cell.get_text(strip=True) or "").strip()
                            if self._looks_obfuscated_or_masked_label(t) or _looks_like_part_code(t):
                                continue
                            if 4 <= len(t) <= 35 and t.isupper() and re.match(r"^[A-Za-z0-9\s\-]+$", t):
                                if t != code and t != row_brand_raw:
                                    row_brand = self._resolve_brand(t, slug=brand_slug)
                                    break
                    if not row_brand and column_map is None:
                        for cell in cells:
                            t = (cell.get_text(strip=True) or "").strip()
                            if self._looks_obfuscated_or_masked_label(t) or _looks_like_part_code(t):
                                continue
                            if 4 <= len(t) <= 25 and t.isalpha() and t[0].isupper():
                                if t != code and t != row_brand_raw:
                                    row_brand = self._resolve_brand(t, slug=brand_slug)
                                    break
                    # Производитель — только в описание, не в поле «бренд»
                    row_manufacturer = row_manufacturer_cell
                    if not row_manufacturer and column_map is None:
                        for cell in cells:
                            t = (cell.get_text(strip=True) or "").strip()
                            if self._looks_obfuscated_or_masked_label(t) or _looks_like_part_code(t):
                                continue
                            if 4 <= len(t) <= 35 and t.isupper() and re.match(r"^[A-Za-z0-9\s\-]+$", t):
                                if t != code and t != row_brand_raw and t != row_brand:
                                    row_manufacturer = t
                                    break
                    # Описание — полный текст из ячейки; в конец добавляем производителя, если есть
                    full_desc = (desc or name or "").strip()
                    if row_manufacturer and row_manufacturer not in full_desc:
                        full_desc = f"{full_desc} {row_manufacturer}".strip()
                    # Несколько цен в строке: первая — розница/отображаемая, вторая — закуп (под аккаунтом)
                    if price_texts_in_row:
                        price_text = price_texts_in_row[0]
                        purchase_text = price_texts_in_row[1] if len(price_texts_in_row) > 1 else ""
                    else:
                        price_text = ""
                        purchase_text = ""
                    price_val = self.parse_price(price_text) if price_text else None
                    purchase_val = self.parse_price(purchase_text) if purchase_text else None
                    # Явные колонки по заголовку таблицы: «Розница» / «Закуп» / «Опт»
                    if column_map:
                        if "retail_price" in column_map:
                            rtxt = _cell_at("retail_price")
                            if rtxt:
                                rpv = self.parse_price(rtxt)
                                if rpv is not None:
                                    price_val = rpv
                                    price_text = rtxt
                        if "purchase" in column_map:
                            ptxt = _cell_at("purchase")
                            if ptxt:
                                ppv = self.parse_price(ptxt)
                                if ppv is not None:
                                    purchase_val = ppv
                    # Явная колонка закупа по классу ячейки
                    if purchase_val is None:
                        for cell in cells:
                            cell_cls = str(cell.get("class", "")).lower()
                            if not any(
                                k in cell_cls
                                for k in (
                                    "purchase",
                                    "buyprice",
                                    "buy-price",
                                    "wholesale",
                                    "закуп",
                                    "закупоч",
                                    "оптов",
                                    "оптprice",
                                    "dealer",
                                    "dealerprice",
                                    "netprice",
                                    "inprice",
                                )
                            ):
                                continue
                            t = cell.get_text(strip=True)
                            pv = self.parse_price(t)
                            if pv is not None:
                                purchase_val = pv
                                break
                    # data-* (часто так отдают закуп в вёрстке)
                    if purchase_val is None:
                        _pur_attrs = (
                            "data-purchase",
                            "data-purchase-price",
                            "data-purchasing",
                            "data-buy-price",
                            "data-wholesale",
                            "data-opt-price",
                            "data-optprice",
                            "data-dealer-price",
                            "data-in-price",
                        )
                        for cell in cells:
                            for attr in _pur_attrs:
                                raw = cell.get(attr)
                                if not raw:
                                    continue
                                pv = self.parse_price(str(raw))
                                if pv is not None:
                                    purchase_val = pv
                                    break
                            if purchase_val is not None:
                                break
                    # title у ячейки («закуп 250…»)
                    if purchase_val is None:
                        for cell in cells:
                            title = (cell.get("title") or "").strip()
                            if not title:
                                continue
                            low = title.lower()
                            if not any(
                                k in low
                                for k in (
                                    "закуп",
                                    "опт",
                                    "закупоч",
                                    "wholesale",
                                    "purchase",
                                    "buy",
                                )
                            ):
                                continue
                            pv = self.parse_price(title)
                            if pv is not None:
                                purchase_val = pv
                                break
                    href = link.get("href", "") if link else ""
                    full_url = urljoin(self.BASE_URL, href) if href else ""
                    if name or price_val is not None or code != article:
                        item = {
                            "name": name or code,
                            "code": code,
                            "brand": row_brand.strip(),
                            "manufacturer": row_manufacturer.strip(),
                            "price": price_val,
                            "price_text": price_text or (f"{price_val:.2f} ₽" if price_val else ""),
                            "purchase_price": purchase_val,
                            "url": full_url,
                            "description": full_desc,
                            "availability": availability,
                            "warehouse_info": warehouse_info,
                            "row_classes": row_class,
                        }
                        if use_section_from_headers:
                            item["type"] = current_section
                        items.append(item)

            # Вариант 2: карточки товаров (div с классом product, item, card)
            if not items:
                for block in soup.find_all(["div", "article"], class_=re.compile(r"product|item|card|search-result", re.I)):
                    block_class = " ".join(block.get("class", []))
                    link = block.find("a", href=True)
                    name_el = block.find(class_=re.compile(r"name|title|product-name"))
                    price_el = block.find(class_=re.compile(r"price|cost"))
                    code_el = block.find(class_=re.compile(r"article|code|sku|art"))
                    brand_el = block.find(class_=re.compile(r"brand|бренд"))
                    desc_el = block.find(class_=re.compile(r"description|desc|описание"))
                    avail_el = block.find(class_=re.compile(r"availability|stock|налич|остаток"))
                    wh_el = block.find(string=re.compile(r"склад|возврат возможен|возврат в течении", re.I))
                    if not wh_el:
                        wh_el = block.find(string=re.compile(r"склад|возврат", re.I))
                    if wh_el:
                        wh_el = wh_el.parent if hasattr(wh_el, "parent") else wh_el
                    name = (name_el.get_text(strip=True) if name_el else "") or (link.get_text(strip=True) if link else "")
                    price_text = price_el.get_text(strip=True) if price_el else ""
                    code = code_el.get_text(strip=True) if code_el else article
                    row_brand = brand_el.get_text(strip=True) if brand_el else ""
                    desc = desc_el.get_text(strip=True) if desc_el else ""
                    availability = avail_el.get_text(strip=True) if getattr(avail_el, "get_text", None) else ""
                    warehouse_info = ""
                    if wh_el:
                        warehouse_info = wh_el.get_text(strip=True)[:200] if hasattr(wh_el, "get_text") else str(wh_el).strip()[:200]
                    price_val = self.parse_price(price_text)
                    href = link.get("href", "") if link else ""
                    full_url = urljoin(self.BASE_URL, href) if href else ""
                    if name or price_val is not None:
                        items.append({
                            "name": name or code,
                            "code": code,
                            "brand": row_brand,
                            "price": price_val,
                            "price_text": price_text or (f"{price_val:.2f} ₽" if price_val else ""),
                            "url": full_url,
                            "description": desc,
                            "availability": availability,
                            "warehouse_info": warehouse_info,
                            "row_classes": block_class,
                        })

            # Вариант 3: список ul/ol с ссылками и ценами
            if not items:
                for ul in soup.find_all(["ul", "ol"], class_=re.compile(r"search|result|product|list", re.I)):
                    for li in ul.find_all("li"):
                        link = li.find("a", href=True)
                        price_el = li.find(class_=re.compile(r"price|cost")) or re.search(r"[\d\s,.]+\s*₽", li.get_text())
                        name = (link.get_text(strip=True) if link else li.get_text(strip=True)) or ""
                        price_text = price_el.group(0) if isinstance(price_el, re.Match) else (price_el.get_text(strip=True) if getattr(price_el, "get_text", None) else "")
                        price_val = self.parse_price(price_text) if price_text else None
                        href = link.get("href", "") if link else ""
                        full_url = urljoin(self.BASE_URL, href) if href else ""
                        li_text = li.get_text()
                        availability = ""
                        warehouse_info = ""
                        if re.search(r"налич|остаток|в наличии", li_text, re.I):
                            availability = li_text.strip()[:100]
                        if re.search(r"склад|возврат возможен|возврат в течении", li_text, re.I):
                            warehouse_info = li_text.strip()[:150]
                        if name and (article.lower() in name.lower() or article.lower() in (href or "").lower() or price_val is not None):
                            items.append({
                                "name": name,
                                "code": article,
                                "price": price_val,
                                "price_text": price_text or (f"{price_val:.2f} ₽" if price_val else ""),
                                "url": full_url,
                                "description": "",
                                "availability": availability,
                                "warehouse_info": warehouse_info,
                                "row_classes": "",
                            })

            if not items:
                no_result = soup.find(string=re.compile(r"ничего не найдено|не найдено|no results", re.I))
                if no_result:
                    return {"part_number": article, "items": [], "min_price": None, "brand": page_brand}

            valid_prices = [i["price"] for i in items if i.get("price") is not None]
            min_price = min(valid_prices) if valid_prices else None
            if page_brand and self._looks_obfuscated_or_masked_label(page_brand):
                page_brand = ""
                for it in items:
                    b = (it.get("brand") or "").strip()
                    if b and not self._looks_obfuscated_or_masked_label(b):
                        page_brand = b
                        break
            return {
                "part_number": article,
                "items": items[:50],
                "min_price": min_price,
                "brand": page_brand,
            }
        except Exception:
            return None
