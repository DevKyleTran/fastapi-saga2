from contextlib import asynccontextmanager
import csv
from datetime import datetime, timezone
from calendar import timegm
from io import StringIO
import logging
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo
import uvicorn as uv
from curl_cffi import requests as curl_requests

# ruyijs is the WORST package you'll ever work with, just force curl_cffi to work, you'll always find a way

from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, status

logger = logging.getLogger(__name__)

# make a list of stock news websites, and forex news websites
# then scrape, parse into data, then create get route

LISTING_URLS = {
    "NASDAQ": "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "NYSE": "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
}
PRICE_URLS = {
    "NASDAQ": "https://www.slickcharts.com/nasdaq",
    "NYSE": "https://www.slickcharts.com/nyse",
}
EXCHANGE_INFO = {
    "NASDAQ": {"country_code": "US", "currency": "USD", "timezone": "America/New_York"},
    "NYSE": {"country_code": "US", "currency": "USD", "timezone": "America/New_York"},
}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 stock-market-api/1.0"})
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

# run a for loop through these
# get news of specific stocks, not just news in general
# or create a separate tab for that

# don't touch for now

# Exchange -> symbol -> company name.
tickers: dict[str, dict[str, str]] = {}
# Exchange -> symbol -> market metrics.
market_data: dict[str, dict[str, dict[str, float]]] = {}


def scrape_forex(from_symbol: str, to_symbol: str) -> dict:

    base, target = from_symbol.strip().upper(), to_symbol.strip().upper()

    if len(base) != 3 or len(target) != 3 or not base.isalpha() or not target.isalpha():
        raise ValueError("Currency codes must be three letters")
    
    if base == target:
        raise ValueError("from_symbol and to_symbol must be different")

    direct_symbol = f"{base}{target}=X"
    reverse_symbol = f"{target}{base}=X"
    symbol = direct_symbol
    invert = False

    response = HTTP.get(f"{YAHOO_CHART_URL}/{direct_symbol}", params={"range": "5d", "interval": "1d"}, timeout=20)
    if response.status_code == 404:
        response = HTTP.get(f"{YAHOO_CHART_URL}/{reverse_symbol}", params={"range": "5d", "interval": "1d"}, timeout=20)
        symbol, invert = reverse_symbol, True

    response.raise_for_status()

    payload = response.json().get("chart", {}).get("result")
    if not payload:
        raise RuntimeError(f"Yahoo Finance returned no quote for {symbol}")
    result = payload[0]

    meta = result.get("meta", {})
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    prices = [float(value) for value in closes if value is not None]

    if not prices:
        raise RuntimeError(f"Yahoo Finance returned no prices for {symbol}")
    
    current_rate = prices[-1]
    rounded_rate = round(current_rate, 4)

    previous_close = float(meta.get("chartPreviousClose", meta.get("previousClose", prices[-2] if len(prices) > 1 else current_rate)))
    rounded_close = round(previous_close, 4)

    stamp = meta.get("regularMarketTime")

    if stamp is None:
        timestamps = result.get("timestamp", [])
        if not timestamps:
            raise RuntimeError(f"Yahoo Finance returned no timestamp for {symbol}")
        stamp = timestamps[-1]

    last_update = datetime.fromtimestamp(int(stamp), tz=timezone.utc)

    if invert:
        rounded_rate = 1 / rounded_rate
        rounded_close = 1 / rounded_close

    return {
        "from_symbol": base,
        "to_symbol": target,
        "type": "currency",
        "exchange_rate": rounded_rate,
        "previous_close": rounded_close,
        "last_update_utc": last_update,
    }


