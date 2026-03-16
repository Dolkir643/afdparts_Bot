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

    def authorize(self) -> bool:
        """Вход на сайт: ищем форму входа и отправляем логин/пароль."""
        try:
            # Пробуем страницу входа или главную
            for path in ("/login", "/user/login", "/auth", "/"):
                url = f"{self.BASE_URL}{path}"
                main = self.session.get(url, timeout=15, headers={"Referer": self.BASE_URL + "/"})
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
                response = self.session.post(
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
                    self.is_authorized = True
                    return True
            if self.debug_save_html:
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
        Поиск по артикулу. Возвращает словарь:
        {
            "part_number": str,
            "requested": [...],   # Запрашиваемый артикул
            "originals": [...],   # Оригинальные замены
            "analogs": [...],     # Аналоги
            "min_price": float | None,
        }
        Каждый элемент: name, code, price, price_text, url, description, availability, warehouse_info, type.
        """
        if not self.is_authorized:
            return None
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
            result = self._parse_search_page(url, article)
            if result is not None:
                return self._split_by_type(result, article)
            time.sleep(0.3)
        return None

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

    def _parse_search_page(self, url: str, article: str) -> dict | None:
        """Парсит страницу результатов поиска; возвращает структуру result или None."""
        try:
            resp = self.session.get(url, timeout=15, headers={"Referer": self.BASE_URL + "/"})
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
                    cells = row.find_all(["td", "th"])
                    if len(cells) < 2:
                        continue
                    link = row.find("a", href=True)
                    name = ""
                    code = article
                    price_text = ""
                    desc = ""
                    availability = ""
                    warehouse_info = ""
                    row_brand = ""
                    row_brand_raw = ""  # значение из ячейки brand (может оказаться кодом)
                    for i, cell in enumerate(cells):
                        text = cell.get_text(strip=True)
                        cell_cls = str(cell.get("class", "")).lower()
                        if link and cell.find("a") == link:
                            name = text or (link.get_text(strip=True))
                        if re.search(r"[\d\s,.]+\s*₽|руб|руб\.|р\.", text, re.I):
                            price_text = text
                        if "артикул" in cell_cls or "code" in cell_cls or "partcode" in cell_cls:
                            code = text or code
                        if "бренд" in cell_cls or "brand" in cell_cls or "resultbrand" in cell_cls or "casebrand" in cell_cls:
                            row_brand_raw = text or row_brand_raw
                        if "производитель" in cell_cls or "manufacturer" in cell_cls or "maker" in cell_cls or "resultmanufacturer" in cell_cls:
                            row_brand = text or row_brand
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
                    # Бренд: из ячейки «brand» может прийти код (Z17713) — не считать брендом; нужен название (ZENTPARTS)
                    def _looks_like_part_code(s: str) -> bool:
                        if not s or len(s) < 3:
                            return False
                        s = s.strip()
                        if re.search(r"\d", s) and re.match(r"^[A-Za-z0-9\-]+$", s):
                            return True
                        if s.isdigit() and len(s) <= 15:
                            return True
                        return False
                    if not row_brand and row_brand_raw and not _looks_like_part_code(row_brand_raw):
                        row_brand = row_brand_raw.strip()
                    if not row_brand:
                        for cell in cells:
                            t = (cell.get_text(strip=True) or "").strip()
                            if 4 <= len(t) <= 35 and t.isupper() and re.match(r"^[A-Za-z0-9\s\-]+$", t):
                                if t != code and t != row_brand_raw and not _looks_like_part_code(t):
                                    row_brand = t
                                    break
                    if not row_brand:
                        for cell in cells:
                            t = (cell.get_text(strip=True) or "").strip()
                            if 4 <= len(t) <= 25 and t.isalpha() and t[0].isupper() and not _looks_like_part_code(t):
                                if t != code and t != row_brand_raw:
                                    row_brand = t
                                    break
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
                    row_brand = self._resolve_brand(row_brand or row_brand_raw, slug=brand_slug)
                    # Производитель (ZENTPARTS и т.п.) — второй брендообразный текст в строке; добавляем в описание
                    row_manufacturer = ""
                    for cell in cells:
                        t = (cell.get_text(strip=True) or "").strip()
                        if 4 <= len(t) <= 35 and t.isupper() and re.match(r"^[A-Za-z0-9\s\-]+$", t):
                            if t != code and not _looks_like_part_code(t) and t != (row_brand or row_brand_raw):
                                row_manufacturer = t
                                break
                    # Описание — полный текст из ячейки; в конец добавляем производителя, если есть
                    full_desc = (desc or name or "").strip()
                    if row_manufacturer and row_manufacturer not in full_desc:
                        full_desc = f"{full_desc} {row_manufacturer}".strip()
                    price_val = self.parse_price(price_text) if price_text else None
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
            return {
                "part_number": article,
                "items": items[:50],
                "min_price": min_price,
                "brand": page_brand,
            }
        except Exception:
            return None
