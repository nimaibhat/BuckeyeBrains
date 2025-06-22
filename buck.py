from bs4 import BeautifulSoup
import requests
from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

uri = os.getenv("MONGODB_URI")

client = MongoClient(uri)
db = client["osu_faculty"]
collection = db["profiles"]

url = "https://cse.osu.edu/directory/tenure-track"

req = requests.get(url)
html = req.text

scrape = BeautifulSoup(html, "html.parser")

for article in scrape.find_all('article'):
    if article.has_attr('about'):
        about = article['about']

        profile_url = "https://cse.osu.edu" + about
        res = requests.get(profile_url)
        html2 = res.text
        soup = BeautifulSoup(html2, "html.parser")
        target_divs = soup.find_all('div', class_="layout__region layout__region--second region-large")

        for div in target_divs:
            p = div.find('p')
            if p:
                about_text = p.get_text(strip=True)

                doc = {
                    "profile_path": about,
                    "profile_url": profile_url,
                    "about_me": about_text
                }

                collection.insert_one(doc)

print("Scraping and saving to MongoDB complete.")
