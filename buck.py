from bs4 import BeautifulSoup
import re
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import time
import logging
from urllib.parse import urljoin, urlparse, parse_qs
import re
import requests
import json


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables from multiple possible files
env_files = ['.env.local', '.env']
for env_file in env_files:
    if os.path.exists(env_file):
        load_dotenv(env_file)
        logger.info(f"Loaded environment from {env_file}")
        break
else:
    logger.warning("No .env file found, using system environment variables")

def setup_database():
    """Setup database connection with proper error handling"""
    uri = os.getenv("MONGODB_URI")
    
    # If no URI is provided, try common local connections
    if not uri:
        possible_uris = [
            "mongodb://localhost:27017/",
            "mongodb://127.0.0.1:27017/",
            "mongodb://localhost:27017/osu_faculty"
        ]
        
        for test_uri in possible_uris:
            try:
                logger.info(f"Trying to connect to: {test_uri}")
                client = MongoClient(test_uri, serverSelectionTimeoutMS=5000)
                # Test the connection
                client.admin.command('ping')
                logger.info(f"Successfully connected to: {test_uri}")
                return client, client["osu_faculty"], client["osu_faculty"]["profiles"]
            except Exception as e:
                logger.warning(f"Failed to connect to {test_uri}: {e}")
                continue
        
        # If all local connections fail, suggest alternatives
        logger.error("Could not connect to local MongoDB. Consider these options:")
        logger.error("1. Start MongoDB locally: brew services start mongodb/brew/mongodb-community (Mac) or sudo systemctl start mongod (Linux)")
        logger.error("2. Use MongoDB Atlas (cloud): Set MONGODB_URI in your .env file")
        logger.error("3. Use alternative storage (see fallback options below)")
        
        # Offer fallback to JSON file storage
        return None, None, None
    else:
        try:
            # Atlas-optimized connection settings
            client = MongoClient(
                uri, 
                serverSelectionTimeoutMS=30000,  # Longer timeout for Atlas
                connectTimeoutMS=30000,
                socketTimeoutMS=30000,
                retryWrites=True,
                w='majority'
            )
            # Test the connection
            client.admin.command('ping')
            logger.info("Successfully connected to MongoDB Atlas!")
            return client, client["osu_faculty"], client["osu_faculty"]["profiles"]
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB Atlas: {e}")
            logger.error("Common Atlas issues:")
            logger.error("1. Check if your IP address is whitelisted in Atlas Network Access")
            logger.error("2. Verify username/password in connection string")
            logger.error("3. Ensure cluster is running (not paused)")
            return None, None, None

# Try to setup database connection
client, db, collection = setup_database()

