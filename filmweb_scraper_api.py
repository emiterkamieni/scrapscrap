import uvicorn
import httpx
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

# --- KONFIGURACJA ---
app = FastAPI(title="Movie Ratings API (Filmweb, IMDb, RT)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Nagłówki, aby serwisy nas nie blokowały
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"
}

# --- MODELE DANYCH ---

class RatingSource(BaseModel):
    source: str  # "Filmweb", "IMDb", "Rotten Tomatoes"
    rating: Optional[float] = None
    vote_count: Optional[str] = None # String, bo czasem to "10k"
    url: Optional[str] = None

class CombinedMovieData(BaseModel):
    query_title: str
    year: Optional[str] = None
    ratings: List[RatingSource]

class UserRating(BaseModel):
    title: str
    user_rating: int
    timestamp: Optional[str] = None

# --- LOGIKA SCRAPERÓW (ASYNC) ---

async def scrape_filmweb(client: httpx.AsyncClient, title: str, year: str = None) -> RatingSource:
    try:
        query = f"{title} {year}" if year else title
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.filmweb.pl/search?q={encoded_query}"
        
        # 1. Szukaj
        resp = await client.get(search_url, headers=HEADERS)
        soup = BeautifulSoup(resp.content, "html.parser")
        
        result = soup.select_one(".resultsList .preview__link") or soup.select_one(".searchResult__link")
        if not result:
            return RatingSource(source="Filmweb", rating=None)
            
        url = result.get("href")
        if url.startswith("/"): url = f"https://www.filmweb.pl{url}"
        
        # 2. Pobierz detale
        details_resp = await client.get(url, headers=HEADERS)
        details_soup = BeautifulSoup(details_resp.content, "html.parser")
        
        rating_tag = details_soup.select_one(".filmRating__rateValue")
        rating = float(rating_tag.text.strip().replace(",", ".")) if rating_tag else None
        
        count_tag = details_soup.select_one(".filmRating__count")
        count = count_tag.text.strip() if count_tag else None
        
        return RatingSource(source="Filmweb", rating=rating, vote_count=count, url=url)
    except Exception as e:
        print(f"Filmweb error: {e}")
        return RatingSource(source="Filmweb", rating=None)

async def scrape_imdb(client: httpx.AsyncClient, title: str, year: str = None) -> RatingSource:
    try:
        # Wyszukiwanie na IMDb jest specyficzne, używamy endpointu find
        query = f"{title} {year}" if year else title
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.imdb.com/find?q={encoded_query}&s=tt" # s=tt szuka tylko tytułów
        
        resp = await client.get(search_url, headers=HEADERS)
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # Nowy layout IMDb
        result_link = soup.select_one(".ipc-metadata-list-summary-item__t")
        if not result_link:
            return RatingSource(source="IMDb", rating=None)
            
        href = result_link.get("href")
        movie_url = f"https://www.imdb.com{href}".split("?")[0]
        
        # Detale
        details_resp = await client.get(movie_url, headers=HEADERS)
        d_soup = BeautifulSoup(details_resp.content, "html.parser")
        
        # Ocena w nowym IMDb to często span wewnątrz div z data-testid="hero-rating-bar__aggregate-rating__score"
        rating_span = d_soup.select_one('[data-testid="hero-rating-bar__aggregate-rating__score"] span')
        rating = float(rating_span.text.strip()) if rating_span else None
        
        # Ilość głosów
        votes_div = d_soup.select_one('div[data-testid="hero-rating-bar__aggregate-rating__score"] ~ div')
        votes = votes_div.text.strip() if votes_div else None

        return RatingSource(source="IMDb", rating=rating, vote_count=votes, url=movie_url)
    except Exception as e:
        print(f"IMDb error: {e}")
        return RatingSource(source="IMDb", rating=None)

async def scrape_rotten_tomatoes(client: httpx.AsyncClient, title: str, year: str = None) -> RatingSource:
    try:
        encoded_query = urllib.parse.quote(title)
        search_url = f"https://www.rottentomatoes.com/search?search={encoded_query}"
        
        resp = await client.get(search_url, headers=HEADERS)
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # Szukanie pierwszego wyniku w sekcji MOVIES
        movie_link = soup.select_one('search-page-result[type="movie"] a')
        if not movie_link:
            movie_link = soup.select_one('#search-results movie-search-result-container a')

        if not movie_link:
             return RatingSource(source="Rotten Tomatoes", rating=None)

        href = movie_link.get("href")
        full_url = href
        if not full_url.startswith("http"):
            full_url = f"https://www.rottentomatoes.com{href}"

        # Detale
        d_resp = await client.get(full_url, headers=HEADERS)
        d_soup = BeautifulSoup(d_resp.content, "html.parser")
        
        # Tomatometer
        score_tag = d_soup.select_one('rt-button[slot="criticsScore"] rt-text') 
        if not score_tag:
            score_tag = d_soup.select_one("score-board-band rt-text")
            
        rating = None
        if score_tag:
            try:
                rating = float(score_tag.text.strip().replace("%", ""))
            except: pass

        return RatingSource(source="Rotten Tomatoes", rating=rating, vote_count=None, url=full_url)
    except Exception as e:
        print(f"RT error: {e}")
        return RatingSource(source="Rotten Tomatoes", rating=None)

async def scrape_filmweb_user_recent(client: httpx.AsyncClient, username: str) -> List[UserRating]:
    """
    Pobiera ostatnio ocenione filmy użytkownika z jego profilu publicznego.
    """
    url = f"https://www.filmweb.pl/user/{username}"
    try:
        resp = await client.get(url, headers=HEADERS)
        soup = BeautifulSoup(resp.content, "html.parser")
        
        ratings = []
        items = soup.select(".voteCommentBox") 
        
        for item in items:
            title_tag = item.select_one(".filmTitle")
            rate_tag = item.select_one(".userRate") 
            
            # Fallback - szukanie w strukturze data-source
            if not rate_tag:
                rate_box = item.select_one(".span-10")
                if rate_box and "ocenił na" in rate_box.text:
                    rate_text = rate_box.text.split("ocenił na")[1].strip().split(" ")[0]
                    ratings.append(UserRating(title=title_tag.text.strip(), user_rating=int(rate_text)))
                    
        return ratings
    except Exception as e:
        print(f"User scrape error: {e}")
        return []

# --- ENDPOINTY ---

@app.get("/")
def home():
    return {"status": "ok", "usage": "/all-ratings?title=Matrix&year=1999"}

@app.get("/all-ratings", response_model=CombinedMovieData)
async def get_all_ratings(title: str, year: str = None):
    """
    Pobiera oceny z Filmweb, IMDb i Rotten Tomatoes równolegle.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            scrape_filmweb(client, title, year),
            scrape_imdb(client, title, year),
            scrape_rotten_tomatoes(client, title, year)
        )
        
    return CombinedMovieData(
        query_title=title,
        year=year,
        ratings=results
    )

@app.get("/user/filmweb/{username}")
async def get_filmweb_user_activity(username: str):
    """
    Pobiera ostatnią aktywność użytkownika.
    """
    async with httpx.AsyncClient() as client:
        ratings = await scrape_filmweb_user_recent(client, username)
    return {"username": username, "recent_ratings": ratings}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)