def retrieve_date(symbol: str, exchange: str) -> datetime:
    """Fetch the timestamp displayed on Google Finance."""
    if exchange not in PRICE_URLS:
        raise ValueError(f"Unsupported exchange: {exchange}")
    url = f"https://www.google.com/finance/quote/{quote(symbol, safe='')}:{exchange}"
    response = HTTP.get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    # Prefer Google's machine-readable timestamp when it is present.
    timestamp = soup.select_one("[data-last-normal-market-timestamp]")
    if timestamp is not None:
        value = timestamp.get("data-last-normal-market-timestamp")
        if isinstance(value, str) and value:
            try:
                stamp = int(value)
                if stamp > 10_000_000_000:  # milliseconds
                    stamp //= 1000
                return datetime.fromtimestamp(stamp, tz=ZoneInfo("UTC"))
            except (ValueError, OSError, OverflowError):
                pass

    # The visible status currently looks like "Closed: Oct 30, 4:00:01 PM GMT-4".
    element = soup.select_one(".jZZ2de")
    raw = element.get_text(" ", strip=True) if element is not None else soup.get_text(" ", strip=True)
    raw = re.sub(r"^(?:As of|Closed):\s*", "", str(raw).strip(), flags=re.IGNORECASE)
    raw = raw.replace("\u202f", " ").replace("\u00a0", " ")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass

    match = re.search(
        r"([A-Z][a-z]{2} \d{1,2}),\s*(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)\s*(?:GMT([+-]\d{1,2})(?::?(\d{2}))?)?",
        raw,
    )
    if match:
        date_text, time_text, offset_hour, offset_minute = match.groups()
        year = datetime.now().year  # Google omits the year in its status line.
        offset = ""
        if offset_hour:
            sign = "+" if offset_hour.startswith("+") else "-"
            offset = f"{sign}{abs(int(offset_hour)):02d}{int(offset_minute or 0):02d}"
        time_format = "%I:%M:%S %p" if time_text.count(":") == 2 else "%I:%M %p"
        stamp_text = f"{date_text}, {year}, {time_text}{offset}"
        fmt = f"%b %d, %Y, {time_format}%z" if offset else f"%b %d, %Y, {time_format}"
        try:
            return datetime.strptime(stamp_text, fmt)
        except ValueError:
            pass

    # Also accept explicit-year values if Google changes its display format.
    for fmt in ("%b %d, %Y, %I:%M:%S %p %Z", "%b %d, %Y, %I:%M %p %Z",
                "%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise RuntimeError(f"Unrecognized quote timestamp for {symbol}:{exchange}: {raw!r}")


def retrieve_listings() -> dict[str, dict[str, str]]:
    listings: dict[str, dict[str, str]] = {}
    for exchange, url in LISTING_URLS.items():
        response = HTTP.get(url, timeout=20)
        response.raise_for_status()
        rows = csv.reader(StringIO(response.text), delimiter="|")
        next(rows, None)  # Header
        companies: dict[str, str] = {}
        for row in rows:
            if len(row) < 2 or row[0].startswith("File Creation Time"):
                continue
            # otherlisted.txt contains multiple venues; N is the NYSE code.
            if exchange == "NYSE" and (len(row) < 3 or row[2].strip().upper() != "N"):
                continue
            symbol, name = row[0].strip().upper(), row[1].strip()
            if symbol and name:
                companies[symbol] = name
        listings[exchange] = companies
    return listings


def _number(text: str) -> float:
    """Parse a displayed dollar/percent value, including parenthesized negatives."""
    value = text.strip().replace(",", "").replace("$", "").replace("%", "")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    return float(value)

def scrape_market_data() -> dict[str, dict[str, dict[str, float]]]:

    all_data: dict[str, dict[str, dict[str, float]]] = {}
    from curl_cffi import requests

    for exchange, url in PRICE_URLS.items(): # url is already defined within this dictionary
        
        headers = {
                "Referer": url
            }
        
        response = requests.get(
            url,
            headers=headers,
            impersonate="chrome")

        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.select_one("table")
        if table is None:
            raise RuntimeError(f"Couldn't find {table}")

        exchange_data: dict[str, dict[str, float]] = {}
        for row in table.select("tbody tr"):
            cells = row.find_all("td")

            if len(cells) < 7:
                continue

            symbol = cells[2].get_text(strip=True).upper() # symbol text

            try:
                price = _number(cells[4].get_text(strip=True))
                change = _number(cells[5].get_text(strip=True))
                change_percent = _number(cells[6].get_text(strip=True))

            except ValueError:
                continue

            if not symbol:
                continue
            
            # NVDA: = {data}
            exchange_data[symbol] = {
                "price": price,
                "change": change,
                "change_percent": change_percent,
                "prev_close": round(price - change, 8)
            }


        if not exchange_data:
            raise RuntimeError(f"No {exchange} market data was found on SlickCharts")

        all_data[exchange] = exchange_data

    return all_data

# make this function work for every webpage instead of a certain one

# now get the url

NEWS_URL = "https://www.forexfactory.com/news" # be specific with variable when working with large codebases

def main():
    url = NEWS_URL
    response = curl_requests.get(url, impersonate="chrome", timeout=20)
    response.raise_for_status()
    html = response.text

    soup = BeautifulSoup(html, "html.parser")

    articles = []
    seen = set()

    for item in soup.select(".news-block__item"):
        title = item.select_one(".news-block__title a[href]")
        if title is None:
            continue

        text = title.get_text(" ", strip=True)
        href = title.get("href")
        if not text or not isinstance(href, str) or not href:
            continue

        full_url = urljoin(url, href)

        if full_url in seen:
            continue

        seen.add(full_url)

        articles.append({
            "title": text,
            "url": full_url
        })

    if not articles:
        raise RuntimeError("No linked Forex Factory news articles were found")

    return articles

# make function so we can call it in fastapi

def retrieve_news(symbol: str) -> list[dict]:

    symbol = symbol.strip().upper()

    stock_url = f"https://finance.yahoo.com/quote/{symbol}/news/"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(stock_url, headers=headers, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')

    titles = soup.select('h3.clamp.yf-28d2hc')
    links = soup.select('a.subtle-link.fin-size-small.titles.noUnderline.yf-119g04z')
    dates = soup.select('div.publishing.yf-bmkwve')

    articles = []
    authors = []
    published_dates = []

    for date in dates:
        parts = date.find_all(string=True)

        author = parts[0].strip() if parts else ""
        published = parts[-1].strip() if parts else ""

        authors.append(author)
        published_dates.append(published)

    for title, link, author, published in zip(titles, links, authors, published_dates):
        text = title.get_text(strip=True)
        href = link.get('href')

        if not text or not isinstance(href, str):
            continue

        articles.append({
            "title": text,
            "url": urljoin(stock_url, href),
            "author": author,
            "published": published
        })

    return articles

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load independently so one upstream outage does not prevent startup.
    global tickers, market_data, news_data # GLOBALIZE FIRST
    try:
        tickers = retrieve_listings()
    except requests.RequestException:
        logger.exception("Unable to retrieve ticker listings at startup")
    try:
        market_data = scrape_market_data()
    except (requests.RequestException, RuntimeError):
        logger.exception("Unable to retrieve stock prices at startup")
    try:
        news_data = main()
    except requests.RequestException:
        logger.exception("Unable to retrieve news data at startup")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/stock", status_code=status.HTTP_200_OK)
def get_stock_list():
    return tickers


@app.get("/stock/{stock_name}", description="Example: MSFT/msft")
def search_stock(stock_name: str):
    query = stock_name.strip().upper()
    for exchange, companies in tickers.items():
        for symbol, name in companies.items():
            if query == symbol or query == name.upper():
                try:
                    quote_datetime = retrieve_date(symbol, exchange)
                except (requests.RequestException, RuntimeError) as exc:
                    logger.exception("Unable to retrieve quote timestamp for %s", symbol)
                    raise HTTPException(status_code=502, detail="Quote timestamp unavailable") from exc
                info = EXCHANGE_INFO[exchange]
                exchange_zone = ZoneInfo(info["timezone"])
                quote_day = quote_datetime.astimezone(exchange_zone).date() if quote_datetime.tzinfo else quote_datetime.date()
                open_time = datetime.strptime("09:30", "%H:%M").time()
                close_time = datetime.strptime("16:00", "%H:%M").time()
                exchange_open = datetime.combine(quote_day, open_time, exchange_zone)
                exchange_close = datetime.combine(quote_day, close_time, exchange_zone)
                utc_offset = exchange_close.utcoffset()
                return {
                    "symbol": symbol,
                    "name": name,
                    "type": "stock",
                    "exchange": exchange,
                    "last_update_utc": quote_datetime,
                    "country_code": info["country_code"],
                    "exchange_open": exchange_open,
                    "exchange_close": exchange_close,
                    "timezone": info["timezone"],
                    "utc_offset_sec": int(utc_offset.total_seconds()) if utc_offset is not None else 0,
                    "currency": info["currency"],
                    **market_data.get(exchange, {}).get(symbol, {}),
                }
    raise HTTPException(status_code=404, detail="Stock not found")


@app.get("/forex/{from_symbol}/{to_symbol}", description="Major pairs only supported (e.g. EUR/USD).")
def convert_forex(from_symbol: str, to_symbol: str):
    try:
        return scrape_forex(from_symbol, to_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.RequestException as exc:
        logger.exception("Unable to retrieve forex rate for %s/%s", from_symbol, to_symbol)
        raise HTTPException(status_code=502, detail="Forex rate unavailable") from exc
    except RuntimeError as exc:
        logger.exception("Unable to retrieve forex rate for %s/%s", from_symbol, to_symbol)
        raise HTTPException(status_code=502, detail="Forex rate unavailable") from exc

@app.get("/forex/news")
# title is function name
def get_forex_factory_news():
    try:
        return main()
    except (curl_requests.exceptions.RequestException, RuntimeError) as e:
        logger.exception("Unable to retrieve forex news")
        raise HTTPException(status_code=502, detail="Forex news unavailable") from e

@app.get("/stock/{symbol}/news", description="Example: NVDA/nvda")
def get_stock_news(symbol: str):
    try:
        return retrieve_news(symbol) # add await if async
    except RuntimeError as e:
        logger.exception("Unable to retrieve stock news")
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.get("/")
def home():
    return {"Message": "Received!"}

if __name__ == "__main__":
    uv.run(app, host="127.0.0.1", port=8000, reload=False)