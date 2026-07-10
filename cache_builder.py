import os
import re
import csv
import argparse
import requests
from urllib.parse import urljoin, quote_plus
from PIL import Image
from bs4 import BeautifulSoup
from image_cache_utils import ALLOWED_IMAGE_EXTENSIONS, validate_cached_image
from path_config import PATHS

# --- Constants and Configuration ---
CACHE_DIRECTORY = str(PATHS.image_cache_dir)
SPECIES_FILE = str(PATHS.species_file)
IMAGES_PER_SPECIES = 3
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
}

# --- Helper Functions ---
def format_author_name(author_str):
    if not author_str: return ""
    cleaned_author = author_str.split('[a]')[0].strip()
    if len(cleaned_author) > 20:
        cut_off_point = cleaned_author.rfind(' ', 0, 20)
        return cleaned_author[:cut_off_point] + "..." if cut_off_point != -1 else cleaned_author[:20] + "..."
    return cleaned_author

def load_species_from_file(filename):
    """Loads a list of bird species from a CSV file (common_name, scientific_name)."""
    if not os.path.exists(filename): return []
    species_list = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 2 and row[0] and row[1]:
                    species_list.append((row[0].strip(), row[1].strip()))
        return species_list
    except (IOError, csv.Error) as e:
        print(f"Error reading or parsing species CSV file '{filename}': {e}")
        return []

# --- Web Scraping and Downloading ---
def _fetch_and_parse_wikimedia_search(search_query, num_images):
    """Helper function to perform a single search query on Wikimedia and parse results."""
    base_url = "https://commons.wikimedia.org"
    search_url = f"{base_url}/w/index.php?search={quote_plus(search_query)}&title=Special:MediaSearch&go=Go&type=image"
    try:
        response = requests.get(search_url, headers={'User-Agent': HEADERS['User-Agent']})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        result_elements = soup.select('a.sdms-image-result')
        image_data = []
        for result_a_tag in list(dict.fromkeys(result_elements))[:num_images]:
            file_page_url = urljoin(base_url, result_a_tag.get('href', ''))
            img_tag = result_a_tag.find('img')
            if not file_page_url or not img_tag or not img_tag.get('data-src'): continue
            thumbnail_url = img_tag['data-src']
            full_res_url = thumbnail_url.replace('/thumb', '').rsplit('/', 1)[0]
            try:
                page_response = requests.get(file_page_url, headers={'User-Agent': HEADERS['User-Agent']}, timeout=10)
                page_soup = BeautifulSoup(page_response.text, 'html.parser')
                attribution = "Wikimedia Commons"
                author_header = page_soup.find('td', string=re.compile(r'^\s*Author\s*$'))
                if author_header and author_header.find_next_sibling('td'):
                    attribution_cell = author_header.find_next_sibling('td')
                    attribution = attribution_cell.get_text(strip=True, separator=' ').split('(')[0].strip()
                formatted_attribution = format_author_name(attribution)
                final_attribution = f"© {formatted_attribution}" if formatted_attribution else "© Wikimedia Commons"
                image_data.append({'url': full_res_url, 'attribution': final_attribution})
            except requests.exceptions.RequestException: continue
        return image_data
    except requests.exceptions.RequestException as e:
        print(f"Error scraping Wikimedia for query '{search_query}': {e}")
        return []

def scrape_wikimedia_for_image_data(common_name, scientific_name, num_images):
    """Searches Wikimedia with a priority of queries to find the best quality images."""
    search_queries = [f"{common_name} {scientific_name} bird", f"{scientific_name} bird", f"{common_name} bird"]
    for query in search_queries:
        image_data = _fetch_and_parse_wikimedia_search(query, num_images)
        if image_data: return image_data
    return []

def download_image_and_attribution(image_info, folder_path, file_name_base):
    """Downloads an image and saves its attribution, skipping if files already exist."""
    if not os.path.exists(folder_path): os.makedirs(folder_path)
    file_ext = os.path.splitext(image_info['url'].split('(')[0])[-1] or ".jpg"
    image_file_path = os.path.join(folder_path, f"{file_name_base}{file_ext}")
    attr_file_path = os.path.join(folder_path, f"{file_name_base}.txt")
    if os.path.exists(image_file_path) and os.path.exists(attr_file_path):
        validation = validate_cached_image(image_file_path, CACHE_DIRECTORY)
        if validation.ok:
            return
        print(f"Existing cached image is invalid: {image_file_path}. Reason: {validation.reason}")
    try:
        image_response = requests.get(image_info['url'], timeout=15, headers={'User-Agent': HEADERS['User-Agent']})
        image_response.raise_for_status()
        with open(image_file_path, 'wb') as f: f.write(image_response.content)
        validation = validate_cached_image(image_file_path, CACHE_DIRECTORY)
        if not validation.ok:
            try:
                os.remove(image_file_path)
            except OSError:
                pass
            try:
                if os.path.exists(attr_file_path):
                    os.remove(attr_file_path)
            except OSError:
                pass
            print(f"Rejected downloaded image for {file_name_base}. Reason: {validation.reason}")
            return
        with open(attr_file_path, 'w', encoding='utf-8') as f: f.write(image_info['attribution'])
        print(f"Successfully cached {os.path.basename(image_file_path)}")
    except (requests.exceptions.RequestException, IOError) as e:
        print(f"Failed to download/save for {file_name_base}. Error: {e}")