class PaginatedScraper:
    def __init__(self, base_url, delay=1, use_file_storage=False):
        self.base_url = base_url
        self.delay = delay
        self.use_file_storage = use_file_storage
        self.file_storage_path = "faculty_profiles.json"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Load existing data if using file storage
        if self.use_file_storage:
            self.existing_profiles = self.load_from_file()
        else:
            self.existing_profiles = []
    
    def load_from_file(self):
        """Load existing profiles from JSON file"""
        try:
            if os.path.exists(self.file_storage_path):
                with open(self.file_storage_path, 'r', encoding='utf-8') as f:
                    return json.loads(f.read())
            return []
        except Exception as e:
            logger.error(f"Error loading from file: {e}")
            return []
    
    def save_to_file(self, profiles_data):
        """Save profiles to JSON file"""
        try:
            self.existing_profiles.extend(profiles_data)
            with open(self.file_storage_path, 'w', encoding='utf-8') as f:
                json.dump(self.existing_profiles, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved {len(profiles_data)} profiles to {self.file_storage_path}")
        except Exception as e:
            logger.error(f"Error saving to file: {e}")
    
    def profile_exists(self, profile_path):
        """Check if profile already exists"""
        if self.use_file_storage:
            return any(p.get('profile_path') == profile_path for p in self.existing_profiles)
        elif collection is not None:
            return collection.find_one({"profile_path": profile_path}) is not None
        return False
    
    def save_profiles(self, profiles_data):
        """Save profiles to database or file"""
        if not profiles_data:
            return
        
        if self.use_file_storage:
            self.save_to_file(profiles_data)
        elif collection is not None:
            try:
                collection.insert_many(profiles_data)
                logger.info(f"Saved {len(profiles_data)} profiles to MongoDB")
            except Exception as e:
                logger.error(f"Error saving to MongoDB: {e}")
                # Fallback to file storage
                logger.info("Falling back to file storage...")
                self.use_file_storage = True
                self.save_to_file(profiles_data)
        else:
            # No database connection, use file storage
            logger.info("No database connection, using file storage...")
            self.use_file_storage = True
            self.save_to_file(profiles_data)
    
    def get_page(self, url):
        """Fetch a single page with error handling"""
        try:
            logger.info(f"Fetching: {url}")
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            time.sleep(self.delay)  # Be respectful to the server
            return response.text
        except requests.RequestException as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
    
    def find_pagination_links(self, soup, current_url):
        """Find pagination links - customize this based on the website's pagination structure"""
        pagination_links = []
        
        # Common pagination patterns - adjust these selectors based on the target website
        selectors = [
            'a[rel="next"]',  # Next link with rel attribute
            '.pagination a',  # Links in pagination class
            '.pager a',       # Links in pager class
            'a:contains("Next")',  # Links containing "Next" text
            'a:contains(">")',     # Links containing ">" symbol
            '.page-numbers a',     # WordPress style pagination
            '.pagination-next a',  # Another common pattern
        ]
        
        for selector in selectors:
            try:
                links = soup.select(selector)
                for link in links:
                    href = link.get('href')
                    if href:
                        # Convert relative URLs to absolute
                        full_url = urljoin(current_url, href)
                        pagination_links.append(full_url)
            except Exception as e:
                logger.debug(f"Error with selector {selector}: {e}")
        
        # Also look for numbered pagination
        page_links = soup.find_all('a', href=re.compile(r'page=\d+|p=\d+|/page/\d+'))
        for link in page_links:
            href = link.get('href')
            if href:
                full_url = urljoin(current_url, href)
                pagination_links.append(full_url)
        
        # Remove duplicates and return
        return list(set(pagination_links))
    
    def extract_name_from_article(self, article):
        """Extract name information from article element"""
        name_info = {
            'full_name': '',
            'first_name': '',
            'last_name': ''
        }
    
        try:
            # Look for the grid-item-link with title attribute
            link = article.find('a', class_='grid-item-link')
            if link and link.get('title'):
                title = link.get('title')
                
                # Extract name from title like "View full profile for Pichkar, Zina"
                if 'View full profile for' in title:
                    name_part = title.replace('View full profile for', '').strip()
                    
                    # Handle "Last, First" format
                    if ',' in name_part:
                        parts = name_part.split(',', 1)
                        last_name = parts[0].strip()
                        first_name = parts[1].strip() if len(parts) > 1 else ''
                        full_name = f"{first_name} {last_name}".strip()
                    else:
                        # Fallback if format is different
                        full_name = name_part.strip()
                        name_parts = full_name.split()
                        if len(name_parts) >= 2:
                            first_name = name_parts[0]
                            last_name = ' '.join(name_parts[1:])
                        else:
                            first_name = full_name
                            last_name = ''
                    
                    name_info.update({
                        'full_name': full_name,
                        'first_name': first_name,
                        'last_name': last_name
                    })
            
            # Alternative: try to find name in other elements if grid-item-link fails
            if not name_info['full_name']:
                # Look for other common name containers
                selectors = [
                    'h2 a',
                    'h3 a', 
                    '.name a',
                    '.profile-name a',
                    '.faculty-name a',
                    'a[href*="/people/"]'
                ]
                
                for selector in selectors:
                    element = article.select_one(selector)
                    if element:
                        text = element.get_text(strip=True)
                        if text:
                            name_info['full_name'] = text
                            # Try to split into first/last
                            parts = text.split()
                            if len(parts) >= 2:
                                name_info['first_name'] = parts[0]
                                name_info['last_name'] = ' '.join(parts[1:])
                            else:
                                name_info['first_name'] = text
                            break
                        
        except Exception as e:
            logger.debug(f"Error extracting name: {e}")
        
        return name_info
    
    def detect_pagination_pattern(self, url):
        """Detect pagination pattern and generate page URLs"""
        page_urls = []
        
        # Pattern 1: Query parameter (page=1, p=1, etc.)
        if '?' in url:
            base_url, params = url.split('?', 1)
            # Try incrementing page numbers
            for page_num in range(1, 51):  # Limit to reasonable number
                test_url = f"{base_url}?{params}&page={page_num}"
                page_urls.append(test_url)
                
                # Also try 'p' parameter
                test_url2 = f"{base_url}?{params}&p={page_num}"
                page_urls.append(test_url2)
        
        # Pattern 2: Path-based pagination (/page/1, /p/1, etc.)
        else:
            for page_num in range(1, 51):
                page_urls.extend([
                    f"{url}/page/{page_num}",
                    f"{url}/p/{page_num}",
                    f"{url}?page={page_num}",
                    f"{url}?p={page_num}"
                ])
        
        return page_urls
    
    def scrape_profile(self, profile_url):
        """Scrape individual profile page"""
        try:
            html = self.get_page(profile_url)
            if not html:
                return None
            
            soup = BeautifulSoup(html, "html.parser")
            
            # Your existing profile scraping logic
            target_divs = soup.find_all('div', class_="col-xs-12 col-sm-9 bio-btm-left")
            
            combined_text = []  # List to store all text chunks
            target_name = soup.find('div', class_="col-xs-12 col-sm-5 bio-top-left")

            if target_name:
                h1_element = target_name.find('h1')
            
            
            name = h1_element.get_text(strip=True)

            areasOfExpertise = soup.find("div", class_ = "col-xs-12 col-sm-6 bio-exp")
            if areasOfExpertise:
                ul_element = areasOfExpertise.find_all("ul")
                for item in ul_element:
                    text = item.get_text(strip = True)
                    if text:
                        combined_text.append("Areas of Expertise: ")

                        combined_text.append(text)
                        combined_text.append("  ")

            
            for div in target_divs:
                p_tags = div.find_all('p')  # find_all returns a list
                if p_tags:
                    for p in p_tags:
                        text = p.get_text(strip=True)
                        if text:  # Only add non-empty text
                            combined_text.append(" ")

                            combined_text.append(text)
                            combined_text.append(" ")
         
            
            
            
            # Join all text with spaces or newlines
            if combined_text:
                return {"about":  ''.join(combined_text),
                        "name": name
                                 
                                 
                                 
                                 }
                
            # Alternative selectors if the original doesn't work
            alternative_selectors = [
                '.biography p',
                '.about p',
                '.profile-description p',
                '.faculty-bio p',
                'div[class*="bio"] p',
                'div[class*="about"] p',
                
            ]
            
            for selector in alternative_selectors:
                elements = soup.select(selector)
                if elements:
                    about_text = elements[0].get_text(strip=True)
                    if about_text:
                        return about_text
            
            return None
            
        except Exception as e:
            logger.error(f"Error scraping profile {profile_url}: {e}")
            return None
    
    def scrape_directory_page(self, url):
        """Scrape a single directory page for faculty profiles"""
        html = self.get_page(url)
        if not html:
            return [], []
        
        soup = BeautifulSoup(html, "html.parser")
        profiles_data = []
        
        # # Your existing scraping logic for individual articles
        # articles = soup.find_all('a')
        # logger.info(f"Found {len(articles)} articles on page")
        
        # for article in articles:
        #     if article.has_attr('about'):
        #         about = article['about']
        #         profile_url = urljoin(self.base_url, about)
                
        #         # Extract name from the grid-item-link
        #         name_info = self.extract_name_from_article(article)
                
        #         # Check if profile already exists
        #         if self.profile_exists(about):
        #             logger.info(f"Profile {about} already exists, skipping")
        #             continue
                
        #         logger.info(f"Scraping profile: {profile_url} ({name_info['full_name']})")
        #         about_text = self.scrape_profile(profile_url)
                
        #         if about_text or name_info['full_name']:  # Save even if only name is found
        #             doc = {
        #                 "profile_path": about,
        #                 "profile_url": profile_url,
        #                 "full_name": name_info['full_name'],
        #                 "first_name": name_info['first_name'],
        #                 "last_name": name_info['last_name'],
        #                 "about_me": about_text
        #             }
        #             profiles_data.append(doc)
        #             logger.info(f"Successfully scraped profile: {about} - {name_info['full_name']}")
        #         else:
        #             logger.warning(f"No data found for profile: {profile_url}")
        
        # # Find pagination links for next pages
        pagination_links = self.find_pagination_links(soup, url)
     
  

        # Get all anchor tags
        all_links = soup.find_all('a')

        # Filter for people links
        people_links = []
        for link in all_links:
            href = link.get('href')
            if href and '/people/' in href:
                people_links.append(href)

        print(f"Found {len(people_links)} people links:")
        for link in people_links:
            person = urljoin(self.base_url, link)
            name = ""
            about = ""

            text = self.scrape_profile(person)
            if text:
                name = ""
                about = ""

            if text:
                if isinstance(text, dict):
                    # Main return path - dictionary with 'name' and 'about'
                    name = text.get('name', '')
                    about = text.get('about', '')
                else:
                    # Fallback return path - just a string (about text only)
                    about = str(text)
                    name = ""  # No name available from fallback

            doc = {
                "profile_path": person,
                "profile_url": person,
                "full_name": name,
                "about_me": about
            }
            doc = {
                        "profile_path": person,
                        "profile_url": person,
                        "full_name": name,
                        
                        "about_me": about
                    }
            print(doc)
            profiles_data.append(doc)
            logger.info(f"Successfully scraped profile: {person} - {name}")
            
            
        return profiles_data, pagination_links
    
    def scrape_all_pages(self, start_url, max_pages=None):
        """Scrape all pages starting from the given URL"""
        visited_urls = set()
        urls_to_visit = [start_url]
        all_profiles = []
        page_count = 0
        
        while urls_to_visit and (max_pages is None or page_count < max_pages):
            current_url = urls_to_visit.pop(0)
            
            if current_url in visited_urls:
                continue
            
            visited_urls.add(current_url)
            page_count += 1
            
            logger.info(f"Scraping page {page_count}: {current_url}")
            
            profiles_data, pagination_links = self.scrape_directory_page(current_url)
            
            if profiles_data:
                # Save to database or file
                self.save_profiles(profiles_data)
                all_profiles.extend(profiles_data)
            else:
                logger.info(f"No new profiles found on page {page_count}")
            
            # Add new pagination links to visit
            for link in pagination_links:
                if link not in visited_urls and link not in urls_to_visit:
                    urls_to_visit.append(link)
            
            # If no profiles found and no pagination links, we might be done
            if not profiles_data and not pagination_links:
                logger.info("No more profiles or pagination links found")
                break
        
        return all_profiles

def main():
    base_url = "https://linguistics.osu.edu/people"

    
    # Determine storage method based on database availability
    use_file_storage = collection is None
    if use_file_storage:
        logger.info("Using file storage (faculty_profiles.json)")
    else:
        logger.info("Using MongoDB storage")
    
    scraper = PaginatedScraper(base_url, delay=2, use_file_storage=use_file_storage)
    
    try:
        # Method 1: Start with known URL and follow pagination
        logger.info("Starting pagination scrape...")
        all_profiles = scraper.scrape_all_pages(base_url, max_pages=50)  # Limit to 50 pages
        
        # Method 2: If pagination isn't automatically detected, try common patterns
        if not all_profiles:
            logger.info("No profiles found with automatic pagination, trying manual patterns...")
            test_urls = scraper.detect_pagination_pattern(base_url)
            
            for url in test_urls[:10]:  # Test first 10 patterns
                profiles_data, _ = scraper.scrape_directory_page(url)
                if profiles_data:
                    scraper.save_profiles(profiles_data)
                    all_profiles.extend(profiles_data)
                    logger.info(f"Found profiles on: {url}")
                else:
                    break  # Stop if no profiles found
        
        logger.info(f"Scraping complete! Total profiles scraped: {len(all_profiles)}")
        
        if use_file_storage:
            logger.info(f"Data saved to: {scraper.file_storage_path}")
        
    except KeyboardInterrupt:
        logger.info("Scraping interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        if client is not None:
            client.close()

if __name__ == "__main__":
    main()