def count_valid_cached_images(folder_path):
    valid_count = 0
    if not os.path.isdir(folder_path):
        return valid_count
    for filename in os.listdir(folder_path):
        if os.path.splitext(filename)[1].lower() not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        image_path = os.path.join(folder_path, filename)
        validation = validate_cached_image(image_path, CACHE_DIRECTORY)
        if validation.ok:
            valid_count += 1
        else:
            print(f"Skipping invalid cached image: {image_path}. Reason: {validation.reason}")
    return valid_count

# --- Main Cache Building Process ---
def ensure_cache_is_built():
    """Checks for and builds the offline image cache, skipping already completed species."""
    print("--- Checking local image cache... ---")
    bird_species_to_cache = load_species_from_file(SPECIES_FILE)
    if not bird_species_to_cache:
        print(f"WARNING: '{SPECIES_FILE}' not found or empty. Cannot build cache.")
        return

    for common_name, scientific_name in bird_species_to_cache:
        species_folder_name = "".join(c for c in common_name if c.isalnum() or c in ' _').rstrip().replace(' ', '_')
        species_folder_path = os.path.join(CACHE_DIRECTORY, species_folder_name)
        if os.path.isdir(species_folder_path):
            images_found = count_valid_cached_images(species_folder_path)
            if images_found >= IMAGES_PER_SPECIES:
                print(f"Cache for '{common_name}' is already complete ({images_found} images). Skipping.")
                continue
        image_infos = scrape_wikimedia_for_image_data(common_name, scientific_name, IMAGES_PER_SPECIES)
        if not image_infos: continue
        for i, info in enumerate(image_infos):
            download_image_and_attribution(info, species_folder_path, f"{species_folder_name}_{i+1}")
    print("--- Image cache check complete. ---")

def resize_cached_images():
    """Resizes large images in the cache to fit within a bounding box."""
    print("--- Checking and resizing large cached images... ---")
    target_width = 800
    target_height = 600
    for root, _, files in os.walk(CACHE_DIRECTORY):
        for file in files:
            if os.path.splitext(file)[1].lower() in ALLOWED_IMAGE_EXTENSIONS:
                image_path = os.path.join(root, file)
                validation = validate_cached_image(image_path, CACHE_DIRECTORY)
                if not validation.ok:
                    print(f"Could not resize {image_path}. Reason: {validation.reason}")
                    continue
                try:
                    with Image.open(image_path) as img:
                        w, h = img.size
                        if w <= target_width and h <= target_height: continue 
                        scale = min(target_width / w, target_height / h)
                        new_width = int(w * scale)
                        new_height = int(h * scale)
                        print(f"Downscaling {file} from {w}x{h} to {new_width}x{new_height}...")
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        resized_img.save(image_path)
                except Exception as e:
                    print(f"Could not resize {image_path}. Error: {e}")
    print("--- Image resizing complete. ---")

def parse_args():
    parser = argparse.ArgumentParser(description="Build the BirdNET Display image cache.")
    parser.add_argument(
        "--source",
        choices=("wikimedia", "avicommons"),
        default="wikimedia",
        help="Image source to use. Default keeps the existing Wikimedia cache builder.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Avicommons only: report downloads without writing files.")
    parser.add_argument("--size", type=int, default=480, help="Avicommons only: image size.")
    parser.add_argument("--limit", type=int, default=0, help="Avicommons only: max images to download.")
    parser.add_argument("--max-per-species", type=int, default=1, help="Avicommons only: max images per species.")
    parser.add_argument("--json-cache", default="", help="Avicommons only: local JSON cache path.")
    parser.add_argument("--refresh-json", action="store_true", help="Avicommons only: refresh local JSON cache.")
    parser.add_argument("--from-db", action="store_true", help="Avicommons only: read species from the BirdNET DB.")
    parser.add_argument("--from-species-list", action="store_true", help="Avicommons only: read species from species_list.csv.")
    parser.add_argument("--species", action="append", default=[], help="Avicommons only: limit to one common name. Repeatable.")
    parser.add_argument("--licenses", default="", help="Avicommons only: comma-separated license allowlist.")
    return parser.parse_args()


def run_avicommons_cache(args):
    from scripts.cache_avicommons_images import main as avicommons_main
    import sys

    argv = ["cache_avicommons_images.py"]
    if args.dry_run:
        argv.append("--dry-run")
    if args.refresh_json:
        argv.append("--refresh-json")
    if args.from_db:
        argv.append("--from-db")
    if args.from_species_list:
        argv.append("--from-species-list")
    if args.limit:
        argv.extend(["--limit", str(args.limit)])
    if args.max_per_species:
        argv.extend(["--max-per-species", str(args.max_per_species)])
    if args.json_cache:
        argv.extend(["--json-cache", args.json_cache])
    if args.size:
        argv.extend(["--size", str(args.size)])
    if args.licenses:
        argv.extend(["--licenses", args.licenses])
    for species in args.species:
        argv.extend(["--species", species])

    old_argv = sys.argv
    try:
        sys.argv = argv
        return avicommons_main()
    finally:
        sys.argv = old_argv


# This allows the script to be run directly from the command line
if __name__ == '__main__':
    args = parse_args()
    if args.source == "avicommons":
        raise SystemExit(run_avicommons_cache(args))

    os.makedirs(CACHE_DIRECTORY, exist_ok=True)
    print("--- Starting Offline Image Cache Builder ---")
    ensure_cache_is_built()
    resize_cached_images()
    print("--- Cache building process complete. ---")
